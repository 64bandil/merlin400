if __name__ == "__main__":
  #Add root folder (/src/) to paths for import if this script is run standalone
  from pathlib import Path
  import sys
  sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

import RPi.GPIO as GPIO
from hardware.components.module_ADC_driver import module_ADC_driver

import atexit

from common.module_logging import get_app_logger
from common import utils
import system_setup

class NotSupportedFanError(Exception):
    """Raise when hardware doesn't support fan."""


class module_fancontrol:
    """
    Module for controlling a pump using PWM. It can run in normal or
    inverse mode. Inverse mode is for circuits with a priming mosfet
    that inverts the signal
    """

    FREQUENCY = 500
    PWM_LEVEL_MAX = 100
    PWM_LEVEL_MIN = 0
    PWM_LEVEL_DEFAULT = 20
    ADC_CHANNEL = 1

    #ADC threshhold levels
    ADC_LEVEL_NCD9830_OFF_LOW = 0
    ADC_LEVEL_NCD9830_OFF_HIGH = 1
    ADC_LEVEL_NCD9830_ON_LOW = 1.1
    ADC_LEVEL_NCD9830_ON_HIGH = 10
    ADC_LEVEL_NCD9830_ERROR_LOW = -2
    ADC_LEVEL_NCD9830_ERROR_HIGH = -1

    ADC_LEVEL_ADS7828_OFF_LOW = 0
    ADC_LEVEL_ADS7828_OFF_HIGH = 20
    ADC_LEVEL_ADS7828_ON_LOW = 50
    ADC_LEVEL_ADS7828_ON_HIGH = 400
    ADC_LEVEL_ADS7828_ERROR_LOW = -2
    ADC_LEVEL_ADS7828_ERROR_HIGH = -1

    FAN_ADC_LEVEL_ON = 1
    FAN_ADC_LEVEL_OFF = 0
    FAN_ADC_LEVEL_ERROR = -1


    def __init__(self, device_version, pin_fan_pwm=PWM_LEVEL_DEFAULT, invert_signal=False, i2c_dev=None, chip_adress=None, chip_type=None):
        """
        Constructor - pin_fan_pwm is the pin used to connect the fan
        drive circuit. Invert signal indicates if PWM signal is inverted
        or not.
        """
        self.device_version = device_version
        self._logger = get_app_logger(str(self.__class__))
        self._check_adc = False

        # setup local variables here
        self._pwm_value = self.PWM_LEVEL_MIN
        self._invert_signal = invert_signal
        self._pin_fan_pwm = pin_fan_pwm

        # init GPIO's
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self._pin_fan_pwm, GPIO.OUT)

        # setup pin as PWM output
        self._pwm = GPIO.PWM(self._pin_fan_pwm, module_fancontrol.FREQUENCY)

        #init ADC if requested
        if i2c_dev:
            print('ADC check requested')
            self._i2c_dev=i2c_dev
            self._chip_adress=chip_adress
            self._chip_type=chip_type
            self._adc = module_ADC_driver(i2c_dev, chip_adress, chip_type)
            self._check_adc = True
            self._adc_channel=self.ADC_CHANNEL
        else:
            print('No ADC check')

        # start system in off state
        if self._invert_signal:
            self._pwm.start(self.PWM_LEVEL_MAX)
        else:
            self._pwm.start(self.PWM_LEVEL_MIN)

        # add cleanup method
        atexit.register(self.cleanup)

    def _check_current_level(self):
        current_level = self.FAN_ADC_LEVEL_OFF

        #read the ADC channel
        raw_adc = self._get_ADC_reading()

        if self.device_version == system_setup.DEVICE_V1:
            current_level = None
        elif self.device_version == system_setup.DEVICE_V2 and self._chip_type == module_ADC_driver.CHIP_NCD9830:
            # interpolate calibration values to get celcius result
            self._logger.info('Got NCD9830, level is: {}'.format(raw_adc))
            if raw_adc < self.ADC_LEVEL_NCD9830_ON_HIGH and raw_adc > self.ADC_LEVEL_NCD9830_ON_LOW:
                #fan is ON
                current_level = self.FAN_ADC_LEVEL_ON
            elif raw_adc < self.ADC_LEVEL_NCD9830_OFF_HIGH and raw_adc >= self.ADC_LEVEL_NCD9830_OFF_LOW:
                #fan is OFF
                current_level = self.FAN_ADC_LEVEL_OFF
            else:
                current_level = self.FAN_ADC_LEVEL_ERROR
        elif self.device_version == system_setup.DEVICE_V2 and self._chip_type == module_ADC_driver.CHIP_ADS7828:
            self._logger.info('Got ADS7828, level is: {}'.format(raw_adc))
            if raw_adc < self.ADC_LEVEL_ADS7828_ON_HIGH and raw_adc > self.ADC_LEVEL_ADS7828_ON_LOW:
                #fan is ON
                current_level = self.FAN_ADC_LEVEL_ON
            elif raw_adc < self.ADC_LEVEL_ADS7828_OFF_HIGH and raw_adc >= self.ADC_LEVEL_ADS7828_OFF_LOW:
                #fan is OFF
                current_level = self.FAN_ADC_LEVEL_OFF
            else:
                current_level = self.FAN_ADC_LEVEL_ERROR
        else:
            self._logger.error("This condition normally shouldn't happen. Can't determine fan state.")
            current_level = None

        return current_level

    def _get_ADC_reading(self):
        """
        Method accepts a thermistor channel as an argument and returns the corresponding temperature as float
        """
        # read raw adc value
        raw_adc_value = self._adc.read_adc_64x(self._adc_channel)

        return raw_adc_value

    #checks the if ADC of the fan current is within ON, OFF or ERROR limits
    @property
    def fan_adc_check(self):
        return self._check_current_level()

    @property
    def fan_adc_check_string(self):
        current_check = self.fan_adc_check
        return_string = "ERROR"
        if current_check == 1:
           return_string = "FAN_ON"
        elif current_check == 0:
           return_string = "FAN_OFF"
        elif current_check is None:
            return_string = "NOT_SUPPORTED"
        return return_string

    @property
    def fan_adc_value(self):
        return self._get_ADC_reading()

    @property
    def fan_pwm(self):
        return self._pwm_value

    @fan_pwm.setter
    def fan_pwm(self, value):
        if not isinstance(value, int):
            raise Exception("Error, function only accepts integers")
        if value > self.PWM_LEVEL_MAX:
            raise Exception("Error, pwm value larger than 100")
        elif value < self.PWM_LEVEL_MIN:
            raise Exception("Error, pwm value cannot be negative")

        self._pwm_value = value

        if self._invert_signal:
            # set pwm in inverted mode, ie 100-value
            self._pwm.start(self.PWM_LEVEL_MAX - self._pwm_value)
        else:
            self._pwm.start(self._pwm_value)

    # method always runs on exit
    def cleanup(self):
        self._logger.debug("Module fancontrol - running cleanup")
        self._pwm_value = self.PWM_LEVEL_MIN
        GPIO.cleanup()


def main():
    myfan = module_fancontrol(pin_fan_pwm=module_fancontrol.PWM_LEVEL_DEFAULT)

    while True:
        keypress = input("\nPress enter fan level in %, q to quit> ")

        if str(keypress) == "q":
            exit(1)
        try:
            value = int(keypress)
        except Exception:
            print("Error, function only accepts integers")
            continue

        if value > module_fancontrol.PWM_LEVEL_MAX:
            print("Error, pwm value larger than 100")
            continue

        elif value < module_fancontrol.PWM_LEVEL_MIN:
            print("Error, pwm value cannot be negative")
            continue

        # read powerlevel from termal input
        fanlevel = int(value)

        # and output it to thermal control system
        myfan.fan_pwm = fanlevel

        print("Fanlevel is: " + str(myfan.fan_pwm) + "%")


if __name__ == "__main__":
    main()
