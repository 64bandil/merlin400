if __name__ == "__main__":
  #Add root folder (/src/) to paths for import if this script is run standalone
  from pathlib import Path
  import sys
  sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

import RPi.GPIO as GPIO
import atexit

from common.module_logging import get_app_logger


class module_bottomheatercontrol:
    FREQUENCY = 500

    def __init__(self, max_wattage, pwm_pin=12):
        self._logger = get_app_logger(str(self.__class__))

        self._pin_pwm = pwm_pin
        self._max_wattage = max_wattage

        # duty cycle is always initialized to zero
        self._duty_cycle = 0

        # initialize IO system
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(self._pin_pwm, GPIO.OUT)
        self._pwm_output = GPIO.PWM(self._pin_pwm, module_bottomheatercontrol.FREQUENCY)
        self._pwm_output.start(self._duty_cycle)

        # add cleanup method
        atexit.register(self.cleanup)

    @property
    def max_wattage(self):
        return self._max_wattage

    @property
    def wattage(self):
        wattage = self._max_wattage / 100 * self._duty_cycle
        return wattage

    @wattage.setter
    def wattage(self, wattage):
        if not (isinstance(wattage, int) or isinstance(wattage, float)):
            raise AttributeError(
                "Wattage must be an integer or float between 0 and {}".format(
                    self._max_wattage
                )
            )
        if wattage < 0:
            raise AttributeError("Wattage cannot be below 0")
        elif wattage > self._max_wattage:
            raise AttributeError("Wattage cannot be over {}W".format(self._max_wattage))
        else:
            # calculate and set the dutycycle
            self.power_percent = (wattage / self._max_wattage) * 100

    @property
    def power_percent(self):
        return self._duty_cycle

    @power_percent.setter
    def power_percent(self, power_pct):
        if not (isinstance(power_pct, int) or isinstance(power_pct, float)):
            raise AttributeError("Wattage must be an integer between 0 and 100")
        if power_pct < 0:
            raise AttributeError("Wattage cannot be below 0")
        elif power_pct > 100:
            raise AttributeError("Wattage cannot be over 100%")
        else:

            # set actual output level
            self._duty_cycle = power_pct
            self._pwm_output.ChangeDutyCycle(self._duty_cycle)

    def shutdown(self):
        self._duty_cycle = 0
        self._pwm_output.stop()

    # method always runs on exit
    def cleanup(self):
        self._logger.debug("Module bottomheatercontrol - running cleanup")
        self.shutdown()
        GPIO.cleanup()


def main():
    myheater = module_bottomheatercontrol(max_wattage=50)

    print("Max wattage is {} watts".format(myheater.max_wattage))

    while True:
        keypress = input("\nPress enter power level in %> ")

        if str(keypress) == "q":
            exit(1)

        # read powerlevel from termal input
        powerlevel = int(keypress)

        # and output it to thermal control system
        myheater.power_percent = powerlevel

        # myheater.wattage = powerlevel
        print("Wattage is: " + str(myheater.wattage))
        print("Powerlevel is: " + str(myheater.power_percent) + "%")


if __name__ == "__main__":
    main()
