if __name__ == "__main__":
  #Add root folder (/src/) to paths for import if this script is run standalone
  from pathlib import Path
  import sys
  sys.path.append(str(Path(__file__).resolve().parent.parent.parent))
  
import time
import RPi.GPIO as GPIO
import smbus
import enum

# Import the NCD9830 ADC
from hardware.components.module_ADC_driver import module_ADC_driver
from hardware.components.module_ADC_driver import detect_adc

import atexit

from module_logging import get_app_logger


class module_alcoholsensor:
    """
    Module for detecting alcohol sensor using the onboard MQ-3 sensor and onboard ADC ADS1115 IC.
    IC is interfaced using I2C.
    """

    # Default I2C Address
    DEVICE_ADDR = 0x48
    ADC_CHANNEL = 2
    BUS_NUM = 1
    SENSOR_MOSFET_PIN = 19

    # seconds before measurements are ok
    INIT_TIME = 45

    class AlcoholLevelThreshold_NCD9830(int, enum.Enum):
        THRESHOLD_DANGER = 150
        THRESHOLD_WARNING = 100
        THRESHOLD_OK = 30

    class AlcoholLevelThreshold_ADS7828(int, enum.Enum):
        THRESHOLD_DANGER = 5000
        THRESHOLD_WARNING = 4000
        THRESHOLD_OK = 800


    class AlcoholLevelMessage(enum.Enum):
        OK = "OK"
        WARNING = "WARNING"
        DANGER = "DANGER, SHUTDOWN NOW"
        NOT_READY = "NOT READY"
        OFF = "OFF"

    def __init__(self, i2c_dev, chip_adress, chip_type):
        self._logger = get_app_logger(str(self.__class__))
        self._logger.debug("MODULE module_alcoholsensor initializing")

        self._chip_type = chip_type

        # Create an NCD9830
        self._adc = module_ADC_driver(i2c_dev, chip_adress, self._chip_type)
        
        # Channel to fetch alcohol signal
        self._adc_channel = module_alcoholsensor.ADC_CHANNEL

        # setup pin to control mosfet
        GPIO.setmode(GPIO.BCM)

        self._alcohol_sensor_pin = module_alcoholsensor.SENSOR_MOSFET_PIN
        GPIO.setwarnings(False)
        GPIO.setup(self._alcohol_sensor_pin, GPIO.OUT)
        GPIO.output(self._alcohol_sensor_pin, GPIO.LOW)
        self._heater_on = False

        self._raw_adc_value = 0

        # add cleanup method
        atexit.register(self.cleanup)

    def cleanup(self):
        self._logger.debug("Module alcoholsensor - running cleanup")
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self._alcohol_sensor_pin, GPIO.OUT)
        self.heater_off()
        GPIO.cleanup()

    def heater_on(self):
        GPIO.output(self._alcohol_sensor_pin, GPIO.HIGH)
        self._heater_start_time = time.time()
        self._heater_on = True
        self._logger.debug("Module alcoholsensor - heater ON")

    def heater_off(self):
        GPIO.output(self._alcohol_sensor_pin, GPIO.LOW)
        self._heater_on = False
        self._logger.debug("Module alcoholsensor - heater OFF")

    def get_alcohol_level(self):
        """
        Method fetches the alcohol signal from the alcohol sensor and returns a signal: OK, Alcohol detected or danger
        """
        if not self._heater_on:
            return module_alcoholsensor.AlcoholLevelMessage.OFF

        # give the heater a few seconds to start up
        if self._heater_start_time + module_alcoholsensor.INIT_TIME > time.time():
            return module_alcoholsensor.AlcoholLevelMessage.NOT_READY

        # read raw adc value
        self._raw_adc_value = self._adc.read_adc_64x(self._adc_channel)

        if self._chip_type == module_ADC_driver.CHIP_NCD9830:
            if (
                self._raw_adc_value
                > module_alcoholsensor.AlcoholLevelThreshold_NCD9830.THRESHOLD_DANGER
            ):
                return_value = module_alcoholsensor.AlcoholLevelMessage.DANGER
            elif (
                self._raw_adc_value
                > module_alcoholsensor.AlcoholLevelThreshold_NCD9830.THRESHOLD_WARNING
            ):
                return_value = module_alcoholsensor.AlcoholLevelMessage.WARNING
            else:
                return_value = module_alcoholsensor.AlcoholLevelMessage.OK
            return return_value
        elif self._chip_type == module_ADC_driver.CHIP_ADS7828:
            if (
                self._raw_adc_value
                > module_alcoholsensor.AlcoholLevelThreshold_ADS7828.THRESHOLD_DANGER
            ):
                return_value = module_alcoholsensor.AlcoholLevelMessage.DANGER
            elif (
                self._raw_adc_value
                > module_alcoholsensor.AlcoholLevelThreshold_ADS7828.THRESHOLD_WARNING
            ):
                return_value = module_alcoholsensor.AlcoholLevelMessage.WARNING
            else:
                return_value = module_alcoholsensor.AlcoholLevelMessage.OK
            return return_value
        else:
            raise Exception('Error, ADC chip type not detected at startup!')

    @property
    def alcohol_level(self):
        return self.get_alcohol_level()

    @property
    def get_raw_alcohol_level(self):
        return self._raw_adc_value

    def shutdown(self):
        # shut down
        self.heater_off()
        self._heater_on = False


def main():
    """
    Main is simply used for unit testing. module_relaycontrol is designed as a driver for other modules.
    """
    _bus = smbus.SMBus(module_alcoholsensor.BUS_NUM)
    _i2c_address = module_alcoholsensor.DEVICE_ADDR

    myadc = detect_adc()

    myalcoholsensor = module_alcoholsensor(_bus, _i2c_address, myadc)
    myalcoholsensor.heater_on()

    while True:

        alcohol_level = myalcoholsensor.alcohol_level
        print("Alcohol level: {}, raw: {}".format(alcohol_level, myalcoholsensor.get_raw_alcohol_level))

        time.sleep(1)


if __name__ == "__main__":
    main()
