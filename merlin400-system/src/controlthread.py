import enum
import sqlite3
import sys
import threading
import time
from datetime import datetime

import system_setup
from common.settings import HEARTBEAT_TIMEOUT_SECONDS, ALCOHOL_SENSOR_ENABLED
from hardware.module_HardwareControlSystem import (
    module_HardwareControlSystem,
    INIT_STATUS_OK, INIT_STATUS_USER_PANEL_ERROR, INIT_STATUS_PRESSURE_SENSOR_ERROR, HardwareFailure,
    PressureSensorFailure, UserPanelError, ElectricalError
)
from hardware.components.module_physicalinterface import module_physicalinterface
from hardware.components.module_fancontrol import NotSupportedFanError, module_fancontrol
from hardware.module_FSM import FailureMode
from common.module_logging import get_app_logger
from hardware.commands.basecommand import BaseCommand

from hardware.commands.start_extraction import Command_StartExtraction
from hardware.commands.start_heat_oil import Command_StartHeatOil
from hardware.commands.start_clean_pump import Command_StartCleanPump
from hardware.commands.start_decarb import Command_StartDecarb
from hardware.commands.start_distill import Command_StartDistill
from hardware.commands.start_vent_pump import Command_StartVentPump
from hardware.commands.pause_program import Command_PauseProgram
from hardware.commands.resume_program import Command_ResumeProgram
from hardware.commands.reset import Command_Reset
from hardware.commands.clean_valve import Command_CleanValve



if ALCOHOL_SENSOR_ENABLED:
    from hardware.components.module_alcoholsensor import module_alcoholsensor

