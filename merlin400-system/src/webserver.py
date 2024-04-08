from flask import Flask, jsonify
import sys, traceback

import controlthread as controlthread
from hardware.commands.start_extraction import Command_StartExtraction
from hardware.commands.start_heat_oil import Command_StartHeatOil
from hardware.commands.start_clean_pump import Command_StartCleanPump
from hardware.commands.start_decarb import Command_StartDecarb
from hardware.commands.start_distill import Command_StartDistill
from hardware.commands.start_vent_pump import Command_StartVentPump
from hardware.commands.pause_program import Command_PauseProgram
from hardware.commands.resume_program import Command_ResumeProgram
from hardware.commands.reset import Command_Reset
from hardware.commands.clean_valve import Command_CleanValve

app = Flask(__name__)

@app.route("/")
def home():
    global control_thread 
    return control_thread.get_machine_json_status()

@app.route("/logfile.txt")
def get_log_file():
    return ""

@app.route("/api/startextract", methods = ['POST'])
def start_extract():
    #parameters: Full, SoakTime
    global control_thread 
    command = Command_StartExtraction(True)
    return process_command(command)

@app.route("/api/startheatoil", methods = ['POST'])
def start_heat_oil():
    return process_command(Command_StartHeatOil())

@app.route("/api/startcleanpump", methods = ['POST'])
def start_clean_pump():
    return process_command(Command_StartCleanPump())

@app.route("/api/startdecarb", methods = ['POST'])
def start_decarb():
    return process_command(Command_StartDecarb())

@app.route("/api/startdistill", methods = ['POST'])
def start_distill():
    return process_command(Command_StartDistill())

@app.route("/api/startventpump", methods = ['POST'])
def start_vent_pump():
    return process_command(Command_StartVentPump())

@app.route("/api/pause", methods = ['POST'])
def pause():
    return process_command(Command_PauseProgram())

@app.route("/api/resume", methods = ['POST'])
def resume():
    return process_command(Command_ResumeProgram())

@app.route("/api/reset", methods = ['POST'])
def reset():
    return process_command(Command_Reset())

@app.route("/api/cleanvalve/<int:valvenumber>", methods = ['POST'])
def clean_valve(valvenumber: int):
    return process_command(Command_CleanValve(valvenumber))

def process_command(command):
    global control_thread 
    try:
        control_thread.schedule_command_for_execution(command)
    except Exception as error:
        exc_info = sys.exc_info()
        sExceptionInfo = ''.join(traceback.format_exception(*exc_info))
        exc_type, exc_value, exc_context = sys.exc_info()

        responseJson = jsonify( 
            {
                "type": exc_type.__name__,
                "description": str(exc_value),
                #"details": sExceptionInfo 
            }
        )
        return responseJson, 409
    return '',204

def start_server(controlThread:controlthread.ControlThread ):
    global control_thread
    control_thread=controlThread
    app.run(debug=False, host="0.0.0.0", port=int("8080"))
