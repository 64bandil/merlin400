import datetime
import gzip
import os
import shutil
import sqlite3
import subprocess
import threading
import queue
import socket
from pathlib import Path

import boto3
import requests
from boto3.s3.transfer import S3Transfer, S3UploadFailedError
from getmac import get_mac_address
from zipfile import ZipFile
from common.module_logging import get_app_logger

import common.settings

CPU_SERIAL_BLANK = "0000000000000000"
CPU_SERIAL_ERROR = "ERROR000000000"
UPDATE_DIR = Path.cwd()
DEVICE_V1 = "v1"
DEVICE_V2 = "v2"
AP_CONF_FILE = "/etc/hostapd/hostapd.conf"


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


def getserial():
    # Extract serial from cpuinfo file
    _cpuserial = CPU_SERIAL_BLANK
    try:
        f = open("/proc/cpuinfo", "r")
        for line in f:
            if line[0:6] == "Serial":
                _cpuserial = line[10:26]
        f.close()
    except Exception:
        _cpuserial = CPU_SERIAL_ERROR

    return _cpuserial


def get_raspi_id():
  # Extract serial from cpuinfo file
  raspi_id = "m000000"
  try:
    f = open('/proc/cpuinfo','r')
    for line in f:
      if line[0:6]=='Serial':
        raspi_id = 'm' + line[20:26]
    f.close()
  except:
    raspi_id = "m_ERROR"

  return raspi_id


def get_password():
  # return "".join(map(lambda it: chr(int(it, 16)%25+97), get_mac_address(interface="wlan0").split(':')))
  #fetch mac id
  mac_add_array = get_mac_address(interface="wlan0").split(':')

  #convert to integer
  int_list = []
  for entry in mac_add_array:
     int_list.append(int(entry,16))

  mypassword = ''
  for entry in int_list:
    nextletter = (entry%25)+97
    mypassword  += chr(nextletter)

  #append two characters to the password, to reach minimum of eight characters
  nextletter = ((int_list[0]+3)%25)+97
  mypassword  += chr(nextletter)
  nextletter = ((int_list[0]+9)%25)+97
  mypassword  += chr(nextletter)

  return mypassword



def get_unique_id_v1():
    return getserial() + '_' + get_mac_address(interface="wlan0")


def get_unique_id_v2():
    return get_raspi_id() + '_' + get_password()

def get_unique_id():
    print('Unique ID type: {}'.format(get_device_version()))
    return get_unique_id_v1() if get_device_version() == DEVICE_V1 else get_unique_id_v2()


def get_device_version():
    lines = open(AP_CONF_FILE).readlines()
    for line in lines:
        if line.startswith("ssid"):
            clean_string = line.strip().split("=")[-1]
            return DEVICE_V2 if (len(clean_string) == 7 and clean_string[0] == 'm') else DEVICE_V1
    return DEVICE_V1


def update():
    print("Starting update process...")
    update_path = str(UPDATE_DIR / "update.sh")
    subprocess_args = [update_path]
    env = dict(
        os.environ,
        AWS_ACCESS_KEY_ID=settings.AWS.AWS_ACCESS_KEY,
        AWS_SECRET_ACCESS_KEY=settings.AWS.AWS_SECRET_KEY,
    )
    error = None
    try:
        p = subprocess.Popen(
            subprocess_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
        )
        output, err = p.communicate()
        print("Update log:\n", output.decode("utf-8"))
        if p.returncode != 0:
            error = err
        else:
            print("Rebooting system...")
            subprocess.run(["reboot"], shell=True, check=True)
    except Exception as e:
        print("Failed running update process", e)
        error = str(e)

    if error:
        raise UpdateError(error)

    print("Update process finished.")


def reboot():
    print("Rebooting system...")
    subprocess.run(["reboot"], shell=True, check=True)


def get_temporary_aws_credentials(thing_name):
    """Allows to fetch temporary aws credentials with credentials provider workflow."""
    creds_provider = settings.AWS.CREDENTIALS_PROVIDER_ENDPOINT
    credential_provider_endpoint = (
        "https://{}/role-aliases/iot-s3-access-role-alias/credentials".format(
            creds_provider
        )
    )
    resp = requests.get(
        credential_provider_endpoint,
        headers={"x-amzn-iot-thingname": thing_name},
        cert=(settings.AWS.CERTIFICATE, settings.AWS.PRIVATE_CERT),
    )
    if not resp.ok:
        print("Failed to retrieve aws credentials:", resp.text)
        return None, None, None

    credentials = resp.json()
    access_key = credentials["credentials"]["accessKeyId"]
    secret = credentials["credentials"]["secretAccessKey"]
    session_token = credentials["credentials"]["sessionToken"]
    return access_key, secret, session_token


