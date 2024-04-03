if __name__ == "__main__":
  #Add root folder (/src/) to paths for import if this script is run standalone
  from pathlib import Path
  import sys
  sys.path.append(str(Path(__file__).resolve().parent.parent))
  print(sys.path)

import time
import enum
import hardware.module_math as math
from common.module_logging import get_app_logger

from hardware.components.module_fancontrol import NotSupportedFanError
from hardware.components.module_fancontrol import module_fancontrol
from common.settings import ALCOHOL_SENSOR_ENABLED

if ALCOHOL_SENSOR_ENABLED:
    from hardware.components.module_alcoholsensor import module_alcoholsensor


# Errormode definitions
class FailureMode(enum.Enum):
    NONE = 0
    EVC_LEAK = 1
    EXC_LEAK = 2
    ALCOHOL_GASLEVEL_ERROR = 3
    VALVE_3_BLOCKED = 4
    HEATER_ERROR = 5
    PUMP_NEEDS_CLEAN_OR_REPLACEMENT = 6
    VALVE_2_BLOCKED = 7
    VALVE_4_BLOCKED = 8
    VALVE_1_OR_VALVE_3_BLOCKED = 9
    FAN_ERROR = 10
    PRESSURE_SENSOR_ERROR = 11
    THERMAL_RUNAWAY = 12
    UNKNOWN_ERROR = 15


# Base type for machine object
Machine = type("Machine", (object,), {"day": 0})

# =====================================================
##Finite state machine (FSM) implementation below
# =====================================================

# =====================================================
##TRANSITION CLASS, used to move from one state to another
class Transition(object):
    def __init__(self, toState):
        self.toState = toState

    def Execute(self):
        pass


##=====================================================
##STATES - implement actual functionality in classes below for each state
# Baseclass
class State(object):
    def __init__(self, FSM):
        self.name = self.__class__.__name__
        self._logger = get_app_logger(str(self.__class__))
        self.FSM = FSM
        self.timer = 0
        self.onPause = False
        self.startTime = 0
        self._eventTimePreviousMeasurement = 0.0
        self.eventDuration = 0
        self.eventDurationWithPause = 0.0
        self.estimatedTimeLeftSeconds = None
        self.progressPercentage = 0
        self.humanReadableLabel = "Unset"
        self.warning = None  # In case current state has any kind of warning message, set it to this attribute.

    def Enter(self):
        self._logger.info("*** Enter ***")
        self.startTime = time.time()
        self.eventDuration = 0
        self.eventDurationWithPause = 0.0
        self._eventTimePreviousMeasurement = self.startTime
        self.warning = None

    def Execute(self):
        now = time.time()
        self.eventDuration = now - self.startTime
        delta = 0.0
        if not self.onPause:
            delta = now - self._eventTimePreviousMeasurement

        self._eventTimePreviousMeasurement = now
        self.eventDurationWithPause += delta

    def Exit(self):
        self._logger.info("*** Exit ***")
        self.warning = None


# Error state
class StateError(State):
    def __init__(self, FSM):
        super(StateError, self).__init__(FSM)

    def Enter(self):
        super(StateError, self).Enter()
        self._logger.info("System entering errorstate")
        self.humanReadableLabel = "Error"

        # Shut everything down
        self.FSM.machine.PID_off()
        self.FSM.machine.set_PID_target(0)
        self.FSM.machine.bottom_heater_percent = 0
        self.FSM.machine.fan_value = 0
        self.FSM.machine.pump_value = 0

        # error state indication
        self._blink_interval = 0.5
        self._blink_time = time.time()

        # show error in display
        self.FSM.machine.show_error_code_in_display()

    def Execute(self):
        super(StateError, self).Execute()
        self.FSM.FSMOutputText = "System error - please reset system!"
        # blink red light
        if (time.time() - self._blink_time) > self._blink_interval:
            self.FSM.machine.toggle_red_light()
            self._blink_time = time.time()

    def Exit(self):
        super(StateError, self).Exit()
        self.FSM.FSMOutputText = "Exiting ready state"
        self._logger.info(self.FSM.FSMOutputText)


# Ready state
class StateReady(State):
    def __init__(self, FSM):
        super(StateReady, self).__init__(FSM)

    def Enter(self):
        super(StateReady, self).Enter()
        self.FSM.FSMOutputText = "System getting ready"
        self.FSM.fsmData["running_flag"] = False
        self.humanReadableLabel = "System ready."

        # turn off warm light
        self.FSM.machine.light_off()

        # turn off alcohol sensor
        self.FSM.machine.set_alcohol_sensor_off()
        self.FSM.intitialAlcoholCheckDone = None

        self._logger.info(self.FSM.FSMOutputText)

    def Execute(self):
        super(StateReady, self).Execute()
        self.FSM.FSMOutputText = "System ready!"

        # Wait from signal from user
        if self.FSM.fsmData["start_flag"]:
            # got start signal - start FSM
            self.FSM.fsmData["start_flag"] = False
            self.FSM.ToTransistion("toStateSystemCheck")

    def Exit(self):
        super(StateReady, self).Exit()


