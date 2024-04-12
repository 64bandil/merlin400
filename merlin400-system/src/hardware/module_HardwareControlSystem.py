"""
Control class for drizzle extractor
Instantized from ControlThread
"""
if __name__ == "__main__":
  #Add root folder (/src/) to paths for import if this script is run standalone
  from pathlib import Path
  import sys
  sys.path.append(str(Path(__file__).resolve().parent.parent))

import atexit
import configparser
import datetime
import os
import time
from pathlib import Path
from timeit import default_timer as timer

import smbus

# import all required hardware modules here
import hardware.module_math as math
import hardware.module_FSM as module_FSM
import hardware.components.module_steppervalvecontrol as module_steppervalvecontrol  # Module to control valves. Also required the pca9685 driver
import hardware.components.module_thermistorinput as module_thermistorinput  # Module to read thermistors over the ADC
import hardware.components.PID as PID  # PID control module
from hardware.components.module_bottomheatercontrol import module_bottomheatercontrol
from hardware.components.module_fancontrol import module_fancontrol
from hardware.module_FSM import Machine
from hardware.module_FSM import FailureMode
from hardware.components.module_ADC_driver import detect_adc
import hardware.components.module_ADC_driver as module_ADC_driver

from hardware.components.module_LED_RGBW_control import (
    module_LED_RGBW_control,
)  # RGBW LED controller module
from hardware.components.module_physicalinterface import (
    module_physicalinterface,
)  # Module to control the users physical interface
from hardware.components.module_pressuresensor_bmp280 import (
    module_pressuresensor_bmp280,
)  # Module to read BMP280 pressure sensor
from hardware.components.module_pressuresensor_bmp384 import (
    module_pressuresensor_bmp384,
)  # Module to read BMP384 pressure sensor
from hardware.components.module_pumpcontrol import module_pumpcontrol  # Module to control diaphragm pump
from common.module_logging import get_app_logger
from common.settings import LOGS_DIRECTORY, ALCOHOL_SENSOR_ENABLED

if ALCOHOL_SENSOR_ENABLED:
    # module to control alcohol sensor
    from hardware.components.module_alcoholsensor import module_alcoholsensor


"""
Container for the physical module drivers
"""

INIT_STATUS_OK = 0
INIT_STATUS_PRESSURE_SENSOR_ERROR = 1
INIT_STATUS_VALVE_CONTROLLER_ERROR = 2
INIT_STATUS_I2C_BUS_ERROR = 3
INIT_STATUS_ADC_CHIP_ERROR = 4
INIT_STATUS_THERMISTOR_ERROR = 5
INIT_STATUS_USER_PANEL_ERROR = 6
INIT_STATUS_ALCOHOL_SENSOR_ERROR = 7


class HardwareFailure(Exception):
    """Raise when hardware failure encointered."""

class ElectricalError(HardwareFailure):
    """Raise when there is a problem with i2c or other electrical error."""

class UserPanelError(HardwareFailure):
    """Raise when there is a problem with user panel."""

class PressureSensorFailure(HardwareFailure):
    """Raise when problem with pressure sensor detected."""


