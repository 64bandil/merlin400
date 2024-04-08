import atexit
import signal
import threading
import time

from common import utils
from common.module_logging import setup_logging, get_app_logger
import system_setup

from controlthread import ControlThread
import webserver


class Startup:
    """
    Application starts here
    """
    def main(self):
        setup_logging()

        self._logger = get_app_logger(str(self.__class__))
        self._logger .info("Starting drizzle control v1.1")

        #check for sd card move
        system_setup.update_sd_card_data()

        # check for sudo access
        if not utils.is_root():
            print("Error, run application as sudo")
            exit(1)

        self._heartbeet = threading.Event()
        self._last_heartbeet = time.time()
        atexit.register(self.shutdown)
        for signal_name in (
            signal.SIGHUP,
            signal.SIGABRT,
            signal.SIGHUP,
            signal.SIGTERM,
            signal.SIGSEGV,
        ):
            signal.signal(signal_name, self.shutdown_handler)

        self._logger.debug("Initializing ControlThread object")
        self.myControlObject = ControlThread("ControlThread", self._heartbeet, daemon=True)
        self._logger.debug("Starting ControlThread")
        self.myControlObject.start()
        self._logger.debug("ControlThread started")

        self._logger.debug("Intializing and starting WebServer")
        webserver.start_server(self.myControlObject)


    def shutdown(self):
        self._logger.debug("Control Thread - executing shutdown sequence...")
        self._logger.debug("Closing server...")

        self._logger.debug("Server closed")

    def shutdown_handler(self, *args):
        self._logger.debug("Handling shutdown signal...")
        self._logger.debug("Control Thread - handling shutdown signal...")
        if hasattr(self, "myControlObject"):
            # try:
            #     # TODO: replace this.
            #     #self.myControlObject.iot_client.disconnect()
            # except Exception:
            #     # Ignore disconnect error as we're shutting down anyway.
            #     pass
            self.myControlObject.stop()
            self.myControlObject.join()
        self.shutdown()
        self._logger.debug("Control Thread - handling shutdown signal - exiting app.")
        exit(0)

if __name__ == "__main__":
    program = Startup()
    program.main()
