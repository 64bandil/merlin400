if __name__ == "__main__":
  #Add root folder (/src/) to paths for import if this script is run standalone
  from pathlib import Path
  import sys
  sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

import atexit
import enum
import threading
import time

import RPi.GPIO as GPIO
import smbus
from common.module_logging import get_app_logger


class module_physicalinterface:
    """
    Module is for controlling the pluggable physical interface with an I2C port expander and buttons + LED's
    """

    # Default I2C Address
    DEVICE_ADDR = 0x20
    BUS_NUM = 1
    INTERRUPT_PIN = 14

    # REGISTERS
    class Register(int, enum.Enum):
        INPUT_PORT = 0x00
        OUTPUT_PORT = 0x02
        POLARITY_INV_PORT = 0x04
        CONFIGURATION_PORT = 0x06
        OUTPUT_DRIVE_STR0 = 0x040
        OUTPUT_DRIVE_STR1 = 0x042
        INPUT_LATCH = 0x44
        PULL_UP_DOWN_EN = 0x46
        PULL_UP_DOWN_SEL = 0x48
        IRQ_MASK = 0x4A
        IRQ_STATUS = 0x4C
        OUTPUT_PORT_CONF = 0x4F

    class LED(int, enum.Enum):
        GREEN_BLINK = 6
        S1 = 0
        S2 = 1
        S3 = 2
        S4 = 3
        SELECT = 4
        PAUSE = 4
        PLAY_RED = 0
        PLAY_GREEN = 7

    class Port(int, enum.Enum):
        CHASE = 0
        LED_S1 = 1
        LED_S2 = 1
        LED_S3 = 1
        LED_S4 = 1
        LED_SELECT = 0
        LED_PAUSE = 1
        LED_PLAY_RED = 0
        LED_PLAY_GREEN = 0

    # pin definitions for the buttons
    class ButtonPin(int, enum.Enum):
        PLAY = 3
        STOP = 5
        SELECT = 6

    # State definitions
    class DeviceState(enum.Enum):
        BOOTING = 0
        READY = 1
        RUNNING_PAUSE_DISABLED = 2
        RUNNING_PAUSE_ENABLED = 3
        PAUSE = 4
        RESET_WARNING = 5
        RESETTING = 6
        UPDATING = 7
        ERROR = 10
        SENDING_LOGS = 11

    # Blink definitions
    class BlinkType(enum.Enum):
        NONE = 0
        PAUSE = 1
        RUNNING = 2
        ALL = 3


    class ButtonPressed(enum.Enum):
        NONE = 0
        SELECT = 1
        PLAY = 2
        PAUSE = 3
        RESET = 4

    def __init__(self, i2c_dev, chip_adress):
        self._logger = get_app_logger(str(self.__class__))
        self._logger.debug("MODULE module_physicalinterface initializing")

        # Set default values
        self._state = module_physicalinterface.DeviceState.BOOTING
        self._selected_program = 1
        self._error_indicator = [False, False, False, False]

        # variable that contains last button press
        self._last_button_press = module_physicalinterface.ButtonPressed.NONE

        # add cleanup method
        atexit.register(self.cleanup)

        # setup interrupt pin and callback method
        GPIO.setmode(GPIO.BCM)
        self._interrupt_pin = module_physicalinterface.INTERRUPT_PIN
        GPIO.setwarnings(False)
        GPIO.setup(self._interrupt_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.add_event_detect(
            self._interrupt_pin,
            GPIO.FALLING,
            callback=self._my_button_callback,
            bouncetime=300,
        )

        # initialize i2c
        self._i2c_address = chip_adress
        self._i2c_read_address = (self._i2c_address << 1) & 0xFE
        self._i2c_write_address = (self._i2c_address << 1) | 0x01
        self._bus = i2c_dev

        # setup port and pins
        port_config_output_byte_0 = 0x00
        port_config_output_byte_0 |= 1 << module_physicalinterface.ButtonPin.PLAY

        port_config_output_byte_1 = 0x00
        port_config_output_byte_1 |= 1 << module_physicalinterface.ButtonPin.SELECT
        port_config_output_byte_1 |= 1 << module_physicalinterface.ButtonPin.STOP

        port_config_interrupt_mask_0 = 0xFF
        port_config_interrupt_mask_0 &= ~(1 << module_physicalinterface.ButtonPin.PLAY)

        port_config_interrupt_mask_1 = 0xFF
        port_config_interrupt_mask_1 &= ~(
            1 << module_physicalinterface.ButtonPin.SELECT
        )
        port_config_interrupt_mask_1 &= ~(1 << module_physicalinterface.ButtonPin.STOP)

        # Set drive strength
        self._write_i2c(module_physicalinterface.Register.OUTPUT_DRIVE_STR0, 0xFF, 0xFF)
        self._write_i2c(module_physicalinterface.Register.OUTPUT_DRIVE_STR1, 0xFF, 0xFF)

        # Set LED pins to output and buttons to input
        self._write_i2c(
            module_physicalinterface.Register.CONFIGURATION_PORT,
            port_config_output_byte_0,
            port_config_output_byte_1,
        )
        self._write_i2c(
            module_physicalinterface.Register.IRQ_MASK,
            port_config_interrupt_mask_0,
            port_config_interrupt_mask_1,
        )

        # set latch on buttons
        # self._write_i2c(module_physicalinterface.REG_INPUT_LATCH, port_config_output_byte_0, port_config_output_byte_1)
        self._write_i2c(module_physicalinterface.Register.INPUT_LATCH, 0x00, 0x00)

        # initial state of pins
        self._P0_outputstate = 0x00
        self._P1_outputstate = 0x00

        # initialize output on all pins
        self._set_all_pins()

        self._goto_next = False

        # setup data for blinking
        self._blink_state = module_physicalinterface.BlinkType.NONE
        self._blink_period = 0.8
        self._last_blink_period = False

        # check all registers
        self._read_all_registers()

        # set shutdown flag
        self._shutdown = False

        # set _do_physical_update flag
        self._do_physical_update = False

        self._logger.debug("MODULE module_physicalinterface done initializing")

        # start timer for blinking
        self._blink_callback()

        # flag for red light (error state)
        self._red_light = False

    def set_error_indicator(self, LED1=False, LED2=False, LED3=False, LED4=False):
        self._error_indicator = [LED1, LED2, LED3, LED4]

    def is_new_program(self, program):
        return program != self._selected_program

    def set_program_and_state(self, new_program, new_state):
        # Check if the program is new
        if self.is_new_program(new_program):
            self._do_physical_update = True

        self._selected_program = new_program
        self.set_state(new_state)

    # used to set the program from an external module
    def set_program(self, new_program):
        # Check if the program is new
        if self.is_new_program(new_program):
            self._do_physical_update = True

        self._selected_program = new_program

        # update display
        self.set_state(self._state)

    def do_print_label_blink(self):
        for _ in range(0, 15):
            self._button_LED_play_green_on()
            self._set_all_pins()
            time.sleep(0.1)
            self._button_LED_play_green_off()
            self._set_all_pins()
            time.sleep(0.1)

    def do_force_afterstill_blink(self):
        for _ in range(0, 10):
            self._button_LED_select_on()
            self._set_all_pins()
            time.sleep(0.1)
            self._button_LED_select_off()
            self._set_all_pins()
            time.sleep(0.1)

    def do_shutdown_blink(self):
        for _ in range(0, 15):
            self._button_LED_play_red_on()
            self._set_all_pins()
            time.sleep(0.1)
            self._button_LED_play_red_off()
            self._set_all_pins()
            time.sleep(0.1)

    def do_connected_flash(self):
        for _ in range(0, 10):
            time.sleep(0.1)
            self._button_LED_play_green_on()
            self._set_all_pins()
            time.sleep(0.1)
            self._button_LED_play_green_off()
            self._set_all_pins()
        self._button_LED_play_green_on()
        self._set_all_pins()

    def do_disconnected_flash(self):
        self._button_LED_play_green_off()
        for _ in range(0, 10):
            time.sleep(0.1)
            self._button_LED_play_red_on()
            self._set_all_pins()
            time.sleep(0.1)
            self._button_LED_play_red_off()
            self._set_all_pins()
        self._button_LED_play_green_on()
        self._set_all_pins()

    def do_reset_flash(self):
        self._green_555_blink_off()
        self._button_LED_select_off()
        self._button_LED_pause_off()
        self._button_LED_play_green_off()
        self._button_LED_play_red_off()
        self._stop_blinking()
        self._button_LED_pause_off()

        # turn off all program selector LED's
        self._set_program_indicator(False, False, False, False)
        # set the pins over i2c
        self._set_all_pins()
        for _ in range(0, 3):
            time.sleep(0.1)
            self._button_LED_play_red_on()
            self._set_all_pins()
            time.sleep(0.1)
            self._button_LED_play_red_off()
            self._set_all_pins()

    def do_flash_green(self):
        self._set_all_pins()
        for _ in range(0, 20):
            time.sleep(0.5)
            self._button_LED_play_green_on()
            self._set_all_pins()
            time.sleep(0.5)
            self._button_LED_play_green_off()
            self._set_all_pins()

    def do_flash_green_3sec(self):
        self._set_all_pins()
        self._button_LED_play_red_off()
        for _ in range(0, 3):
            time.sleep(0.5)
            self._button_LED_play_green_on()
            self._set_all_pins()
            time.sleep(0.5)
            self._button_LED_play_green_off()
            self._set_all_pins()

    def do_red_light(self):
        self._set_all_pins()
        self._button_LED_play_green_off()
        self._button_LED_play_red_on()
        self._set_all_pins()

    def toggle_reg_light(self):
        if self._red_light:
            self._set_all_pins()
            self._button_LED_play_green_off()
            self._button_LED_play_red_off()
            self._set_all_pins()
            self._red_light = False
        else:
            self._set_all_pins()
            self._button_LED_play_green_off()
            self._button_LED_play_red_on()
            self._set_all_pins()
            self._red_light = True

    def _leds_show_program(self):
        led_state = [False, False, False, False]
        try:
            led_state[self._selected_program - 1] = True
        except IndexError:
            # Ignore situation when program number is not correct and does not correspond to 4 leds.
            pass
        self._set_program_indicator(*led_state)

    def is_new_state(self, state):
        return state != self._state

    def set_state(self, new_state, force_physical_update=False):
        if self.is_new_state(new_state) or force_physical_update:
            self._do_physical_update = True
            self._logger.debug(
                "I need to update the UI, old state was: {} new state is: {}".format(
                    self._state, new_state
                )
            )

        if new_state is module_physicalinterface.DeviceState.BOOTING:
            self._state = new_state
            self._set_program_indicator(False, False, False, False)
            self._green_555_blink_on()
            self._button_LED_select_off()
            self._button_LED_pause_off()
            self._button_LED_play_green_off()
            self._button_LED_play_red_off()
            self._stop_blinking()

        elif new_state is module_physicalinterface.DeviceState.READY:
            self._state = new_state
            self._leds_show_program()
            self._green_555_blink_off()
            self._button_LED_select_on()
            self._button_LED_pause_off()
            self._button_LED_play_green_on()
            self._button_LED_play_red_off()
            self._stop_blinking()

        elif new_state is module_physicalinterface.DeviceState.RUNNING_PAUSE_DISABLED:
            self._state = new_state
            self._leds_show_program()
            self._green_555_blink_off()
            self._button_LED_select_off()
            self._button_LED_pause_off()
            self._button_LED_play_green_off()
            self._button_LED_play_red_off()
            self._running_blink_on()

        elif new_state is module_physicalinterface.DeviceState.RUNNING_PAUSE_ENABLED:
            self._state = new_state
            self._leds_show_program()
            self._green_555_blink_off()
            self._button_LED_select_off()
            self._button_LED_pause_on()
            self._button_LED_play_green_off()
            self._button_LED_play_red_off()
            self._running_blink_on()

        elif new_state is module_physicalinterface.DeviceState.PAUSE:
            # Code program into LEDs that shows current program
            self._state = new_state
            self._leds_show_program()
            self._green_555_blink_off()
            self._button_LED_select_off()
            self._button_LED_pause_off()
            self._button_LED_play_green_on()
            self._button_LED_play_red_off()
            self._pause_blink_on()

        elif new_state is module_physicalinterface.DeviceState.UPDATING:
            # CHANGE TO ALL BLINKS
            self._state = new_state
            self._set_program_indicator(True, True, True, True)
            self._green_555_blink_off()
            self._button_LED_select_off()
            self._button_LED_pause_off()  # CHANGE TO BLINK
            self._button_LED_play_green_off()
            self._button_LED_play_red_off()
            self._all_blink_on()

        elif new_state is module_physicalinterface.DeviceState.RESET_WARNING:
            # CHANGE TO ALL BLINKS
            self._state = new_state
            self._set_program_indicator(True, True, True, True)
            self._green_555_blink_off()
            self._button_LED_select_off()
            self._button_LED_pause_off()  # CHANGE TO BLINK
            self._button_LED_play_green_off()
            self._button_LED_play_red_off()
            self._all_blink_on()

        elif new_state is module_physicalinterface.DeviceState.RESETTING:
            # CHANGE TO ALL BLINKS
            self._state = new_state
            self._set_program_indicator(True, True, True, True)
            self._green_555_blink_off()
            self._button_LED_select_on()
            self._button_LED_pause_on()
            self._button_LED_play_green_off()
            self._button_LED_play_red_on()
            self._stop_blinking()

        elif new_state is module_physicalinterface.DeviceState.ERROR:
            # CHANGE TO ALL BLINKS
            self._state = new_state
            self._set_program_indicator(self._error_indicator[0], self._error_indicator[1], self._error_indicator[2], self._error_indicator[3])
            self._green_555_blink_off()
            self._button_LED_select_off()
            self._button_LED_pause_off()
            self._button_LED_play_green_off()
            self._button_LED_play_red_on()
            self._stop_blinking()

        else:
            raise Exception("Error, unknown state for physical interface")

        if self._do_physical_update:
            self._logger.debug("updating physical IO")
            # set the pins over i2c
            self._set_all_pins()
            self._do_physical_update = False

    @property
    def button_pressed(self):
        the_button_pressed = self._last_button_press
        self._last_button_press = module_physicalinterface.ButtonPressed.NONE
        return the_button_pressed

    def _get_pressed_button(self, irq_status):
        # irq status [8, 0] = select
        # irq status [0, 64] = play
        # irq status [0, 32] = pause
        # irq status [8, 32] = reset
        button_press = module_physicalinterface.ButtonPressed.NONE
        if not irq_status:
            return button_press
        if irq_status[0] == 8 and irq_status[1] == 0:
            # self._logger.debug('Pressed select')
            button_press = module_physicalinterface.ButtonPressed.SELECT
        elif irq_status[0] == 0 and irq_status[1] == 64:
            # self._logger.debug('Pressed play')
            button_press = module_physicalinterface.ButtonPressed.PLAY
        elif irq_status[0] == 0 and irq_status[1] == 32:
            # self._logger.debug('Pressed pause')
            button_press = module_physicalinterface.ButtonPressed.PAUSE
        elif irq_status[0] == 8 and irq_status[1] == 32:
            # self._logger.debug('Pressed pause')
            button_press = module_physicalinterface.ButtonPressed.RESET
        else:
            self._logger.debug("Unknown command")

        return button_press

    @property
    def button_pressed_force(self):
        # used to check what is currently pressed - used in reset situations
        irq_status = self._read_i2c(module_physicalinterface.Register.IRQ_STATUS)
        button_press = self._get_pressed_button(irq_status)
        return button_press

    # Stop blinking using the i2c interface for timing
    def _stop_blinking(self):
        self._blink_state = module_physicalinterface.BlinkType.NONE

    def _blink_callback(self):
        if self._blink_state is module_physicalinterface.BlinkType.RUNNING:
            # print('In BLINK_RUNNING: {}'.format(datetime.datetime.now()))
            if self._last_blink_period:
                # print('In BLINK_RUNNING ON')
                self._last_blink_period = False
                self._button_LED_play_green_on()
            else:
                # print('In BLINK_RUNNING OFF')
                self._last_blink_period = True
                self._button_LED_play_green_off()

            # write to LEDs
            self._set_all_pins()

        elif self._blink_state is module_physicalinterface.BlinkType.PAUSE:
            # print('In BLINK_PAUSE: {}'.format(datetime.datetime.now()))
            if self._last_blink_period:
                # print('In BLINK_PAUSE ON')
                self._last_blink_period = False
                self._button_LED_pause_on()
            else:
                # print('In BLINK_PAUSE OFF')
                self._last_blink_period = True
                self._button_LED_pause_off()

            # write to LEDs
            self._set_all_pins()

        elif self._blink_state is module_physicalinterface.BlinkType.ALL:
            # print('In BLINK_ALL')
            if self._last_blink_period:
                # print('In BLINK_ALL ON')
                self._last_blink_period = False
                self._all_leds_on()
            else:
                # print('In BLINK_ALL OFF')
                self._last_blink_period = True
                self._all_leds_off()
            # write to LEDs
            self._set_all_pins()

        if not self._shutdown:
            # restart next period
            threading.Timer(self._blink_period, self._blink_callback).start()
        else:
            self._logger.debug("Shutting down physical interface")

    # Callback interrupt when the user clicks a button
    def _my_button_callback(self, channel):
        # do a small delay to allow for dual press
        time.sleep(0.1)
        irq_status = self._read_i2c(module_physicalinterface.Register.IRQ_STATUS)
        self._logger.debug("IRQ Status:{}, {}".format(irq_status[0], irq_status[1]))
        self._last_button_press = self._get_pressed_button(irq_status)

    def shutdown(self):
        self._shutdown = True

    def cleanup(self):
        self._logger.debug("Module physicalinterface - running cleanup")
        # Set all ports to input and tristate pins
        self._write_i2c(
            module_physicalinterface.Register.CONFIGURATION_PORT, 0xFF, 0xFF
        )
        # clean up the GPIO usage
        GPIO.cleanup()
        # close smbus
        self._bus.close()

    # Methods to set the LED's of the interface
    # sets the variable that controls the program indicator LED's
    def _set_program_indicator(self, prog1, prog2, prog3, prog4):
        if not all(isinstance(item, bool) for item in (prog1, prog2, prog3, prog4)):
            raise Exception("Error, program variables must all be bool")

        if prog1:
            self._set_pin_high_p1(module_physicalinterface.LED.S4)
        else:
            self._set_pin_low_p1(module_physicalinterface.LED.S4)

        if prog2:
            self._set_pin_high_p1(module_physicalinterface.LED.S3)
        else:
            self._set_pin_low_p1(module_physicalinterface.LED.S3)

        if prog3:
            self._set_pin_high_p1(module_physicalinterface.LED.S2)
        else:
            self._set_pin_low_p1(module_physicalinterface.LED.S2)

        if prog4:
            self._set_pin_high_p1(module_physicalinterface.LED.S1)
        else:
            self._set_pin_low_p1(module_physicalinterface.LED.S1)

    def _pause_blink_on(self):
        self._blink_state = module_physicalinterface.BlinkType.PAUSE

    def _all_blink_on(self):
        self._blink_state = module_physicalinterface.BlinkType.ALL

    def _running_blink_on(self):
        self._blink_state = module_physicalinterface.BlinkType.RUNNING

    def _all_leds_off(self):
        self._green_555_blink_off()
        self._button_LED_select_off()
        self._button_LED_play_red_off()
        self._button_LED_pause_off()
        self._set_program_indicator(False, False, False, False)

    def _all_leds_on(self):
        self._green_555_blink_off()
        self._button_LED_select_on()
        self._button_LED_play_red_on()
        self._button_LED_pause_on()
        self._set_program_indicator(True, True, True, True)

    def _green_555_blink_on(self):
        # print('Turning on chase')
        self._set_pin_high_p0(module_physicalinterface.LED.GREEN_BLINK)

    def _green_555_blink_off(self):
        # print('Turning off chase')
        self._set_pin_low_p0(module_physicalinterface.LED.GREEN_BLINK)

    def _button_LED_select_on(self):
        self._set_pin_high_p0(module_physicalinterface.LED.SELECT)
        # print('Select on')

    def _button_LED_select_off(self):
        self._set_pin_low_p0(module_physicalinterface.LED.SELECT)
        # print('Select off')

    def _button_LED_pause_on(self):
        self._set_pin_high_p1(module_physicalinterface.LED.PAUSE)
        # print('Pause on')

    def _button_LED_pause_off(self):
        self._set_pin_low_p1(module_physicalinterface.LED.PAUSE)
        # print('Pause off')

    def _button_LED_play_green_on(self):
        self._set_pin_high_p0(module_physicalinterface.LED.PLAY_GREEN)
        # print('Pause on')

    def _button_LED_play_green_off(self):
        self._set_pin_low_p0(module_physicalinterface.LED.PLAY_GREEN)
        # print('Pause off')

    def _button_LED_play_red_on(self):
        self._set_pin_high_p0(module_physicalinterface.LED.PLAY_RED)
        # print('Pause on')

    def _button_LED_play_red_off(self):
        self._set_pin_low_p0(module_physicalinterface.LED.PLAY_RED)
        # print('Pause off')

    def _read_all_registers(self):
        self._read_i2c(module_physicalinterface.Register.INPUT_PORT)
        self._read_i2c(module_physicalinterface.Register.OUTPUT_PORT)
        self._read_i2c(module_physicalinterface.Register.POLARITY_INV_PORT)
        self._read_i2c(module_physicalinterface.Register.CONFIGURATION_PORT)
        self._read_i2c(module_physicalinterface.Register.OUTPUT_DRIVE_STR0)
        self._read_i2c(module_physicalinterface.Register.OUTPUT_DRIVE_STR1)
        self._read_i2c(module_physicalinterface.Register.INPUT_LATCH)
        self._read_i2c(module_physicalinterface.Register.PULL_UP_DOWN_EN)
        self._read_i2c(module_physicalinterface.Register.PULL_UP_DOWN_SEL)
        self._read_i2c(module_physicalinterface.Register.IRQ_MASK)
        self._read_i2c(module_physicalinterface.Register.IRQ_STATUS)
        self._read_i2c(module_physicalinterface.Register.OUTPUT_PORT_CONF)

    def _read_i2c(self, command_byte, print_debug=False):
        try:
            # Read two bytes from the PCAL6416 IC register
            read_data = self._bus.read_i2c_block_data(self._i2c_address, command_byte, 2)

            if print_debug:
                self._logger.debug("reg: 0x{:02x}: 0x{:02x}".format(command_byte, read_data[0]))
                self._logger.debug("reg: 0x{:02x}: 0x{:02x}".format(command_byte + 1, read_data[1]))
            return read_data
        except OSError as err:
            self._logger.error("Failed to read i2c data: {}".format(str(err)))
            return None

    def _write_i2c(self, command_byte, data0, data1, print_debug=False):
        # merge databytes in list
        data = [data0, data1]
        # write to i2cbus
        self._bus.write_i2c_block_data(self._i2c_address, command_byte, data)

        if print_debug:
            self._logger.debug(
                "Wrote: {:02x}, {:02x} with command: {:02x} to address: {:02x}".format(
                    data0, data1, command_byte, self._i2c_write_address
                )
            )

    def _set_all_pins(self, debug_print=False):
        # sets the pin state according to the internal variable
        self._write_i2c(
            module_physicalinterface.Register.OUTPUT_PORT,
            self._P0_outputstate,
            self._P1_outputstate,
        )

        if debug_print:
            self._logger.debug(
                "P0: {:02x}, P1: {:02x}".format(
                    self._P0_outputstate, self._P1_outputstate
                )
            )
            self._logger.debug(
                "P0: {:08b}, P1: {:08b}".format(
                    self._P0_outputstate, self._P1_outputstate
                )
            )

    def _set_all_pins_high(self):
        self._write_i2c(module_physicalinterface.Register.OUTPUT_PORT, 0xFF, 0xFF)

    def _set_all_pins_low(self):
        self._write_i2c(module_physicalinterface.Register.OUTPUT_PORT, 0x00, 0x00)

    def _set_pin_high_p0(self, pin_number, debug_print=False):
        self._P0_outputstate |= 1 << pin_number

    def _set_pin_high_p1(self, pin_number, debug_print=False):
        self._P1_outputstate |= 1 << pin_number

    def _set_pin_low_p0(self, pin_number, debug_print=False):
        self._P0_outputstate &= ~(1 << pin_number)

    def _set_pin_low_p1(self, pin_number, debug_print=False):
        self._P1_outputstate &= ~(1 << pin_number)


def main():
    """
    Main is simply used for unit testing. module_relaycontrol is designed as a driver for other modules.
    """
    _bus = smbus.SMBus(module_physicalinterface.BUS_NUM)
    _i2c_address = module_physicalinterface.DEVICE_ADDR

    myphysicalinterface = module_physicalinterface(_bus, _i2c_address)

    while True:
        print("Setting state BOOTING")
        myphysicalinterface.set_program(1)
        myphysicalinterface.set_state(module_physicalinterface.DeviceState.BOOTING)
        time.sleep(1)
        print("Setting state READY program 1")
        myphysicalinterface.set_program(1)
        myphysicalinterface.set_state(module_physicalinterface.DeviceState.READY)
        time.sleep(1)
        print("Setting state READY program 2")
        myphysicalinterface.set_program(2)
        myphysicalinterface.set_state(module_physicalinterface.DeviceState.READY)
        time.sleep(1)
        print("Setting state READY program 3")
        myphysicalinterface.set_program(3)
        myphysicalinterface.set_state(module_physicalinterface.DeviceState.READY)
        time.sleep(1)
        print("Setting state READY program 4")
        myphysicalinterface.set_program(4)
        myphysicalinterface.set_state(module_physicalinterface.DeviceState.READY)
        time.sleep(1)
        print("Setting state RUNNING without pause")
        myphysicalinterface.set_program(1)
        myphysicalinterface.set_state(
            module_physicalinterface.DeviceState.RUNNING_PAUSE_DISABLED
        )
        time.sleep(4)
        print("Setting state RUNNING with pause")
        myphysicalinterface.set_program(1)
        myphysicalinterface.set_state(
            module_physicalinterface.DeviceState.RUNNING_PAUSE_ENABLED
        )
        time.sleep(4)
        print("Setting state PAUSE")
        myphysicalinterface.set_program(1)
        myphysicalinterface.set_state(module_physicalinterface.DeviceState.PAUSE)
        time.sleep(4)
        print("Setting state RESET WARNING")
        myphysicalinterface.set_program(1)
        myphysicalinterface.set_state(
            module_physicalinterface.DeviceState.RESET_WARNING
        )
        time.sleep(4)
        print("Setting state RESETTING")
        myphysicalinterface.set_program(1)
        myphysicalinterface.set_state(module_physicalinterface.DeviceState.RESETTING)
        time.sleep(4)
        print("Setting state ERROR")
        myphysicalinterface.set_program(1)
        myphysicalinterface.set_state(module_physicalinterface.DeviceState.ERROR)
        time.sleep(4)


if __name__ == "__main__":
    main()
