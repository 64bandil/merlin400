import atexit
import datetime
import functools
import logging
import os
from logging.handlers import MemoryHandler, RotatingFileHandler

from common.settings import LOGS_DIRECTORY, BATCH_LOGGING


APP_LOGGER_NAME = "drizzle"


def get_app_logger(name):
    return logging.getLogger(APP_LOGGER_NAME + "." + name)


def get_root_logger():
    return logging.getLogger(APP_LOGGER_NAME)


def with_logging_enabled(method):
    """Decorator that enables logging for a decorated class method."""
    @functools.wraps(method)
    def _impl(self, *method_args, **method_kwargs):
        root_logger = get_root_logger()
        logging_level = None
        if root_logger.level != logging.DEBUG:
            logging_level = root_logger.level
            root_logger.setLevel(logging.DEBUG)

        try:
            result = method(self, *method_args, **method_kwargs)
            return result
        finally:
            if logging_level is not None:
                root_logger.setLevel(logging_level)

    return _impl


def flush_logger():
    """Manually flush memory handler."""
    root_logger = get_root_logger()
    for handler in root_logger.handlers:
        if isinstance(handler, MemoryHandler):
            handler.flush()
            break


def setup_logging():
    logs_dir = LOGS_DIRECTORY
    if not logs_dir.exists():
        os.makedirs(str(logs_dir))

    app_logger = logging.getLogger(APP_LOGGER_NAME)
    app_logger.setLevel(logging.DEBUG)
    log_name = str(
        logs_dir / "drizzle_log_{0.year}.txt".format(datetime.datetime.now())
    )

    if os.path.exists(log_name):
        stats = os.stat(log_name)
        # If log file already exists and larger than 500Mb, remove it.
        if stats.st_size > 500 * 1024 * 1024:
            os.remove(log_name)

    # File based logging handler. Log file is rotated when it reaches 50Mb.
    # One backup copy of the previous log is kept on the file system.
    fh = RotatingFileHandler(log_name, mode="a", maxBytes=50*1024*1024, backupCount=1, delay=False)
    fh.setLevel(logging.DEBUG)

    # Stream based logging handler
    sh = logging.StreamHandler()
    sh.setLevel(logging.DEBUG)

    # Buffered handler setup
    mh = MemoryHandler(
        capacity=1000,  # Flush to file every 1000 records.
        flushLevel=logging.ERROR,  # FLush when error level message received.
        target=fh,
    )

    # Log formatter
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(threadName)s - %(levelname)s - %(message)s"
    )
    fh.setFormatter(formatter)
    sh.setFormatter(formatter)
    mh.setFormatter(formatter)

    app_logger.addHandler(sh)

    # Both memory and stream handlers are attached to the app logger
    # Memory handler flushes records to file handler according to the set up.
    if BATCH_LOGGING:
        app_logger.addHandler(mh)
        def flush():
            """When process exits, flush buffer to file handler."""
        mh.flush()

        atexit.register(flush)
    else:
        app_logger.addHandler(fh)
