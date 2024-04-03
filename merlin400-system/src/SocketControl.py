import atexit
import ctypes
import enum
import os
import pickle
import socket
import sqlite3
import sys
import threading
import time
import signal
import subprocess
import traceback

from queue import Queue
from time import sleep
from datetime import datetime

from common import utils
from common.settings import HEARTBEAT_TIMEOUT_SECONDS, ALCOHOL_SENSOR_ENABLED
from hardware.module_HardwareControlSystem import (
    module_HardwareControlSystem,
    INIT_STATUS_OK, INIT_STATUS_USER_PANEL_ERROR, INIT_STATUS_PRESSURE_SENSOR_ERROR, HardwareFailure,
    PressureSensorFailure, UserPanelError, ElectricalError
)
from hardware.components.module_physicalinterface import module_physicalinterface
from hardware.components.module_fancontrol import NotSupportedFanError, module_fancontrol

from hardware.module_FSM import FailureMode
from common.module_logging import setup_logging, get_app_logger, flush_logger

if ALCOHOL_SENSOR_ENABLED:
    from hardware.components.module_alcoholsensor import module_alcoholsensor

RESET_COUNTER = 30
SELECT_COUNTER = 30
PLAY_COUNTER = 50
PAUSE_COUNTER_SHUTDOWN = 30
PAUSE_COUNTER_PRINT_LABEL = 10

class DeviceProgram(enum.IntEnum):
    PROGRAM01 = 1
    PROGRAM02 = 2
    PROGRAM03 = 3
    PROGRAM04 = 4
    PROGRAM05 = 5
    PROGRAM06 = 6
    PROGRAM07 = 7