def upload_log_file_to_s3(file_path):
    """Uploads log file specified by file_path to s3 bucket provided in settings."""
    # TODO: following requires more time to investigate how to properly set up permissions
    # access_key, secret_key, token = get_temporary_aws_credentials(thing_name)
    # if not access_key:
    #     print("Cant upload logs, failed to get aws credentials.")

    destination_bucket = settings.AWS.LOGS_BUCKET
    s3_key = os.path.basename(file_path)
    s3_client = boto3.client(
        "s3",
        aws_access_key_id=settings.AWS.AWS_ACCESS_KEY,
        aws_secret_access_key=settings.AWS.AWS_SECRET_KEY,
    )
    transfer = S3Transfer(s3_client)
    try:
        transfer.upload_file(file_path, destination_bucket, s3_key)
    except S3UploadFailedError as error:
        print("Failed to upload file:", str(error))


def reverse_reader(filename, buf_size=8192):
    """A generator that returns the lines of a file in reverse order.

    Borrowed from SO answer: https://stackoverflow.com/a/23646049.
    """
    with open(filename) as fh:
        segment = None
        offset = 0
        fh.seek(0, os.SEEK_END)
        file_size = remaining_size = fh.tell()
        while remaining_size > 0:
            offset = min(file_size, offset + buf_size)
            fh.seek(file_size - offset)
            buffer = fh.read(min(remaining_size, buf_size))
            remaining_size -= buf_size
            lines = buffer.split("\n")
            # The first line of the buffer is probably not a complete line so
            # we'll save it and append it to the last line of the next buffer
            # we read
            if segment is not None:
                # If the previous chunk starts right from the beginning of line
                # do not concat the segment to the last line of new chunk.
                # Instead, yield the segment first
                if buffer[-1] != "\n":
                    lines[-1] += segment
                else:
                    yield segment
            segment = lines[0]
            for index in range(len(lines) - 1, 0, -1):
                if lines[index]:
                    yield lines[index]
        # Don't yield None if the file was empty
        if segment is not None:
            yield segment


def filter_log_data(input_log_file_paths, output_file_obj, start_date, end_date, reverse=True):
    """Filter input_log_file_path file records and extract lines that have log date between start_date and end_date.
    Result records are stored as output_log_file_path."""

    def _extract_date(line):
        chunks = [item.strip() for item in line.split()]
        try:
            return datetime.datetime.strptime(chunks[0], "%Y-%m-%d").date()
        except (IndexError, ValueError, TypeError):
            pass

    with output_file_obj:

        for input_file_path in input_log_file_paths:
            if os.path.isfile(input_file_path):
                reader = reverse_reader if reverse else open
                ifp = reader(input_file_path)

                for line in ifp:
                    log_date = _extract_date(line)
                    if log_date:
                        if log_date >= start_date and log_date <= end_date:
                            output_file_obj.write(line + ("\n" if not line.endswith("\n") else ""))
                        # Finish reading file if we found log line that has date earlier start date.
                        if log_date < start_date:
                            if reverse:
                                break
                            else:
                                continue

                    else:
                        output_file_obj.write(line + ("\n" if not line.endswith("\n") else ""))


def compress_file(input_file_path, output_file_path):
    with open(input_file_path, "rb") as ifp:
        with gzip.open(output_file_path, "wb") as ofp:
            shutil.copyfileobj(ifp, ofp)


def get_runtime_data():
    with sqlite3.connect("stats.db") as conn:
        query = """
        select
            strftime('%Y', DATE(ts, 'unixepoch')) as yearof,
            strftime('%W', DATE(ts, 'unixepoch')) as weekof,
            sum(value) as run_time
        from stats_log
        group by yearof, weekof;
        """
        cur = conn.execute(query)
        result = cur.fetchall()
        return "\n".join([", ".join([str(it) for it in row]) for row in result])


def upload_log_files(user_initials = "", problem="", compress=True, callback=None, reverse_result=True):
    threading.Thread(target=_upload_log_files, args=(user_initials, problem, compress, callback, reverse_result)).start()


