if __name__ == "__main__":
  #Add root folder (/src/) to paths for import if this script is run standalone
  from pathlib import Path
  import sys
  sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

import atexit
import enum
import time
import smbus

from common.module_logging import get_app_logger

def detect_adc():
    i2c = None
    myadc = module_ADC_driver.CHIP_NCD9830
    output_data = [0x84, 0x94, 0xa4, 0xb4]

    i2c = smbus.SMBus(module_ADC_driver.BUS_NUM)

    for data_byte in output_data:
        i2c.write_byte(module_ADC_driver.DEVICE_ADDR, data_byte)

        time.sleep(0.05)
        return_data = i2c.read_i2c_block_data(module_ADC_driver.DEVICE_ADDR, data_byte, 2)
        if return_data[0] == return_data[1]:
            continue
        else:
            myadc = module_ADC_driver.CHIP_ADS7828 
            break
        
        time.sleep(0.2)
    
    return myadc

class module_ADC_driver:
    """
    Module that adds a raspberry pi interface to the NCD9830 ADC IC from On Semi Protocol.
    """

    MAX_I2C_READ_RETRIES = 100
    MAX_I2C_READ_RETRIES_TIMEOUT_SECONDS = 10

    # I2C Address
    DEVICE_ADDR = 0x48
    BUS_NUM = 1
    CHIP_ADS7828 = 0
    CHIP_NCD9830 = 1

    class CommandByte_NCD9830(int, enum.Enum):
        def __new__(cls, channel, command_byte):
            obj = int.__new__(cls, channel)
            obj._value_ = channel
            obj.command_byte = command_byte
            return obj

        CH0 = (0, 0b10000100)  # SD=true, PD1=0, PD0=1, C2=0, C1=0, C0=0
        CH1 = (1, 0b11000100)  # SD=true, PD1=0, PD0=1, C2=1, C1=0, C0=0
        CH2 = (2, 0b10010100)  # SD=true, PD1=0, PD0=1, C2=0, C1=0, C0=1
        CH3 = (3, 0b11010100)  # SD=true, PD1=0, PD0=1, C2=1, C1=0, C0=1
        CH4 = (4, 0b10100100)  # SD=true, PD1=0, PD0=1, C2=0, C1=1, C0=0
        CH5 = (5, 0b11100100)  # SD=true, PD1=0, PD0=1, C2=1, C1=1, C0=0
        CH6 = (6, 0b10110100)  # SD=true, PD1=0, PD0=1, C2=0, C1=1, C0=1
        CH7 = (7, 0b11110100)  # SD=true, PD1=0, PD0=1, C2=1, C1=1, C0=1

    class CommandByte_ADS7828(int, enum.Enum):
        def __new__(cls, channel, command_byte):
            obj = int.__new__(cls, channel)
            obj._value_ = channel
            obj.command_byte = command_byte
            return obj
        #CONFIG = SINGLE ENDED, INT REF OFF, AD ON
        CH0 = (0, 0x84)
        CH1 = (1, 0xc4)
        CH2 = (2, 0x94)
        CH3 = (3, 0xd4)
        CH4 = (4, 0xa4)
        CH5 = (5, 0xe4)
        CH6 = (6, 0xb4)
        CH7 = (7, 0xf4)

    def __init__(self, i2c_dev, chip_adress, chip_type):
        self._logger = get_app_logger(str(self.__class__))

        # initialize i2c
        self._i2c_address = chip_adress
        self._bus = i2c_dev

        self._chip_type = chip_type

        # set cleaup method to run whenever the object is disposed.
        atexit.register(self.cleanup)

    # method always runs on exit
    def cleanup(self):
        self._logger.debug("Module ADC_NCD9830 - running cleanup")


    def _get_command_byte(self, channel):
        if not isinstance(channel, int):
            raise AttributeError("Error, thermistor_channel must be an int")
        if channel < 0:
            raise AttributeError(
                "Error, thermistor_channel be between 0 and 8, selected channel was: {}".format(channel)
            )
        if channel > 8:
            raise AttributeError(
                "Error, thermistor_channel be between 0 and 8, selected channel was: {}".format(channel)
            )

        if self._chip_type == module_ADC_driver.CHIP_NCD9830:
            return self.CommandByte_NCD9830(channel).command_byte
        elif self._chip_type == module_ADC_driver.CHIP_ADS7828:
            return self.CommandByte_ADS7828(channel).command_byte
        else:
            raise Exception('Error, no ADC type selected during init')

    def _read_single(self, channel, print_debug=False):
        command_byte = self._get_command_byte(channel)

        retry_counter = 0
        start_time = time.time()
        _error_reported = False
        return_data = 0
        while (time.time() - start_time) < self.MAX_I2C_READ_RETRIES_TIMEOUT_SECONDS:
            try:
                # read conversion
                if self._chip_type == module_ADC_driver.CHIP_NCD9830:
                    read_data = self._bus.read_i2c_block_data(self._i2c_address, command_byte, 1)
                    return_data = read_data[0]
                elif self._chip_type == module_ADC_driver.CHIP_ADS7828:
                    read_data = self._bus.read_i2c_block_data(self._i2c_address, command_byte, 2)
                    return_data = ((read_data[0] << 8) + read_data[1])
                else:
                    raise Exception('Error, no ADC type selected during init')
                break
            except Exception as error:
                # error checking because of potential i2c errors
                retry_counter += 1
                if not _error_reported:
                    self._logger.error("Got non critical i2c error on ADC is: {}".format(error))
                    self._logger.error("Retrying temperature reading...")
                    _error_reported = True
        else:
            self._logger.error("Critical i2c error on ADC ic. Cannot read temperature.")
            raise Exception("Unable to read i2c bus")

        if print_debug:
            print("command: 0x{:02x}, 0b{:08b}, data: {:02x}".format(command_byte, command_byte, return_data))

        return return_data

    def read_adc_1x(self, channel):
        adc_val = self._read_single(channel)
        return adc_val

    def read_adc_8x(self, channel):
        adc_val_accumulated = 0

        for _ in range(0, 8):
            adc_val_accumulated += self._read_single(channel)

        adc_val = adc_val_accumulated / 8

        return adc_val

    def read_adc_64x(self, channel):
        adc_val_accumulated = 0

        for _ in range(0, 64):
            adc_val_accumulated += self._read_single(channel)

        adc_val = adc_val_accumulated / 64

        return adc_val

def main():
    """
    Main is simply used for unit testing. module_ADC_driver is designed as a driver for other modules.
    """
    _i2c_address = module_ADC_driver.DEVICE_ADDR
    _bus = smbus.SMBus(module_ADC_driver.BUS_NUM)

    adc_chip = detect_adc()

    # setup relay module
    my_adc = module_ADC_driver(_bus, _i2c_address, adc_chip)

    print("")
    print("---==module_ADC_NCD9830==---")

    while True:
        for i in range(0, 1):
            adc_value = my_adc.read_adc_64x(i)
            print("ADC{}: {}".format(i, adc_value))
        time.sleep(1)


if __name__ == "__main__":
    main()