class ControlThread(threading.Thread):
    """
    Controlthread is instatized from main and run as a separate thread. It
    uses a queue based system (Queue) to pass messages from the console to
    the ControlThread in a thread safe manner.
    """

    def __init__(self, name, in_queue, heartbeet, **kwargs):
        """
        Constructor method
        """

        if not isinstance(in_queue, Queue):
            raise AttributeError("Error, in_queue must be a Queue object")

        self._heartbeet = heartbeet
        self._heartbeet.set()

        # Initialize thread
        super().__init__(**kwargs)

        self._logger = get_app_logger(str(self.__class__))

        # Read fw version
        self.version = open("VERSION").readlines()[0].strip()

        # Store thread name
        self.name = name

        # store in_queue in local object
        self._in_queue = in_queue

        self.device_version = utils.get_device_version()

        # Initialize hardware here
        self._logger.info(
            "Initializing control system. Firmware version: {}".format(self.version)
        )
        try:
            self._myHardwareControlSystem = module_HardwareControlSystem(self.device_version)
            if self._myHardwareControlSystem.init_status == INIT_STATUS_PRESSURE_SENSOR_ERROR:
                raise PressureSensorFailure("Pressure sensor initialization problem")
            elif self._myHardwareControlSystem.init_status == INIT_STATUS_USER_PANEL_ERROR:
                raise UserPanelError("Failed to initialize user panel")
            elif self._myHardwareControlSystem.init_status != INIT_STATUS_OK:
                raise ElectricalError("I2C related error.")

        except PressureSensorFailure:
            self._logger.error("Pressure sensor initialization error. Entering error state...")
            self._myHardwareControlSystem.FSM.fsmData["failure_mode"] = FailureMode.PRESSURE_SENSOR_ERROR
            self._myHardwareControlSystem.FSM.ToTransistion("toStateError")

        except ElectricalError:
            self._logger.error("Electrical error. Entering error state...")
            self._myHardwareControlSystem.do_fast_blink()
            self._myHardwareControlSystem.FSM.ToTransistion("toStateError")

        except UserPanelError:
            self._logger.error("User panel error. Entering error state...")
            self._myHardwareControlSystem.do_slow_blink()
            self._myHardwareControlSystem.FSM.ToTransistion("toStateError")

        except HardwareFailure:
            self._logger.error("Hardware error. Entering error state...")
            self._myHardwareControlSystem.do_fast_blink()
            self._myHardwareControlSystem.FSM.ToTransistion("toStateError")

        else:
            self._myHardwareControlSystem.FSM.SetFSMData("start_flag", False)
            self._logger.info("Control system initialized")

        # Initialize variables required in thread here
        self._user_feedback = "Command ok"

        # Holds the last command from the user
        self._input_variable = False

        self._got_new_input_flag = False

        self._selected_program = 1

        # variable used to limit alcohol logging to once persecond
        self._last_log_second = datetime.now().timestamp()

        # counter that counts how many times we have seen a reset request
        self._reset_request_counter = 0

        # counter that counts how many times we have seen a select button request
        self._select_request_counter = 0

        # counter that counts how many times we have seen a pause button request in a row
        self._pause_request_counter = 0

        # counter that counts how many times we have seen a play button request in a row
        self._play_request_counter = 0

        # if True it indicates that an upgrade is in progress
        self._updating = False

        # distill runtime variables
        self._last_distill_runtime_total = 0.0
        self._distill_runtime_total = 0.0
        self._distill_mode = "distill"
        self._since_date = None

        # stats db connection.
        self.init_stats_db()
        self.load_total_run_minutes()

    def init_stats_db(self):
        with sqlite3.connect("stats.db") as conn:
            # TODO: consider populating table for aggregated results with trigger.
            conn.execute("create table if not exists stats_log(ts int, mode int, value real)")
            conn.execute("create table if not exists stats(date_since varchar, mode varchar, value real)")
            # If database was just created, initialize it.
            cur = conn.execute("select * from stats")
            row = cur.fetchone()
            if not row:
                with conn:
                    now = datetime.utcnow().date().strftime("%Y-%m-%d")
                    conn.execute("insert into stats values (?, ?, ?)", (now, self._distill_mode, 0))

    def load_total_run_minutes(self):
        with sqlite3.connect("stats.db") as conn:
            cur = conn.execute("select date_since, value from stats where mode = ?", (self._distill_mode,))
            row = cur.fetchone()
            since_date, value = row
            self._since_date = since_date
            self._distill_runtime_total = value
            self._last_distill_runtime_total = 0
            self._logger.info("Initialized device stats, total run minutes %s since %s.", value, since_date)

    def increment_run_counters(self, value):
        if value > 0:
            # update distill runtime total
            delta = value - self._last_distill_runtime_total
            self._last_distill_runtime_total += delta
            self._distill_runtime_total += delta
            with sqlite3.connect("stats.db") as conn:
                conn.execute("update stats set value = ? where mode = ?", (self._distill_runtime_total, self._distill_mode))
                # Incrementally update stats log. This way we keep history of updates and we can double check value
                # in stats table. This also gives us the history of distill runs.
                if delta > 0:
                    now = datetime.utcnow().timestamp()
                    # Hard coding mode = 1 for distill process. For now there is no need to support other modes.
                    # but we'll be able to do that without schema change.
                    conn.execute("insert into stats_log values (?, ?, ?)", (int(now), 1, int(delta)))

    def check_fan_is_off(self):
        try:
            self._logger.info("Checking fan status on boot.")
            status = self._myHardwareControlSystem._fan_control.fan_adc_check
            # This check expects that fan is off as it's performed on device start.
            if status != module_fancontrol.FAN_ADC_LEVEL_OFF:
                self._logger.info("Reading fan status as {}.".format(status))
                self._myHardwareControlSystem._myphysicalinterface.set_state(
                    module_physicalinterface.DeviceState.ERROR
                )
                self._myHardwareControlSystem.FSM.fsmData["failure_mode"] = FailureMode.FAN_ERROR
                self._myHardwareControlSystem.FSM.ToTransistion("toStateError")
                self._myHardwareControlSystem.FSM.fsmData["failure_description"] = (
                    "Error, air fan seems to be defective. Please try again and if it still fails, contact drizzle "
                    "support."
                )
        except NotSupportedFanError:
            # This exception means that device does't support fan and there is nothing to do.
            self._logger.info("Fan not supported on this hardware.")

    def reset_session_counter(self):
        self._last_distill_runtime_total = 0

    def _handle_aws_iot_message(self, message):
        action = message.get("action")
        program = message.get("program")
        program_params = message.get("programParameters", {})
        self._logger.info("Received action {}, program {}, program params {}".format(action, program, program_params))

        device_is_paused = self._myHardwareControlSystem.FSM.fsmData["pause_flag"]
        device_is_running = self._myHardwareControlSystem.FSM.fsmData["running_flag"]
        user_initials = ""

        if (
            program_params
            and "soakTime" in program_params
            and program_params["soakTime"]
            and not device_is_paused
            and not device_is_running
        ):
            try:
                soak_time = int(program_params["soakTime"])
            except (ValueError, TypeError):
                soak_time = None

            if soak_time is not None:
                self._myHardwareControlSystem._config["SYSTEM"]["soak_time_seconds"] = str(soak_time)
                self._myHardwareControlSystem.store_config()
                self._logger.info("Setting soak time to {} seconds.".format(program_params["soakTime"]))

        if program_params and "initials" in program_params and program_params["initials"]:
            user_initials = program_params["initials"]

        if action == "upgrade":
            try:
                self._logger.info("Updating software...")
                self._updating = True
                # flash all LED's to indicate an update in progress
                self._myHardwareControlSystem._myphysicalinterface.set_state(
                    module_physicalinterface.DeviceState.UPDATING
                )
                utils.update()
                self._updating = False
                self._logger.info("Software update completed.")
            except Exception as e:
                self._updating = False
                self._logger.exception("Software update failed:")
                return

        if action == "sendlog":
            def callback():
                initial_state = self._myHardwareControlSystem.FSM.curHandle
                self._myHardwareControlSystem._myphysicalinterface.do_flash_green_3sec()
                if initial_state == "Ready":
                    self._myHardwareControlSystem._myphysicalinterface.set_state(
                        module_physicalinterface.DeviceState.READY, force_physical_update=True
                    )
                self._logger.info("Logs to the AWS has been sent.")

            try:
                self._logger.info("Sending log to AWS...")
                self._myHardwareControlSystem._myphysicalinterface.do_red_light()
                flush_logger()
                utils.upload_log_files(user_initials=user_initials, problem=message.get("problemDescription", ""), callback=callback)
            except Exception as e:
                self._logger.exception("Failed to send logs to AWS:")
                return

        # In order to start program device should not be already running and should not be paused.
        if action == "start" and program and not device_is_paused and not device_is_running:
            try:
                program_id = DeviceProgram[program.upper()]
                self._selected_program = program_id.value
                if self._selected_program == 1:
                    # start extraction
                    self._logger.debug("starting extraction")
                    self.start_extraction()
                elif self._selected_program == 2:
                    # start decarb
                    self._logger.debug("starting decarb")
                    self.start_decarb()
                elif self._selected_program == 3:
                    # start heatoil
                    self._logger.debug("starting heatoil")
                    self.start_heatoil()
                elif self._selected_program == 4:
                    # start just distilling
                    self._logger.debug("starting distill")
                    self.start_distill()
                elif self._selected_program == 5:
                    # start just extracting
                    self._logger.debug("starting distill")
                    self.start_just_extract()
                elif self._selected_program == 6:
                    # vent valves
                    self._logger.debug("starting venting of pump")
                    self.start_vent_pump()
                elif self._selected_program == 7:
                    # vent valves
                    self._logger.debug("starting cleaning pump")
                    self.start_clean_pump()
                else:
                    self._logger.debug("Unknown program")

            except ValueError:
                self._logger.error("Invalid command {}.".format(program))

        if action == "stop":
            # reset machine
            self.reset_machine()

        if action == "pause" and not device_is_paused and device_is_running:
            self.pause_machine()

        if (action == "start" and device_is_paused and device_is_running) or (action == "resume" and device_is_paused and device_is_running):
            self.unpause_machine()

        if action == "reset":
            self.reset_machine()

        if action == "clean_valve1" and self._myHardwareControlSystem.FSM.curHandle == "Ready":
            self.clean_valve_1()

        if action == "clean_valve2" and self._myHardwareControlSystem.FSM.curHandle == "Ready":
            self.clean_valve_2()

        if action == "clean_valve3" and self._myHardwareControlSystem.FSM.curHandle == "Ready":
            self.clean_valve_3()

        if action == "clean_valve4" and self._myHardwareControlSystem.FSM.curHandle == "Ready":
            self.clean_valve_4()

        self._logger.info(
            "Finished handling action {}, program {}".format(action, program)
        )

    def _get_id(self):
        """
        Get the thread id
        """
        # returns id of the respective thread
        if hasattr(self, "_thread_id"):
            return self._thread_id
        for id, thread in threading._active.items():
            if thread is self:
                return id

    def raise_exception(self):
        """
        Method for killing thread
        """
        thread_id = self._get_id()
        res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
            thread_id, ctypes.py_object(SystemExit)
        )
        if res > 1:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(thread_id, 0)
            self._logger.error("Exception raise failure")

    def get_unique_id(self):
        if not self._device_id:
            self._device_id = utils.get_unique_id()
        return self._device_id


    def show_connectivity(self):
        self._myHardwareControlSystem._myphysicalinterface.do_disconnected_flash()

    def reset_machine(self):
        self._logger.info("Resetting!!!")
        self._user_feedback = "Command ok, resetting FSM"
        # flash to indicated actual reset
        self._myHardwareControlSystem._myphysicalinterface.do_reset_flash()
        self._myHardwareControlSystem.set_valve("valve1", 0)
        self._myHardwareControlSystem.set_valve("valve4", 100)
        self._myHardwareControlSystem.set_valve("valve3", 100)
        self._myHardwareControlSystem.set_valve("valve2", 100)

        self._myHardwareControlSystem.bottom_heater_power = 0
        self._myHardwareControlSystem.PID_off()
        self._myHardwareControlSystem.fan_value = 0
        self._myHardwareControlSystem.init_FSM()
        self._myHardwareControlSystem.init_config()
        self._myHardwareControlSystem.light_off()
        self._reset_request_counter = 0
        self._select_request_counter = 0
        self._pause_request_counter = 0
        self._play_request_counter = 0
        self._myHardwareControlSystem._myphysicalinterface.set_program_and_state(
            1, module_physicalinterface.DeviceState.READY
        )
        self._selected_program = 1
        self._logger.info("Resetting completed.")

    def pause_machine(self):
        self._logger.debug("User pressed pause from app")
        # may only run if state is DistillBulk, otherwise just ignore or is in pause mode
        if self._myHardwareControlSystem.FSM.curHandle != "DistillBulk":
            self._logger.debug("Machine is in wrong mode, ignore pause request")
            return
        self._myHardwareControlSystem.FSM.SetFSMData("pause_flag", True)
        self._myHardwareControlSystem._myphysicalinterface.set_state(
            module_physicalinterface.DeviceState.PAUSE
        )
        self._myHardwareControlSystem.FSM.machine.set_PID_target(0)
        self._myHardwareControlSystem.FSM.machine.pump_value = 0

    def unpause_machine(self):
        self._logger.debug("User pressed resume from app")
        # may only run if state is DistillBulk, otherwise just ignore or is in pause mode
        if self._myHardwareControlSystem.FSM.curHandle != "DistillBulk":
            self._logger.debug("Machine is in wrong mode, ignore resume request")
            return
        self._myHardwareControlSystem.FSM.SetFSMData("pause_flag", False)
        self._myHardwareControlSystem._myphysicalinterface.set_state(
            module_physicalinterface.DeviceState.RUNNING_PAUSE_ENABLED
        )

    def check_for_button_reset(self):
        if self._reset_request_counter > RESET_COUNTER:
            # user requested reset
            self.reset_machine()

    def check_for_button_select(self):
        # print('Buttons select counter: {}'.format(self._select_request_counter))
        if self._select_request_counter > SELECT_COUNTER:
            self._logger.debug("Showing connectivity")
            # user requested display of wifi status
            self.show_connectivity()

            self._select_request_counter = 0

    def check_for_button_play(self):
        if self._play_request_counter > PLAY_COUNTER:
            self._logger.debug("Forcing afterstill")
            self._myHardwareControlSystem._myphysicalinterface.do_force_afterstill_blink()
            self._myHardwareControlSystem.FSM.SetFSMData("force_afterstill", True)

            self._play_request_counter = 0

    def connected_to_drizzle_wifi(self):
        _is_connected_to_drizzle_wifi = 'DrizzleRaspberry' in subprocess.run(['iwgetid'], stdout=subprocess.PIPE).stdout.decode('utf-8')
        return _is_connected_to_drizzle_wifi

    def check_for_button_pause(self):
        # If connected to our wifi: Press and hold for one second: print a label, blink green untill button is released.
        if self._pause_request_counter > PAUSE_COUNTER_PRINT_LABEL and self.connected_to_drizzle_wifi():
            self._logger.debug("Pause button is pressed for 1 second, printing label.")
            self._myHardwareControlSystem._myphysicalinterface.do_print_label_blink()
            self.print_label()
            self._pause_request_counter = 0

    def start_extraction(self):
        self._myHardwareControlSystem._myphysicalinterface.set_state(
            module_physicalinterface.DeviceState.RUNNING_PAUSE_DISABLED
        )
        self._myHardwareControlSystem.FSM.SetFSMData("run_full_extraction", 1)
        self._myHardwareControlSystem.FSM.SetFSMData("running_flag", True)
        self._myHardwareControlSystem.FSM.SetFSMData("start_flag", True)

    def start_decarb(self):
        self._myHardwareControlSystem._myphysicalinterface.set_state(
            module_physicalinterface.DeviceState.RUNNING_PAUSE_DISABLED
        )
        self._user_feedback = "Command ok, Decarboxylating"
        self._myHardwareControlSystem.FSM.SetFSMData("running_flag", True)
        self._myHardwareControlSystem.FSM.ToTransistion("toStateDecarb")

    def start_heatoil(self):
        self._user_feedback = "Command ok, Mixing oil"
        self._myHardwareControlSystem._myphysicalinterface.set_state(
            module_physicalinterface.DeviceState.RUNNING_PAUSE_DISABLED
        )
        self._myHardwareControlSystem.FSM.SetFSMData("running_flag", True)
        self._myHardwareControlSystem.FSM.ToTransistion("toStateMixOil")

    def start_distill(self):
        self._user_feedback = "Command ok, distilling"
        self.reset_session_counter()
        self._myHardwareControlSystem._myphysicalinterface.set_state(
            module_physicalinterface.DeviceState.RUNNING_PAUSE_ENABLED
        )
        self._myHardwareControlSystem.FSM.SetFSMData("running_flag", True)
        self._myHardwareControlSystem.FSM.SetFSMData("run_full_extraction", 0)
        self._myHardwareControlSystem.FSM.ToTransistion("toStateDistillBulk")

    def start_just_extract(self):
        self._user_feedback = "Command ok, starting extract only"
        self._myHardwareControlSystem._myphysicalinterface.set_state(
            module_physicalinterface.DeviceState.RUNNING_PAUSE_ENABLED
        )
        self._myHardwareControlSystem.FSM.SetFSMData("run_full_extraction", 0)
        self._myHardwareControlSystem.FSM.SetFSMData("start_flag", True)

    def start_vent_pump(self):
        self._user_feedback = "Command ok, venting valves"
        self._myHardwareControlSystem._myphysicalinterface.set_state(
            module_physicalinterface.DeviceState.RUNNING_PAUSE_ENABLED
        )
        self._myHardwareControlSystem.FSM.SetFSMData("running_flag", True)
        self._myHardwareControlSystem.FSM.ToTransistion("toStateVentPump")

    def start_clean_pump(self):
        self._user_feedback = "Command ok, cleaning pump"
        self._myHardwareControlSystem._myphysicalinterface.set_state(
            module_physicalinterface.DeviceState.RUNNING_PAUSE_ENABLED
        )
        self._myHardwareControlSystem.FSM.SetFSMData("running_flag", True)
        self._myHardwareControlSystem.FSM.SetFSMData("run_full_extraction", 0)
        self._myHardwareControlSystem.FSM.ToTransistion("toStateCleanPump")

    #Sets all valves, such that valve 1 may be cleaned by the user
    def clean_valve_1(self):
        self._logger.info("Setting valves positions to clean valve1")
        self._myHardwareControlSystem.set_valve("valve1", 100)
        self._myHardwareControlSystem.set_valve("valve2", 0)
        self._myHardwareControlSystem.set_valve("valve3", 0)
        self._myHardwareControlSystem.set_valve("valve4", 0)

    #Sets all valves, such that valve 2 may be cleaned by the user
    def clean_valve_2(self):
        self._logger.info("Setting valves positions to clean valve2")
        self._myHardwareControlSystem.set_valve("valve1", 0)
        self._myHardwareControlSystem.set_valve("valve2", 100)
        self._myHardwareControlSystem.set_valve("valve3", 0)
        self._myHardwareControlSystem.set_valve("valve4", 0)

    #Sets all valves, such that valve 3 may be cleaned by the user
    def clean_valve_3(self):
        self._logger.info("Setting valves positions to clean valve3")
        self._myHardwareControlSystem.set_valve("valve1", 0)
        self._myHardwareControlSystem.set_valve("valve2", 0)
        self._myHardwareControlSystem.set_valve("valve3", 100)
        self._myHardwareControlSystem.set_valve("valve4", 0)

    #Sets all valves, such that valve 4 may be cleaned by the user
    def clean_valve_4(self):
        self._logger.info("Setting valves positions to clean valve4")
        self._myHardwareControlSystem.set_valve("valve1", 0)
        self._myHardwareControlSystem.set_valve("valve2", 0)
        self._myHardwareControlSystem.set_valve("valve3", 0)
        self._myHardwareControlSystem.set_valve("valve4", 100)

    def check_alcohol_level(self):
        # ALCOHOL CHECK START
        # check ambient alcohol levels
        _previous_alcohol_level = None
        alcohol_level = None
        try:
            if self._myHardwareControlSystem.FSM.curHandle != "Error":
                alcohol_level = self._myHardwareControlSystem.alcohol_level
        except HardwareFailure:
            self._logger.error("Electrical error. Entering error state...")
            self._myHardwareControlSystem.do_fast_blink()
            self._myHardwareControlSystem.FSM.ToTransistion("toStateError")

        # print("Alcohol level message: {}".format(alcohol_level.value))
        # print("Raw alcohol level: {}".format(s))
        # _previous_alcohol_level = alcohol_level

        # log the alcohol level
        if hasattr(self._myHardwareControlSystem, "myalcoholdatalogger"):
            # datalogging is on
            # log once per second
            if self._last_log_second != int(datetime.now().timestamp()):
                self._last_log_second = int(datetime.now().timestamp())
                logdate = "{:%Y-%m-%d-%H:%M:%S}".format(datetime.now())

                data = {
                    "Time": logdate,
                    "AlcoholLevel": self._myHardwareControlSystem.alcohol_level_raw,
                }
                self._myHardwareControlSystem.myalcoholdatalogger.append_data(
                    data
                )

        if alcohol_level is module_alcoholsensor.AlcoholLevelMessage.DANGER:
            self._logger.warning("Alcohol level critical - stop now!")

            try:
                self._myHardwareControlSystem._mypump.pump_pwm = 0
            except Exception as error:
                self._logger.exception("Failed to shutdown pump:")

            try:
                self._myHardwareControlSystem.bottom_heater_power = 0
            except Exception as error:
                self._logger.exception("Failed to shutdown heater:")

            try:
                self._myHardwareControlSystem._valve_controller.shutdown()
            except Exception as error:
                self._logger.exception("Failed to shutdown valves:")

            try:
                self._myHardwareControlSystem._myalcoholsensor.shutdown()
            except Exception as error:
                self._logger.exception("Failed to shutdown alcohol sensor:")

            # set failure mode
            self._myHardwareControlSystem.FSM.fsmData[
                "failure_mode"
            ] = FailureMode.ALCOHOL_GASLEVEL_ERROR
            # display failure mode in display
            # self._myHardwareControlSystem.show_error_code_in_display()
            # Transition to Error state.
            self._myHardwareControlSystem.FSM.ToTransistion("toStateError")

        # # ALCOHOL CHECK STOP

    def check_PhysicalInterface(self):
        # check for system update in progress
        if self._updating:
            return

        # if there has been a reset request recheck it!
        if self._reset_request_counter > 0:
            button_press = self._myHardwareControlSystem.button_press_force
            if button_press is module_physicalinterface.ButtonPressed.RESET:
                self._reset_request_counter += 1

                # check if it is time for a reset
                self.check_for_button_reset()
                return
            else:
                self._reset_request_counter = 0
                self._myHardwareControlSystem._myphysicalinterface.set_state(
                    self._last_display_state
                )

        # if there has been a wifi connectivity request (select) recheck it!
        if self._select_request_counter > 0:
            # print('Select counter: {}'.format(self._select_request_counter))
            button_press = self._myHardwareControlSystem.button_press_force
            if button_press is module_physicalinterface.ButtonPressed.SELECT:
                self._select_request_counter += 1
                # check if it is time for a display of connectivity
                self.check_for_button_select()
                return
            else:
                self._select_request_counter = 0

        # if there has been a print labe request (select) recheck it!
        if self._pause_request_counter > 0:
            # print('Select counter: {}'.format(self._select_request_counter))
            button_press = self._myHardwareControlSystem.button_press_force
            if button_press is module_physicalinterface.ButtonPressed.PAUSE:
                self._pause_request_counter += 1
                # check if it is time for a display of connectivity
                self.check_for_button_pause()
                return
            else:
                self._pause_request_counter = 0

        # if there has been a force afterstill press - recheck it!
        if self._play_request_counter > 0:
            button_press = self._myHardwareControlSystem.button_press_force
            if button_press is module_physicalinterface.ButtonPressed.PLAY:
                self._play_request_counter += 1
                # check if it is time for a display of connectivity
                self.check_for_button_play()
                return
            else:
                self._play_request_counter = 0

        button_press = self._myHardwareControlSystem.button_press

        if button_press is module_physicalinterface.ButtonPressed.SELECT:
            # may only run if state is ready, otherwise just ignore
            if self._myHardwareControlSystem.FSM.curHandle != "Ready":
                return

            self._select_request_counter += 1
            self._logger.debug("User pressed select")
            self._selected_program += 1
            if self._selected_program > 4:
                self._selected_program = 1
            self._myHardwareControlSystem._myphysicalinterface.set_program(
                self._selected_program
            )
            self._myHardwareControlSystem._myphysicalinterface.set_state(
                module_physicalinterface.DeviceState.READY
            )

        elif button_press is module_physicalinterface.ButtonPressed.PLAY:
            self._logger.debug("User pressed play")
            # may only run if state is ready, otherwise just ignore or is in pause mode
            if (
                self._myHardwareControlSystem.FSM.curHandle != "Ready"
                and self._myHardwareControlSystem.FSM.curHandle not in ("DistillBulk", "CleanPump")
            ):
                # Machine is running, use the buttonpress to toggle LED state
                self._myHardwareControlSystem.toggle_light()
                return

            if self._myHardwareControlSystem.FSM.curHandle == "Ready":
                if self._selected_program == 1:
                    self._logger.debug("Extrating")
                    self.start_extraction()

                elif self._selected_program == 2:
                    self._logger.debug("Decarboxylating")
                    self.start_decarb()

                elif self._selected_program == 3:
                    self._logger.debug("Heating oil")
                    self.start_heatoil()

                elif self._selected_program == 4:
                    self._logger.debug("Distilling")
                    self.start_distill()

                else:
                    self._logger.debug("Invalid program %r, ignoring...", self._selected_program)
                    return

            elif self._myHardwareControlSystem.FSM.curHandle in ("DistillBulk", "CleanPump"):
                # if distillation is running, toggle white light
                self._play_request_counter += 1
                if not self._myHardwareControlSystem.FSM.fsmData["pause_flag"]:
                    self._myHardwareControlSystem.toggle_light()

                # restart distillation
                self._myHardwareControlSystem.FSM.SetFSMData("pause_flag", False)
            else:
                self._user_feedback = "Error, wrong state for decarb run"
            # self._myHardwareControlSystem._myphysicalinterface.set_state(module_physicalinterface.STATE_RUNNING_PAUSE_ENABLED)

        elif button_press is module_physicalinterface.ButtonPressed.PAUSE:
            self._pause_request_counter += 1
            self._logger.debug("User pressed pause")
            # may only run if state is ready, otherwise just ignore or is in pause mode
            if self._myHardwareControlSystem.FSM.curHandle not in ("DistillBulk", "CleanPump"):
                self._logger.debug("Machine is in wrong mode, ignore button press")
                return
            self._myHardwareControlSystem.FSM.SetFSMData("pause_flag", True)
            self._myHardwareControlSystem._myphysicalinterface.set_state(
                module_physicalinterface.DeviceState.PAUSE
            )

        elif button_press is module_physicalinterface.ButtonPressed.RESET:
            self._logger.debug("User pressed reset")
            # store existing state
            self._last_display_state = (
                self._myHardwareControlSystem._myphysicalinterface._state
            )
            self._reset_request_counter += 1
            self._myHardwareControlSystem._myphysicalinterface.set_state(
                module_physicalinterface.DeviceState.RESET_WARNING
            )

    def update_PhysicalUI(self):
        if self._reset_request_counter > 0:
            return
        if self._updating:
            return

        if not self._myHardwareControlSystem._myphysicalinterface:
            return

        if (
            self._myHardwareControlSystem.FSM.curHandle == "Ready"
            and not self._myHardwareControlSystem.FSM.fsmData["running_flag"]
        ):
            self._myHardwareControlSystem._myphysicalinterface.set_state(
                module_physicalinterface.DeviceState.READY
            )
        elif self._myHardwareControlSystem.FSM.curHandle == "Error":
            self._myHardwareControlSystem._myphysicalinterface.set_state(
                module_physicalinterface.DeviceState.ERROR
            )
        elif self._myHardwareControlSystem.FSM.curHandle == "DistillBulk":
            if self._myHardwareControlSystem.FSM.fsmData["pause_flag"]:
                self._myHardwareControlSystem._myphysicalinterface.set_state(
                    module_physicalinterface.DeviceState.PAUSE
                )
            else:
                self._myHardwareControlSystem._myphysicalinterface.set_state(
                    module_physicalinterface.DeviceState.RUNNING_PAUSE_ENABLED
                )
        elif self._myHardwareControlSystem.FSM.curHandle == "CleanPump":
            if self._myHardwareControlSystem.FSM.fsmData["pause_flag"]:
                self._myHardwareControlSystem._myphysicalinterface.set_state(
                    module_physicalinterface.DeviceState.PAUSE
                )
            else:
                self._myHardwareControlSystem._myphysicalinterface.set_state(
                    module_physicalinterface.DeviceState.RUNNING_PAUSE_ENABLED
                )
        else:
            self._myHardwareControlSystem._myphysicalinterface.set_state(
                module_physicalinterface.DeviceState.RUNNING_PAUSE_DISABLED
            )

    def parse_UserInput(self):

        # Check for a new command
        if self._got_new_input_flag:
            self._got_new_input_flag = False
        else:
            return

        # split input from user
        if self._input_variable is None:
            return

        myInput = self._input_variable.split(":")
        self._logger.info("Received user input {!r}".format(myInput))

        # check first statement
        if "valve" in myInput[0]:
            try:
                self._logger.debug("Input: {}".format(myInput[0].upper()))
                valve = self._myHardwareControlSystem._valves[myInput[0].upper()]
                valve_position = int(myInput[1])
                self._user_feedback = "Command ok, {} is {}".format(
                    valve, valve_position
                )
                self._myHardwareControlSystem.set_valve(valve, valve_position)
            except Exception:
                self._user_feedback = "Error parsing valve data"
        elif "pump" in myInput[0]:
            try:
                pump_power = int(myInput[1])
                self._myHardwareControlSystem.pump_value = pump_power
                self._user_feedback = "Command ok, pump power is {}".format(pump_power)
            except Exception:
                self._user_feedback = "Error parsing pump data"
        elif "run_ex" in myInput[0]:
            if self._myHardwareControlSystem.FSM.curHandle == "Ready":
                if (
                    (
                        self._myHardwareControlSystem.FSM.fsmData[
                            "aspirate_speed_target"
                        ]
                        > 0.1
                    )
                    and (
                        self._myHardwareControlSystem.FSM.fsmData[
                            "aspirate_speed_target"
                        ]
                        < 50
                    )
                    and (
                        self._myHardwareControlSystem.FSM.fsmData[
                            "aspirate_volume_target"
                        ]
                        > 1
                    )
                    and (
                        self._myHardwareControlSystem.FSM.fsmData[
                            "aspirate_volume_target"
                        ]
                        < 400
                    )
                ):
                    self._user_feedback = "Command ok, aspirate started"
                    self._myHardwareControlSystem.FSM.SetFSMData(
                        "run_full_extraction", 0
                    )
                    self._myHardwareControlSystem.FSM.SetFSMData("start_flag", True)
                else:
                    self._user_feedback = "Error in run parameters, please check!"
            else:
                self._user_feedback = "Error, system is invalid state"
        elif "run_ev" in myInput[0]:
            if self._myHardwareControlSystem.FSM.curHandle == "Ready":
                if (self._myHardwareControlSystem.FSM.fsmData["target_temp"] >= 0) and (
                    self._myHardwareControlSystem.FSM.fsmData["target_temp"] <= 160
                ):
                    self._user_feedback = "Command ok, distillation started"
                    self._myHardwareControlSystem.FSM.SetFSMData(
                        "run_full_extraction", 0
                    )
                    self._myHardwareControlSystem.FSM.ToTransistion(
                        "toStateDistillBulk"
                    )
                else:
                    self._user_feedback = "Error in run parameters, please check!"
            else:
                self._user_feedback = "Error, system is invalid state"
        elif "drain" in myInput[0]:
            self._user_feedback = "Command ok, draining"
            self._myHardwareControlSystem.drain_system()
        elif "manual_flush" in myInput[0]:
            self._user_feedback = "Command ok, flush"
            # self._myHardwareControlSystem.flush_system()
            self._myHardwareControlSystem.FSM.ToTransistion("toStateFlush")
        elif "reset" in myInput[0]:
            self._user_feedback = "Command ok, resetting FSM"
            self.reset_machine()
        elif "vent" in myInput[0]:
            self._user_feedback = "Command ok, venting pump"
            self.start_vent_pump()
        elif "set_temp" in myInput[0]:
            if "off" in myInput[1] or myInput[1] == "0":
                # turn off heater
                target_temp = 0
                self._user_feedback = "Command ok, PID target set to {}".format(
                    target_temp
                )
                self._myHardwareControlSystem.FSM.SetFSMData("target_temp", target_temp)
                self._myHardwareControlSystem._PID.PID_running = False
                self._myHardwareControlSystem._PID.reset()
            else:
                try:
                    target_temp = float(myInput[1])
                except Exception:
                    self._user_feedback = "Error parsing temperature"

                if (
                    target_temp
                    <= float(
                        self._myHardwareControlSystem._config["FSM_EV"]["max_temp"]
                    )
                ) and (
                    target_temp
                    >= float(
                        self._myHardwareControlSystem._config["FSM_EV"]["min_temp"]
                    )
                ):
                    self._user_feedback = "Command ok, PID target set to {}".format(
                        target_temp
                    )
                    self._myHardwareControlSystem.FSM.SetFSMData(
                        "target_temp", target_temp
                    )
                    self._myHardwareControlSystem._PID.PID_running = True
                else:
                    self._user_feedback = (
                        "Error - temperature outside {} to {} degrees limit".format(
                            self._myHardwareControlSystem._config["FSM_EV"]["min_temp"],
                            self._myHardwareControlSystem._config["FSM_EV"]["max_temp"],
                        )
                    )
        elif "auto_flush" in myInput[0]:
            try:
                if myInput[1] == "yes":
                    self._user_feedback = "OK, flushing after aspiration"
                    self._myHardwareControlSystem.FSM.SetFSMData("auto_flush", 1)
                elif myInput[1] == "no":
                    self._user_feedback = "OK, not flushing after aspiration"
                    self._myHardwareControlSystem.FSM.SetFSMData("auto_flush", 0)
                else:
                    self._user_feedback = "Error, invalid auto_flush selection. Valid options are yes or no"
            except Exception:
                self._user_feedback = "Error parsing paramters"
        elif "fan" in myInput[0]:
            try:
                fan_value = int(myInput[1])
            except Exception:
                self._user_feedback = (
                    "Error parsing fan setting, integers between 0 and 100 only!"
                )

            if (fan_value >= 0) and (fan_value <= 100):
                self._user_feedback = "Command ok, fan set to to {}".format(fan_value)
                self._myHardwareControlSystem.FSM.machine.fan_value = fan_value
            else:
                self._user_feedback = (
                    "Error - temperature outside {} to {} degrees limit".format(
                        self._myHardwareControlSystem._config["FSM_EV"]["min_temp"],
                        self._myHardwareControlSystem._config["FSM_EV"]["max_temp"],
                    )
                )
        elif "extract" in myInput[0]:
            self._user_feedback = "OK, starting full extraction"
            self._myHardwareControlSystem.FSM.SetFSMData("run_full_extraction", 1)
            self._myHardwareControlSystem.FSM.SetFSMData("start_flag", True)
        elif "decarb" in myInput[0]:
            if self._myHardwareControlSystem.FSM.curHandle == "Ready":
                self._user_feedback = "Command ok, Decarboxylating"
                self._myHardwareControlSystem.FSM.ToTransistion("toStateDecarb")
            else:
                self._user_feedback = "Error, wrong state for decarb run"
        elif "mixoil" in myInput[0]:
            if self._myHardwareControlSystem.FSM.curHandle == "Ready":
                self._user_feedback = "Command ok, Mixing oil"
                self._myHardwareControlSystem.FSM.ToTransistion("toStateMixOil")
            else:
                self._user_feedback = "Error, wrong state for mixoil"
        elif "print_label" in myInput[0]:
            self.print_label()
            self._user_feedback = "Command ok, printing label"
        elif "flash" in myInput[0]:
            self._myHardwareControlSystem._myphysicalinterface.do_flash_green()
            self._user_feedback = "Command ok, flashing done"
        elif "go_to_error" in myInput[0]:
            self._myHardwareControlSystem._myphysicalinterface.do_red_light()
            self._myHardwareControlSystem.FSM.fsmData["failure_mode"] = FailureMode.UNKNOWN_ERROR
            self._myHardwareControlSystem.FSM.ToTransistion("toStateError")
            self._user_feedback = "Command ok, entered error state"
        elif "number_of_flushes" in myInput[0]:
            try:
                number_of_flushes = int(myInput[1])
                self._myHardwareControlSystem._config["FSM_EV"]["number_of_flushes"] = str(number_of_flushes)
                self._myHardwareControlSystem.store_config()
                self._user_feedback = "Command ok, number_of_flushes updated to {}".format(number_of_flushes)
            except Exception:
                self._user_feedback = "Error parsing number_of_flush data"
        elif "dist_temperature" in myInput[0]:
            try:
                dist_temperature = int(myInput[1])
                self._myHardwareControlSystem._config["FSM_EV"]["distillation_temperature"] = str(dist_temperature)
                self._myHardwareControlSystem.store_config()
                self._user_feedback = "Command ok, dist_temperature updated to {}".format(dist_temperature)
            except Exception:
                self._user_feedback = "Error parsing dist_temperature data"
        elif "after_heat_time" in myInput[0]:
            try:
                after_heat_time = int(myInput[1])
                self._myHardwareControlSystem._config["FSM_EV"]["after_heat_time"] = str(after_heat_time)
                self._myHardwareControlSystem.store_config()
                self._user_feedback = "Command ok, after_heat_time updated to {}".format(after_heat_time)
            except Exception:
                self._user_feedback = "Error parsing after_heat_time data"
        elif "after_heat_temp" in myInput[0]:
            try:
                after_heat_temp = int(myInput[1])
                self._myHardwareControlSystem._config["FSM_EV"]["after_heat_temp"] = str(after_heat_temp)
                self._myHardwareControlSystem.store_config()
                self._user_feedback = "Command ok, after_heat_temp updated to {}".format(after_heat_temp)
            except Exception:
                self._user_feedback = "Error parsing after_heat_temp data"
        elif "final_air_cycles_time_open" in myInput[0]:
            try:
                final_air_cycles_time_open = int(myInput[1])
                self._myHardwareControlSystem._config["FSM_EV"]["final_air_cycles_time_open"] = str(final_air_cycles_time_open)
                self._myHardwareControlSystem.store_config()
                self._user_feedback = "Command ok, final_air_cycles_time_open updated to {}".format(final_air_cycles_time_open)
            except Exception:
                self._user_feedback = "Error parsing final_air_cycles_time_open data"
        elif "final_air_cycles_time_closed" in myInput[0]:
            try:
                final_air_cycles_time_closed = int(myInput[1])
                self._myHardwareControlSystem._config["FSM_EV"]["final_air_cycles_time_closed"] = str(final_air_cycles_time_closed)
                self._myHardwareControlSystem.store_config()
                self._user_feedback = "Command ok, final_air_cycles_time_closed updated to {}".format(final_air_cycles_time_closed)
            except Exception:
                self._user_feedback = "Error parsing final_air_cycles_time_closed data"
        elif "final_air_cycles" in myInput[0]:
            try:
                final_air_cycles = int(myInput[1])
                self._myHardwareControlSystem._config["FSM_EV"]["final_air_cycles"] = str(final_air_cycles)
                self._myHardwareControlSystem.store_config()
                self._user_feedback = "Command ok, final_air_cycles updated to {}".format(final_air_cycles)
            except Exception:
                self._user_feedback = "Error parsing final_air_cycles data"
        elif "wattage_decrease_limit" in myInput[0]:
            try:
                wattage_decrease_limit = int(myInput[1])
                self._myHardwareControlSystem._config["PID"]["wattage_decrease_limit"] = str(wattage_decrease_limit)
                self._myHardwareControlSystem.store_config()
                self._user_feedback = "Command ok, wattage_decrease_limit updated to {}".format(wattage_decrease_limit)
            except Exception:
                self._user_feedback = "Error parsing wattage_decrease_limit data"
        else:
            self._user_feedback = "Command not understood"
        self._logger.debug(self._user_feedback)

    def task_readSocketInput(self):
        # try to read string from in_queue
        if not self._in_queue.empty():
            self._input_variable = self._in_queue.get()
            self._got_new_input_flag = True

    def update_app(self):
        try:
            pressure = self._myHardwareControlSystem.pressure
        except Exception:
            pressure = None

        message = {
            "clientId": None,
            "receiver": "client",
            "firmwareVersion": self.version,
            "machineVersion": "1.0",
            "machineState": "idle",
            "runMinutesSince": self._distill_runtime_total,
            "sinceDate": self._since_date,
            "timestamp": int(time.time()),
            "activeProgram": {
                "warning": None,
                "temperature": self._myHardwareControlSystem.bottom_temperature,
                "pressure": pressure,
                "progress": 0,
                "programId": "none",
                "currentAction": None,
                "estimatedTimeLeft": None,
                "timeElapsed": None,
                "power": self._myHardwareControlSystem.bottom_heater_percent,
            },
            "log": "",
            "programParameters": {
                "soakTime": self._myHardwareControlSystem._config["SYSTEM"]["soak_time_seconds"],
            }
        }

        if self._myHardwareControlSystem.FSM.curHandle == "Ready":
            message["machineState"] = "idle"

        elif self._myHardwareControlSystem.FSM.curHandle == "Error":
            message["machineState"] = "error"
            message["activeProgram"]["currentAction"] = "Error"
            message["log"] = self._myHardwareControlSystem.FSM.fsmData["failure_description"]

        else:
            program_string = "none"
            try:
                program = DeviceProgram(self._selected_program)
                program_string = program.name.lower()
            except ValueError:
                pass

            if self._myHardwareControlSystem.FSM.fsmData["pause_flag"]:
                message["machineState"] = "pause"
            else:
                message["machineState"] = "running"
            message["activeProgram"]["progress"] = self._myHardwareControlSystem.FSM.curState.progressPercentage
            message["activeProgram"]["programId"] = program_string
            message["activeProgram"]["currentAction"] = self._myHardwareControlSystem.FSM.curState.humanReadableLabel
            message["activeProgram"]["estimatedTimeLeft"] = self._myHardwareControlSystem.FSM.curState.estimatedTimeLeftSeconds
            message["activeProgram"]["timeElapsed"] = self._myHardwareControlSystem.FSM.curState.eventDurationWithPause
            message["activeProgram"]["warning"] = self._myHardwareControlSystem.FSM.curState.warning


    def get_machine_json_status(self):
        #read system ssid and passwd
        (myssid, mypasswd) = self._myHardwareControlSystem.get_ssid_and_passwd
        status = {
            "current_status": self._myHardwareControlSystem.FSM.curHandle,
            "SSID": myssid.strip(),
            "Password": mypasswd.strip(),
            "soak_time": self._myHardwareControlSystem._config["SYSTEM"][
                "soak_time_seconds"
            ],
            "number_of_flushes": self._myHardwareControlSystem._config["FSM_EX"][
                "number_of_flushes"
            ],
            "dist_temperature": self._myHardwareControlSystem._config["FSM_EV"][
                "distillation_temperature"
            ],
            "wattage_decrease_limit": self._myHardwareControlSystem._config["PID"][
                "wattage_decrease_limit"
            ],
            "after_heat_time": self._myHardwareControlSystem._config["FSM_EV"][
                "after_heat_time"
            ],
            "after_heat_temp": self._myHardwareControlSystem._config["FSM_EV"][
                "after_heat_temp"
            ],
            "final_air_cycles": self._myHardwareControlSystem._config["FSM_EV"][
                "final_air_cycles"
            ],
            "final_air_cycles_time_open": self._myHardwareControlSystem._config["FSM_EV"][
                "final_air_cycles_time_open"
            ],
            "final_air_cycles_time_closed": self._myHardwareControlSystem._config["FSM_EV"][
                "final_air_cycles_time_closed"
            ],
            "pump_power": self._myHardwareControlSystem.pump_value,
            "heater_pct": self._myHardwareControlSystem.bottom_heater_percent,
            "fan_pwm": self._myHardwareControlSystem.fan_value,
            "fan_adc_value": self._myHardwareControlSystem._fan_control.fan_adc_value,
            "fan_adc_check": self._myHardwareControlSystem._fan_control.fan_adc_check_string,
            "pressure": self._myHardwareControlSystem.pressure,
            "gas_temp": self._myHardwareControlSystem.gas_temperature,
            "bottom_heater": self._myHardwareControlSystem.bottom_temperature,
            "firmwareVersion": self.version,
            "machine_id": utils.getserial(),
            "unique_id" : utils.get_unique_id(),
        }

        status.update(self._myHardwareControlSystem.valve_status)

        return status

    def stop(self):
        self._running = False
        self._myHardwareControlSystem.shutdown()
        self._logger.debug("Stopping ControlThread.")

    def run(self):
        """
        Actual control loop
        Control loop has the following tasks
            1 Read sensors
            2 Process user input
            3 Process FSM
            4 Set physical outputs based on decisions
            5 Update UI
        """
        # Process user input
        # Make state decision
        # Set physical outputs based on decisions
        # Read sensors and update UI
        _last_time_update_app_timestamp = None
        self._running = True
        _logged_control_loop = False
        self._heartbeet.set()

        try:
            # main control loop
            while self._running:

                self._heartbeet.set()

                if (int(time.time()) % 600) == 0 and not _logged_control_loop:
                    self._logger.debug("Control loop is running. Device FSM state: {}, FSM handle {}. Pause flag {}.".format(
                        self._myHardwareControlSystem.FSM.curState.name,
                        self._myHardwareControlSystem.FSM.curHandle,
                        self._myHardwareControlSystem.FSM.fsmData["pause_flag"],
                    ))
                    _logged_control_loop = True
                elif (int(time.time()) % 600) != 0 and _logged_control_loop:
                    _logged_control_loop = False

                # check for config changes
                self._myHardwareControlSystem.update_config()

                # Read input from the user
                self.task_readSocketInput()

                # Check physical interface
                self.check_PhysicalInterface()

                if ALCOHOL_SENSOR_ENABLED:
                    self.check_alcohol_level()

                # Capture run time miutes for distill state.
                if self._myHardwareControlSystem.FSM.curHandle == "DistillBulk":
                    counter = self._myHardwareControlSystem.FSM.curState.eventDurationWithPause
                    runtime_minutes = int(counter / 60)
                    if (runtime_minutes - self._last_distill_runtime_total) > 0:
                        self.increment_run_counters(runtime_minutes)

                # Process FSM
                try:
                    self._myHardwareControlSystem.FSM.Execute()

                except HardwareFailure:
                    self._logger.error("Electrical error. Entering error state...")
                    self._myHardwareControlSystem.do_fast_blink()
                    self._myHardwareControlSystem.FSM.ToTransistion("toStateError")

                except Exception as e:
                    self._logger.exception(
                        "FSM state transition failed: {!r}".format(e)
                    )
                    self._myHardwareControlSystem.FSM.fsmData[
                        "failure_mode"
                    ] = FailureMode.UNKNOWN_ERROR
                    self._myHardwareControlSystem.FSM.ToTransistion("toStateError")

                # self.adjust_logging_level()  # TODO: temporary disable to investigate issues with unresponsiveness.

                if self._myHardwareControlSystem.FSM.curHandle == "Ready":
                    if self._myHardwareControlSystem._PID.PID_running:
                        self._myHardwareControlSystem.set_PID_target(
                            self._myHardwareControlSystem.FSM.fsmData["target_temp"]
                        )

                        # if adjustment period has run, start next cycle
                        self._myHardwareControlSystem.update_PID()
                    else:
                        self._myHardwareControlSystem._mybottomheater.power_percent = 0

                # Update the UI
                self.update_PhysicalUI()

                # Every 30 seconds update the app.
                # Hardware error can also happen here because when creating app payload we're reading
                # some of the sensors: pressure, temperature, etc.
                try:
                    if _last_time_update_app_timestamp is None or (time.time() - _last_time_update_app_timestamp) > 10:
                        _last_time_update_app_timestamp = time.time()
                        self.update_app()
                except HardwareFailure:
                    self._logger.error("Electrical error. Entering error state...")
                    self._myHardwareControlSystem.do_fast_blink()
                    self._myHardwareControlSystem.FSM.ToTransistion("toStateError")

                # Parse the user input. Reply on socket here
                self.parse_UserInput()

                # timing signal - 100 ms period
                time.sleep(.01)

        except Exception as error:
            self._logger.exception(
                "Error encountered in control loop: {!r}".format(error)
            )
            raise

        finally:
            self._logger.info("Exiting control loop...")
            sys.exit(0)

