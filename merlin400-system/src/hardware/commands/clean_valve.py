from common.module_logging import get_app_logger
from hardware.components.module_physicalinterface import module_physicalinterface
from hardware.module_HardwareControlSystem import module_HardwareControlSystem
from hardware.commands.basecommand import BaseCommand

class Command_CleanValve(BaseCommand):

    def __init__(self, valveNumber: int):
        if valveNumber<1 or valveNumber>4:
            raise Exception("Valvenumber must be between 1 and 4")
        
        self._logger = get_app_logger(str(self.__class__))
        self._valveString = "valve" + valveNumber

    def validate_state(self, hardwareControlSystem: module_HardwareControlSystem):
        pass

    def execute(self, hardwareControlSystem: module_HardwareControlSystem):
        self._logger.info("Setting valves positions to clean " + self._valveString)
        hardwareControlSystem.set_valve("valve1", 0)
        hardwareControlSystem.set_valve("valve2", 0)
        hardwareControlSystem.set_valve("valve3", 0)
        hardwareControlSystem.set_valve("valve4", 0)
        hardwareControlSystem.set_valve(self._valveString, 100)

