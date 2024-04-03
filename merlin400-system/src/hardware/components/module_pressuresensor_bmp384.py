if __name__ == "__main__":
  #Add root folder (/src/) to paths for import if this script is run standalone
  from pathlib import Path
  import sys
  sys.path.append(str(Path(__file__).resolve().parent.parent.parent))
  
import time
import atexit
import board
import adafruit_bmp3xx
import smbus
import threading
from common.module_logging import get_app_logger
from common.utils import stop_after

class module_pressuresensor_bmp384:
    PRESSURE_OVERSAMPLING = 8
    FILTER_COEFFICIENT = 2
    I2C_ADDRESS = 0x76
    READING_TIMEOUT = 3

    def __init__(self, chip_adress):
        """
        Constructor - servo_pwm_pin is the pin used to connect the servo
        drive circuit.
        """
        self._i2c_dev = board.I2C()
        self._logger = get_app_logger(str(self.__class__))

        # store motor adress
        self._chip_adress = chip_adress

        self._mysensor = adafruit_bmp3xx.BMP3XX_I2C(self._i2c_dev, address=chip_adress)

        # add cleanup method
        atexit.register(self.cleanup)

        self._temperature = None
        self._pressure = None
        self._humidity = None
        self._lock = threading.Lock()

    @property
    @stop_after(READING_TIMEOUT)
    def pressure(self):
        try:
            with self._lock:
                self._pressure = self._mysensor.pressure
        except Exception:
            self._logger.exception("Error reading pressure sensor")
        return self._pressure

    @property
    @stop_after(READING_TIMEOUT)
    def temperature(self):
        try:
            with self._lock:
                self._temperature = self._mysensor.temperature
        except Exception:
            self._logger.exception("Error reading pressure sensor")
        return self._temperature

    @property
    def humidity(self):
        try:
            self._humidity = 0
        except Exception:
            self._logger.exception("Error reading humidity")

        return self._humidity

    # method always runs on exit
    def cleanup(self):
        self._logger.debug("Module pressuresensor 384 - running cleanup")


def main():
    pressure_sensor = module_pressuresensor_bmp384(module_pressuresensor_bmp384.I2C_ADDRESS)

    while(True):
        print('Time:  {} seconds'.format(int(time.time())))
        print("Pressure: {:6.1f} mbar".format(pressure_sensor.pressure))
        print("Temperature: {:5.2f} C".format(pressure_sensor.temperature))
        time.sleep(0.5)



if __name__ == "__main__":
    main()