# System Check State
class StateSystemCheck(State):
    def check_fan_is_on(self):
        status = self.FSM.machine._fan_control.fan_adc_check
        self._logger.info("Checking fan status... Fan status is {}".format(status))
        if status == module_fancontrol.FAN_ADC_LEVEL_ON:
            self._fan_ok = True
        elif status is not None:
            self.FSM.fsmData["failure_mode"] = FailureMode.FAN_ERROR
            self.FSM.ToTransistion("toStateError")
            self.FSM.fsmData["failure_description"] = (
                "Error, air fan seems to be defective. Please try again and if it still fails, contact "
                "drizzle support."
            )
        else:
            # Fan not supported.
            self._fan_ok = True

    def __init__(self, FSM):
        super(StateSystemCheck, self).__init__(FSM)

    def Enter(self):
        super(StateSystemCheck, self).Enter()
        # Check heater (checks physical heater and thermistor and ADC)

        self.FSM.FSMOutputText = "System getting ready"
        self.humanReadableLabel = "Initial system check"
        self._logger.info(self.FSM.FSMOutputText)
        # variable that keeps track of the current system check state
        self.system_check_state = 0

        # drain system
        self.FSM.machine.drain_system()

        # allow system to depressurize
        time.sleep(2)

        # If we already measured alcohol level, we can skip turning on sensor and checking again.
        # If not, we need to turn on the sensor and it will be checked as first step.
        if not self.FSM.intitialAlcoholCheckDone:
            self.FSM.machine.set_alcohol_sensor_on()

        # read initial pressure
        self.FSM.fsmData["atm_pressure"] = self.FSM.machine.pressure

        self._pressure_last_log_time = None

        # system_check_state == 0:   Check alcohol level -> Throw ALCOHOL_GASLEVEL_ERROR
        # system_check_state == 1:   Close all valves
        # system_check_state == 2:   Reduce pressure to MAXIMUM, test for pump error
        # system_check_state == 3:   Check for leaks, test for evc leak error
        # system_check_state == 4:   Check that pressure is reduced and calculate approximate EXC volume -> Throw valve 3 error
        # system_check_state == 5:   Check for leaks -> Throw EXC leak error
        # system_check_state == 6:   Check that EXC volume is withing limits -> Throw volume error
        # system_check_state == 7:   Open valve 4
        # system_check_state == 8:   Check that pressure is equalized -> Throw valve 4 error
        # system_check_state == 9:   Reduce pressure to MAXIMUM pressure
        # system_check_state == 10:   Open valve 2
        # system_check_state == 11:   Check that pressure is equalized -> Throw valve 2 error
        # system_check_state == 12:   Check heater
        # system_check_state == 13:   Check alcohol level -> Throw ALCOHOL_GASLEVEL_ERROR
        # system_check_state == 14:   System OK

    def Execute(self):
        super(StateSystemCheck, self).Execute()
        self.FSM.FSMOutputText = "Checking system"

        # Check alcohol level, if alcohol level it's dangerous
        # the machine must go into ALCOHOL_GASLEVEL_ERROR
        if self.system_check_state == 0:
            if ALCOHOL_SENSOR_ENABLED:
                if not self.FSM.intitialAlcoholCheckDone:
                    # Wait until alcohol sensor reading stabilizes.
                    alcohol_level = self.FSM.machine.alcohol_level

                    if alcohol_level is module_alcoholsensor.AlcoholLevelMessage.NOT_READY or alcohol_level is module_alcoholsensor.AlcoholLevelMessage.OFF:
                        return

                    alcohol_level_raw = self.FSM.machine.alcohol_level_raw

                    self._logger.debug("Alcohol level - {}: {}".format(alcohol_level, alcohol_level_raw))

                    if alcohol_level is module_alcoholsensor.AlcoholLevelMessage.DANGER:
                        self._logger.error("DANGER - High alcohol level.")
                        self.FSM.ToTransistion("toStateError")
                        self.FSM.fsmData["failure_mode"] = FailureMode.ALCOHOL_GASLEVEL_ERROR
                        self.FSM.fsmData["failure_description"] = "Alcohol gas level is too high. Unplug Merlin400, make sure there is no spilled alcohol on it. Open side covers and make sure there is no alcohol inside Merlin400. Try again after cleaning it."
                    else:
                        self._logger.info("Alcohol level ok: {}".format(alcohol_level))
                        self.FSM.intitialAlcoholCheckDone = True

                else:
                    self._logger.info("Skipping alcohol level check...")
            else:
                self._logger.info("Alcohol sensor is disabled. Skipping alcohol level check.")

            # start fan
            self.FSM.machine.fan_value = 100
            # check fan
            time.sleep(2)
            self.check_fan_is_on()

            #Chech the sanity of the pressure sensor
            pressure_lower_bound = float(self.FSM.machine._config["FSM_EV"]["ambient_pressure_lower_bound"])
            pressure_upper_bound = float(self.FSM.machine._config["FSM_EV"]["ambient_pressure_upper_bound"])

            # open all valves
            for valve in self.FSM.machine._myvalves:
                self.FSM.machine.set_valve(valve, 100)

            time.sleep(2)
            current_pressure = self.FSM.machine.pressure
            self._logger.info(
                "Checking ambient pressure at the start of distill proces. Current pressure is {}".format(
                    current_pressure
                )
            )
            if (current_pressure > pressure_upper_bound) or (current_pressure < pressure_lower_bound):
                self._logger.info("Error, bad ambient pressure level. Entering error state.")
                self.FSM.fsmData["failure_mode"] = FailureMode.PRESSURE_SENSOR_ERROR
                self.FSM.ToTransistion("toStateError")
                self.FSM.fsmData["failure_description"] = "Error, ambient pressure is either too low or too high. Pressure sensor is defective."
                return

            self.system_check_state += 1
            return

        # Close all valves
        elif self.system_check_state == 1:
            self._logger.info("System state: {}".format(self.system_check_state))

            # close all valves
            for valve in self.FSM.machine._myvalves:
                self.FSM.machine.set_valve(valve, 0)

            # turn on pump
            self.FSM.machine.pump_value = 100

            # fetch starting time
            self.start_time = time.time()

            self._logger.info(
                "System state: {} completed, reducing pressure".format(
                    self.system_check_state
                )
            )
            self.system_check_state += 1
            return

        # Reduce pressure to MAXIMIMUM, test for pump error
        elif self.system_check_state == 2:
            # self._logger.info('System state: {}'.format(self.system_check_state))
            pressure = self.FSM.machine.pressure

            _pressure_log_interval = 10 # log every ten seconds
            if not self._pressure_last_log_time or ((time.time() - self._pressure_last_log_time) > _pressure_log_interval):
                self._logger.info("Pressure: {} mbar".format(pressure))
                self._pressure_last_log_time = time.time()


            if pressure < float(
                self.FSM.machine._config["FSM_EX"]["maximum_vacuum_pressure"]
            ):
                # reached pressure target

                # turn off pump
                self.FSM.machine.pump_value = 0

                # restart timer
                self.start_time = time.time()

                self._logger.info(
                    "System state: {} completed, pressure reduced, waiting {} seconds before checking for leaks".format(
                        self.system_check_state,
                        self.FSM.machine._config["FSM_EX"]["leak_delay_time"],
                    )
                )

                # go to next stage of system init
                self.system_check_state += 1

                return

            # check for timeout
            if time.time() - self.start_time > float(
                self.FSM.machine._config["FSM_EX"]["maximum_vacuum_time"]
            ):
                self._logger.info(
                    "Error, did not reach required vacuum of {} mbar, in {} seconds".format(
                        float(
                            self.FSM.machine._config["FSM_EX"][
                                "maximum_vacuum_pressure"
                            ]
                        ),
                        float(
                            self.FSM.machine._config["FSM_EX"]["maximum_vacuum_time"]
                        ),
                    )
                )

                # Check for absurdly high pressure that indicates that the gasket is leaking
                # 900 is the pressure in mbar
                if pressure > 900:
                    mypressure = self.FSM.machine.pressure
                    self.FSM.fsmData["failure_mode"] = FailureMode.EVC_LEAK
                    self.FSM.fsmData["failure_description"] = "Unable to pull proper vacuum (pressure: {} mbar) in the distillation chamber. You most likely have a very high leak. Please check and clean top and bottom gaskets, make sure the glass is properly in place, the lid is closed and try again. If that fails contact support@drizzle.life".format(round(mypressure,2))
                    self._logger.debug(self.FSM.fsmData["failure_description"])
                    self.FSM.ToTransistion("toStateError")
                    return

                #Start by checking for leaks
                if not self.FSM.hasPumpErrorBeenChecked:
                    print('Error, pump might be defective, checking')

                    #OK, the error was not a leak, proceed to vent pump
                    self.FSM.hasPumpErrorBeenChecked = True

                    pressure_check_1 = self.FSM.machine.pressure

                    #switch off pump
                    self.FSM.machine.pump_value = 0

                    #let pressure stabilize just a bit before
                    time.sleep(2)

                    #check that pressure has not just jumped like crazy
                    pressure_check_2 = self.FSM.machine.pressure
                    pressure_increase = pressure_check_2 - pressure_check_1
                    if pressure_increase > 5:
                        #definately got a leak!
                        leak_quant = round(pressure_increase / 2, 2)
                        self._logger.debug('Error, pressure increased {} mbar during a two second period. Limit is 5 mbar. Leak detected'.format(pressure_increase))
                        self.FSM.fsmData["failure_mode"] = FailureMode.EVC_LEAK
                        self.FSM.fsmData["failure_description"] = 'High leak ({} mbar/s) in the distillation chamber. Please check and clean top and bottom gaskets and make sure glass is properly in place. Try again after cleaning.'.format(leak_quant)
                        self.FSM.ToTransistion("toStateError")
                        return
                    else:
                        #retest with stable pressure
                        time.sleep(4)
                        pressure_check_3 = self.FSM.machine.pressure
                        pressure_increase = pressure_check_3 - pressure_check_2
                        if pressure_increase > 10:
                            #definately got a leak!
                            leak_quant = round(pressure_increase / 4, 2)
                            self._logger.debug('Error, pressure increased {} mbar during a four second period. Limit is 10 mbar. Leak detected'.format(pressure_increase))
                            self.FSM.fsmData["failure_mode"] = FailureMode.EVC_LEAK
                            self.FSM.fsmData["failure_description"] = 'High leak ({} mbar/s) in the distillation chamber. Please check and clean top and bottom gaskets and make sure glass is properly in place. Try again after cleaning.'.format(leak_quant)
                            self.FSM.ToTransistion("toStateError")
                            return


                # attempt to correct pressure loss problem before going into error
                if self.FSM.numberOfVentingRetries >= 3:
                    self._logger.info("Error, venting did not work")
                    self.FSM.ToTransistion("toStateError")
                    self.FSM.fsmData["failure_mode"] = FailureMode.PUMP_NEEDS_CLEAN_OR_REPLACEMENT
                    self.FSM.fsmData["failure_description"] = "Pump could not pull enough vacuum. Please refer to website for guide to cleaning your pump. If you have allready done so, the pump may be defective. Please contact support@drizzle.life"
                else:
                    self._logger.debug(
                        "Attempting to vent pump, attempt number {}".format(
                            self.FSM.numberOfVentingRetries
                        )
                    )
                    self.FSM.numberOfVentingRetries += 1
                    self.FSM.startExtractAfterVent = True
                    self.FSM.ToTransistion("toStateVentPump")
                return

        # Check for leaks, test for evc leak error - part one - wait for pressure to stabilize and get first pressure reading
        elif self.system_check_state == 3:
            # wait pressure_sample_delay seconds before starting measurement
            if time.time() - self.start_time > float(
                self.FSM.machine._config["FSM_EX"]["leak_delay_time"]
            ):
                # read initial pressure
                self.start_pressure = self.FSM.machine.pressure

                # reset timer
                self.start_time = time.time()

                self._logger.info(
                    "System state: {} completed, pressure reduced, checking for leaks".format(
                        self.system_check_state
                    )
                )

                # go to next state of system init
                self.system_check_state += 1
                return

        # Check for leaks, test for evc leak error - part two - wait for predefined time before taking second leak measurement
        elif self.system_check_state == 4:
            # wait leak_sample_time before processing
            if time.time() > self.start_time + float(
                self.FSM.machine._config["FSM_EX"]["leak_sample_time"]
            ):
                self.stop_pressure = self.FSM.machine.pressure
                self._logger.info(
                    "Pressure leak: {:.02f} mbar, time is: {:.02f} seconds".format(
                        self.stop_pressure - self.start_pressure,
                        float(self.FSM.machine._config["FSM_EX"]["leak_sample_time"]),
                    )
                )
                # caculate pressure leak
                self.pressure_leak = math.get_pressure_leak(
                    self.stop_pressure,
                    self.start_pressure,
                    time.time(),
                    self.start_time,
                )

                if self.pressure_leak > float(
                    self.FSM.machine._config["FSM_EX"]["max_pressure_loss_evc"]
                ):
                    self._logger.warning(
                        "Leak is too high: {:.02f} mbar/sec, max allowed is: {:.02f} mbar/sec".format(
                            self.pressure_leak,
                            float(
                                self.FSM.machine._config["FSM_EX"][
                                    "max_pressure_loss_evc"
                                ]
                            ),
                        )
                    )
                    self.FSM.FSMOutputText = "Error - EVC has leaks"

                    # go to error state
                    self.FSM.ToTransistion("toStateError")
                    self.FSM.fsmData["failure_mode"] = FailureMode.EVC_LEAK
                    self.FSM.fsmData["failure_description"] = 'Small leak ({} mbar/s) in the distillation chamber. Please check and clean top and bottom gaskets and make sure glass is properly in place. Try again after cleaning.'.format(round(self.pressure_leak, 2))
                    return

                else:
                    self._logger.info(
                        "Leak is ok: {:.02f} mbar/sec, max allowed is: {:.02f} mbar/sec".format(
                            self.pressure_leak,
                            float(
                                self.FSM.machine._config["FSM_EX"][
                                    "max_pressure_loss_evc"
                                ]
                            ),
                        )
                    )
                    self.FSM.fsmData["system_leak"] = self.pressure_leak

                    # fetch starting pressure
                    self.last_pressure = self.FSM.machine.pressure

                    # open valve
                    self.FSM.machine.set_valve("valve3", 100)

                    # reset timer
                    self.start_time = time.time()

                    self._logger.info(
                        "System state: {}, checking for EXC volume".format(
                            self.system_check_state
                        )
                    )

                    # pressure loss ok, move on!
                    self.system_check_state += 1

                    return

        # Waits for pressure to stabilize and calculates EXC volume
        elif self.system_check_state == 5:

            # wait for pressure to stabilize
            if time.time() - self.start_time > float(
                self.FSM.machine._config["FSM_EX"]["pressure_eq_time"]
            ):
                evc_volume = float(self.FSM.machine._config["FSM_EX"]["evc_volume"])

                # get new pressure
                full_system_pressure = self.FSM.machine.pressure

                # we should atleast see a 100 mbar increase in pressure
                if self.last_pressure + 100 > full_system_pressure:
                    self._logger.error(
                        "Error, pressure did not increase enough. Please check valve 3."
                    )
                    # pressure did not increase enough. Something is wrong!
                    self.FSM.ToTransistion("toStateError")
                    self.FSM.fsmData["failure_mode"] = FailureMode.VALVE_3_BLOCKED
                    self.FSM.fsmData["failure_description"] = "Valve 3 (Extractor -> Distiller) seems stuck or clogged. Please refer to the article on cleaning the valves on our website."

                # calculate volume
                exc_volume_raw = math.calculate_raw_volume(
                    full_system_pressure,
                    evc_volume,
                    self.start_pressure,
                    self.FSM.fsmData["atm_pressure"],
                )
                exc_volume_converted_to_liquid = (
                    self.FSM.machine.convert_air_volume_to_plant_and_liquid_volume(
                        exc_volume_raw
                    )
                )
                tot_volume = evc_volume + exc_volume_raw
                self.FSM.fsmData["exc_volume"] = exc_volume_raw
                self.FSM.fsmData["total_volume"] = tot_volume
                self.FSM.fsmData["exc_volume_liquid"] = exc_volume_converted_to_liquid

                # calculate total amount of liquid to aspirate
                self.FSM.fsmData["total_liquid_volume_to_aspirate"] = (
                    exc_volume_converted_to_liquid
                    + self.FSM.fsmData["aspirate_volume_target"]
                )
                # calculate total runtime (seconds)
                self.FSM.fsmData["total_runtime_theoretical"] = self.FSM.fsmData[
                    "total_liquid_volume_to_aspirate"
                ] / float(self.FSM.machine._config["FSM_EX"]["aspirate_speed"])
                self._logger.info(
                    "Theoretical runtime is: {} seconds".format(
                        self.FSM.fsmData["total_runtime_theoretical"]
                    )
                )

                self._logger.info(
                    "System state: {}, got exc vol: {} mL, total vol: {} mL, liquid vol: {} mL".format(
                        self.system_check_state,
                        exc_volume_raw,
                        tot_volume,
                        exc_volume_converted_to_liquid,
                    )
                )

                # do volumecheck here and transition to error if required
                if exc_volume_raw > 500:
                    # definately an error if volume is above 500 mL
                    self._logger.error(
                        "Initialization error, EXC is probably leaking. Please check gaskets"
                    )
                    self.FSM.ToTransistion("toStateError")
                    self.FSM.fsmData["failure_mode"] = FailureMode.EXC_LEAK
                    self.FSM.fsmData["failure_description"] = "There is a leak in the extraction chamber. Please check that the upper and lower gaskets are in place and try again. If the error persists, please check that the front of the lid is not broken and contact support@drizzle.life"

                # reset timer
                self.start_time = time.time()

                self.start_pressure = full_system_pressure

                self._logger.info(
                    "System state: {} complete, checking for leaks".format(
                        self.system_check_state
                    )
                )

                # go to next substate
                self.system_check_state += 1

                return

        # Check for leaks -> Throw EXC leak error
        elif self.system_check_state == 6:

            # wait pressure_sample_delay seconds before starting measurement
            if time.time() > self.start_time + float(
                self.FSM.machine._config["FSM_EX"]["leak_sample_time"]
            ):
                # read initial pressure
                stop_pressure = self.FSM.machine.pressure

                # calculate pressure leak
                self.pressure_leak = (stop_pressure - self.start_pressure) / (
                    time.time() - self.start_time
                )

                # check against config
                if self.pressure_leak > float(
                    self.FSM.machine._config["FSM_EX"]["max_pressure_loss_evc"]
                ):
                    self._logger.warning(
                        "Leak is too high: {:.02f} mbar/sec, max allowed is: {:.02f} mbar/sec".format(
                            self.pressure_leak,
                            float(
                                self.FSM.machine._config["FSM_EX"][
                                    "max_pressure_loss_evc"
                                ]
                            ),
                        )
                    )
                    self.FSM.FSMOutputText = "Error - EXC has leaks"

                    # go to error state
                    self.FSM.ToTransistion("toStateError")
                    self.FSM.fsmData["failure_mode"] = FailureMode.EXC_LEAK
                    self.FSM.fsmData["failure_description"] = "Leaks from extraction chamber detected, check gaskets, check that the tubes are in the valves, and check the EXC lid for signs of cracks or stretches."

                    return

                else:
                    self._logger.info(
                        "Leak is ok: {:.02f} mbar/sec, max allowed is: {:.02f} mbar/sec".format(
                            self.pressure_leak,
                            float(

                                self.FSM.machine._config["FSM_EX"][
                                    "max_pressure_loss_evc"
                                ]
                            ),
                        )
                    )
                    self.FSM.fsmData["system_leak"] = self.pressure_leak

                    # reset timer
                    self.start_time = time.time()

                    # pressure loss ok, move on!
                    self.system_check_state += 1

                    return

        # Open valve 4
        elif self.system_check_state == 7:
            self._logger.info("System state: {}".format(self.system_check_state))

            #store pressure before opening
            self.start_pressure = self.FSM.machine.pressure

            self.FSM.machine.set_valve("valve4", 100)
            self.start_time = time.time()
            self.system_check_state += 1

            return

        # Check that pressure is equalized -> Throw valve 4 error
        elif self.system_check_state == 8:

            # wait for pressure to stabilize
            if time.time() - self.start_time > float(
                self.FSM.machine._config["FSM_EX"]["pressure_eq_time"]
            ):
                # do check here
                pressure = self.FSM.machine.pressure

                # we should at least see 100 mbar increase in pressure
                if pressure > (self.start_pressure + 100):
                    # ok if we get here
                    self._logger.info(
                        "Valve4 is ok, pressure rose from {} mbar to {} mbar".format(
                            self.start_pressure, pressure
                        )
                    )

                    # close valve4 again and start reducing pressure
                    self.FSM.machine.set_valve("valve4", 0)

                    # start pump
                    self.FSM.machine.pump_value = 100

                    # reset timer
                    self.start_time = time.time()
                    self._logger.info(
                        "System state: {} comlpete, reducing pressure".format(
                            self.system_check_state
                        )
                    )
                    # go to next substate
                    self.system_check_state += 1
                else:
                    self.FSM.ToTransistion("toStateError")
                    self.FSM.fsmData["failure_mode"] = FailureMode.VALVE_4_BLOCKED
                    self.FSM.fsmData["failure_description"] = "Valve 4 (Air -> Distiller) seems stuck or clogged. Please refer to the article on cleaning the valves on our website."

        # Reduce pressure to MAXIMUM pressure
        elif self.system_check_state == 9:
            pressure = self.FSM.machine.pressure
            _pressure_log_interval = 10 # log every ten seconds
            if not self._pressure_last_log_time or ((time.time() - self._pressure_last_log_time) > _pressure_log_interval):
                self._logger.info("Pressure: {} mbar".format(pressure))
                self._pressure_last_log_time = time.time()

            if pressure < float(
                self.FSM.machine._config["FSM_EX"]["maximum_vacuum_pressure"]
            ):
                # reached pressure target
                self._logger.info(
                    "System state: {} completed, reducing pressure".format(
                        self.system_check_state
                    )
                )

                # turn off pump
                self.FSM.machine.pump_value = 0

                # open valve2 to equalize pressure
                self.FSM.machine.set_valve("valve2", 100)

                # restart timer
                self.start_time = time.time()

                # go to next stage of system init
                self.system_check_state += 1

                self._logger.info(
                    "System state: {}, checking pressure equalization".format(
                        self.system_check_state
                    )
                )

                return

            # check for timeout
            if (time.time() - self.start_time) > float(
                self.FSM.machine._config["FSM_EX"]["maximum_vacuum_time"]
            ):
                self._logger.error(
                    "Error, did not reach required vacuum of {} mbar, in {} seconds".format(
                        float(
                            self.FSM.machine._config["FSM_EX"][
                                "maximum_vacuum_pressure"
                            ]
                        ),
                        float(
                            self.FSM.machine._config["FSM_EX"]["maximum_vacuum_time"]
                        ),
                    )
                )

                # go to error state
                self.FSM.ToTransistion("toStateError")
                self.FSM.fsmData["failure_mode"] = FailureMode.PUMP_NEEDS_CLEAN_OR_REPLACEMENT
                self.FSM.fsmData["failure_description"] = "Pump seems to be dirty or damaged. Please run the extended cleaning, as described in the manual, and check our knowledgebase online for further tips."
                return

        # Wait for pressure to equalize and check resulting pressure
        elif self.system_check_state == 10:

            # wait for pressure to stabilize
            if time.time() - self.start_time > float(
                self.FSM.machine._config["FSM_EX"]["pressure_eq_time"]
            ):
                # do check here
                pressure = self.FSM.machine.pressure

                # we should at least see the pressure hit 100 mbar below atmospheric pressure
                if pressure > (self.FSM.fsmData["atm_pressure"] - 100):
                    # ok if we get here
                    self._logger.info("Valve2 is ok, pressure rose to {} mbar".format(pressure))

                    # close all valves
                    for valve in self.FSM.machine._myvalves:
                        self.FSM.machine.set_valve(valve, 0)

                    # yes, check is ok! Go for heat check
                    self._logger.info("Pressure system check ok! Go for heat check")
                    self.system_check_state += 1
                    return
                else:
                    self._logger.error(
                        "Error in state 10, did not see correct fall in pressure. Atm pressure is {} mbar, pressure measured was: {} bar".format(
                            self.FSM.fsmData["atm_pressure"], pressure
                        )
                    )
                    self.FSM.ToTransistion("toStateError")
                    self.FSM.fsmData["failure_mode"] = FailureMode.VALVE_2_BLOCKED
                    self.FSM.fsmData["failure_description"] = "Valve 2 (Air -> Extraction) seems stuck or clogged. Please refer to the article on cleaning the valves on our website."

        # Check heater
        elif self.system_check_state == 11:
            self.start_time = time.time()

            # Read heater temperature
            self.start_temp = self.FSM.machine.bottom_temperature

            # run heater at 100%
            self.FSM.machine.bottom_heater_percent = 100

            self._logger.info(
                "Pressure test passed - checking heater...".format(
                    self.system_check_state
                )
            )

            self.system_check_state += 1

        # Wait a peroid of time and check heater increase
        elif self.system_check_state == 12:
            stop_temp = self.FSM.machine.bottom_temperature

            # check for successfull heating
            if stop_temp - self.start_temp > 5:
                # yes, heater check is ok! Go for extraction
                self.FSM.machine.bottom_heater_percent = 0
                self._logger.info("Heat and system check ok!")
                self.system_check_state += 1
            else:
                # Max time to heat is 20 seconds
                if (time.time() - self.start_time) > 20:
                    # Heater off
                    self.FSM.machine.bottom_heater_percent = 0

                    # got a heater error
                    self._logger.error("Heater error, please check")
                    self.FSM.ToTransistion("toStateError")
                    self.FSM.fsmData["failure_mode"] = FailureMode.HEATER_ERROR
                    self.FSM.fsmData["failure_description"] = "Heater error. Please swtich off machine completely, wait 1 minute and turn it back on and try again. If the problem persists your heater cable is damaged or the thermal fuse is blown. Conact support@drizzle.life for help."

        # Final alcohol level chec. If level is still in in warning or
        # danger zone - throw en error.
        elif self.system_check_state == 13:
            if ALCOHOL_SENSOR_ENABLED:
                alcohol_level = self.FSM.machine.alcohol_level
                self._logger.debug("Alcohol level - {}.".format(alcohol_level))
                if alcohol_level is module_alcoholsensor.AlcoholLevelMessage.NOT_READY:
                    self._logger.error("Failed to read alcohol level.")
                    self.FSM.ToTransistion("toStateError")
                    self.FSM.fsmData["failure_mode"] = FailureMode.ALCOHOL_GASLEVEL_ERROR
                    self.FSM.fsmData["failure_description"] = "Failed to read alcohol gas level, please contact support@drizzle.life"

                if alcohol_level in (
                    module_alcoholsensor.AlcoholLevelMessage.DANGER,
                    module_alcoholsensor.AlcoholLevelMessage.WARNING
                ):
                    self._logger.error("DANGER - Alcohol level is still too high.")
                    self.FSM.ToTransistion("toStateError")
                    self.FSM.fsmData["failure_mode"] = FailureMode.ALCOHOL_GASLEVEL_ERROR
                    self.FSM.fsmData["failure_description"] = "Error, alcohol gas level is too high. If you have spilled in or around the machine, please wipe it off with a cloth. Wait a few more minutes and try again. If the problem persists, drain the machine completely of alcohol and leave the machine for a day at room temperature or slightly higher."
                else:
                    self._logger.info("Alcohol level is ok. Starting full extraction.")
                    self.system_check_state += 1
                    self.FSM.ToTransistion("toStatePreFillTubes")
            else:
                self._logger.info("Alcohol sensor is disabled. Starting full extraction.")
                self.system_check_state += 1
                self.FSM.ToTransistion("toStatePreFillTubes")

    def Exit(self):
        super(StateSystemCheck, self).Exit()