RESET_COUNTER = 30
SELECT_COUNTER_SHOW_CONNECTIVITY = 30
PLAY_COUNTER_AFTERSTILL = 50
PAUSE_COUNTER_SHUTDOWN = 30

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
    Controlthread is instantiated from startup and runs as a separate thread. It
    uses a command based system to let other threads execute actions on 
    the ControlThread in a thread safe manner.
    """
    _scheduledCommand: BaseCommand = None
    _activeCommand: BaseCommand = None
    _statusDict: any = {
        "machineState": "idle",
        "currentStatus": None,
        "timestamp": int(time.time()),

        "deviceInfo" : {
            "machine_id": None,
            "unique_id" : None,
            "firmwareVersion": None,
            "runMinutesSince": None,
            "sinceDate": None,
        },

        "hardwareMonitor": {
            "pump_power": None,
            "heater_pct": None,
            "fan_pwm": None,
            "fan_adc_value": None,
            "fan_adc_check": None,
            "pressure": None,
            "gas_temp": None,
            "bottom_heater_power": None,
            "bottom_heater_temperature": None,
        },        

        "activeProgram": {
            "programId": "none",
            "currentAction": None,
            "progress": 0,
            "estimatedTimeLeft": None,
            "timeElapsed": None,
            "warning": None,
            "errorMessage": None,
        },

        "programParameters": {
            "soakTime": None,
            "number_of_flushes":None,
            "dist_temperature": None,
            "wattage_decrease_limit": None,
            "after_heat_time": None,
            "after_heat_temp": None,
            "final_air_cycles": None,
            "final_air_cycles_time_open": None,
            "final_air_cycles_time_closed": None,
        },
    }

    def __init__(self, name, heartbeat, **kwargs):
        """
        Constructor method
        """
        self._heartbeat = heartbeat
        self._heartbeat.set()

        # Initialize thread
        super().__init__(**kwargs)
        # Store thread name
        self.name = name

        self._logger = get_app_logger(str(self.__class__))

        # Read fw version and device version
        self.version = open("VERSION").readlines()[0].strip()
        self.device_version = system_setup.get_device_version()

        # Initialize hardware here
        self._logger.info("Initializing control system. Firmware version: {}".format(self.version))
        try:
            self._hardwareControlSystem = module_HardwareControlSystem(self.device_version)
            if self._hardwareControlSystem.init_status == INIT_STATUS_PRESSURE_SENSOR_ERROR:
                raise PressureSensorFailure("Pressure sensor initialization problem")
            elif self._hardwareControlSystem.init_status == INIT_STATUS_USER_PANEL_ERROR:
                raise UserPanelError("Failed to initialize user panel")
            elif self._hardwareControlSystem.init_status != INIT_STATUS_OK:
                raise ElectricalError("I2C related error.")

        except PressureSensorFailure:
            self._logger.error("Pressure sensor initialization error. Entering error state...")
            self._hardwareControlSystem.FSM.fsmData["failure_mode"] = FailureMode.PRESSURE_SENSOR_ERROR
            self._hardwareControlSystem.FSM.ToTransistion("toStateError")

        except ElectricalError:
            self._logger.error("Electrical error. Entering error state...")
            self._hardwareControlSystem.do_fast_blink()
            self._hardwareControlSystem.FSM.ToTransistion("toStateError")

        except UserPanelError:
            self._logger.error("User panel error. Entering error state...")
            self._hardwareControlSystem.do_slow_blink()
            self._hardwareControlSystem.FSM.ToTransistion("toStateError")

        except HardwareFailure:
            self._logger.error("Hardware error. Entering error state...")
            self._hardwareControlSystem.do_fast_blink()
            self._hardwareControlSystem.FSM.ToTransistion("toStateError")

        else:
            self._hardwareControlSystem.FSM.SetFSMData("start_flag", False)
            self._logger.info("Control system initialized")

        self._selected_program = 1

        # counter that counts how many times we have seen a reset request
        self._reset_request_counter = 0
        # counter that counts how many times we have seen a select button request
        self._select_request_counter = 0
        # counter that counts how many times we have seen a pause button request in a row
        self._pause_request_counter = 0
        # counter that counts how many times we have seen a play button request in a row
        self._play_request_counter = 0

        # variable used to limit alcohol logging to once persecond
        self._alcohollevel_last_log_second = datetime.now().timestamp()

        # distill runtime variables
        self._last_distill_runtime_total = 0.0
        self._distill_runtime_total = 0.0
        self._distill_mode = "distill"
        self._since_date = None

        # stats db connection.
        self._init_stats_db()
        self._load_total_run_minutes()

        #Only set at init (never changes)
        self._statusDict["deviceInfo"]["machine_id"] = system_setup.getserial()
        self._statusDict["deviceInfo"]["unique_id"] = system_setup.get_unique_id()
        self._statusDict["deviceInfo"]["firmwareVersion"] = self.version
        self._statusDict["deviceInfo"]["sinceDate"] = self._since_date

    def _init_stats_db(self):
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

    def _load_total_run_minutes(self):
        with sqlite3.connect("stats.db") as conn:
            cur = conn.execute("select date_since, value from stats where mode = ?", (self._distill_mode,))
            row = cur.fetchone()
            since_date, value = row
            self._since_date = since_date
            self._distill_runtime_total = value
            self._last_distill_runtime_total = 0
            self._logger.info("Initialized device stats, total run minutes %s since %s.", value, since_date)

    def _increment_run_counters(self, value):
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

    def _reset_session_counter(self):
        self._last_distill_runtime_total = 0


    def _show_connectivity(self):
        if False:
            self._hardwareControlSystem._myphysicalinterface.do_connected_flash()
        else:
            self._hardwareControlSystem._myphysicalinterface.do_disconnected_flash()

    def _update_PhysicalUI(self):
        if self._reset_request_counter > 0:
            return

        if not self._hardwareControlSystem._myphysicalinterface:
            return

        if (
            self._hardwareControlSystem.FSM.curHandle == "Ready"
            and not self._hardwareControlSystem.FSM.fsmData["running_flag"]
        ):
            self._hardwareControlSystem._myphysicalinterface.set_state(
                module_physicalinterface.DeviceState.READY
            )
        elif self._hardwareControlSystem.FSM.curHandle == "Error":
            self._hardwareControlSystem._myphysicalinterface.set_state(
                module_physicalinterface.DeviceState.ERROR
            )
        elif self._hardwareControlSystem.FSM.curHandle == "DistillBulk":
            if self._hardwareControlSystem.FSM.fsmData["pause_flag"]:
                self._hardwareControlSystem._myphysicalinterface.set_state(
                    module_physicalinterface.DeviceState.PAUSE
                )
            else:
                self._hardwareControlSystem._myphysicalinterface.set_state(
                    module_physicalinterface.DeviceState.RUNNING_PAUSE_ENABLED
                )
        elif self._hardwareControlSystem.FSM.curHandle == "CleanPump":
            if self._hardwareControlSystem.FSM.fsmData["pause_flag"]:
                self._hardwareControlSystem._myphysicalinterface.set_state(
                    module_physicalinterface.DeviceState.PAUSE
                )
            else:
                self._hardwareControlSystem._myphysicalinterface.set_state(
                    module_physicalinterface.DeviceState.RUNNING_PAUSE_ENABLED
                )
        else:
            self._hardwareControlSystem._myphysicalinterface.set_state(
                module_physicalinterface.DeviceState.RUNNING_PAUSE_DISABLED
            )

    def schedule_command_for_execution(self, command: BaseCommand):        
        self._logger.info("Scheduling command for execution")
        command.validate_state(self._hardwareControlSystem)
        self._scheduledCommand=command

    def get_machine_json_status(self):
        curHandle = self._hardwareControlSystem.FSM.curHandle
        self._statusDict["timestamp"] = int(time.time())
        self._statusDict["currentStatus"] = curHandle
        self._statusDict["deviceInfo"]["runMinutesSince"] = self._distill_runtime_total

        activeProgramDict=self._statusDict["activeProgram"]

        if curHandle == "Ready":
            self._statusDict["machineState"] = "idle"
            activeProgramDict["progress"] = None
            activeProgramDict["programId"] = None
            activeProgramDict["currentAction"] = None
            activeProgramDict["estimatedTimeLeft"] = None
            activeProgramDict["timeElapsed"] = None
            activeProgramDict["warning"] = None
            activeProgramDict["errorMessage"] = None
        elif curHandle == "Error":
            self._statusDict["machineState"] = "error"
            activeProgramDict["currentAction"] = "Error"
            activeProgramDict["errorMessage"] = self._hardwareControlSystem.FSM.fsmData["failure_description"]
        else:
            if self._hardwareControlSystem.FSM.fsmData["pause_flag"]:
                self._statusDict["machineState"] = "pause"
            else:
                self._statusDict["machineState"] = "running"

            program_string = "none"
            try:
                program = DeviceProgram(self._selected_program)
                program_string = program.name.lower()
            except ValueError:
                pass

            activeProgramDict["progress"] = self._hardwareControlSystem.FSM.curState.progressPercentage
            activeProgramDict["programId"] = program_string
            activeProgramDict["currentAction"] = self._hardwareControlSystem.FSM.curState.humanReadableLabel
            activeProgramDict["estimatedTimeLeft"] = self._hardwareControlSystem.FSM.curState.estimatedTimeLeftSeconds
            activeProgramDict["timeElapsed"] = self._hardwareControlSystem.FSM.curState.eventDurationWithPause
            activeProgramDict["warning"] = self._hardwareControlSystem.FSM.curState.warning
            activeProgramDict["errorMessage"] = None
        
        return self._statusDict


    #Invoked from control thread - Updates statusobject with values read from hardware
    def _update_hardware_status(self):
        try:
            pressure = self._hardwareControlSystem.pressure
        except Exception:
            pressure = None

        self._statusDict["hardwareMonitor"] =  {
            "pump_power": self._hardwareControlSystem.pump_value,
            "heater_pct": self._hardwareControlSystem.bottom_heater_percent,
            "fan_pwm": self._hardwareControlSystem.fan_value,
            "fan_adc_value": self._hardwareControlSystem._fan_control.fan_adc_value,
            "fan_adc_check": self._hardwareControlSystem._fan_control.fan_adc_check_string,
            "pressure": pressure,
            "gas_temp": self._hardwareControlSystem.gas_temperature,
            "bottom_heater_power": self._hardwareControlSystem.bottom_heater_percent,
            "bottom_heater_temperature": self._hardwareControlSystem.bottom_temperature,
        }
        self._statusDict["hardwareMonitor"].update(self._hardwareControlSystem.valve_status)

        self._statusDict["programParameters"] = {
            "soakTime": self._hardwareControlSystem._config["SYSTEM"]["soak_time_seconds"],
            "number_of_flushes": self._hardwareControlSystem._config["FSM_EX"]["number_of_flushes"],
            "dist_temperature": self._hardwareControlSystem._config["FSM_EV"]["distillation_temperature"],
            "wattage_decrease_limit": self._hardwareControlSystem._config["PID"]["wattage_decrease_limit"],
            "after_heat_time": self._hardwareControlSystem._config["FSM_EV"]["after_heat_time"],
            "after_heat_temp": self._hardwareControlSystem._config["FSM_EV"]["after_heat_temp"],
            "final_air_cycles": self._hardwareControlSystem._config["FSM_EV"]["final_air_cycles"],
            "final_air_cycles_time_open": self._hardwareControlSystem._config["FSM_EV"]["final_air_cycles_time_open"],
            "final_air_cycles_time_closed": self._hardwareControlSystem._config["FSM_EV"]["final_air_cycles_time_closed"],
        }

#-----------------------------------------------------------------------------------------
# Functions that checks state of physical buttons presses and performs actions if pressed
# invoked by the control thread
#-----------------------------------------------------------------------------------------
    def _check_PhysicalInterface(self):
        # if there has been a reset request recheck it!
        if self._reset_request_counter > 0:
            button_press = self._hardwareControlSystem.button_press_force
            if button_press is module_physicalinterface.ButtonPressed.RESET:
                self._reset_request_counter += 1

                # check if it is time for a reset
                self._check_for_button_reset()
                return
            else:
                self._reset_request_counter = 0
                self._hardwareControlSystem._myphysicalinterface.set_state(
                    self._last_display_state
                )

        # if there has been a wifi connectivity request (select) recheck it!
        if self._select_request_counter > 0:
            # print('Select counter: {}'.format(self._select_request_counter))
            button_press = self._hardwareControlSystem.button_press_force
            if button_press is module_physicalinterface.ButtonPressed.SELECT:
                self._select_request_counter += 1
                # check if it is time for a display of connectivity
                self._check_for_button_select()
                return
            else:
                self._select_request_counter = 0

        # if there has been a print labe request (select) recheck it!
        if self._pause_request_counter > 0:
            # print('Select counter: {}'.format(self._select_request_counter))
            button_press = self._hardwareControlSystem.button_press_force
            if button_press is module_physicalinterface.ButtonPressed.PAUSE:
                self._pause_request_counter += 1
                # check if it is time for a display of connectivity
                self.check_for_button_pause()
                return
            else:
                self._pause_request_counter = 0

        # if there has been a force afterstill press - recheck it!
        if self._play_request_counter > 0:
            button_press = self._hardwareControlSystem.button_press_force
            if button_press is module_physicalinterface.ButtonPressed.PLAY:
                self._play_request_counter += 1
                # check if it is time for a display of connectivity
                self._check_for_button_play()
                return
            else:
                self._play_request_counter = 0

        button_press = self._hardwareControlSystem.button_press

        if button_press is module_physicalinterface.ButtonPressed.SELECT:
            # may only run if state is ready, otherwise just ignore
            if self._hardwareControlSystem.FSM.curHandle != "Ready":
                return

            self._select_request_counter += 1
            self._logger.debug("User pressed select")
            self._selected_program += 1
            if self._selected_program > 4:
                self._selected_program = 1
            self._hardwareControlSystem._myphysicalinterface.set_program(self._selected_program)
            self._hardwareControlSystem._myphysicalinterface.set_state(module_physicalinterface.DeviceState.READY)

        elif button_press is module_physicalinterface.ButtonPressed.PLAY:
            self._logger.debug("User pressed play")
            # may only run if state is ready, otherwise just ignore or is in pause mode
            if (
                self._hardwareControlSystem.FSM.curHandle != "Ready"
                and self._hardwareControlSystem.FSM.curHandle not in ("DistillBulk", "CleanPump")
            ):
                # Machine is running, use the buttonpress to toggle LED state
                self._hardwareControlSystem.toggle_light()
                return

            if self._hardwareControlSystem.FSM.curHandle == "Ready":
                if self._selected_program == 1:
                    self._logger.debug("Extracting")
                    self.schedule_command_for_execution(Command_StartExtraction(runFull=True))

                elif self._selected_program == 2:
                    self._logger.debug("Decarboxylating")
                    self.schedule_command_for_execution(Command_StartDecarb())

                elif self._selected_program == 3:
                    self._logger.debug("Heating oil")
                    self.schedule_command_for_execution(Command_StartHeatOil())

                elif self._selected_program == 4:
                    self._logger.debug("Distilling")
                    self.schedule_command_for_execution(Command_StartDistill())

                else:
                    self._logger.debug("Invalid program %r, ignoring...", self._selected_program)
                    return

            elif self._hardwareControlSystem.FSM.curHandle in ("DistillBulk", "CleanPump"):
                # if distillation is running, toggle white light
                self._play_request_counter += 1
                if not self._hardwareControlSystem.FSM.fsmData["pause_flag"]:
                    self._hardwareControlSystem.toggle_light()

                # restart distillation
                self._hardwareControlSystem.FSM.SetFSMData("pause_flag", False)
            else:
                self._user_feedback = "Error, wrong state for decarb run"
            # self._myHardwareControlSystem._myphysicalinterface.set_state(module_physicalinterface.STATE_RUNNING_PAUSE_ENABLED)

        elif button_press is module_physicalinterface.ButtonPressed.PAUSE:
            self._pause_request_counter += 1
            self._logger.debug("User pressed pause")
            # may only run if state is ready, otherwise just ignore or is in pause mode
            if self._hardwareControlSystem.FSM.curHandle not in ("DistillBulk", "CleanPump"):
                self._logger.debug("Machine is in wrong mode, ignore button press")
                return
            self._hardwareControlSystem.FSM.SetFSMData("pause_flag", True)
            self._hardwareControlSystem._myphysicalinterface.set_state(
                module_physicalinterface.DeviceState.PAUSE
            )

        elif button_press is module_physicalinterface.ButtonPressed.RESET:
            self._logger.debug("User pressed reset")
            # store existing state
            self._last_display_state = (
                self._hardwareControlSystem._myphysicalinterface._state
            )
            self._reset_request_counter += 1
            self._hardwareControlSystem._myphysicalinterface.set_state(
                module_physicalinterface.DeviceState.RESET_WARNING
            )

    def _check_for_button_reset(self):
        if self._reset_request_counter > RESET_COUNTER:
            # user requested reset
            self.schedule_command_for_execution(Command_Reset())

    def _check_for_button_select(self):
        # print('Buttons select counter: {}'.format(self._select_request_counter))
        if self._select_request_counter > SELECT_COUNTER_SHOW_CONNECTIVITY:
            self._logger.debug("Showing connectivity")
            # user requested display of wifi status
            self._show_connectivity()
            self._select_request_counter = 0

    def _check_for_button_play(self):
        if self._play_request_counter > PLAY_COUNTER_AFTERSTILL:
            self._logger.debug("Forcing afterstill")
            self._hardwareControlSystem._myphysicalinterface.do_force_afterstill_blink()
            self._hardwareControlSystem.FSM.SetFSMData("force_afterstill", True)
            self._play_request_counter = 0

#-----------------------------------------------------------------------------------------
# Functions that checks hardware status
# invoked by the control thread
#-----------------------------------------------------------------------------------------
    def _check_alcohol_level(self):
        # ALCOHOL CHECK START
        # check ambient alcohol levels
        _previous_alcohol_level = None
        alcohol_level = None
        try:
            if self._hardwareControlSystem.FSM.curHandle != "Error":
                alcohol_level = self._hardwareControlSystem.alcohol_level
        except HardwareFailure:
            self._logger.error("Electrical error. Entering error state...")
            self._hardwareControlSystem.do_fast_blink()
            self._hardwareControlSystem.FSM.ToTransistion("toStateError")

        # print("Alcohol level message: {}".format(alcohol_level.value))
        # print("Raw alcohol level: {}".format(s))
        # _previous_alcohol_level = alcohol_level

        # log the alcohol level
        if hasattr(self._hardwareControlSystem, "myalcoholdatalogger"):
            # datalogging is on
            # log once per second
            if self._last_log_second != int(datetime.now().timestamp()):
                self._last_log_second = int(datetime.now().timestamp())
                logdate = "{:%Y-%m-%d-%H:%M:%S}".format(datetime.now())

                data = {
                    "Time": logdate,
                    "AlcoholLevel": self._hardwareControlSystem.alcohol_level_raw,
                }
                self._hardwareControlSystem.myalcoholdatalogger.append_data(
                    data
                )

        if alcohol_level is module_alcoholsensor.AlcoholLevelMessage.DANGER:
            self._logger.warning("Alcohol level critical - stop now!")

            try:
                self._hardwareControlSystem._mypump.pump_pwm = 0
            except Exception as error:
                self._logger.exception("Failed to shutdown pump:")

            try:
                self._hardwareControlSystem.bottom_heater_power = 0
            except Exception as error:
                self._logger.exception("Failed to shutdown heater:")

            try:
                self._hardwareControlSystem._valve_controller.shutdown()
            except Exception as error:
                self._logger.exception("Failed to shutdown valves:")

            try:
                self._hardwareControlSystem._myalcoholsensor.shutdown()
            except Exception as error:
                self._logger.exception("Failed to shutdown alcohol sensor:")

            # set failure mode
            self._hardwareControlSystem.FSM.fsmData[
                "failure_mode"
            ] = FailureMode.ALCOHOL_GASLEVEL_ERROR
            # display failure mode in display
            # self._myHardwareControlSystem.show_error_code_in_display()
            # Transition to Error state.
            self._hardwareControlSystem.FSM.ToTransistion("toStateError")
    # # ALCOHOL CHECK STOP

    def check_fan_is_off(self):
        try:
            self._logger.info("Checking fan status on boot.")
            status = self._hardwareControlSystem._fan_control.fan_adc_check
            # This check expects that fan is off as it's performed on device start.
            if status != module_fancontrol.FAN_ADC_LEVEL_OFF:
                self._logger.info("Reading fan status as {}.".format(status))
                self._hardwareControlSystem._myphysicalinterface.set_state(
                    module_physicalinterface.DeviceState.ERROR
                )
                self._hardwareControlSystem.FSM.fsmData["failure_mode"] = FailureMode.FAN_ERROR
                self._hardwareControlSystem.FSM.ToTransistion("toStateError")
                self._hardwareControlSystem.FSM.fsmData["failure_description"] = (
                    "Error, air fan seems to be defective. Please try again and if it still fails, contact drizzle "
                    "support."
                )
        except NotSupportedFanError:
            # This exception means that device does't support fan and there is nothing to do.
            self._logger.info("Fan not supported on this hardware.")

#-----------------------------------------------------------------------------------------
# The control loop
#-----------------------------------------------------------------------------------------
    def run(self):
        """
        Actual control loop
        Control loop has the following tasks
            1 Read sensors
            2 Process user input (from physical interface or web interface)
            3 Process FSM
            4 Set physical outputs based on decisions
            5 Update UI
        """
        _last_time_update_app_timestamp = None
        self._running = True
        _logged_control_loop = False
        self._heartbeat.set()

        try:
            # main control loop
            while self._running:
                self._heartbeat.set()

                if (int(time.time()) % 600) == 0 and not _logged_control_loop:
                    self._logger.debug("Control loop is running. Device FSM state: {}, FSM handle {}. Pause flag {}.".format(
                        self._hardwareControlSystem.FSM.curState.name,
                        self._hardwareControlSystem.FSM.curHandle,
                        self._hardwareControlSystem.FSM.fsmData["pause_flag"],
                    ))
                    _logged_control_loop = True
                elif (int(time.time()) % 600) != 0 and _logged_control_loop:
                    _logged_control_loop = False

                # check for config changes
                self._hardwareControlSystem.update_config()

                # Check physical interface
                self._check_PhysicalInterface()

                if ALCOHOL_SENSOR_ENABLED:
                    self._check_alcohol_level()

                # Capture run time miutes for distill state.
                if self._hardwareControlSystem.FSM.curHandle == "DistillBulk":
                    counter = self._hardwareControlSystem.FSM.curState.eventDurationWithPause
                    runtime_minutes = int(counter / 60)
                    if (runtime_minutes - self._last_distill_runtime_total) > 0:
                        self._increment_run_counters(runtime_minutes)

                hasExecutedCommand=False
                #Check if there is a command to be executed, if so validate and execute it
                self._activeCommand=self._scheduledCommand
                self._scheduledCommand=None
                if self._activeCommand!=None:
                    self._logger.info("Executing scheduled command")
                    self._activeCommand.validate_state(self._hardwareControlSystem)
                    self._activeCommand.execute(self._hardwareControlSystem)
                    self._logger.info("Finished executing scheduled command")
                    hasExecutedCommand=True

                # Process FSM
                try:
                    self._hardwareControlSystem.FSM.Execute()

                except HardwareFailure:
                    self._logger.error("Electrical error. Entering error state...")
                    self._hardwareControlSystem.do_fast_blink()
                    self._hardwareControlSystem.FSM.ToTransistion("toStateError")

                except Exception as e:
                    self._logger.exception("FSM state transition failed: {!r}".format(e))
                    self._hardwareControlSystem.FSM.fsmData["failure_mode"] = FailureMode.UNKNOWN_ERROR
                    self._hardwareControlSystem.FSM.ToTransistion("toStateError")

                # self.adjust_logging_level()  # TODO: temporary disable to investigate issues with unresponsiveness.

                if self._hardwareControlSystem.FSM.curHandle == "Ready":
                    if self._hardwareControlSystem._PID.PID_running:
                        self._hardwareControlSystem.set_PID_target(self._hardwareControlSystem.FSM.fsmData["target_temp"])
                        # if adjustment period has run, start next cycle
                        self._hardwareControlSystem.update_PID()
                    else:
                        self._hardwareControlSystem._mybottomheater.power_percent = 0

                # Update the UI
                self._update_PhysicalUI()

                # Hardware error can also happen here because when creating app payload we're reading
                # some of the sensors: pressure, temperature, etc.
                try:
                    if _last_time_update_app_timestamp is None or (time.time() - _last_time_update_app_timestamp) > 10 or hasExecutedCommand:
                        self._logger.info("Updating appstatus deep...")
                        _last_time_update_app_timestamp = time.time()
                        self._update_hardware_status()
                except HardwareFailure:
                    self._logger.error("Electrical error. Entering error state...")
                    self._hardwareControlSystem.do_fast_blink()
                    self._hardwareControlSystem.FSM.ToTransistion("toStateError")

                # timing signal - 100 ms period
                time.sleep(.01)

        except Exception as error:
            self._logger.exception("Unhandled exception in control loop: {!r}".format(error))
            raise

        finally:
            self._logger.info("Exiting control loop...")

    def stop(self):
        self._running = False
        self._logger.debug("ControlThread:stop - Shutting down hardwarecontrolsystem.")
        self._hardwareControlSystem.shutdown()
