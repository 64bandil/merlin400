import os
import subprocess
import socket
from pathlib import Path

from getmac import get_mac_address
from common.module_logging import get_app_logger

CPU_SERIAL_BLANK = "0000000000000000"
CPU_SERIAL_ERROR = "ERROR000000000"
UPDATE_DIR = Path.cwd()
DEVICE_V1 = "v1"
DEVICE_V2 = "v2"
AP_CONF_FILE = "/etc/hostapd/hostapd.conf"

def reboot():
    print("Rebooting system...")
    subprocess.run(["reboot"], shell=True, check=True)


def shutdown_raspi():
    print("Shutting down system...")
    subprocess.run(["shutdown --poweroff now"], shell=True, check=True)


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