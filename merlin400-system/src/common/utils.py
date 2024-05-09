import gzip
import os
import shutil
import threading
import queue
import subprocess

class UpdateError(Exception):
    """Raise when there is a problem with update process."""


def retry(times, exceptions=(Exception,)):
    """Retry function if it raised an exception."""
    def _wrapper(func):
        def _inner(*args, **kwargs):
            attempt = 0
            while attempt < times:
                try:
                    return func(*args, **kwargs)
                except exceptions:
                    print("Exception when executing %s, attempt %d of %d" % (func, attempt, times))
                    attempt += 1
            return func(*args, **kwargs)
        return _inner
    return _wrapper


def is_root():
    return os.geteuid() == 0

def reboot():
    print("Rebooting system...")
    subprocess.run(["reboot"], shell=True, check=True)

def compress_file(input_file_path, output_file_path):
    with open(input_file_path, "rb") as ifp:
        with gzip.open(output_file_path, "wb") as ofp:
            shutil.copyfileobj(ifp, ofp)


def stop_after(timeout):
    def wrapper(f):
        def _wrapper(*args, **kwargs):

            def _func_wrapper():
                res = f(*args, **kwargs)
                q.put(res)

            q = queue.Queue()
            threading.Thread(target=_func_wrapper).start()
            try:
                result = q.get(timeout=timeout)
            except queue.Empty:
                result = None
            return result
        return _wrapper
    return wrapper