# Fill inlet tubes
class StatePreFillTubes(State):
    def __init__(self, FSM):
        super(StatePreFillTubes, self).__init__(FSM)

    def Enter(self):
        super(StatePreFillTubes, self).Enter()

        self.humanReadableLabel = "Prefilling tubes"
        self.FSM.FSMOutputText = "Prefilling tubes"

        self._logger.info("Draining system")
        # drain system
        self.FSM.machine.drain_system()

        # read and store atmospheric pressure
        self.FSM.SetFSMData("atm_pressure", self.FSM.machine.pressure)

        self._logger.info("Setting valves in tube filling position")
        # open valve 3 and close valves 1+2+4
        self.FSM.machine.set_valve("valve1", 0)
        self.FSM.machine.set_valve("valve2", 0)
        self.FSM.machine.set_valve("valve3", 100)
        self.FSM.machine.set_valve("valve4", 0)

        # turn on warm light
        self.FSM.machine.light_warm()

        # Start pump at 100#
        self._logger.info("Reducing pressure")
        self.FSM.machine.pump_value = 100

    def Execute(self):
        super(StatePreFillTubes, self).Execute()
        pressure = self.FSM.machine.pressure
        self.FSM.FSMOutputText = "System depressuring: {:.02f} mbar".format(pressure)

        # Condition to see if we need to change state
        if pressure < float(self.FSM.machine._config["FSM_EX"]["tube_filling_vacuum"]):
            # Turn off pump
            self.FSM.machine.pump_value = 0

            # do fast liquid flush
            self.FSM.machine.set_valve(
                "valve1",
                float(self.FSM.machine._config["FSM_EX"]["valve_start_close_value"]),
            )

            time.sleep(
                float(self.FSM.machine._config["FSM_EX"]["valve_start_close_time"])
            )

            self.FSM.ToTransistion("toStateFirstDepressurize")
            self._logger.info("maximum vacuum pressure achieved")


# Depressure state
class StateFirstDepressurize(State):
    def __init__(self, FSM):
        super(StateFirstDepressurize, self).__init__(FSM)

    def Enter(self):
        super(StateFirstDepressurize, self).Enter()

        # depressurize
        self.FSM.FSMOutputText = "Depressurising init"
        self.humanReadableLabel = "Checking for leaks"

        # close all valves
        for valve in self.FSM.machine._myvalves:
            self.FSM.machine.set_valve(valve, 0)

        self._pressure_last_log_time = None

        # Start pump at 100#
        self.FSM.machine.pump_value = 100

    def Execute(self):
        super(StateFirstDepressurize, self).Execute()
        pressure = self.FSM.machine.pressure
        self.FSM.FSMOutputText = "System depressuring: {:.02f} mbar".format(pressure)
        _pressure_log_interval = 10 # log every ten seconds
        if not self._pressure_last_log_time or ((time.time() - self._pressure_last_log_time) > _pressure_log_interval):
            self._logger.info(self.FSM.FSMOutputText)
            self._pressure_last_log_time = time.time()

        # Condition to see if we need to change state
        if pressure < float(
            self.FSM.machine._config["FSM_EX"]["maximum_vacuum_pressure"]
        ):
            self.FSM.ToTransistion("toStateMeasureEXCVolume")
            self._logger.info("maximum vacuum pressure achieved")

    def Exit(self):
        super(StateFirstDepressurize, self).Exit()
        # Turn off pump
        self.FSM.machine.pump_value = 0
        self.FSM.FSMOutputText = "Exiting depressuring state"
        self._logger.info(self.FSM.FSMOutputText)


# Measure exc volume
class StateMeasureEXCVolume(State):
    def __init__(self, FSM):
        super(StateMeasureEXCVolume, self).__init__(FSM)

    def Enter(self):
        super(StateMeasureEXCVolume, self).Enter()
        self.FSM.FSMOutputText = "Init exc volume measurement"
        self.humanReadableLabel = "Checking for leaks"
        self._logger.info(self.FSM.FSMOutputText)

        # read initial pressure
        self._initial_pressure = self.FSM.machine.pressure

        # open valve to equalize pressure
        self.FSM.machine.set_valve("valve3", 100)

    def Execute(self):
        super(StateMeasureEXCVolume, self).Execute()
        self.FSM.FSMOutputText = "Measuring exc volume"
        evc_volume = float(self.FSM.machine._config["FSM_EX"]["evc_volume"])

        # Wait a period of time untill pressure has stabilized
        if float(self.eventDuration) > float(
            self.FSM.machine._config["FSM_EX"]["pressure_eq_time"]
        ):
            # get new pressure
            full_system_pressure = self.FSM.machine.pressure

            self._logger.info("initial pressure: {}".format(self._initial_pressure))
            self._logger.info("full system pressure: {}".format(full_system_pressure))
            self._logger.info("evc volume: {}".format(evc_volume))

            # calculate volume
            exc_volume_raw = math.calculate_raw_volume(
                full_system_pressure,
                evc_volume,
                self._initial_pressure,
                self.FSM.fsmData["atm_pressure"],
            )
            exc_volume_converted_to_liquid = (
                self.FSM.machine.convert_air_volume_to_plant_and_liquid_volume(
                    exc_volume_raw
                )
            )
            tot_volume = evc_volume + exc_volume_raw
            self.FSM.fsmData["exc_volume"] = exc_volume_raw
            self.FSM.fsmData["total_volume"] = tot_volume
            self.FSM.fsmData["exc_volume_liquid"] = exc_volume_converted_to_liquid
            self.FSM.ToTransistion("toStateSecondDepressurize")

    def Exit(self):
        super(StateMeasureEXCVolume, self).Exit()
        self.FSM.FSMOutputText = "Exiting exc volume measurement"
        self._logger.info(self.FSM.FSMOutputText)


