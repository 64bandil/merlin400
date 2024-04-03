if __name__ == "__main__":
  #Add root folder (/src/) to paths for import if this script is run standalone
  from pathlib import Path
  import sys
  sys.path.append(str(Path(__file__).resolve().parent.parent.parent))
  
import time
import smbus
import numpy as np

# Import the NCD9830 ADC
from hardware.components.module_ADC_driver import module_ADC_driver
from hardware.components.module_ADC_driver import detect_adc
import atexit
from common.module_logging import get_app_logger


class module_thermistorinput:
    """
    Module for reading temperature using thermistors and a ADS1115 IC.
    IC is interfaced using I2C.
    """

    # Default I2C Address
    DEVICE_ADDR = 0x48
    BUS_NUM = 1

    # calibration data from 07-09-2020
    TEMP_CALIBRATION_DEGREES_NCD9830 = [0, 21, 30, 40, 50, 65, 70, 80, 90, 100, 110, 120, 130, 140, 150, 160, 170, 180]
    TEMP_CALIBRATION_ADC_NCD9830 = [0, 8, 10, 14, 19, 30, 35, 44, 55, 68, 83, 98, 113, 128, 145, 157, 170, 182]

    #calibration data from 06-01-2023
    TEMP_CALIBRATION_DEGREES_ADS7828 = [0,6.4,15,22,26,30,40,50,60,70,80,90,100,110,120,130,140,150,160,170,180,190,200,210,220,230,240,250,260,270]
    TEMP_CALIBRATION_ADC_ADS7828 = [0,51,100,130,149,172,242,335,450,587,751,927,1140,1382,1673,1905,2122,2353,2538,2716,2871,3022,3134,3245,3345,3427,3502,3569,3628,3680]


    def __init__(self, thermistor_channels, i2c_dev, chip_adress, chip_type):
        self._logger = get_app_logger(str(self.__class__))
        self._logger.debug("MODULE module_thermistorinput initializing")

        # store thermistor channels
        self._thermistor_channels = thermistor_channels
        # store ADC chip type
        self._chip_type = chip_type


        if self._chip_type == module_ADC_driver.CHIP_NCD9830:
            # defines dynamic range variables
            self.max_temp = module_thermistorinput.TEMP_CALIBRATION_DEGREES_NCD9830[-1]
            self.min_temp = module_thermistorinput.TEMP_CALIBRATION_DEGREES_NCD9830[0]
            self.max_adc = module_thermistorinput.TEMP_CALIBRATION_ADC_NCD9830[0]
            self.min_adc = module_thermistorinput.TEMP_CALIBRATION_ADC_NCD9830[-1]
        elif self._chip_type == module_ADC_driver.CHIP_ADS7828:
            self.max_temp = module_thermistorinput.TEMP_CALIBRATION_DEGREES_ADS7828[-1]
            self.min_temp = module_thermistorinput.TEMP_CALIBRATION_DEGREES_ADS7828[0]
            self.max_adc = module_thermistorinput.TEMP_CALIBRATION_ADC_ADS7828[0]
            self.min_adc = module_thermistorinput.TEMP_CALIBRATION_ADC_ADS7828[-1]
        else:
            raise Exception('ADC not detected during startup')

        # Create an NCD9830
        self._adc = module_ADC_driver(i2c_dev, chip_adress, chip_type)

        # add cleanup method
        atexit.register(self.cleanup)

    def cleanup(self):
        self._logger.debug("Module thermistorinput - running cleanup")

    def _convert_to_celcius(self, raw_adc):
        if self._chip_type == module_ADC_driver.CHIP_NCD9830:
            # interpolate calibration values to get celcius result
            temperature = np.interp(
                float(raw_adc),
                module_thermistorinput.TEMP_CALIBRATION_ADC_NCD9830,
                module_thermistorinput.TEMP_CALIBRATION_DEGREES_NCD9830,
            )
        elif self._chip_type == module_ADC_driver.CHIP_ADS7828:
            temperature = np.interp(
                float(raw_adc),
                module_thermistorinput.TEMP_CALIBRATION_ADC_ADS7828,
                module_thermistorinput.TEMP_CALIBRATION_DEGREES_ADS7828,
            )
        else:
            raise Exception('ADC not detected during startup')

        return temperature

    def get_raw(self, thermistor_channel):
        if not isinstance(thermistor_channel, str):
            raise AttributeError("Error, thermistor_channel must be a string")

        # check for valid channel input
        if not thermistor_channel in self._thermistor_channels:
            raise LookupError("Pin not found in object")

        raw_adc_value = self._adc.read_adc_64x(self._thermistor_channels[thermistor_channel])

        return raw_adc_value

    def get_temperature(self, thermistor_channel):
        """
        Method accepts a thermistor channel as an argument and returns the corresponding temperature as float
        """
        if not self.has_channel(thermistor_channel):
            raise LookupError("Pin not found in object")

        # read raw adc value
        raw_adc_value = self._adc.read_adc_64x(self._thermistor_channels[thermistor_channel])

        # convert to celcius
        temperature = self._convert_to_celcius(raw_adc_value)

        return temperature

    @property
    def get_all_temperatures(self):
        return {str(key): float(self.get_temperature(key)) for key in self._thermistor_channels}

    def has_channel(self, thermistor_channel):
        """
        Method accepts a pin_name string as argument and return True if the value is present in the thermistorchannel dict
        """
        if not isinstance(thermistor_channel, str):
            raise AttributeError("Error, thermistor_channel must be a string")

        return thermistor_channel in self._thermistor_channels


def main():
    """
    Main is simply used for unit testing. module_relaycontrol is designed as a driver for other modules.
    """
    _bus = smbus.SMBus(module_thermistorinput.BUS_NUM)
    _i2c_address = module_thermistorinput.DEVICE_ADDR

    thermistors = dict()
    thermistors["thermistor0"] = 0
    
    myadc = detect_adc()

    # setup relay module
    mythermistors = module_thermistorinput(thermistors, _bus, _i2c_address, myadc)

    while True:
        temperature0 = mythermistors.get_temperature("thermistor0")
        raw0 = mythermistors.get_raw("thermistor0")
        print("Raw data on channel 0 is: {:.02f} degrees".format(raw0))
        print("Temperature on channel 0 is: {:.02f} degrees".format(temperature0))
    

        time.sleep(1)


if __name__ == "__main__":
    main()
