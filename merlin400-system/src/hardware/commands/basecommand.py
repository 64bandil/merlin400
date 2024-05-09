from abc import ABC, abstractmethod
from hardware.module_HardwareControlSystem import module_HardwareControlSystem

class BaseCommand(ABC):

    @abstractmethod
    def execute(self, hardwareControlSystem: module_HardwareControlSystem):
        pass

    @abstractmethod
    #Is invoked on the thread that schedules the command, when the command is scheduled
    #Is also invoked on control thread before execute is called
    def validate_state(self, hardwareControlSystem: module_HardwareControlSystem):
        pass