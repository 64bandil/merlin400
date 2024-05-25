# Merlin400 - System software and control app
This software can be installed on your Merlin 400 alongside the existing software.
That means it should be safe to install and run this for testing purposes as the old Merlin software will start as normal after a reboot / poweroff.

The control app is included as part of the system software- The system software ha a built-in webserver that is used for serving the app to the local network(s).

For installing and starting this software you must use SSH from a computer to login to your Merlin .


## Installation
    ssh pi@192.168.10.1
    <enter your Merlin's SSID password>

    sudo pip install github-clone
    cd /home/pi
    ghclone https://github.com/64bandil/merlin400/tree/main/merlin400-system    
    sudo cp merlin400-system/merlin400-system.service /etc/systemd/system/

## Usage
Before the new application can be started, the existing Drizzle applications must be stopped:

(These commands only stop the applications currently running. After reboot they will start up as before.)

    sudo systemctl stop drizzle
    sudo pkill -f python

When the application is stopped "Play" button will blink green.

To start the new application:

    sudo systemctl start merlin400-system


## Upgrade 
After installation you can upgrade the application like this:
    sudo systemctl stop merlin400-system
    cd /home/pi
    ghclone https://github.com/64bandil/merlin400/tree/main/merlin400-system
    sudo systemctl start merlin400-system

This will update installation to latest code in main branch