# Depressurize
class StateSecondDepressurize(State):
    def __init__(self, FSM):
        super(StateSecondDepressurize, self).__init__(FSM)

    def Enter(self):
        super(StateSecondDepressurize, self).Enter()
        self.FSM.FSMOutputText = "Depressurising init"
        self.humanReadableLabel = "Checking for leaks"
        self._logger.info(self.FSM.FSMOutputText)

        # Start pump at 100#
        self.FSM.machine.pump_value = 100

    def Execute(self):
        super(StateSecondDepressurize, self).Execute()
        self.FSM.FSMOutputText = "Depressurising"

        pressure = self.FSM.machine.pressure
        self.FSM.FSMOutputText = "System depressuring: {:.02f} mbar".format(pressure)

        # wait for atleast two seconds before measuring
        if self.eventDuration > 2:
            # if pressure is below requirement, proceed
            if pressure < float(
                self.FSM.machine._config["FSM_EX"]["maximum_vacuum_pressure"]
            ):
                self.FSM.ToTransistion("toStateSecondLeakCheck")

    def Exit(self):
        super(StateSecondDepressurize, self).Exit()
        self.FSM.FSMOutputText = "Exiting depressurization"
        self._logger.info(self.FSM.FSMOutputText)


# Check for leaks
class StateSecondLeakCheck(State):
    def __init__(self, FSM):
        super(StateSecondLeakCheck, self).__init__(FSM)

    def Enter(self):
        super(StateSecondLeakCheck, self).Enter()
        self.humanReadableLabel = "Checking for leaks"
        self._first_measurement = True
        self.FSM.machine.pump_value = 0
        self._logger.info("Entering state 6")

    def Execute(self):
        super(StateSecondLeakCheck, self).Execute()

        leak_delay_time = float(self.FSM.machine._config["FSM_EX"]["leak_delay_time"])
        leak_sample_time = float(self.FSM.machine._config["FSM_EX"]["leak_sample_time"])

        self.FSM.FSMOutputText = "{} sec delay before full system leak check".format(
            leak_delay_time
        )

        if self._first_measurement and (self.eventDuration > leak_delay_time):
            self._first_measurement = False
            # read initial pressure
            self._start_pressure = self.FSM.machine.pressure

        if not self._first_measurement and (
            self.eventDuration > (leak_delay_time + leak_sample_time)
        ):
            self._stop_pressure = self.FSM.machine.pressure
            self._logger.info(
                "Pressure leak: {:.02f} mbar, time is: {:.02f} seconds".format(
                    self._stop_pressure - self._start_pressure,
                    leak_sample_time,
                )
            )
            self._pressure_leak = math.get_pressure_leak_by_sample_time(
                self._stop_pressure,
                self._start_pressure,
                leak_sample_time,
            )
            self._logger.info(
                "Leak is: {:.02f} mbar/sec, max allowed is: {:.02f} mbar/sec".format(
                    self._pressure_leak,
                    float(self.FSM.machine._config["FSM_EX"]["max_pressure_loss_evc"]),
                )
            )
            # store pressure leak for future use
            self.FSM.fsmData["system_leak"] = self._pressure_leak
            self.FSM.ToTransistion("toStateTopUpEXC")

    def Exit(self):
        super(StateSecondLeakCheck, self).Exit()
        self.FSM.FSMOutputText = "Exiting leak check"
        self._logger.info(self.FSM.FSMOutputText)


# TopUpEXC
class StateTopUpEXC(State):
    def __init__(self, FSM):
        super(StateTopUpEXC, self).__init__(FSM)

    def Enter(self):
        self.FSM.FSMOutputText = "Topping up exc"
        self.humanReadableLabel = "Soaking herb"
        super(StateTopUpEXC, self).Enter()

        # make sure all valves are closed
        for valve in self.FSM.machine._myvalves:
            self.FSM.machine.set_valve(valve, 0)

        self.start_time = time.time()

    def Execute(self):
        super(StateTopUpEXC, self).Execute()
        # frequency between each adjustment
        self.FSM.FSMOutputText = "Topping up"
        # open valve1
        self.FSM.machine.set_valve("valve1", 100)

        # TODO: Add as a config.ini variable
        top_up_time = float(self.FSM.machine._config["FSM_EX"]["top_up_time"])
        top_up_afterfill_valve_setting = float(
            self.FSM.machine._config["FSM_EX"]["top_up_afterfill_valve_setting"]
        )

        # and wait for chamber to fully fill
        if (time.time() - self.start_time) < top_up_time:
            pass
        else:
            # fill tubes from exc to evc
            self.FSM.machine.set_valve("valve3", top_up_afterfill_valve_setting)
            self.FSM.machine.set_valve("valve3", 0)
            self.FSM.ToTransistion("toStateSoak")

    def Exit(self):
        super(StateTopUpEXC, self).Exit()
        self.FSM.FSMOutputText = "Done topping up"

        # close all valves
        for valve in self.FSM.machine._myvalves:
            self.FSM.machine.set_valve(valve, 0)

        self._logger.info(self.FSM.FSMOutputText)


class StateSoak(State):
    def __init__(self, FSM):
        super(StateSoak, self).__init__(FSM)

    def Enter(self):
        super(StateSoak, self).Enter()
        self.FSM.FSMOutputText = "Soak init"
        self.humanReadableLabel = "Soak state"
        self.FSM.machine.set_valve("valve1", 0)
        self.FSM.machine.set_valve("valve3", 0)
        self._start_time = time.time()
        self._wait_time_seconds = float(self.FSM.machine._config["SYSTEM"]["soak_time_seconds"])
        self.FSM.FSMOutputText = "Waiting for {} seconds.".format(self._wait_time_seconds)
        self._logger.info(self.FSM.FSMOutputText)

    def Execute(self):
        super(StateSoak, self).Execute()
        elapsed_time = time.time() - self._start_time
        if elapsed_time > self._wait_time_seconds:
            self._logger.info("Finished waiting.")
            self.FSM.ToTransistion("toStateThirdDepressurize")
        time.sleep(1)

    def Exit(self):
        super(StateSoak, self).Exit()
        self.FSM.FSMOutputText = "Exiting soak state"
        self._logger.info(self.FSM.FSMOutputText)


# Third depressure state before doing final aspirate
class StateThirdDepressurize(State):
    def __init__(self, FSM):
        super(StateThirdDepressurize, self).__init__(FSM)

    def Enter(self):
        super(StateThirdDepressurize, self).Enter()

        # depressurize
        self.FSM.FSMOutputText = "Depressurising init"
        self.humanReadableLabel = "Checking for leaks"

        # close all valves
        for valve in self.FSM.machine._myvalves:
            self.FSM.machine.set_valve(valve, 0)

        self._pressure_last_log_time = None

        # Start pump at 100#
        self.FSM.machine.pump_value = 100

    def Execute(self):
        super(StateThirdDepressurize, self).Execute()
        pressure = self.FSM.machine.pressure
        self.FSM.FSMOutputText = "System depressuring: {:.02f} mbar".format(pressure)

        _pressure_log_interval = 10 # log every ten seconds
        if not self._pressure_last_log_time or ((time.time() - self._pressure_last_log_time) > _pressure_log_interval):
            self._logger.info(self.FSM.FSMOutputText)
            self._pressure_last_log_time = time.time()

        # Condition to see if we need to change state
        if pressure < float(
            self.FSM.machine._config["FSM_EX"]["maximum_vacuum_pressure"]
        ):
            self.FSM.ToTransistion("toStateAspirate")
            self._logger.info("maximum vacuum pressure achieved")

    def Exit(self):
        super(StateThirdDepressurize, self).Exit()
        # Turn off pump
        self.FSM.machine.pump_value = 0
        # wait for pressure to stabilize
        time.sleep(float(self.FSM.machine._config["FSM_EX"]["leak_delay_time"]))
        self.FSM.FSMOutputText = "Exiting depressuring state"
        self._logger.info(self.FSM.FSMOutputText)


