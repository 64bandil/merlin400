from common.module_logging import get_app_logger
from hardware.components.module_physicalinterface import module_physicalinterface
from hardware.module_HardwareControlSystem import module_HardwareControlSystem
from hardware.commands.basecommand import BaseCommand

class Command_StartExtraction(BaseCommand):

    _runFull: bool
    _soakTime: int=None

    def __init__(self, runFull:bool, soakTime: int=None):
        self._logger = get_app_logger(str(self.__class__))
        self._runFull = runFull
        self._soakTime = soakTime

    def validate_state(self, hardwareControlSystem: module_HardwareControlSystem):
        device_is_paused: bool = hardwareControlSystem.FSM.fsmData["pause_flag"]
        device_is_running: bool = hardwareControlSystem.FSM.fsmData["running_flag"]

        if device_is_paused:
            raise Exception("Can not start new program when device is paused.")
        if device_is_running:
            raise Exception("Can not start new program when device is running.")

    def execute(self, hardwareControlSystem: module_HardwareControlSystem):
        if self._soakTime is not None:
            hardwareControlSystem._config["SYSTEM"]["soak_time_seconds"] = str(self._soakTime)
            hardwareControlSystem.store_config()
            self._logger.info("Setting soak time to {} seconds.".format(self._soakTime))

        hardwareControlSystem._myphysicalinterface.set_state(module_physicalinterface.DeviceState.RUNNING_PAUSE_DISABLED)
        if self._runFull:            
            hardwareControlSystem.FSM.SetFSMData("run_full_extraction", 1)
        else:
             hardwareControlSystem.FSM.SetFSMData("run_full_extraction", 0)

        hardwareControlSystem.FSM.SetFSMData("running_flag", True)
        hardwareControlSystem.FSM.SetFSMData("start_flag", True)
