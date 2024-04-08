from common.module_logging import get_app_logger
from hardware.components.module_physicalinterface import module_physicalinterface
from hardware.module_HardwareControlSystem import module_HardwareControlSystem
from hardware.commands.basecommand import BaseCommand

class Command_StartDistill(BaseCommand):

    def __init__(self):
        self._logger = get_app_logger(str(self.__class__))

    def validate_state(self, hardwareControlSystem: module_HardwareControlSystem):
        device_is_paused: bool = hardwareControlSystem.FSM.fsmData["pause_flag"]
        device_is_running: bool = hardwareControlSystem.FSM.fsmData["running_flag"]

        if device_is_paused:
            raise Exception("Can not start new program when device is paused.")
        if device_is_running:
            raise Exception("Can not start new program when device is running.")

    def execute(self, hardwareControlSystem: module_HardwareControlSystem):
        #self._user_feedback = "Command ok, distilling"
        #self.__reset_session_counter()
        hardwareControlSystem._myphysicalinterface.set_state(module_physicalinterface.DeviceState.RUNNING_PAUSE_ENABLED)
        hardwareControlSystem.FSM.SetFSMData("running_flag", True)
        hardwareControlSystem.FSM.SetFSMData("run_full_extraction", 0)
        hardwareControlSystem.FSM.ToTransistion("toStateDistillBulk")
