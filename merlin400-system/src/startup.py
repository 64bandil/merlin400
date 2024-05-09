import signal
import threading
import time
import sys
from common import utils
from common.module_logging import setup_logging, get_app_logger
from common.settings import HEARTBEAT_TIMEOUT_SECONDS
import system_setup
from controlthread import ControlThread
from webserver import ServerThread

class Startup:
    """
    Application starts here
    """
    _controlThread: ControlThread = None
    _webServerThread: ServerThread = None
    _isRunning = True

    def main(self):
        setup_logging()
        self._logger = get_app_logger(str(self.__class__))
        self._logger.info("Starting Drizzle Merlin400 control...")

        #check for sd card move
        system_setup.update_sd_card_data()

        if not utils.is_root():
            print("Error, run application as sudo")
            exit(1)

        #Register our handler for shutdown signals (CTRL+C, kill, etc.)
        for signal_name in (
            signal.SIGHUP,
            signal.SIGINT,
            signal.SIGABRT,
            signal.SIGTERM,
            signal.SIGSEGV,
        ):
            signal.signal(signal_name, self.shutdown_handler)

        self._heartbeat = threading.Event()
        self._last_heartbeat = time.time()

        #Start control and webserver threads as Daemons (background threads that will exit when main thread exits)
        self._logger.debug("Initializing ControlThread")
        self._controlThread = ControlThread("ControlThread", self._heartbeat, daemon=True)
        self._logger.debug("Starting ControlThread")
        self._controlThread.start()

        self._logger.debug("Intializing WebServerThread")
        self._webServerThread = ServerThread(self._controlThread, daemon=True)
        self._logger.debug("Starting WebServerThread")
        self._webServerThread.start()
       
        self._logger.debug("Starting application main loop")
        while self._isRunning:
            try:            
                # Check if child thread is still running.
                # Additional heartbeat check to make sure controlThread is running.
                _seconds_since_last_heartbeat = time.time() - self._last_heartbeat
                if not self._heartbeat.is_set():
                    self._logger.warning("Heartbeat not set... Time passed: {:.02f}".format(_seconds_since_last_heartbeat))
                else:
                    self._last_heartbeat = time.time()
                    _seconds_since_last_heartbeat = time.time() - self._last_heartbeat

                if (_seconds_since_last_heartbeat > HEARTBEAT_TIMEOUT_SECONDS and not self._heartbeat.is_set()) or not self._controlThread.is_alive():
                    self._logger.error("ControlThread is dead. Exiting application.")
                    self.shutdown_handler()
                    #self._logger.error("ControlThread is dead. Restarting device...")
                    #utils.reboot()

                if self._controlThread._running:
                    self._heartbeat.clear()

                if self._isRunning:
                    time.sleep(0.5)

            except Exception as error:
                self._logger.exception("Unexpected exception in application main loop: {!r}".format(error))
        
    def shutdown_handler(self, *args):
        self._logger.debug("Handling shutdown signal...")
        self._isRunning=False

        if(self._controlThread!=None):
            self._logger.debug("Stopping ControlThread - Calling stop()")
            self._controlThread.stop()
            self._logger.debug("Stopping ControlThread - Calling join()")
            self._controlThread.join()
            self._controlThread=None
            self._logger.debug("ControlThread is stopped.")

        if(self._webServerThread!=None):
            self._logger.debug("Stopping WebServerThread - Calling stop()")
            self._webServerThread.stop()
            self._logger.debug("Stopping WebServerThread - Calling join()")
            self._webServerThread.join()
            self._webServerThread=None
            self._logger.debug("WebServerThread is stopped.")

        sys.exit(0)

if __name__ == "__main__":
    program = Startup()
    program.main()
