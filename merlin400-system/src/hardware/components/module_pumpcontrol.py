if __name__ == "__main__":
  #Add root folder (/src/) to paths for import if this script is run standalone
  from pathlib import Path
  import sys
  sys.path.append(str(Path(__file__).resolve().parent.parent.parent)) 

import RPi.GPIO as GPIO
import atexit
from common.module_logging import get_app_logger

class module_pumpcontrol:
    """
    Module for controlling a pump using PWM. It can run in normal or
    inverse mode. Inverse mode is for circuits with a priming mosfet
    that inverts the signal
    """

    FREQUENCY = 500
    PUMP_PWM_MIN = 0
    PUMP_PWM_MAX = 100
    PUMP_PWM_DEFAULT = 16

    def __init__(self, pin_pump_pwm=PUMP_PWM_DEFAULT, invert_signal=False):
        """
        Constructor - pin_pump_pwm is the pin used to connect the pump
        drive circuit. Invert signal indicates if PWM signal is inverted
        or not.
        """
        self._logger = get_app_logger(str(self.__class__))

        # setup local variables here
        self._pwm_value = self.PUMP_PWM_MIN
        self._invert_signal = invert_signal
        self._pin_pump_pwm = pin_pump_pwm

        # init GPIO's
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self._pin_pump_pwm, GPIO.OUT)

        # setup pin as PWM output
        self._pwm = GPIO.PWM(self._pin_pump_pwm, module_pumpcontrol.FREQUENCY)

        # start system in off state
        if self._invert_signal:
            self._pwm.start(self.PUMP_PWM_MAX)
        else:
            self._pwm.start(self.PUMP_PWM_MIN)

        # add cleanup method
        atexit.register(self.cleanup)

    def _check_pump_pwm(self, value):
        if not isinstance(value, int):
            raise Exception("Error, function only accepts integers")
        if value > self.PUMP_PWM_MAX:
            raise Exception("Error, pwm value larger than 100")
        elif value < self.PUMP_PWM_MIN:
            raise Exception("Error, pwm value cannot be negative")

    @property
    def pump_pwm(self):
        return self._pwm_value

    @pump_pwm.setter
    def pump_pwm(self, value):
        self._check_pump_pwm(value)

        self._pwm_value = value

        if self._invert_signal:
            # set pwm in inverted mode, ie 100-value
            self._pwm.start(self.PUMP_PWM_MAX - self._pwm_value)
        else:
            self._pwm.start(self._pwm_value)

    # method always runs on exit
    def cleanup(self):
        self._logger.debug("Module pumpcontrol - running cleanup")
        GPIO.cleanup()


def main():
    mypump = module_pumpcontrol(pin_pump_pwm=module_pumpcontrol.PUMP_PWM_DEFAULT)

    while True:
        keypress = input("\nPress enter pump level in %, q to quit> ")

        if str(keypress) == "q":
            exit(1)
        try:
            value = int(keypress)
        except Exception:
            print("Error, function only accepts integers")
            continue

        if value > 100:
            print("Error, pwm value larger than 100")
            continue

        elif value < 0:
            print("Error, pwm value cannot be negative")
            continue

        # read powerlevel from termal input
        pumplevel = int(value)

        # and output it to thermal control system
        mypump.pump_pwm = pumplevel

        print("Pumplevel is: " + str(mypump.pump_pwm) + "%")


if __name__ == "__main__":
    main()