# Aspirate state
class StateAspirate(State):
    def __init__(self, FSM):
        super(StateAspirate, self).__init__(FSM)

    def add_flowrate(self, flowrate):
        interval_seconds = 60  # interval to calculate average flowrate.
        curr_time = time.time()
        for idx, (ts, _) in enumerate(self._flowrate_container):
            if (curr_time - ts) > interval_seconds:
                self._flowrate_container.pop(idx)
        self._flowrate_container.append((curr_time, flowrate))

    @property
    def flowrate_avg(self):
        return sum([item[1] for item in self._flowrate_container])/len(self._flowrate_container)

    def Enter(self):
        self.FSM.FSMOutputText = "Init Aspirate"
        self.humanReadableLabel = "Aspirating solvent"
        super(StateAspirate, self).Enter()

        # start with a small delay to allow pressure to stabilize
        time.sleep(2)

        # fetch variables for dictionary
        self._total_volume = float(self.FSM.machine._config["FSM_EX"]["evc_volume"])
        self._aspirate_volume_target = float(
            self.FSM.machine._config["FSM_EX"]["aspirate_volume"]
        )

        # this does not compensate for flow speed error over time, and uses the aspirate_speed variable instead of the corrected one
        self._aspirate_speed_target = float(
            self.FSM.machine._config["FSM_EX"]["aspirate_speed"]
        )
        # assume flow is zero and fetch values from there
        (
            self._current_step_size,
            self._current_sample_period,
        ) = self.FSM.machine.get_step_and_period(0)
        self._current_flow_error_pct = 0

        # used to adjust leak detect on the fly
        self._historic_leak = 0

        # read initial pressure
        self._pressure_test_start = self.FSM.machine.pressure

        self._current_time_start = time.time()
        self._last_known_valve_setting_measured = False

        # calculate pv contant for volume measuremnts
        self._pv_const = math.get_pv_const(
            self._pressure_test_start, self._total_volume
        )

        # set valve to last know run setting - the hardcoded 2 is the amount we want the valve to start under the target - add as config number later
        self._valve_setting = (
            float(self.FSM.machine._config["FSM_EV"]["valve_last_known_setting"]) - 2
        )
        self.FSM.machine.set_valve("valve3", 0)
        self.FSM.machine.set_valve("valve1", 0)

        # calculates the accumulated pressure loss due to leaks
        self._total_volume_aspirated_start = 0

        # Get flowrate fall limit from the config, measured in ml/sec
        self._flowrate_fall_limit = float(
            self.FSM.machine._config["FSM_EX"]["flowrate_fall_limit"]
        )
        self._flowrate_warning_limit = self._aspirate_speed_target / 2
        # variable to hold flowrate values for avg calculation.
        self._flowrate_container = []

        # wait a second before detecting leaks
        time.sleep(1)

        # get starting pressure and time
        pressure_leak_detect_start = self.FSM.machine.pressure
        time_leak_detect_start = time.time()
        time.sleep(float(self.FSM.machine._config["FSM_EV"]["leak_detect_duration"]))

        # get pressure and time at stop
        pressure_leak_detect_stop = self.FSM.machine.pressure
        time_leak_detect_stop = time.time()

        # calculate corrected leak
        self._system_leak = math.get_pressure_leak(
            pressure_leak_detect_stop,
            pressure_leak_detect_start,
            time_leak_detect_stop,
            time_leak_detect_start,
        )
        self.FSM.fsmData["system_leak"] = self._system_leak

        # setup timing variables
        self._last_run_time = time.time()
        self._last_leak_detect = time.time()
        self._last_pressure_loss_time = time.time()

        self._aspirate_last_log_time = None

        # open valve1
        self.FSM.machine.set_valve("valve1", 100)

        # wait a little while before opening valve3
        time.sleep(1)

        # open valve3 to initial setting
        self.FSM.machine.set_valve("valve3", self._valve_setting)

    def Execute(self):
        super(StateAspirate, self).Execute()

        self.FSM.FSMOutputText = "Aspirating solvent"

        # if valve adjustment period has run, start next cycle
        if (
            self._last_run_time
            + float(self.FSM.machine._config["FSM_EV"]["valve_adjust_delay"])
        ) < time.time():
            self._last_run_time = time.time()

            # sample time and pressure
            self._current_time_stop = time.time()
            self._pressure_test_stop = self.FSM.machine.pressure

            # calculate total aspiration volume based on volumechange, using a derived ideal gas equation
            # We removed the leak compensation, it is more precise without
            # historic_leak is an artifact from an attemp to implement on the fly leak testing. It is simply set to zero.
            # system_leak is kept static throughout the aspiration.
            leakfactor = math.get_leakfactor(
                time.time(),
                self._last_leak_detect,
                self._system_leak,
                self._historic_leak,
            )
            self._total_volume_aspirated_stop = math.get_total_volume_aspiration(
                self._total_volume,
                self._pv_const,
                self._pressure_test_stop - leakfactor,
            )

            # update FSM database
            self.FSM.SetFSMData(
                "aspirate_volume_actual",
                self._total_volume_aspirated_stop,
            )

            # calculate current flowrate
            self._flowrate_actual = math.get_flowrate(
                self._total_volume_aspirated_stop,
                self._total_volume_aspirated_start,
                self._current_time_stop,
                self._current_time_start,
            )

            # Add current flowrate value to running average calculations.
            self.add_flowrate(self._flowrate_actual)

            # calculate flowrate error
            self._current_flow_error_pct = (
                self._flowrate_actual / self._aspirate_speed_target * 100
            )

            # get adjustment paraters for valve adjustment
            (
                self._current_step_size,
                self._current_sample_period,
            ) = self.FSM.machine.get_step_and_period(self._current_flow_error_pct)

            # update FSM database
            self.FSM.SetFSMData("aspirate_speed_actual", self._flowrate_actual)
            self.FSM.SetFSMData("aspirate_error", self._current_flow_error_pct)
            self.FSM.SetFSMData("current_step_size", self._current_step_size)
            self.FSM.SetFSMData("current_sample_period", self._current_sample_period)

            _aspirate_log_interval = 10 # log every ten seconds
            if not self._aspirate_last_log_time or ((time.time() - self._aspirate_last_log_time) > _aspirate_log_interval):
                self._logger.info(
                    "Total aspirated: {} mL, Flow: {} mL/s, Actual Pressure Loss: {} mbar / sec, Total Calculated Pressure Loss: {} mbar, error_pct: {}%, step_size: {} steps, sample_period: {}s, flowrate avg {} mL/s".format(
                        self._total_volume_aspirated_stop,
                        self._flowrate_actual,
                        self.FSM.machine.pressure_slope,
                        leakfactor,
                        self._current_flow_error_pct,
                        self._current_step_size,
                        self._current_sample_period,
                        self.flowrate_avg,
                    )
                )
                self._aspirate_last_log_time = time.time()


            # remeasure volume
            self._current_time_start = time.time()
            self._pressure_test_start = self.FSM.machine.pressure
            self._total_volume_aspirated_start = math.get_total_volume_aspiration(
                self._total_volume,
                self._pv_const,
                self._pressure_test_start - leakfactor,
            )

            # adjust flow
            # adjust speed
            if self._flowrate_actual > self._aspirate_speed_target:
                # decrease opening
                self._valve_setting = self._valve_setting - self._current_step_size
                if self._valve_setting < 0:
                    self._valve_setting = 0
                self.FSM.machine.set_valve("valve3", self._valve_setting)
                self.warning = None

            if self._flowrate_actual < self._aspirate_speed_target:
                # increase opening
                self._valve_setting = self._valve_setting + self._current_step_size
                if self._valve_setting >= 100:
                    self._valve_setting = 100

                    # Check if current average flowrate is 2 times lower than the target flowrate.
                    # In case if it is, send a warning.
                    if self.eventDuration > 60 and self.flowrate_avg <= self._flowrate_warning_limit:
                        self._logger.warning("Current avg flowrate ({} mL/s) is 2 times lower than the target {} mL/s".format(self.flowrate_avg, self._flowrate_warning_limit))
                        self.warning = "Flow rate is lower than expected, try cleaning the machine and packing it less tight."

                    # Check if actual flowrate is lower than the limit. In case it is, trigger VALVE_1_OR_VALVE_3_BLOCKED error.
                    # This check happens after first 60 seconds of the run time.
                    if self.eventDuration > 60 and self.flowrate_avg <= self._flowrate_fall_limit:
                        self._logger.error("Error, valve is clogged, failure_mode=VALVE_1_OR_VALVE_3_BLOCKED.")
                        self.FSM.ToTransistion("toStateError")
                        self.FSM.fsmData["failure_mode"] = FailureMode.VALVE_1_OR_VALVE_3_BLOCKED
                        self.FSM.fsmData["failure_description"] = (
                            "Valve 1 is clogged and needs to be cleaned. Please reset the machine, pull out your herb "
                            "tube and follow the guideline to clear valves from our knowledgebase on drizzle.life"
                        )
                        return

                self.FSM.machine.set_valve("valve3", self._valve_setting)

            # check if we should store last known good valve setting
            valve_adjust_hysteresis = float(
                self.FSM.machine._config["FSM_EV"]["valve_adjust_hysteresis"]
            )
            if (
                self._flowrate_actual
                < (self._aspirate_speed_target + valve_adjust_hysteresis)
            ) and (
                self._flowrate_actual
                > (self._aspirate_speed_target - valve_adjust_hysteresis)
            ):
                # We are on target here, check if the value should be stored for later use
                if not self._last_known_valve_setting_measured:
                    # store last known valve setting for later use
                    self.FSM.machine._config["FSM_EV"][
                        "valve_last_known_setting"
                    ] = str("{:.02f}".format(self._valve_setting))
                    self._last_known_valve_setting_measured = True
                    self._logger.info(
                        "Stored last known valve setting: {}".format(
                            self.FSM.machine._config["FSM_EV"][
                                "valve_last_known_setting"
                            ]
                        )
                    )

            self._logger.info("Valve setting: {}".format(self._valve_setting))

            # check if evc chamber is filled
            if self._total_volume_aspirated_stop > float(
                self.FSM.machine._config["FSM_EX"]["aspirate_volume"]
            ):
                # save config
                self.FSM.machine.store_config()

                # transit to either flush, distill or ready
                if int(self.FSM.machine._config["FSM_EX"]["number_of_flushes"]) >= 1:
                    self.FSM.ToTransistion("toStateFlush")
                else:
                    if self.FSM.fsmData["run_full_extraction"] == 1:
                        self.FSM.ToTransistion("toStateDistillBulk")
                    else:
                        self.FSM.ToTransistion("toStateReady")

    def Exit(self):
        super(StateAspirate, self).Exit()
        self.FSM.FSMOutputText = "Exiting cannabis solvent aspirate"

        # close all valves
        for valve in self.FSM.machine._myvalves:
            self.FSM.machine.set_valve(valve, 0)

        self._logger.info(self.FSM.FSMOutputText)


# Flush state
class StateFlush(State):
    def __init__(self, FSM):
        super(StateFlush, self).__init__(FSM)

    def Enter(self):
        super(StateFlush, self).Enter()

        self.pressure_achieved = False
        self.valves_opened = False

        self.flush_start_time = time.time()

        # depressurize
        self.FSM.FSMOutputText = "Flushing EXC - Depressurizing"
        self.humanReadableLabel = "Flushing"

        # close all valves
        time.sleep(0.5)
        for valve in self.FSM.machine._myvalves:
            self.FSM.machine.set_valve(valve, 0)

        # Start pump at 100#
        self.FSM.machine.pump_value = 100

    def Execute(self):
        super(StateFlush, self).Execute()

        pressure = self.FSM.machine.pressure

        # wait untill pressure is low enough
        if (
            pressure
            < float(self.FSM.machine._config["FSM_EX"]["maximum_vacuum_pressure"])
            and not self.pressure_achieved
        ):
            self._logger.info("maximum vacuum pressure achieved")
            self.flush_start_time = time.time()
            self.pressure_achieved = True

        if self.pressure_achieved and not self.valves_opened:
            self.FSM.machine.set_valve("valve2", 100)
            self.FSM.machine.set_valve("valve3", 100)
            self.valves_opened = True
            self.flush_start_time = time.time()

        if self.valves_opened:
            # flush for seven seconds flush_time
            if (time.time() - self.flush_start_time) > float(self.FSM.machine._config["FSM_EX"]["flush_time"]):
                # add one to actual number of flushes performed
                self.FSM.fsmData["flushes_performed"] = (
                    int(self.FSM.fsmData["flushes_performed"]) + 1
                )

                # Check if we need more flushes
                if int(self.FSM.fsmData["flushes_performed"]) < int(
                    self.FSM.machine._config["FSM_EX"]["number_of_flushes"]
                ):
                    self._logger.info(
                        "I have performed {} flushes, performing another flush!".format(
                            int(self.FSM.fsmData["flushes_performed"])
                        )
                    )
                    self.FSM.ToTransistion("toStateExtraFlushDepressurize")
                else:
                    self._logger.info("Enought with the flushing allready!")
                    # check for full extraction cycle
                    if self.FSM.fsmData["run_full_extraction"] == 1:
                        self.FSM.ToTransistion("toStateDistillBulk")
                    else:
                        self.FSM.ToTransistion("toStateReady")


def Exit(self):
    super(StateFlush, self).Exit()
    # Turn off pump
    self.FSM.machine.pump_value = 0
    time.sleep(1)

    # close all valves
    self._logger.info("Closing all valves")
    for valve in self.FSM.machine._myvalves:
        self.FSM.machine.set_valve(valve, 0)


# Third depressure state before doing final aspirate
class StateExtraFlushDepressurize(State):
    def __init__(self, FSM):
        super(StateExtraFlushDepressurize, self).__init__(FSM)

    def Enter(self):
        super(StateExtraFlushDepressurize, self).Enter()

        # depressurize
        self.FSM.FSMOutputText = "Depressurising for another flush init"
        self.humanReadableLabel = "Flushing"

        # close all valves except 3
        self.FSM.machine.set_valve("valve1", 0)
        self.FSM.machine.set_valve("valve2", 0)
        self.FSM.machine.set_valve("valve3", 100)
        self.FSM.machine.set_valve("valve4", 0)

        # Start pump at 100#
        self.FSM.machine.pump_value = 100

    def Execute(self):
        super(StateExtraFlushDepressurize, self).Execute()
        pressure = self.FSM.machine.pressure
        self.FSM.FSMOutputText = "System depressuring: {:.02f} mbar".format(pressure)
        self._logger.info(self.FSM.FSMOutputText)

        # Condition to see if we need to change state
        if pressure < float(
            self.FSM.machine._config["FSM_EX"]["maximum_vacuum_pressure"]
        ):
            self.FSM.ToTransistion("toStateTopUpEXC")
            self._logger.info("maximum vacuum pressure achieved")

    def Exit(self):
        super(StateExtraFlushDepressurize, self).Exit()
        # Turn off pump
        self.FSM.machine.pump_value = 0

        # seal off EXC and equalize EVC
        self.FSM.machine.set_valve("valve1", 0)
        self.FSM.machine.set_valve("valve2", 0)
        self.FSM.machine.set_valve("valve3", 0)
        self.FSM.machine.set_valve("valve4", 100)

        # wait for pressure to stabilize
        self.FSM.FSMOutputText = "Exiting depressuring state"
        self._logger.info(self.FSM.FSMOutputText)


