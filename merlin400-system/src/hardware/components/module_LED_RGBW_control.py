if __name__ == "__main__":
  #Add root folder (/src/) to paths for import if this script is run standalone
  from pathlib import Path
  import sys
  sys.path.append(str(Path(__file__).resolve().parent.parent.parent))
  
import atexit
import enum
import time

import RPi.GPIO as GPIO
from common.module_logging import get_app_logger


class module_LED_RGBW_control:
    """
    Module for controlling a pump using PWM. It can run in normal or
    inverse mode. Inverse mode is for circuits with a priming mosfet
    that inverts the signal
    """

    FREQUENCY = 500

    class RGBWPin(int, enum.Enum):
        RED = 18
        GREEN = 23
        BLUE = 24
        WHITE = 25

    PIN_LEVEL_OFF = 0
    PIN_LEVEL_ON = 100

    def __init__(self):
        """
        Constructor - pin_fan_pwm is the pin used to connect the fan
        drive circuit. Invert signal indicates if PWM signal is inverted
        or not.
        """
        self._logger = get_app_logger(str(self.__class__))

        # setup local variables here
        self._pwm_red = self.PIN_LEVEL_OFF
        self._pwm_green = self.PIN_LEVEL_OFF
        self._pwm_blue = self.PIN_LEVEL_OFF
        self._pwm_white = self.PIN_LEVEL_OFF
        self._pin_red = self.RGBWPin.RED
        self._pin_green = self.RGBWPin.GREEN
        self._pin_blue = self.RGBWPin.BLUE
        self._pin_white = self.RGBWPin.WHITE

        # init GPIO's
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self._pin_red, GPIO.OUT)
        GPIO.setup(self._pin_green, GPIO.OUT)
        GPIO.setup(self._pin_blue, GPIO.OUT)
        GPIO.setup(self._pin_white, GPIO.OUT)

        # setup pin as PWM output
        self._pwm_red = GPIO.PWM(self._pin_red, module_LED_RGBW_control.FREQUENCY)
        self._pwm_green = GPIO.PWM(self._pin_green, module_LED_RGBW_control.FREQUENCY)
        self._pwm_blue = GPIO.PWM(self._pin_blue, module_LED_RGBW_control.FREQUENCY)
        self._pwm_white = GPIO.PWM(self._pin_white, module_LED_RGBW_control.FREQUENCY)

        # start system in off state
        self._pwm_red.start(self.PIN_LEVEL_OFF)
        self._pwm_green.start(self.PIN_LEVEL_OFF)
        self._pwm_blue.start(self.PIN_LEVEL_OFF)
        self._pwm_white.start(self.PIN_LEVEL_OFF)

        self._light_status = 0

        # add cleanup method
        atexit.register(self.cleanup)

    # method always runs on exit
    def cleanup(self):
        self._logger.debug("Module LED_RGBW_control - Running cleanup")
        self._pwm_red.start(self.PIN_LEVEL_OFF)
        self._pwm_green.start(self.PIN_LEVEL_OFF)
        self._pwm_blue.start(self.PIN_LEVEL_OFF)
        self._pwm_white.start(self.PIN_LEVEL_OFF)
        GPIO.cleanup()

    def light_warm(self):
        self._light_status = 1
        self.set_light(100, 0, 0, 0)

    def light_red(self):
        self.set_light(0, 100, 0, 0)

    def light_off(self):
        self._light_status = 0
        self.set_light(0, 0, 0, 0)

    def toggle_white_light(self):
        if self._light_status == 0:
            self.light_warm()
            self._light_status = 1
        elif self._light_status == 1:
            self.light_off()
            self._light_status = 0
        else:
            raise Exception('Error, invalid light state')

    # method to change light from other modules
    def set_light(self, white, red, green, blue):
        self._pwm_white.start(white)
        self._pwm_red.start(red)
        self._pwm_green.start(green)
        self._pwm_blue.start(blue)

if __name__ == "__main__":
    my_led_rgbw = module_LED_RGBW_control()
    my_led_rgbw.light_warm()

    while True:
        time.sleep(4)
        print("Setting 3")