@retry(3)
def _upload_log_files(user_initials = "", problem="", compress=True, callback=None, reverse_result=True):
    """Execute logs processing logic.

    This code filters existing logs for records that were added during last 7 days and creates a new file with
    recent logs. This new file has device id in its name and is uploaded to s3 bucket.
    :param reverse_result: write result file in reversed order if set to True.
    """
    user_initials_prefix = "user_{}_".format(user_initials) if user_initials else ""
    thing_name = "raspi_" + get_unique_id()
    zip_archive = str(settings.LOGS_DIRECTORY / "{}_{}data_upload_{}.zip".format(
        datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S"), user_initials_prefix, thing_name
    ))
    problem_file_name = str(settings.LOGS_DIRECTORY / "{}problem_description.txt".format(user_initials_prefix))
    with open(problem_file_name, "w") as fp:
        fp.write(problem)
    now = datetime.datetime.utcnow()
    end_date = now.date()
    start_date = end_date - datetime.timedelta(days=30)
    input_file_paths = list(
        set(
            [
                str(settings.LOGS_DIRECTORY / "drizzle_log_{0.year}.txt.1".format(start_date)),
                str(settings.LOGS_DIRECTORY / "drizzle_log_{0.year}.txt".format(start_date)),
                str(settings.LOGS_DIRECTORY / "drizzle_log_{0.year}.txt.1".format(end_date)),
                str(settings.LOGS_DIRECTORY / "drizzle_log_{0.year}.txt".format(end_date)),
            ]
        )
    )
    output_file_path = str(
        settings.LOGS_DIRECTORY / "{}{}_{}.txt".format(
            user_initials_prefix, thing_name, int(now.strftime("%Y%m%d%H%M%S"))
        )
    )

    if compress:
        output_file_path += ".gz"
        output_file_obj = gzip.open(output_file_path, "wt")
    else:
        output_file_obj = open(output_file_path, "w")

    filter_log_data(input_file_paths, output_file_obj, start_date, end_date, reverse=reverse_result)

    # compress and upload syslog
    syslog_compressed = "/tmp/{}{}_syslog_{}.txt.gz".format(
        user_initials_prefix, thing_name, now.strftime("%Y%m%d%H%M%S")
    )
    compress_file("/var/log/syslog", syslog_compressed)

    # create and upload runtime log
    runtime_data = get_runtime_data()
    runtime_file_path = str(
        settings.LOGS_DIRECTORY / "{}{}_runtime_{}.txt.gz".format(
            user_initials_prefix, thing_name, int(now.strftime("%Y%m%d%H%M%S"))
        )
    )
    with gzip.open(runtime_file_path, "wt") as rf:
        header = "year, week, run_time_minutes\n"
        rf.write(header)
        rf.write(runtime_data)

    with ZipFile(zip_archive, "w") as zfp:
        zfp.write(problem_file_name, os.path.basename(problem_file_name))
        zfp.write(output_file_path, os.path.basename(output_file_path))
        zfp.write(syslog_compressed, os.path.basename(syslog_compressed))
        zfp.write(runtime_file_path, os.path.basename(runtime_file_path))

    upload_log_file_to_s3(zip_archive)

    if callback is not None:
        callback()

    try:
        os.remove(problem_file_name)
        os.remove(output_file_path)
        os.remove(syslog_compressed)
        os.remove(runtime_file_path)
    except Exception:
        pass


def shutdown_raspi():
    print("Shutting down system...")
    subprocess.run(["shutdown --poweroff now"], shell=True, check=True)


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

def get_ap_name_from_sdcard():
  result = ''
  with open('/etc/hostapd/hostapd.conf', 'r') as fp:
    for line in fp:
      # search string
      if 'ssid' in line:
        result = line.split('=')[1].strip()
        # don't look for next lines
        break
  return result

def get_password_from_sd_card():
  result = ''
  with open('/etc/hostapd/hostapd.conf', 'r') as fp:
    for line in fp:
      # search string
      if 'wpa_passphrase' in line:
        result = line.split('=')[1].strip()
        # don't look for next lines
        break
  return result

#returns True if the SD card was moved to a new raspberry pi
def has_sd_card_moved():
    _logger = get_app_logger("SD Card Move Check")
    _logger.debug('Hostname from SD card is: {}'.format(socket.gethostname()))
    _logger.debug('Hostname from CPU is: {}'.format(get_raspi_id()))

    #check for correct version of software on board
    if len(socket.gethostname()) != 7:
        _logger.debug('Invalid hostname length, skipping test')
        return False
    elif socket.gethostname()[0] != 'm':
        _logger.debug('Invalid hostname format, skipping test')
        return False

    #check if the SD card was moved to another raspberry pi
    if get_raspi_id() == socket.gethostname():
        print('SD card not changed')
        return False
    else:
        print('SD card was moved')
        return True
  
#modifies the password for the access point to match the MAC ID
def update_ap_password():
  fin = open('/etc/hostapd/hostapd.conf', 'rt')
  data = fin.read()
  data = data.replace(get_password_from_sd_card(), get_password())
  fin.close()
  fin = open('/etc/hostapd/hostapd.conf', "wt")
  fin.write(data)
  fin.close()
  print('ap password updated')

#modifies the SSID for the access point to match the serial number
def update_ap_ssid():
  fin = open('/etc/hostapd/hostapd.conf', 'rt')
  data = fin.read()
  data = data.replace(socket.gethostname(), get_raspi_id())
  fin.close()
  fin = open('/etc/hostapd/hostapd.conf', "wt")
  fin.write(data)
  fin.close()
  print('ap ssid updated')

#changes the system password for the user 'pi'
def update_ssh_password():
   #change ssh password
   ssh_password_command = 'echo "pi:{}" | sudo chpasswd'.format(get_password())
   os.system(ssh_password_command)

#changes hosname of raspberry to match the internal serial number
def update_hostname():
   shell_command = 'raspi-config nonint do_hostname {}'.format(get_raspi_id())
   os.system(shell_command)

def update_sd_card_data():
    _logger = get_app_logger("SD Card Move Check")
    _logger.debug("Starting check of SD card move")

    if has_sd_card_moved() == True:
        _logger.debug("SD Card was moved")
        update_ap_ssid()
        _logger.debug("AP SSID updated")
        update_ap_password()
        _logger.debug("AP password updated")
        update_ssh_password()
        _logger.debug("SSD Password updated")
        update_hostname()
        _logger.debug("Hostname updated")
        reboot()
    else:
        _logger.debug('SD Card was not moved, skipping update')