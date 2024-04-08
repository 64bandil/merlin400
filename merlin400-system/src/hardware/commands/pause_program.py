from common.module_logging import get_app_logger
from hardware.components.module_physicalinterface import module_physicalinterface
from hardware.module_HardwareControlSystem import module_HardwareControlSystem
from hardware.commands.basecommand import BaseCommand

class Command_PauseProgram(BaseCommand):

    def __init__(self):
        self._logger = get_app_logger(str(self.__class__))

    def validate_state(self, hardwareControlSystem: module_HardwareControlSystem):
        # may only run if state is DistillBulk, otherwise just ignore or is in pause mode
        if hardwareControlSystem.FSM.curHandle != "DistillBulk":
            raise Exception("Machine is in wrong mode, ignore pause request")

    def execute(self, hardwareControlSystem: module_HardwareControlSystem):
        self._logger.debug("User pressed pause from app")
        hardwareControlSystem.FSM.SetFSMData("pause_flag", True)
        hardwareControlSystem._myphysicalinterface.set_state(module_physicalinterface.DeviceState.PAUSE)
        hardwareControlSystem.FSM.machine.set_PID_target(0)
        hardwareControlSystem.FSM.machine.pump_value = 0
