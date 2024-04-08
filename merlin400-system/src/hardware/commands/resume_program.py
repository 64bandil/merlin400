from common.module_logging import get_app_logger
from hardware.components.module_physicalinterface import module_physicalinterface
from hardware.module_HardwareControlSystem import module_HardwareControlSystem
from hardware.commands.basecommand import BaseCommand

class Command_ResumeProgram(BaseCommand):

    def __init__(self):
        self._logger = get_app_logger(str(self.__class__))

    def validate_state(self, hardwareControlSystem: module_HardwareControlSystem):
        # may only run if state is DistillBulk, otherwise just ignore or is in pause mode
        if hardwareControlSystem.FSM.curHandle != "DistillBulk":
            raise Exception("Machine is in wrong mode, ignoring resume request")


    def execute(self, hardwareControlSystem: module_HardwareControlSystem):
        hardwareControlSystem.FSM.SetFSMData("pause_flag", False)
        hardwareControlSystem._myphysicalinterface.set_state(module_physicalinterface.DeviceState.RUNNING_PAUSE_ENABLED)