# Bulk Distillation
class StateDistillBulk(State):
    def __init__(self, FSM):
        super(StateDistillBulk, self).__init__(FSM)

    def get_progress(self):
        """Calculates progress percentage and ETA in seconds."""
        now = time.time()
        time_delta = time.time() - self._last_time_measure_time
        self._last_time_measure_time = now
        power_uptake = (self.FSM.machine._PID.current_window_power_average or self.FSM.machine.bottom_heater_percent) / 100
        if not self.FSM.fsmData["pause_flag"]:
            self._elapsed_time_seconds += time_delta

        return math.calculate_distill_progress(int(self._elapsed_time_seconds), power_uptake)

    def heater_temperature_increase_check(self):
        temperature = self.FSM.machine.bottom_temperature
        elapsed_time = time.time() - self._temperature_capture_time

        # in case heater is already hot - skip check.
        if temperature >= self._temperature_check_threshold:
            self._temperature_check_required = False
            return

        if elapsed_time > self._temperature_check_interval:
            if temperature - self._temperature <= self._temperature_increase_threshold:
                self._logger.error(
                    "Heater error, current temperature {} did not increase from {} over {} seconds.".format(
                        temperature, self._temperature, elapsed_time,
                    )
                )
                self.FSM.fsmData["failure_mode"] = FailureMode.HEATER_ERROR
                # TODO: @peter - add more details, mention cable issue?
                self.FSM.fsmData["failure_description"] = "The heater stopped heating during the distill process."
                self.FSM.ToTransistion("toStateError")
            else:
                self._temperature_check_required = False

    def check_fan_is_on(self):
        status = self.FSM.machine._fan_control.fan_adc_check
        self._logger.info("Checking fan status... Fan status is {}".format(status))
        if status == module_fancontrol.FAN_ADC_LEVEL_ON:
            self._fan_ok = True
        elif status is not None:
            self.FSM.fsmData["failure_mode"] = FailureMode.FAN_ERROR
            self.FSM.ToTransistion("toStateError")
            self.FSM.fsmData["failure_description"] = (
                "Error, air fan seems to be defective. Please try again and if it still fails, contact "
                "drizzle support."
            )
        else:
            # Fan not supported.
            self._fan_ok = True

    def Enter(self):
        super(StateDistillBulk, self).Enter()
        self.FSM.FSMOutputText = "Init distillation"
        self.humanReadableLabel = "Distilling"

        # set warm light
        self.FSM.machine.light_warm()

        # turn on alcohol sensor
        self.FSM.machine.set_alcohol_sensor_on()
        self._alcohol_sensor_start_time = int(time.time())
        self._alcohol_sensor_level_phase_one_passed = False

        # ambient pressure check
        pressure_lower_bound = float(self.FSM.machine._config["FSM_EV"]["ambient_pressure_lower_bound"])
        pressure_upper_bound = float(self.FSM.machine._config["FSM_EV"]["ambient_pressure_upper_bound"])
        self.FSM.machine.set_valve("valve4", 100)
        time.sleep(3)
        current_pressure = self.FSM.machine.pressure
        self._logger.info(
            "Checking ambient pressure at the start of distill proces. Current pressure is {}".format(current_pressure)
        )
        if (current_pressure > pressure_upper_bound) or (current_pressure < pressure_lower_bound):
            self._logger.info("Error, bad ambient pressure level. Entering error state.")
            self.FSM.fsmData["failure_mode"] = FailureMode.PRESSURE_SENSOR_ERROR
            self.FSM.ToTransistion("toStateError")
            self.FSM.fsmData["failure_description"] = "Error, ambient pressure is either too low or too high. Pressure sensor is defective."

        # close all valves
        self.FSM.machine.set_valve("valve1", 0)
        self.FSM.machine.set_valve("valve2", 0)
        self.FSM.machine.set_valve("valve3", 0)
        self.FSM.machine.set_valve("valve4", 0)

        self.FSM.machine.PID_on()

        # set PID target temp to 90 degrees
        self.FSM.machine.set_PID_target(
            float(self.FSM.machine._config["FSM_EV"]["distillation_temperature"])
        )

        self.FSM.fsmData["pressure_failure_counter"] = 0

        # start fan
        self.FSM.machine.fan_value = 100

        # start pump
        self.FSM.machine.pump_value = 100

        # store start times
        self._start_time = time.time()
        self._elapsed_time_seconds = 0
        self._last_time_measure_time = self._start_time
        self._last_run_time = time.time()
        self._last_heatplate_temperature_regulation = time.time()
        self._distillation_temperature = float(
            self.FSM.machine._config["FSM_EV"]["distillation_temperature"]
        )
        self._fan_check_interval_seconds = 180
        self._fan_last_check_time = None
        self._last_log_time = None
        self._log_interval = 5
        self._temperature_log_interval = 60
        self._temperature_last_log_time = None
        self._temperature_critical_level_start = None
        # temp check
        self._temperature_check_required = True
        self._temperature_check_interval = int(self.FSM.machine._config["FSM_EV"]["temperature_check_interval"])
        self._temperature_increase_threshold = int(self.FSM.machine._config["FSM_EV"]["temperature_increase_threshold"])
        self._temperature_check_threshold = int(self.FSM.machine._config["FSM_EV"]["temperature_check_threshold"])
        self._temperature = self.FSM.machine.bottom_temperature
        self._temperature_capture_time = time.time()
        # fan check flag:
        self._fan_ok = None

        # state of the pressure peaks.
        self._handle_pressure_peak = None
        self._pressure_reached_peak = 0
        self._pressure_peak_handling_start_time = None
        self._pressure_peak_detected_start_time = None
        self._peak_pressure_detection_interval = float(
            self.FSM.machine._config["FSM_EV"]["peak_pressure_detection_interval_seconds"]
        )
        self._peak_pressure_during_distill = float(
            self.FSM.machine._config["FSM_EV"]["peak_pressure_during_distill"]
        )
        self._pressure_peak_handle_time_seconds =  float(
            self.FSM.machine._config["FSM_EV"]["pressure_peak_handle_time_seconds"]
        )
        self._pressure_peak_max_pressure =  float(
            self.FSM.machine._config["FSM_EV"]["pressure_peak_max_pressure"]
        )
        self._pressure_peak_warning_sent = None
        self._new_cycle_started_time = None
        # wait just two seconds to allow the fan to start
        time.sleep(2)

    def Execute(self):
        super(StateDistillBulk, self).Execute()
        self.FSM.FSMOutputText = "Distilling"
        # frequency between each adjustment Fetch this from dictionary
        self.progressPercentage, self.estimatedTimeLeftSeconds = self.get_progress()

        if self.FSM.fsmData["pause_flag"]:
            # pause things for a bit
            self.FSM.machine.set_PID_target(0)
            self.FSM.machine.bottom_heater_percent = 0
            self.FSM.machine.pump_value = 0
            self._last_run_time = time.time()
            self._last_heatplate_temperature_regulation = time.time()
            self._pause_time_start = time.time()
            self.onPause = True
        else:
            self.FSM.machine.set_PID_target(self._distillation_temperature)
            self.FSM.machine.pump_value = 100
            self._pause_start_time = None
            self.onPause = False

        if self._fan_ok is None:
            self.check_fan_is_on()

        # A check of the heater needs to look for an increase in temperature over configured time interval.
        # This process runs only once after distill process starts and device is not paused.
        if self._temperature_check_required and not self.FSM.fsmData["pause_flag"]:
            self.heater_temperature_increase_check()

        # Log bottom heater temperature, bottom heater power (pct), gas sensor temperature, pressure
        # every 10 seconds when device is runnning and every minute when device is paused.
        _temperature_log_interval = self._temperature_log_interval if not self.FSM.fsmData["pause_flag"] else self._temperature_log_interval * 6
        if not self._temperature_last_log_time or ((time.time() - self._temperature_last_log_time) > _temperature_log_interval):
            self._logger.info(
                "Heater temperature: {:.02f}, Heater power: {:.02f}%, gas sensor temperature: {:.02f}, pressure: {:.02f}, PID target {}".format(
                    self.FSM.machine.bottom_temperature, self.FSM.machine.bottom_heater_percent, self.FSM.machine.gas_temperature,
                    self.FSM.machine.pressure, self.FSM.machine._PID.setpoint
                )
            )
            self._temperature_last_log_time = time.time()

        if self._handle_pressure_peak:
            # when pressure raises above 300mbar again - we turn off the heater for 10 mins and turn it on
            # after 10 mins and reduce output by 10% and send warning to device.
            # when pressure raises above 300mbar again - go into error state
            if self._pressure_peak_handling_start_time is None:
                self._pressure_peak_handling_start_time = time.time()

            # send warning
            if self._pressure_peak_warning_sent is None:
                self._pressure_peak_warning_sent = True
                self.warning = "Pressure peak detected."

            # check if we already reached pressure peak 2 times before.
            if self._pressure_reached_peak > 2:
                self.FSM.machine.pump_value = 0
                self.FSM.machine.set_PID_target(0)
                self.FSM.machine.bottom_heater_percent = 0
                self.FSM.machine.set_valve("valve4", 100)
                self._logger.error(
                        "Error, pressure has rached critical level {:.02f} during distillation multiple times.".format(
                            self.FSM.machine.pressure
                        )
                    )
                self.FSM.fsmData["failure_mode"] = FailureMode.PUMP_NEEDS_CLEAN_OR_REPLACEMENT
                self.FSM.fsmData["failure_description"] = "Pump could not pull enough vacuum. Please refer to website for guide to cleaning your pump. If you have allready done so, the pump may be defective. Please contact support@drizzle.life"
                self.FSM.ToTransistion("toStateError")
                return

            handling_time = time.time() - self._pressure_peak_handling_start_time
            if handling_time < self._pressure_peak_handle_time_seconds:
                # turn off the heater for 10 mins
                self.FSM.machine.set_PID_target(0)
                self.FSM.machine.bottom_heater_percent = 0
                self.FSM.machine.pump_value = 100
                # venting at the start the cooldown sequence
                if (handling_time < 5):
                    self.FSM.machine.set_valve("valve4", 100)
                    time.sleep(5)
                    self.FSM.machine.set_valve("valve4", 0)
                # don't do anything else until pressure_peak_handle_time_seconds interval passes.
                return
            else:
                # venting at the end of the cooldown sequence
                self.FSM.machine.set_valve("valve4", 100)
                time.sleep(5)
                self.FSM.machine.set_valve("valve4", 0)
                # after waiting for 10 mins with heater off, reduce output by 10% and continue distill.
                new_output_limit = self.FSM.machine.MAX_PID_POWER_OUTPUT - 10 * self._pressure_reached_peak
                self.FSM.machine.reload_PID(pid_max_output_limit=new_output_limit)
                self.FSM.machine.set_PID_target(self._distillation_temperature)
                self.FSM.machine.pump_value = 100
                # reset peak handling state
                self._handle_pressure_peak = None
                self._pressure_peak_handling_start_time = None
                self._pressure_peak_warning_sent = None
                self.warning = None
                self._new_cycle_started_time = time.time()

        # check for pressure drop absolute limits
        if (time.time() - self._last_run_time) > float(
            self.FSM.machine._config["FSM_EV"]["time_delay_before_pressure_check"]
        ):
            pressure = self.FSM.machine.pressure

            # If the pressure rises above 300 mbar for 1 minute during distillation, we need to shut down the heating
            # element for 10 minutes, reduce the max output by 10% and then start again.
            elapsed_time = time.time() - self._start_time
            # when cooldown period to handle pressure peak is finished, we need to wait 30 seconds before starting measuring pressure again.
            if self._new_cycle_started_time and (time.time() - self._new_cycle_started_time) > 30:
                self._new_cycle_started_time = None
            new_cycle = (time.time() - self._new_cycle_started_time) < 30 if self._new_cycle_started_time else False

            if elapsed_time < 120 or new_cycle:
                # wait before starting to check pressure.
                pass
            elif 120 < elapsed_time < 600:
                if pressure > self._pressure_peak_max_pressure:
                    if self._pressure_peak_detected_start_time is None:
                        self._pressure_peak_detected_start_time = time.time()

                    if ((time.time() - self._pressure_peak_detected_start_time) > self._peak_pressure_detection_interval):
                        self.FSM.machine.pump_value = 0
                        self.FSM.machine.set_PID_target(0)
                        self.FSM.machine.bottom_heater_percent = 0
                        self.FSM.machine.set_valve("valve4", 100)
                        self._logger.error("Error, pressure reached {:.02f} during distillation".format(pressure))
                        self.FSM.fsmData["failure_mode"] = FailureMode.PUMP_NEEDS_CLEAN_OR_REPLACEMENT
                        self.FSM.ToTransistion("toStateError")
                        self.FSM.fsmData["failure_description"] = "Error, pressure is way to high during distillation. Pump could not pull enough vacuum. Please refer to website for guide to cleaning your pump. If you have allready done so, the pump may be defective. Please contact support@drizzle.life"
                        return
                else:
                    self._pressure_peak_detected_start_time = None

            else:
                if pressure > self._peak_pressure_during_distill:
                    if self._pressure_peak_detected_start_time is None:
                        self._pressure_peak_detected_start_time = time.time()

                    if not self._handle_pressure_peak and ((time.time() - self._pressure_peak_detected_start_time) > self._peak_pressure_detection_interval):
                        self._logger.warning("Warning - pressure peak {:.02f} detected.".format(pressure))
                        self._handle_pressure_peak = True
                        self._pressure_reached_peak += 1

                else:
                    self._pressure_peak_detected_start_time = None

            # this checks that we are below the absolute maximum permissible target for pressure during distillation
            if not new_cycle and pressure > float(self.FSM.machine._config["FSM_EV"]["error_pressure_during_distill"]):
                self.FSM.fsmData["pressure_failure_counter"] += 1
                self._logger.warning(
                    "Warning - critical distillation pressure, waiting a little before triggering error"
                )

                # it needs to fail twenty sequential times
                if self.FSM.fsmData["pressure_failure_counter"] > 20:
                    # If pressure rises above the defined limit during a distillation, before throwing an error, we
                    # need to do the following to determine the nature of the error:
                    # Shut of pump, turn of heater, wait 3 seconds for pressure to stabilize, measure pressure, wait
                    # three seconds, measure pressure again. Then go into error mode.
                    # If the pressure rises more than 4 mbar/second (defined in config file) in the three second
                    # measurement window, we have a leak (error 1, leak in distillation chamber), otherwise it is a
                    # faulty pump (error 6).
                    self.FSM.machine.pump_value = 0
                    self.FSM.machine.set_PID_target(0)
                    self.FSM.machine.bottom_heater_percent = 0
                    time.sleep(3)
                    time_before = time.time()
                    pressure_before = self.FSM.machine.pressure
                    time.sleep(3)
                    pressure_after = self.FSM.machine.pressure
                    time_after = time.time()
                    pressure_increase_threshold = float(self.FSM.machine._config["FSM_EV"]["error_pressure_increase_threshold"])
                    pressure_increase = (pressure_after - pressure_before) / (time_after - time_before)
                    if pressure_increase > pressure_increase_threshold:
                        self._logger.error("Error, pressure reached {} during distillation".format(pressure))
                        self.FSM.fsmData["failure_mode"] = FailureMode.EVC_LEAK
                        self.FSM.ToTransistion("toStateError")
                        self.FSM.fsmData["failure_description"] = "Error, pressure is way to high during distillation. Detected leak in distillation chamber."
                    else:
                        self._logger.error("Error, pressure reached {} during distillation".format(pressure))
                        self.FSM.fsmData["failure_mode"] = FailureMode.PUMP_NEEDS_CLEAN_OR_REPLACEMENT
                        self.FSM.ToTransistion("toStateError")
                        self.FSM.fsmData["failure_description"] = "Error, pressure is way to high during distillation. Pump could not pull enough vacuum. Please refer to website for guide to cleaning your pump. If you have allready done so, the pump may be defective. Please contact support@drizzle.life"
            else:
                self.FSM.fsmData["pressure_failure_counter"] = 0

        # Temperature check. If temperature stays more than ["FSM_EV"]["temperature_critical_level"] during more than
        # ["FSM_EV"]["temperature_check_interval"] seconds, device will enter error state
        if (time.time() - self._last_run_time) > float(
            self.FSM.machine._config["FSM_EV"]["temperature_check_interval"]
        ):
            temperature = self.FSM.machine.bottom_temperature
            if temperature >= int(self.FSM.machine._config["FSM_EV"]["temperature_critical_level"]):
                if self._temperature_critical_level_start is None:
                    self._temperature_critical_level_start = time.time()
                temperature_critical_level_time = time.time() - self._temperature_critical_level_start
                self._logger.info(
                    "Warning, temperature reached {} during distillation".format(
                        temperature
                    )
                )
                if temperature_critical_level_time >= int(self.FSM.machine._config["FSM_EV"]["temperature_critical_level_max_interval"]):
                    self._logger.error(
                        "Error, temperature reached critical level {} during distillation".format(
                            temperature
                        )
                    )
                    self.FSM.fsmData["failure_mode"] = FailureMode.THERMAL_RUNAWAY
                    self.FSM.fsmData["failure_description"] = "The temperature of the heater plate exceeded the maximum level. Please reset and try again. If the error occurs again, please contact support@drizzle.life"
                    self.FSM.ToTransistion("toStateError")
                    return
            else:
                self._temperature_critical_level_start = None

        # Run process only if device is not paused.
        if not self.FSM.fsmData["pause_flag"]:
            # if adjustment period has run, start next cycle
            if self.FSM.machine.update_PID(log=False):
                current_power_average = self.FSM.machine._PID.current_window_power_average
                cutoff_limit = float(
                    self.FSM.machine._config["PID"]["wattage_decrease_limit"]
                )
                output = self.FSM.machine.bottom_heater_percent
                current_temp = self.FSM.machine.bottom_temperature
                target = self.FSM.machine._PID.setpoint

                # Do a check for complete distillation
                if self.FSM.fsmData['force_afterstill'] == True:
                    self.FSM.fsmData['force_afterstill'] = False
                    self._logger.info("User forced me to afterstill")
                    self.FSM.ToTransistion("toStateAfterDistill")
                if current_power_average:
                    if current_power_average < cutoff_limit:
                        self._logger.info("Bulk distillation done")
                        self.FSM.ToTransistion("toStateAfterDistill")

    def Exit(self):
        super(StateDistillBulk, self).Exit()
        self.estimatedTimeLeftSeconds = 0
        self.progressPercentage = 100
        # set output power to zero
        self.FSM.machine.set_PID_target(0)
        self.FSM.machine.bottom_heater_percent = 0
        self.FSM.FSMOutputText = "Exiting distillation"
        self._logger.info(self.FSM.FSMOutputText)


