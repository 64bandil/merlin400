from abc import ABC, abstractmethod
from hardware.module_HardwareControlSystem import module_HardwareControlSystem

class BaseCommand(ABC):

    @abstractmethod
    def execute(self, hardwareControlSystem: module_HardwareControlSystem):
        pass

    @abstractmethod
    def validate_state(self, hardwareControlSystem: module_HardwareControlSystem):
        pass