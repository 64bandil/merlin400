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

class module_steppervalvecontrol:
    class CoilPins(int, enum.Enum):
        COIL_A_1_PIN = 4  # IN 1
        COIL_B_1_PIN = 27  # IN 2
        COIL_A_2_PIN = 5  # IN 3
        COIL_B_2_PIN = 6  # IN 4

    class EnableMotorPin(int, enum.Enum):
        ENABLE_MOTOR_PIN_1 = 13
        ENABLE_MOTOR_PIN_2 = 26
        ENABLE_MOTOR_PIN_3 = 17
        ENABLE_MOTOR_PIN_4 = 22

    # Values for 12VDC motor on 12VDC
    HALFSTEP_DELAY_12V = 0.9 / 1000
    FULLSTEP_DELAY_12V = 1.6 / 1000

    # Values for 5VDC motor on 12VDC
    HALFSTEP_DELAY_5V = 0.6 / 1000
    FULLSTEP_DELAY_5V = 1.0 / 1000
    HOME_STEPS = 300
    STEPS_PER_FULL_SWING = 265

    STEPPER_POS_START = 0
    STEPPER_POS_END = 100

    class ValveList(enum.Enum):
        VALVE1 = "valve1"
        VALVE2 = "valve2"
        VALVE3 = "valve3"
        VALVE4 = "valve4"

    def __init__(self, motor_5v=False, reverse_direction=False):
        self._logger = get_app_logger(str(self.__class__))

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)

        # self.coil_A_1_pin = module_steppervalvecontrol.COIL_A_1_PIN # blue
        self.coil_A_1_pin = self.CoilPins.COIL_B_1_PIN  # blue
        # self.coil_A_2_pin = module_steppervalvecontrol.COIL_A_2_PIN # yellow
        self.coil_A_2_pin = self.CoilPins.COIL_B_2_PIN  # yellow
        # self.coil_B_1_pin = module_steppervalvecontrol.COIL_B_1_PIN # pink
        self.coil_B_1_pin = self.CoilPins.COIL_A_1_PIN  # pink
        # self.coil_B_2_pin = module_steppervalvecontrol.COIL_B_2_PIN # orange
        self.coil_B_2_pin = self.CoilPins.COIL_A_2_PIN  # orange

        self._reverse_motor_direction = reverse_direction

        # Setup halfstepping
        self.StepCountHalfStep = 8
        self.SeqHalfStep = {
            0: [1, 0, 0, 0],
            1: [1, 1, 0, 0],
            2: [0, 1, 0, 0],
            3: [0, 1, 1, 0],
            4: [0, 0, 1, 0],
            5: [0, 0, 1, 1],
            6: [0, 0, 0, 1],
            7: [1, 0, 0, 1],
        }

        self.StepCountFullStep = 4
        self.SeqFullStep = {
            0: [1, 1, 0, 0],
            1: [0, 1, 1, 0],
            2: [0, 0, 1, 1],
            3: [1, 0, 0, 1],
        }

        self.off = [0, 0, 0, 0]

        self._home_steps = module_steppervalvecontrol.HOME_STEPS
        self._steps_per_full_swing = module_steppervalvecontrol.STEPS_PER_FULL_SWING

        if motor_5v:
            self._halfstep_delay = module_steppervalvecontrol.HALFSTEP_DELAY_5V
            self._fullstep_delay = module_steppervalvecontrol.FULLSTEP_DELAY_5V
        else:
            self._halfstep_delay = module_steppervalvecontrol.HALFSTEP_DELAY_12V
            self._fullstep_delay = module_steppervalvecontrol.FULLSTEP_DELAY_12V

        # Setup coil pins
        GPIO.setup(self.coil_A_1_pin, GPIO.OUT)
        GPIO.setup(self.coil_A_2_pin, GPIO.OUT)
        GPIO.setup(self.coil_B_1_pin, GPIO.OUT)
        GPIO.setup(self.coil_B_2_pin, GPIO.OUT)

        # Store list of valves
        self._valve_list = [i for i in self.ValveList]

        # variable for current stepper position
        self._current_position = {valve: 0 for valve in self._valve_list}

        self._current_position_pct = {valve: 0 for valve in self._valve_list}

        self._pin_enable = {
            self.ValveList.VALVE1: self.EnableMotorPin.ENABLE_MOTOR_PIN_1,
            self.ValveList.VALVE2: self.EnableMotorPin.ENABLE_MOTOR_PIN_2,
            self.ValveList.VALVE3: self.EnableMotorPin.ENABLE_MOTOR_PIN_3,
            self.ValveList.VALVE4: self.EnableMotorPin.ENABLE_MOTOR_PIN_4,
        }

        # setup all enable pins
        for valve in self._valve_list:
            GPIO.setup(self._pin_enable[valve], GPIO.OUT)
            GPIO.output(self._pin_enable[valve], GPIO.LOW)

        # home all valves
        for valve in self._valve_list:
            self._logger.info("Homing {}".format(valve))
            self.home_motor(valve)

        # add cleanup method
        atexit.register(self.cleanup)

    def _enable_motor(self, valve):
        if valve == self.ValveList.VALVE1:
            # print('Motor 1')
            GPIO.output(self._pin_enable[self.ValveList.VALVE1], GPIO.HIGH)
            GPIO.output(self._pin_enable[self.ValveList.VALVE2], GPIO.LOW)
            GPIO.output(self._pin_enable[self.ValveList.VALVE3], GPIO.LOW)
            GPIO.output(self._pin_enable[self.ValveList.VALVE4], GPIO.LOW)
        elif valve == self.ValveList.VALVE2:
            # print('Motor 2')
            GPIO.output(self._pin_enable[self.ValveList.VALVE1], GPIO.LOW)
            GPIO.output(self._pin_enable[self.ValveList.VALVE2], GPIO.HIGH)
            GPIO.output(self._pin_enable[self.ValveList.VALVE3], GPIO.LOW)
            GPIO.output(self._pin_enable[self.ValveList.VALVE4], GPIO.LOW)
        elif valve == self.ValveList.VALVE3:
            # print('Motor 3')
            GPIO.output(self._pin_enable[self.ValveList.VALVE1], GPIO.LOW)
            GPIO.output(self._pin_enable[self.ValveList.VALVE2], GPIO.LOW)
            GPIO.output(self._pin_enable[self.ValveList.VALVE3], GPIO.HIGH)
            GPIO.output(self._pin_enable[self.ValveList.VALVE4], GPIO.LOW)
        elif valve == self.ValveList.VALVE4:
            # print('Motor 4')
            GPIO.output(self._pin_enable[self.ValveList.VALVE1], GPIO.LOW)
            GPIO.output(self._pin_enable[self.ValveList.VALVE2], GPIO.LOW)
            GPIO.output(self._pin_enable[self.ValveList.VALVE3], GPIO.LOW)
            GPIO.output(self._pin_enable[self.ValveList.VALVE4], GPIO.HIGH)
        else:
            raise Exception("valve: {} not found in registers".format(valve))

    @property
    def valve_list(self):
        return [v for v in self._valve_list]

    def _check_valve(self, valve):
        if not isinstance(valve, self.ValveList):
            raise Exception("Error, valve_name must be a instance of ValveList")
        if not valve in self.ValveList:
            raise Exception("Error, valve: {} not found in valve_list".format(valve))

    def _check_position(self, pos):
        if not isinstance(pos, int) and not isinstance(pos, float):
            raise Exception("Error, position must be int of float, you passed: {}".format(type(pos)))

    def _normalize_pos(self, pos):
        if pos < self.STEPPER_POS_START:
            pos = self.STEPPER_POS_START

        elif pos > self.STEPPER_POS_END:
            pos = self.STEPPER_POS_END
        return pos

    def get_valve_position(self, valve):
        self._check_valve(valve)

        return self._current_position_pct[valve]

    def _setStep(self, w1, w2, w3, w4):
        GPIO.output(self.coil_A_1_pin, w1)
        GPIO.output(self.coil_B_1_pin, w2)
        GPIO.output(self.coil_A_2_pin, w3)
        GPIO.output(self.coil_B_2_pin, w4)

    def _forwardFullStep(self, steps, valve_name):
        self._enable_motor(valve_name)
        if self._reverse_motor_direction:
            for i in range(steps):
                for j in reversed(range(self.StepCountFullStep)):
                    self._setStep(*self.SeqFullStep[j])
                    time.sleep(self._fullstep_delay)
        else:
            for i in range(steps):
                for j in range(self.StepCountFullStep):
                    self._setStep(*self.SeqFullStep[j])
                    time.sleep(self._fullstep_delay)
        self._motor_off()

    def _backwardsFullStep(self, steps, valve_name):
        self._enable_motor(valve_name)
        if self._reverse_motor_direction:
            for i in range(steps):
                for j in range(self.StepCountFullStep):
                    self._setStep(*self.SeqFullStep[j])
                    time.sleep(self._fullstep_delay)
        else:
            for i in range(steps):
                for j in reversed(range(self.StepCountFullStep)):
                    self._setStep(*self.SeqFullStep[j])
                    time.sleep(self._fullstep_delay)
        self._motor_off()

    def _forward(self, steps, valve_name):
        self._enable_motor(valve_name)
        if self._reverse_motor_direction:
            for i in range(steps):
                for j in reversed(range(self.StepCountHalfStep)):
                    self._setStep(*self.SeqHalfStep[j])
                    time.sleep(self._halfstep_delay)
        else:
            for i in range(steps):
                for j in range(self.StepCountHalfStep):
                    self._setStep(*self.SeqHalfStep[j])
                    time.sleep(self._halfstep_delay)
        self._motor_off()

    def _backwards(self, steps, valve_name):
        self._enable_motor(valve_name)
        if self._reverse_motor_direction:
            for i in range(steps):
                for j in range(self.StepCountHalfStep):
                    self._setStep(*self.SeqHalfStep[j])
                    time.sleep(self._halfstep_delay)
        else:
            for i in range(steps):
                for j in reversed(range(self.StepCountHalfStep)):
                    self._setStep(*self.SeqHalfStep[j])
                    time.sleep(self._halfstep_delay)

        self._motor_off()

    def _motor_off(self):
        self._setStep(*self.off)
        GPIO.output(self._pin_enable[self.ValveList.VALVE1], GPIO.LOW)
        GPIO.output(self._pin_enable[self.ValveList.VALVE2], GPIO.LOW)
        GPIO.output(self._pin_enable[self.ValveList.VALVE3], GPIO.LOW)
        GPIO.output(self._pin_enable[self.ValveList.VALVE4], GPIO.LOW)

    def shutdown(self):
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.coil_A_1_pin, GPIO.OUT)
        GPIO.setup(self.coil_A_2_pin, GPIO.OUT)
        GPIO.setup(self.coil_B_1_pin, GPIO.OUT)
        GPIO.setup(self.coil_B_2_pin, GPIO.OUT)
        self._motor_off()

    # method always runs on exit
    def cleanup(self):
        self._logger.debug("Module steppervalvecontrol - running cleanup")
        GPIO.cleanup()

    def home_motor(self, valve_name):
        self._check_valve(valve_name)

        # reverse for defined number of steps
        self._backwards(self._home_steps, valve_name)

        # update current stepper position
        self._current_position[valve_name] = self._steps_per_full_swing
        self._current_position_pct[valve_name] = self.STEPPER_POS_END

        self._motor_off()

    def move_to_pos_halfstep(self, valve_name, pos):
        self._check_position(pos)

        pos = self._normalize_pos(pos)

        self._check_valve(valve_name)

        absolute_pos = int(pos * module_steppervalvecontrol.STEPS_PER_FULL_SWING / 100)

        if self._current_position[valve_name] == absolute_pos:
            return
        elif self._current_position[valve_name] < absolute_pos:
            steps = absolute_pos - self._current_position[valve_name]
            self._backwards(steps, valve_name)
        else:
            steps = self._current_position[valve_name] - absolute_pos
            self._forward(steps, valve_name)

        self._current_position[valve_name] = absolute_pos
        self._current_position_pct[valve_name] = pos

        return

    def move_to_pos_fullstep(self, valve_name, pos):
        self._check_position(pos)
        pos = self._normalize_pos(pos)

        self._check_valve(valve_name)

        absolute_pos = int(pos * module_steppervalvecontrol.STEPS_PER_FULL_SWING / 100)

        if self._current_position[valve_name] == absolute_pos:
            return
        elif self._current_position[valve_name] < absolute_pos:
            steps = absolute_pos - self._current_position[valve_name]
            self._backwardsFullStep(steps, valve_name)
        else:
            steps = self._current_position[valve_name] - absolute_pos
            self._forwardFullStep(steps, valve_name)

        self._current_position[valve_name] = absolute_pos
        self._current_position_pct[valve_name] = pos

        return


if __name__ == "__main__":
    mycontrol = module_steppervalvecontrol(motor_5v=False, reverse_direction=True)
    valves = module_steppervalvecontrol.ValveList

    while True:
        mycontrol.move_to_pos_fullstep(valves.VALVE1, module_steppervalvecontrol.STEPPER_POS_START)
        mycontrol.move_to_pos_halfstep(valves.VALVE2, module_steppervalvecontrol.STEPPER_POS_START)
        mycontrol.move_to_pos_halfstep(valves.VALVE3, module_steppervalvecontrol.STEPPER_POS_START)
        mycontrol.move_to_pos_halfstep(valves.VALVE4, module_steppervalvecontrol.STEPPER_POS_START)
        time.sleep(1)
        mycontrol.move_to_pos_fullstep(valves.VALVE1, module_steppervalvecontrol.STEPPER_POS_END)
        mycontrol.move_to_pos_halfstep(valves.VALVE2, module_steppervalvecontrol.STEPPER_POS_END)
        mycontrol.move_to_pos_halfstep(valves.VALVE3, module_steppervalvecontrol.STEPPER_POS_END)
        mycontrol.move_to_pos_halfstep(valves.VALVE4, module_steppervalvecontrol.STEPPER_POS_END)
        time.sleep(1)