# After heat to get rid of most of the remaining solvent
class StateAfterDistill(State):
    def __init__(self, FSM):
        super(StateAfterDistill, self).__init__(FSM)

    def Enter(self):
        super(StateAfterDistill, self).Enter()
        self.FSM.FSMOutputText = "After distillation"
        self.humanReadableLabel = "Removing trace solvent"

        # set PID target temp to target
        self.FSM.machine.set_PID_target(
            float(self.FSM.machine._config["FSM_EV"]["after_heat_temp"])
        )

        # start fan
        self.FSM.machine.fan_value = 100

        # start pump
        self.FSM.machine.pump_value = 100

        self._last_run_time = time.time()

    def Execute(self):
        super(StateAfterDistill, self).Execute()
        self.FSM.FSMOutputText = "Distilling"
        # frequency between each adjustment Fetch this from dictionary

        self.FSM.machine.set_PID_target(
            float(self.FSM.machine._config["FSM_EV"]["after_heat_temp"])
        )

        # if adjustment period has run, start next cycle
        self.FSM.machine.update_PID()

        # check for initieal heater completion
        if (
            self._last_run_time
            + float(self.FSM.machine._config["FSM_EV"]["after_heat_time"])
            < time.time()
        ):
            self._logger.info("After heat done")
            self.FSM.ToTransistion("toStateFinalSolventRemoval")

    def Exit(self):
        super(StateAfterDistill, self).Exit()
        self.FSM.FSMOutputText = "Exiting distillation"
        self._logger.info(self.FSM.FSMOutputText)


# Final air flushes to remove any refluxing solvent
class StateFinalSolventRemoval(State):
    def __init__(self, FSM):
        super(StateFinalSolventRemoval, self).__init__(FSM)

    def Enter(self):
        super(StateFinalSolventRemoval, self).Enter()
        self.FSM.FSMOutputText = "Removing refluxing solvent"
        self.humanReadableLabel = "Removing trace solvent"

        # set PID target temp to target
        self.FSM.machine.set_PID_target(
            float(self.FSM.machine._config["FSM_EV"]["after_heat_temp"])
        )

        # start fan
        self.FSM.machine.fan_value = 100

        # start pump
        self.FSM.machine.pump_value = 100

        self._last_run_time = time.time()

        self._airing_counter = 0
        self._final_air_cycles = int(
            self.FSM.machine._config["FSM_EV"]["final_air_cycles"]
        )

    def Execute(self):
        super(StateFinalSolventRemoval, self).Execute()
        self.FSM.FSMOutputText = "Removing traces of solvent"
        # frequency between each adjustment Fetch this from dictionary

        self.FSM.machine.set_PID_target(
            float(self.FSM.machine._config["FSM_EV"]["after_heat_temp"])
        )

        # if adjustment period has run, start next cycle
        self.FSM.machine.update_PID(log=False)

        # open and close valves for the defined number of times
        if self._airing_counter % 2 == 0:
            #check for next open state
            if (
                self._last_run_time
                + float(self.FSM.machine._config["FSM_EV"]["final_air_cycles_time_closed"])
                < time.time()
            ):
                self.FSM.machine.set_valve("valve4", 100)
                # store last runtime
                self._last_run_time = time.time()
                # increase cycle counter
                self._airing_counter += 1
        else:
            #check for next close state
            if (
                self._last_run_time
                + float(self.FSM.machine._config["FSM_EV"]["final_air_cycles_time_open"])
                < time.time()
            ):
                self.FSM.machine.set_valve("valve4", 0)
                # store last runtime
                self._last_run_time = time.time()
                # increase cycle counter
                self._airing_counter += 1

        if self._airing_counter >= (
            int(self.FSM.machine._config["FSM_EV"]["final_air_cycles"]) * 2
        ):
            # we check for double the amount of cycles because an cycle is open AND close
            self.FSM.ToTransistion("toStateReady")

    def Exit(self):
        super(StateFinalSolventRemoval, self).Exit()
        self.FSM.machine.PID_off()
        self.FSM.machine.set_PID_target(0)
        self.FSM.machine.bottom_heater_percent = 0
        self.FSM.machine.fan_value = 0
        self.FSM.machine.pump_value = 0

        # vent machine
        self.FSM.machine.set_valves_in_relax_position()

        self.FSM.FSMOutputText = "Exiting final solvent removal"
        self._logger.info(self.FSM.FSMOutputText)


# Flushes the pump with air a few times
class StateVentPump(State):
    def __init__(self, FSM):
        super(StateVentPump, self).__init__(FSM)

    def Enter(self):
        super(StateVentPump, self).Enter()
        self.FSM.FSMOutputText = "Venting Pump"
        self.humanReadableLabel = "Venting pump"

        # start pump
        self.FSM.machine.pump_value = 100

        # turn on alcohol sensor
        self.FSM.machine.set_alcohol_sensor_on()

        # close all valves
        self.FSM.machine.set_valve("valve1", 0)
        self.FSM.machine.set_valve("valve2", 0)
        self.FSM.machine.set_valve("valve3", 0)
        self.FSM.machine.set_valve("valve4", 0)

        self._last_run_time = time.time()

        self._current_vent_state = "depressurize"
        self._depressure_time = 20
        self._vent_time = 5
        self._airing_counter = 0
        self._number_of_cycles_to_vent = 3

    def Execute(self):
        super(StateVentPump, self).Execute()

        self.FSM.FSMOutputText = "Venting pump"
        # frequency between each adjustment Fetch this from dictionary

        if self._current_vent_state == "depressurize":
            # reduce pressure
            if self._last_run_time + self._depressure_time < time.time():
                # switch to vent state
                self._current_vent_state = "venting"
                # open valve4
                self.FSM.machine.set_valve("valve4", 100)
                # reset state
                self._last_run_time = time.time()
        elif self._current_vent_state == "venting":
            # vent untill done
            if self._last_run_time + self._vent_time < time.time():
                # switch to vent state
                self._current_vent_state = "depressurize"
                # open valve4
                self.FSM.machine.set_valve("valve4", 0)
                # reset state
                self._last_run_time = time.time()

                # increase cycle counter
                self._airing_counter += 1

        if self._airing_counter >= self._number_of_cycles_to_vent:
            # if this flad is set, it means the pump vent was started during a run
            if self.FSM.startExtractAfterVent:
                self.FSM.startExtractAfterVent = False
                self.FSM.ToTransistion("toStateSystemCheck")
            else:
                self.FSM.ToTransistion("toStateReady")

    def Exit(self):
        super(StateVentPump, self).Exit()
        self.FSM.machine.PID_off()
        self.FSM.machine.set_PID_target(0)
        self.FSM.machine.bottom_heater_percent = 0
        self.FSM.machine.fan_value = 0
        self.FSM.machine.pump_value = 0

        # vent machine
        self.FSM.machine.set_valve("valve4", 100)

        self.FSM.FSMOutputText = "Exiting vent pump function"
        self._logger.info(self.FSM.FSMOutputText)


# Decarboxylation state
class StateDecarb(State):
    def __init__(self, FSM):
        super(StateDecarb, self).__init__(FSM)

    def Enter(self):
        super(StateDecarb, self).Enter()
        self.FSM.FSMOutputText = "Init decarboxylation"
        self.humanReadableLabel = "Decarboxylating"

        # open all valves
        self.FSM.machine.set_valves_in_relax_position()

        self.FSM.machine.PID_on()

        # set PID target temp to decarb temp degrees
        self.FSM.machine.set_PID_target(
            float(self.FSM.machine._config["DECARB"]["temperature"])
        )

        # turn on warm light
        self.FSM.machine.light_warm()

        self._decarb_start_time = time.time()

    def Execute(self):
        super(StateDecarb, self).Execute()
        self.FSM.FSMOutputText = "Decarboxylating"

        self.FSM.machine.set_PID_target(
            float(self.FSM.machine._config["DECARB"]["temperature"])
        )

        self.FSM.machine.update_PID()

        if (
            self._decarb_start_time
            + (60 * float(self.FSM.machine._config["DECARB"]["time_minutes"]))
            < time.time()
        ):
            self._logger.info("Decarboxylation done")
            # we check for double the amount of cycles because an cycle is open AND close
            self.FSM.ToTransistion("toStateReady")

    def Exit(self):
        super(StateDecarb, self).Exit()
        self.FSM.machine.light_off()
        self.FSM.machine.PID_off()
        self.FSM.machine.set_PID_target(0)
        self.FSM.machine.bottom_heater_percent = 0


# Cooldown state
class StateCooldown(State):
    def __init__(self, FSM):
        super(StateCooldown, self).__init__(FSM)

    def Enter(self):
        super(StateCooldown, self).Enter()

    def Execute(self):
        super(StateCooldown, self).Execute()

    def Exit(self):
        super(StateCooldown, self).Exit()