class SocketControl:
    """
    Class that handles console input from the user and passes the information to a ControlThread
    """

    # Port used to connect and control the application
    SOCKET_PORT = 666

    def __init__(self):
        self._logger = get_app_logger(str(self.__class__))
        self._heartbeet = threading.Event()
        self._last_heartbeet = time.time()

        # check for sudo access
        if not utils.is_root():
            print("Error, run application as sudo")
            exit(1)

        atexit.register(self.shutdown)
        for signal_name in (
            signal.SIGHUP,
            signal.SIGABRT,
            signal.SIGHUP,
            signal.SIGTERM,
            signal.SIGSEGV,
        ):
            signal.signal(signal_name, self.shutdown_handler)

        self._logger.debug("Initializing communication queue")
        self._q = Queue()

        self._logger.debug("Initializing ControlThread object")
        self.myControlObject = ControlThread("ControlThread", self._q, self._heartbeet, daemon=True)

        self._logger.debug("Starting ControlThread")
        self.myControlObject.start()
        self._logger.debug("ControlThread started")

        # Start socket for incoming commands
        self._logger.debug("Creating socket")
        self._s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # get dir for git updates
        self._gitdir = os.getcwd()

        #self._iot_client_blocked_time = 0

    def shutdown(self):
        self._logger.debug("Control Thread - executing shutdown sequence...")
        self._logger.debug("Closing server...")
        if hasattr(self, "_s"):
            self._s.shutdown(1)
        self._logger.debug("Server closed")

    def shutdown_handler(self, *args):
        self._logger.debug("Handling shutdown signal...")
        self._logger.debug("Control Thread - handling shutdown signal...")
        if hasattr(self, "myControlObject"):
            # try:
            #     # TODO: replace this.
            #     #self.myControlObject.iot_client.disconnect()
            # except Exception:
            #     # Ignore disconnect error as we're shutting down anyway.
            #     pass
            self.myControlObject.stop()
            self.myControlObject.join()
        self.shutdown()
        self._logger.debug("Control Thread - handling shutdown signal - exiting app.")
        exit(0)

    def main(self):
        self._logger.info(
            "Starting drizzle control v{}".format(self.myControlObject.version)
        )
        self.mysocket_port = SocketControl.SOCKET_PORT
        self._logger.info("Binding to socket {}".format(self.mysocket_port))
        self._s.bind(("", self.mysocket_port))
        self._s.listen(1)
        self._s.settimeout(1)
        self._logger.info(
            "Listening for connections on port {}".format(self.mysocket_port)
        )

        while True:
            # Check if child thread is still running.
            # Additional heartbeat check to make sure controlThread is running.
            _seconds_since_last_heartbeat = time.time() - self._last_heartbeet
            if not self._heartbeet.is_set():
                self._logger.warning("Heartbeat not set... Time passed: {:.02f}".format(_seconds_since_last_heartbeat))
            else:
                self._last_heartbeet = time.time()
                _seconds_since_last_heartbeat = time.time() - self._last_heartbeet

            if (_seconds_since_last_heartbeat > HEARTBEAT_TIMEOUT_SECONDS and not self._heartbeet.is_set()) or not self.myControlObject.is_alive():
                self._logger.error("ControlThread is dead. Restarting device...")
                #utils.reboot()

            if self.myControlObject._running:
                self._heartbeet.clear()

            try:
                command = None
                # establish connection
                c, addr = self._s.accept()

                self._logger.info("Got connection from {}".format(addr))
                try:
                    command = (c.recv(1024)).decode("UTF-8")
                except ConnectionResetError as err:
                    self._logger.error("Client has closed connection unexpectedly: {}".format(str(err)))
                    self._logger.info("Binding to socket {}".format(self.mysocket_port))
                    self._s.listen(1)
                    self._s.settimeout(1)
                    self._logger.info(
                        "Listening for connections on port {}".format(self.mysocket_port)
                    )
                    command = None

                reply_dict = {}

                if command == "status":
                    # wait for command to be processed
                    # arrange data into a JSON / Dict format and send it using Pickle
                    reply_dict = self.myControlObject.get_machine_json_status()
                    reply_dict["reply"] = "Got status data"
                elif command == "update_soft":
                    # Start update process
                    reply_dict["reply"] = "Updating software..."
                elif command == "send_logs":
                    # Upload last 7 days of log data to s3 bucket.
                    reply_dict["reply"] = "Uploading log files..."
                elif command == "get_serial":
                    # fetch machine unique ID
                    reply_dict["reply"] = self.myControlObject.get_unique_id()
                elif command == "blink_fast":
                    self.myControlObject._myHardwareControlSystem.do_fast_blink()
                    reply_dict["reply"] = "blink fast done"
                elif command == "blink_slow":
                    self.myControlObject._myHardwareControlSystem.do_slow_blink()
                    reply_dict["reply"] = "blink slow done"
                elif command == "print_stack":
                    reply = ""
                    for th in threading.enumerate():
                        try:
                            reply += "{}\n".format(th)
                            reply += "-"*80 + "\n"
                            reply += "\n".join(traceback.format_stack(sys._current_frames()[th.ident]))
                            reply += "\n"
                        except KeyError:
                            pass
                    self._logger.info("Current stack:\n{}".format(reply))
                    reply_dict["reply"] = reply
                elif command == "quit":
                    # wait for command to be processed
                    self._q.put(command)
                    reply_dict = {"reply": "Terminating server"}
                    self._logger.debug("Attempting to terminate control thread")
                    self.myControlObject.stop()
                    self.myControlObject.join()
                    self._logger.debug("Control thread has stopped")
                else:
                    # Pass user input to ControlThread
                    self._q.put(command)
                    # wait for command to be processed
                    sleep(0.3)
                    # send reply from main thread
                    reply_dict = {"reply": self.myControlObject._user_feedback}

                pickle_data = pickle.dumps(reply_dict)
                c.send(pickle_data)
                c.close()
            except KeyboardInterrupt:
                command = "quit"
                self.myControlObject.stop()
                self.myControlObject.join()

            except socket.timeout:
                pass

            except BrokenPipeError:
                pass

            except Exception as error:
                self._logger.exception(
                    "Unexpected exception in socket control: {!r}".format(error)
                )
                raise

            if command == "quit":
                self._logger.info("Shutting down Socket")
                self.shutdown()
                self._logger.info("Socket shut down")
                sleep(1)
                self._logger.info("Quitting application")
                sys.exit(0)
            elif command == "update_soft":
                try:
                    self._logger.info("Updating software...")
                    self.myControlObject._updating = True
                    # flash all LED's to indicate an update in progress
                    self.myControlObject._myHardwareControlSystem._myphysicalinterface.set_state(
                        module_physicalinterface.DeviceState.UPDATING
                    )
                    utils.update()
                    self._logger.info("Software update completed.")
                except Exception as e:
                    self._logger.exception("Software update failed: {!r}".format(e))
                finally:
                    self.myControlObject._updating = False
            elif command == "send_logs":
                flush_logger()
                utils.upload_log_files()

            time.sleep(0.5)

"""
Application starts here
"""
def main():
    setup_logging()
    module_logger = get_app_logger("main")
    module_logger.info("Starting drizzle control v1.1")

    #check for sd card move
    utils.update_sd_card_data()

    mySocketApplication = SocketControl()
    mySocketApplication.main()

if __name__ == "__main__":
    main()
