from common.module_logging import get_app_logger
from hardware.components.module_physicalinterface import module_physicalinterface
from hardware.module_HardwareControlSystem import module_HardwareControlSystem
from hardware.commands.basecommand import BaseCommand

class Command_Reset(BaseCommand):

    def __init__(self):
        self._logger = get_app_logger(str(self.__class__))

    def validate_state(self, hardwareControlSystem: module_HardwareControlSystem):
        return

    def execute(self, hardwareControlSystem: module_HardwareControlSystem):
        self._logger.info("Resetting!!!")
        #self._user_feedback = "Command ok, resetting FSM"
        # flash to indicated actual reset
        hardwareControlSystem._myphysicalinterface.do_reset_flash()
        hardwareControlSystem.set_valve("valve1", 0)
        hardwareControlSystem.set_valve("valve4", 100)
        hardwareControlSystem.set_valve("valve3", 100)
        hardwareControlSystem.set_valve("valve2", 100)

        hardwareControlSystem.bottom_heater_power = 0
        hardwareControlSystem.PID_off()
        hardwareControlSystem.fan_value = 0
        hardwareControlSystem.init_FSM()
        hardwareControlSystem.init_config()
        hardwareControlSystem.light_off()

        #self._reset_request_counter = 0
        #self._select_request_counter = 0
        #self._pause_request_counter = 0
        #self._play_request_counter = 0

        hardwareControlSystem._myphysicalinterface.set_program_and_state(1, module_physicalinterface.DeviceState.READY)
        self._logger.info("Resetting completed.")