class module_HardwareControlSystem(Machine):

    ###---===PHYSICAL HARDWARE CONSTANTS===---###
    # i2C ADDRESSES
    I2C_ADDRESS_PRESSURE_SENSOR = 0x76
    I2C_ADDRESS_ADC_SENSOR = 0x48

    # HARDWARE DATA
    NUMBER_OF_VALVES = 4
    NUMBER_OF_RELAYS = 2
    NUMBER_OF_THERMISTORS = 2
    BOTTOM_HEATER_WATTAGE = 60

    # PIN NUMBERING
    BOTTOM_HEATER_PIN = 12
    PUMP_PIN = 16

    ENTRIES_IN_FLOW_ADJUST_DATA = 10
    CONFIG_FILE = "config.ini"

    MAX_STARTUP_PRESSURE_CHECK_ATTEMPTS = 10
    MAX_STARTUP_PRESSURE_CHECK_SUCCESS_READS = 3
    MAX_PRESSURE_CHECK_TIME_SECONDS = 30

    #Limiter for the PID algorithm
    MAX_PID_POWER_OUTPUT = 65

    # Constructor initializes the physical modules
    def __init__(self, device_version):
        self._logger = get_app_logger(str(self.__class__))
        self.device_version = device_version

        # add cleanup method
        atexit.register(self.cleanup)

        # flag used to indicate this is the first run
        self._first_run = True

        # Init status will contain information about initialization of the hardware components.
        # In case initialization of the component wasnt successful, init status will be other
        # than INIT_STATUS_OK.
        # First hw component to support this type of initialization is pressure sensor.
        self.init_status = None
        self.init_errors = []

        # INIT LOGGING BELOW
        # open a file for logging
        dt = datetime.datetime.now()

        self.logdir = LOGS_DIRECTORY.as_posix()

        # check for log dir and create it if not existing
        if not os.path.exists(self.logdir):
            os.makedirs(self.logdir)

        # store data logfile name
        self._logfilenamedata = "{}/drizzle_data_log_{}.txt".format(self.logdir, dt.year)

        # INIT HARDWARE BELOW

        self._logger.debug("Initializing  valve controller")
        # Init valve controller
        try:
            self._valve_controller = (
                module_steppervalvecontrol.module_steppervalvecontrol(
                    motor_5v=False, reverse_direction=True
                )
            )
        except Exception as error:
            self._logger.error(
                "Error initializing valve controller: {!r}".format(error)
            )
            self.init_status = INIT_STATUS_VALVE_CONTROLLER_ERROR

        # Initialize valves
        self._logger.info("Initializing valves...")
        self._myvalves = self._valve_controller.valve_list
        self._valves = self._valve_controller.ValveList

        # set all valves to relaxed position
        self.set_valves_in_relax_position()

        # init i2c bus
        # init i2c stuff below
        self._logger.info("Initializing i2c bus")
        try:
            self._i2c_bus = smbus.SMBus(1)
        except Exception as error:
            self._logger.error("Error initializing i2c bus. Please check bus")
            self.init_errors.append(INIT_STATUS_I2C_BUS_ERROR)
            self.init_status = INIT_STATUS_I2C_BUS_ERROR

        #detect ADC chip
        self._myadc = detect_adc()
        if self._myadc == module_ADC_driver.module_ADC_driver.CHIP_ADS7828:
            self._logger.info('Detected ADS7828 ADC IC')
        elif self._myadc == module_ADC_driver.module_ADC_driver.CHIP_NCD9830:
            self._logger.info('Detected NCD9830 ADC IC')
        else:
            self._logger.error("Unable to detect ADC chip type.")
            self.init_errors.append(INIT_STATUS_ADC_CHIP_ERROR)
            self.init_status = INIT_STATUS_ADC_CHIP_ERROR

        # Init thermo sensors
        self._logger.info("Initializing thermistor sensing")
        mythermistors = {
            "thermistor{}".format(i): i for i in range(self.NUMBER_OF_THERMISTORS)
        }

        try:
            self._mythermistors = module_thermistorinput.module_thermistorinput(
                mythermistors, self._i2c_bus, self.I2C_ADDRESS_ADC_SENSOR, self._myadc
            )
        except Exception as error:
            self._logger.error(
                "Error initializing thermistor system, Error message: {!r}".format(
                    error
                )
            )
            self.init_errors.append(INIT_STATUS_THERMISTOR_ERROR)
            self.init_status = INIT_STATUS_THERMISTOR_ERROR

        #Decide wether or not to do ADC checking of the fan
        #Initialize fan control
        self._fan_control = module_fancontrol(self.device_version, i2c_dev=self._i2c_bus, chip_adress=self.I2C_ADDRESS_ADC_SENSOR,chip_type=self._myadc)

        # Initialize physical interface
        try:
            self._myphysicalinterface = module_physicalinterface(
                i2c_dev=self._i2c_bus, chip_adress=module_physicalinterface.DEVICE_ADDR
            )
            # self._myphysicalinterface.set_program(1)
            # set actual
            self._myphysicalinterface.set_program_and_state(
                1, module_physicalinterface.DeviceState.READY
            )

        except Exception as e:
            self._myphysicalinterface = None
            self._logger.error(
                "Error initializing physical interface on adress: {}. Please check bus. Error message: {}".format(
                    module_physicalinterface.DEVICE_ADDR, e
                )
            )
            self.init_errors.append(INIT_STATUS_USER_PANEL_ERROR)
            self.init_status = INIT_STATUS_USER_PANEL_ERROR

        # Initialize alcohol sensor
        if ALCOHOL_SENSOR_ENABLED:
            try:
                self._myalcoholsensor = module_alcoholsensor(
                    self._i2c_bus, module_alcoholsensor.DEVICE_ADDR, self._myadc
                )

            except Exception as e:
                self._logger.error(
                    "Error initializing the alcohol sensor on adress: {}. Please check bus. Error message: {}".format(
                        module_alcoholsensor.DEVICE_ADDR, e
                    )
                )
                self.init_errors.append(INIT_STATUS_ALCOHOL_SENSOR_ERROR)
                self.init_status = INIT_STATUS_ALCOHOL_SENSOR_ERROR

        try:
            self._init_pressure_sensor()
        except Exception as e:
            self._logger.error("Error initializing the pressure sensor. Error message: {}".format(e))
            self.init_errors.append(INIT_STATUS_PRESSURE_SENSOR_ERROR)
            self.init_status = INIT_STATUS_PRESSURE_SENSOR_ERROR

        # initialize non i2c stuff
        # Initialize bottom heater
        self._logger.info("Initializing bottom heater")
        self._mybottomheater = module_bottomheatercontrol(
            max_wattage=self.BOTTOM_HEATER_WATTAGE,
            pwm_pin=self.BOTTOM_HEATER_PIN,
        )

        # Initialize pump
        self._logger.info("Initializing pump")
        self._mypump = module_pumpcontrol(pin_pump_pwm=self.PUMP_PIN)

        # Initialize of finite state machine
        self.FSM = module_FSM.FSM(self)

        # Initialize config
        self.init_config()

        # Initialize PID heater controller
        self.reload_PID()

        # Load RGBW module
        self._my_rgbw_light = module_LED_RGBW_control()

        # PID status log interval settings
        self._pid_status_log_interval = 10
        self._pid_last_log_time = None

        # Set init status to ok if it wasn't set to error.
        if self.init_status is None:
            self.init_status = INIT_STATUS_OK


    def _init_pressure_sensor(self):
        pressure_sensor_type = None
        # detect pressure sensor type
        try:
            print('Detecting pressure sensor type')
            # ID Register Address
            BMP280_REG_ID = 0xD0
            BMP384_REG_ID = 0x00
            #detect BMP280
            (bmp280_chip_id, bmp280_chip_version) = self._i2c_bus.read_i2c_block_data(module_HardwareControlSystem.I2C_ADDRESS_PRESSURE_SENSOR, BMP280_REG_ID, 2)
            #detect BMP384
            (bmp384_chip_id, bmp384_chip_version) = self._i2c_bus.read_i2c_block_data(module_HardwareControlSystem.I2C_ADDRESS_PRESSURE_SENSOR, BMP384_REG_ID, 2)
            #print('BMP280 chip id: {}, BMP280 chip version: {}'.format(bmp280_chip_id, bmp280_chip_version))
            #print('BMP384 chip id: {}, BMP384 chip version: {}'.format(bmp384_chip_id, bmp384_chip_version))
            if bmp280_chip_id == 88:
                print('Detected BMP280 type sensor')
                # Initialize pressure sensor
                self._mypressuresensor = module_pressuresensor_bmp280(
                    i2c_dev=self._i2c_bus,
                    chip_adress=module_HardwareControlSystem.I2C_ADDRESS_PRESSURE_SENSOR,
                )
            elif bmp384_chip_id == 80:
                print('Detected BMP384 type sensor')
                # Initialize pressure sensor
                self._mypressuresensor = module_pressuresensor_bmp384(
                    chip_adress=module_HardwareControlSystem.I2C_ADDRESS_PRESSURE_SENSOR,
                )
            else:
                self._logger.error("Error: unable to detect pressure sensor.")
                raise PressureSensorFailure('Error, unable to detect pressure sensor type')

        except Exception as e:
            self._logger.error(
                "Error detecting i2c pressuresensor on adress: {}. Please check bus. Error message: {}".format(
                    module_HardwareControlSystem.I2C_ADDRESS_PRESSURE_SENSOR, e
                )
            )
            raise PressureSensorFailure("Failed to detect pressure sensor.")

        # Pressure sensor check.
        try:
            self.check_pressure_sensor()
        except Exception as e:
            self._logger.error("Error reading pressuresensor. Error message: {}".format(e))
            raise PressureSensorFailure("Failed to read pressure sensor.")

    def check_pressure_sensor(self):
        """Pressure sensor check that should be done during startup. We want to read pressure up to
        MAX_STARTUP_PRESSURE_CHECK_SUCCESS_READS number of times successfully. In this case we could do that
        Check is passed. If we coudn't read pressure sensor up to MAX_STARTUP_PRESSURE_CHECK_ATTEMPTS number of
        attempts or couldn't reach goal until timeout MAX_STARTUP_PRESSURE_CHECK_TIME_SECONDS reached, pressure
        check considered as failed and device enters error state.
        """
        _attempt = 0
        _start_time = time.time()
        _success_reads = 0
        while time.time() - _start_time < self.MAX_PRESSURE_CHECK_TIME_SECONDS:
            try:
                if (
                    (_attempt > self.MAX_STARTUP_PRESSURE_CHECK_ATTEMPTS) or
                    (_success_reads > self.MAX_STARTUP_PRESSURE_CHECK_SUCCESS_READS)
                ):
                    break
                _ = self._mypressuresensor.pressure
                _success_reads += 1
            except Exception as e:
                self._logger.error("Failed to read pressure sensor: %r", e)
                _attempt += 1
                _success_reads = 0  # reset success reads counter.

        if _attempt > 0 and _success_reads < self.MAX_STARTUP_PRESSURE_CHECK_SUCCESS_READS:
            self._logger.error("Pressure check failed.")
            raise PressureSensorFailure("Pressure sensor check failed.")

    def shutdown(self):
        self._logger.info("HW Control - Starting shutdown sequence...")
        if hasattr(self, "_myphysicalinterface"):
            self._myphysicalinterface.shutdown()

        if hasattr(self, "_valve_controller"):
            self._valve_controller.shutdown()

        if hasattr(self, "_myalcoholsensor"):
            self._myalcoholsensor.shutdown()

        self._logger.info("HW Control - shutdown completed.")

    def cleanup(self):
        # the physical interface needs a shutdown command because of the threading architecture
        self._logger.debug("Module HardwareControlSystem - running cleanup")
        self._myphysicalinterface.shutdown()

    def update_config(self):
        config_file = Path(module_HardwareControlSystem.CONFIG_FILE)

        # only execute if file exists
        if config_file.is_file():
            try:
                # get time since last change
                config_file_change = os.path.getmtime(
                    module_HardwareControlSystem.CONFIG_FILE
                )

                if self._last_config_change != config_file_change:
                    self._logger.info("Config was changed, reloading...")
                    # reload config
                    self.init_config()
            except Exception:
                self._logger.exception("config not loaded yet")

    def store_config(self):
        # write config to file
        with open(module_HardwareControlSystem.CONFIG_FILE, "w") as configfile:
            self._config.write(configfile)

    # Control exposure of the RGBW light
    def light_warm(self):
        self._my_rgbw_light.light_warm()

    def light_off(self):
        self._my_rgbw_light.light_off()

    def light_red(self):
        self._my_rgbw_light.light_red()

    def toggle_red_light(self):
        self._myphysicalinterface.toggle_reg_light()

    def toggle_light(self):
        self._my_rgbw_light.toggle_white_light()

    def do_fast_blink(self):
        for _ in range(0, 30):
            self.light_off()
            time.sleep(0.1)
            self.light_warm()
            time.sleep(0.1)
        self.light_off()

    def do_slow_blink(self):
        for _ in range(0, 10):
            self.light_off()
            time.sleep(0.3)
            self.light_warm()
            time.sleep(0.7)
        self.light_off()

    def show_error_code_in_display(self):
        # use the program indicators to show the error mode the machine is in
        myfailuremode = self.FSM.fsmData["failure_mode"]
        self._logger.debug("Failure mode is: {}".format(myfailuremode))
        if not self._myphysicalinterface:
            return
        self._myphysicalinterface.set_error_indicator(False, False, False, False)
        if myfailuremode == FailureMode.NONE:
            self._myphysicalinterface.set_error_indicator(False, False, False, False)
            self._logger.debug("No error, but still in error mode")
        elif myfailuremode == FailureMode.EVC_LEAK:
            self._myphysicalinterface.set_error_indicator(True, False, False, False)
            self._logger.debug(
                "Error, Leak in distillation chamber"
            )
        elif myfailuremode == FailureMode.EXC_LEAK:
            self._myphysicalinterface.set_error_indicator(False, True, False, False)
            self._logger.debug(
                "Error, Leak in extraction chamber"
            )
        elif myfailuremode == FailureMode.ALCOHOL_GASLEVEL_ERROR:
            self._myphysicalinterface.set_error_indicator(False, False, True, False)
            self._logger.debug(
                "Error, IPA gas level too high"
            )
        elif myfailuremode == FailureMode.VALVE_3_BLOCKED:
            self._myphysicalinterface.set_error_indicator(False, False, False, True)
            self._logger.debug(
                "Error, Valve 3 blocked (Extractor -> Distiller)"
            )
        elif myfailuremode == FailureMode.HEATER_ERROR:
            self._myphysicalinterface.set_error_indicator(True, True, False, False)
            self._logger.debug(
                "Error, Heater or heater cable defective"
            )
        elif myfailuremode == FailureMode.PUMP_NEEDS_CLEAN_OR_REPLACEMENT:
            self._myphysicalinterface.set_error_indicator(True, False, True, False)
            self._logger.debug(
                "Error, Pump needs cleaning or replacement"
            )
        elif myfailuremode == FailureMode.VALVE_2_BLOCKED:
            self._myphysicalinterface.set_error_indicator(False, True, True, False)
            self._logger.debug(
                "Error, Valve 2 blocked (Air -> Extraction)"
            )
        elif myfailuremode == FailureMode.VALVE_4_BLOCKED:
            self._myphysicalinterface.set_error_indicator(False, False, True, True)
            self._logger.debug("Error, Valve 4 blocked (Air -> Distiller)")

        elif myfailuremode == FailureMode.VALVE_1_OR_VALVE_3_BLOCKED:
            self._myphysicalinterface.set_error_indicator(True, False, False, True)
            self._logger.debug(
                "Error, Valve 1 or valve 3 is blocked, difficult aspirating alcohol"
            )
        elif myfailuremode == FailureMode.FAN_ERROR:
            self._myphysicalinterface.set_error_indicator(False, True, False, True)
            self._logger.debug(
                "Error, Fan error"
            )
        elif myfailuremode == FailureMode.PRESSURE_SENSOR_ERROR:
            self._myphysicalinterface.set_error_indicator(False, True, True, True)
            self._logger.debug(
                "Error, Pressure sensor error"
            )

        elif myfailuremode == FailureMode.UNKNOWN_ERROR:
            self._myphysicalinterface.set_error_indicator(True, True, True, True)
            self._logger.debug("Error, Unknown error")

    def init_FSM(self):
        self.FSM.init_FSM()

    def init_config(self):
        # Fetch config file
        _config = configparser.ConfigParser()

        # _config.read doesn't fail when there is no file or file is empty,
        # so we need to check if config is not empty.
        _config.read(module_HardwareControlSystem.CONFIG_FILE)
        if not _config.sections():
            # if there is no config file add a default one
            self._logger.debug("Failed to load config file, creating a new one")
            # set initial data
            _config["SYSTEM"] = {
                "pressure_slope_sample_time": "2000",
                "data_log": "0",
                "alcohol_data_log": "0",
                "soak_time_seconds": "10",
            }

            _config["FSM_EX"] = {
                "min_delta_pressure": "-2",
                "maximum_vacuum_pressure": "300",
                "maximum_vacuum_time": "120",
                "tube_filling_vacuum": "300",
                "max_pressure_loss_evc": "2.5",
                "max_pressure_logg_full": "2.5",
                "sample_time": "1",
                "leak_sample_time": "3",
                "leak_delay_time": "10",
                "pressure_eq_time": "4",
                "evc_volume": "290",
                "valve_last_known_setting": "28",
                "valve_start_close_value": "40",
                "valve_start_close_time": "0.00",
                "valve_adjust_amount": "0.25",
                "valve_adjust_hysteresis": "0.1",
                "valve_adjust_delay": "1",
                "leak_detect_period": "30",
                "leak_detect_duration": "2",
                "calculated_exc_volume_calibration_data": "155.0, 170.0, 185.0",
                "calculated_aspirated_volume_calibration_data": "175.0, 180.0, 185.0",
                "EXC_volume_undershoot": "50",
                "top_up_time": "8",
                "top_up_afterfill_valve_setting": "60",
                "aspirate_volume": "150",
                "aspirate_speed": "2",
                "number_of_flushes": "1",
                "flush_time": "10",
                "flowrate_fall_limit": "0.1",
            }

            _config["FSM_EV"] = {
                "min_temp": "0",
                "max_temp": "160",
                "error_pressure_during_distill": "375",
                "pressure_limit_during_distill": "230",
                "time_delay_before_pressure_check": "90",
                "time_interval_between_temp_regulation": "300",
                "valve_last_known_setting": "28",
                "valve_start_close_value": "40",
                "valve_start_close_time": "0.25",
                "valve_adjust_delay": "1",
                "valve_adjust_amount": "0.25",
                "valve_adjust_hysteresis": "0.1",
                "leak_detect_period": "30",
                "leak_detect_duration": "2",
                "distillation_temperature": "125",
                "after_heat_time": "240",
                "after_heat_temp": "107",
                "final_air_cycles": "16",
                "final_air_cycles_time_open": "2",
                "final_air_cycles_time_closed": "88",
                "temperature_critical_level": "150",
                "temperature_critical_level_max_interval": "30",
                "temperature_check_interval": "20",
                "temperature_increase_threshold": "5",
                "temperature_check_threshold": "100",
                "error_pressure_increase_threshold": "4",
                "ambient_pressure_upper_bound": "1100",
                "ambient_pressure_lower_bound": "750",
                "peak_pressure_detection_interval_seconds": "20",
                "peak_pressure_during_distill": "300",
                "pressure_peak_handle_time_seconds": "600",
                "pressure_peak_max_pressure": "600",
            }

            _config["DECARB"] = {
                "temperature": "125",
                "time_minutes": "30",
            }

            _config["OIL_MIX"] = {
                "temperature": "60",
                "time_minutes": "10",
            }

            _config["PID"] = {
                "Pterm": "1",
                "Iterm": "0.25",
                "Dterm": "0.05",
                "sample_time": "1",
                "windup": "200",
                "initial_window_delay": "300",
                "current_window": "100",
                "wattage_decrease_limit": "35",
            }

            _config["FLOW_ADJ"] = {
                "pct_stage_1": "25",
                "step_size_stage_1": "1",
                "step_period_stage_1": "0.5",
                "pct_stage_2": "50",
                "step_size_stage_2": "0.5",
                "step_period_stage_2": "1",
                "pct_stage_3": "90",
                "step_size_stage_3": "0.25",
                "step_period_stage_3": "2",
                "pct_stage_4": "110",
                "step_size_stage_4": "0",
                "step_period_stage_4": "3",
                "pct_stage_5": "150",
                "step_size_stage_5": "0.25",
                "step_period_stage_5": "2",
                "pct_stage_6": "200",
                "step_size_stage_6": "0.5",
                "step_period_stage_6": "1",
                "pct_stage_7": "300",
                "step_size_stage_7": "1",
                "step_period_stage_7": "0.5",
                "pct_stage_8": "400",
                "step_size_stage_8": "4",
                "step_period_stage_8": "0.5",
                "pct_stage_9": "500",
                "step_size_stage_9": "8",
                "step_period_stage_9": "0.5",
                "pct_stage_10": "600",
                "step_size_stage_10": "8",
                "step_period_stage_10": "0.5",
            }

        self._config = _config
        self.store_config()

        # store last change time
        self._last_config_change = os.path.getmtime(
            module_HardwareControlSystem.CONFIG_FILE
        )

        # process data for flow adjustment
        self.process_flow_adjustment_input()

    def process_flow_adjustment_input(self):
        # reset data
        self._flow_adj_data = {}

        # load available data
        for i in range(1, module_HardwareControlSystem.ENTRIES_IN_FLOW_ADJUST_DATA):
            pct_stage = "pct_stage_" + str(i)
            step_size = "step_size_stage_" + str(i)
            step_period = "step_period_stage_" + str(i)
            self._flow_adj_data[self._config["FLOW_ADJ"][pct_stage]] = {
                "step_size": self._config["FLOW_ADJ"][step_size],
                "step_period": self._config["FLOW_ADJ"][step_period],
            }

    # Method returns desired step size and period based on the actual flow error
    def get_step_and_period(self, error):
        step_size = 0
        step_period = 0

        for i in range(1, 11):
            db_string = "pct_stage_" + str(i)
            if error < float(self._config["FLOW_ADJ"][db_string]):
                step_size = float(self._config["FLOW_ADJ"]["step_size_stage_" + str(i)])
                step_period = float(
                    self._config["FLOW_ADJ"]["step_period_stage_" + str(i)]
                )
                break
        else:
            step_size = float(self._config["FLOW_ADJ"]["step_size_stage_10"])
            step_period = float(self._config["FLOW_ADJ"]["step_period_stage_10"])

        return [step_size, step_period]

    # Conversion between the measured air volume and the required air displacement volume to fill the EXC
    def convert_air_volume_to_plant_and_liquid_volume(self, air_volume):
        air_volume_calib_data = [
            float(i)
            for i in self._config["FSM_EX"][
                "calculated_exc_volume_calibration_data"
            ].split(",")
        ]
        actual_volume_calib_data = [
            float(i)
            for i in self._config["FSM_EX"][
                "calculated_aspirated_volume_calibration_data"
            ].split(",")
        ]

        return math.convert_air_volume_to_plant_and_liquid_volume(
            air_volume_calib_data, actual_volume_calib_data, air_volume
        )

    # Sets the bottom heaters heating target
    def set_PID_target(self, value):
        self._PID.setpoint = value

    # Turns PID heater control on
    def PID_on(self, pid_max_output_limit=MAX_PID_POWER_OUTPUT):
        self._PID.PID_running = True
        self._PID = PID.PID(
            Kp=float(self._config["PID"]["Pterm"]),
            Ki=float(self._config["PID"]["Iterm"]),
            Kd=float(self._config["PID"]["Dterm"]),
            sample_time=float(self._config["PID"]["sample_time"]),
            output_limits=(0, pid_max_output_limit),
            sample_initial_delay=self._config["PID"]["initial_window_delay"],
            current_window_size=self._config["PID"]["current_window"],
        )
        self._PID.reset()
        self._logger.info("PID on")

    # Turns PID heater control off
    def PID_off(self, pid_max_output_limit=MAX_PID_POWER_OUTPUT):
        self._PID.PID_running = False
        self._PID = PID.PID(
            Kp=float(self._config["PID"]["Pterm"]),
            Ki=float(self._config["PID"]["Iterm"]),
            Kd=float(self._config["PID"]["Dterm"]),
            sample_time=float(self._config["PID"]["sample_time"]),
            output_limits=(0, pid_max_output_limit),
            sample_initial_delay=self._config["PID"]["initial_window_delay"],
            current_window_size=self._config["PID"]["current_window"],
        )
        self._PID.reset()
        self._logger.info("PID off")

    # PID Function that updates the heating value based on the current target
    def update_PID(self, log=True):
        # invoke PID controller
        (output, did_run) = self._PID.__call__(self.bottom_temperature)
        (Kp, Ki, Kd) = self._PID.components
        self.bottom_heater_percent = output

        # only log when PID controller updates
        if did_run:
            if log:
                if not self._pid_last_log_time or ((time.time() - self._pid_last_log_time) > self._pid_status_log_interval):
                    self._logger.info(
                        "PID: Kp: {:.02f}; Ki: {:.02f}; Kd: {:.02f}; output: {:.02f}; current temperature: {:.02f}; target: {:.02f}".format(
                            Kp, Ki, Kd, output, self.bottom_temperature, self._PID.setpoint
                        )
                    )
                    self._pid_last_log_time = time.time()

        return did_run

    # Refresh PID parameters based on input from the config file
    def reload_PID(self, pid_max_output_limit=MAX_PID_POWER_OUTPUT):
        # init PID
        self._PID = PID.PID(
            Kp=float(self._config["PID"]["Pterm"]),
            Ki=float(self._config["PID"]["Iterm"]),
            Kd=float(self._config["PID"]["Dterm"]),
            sample_time=float(self._config["PID"]["sample_time"]),
            output_limits=(0, pid_max_output_limit),
            sample_initial_delay=self._config["PID"]["initial_window_delay"],
            current_window_size=self._config["PID"]["current_window"],
        )

    ###---===HARDWARE INTERFACING METHODS===---###

    # thermistor interfacing - read all thermistor values
    @property
    def thermistor_status(self):
        #try:
        return self._mythermistors.get_all_temperatures
        #except Exception:
            #raise HardwareFailure("Can't read temperature")

    # Valve interfacing - get all valve settings
    @property
    def valve_status(self):
        myvalves = {
            valve.value: self._valve_controller.get_valve_position(valve)
            for valve in self._myvalves
        }

        return myvalves

    # Valve interfacing - set valve position
    def set_valve(self, valve, position):
        if isinstance(valve, str):
            if not hasattr(self._valves, valve.upper()):
                raise AttributeError(
                    "Error, valve dict does not have a valve with the name: {}".format(
                        valve
                    )
                )

            valve = self._valves[valve.upper()]

        # set the valve
        self._valve_controller.move_to_pos_fullstep(valve, position)

    #Sets valve in a position ok for switching off machine
    def set_valves_in_relax_position(self):
        self.set_valve("valve1", 0)
        self.set_valve("valve4", 100)
        self.set_valve("valve3", 100)
        self.set_valve("valve2", 100)


    def set_alcohol_sensor_on(self):
        if ALCOHOL_SENSOR_ENABLED:
            self._myalcoholsensor.heater_on()
            self._logger.info("Alcohol sensor on")
        else:
            self._logger.info("Alcohol sensor is disabled")

    def set_alcohol_sensor_off(self):
        if ALCOHOL_SENSOR_ENABLED:
            self._myalcoholsensor.heater_off()
            self._logger.info("Alcohol sensor off")
        else:
            self._logger.info("Alcohol sensor is disabled")

    @property
    def alcohol_level(self):
        try:
            return self._myalcoholsensor.get_alcohol_level()
        except Exception:
            raise HardwareFailure("Can't read alcohol level.")

    @property
    def alcohol_level_raw(self):
        return self._myalcoholsensor.get_raw_alcohol_level

    @property
    def selected_program(self):
        return self._selected_program

    @selected_program.setter
    def selected_program(self, value):
        self._selected_program = value
        self._myphysicalinterface.set_state(self._selected_program)

    @property
    def button_press(self):
        if self._myphysicalinterface:
            return self._myphysicalinterface.button_pressed

    @property
    def button_press_force(self):
        if self._myphysicalinterface:
            return self._myphysicalinterface.button_pressed_force

    @property
    def gas_temperature(self):
        return self._mypressuresensor.temperature

    @property
    def exc_volume(self):
        return self._exc_volume  # TODO: There is no such attribute in this class.

    @property
    def bottom_temperature(self):
        try:
            return self._mythermistors.get_temperature("thermistor0")
        except Exception:
            raise HardwareFailure("Can't read temperature.")

    @property
    def fan_ADC_value(self):
        return 0

    @property
    def fan_value(self):
        return self._fan_control.fan_pwm

    @fan_value.setter
    def fan_value(self, value):
        self._fan_control.fan_pwm = value

    @property
    def bottom_heater_percent(self):
        return self._mybottomheater.power_percent

    @bottom_heater_percent.setter
    def bottom_heater_percent(self, value):
        self._mybottomheater.power_percent = value

    @property
    def bottom_heater_power(self):
        return self._mybottomheater.wattage

    @bottom_heater_power.setter
    def bottom_heater_power(self, value):
        self._mybottomheater.wattage = value

    @property
    def pump_value(self):
        return self._mypump.pump_pwm

    @pump_value.setter
    def pump_value(self, value):
        self._mypump.pump_pwm = value

    @property
    def pressure(self):
        if self.init_status != INIT_STATUS_OK:
            raise PressureSensorFailure("Failed to read pressure sensor")

        self._end_timer = timer()

        if self._first_run:
            # setup variables for detectic pressure changes as function of time
            self._logger.debug("starting time measurement")
            self._first_run = False
            self._start_timer = timer()

            # fetch current pressure
            self._current_pressure = self._mypressuresensor.pressure
            self._last_pressure = self._current_pressure
            return self._current_pressure

        # fetch current pressure
        _start_time = time.time()
        while (time.time() - _start_time) < self.MAX_PRESSURE_CHECK_TIME_SECONDS:
            try:
                self._current_pressure = self._mypressuresensor.pressure
                break
            except Exception:
                self._logger.error("Failed to read pressure sensor. Retrying...")
        else:
            raise PressureSensorFailure("Failed to read pressure sensor")
        #log temperature of sensor
        #self._logger.debug('Pressure sensor temperature: {} C'.format(self._mypressuresensor.temperature))

        # calculate time since last measurement
        if ((self._end_timer - self._start_timer) * 1000) > float(
            self._config["SYSTEM"]["pressure_slope_sample_time"]
        ):
            # calculate elapsed time
            self._time_elapsed = self._end_timer - self._start_timer
            # and pressure difference in that time
            self._pressure_diff = self._current_pressure - self._last_pressure

            # restart time measurement
            self._start_timer = timer()
            # store pressure measurement for later use
            self._last_pressure = self._current_pressure

        return self._current_pressure

    @property
    def pressure_slope(self):
        pressure_slope = 0

        try:
            math.get_pressure_slope(self._pressure_diff, self._time_elapsed)
        except Exception:
            pressure_slope = 0

        return pressure_slope

    #reads the system ssid and password
    @property
    def get_ssid_and_passwd(self):
        hostfile = '/etc/hostapd/hostapd.conf'
        myssid = ''
        mypasswd = ''
        with open(hostfile) as f:
            lines = f.readlines()
        for line in lines:
            if 'ssid' in line:
                myssid = line.split('=')[1]
            if 'wpa_passphrase' in line:
                mypasswd = line.split('=')[1]
        return (myssid, mypasswd)


    ###---===SIMPLE HARDWARE MACROS===---###

    # Method drains the liquid backwards out of the EXC
    def drain_system(self):
        self.set_valve("valve1", 0)
        self.set_valve("valve2", 0)
        self.set_valve("valve4", 100)
        self.set_valve("valve3", 100)
        self.set_valve("valve1", 100)  # TODO: is this ok?

    # Method flushes EXC into the EVC
    def flush_system(self):
        # close all valves

        self._logger.info("Close all valves")
        for valve in self._myvalves:
            self.set_valve(valve, 0)

        # Start pump at 100#
        self.pump_value = 100
        time.sleep(1)
        pressure = self.FSM.machine.pressure
        while pressure > float(self._config["FSM_EX"]["maximum_vacuum_pressure"]):
            time.sleep(1)
            pressure = self.FSM.machine.pressure
            self._logger.info("System depressuring: {:.02f} mbar".format(pressure))

        self.set_valve("valve2", 100)
        self.set_valve("valve3", 100)

        time.sleep(5)

        self.pump_value = 0
        # close all valves
        self._logger.info("Close all valves")
        for valve in self._myvalves:
            self.set_valve(valve, 0)


###---===MAIN, USED FOR UNIT TESTING===---###
def main():
    myHardwareControlSystem = module_HardwareControlSystem()

    while True:
        for key, val in myHardwareControlSystem.valve_status.items():
            myHardwareControlSystem._logger.debug("{} has value: {}".format(key, val))
            time.sleep(0.5)
            # increase motor position
            myHardwareControlSystem.set_valve(key, val + 5)


if __name__ == "__main__":
    main()