# MixOil state
class StateMixOil(State):
    def __init__(self, FSM):
        super(StateMixOil, self).__init__(FSM)

    def Enter(self):
        super(StateMixOil, self).Enter()
        self.FSM.FSMOutputText = "Init oil mixing"
        self.humanReadableLabel = "Mixing oil"

        # open all valves
        self.FSM.machine.set_valves_in_relax_position()

        self.FSM.machine.PID_on()

        # turn on warm light
        self.FSM.machine.light_warm()

        # set PID target temp to decarb temp degrees
        self.FSM.machine.set_PID_target(
            float(self.FSM.machine._config["OIL_MIX"]["temperature"])
        )

        self._oilmix_start_time = time.time()

    def Execute(self):
        super(StateMixOil, self).Execute()
        self.FSM.FSMOutputText = "Oil mixing"

        self.FSM.machine.set_PID_target(
            float(self.FSM.machine._config["OIL_MIX"]["temperature"])
        )

        self.FSM.machine.update_PID()

        if (
            self._oilmix_start_time
            + (60 * float(self.FSM.machine._config["OIL_MIX"]["time_minutes"]))
            < time.time()
        ):
            self._logger.info("Oil mixing done")
            # we check for double the amount of cycles because an cycle is open AND close
            self.FSM.ToTransistion("toStateReady")

    def Exit(self):
        super(StateMixOil, self).Exit()
        self.FSM.machine.light_off()
        self.FSM.machine.PID_off()
        self.FSM.machine.set_PID_target(0)
        self.FSM.machine.bottom_heater_percent = 0


class StateCleanPump(State):
    def __init__(self, FSM):
        super(StateCleanPump, self).__init__(FSM)

    def Enter(self):
        super(StateCleanPump, self).Enter()
        self.FSM.FSMOutputText = "Init clean pump"
        self.humanReadableLabel = "Cleaning pump"

        # set warm light
        self.FSM.machine.light_warm()

        # turn on alcohol sensor
        self.FSM.machine.set_alcohol_sensor_on()
        self._alcohol_sensor_start_time = int(time.time())
        self._alcohol_sensor_level_phase_one_passed = False

        # close all valves
        self.FSM.machine.set_valve("valve1", 0)
        self.FSM.machine.set_valve("valve2", 0)
        self.FSM.machine.set_valve("valve3", 0)
        self.FSM.machine.set_valve("valve4", 0)

        self.FSM.machine.PID_on()

        # set PID target temp to 90 degrees
        self.FSM.machine.set_PID_target(
            float(self.FSM.machine._config["FSM_EV"]["distillation_temperature"])
        )

        # start fan
        self.FSM.machine.fan_value = 100

        # start pump
        self.FSM.machine.pump_value = 100

        self._distillation_temperature = float(
            self.FSM.machine._config["FSM_EV"]["distillation_temperature"]
        )
        self._last_log_time = None
        self._log_interval = 5

        self._start_time = time.time()
        self._elapsed_time_seconds = 0
        self._last_time_measure_time = self._start_time
        self._last_run_time = time.time()
        self._last_heatplate_temperature_regulation = time.time()

    def Execute(self):
        super(StateCleanPump, self).Execute()
        self.FSM.FSMOutputText = "Cleaning pump"

        if self.FSM.fsmData["pause_flag"]:
            self.FSM.machine.set_PID_target(0)
            self.FSM.machine.pump_value = 0
            self._last_run_time = time.time()
            self._last_heatplate_temperature_regulation = time.time()
            self._pause_time_start = time.time()
        else:
            self.FSM.machine.set_PID_target(self._distillation_temperature)
            self.FSM.machine.pump_value = 100
            self._pause_start_time = None

        if not self.FSM.fsmData["pause_flag"]:
            # if adjustment period has run, start next cycle
            if self.FSM.machine.update_PID(log=False):
                current_power_average = self.FSM.machine._PID.current_window_power_average
                cutoff_limit = float(
                    self.FSM.machine._config["PID"]["wattage_decrease_limit"]
                )
                output = self.FSM.machine.bottom_heater_percent
                current_temp = self.FSM.machine.bottom_temperature
                target = self.FSM.machine._PID.setpoint

                # Do a check for complete distillation
                if current_power_average:
                    if current_power_average < cutoff_limit:
                        self._logger.info("Pump clean done")
                        self.FSM.ToTransistion("toStateReady")
    def Exit(self):
        super(StateCleanPump, self).Exit()
        # set output power to zero
        self.FSM.machine.set_PID_target(0)
        self.FSM.machine.bottom_heater_percent = 0
        self.FSM.machine.set_valves_in_relax_position()
        self.FSM.FSMOutputText = "Exiting clean pump"
        self._logger.info(self.FSM.FSMOutputText)

##=====================================================
# FINITE STATE MACHINE
class FSM(object):
    def __init__(self, machine):
        self._logger = get_app_logger(str(self.__class__))
        self._logger.info("Initializing FSM...")
        self.machine = machine  # Reference to the machine object½
        self.states = {}  # A dictionary of available state objects
        self.stateHandles = {}  # A dictionary of human readable state names
        self.transitions = {}  # A dictionary of transition objects
        self.fsmData = {}  # A dictionary of available sensor data
        self.FSMOutputText = None  # Text message to the user
        self.curState = None  # The current state object
        self.prevState = None  # refence to last state, not implemented
        self.trans = None  # Current transition
        self.curHandle = None  # The current handle of the object
        self.startExtractAfterVent = False
        self.hasPumpErrorBeenChecked = False
        self.numberOfVentingRetries = 0
        self.intitialAlcoholCheckDone = None

        # do actual FSM initialize
        self.init_FSM()

    def init_FSM(self):
        # runtime parameters
        self.SetFSMData("start_flag", False)
        self.SetFSMData("pause_flag", False)
        self.SetFSMData("force_afterstill", False)
        self.SetFSMData("running_flag", False)
        self.SetFSMData("exc_volume", 0)
        self.SetFSMData("aspirate_volume_target", 0)
        self.SetFSMData("aspirate_volume_actual", 0)
        self.SetFSMData("aspirate_speed_target", 0)
        self.SetFSMData("aspirate_speed_actual", 0)
        self.SetFSMData("total_volume", 0)
        self.SetFSMData("system_leak", 0)
        self.SetFSMData("target_temp", 0)
        self.SetFSMData("exc_volume_liquid", 0)
        self.SetFSMData("atm_pressure", 0)
        self.SetFSMData("auto_flush", 0)
        self.SetFSMData("aspirate_error", 0)
        self.SetFSMData("current_step_size", 0)
        self.SetFSMData("current_sample_period", 0)
        self.SetFSMData("total_aspirate_duration", 0)
        self.SetFSMData("average_aspirate_speed", 0)
        self.SetFSMData("required_aspirate_speed", 0)
        self.SetFSMData("mbar_change", 0)
        self.SetFSMData("run_full_extraction", 0)
        self.SetFSMData("flushes_performed", 0)
        self.SetFSMData("failure_mode", FailureMode.NONE)
        self.SetFSMData("failure_description", "")


        # FSM STATES
        self.AddState(
            stateName="stateError", stateHandle="Error", state=StateError(self)
        )
        self.AddState(
            stateName="stateReady", stateHandle="Ready", state=StateReady(self)
        )
        self.AddState(
            stateName="stateSystemCheck",
            stateHandle="Checking System",
            state=StateSystemCheck(self),
        )
        self.AddState(
            stateName="statePreFillTubes",
            stateHandle="Prefilling tubes",
            state=StatePreFillTubes(self),
        )
        self.AddState(
            stateName="stateMeasureEXCVolume",
            stateHandle="Measure exc volume",
            state=StateMeasureEXCVolume(self),
        )

        self.AddState(
            stateName="stateTopUpEXC",
            stateHandle="Topping up",
            state=StateTopUpEXC(self),
        )
        self.AddState(
            stateName="stateThirdDepressurize",
            stateHandle="Depressurising",
            state=StateThirdDepressurize(self),
        )
        self.AddState(
            stateName="stateAspirate", stateHandle="Aspirate", state=StateAspirate(self)
        )
        self.AddState(
            stateName="stateFlush", stateHandle="Flushing", state=StateFlush(self)
        )
        self.AddState(
            stateName="stateExtraFlushDepressurize",
            stateHandle="Depressuring for flush",
            state=StateExtraFlushDepressurize(self),
        )
        self.AddState(
            stateName="stateDistillBulk",
            stateHandle="DistillBulk",
            state=StateDistillBulk(self),
        )
        self.AddState(
            stateName="stateAfterDistill",
            stateHandle="AfterDistill",
            state=StateAfterDistill(self),
        )
        self.AddState(
            stateName="stateFinalSolventRemoval",
            stateHandle="FinalSolventRemoval",
            state=StateFinalSolventRemoval(self),
        )
        self.AddState(
            stateName="stateDecarb",
            stateHandle="Decarboxylating",
            state=StateDecarb(self),
        )
        self.AddState(
            stateName="stateCooldown",
            stateHandle="Cooling down",
            state=StateCooldown(self),
        )
        self.AddState(
            stateName="stateMixOil", stateHandle="Mixing Oil", state=StateMixOil(self)
        )
        self.AddState(
            stateName="stateFirstDepressurize",
            stateHandle="Depressurize",
            state=StateFirstDepressurize(self),
        )
        self.AddState(
            stateName="stateSecondDepressurize",
            stateHandle="Depressurize",
            state=StateSecondDepressurize(self),
        )
        self.AddState(
            stateName="stateSecondLeakCheck",
            stateHandle="Check leaks",
            state=StateSecondLeakCheck(self),
        )
        self.AddState(
            stateName="stateVentPump",
            stateHandle="Venting Pump",
            state=StateVentPump(self),
        )
        self.AddState(
            stateName="stateCleanPump",
            stateHandle="CleanPump",
            state=StateCleanPump(self),
        )
        self.AddState(
            stateName="stateSoak",
            stateHandle="Soak",
            state=StateSoak(self),
        )

        # FSM Transitions
        self.AddTransition("toStateError", Transition("stateError"))
        self.AddTransition("toStateReady", Transition("stateReady"))
        self.AddTransition("toStateSystemCheck", Transition("stateSystemCheck"))
        self.AddTransition("toStatePreFillTubes", Transition("statePreFillTubes"))
        self.AddTransition(
            "toStateMeasureEXCVolume", Transition("stateMeasureEXCVolume")
        )
        self.AddTransition("toStateTopUpEXC", Transition("stateTopUpEXC"))
        self.AddTransition(
            "toStateThirdDepressurize", Transition("stateThirdDepressurize")
        )
        self.AddTransition("toStateAspirate", Transition("stateAspirate"))
        self.AddTransition("toStateFlush", Transition("stateFlush"))
        self.AddTransition("toStateDistillBulk", Transition("stateDistillBulk"))
        self.AddTransition("toStateAfterDistill", Transition("stateAfterDistill"))
        self.AddTransition(
            "toStateFinalSolventRemoval", Transition("stateFinalSolventRemoval")
        )
        self.AddTransition("toStateDecarb", Transition("stateDecarb"))
        self.AddTransition("toStateCooldown", Transition("stateCooldown"))
        self.AddTransition("toStateMixOil", Transition("stateMixOil"))
        self.AddTransition(
            "toStateFirstDepressurize", Transition("stateFirstDepressurize")
        )
        self.AddTransition(
            "toStateSecondDepressurize", Transition("stateSecondDepressurize")
        )
        self.AddTransition("toStateSecondLeakCheck", Transition("stateSecondLeakCheck"))
        self.AddTransition(
            "toStateExtraFlushDepressurize", Transition("stateExtraFlushDepressurize")
        )
        self.AddTransition("toStateVentPump", Transition("stateVentPump"))
        self.AddTransition("toStateCleanPump", Transition("stateCleanPump"))
        self.AddTransition("toStateSoak", Transition("stateSoak"))

        self.machine.pump_value = 0
        self.machine.bottom_heater_percent = 0
        self.machine.set_alcohol_sensor_off()
        self.intitialAlcoholCheckDone = None

        # Set FSM start state
        self.SetState("stateReady")

    def AddTransition(self, transName, transition):
        self.transitions[transName] = transition

    def AddState(self, stateName, stateHandle, state):
        self.states[stateName] = state
        self.stateHandles[stateName] = stateHandle

    def SetState(self, stateName):
        self.prevState = self.curState
        self.curState = self.states[stateName]
        self.curHandle = self.stateHandles[stateName]

    def ToTransistion(self, toTrans):
        self.trans = self.transitions[toTrans]

    def SetFSMData(self, sensor, value):
        self.fsmData[sensor] = value

    def Execute(self):
        if self.trans:
            self.curState.Exit()
            self.trans.Execute()
            self.SetState(self.trans.toState)
            self.curState.Enter()
            self.trans = None

        self.curState.Execute()
