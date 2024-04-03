import numpy as np


def get_pv_const(pressure, total_volume):
    """Calculate PV value (based on ideal gas equasion).

    :param pressure: pressure value.
    :param total_volume: total volume value.
    """
    return pressure * total_volume


def get_pressure_leak_by_sample_time(pressure_stop, pressure_start, leak_sample_time):
    """Calculate pressure leak by sample time.
    :param pressure_stop: final pressure value.
    :param pressure_start: initial pressure value.
    :param leak_sample_time: sample time interval.
    """
    return (pressure_stop - pressure_start) / leak_sample_time


def get_pressure_leak(pressure_stop, pressure_start, time_stop, time_start):
    """Calculate pressure leak basedon start/stop pressure values and start/stop time interval values

    :param pressure_stop: last pressure measurement value.
    :param pressure_start: initial pressure measurement value.
    :param time_stop: timestamp of last pressure measurement.
    :param time_start: timestamp of initial pressure measurement.
    """
    return (pressure_stop - pressure_start) / (time_stop - time_start)


def get_leakfactor(current_time, leak_detect_time, system_leak, historic_leak):
    """Calculate total pressure loss.

    :param current_time: - current timestamp.
    :param leak_detect_time: time of leak measure.
    :param system_leak: calculated corrected system leak.
    :param historic_leak: cumulative historic leak.
    """
    return ((current_time - leak_detect_time) * system_leak) + historic_leak


def get_total_volume_aspiration(total_volume, pv_const, current_pressure):
    """Calculate total aspiration volume based on volume change, using a derived ideal gas equation.

    Leak compensation was removed as it is more precise without it.

    :param pv_const: PV, pressure multiplied on volume.
    :param total_volume: total device volume.
    :param current_pressure: current pressure in the system.
    """
    return total_volume - (pv_const / current_pressure)


def get_flowrate(current_aspirated_volume, initial_aspirated_volume, current_time, initial_time):
    """Calculate current flowrate.

    :param current_aspirated_volume: current aspirated volume value.
    :param initial_aspirated_volume: initial value of aspirated volume.
    :param current_time: current timestamp.
    :param initial_time: timestamp of the beginning of the aspiration.
    """
    return (current_aspirated_volume - initial_aspirated_volume) / (current_time - initial_time)


def calculate_raw_volume(current_pressure, total_volume, initial_pressure, atm_pressure):
    """Calculate raw volume of plant and liquid in the device.

    :param full_system_pressure: current pressure in the system.
    :param total_volume: total device volume.
    :param initial_pressure: initial pressure measurement.
    :param atm_pressure: atmospheric pressure.
    """
    return ((current_pressure * total_volume) - (total_volume * initial_pressure)) / (atm_pressure - current_pressure)


def get_historic_leak(system_leak, stop_time, start_time):
    """Calculate historic leak.

    :param system_leak: current system pressure leak (per second).
    :param stop_time: measuring interval stop time.
    :param start_time: measuring interval start time.
    """
    return system_leak * (stop_time - start_time)


def convert_air_volume_to_plant_and_liquid_volume(
    air_volume_calibration_data, actual_volume_calibration_data, air_volume
):
    """Calculate plant and liquid volume based on calibration data and actual air volume using linear interpolation.

    :param air_volume_calibration_data: list of calibration values of air volume of the device.
    :param actual_volume_calibration_data: list of calibration values of aspirated volume of the device.
    :param air_volume: calculated raw value of air volume in the device.
    """
    # Conversion between the measured air volume and the required air displacement volume to fill the EXC
    return np.interp(float(air_volume), air_volume_calibration_data, actual_volume_calibration_data)


def get_pressure_slope(pressure_diff, time_elapsed):
    """Calculate pressure slope based on pressure change over time.

    :param pressure_diff: value of pressure change over time.
    :param time_elapsed: time difference in seconds between pressure measurements
    """
    return pressure_diff / time_elapsed


def calculate_distill_progress(elapsed_time_seconds, power_uptake):
    """Calculate distill progress and ETA (in seconds) based on power uptake."""
    # Current interpolation logic:
    # power 80% = 2,5 hours
    # power 90% = 2 hours
    # power 50% = 6 hours
    power_x = [0.5, 0.8, 0.9]
    time_y = [6 * 3600, 2.5 * 3600, 2 * 3600]
    if elapsed_time_seconds < 1:
        elapsed_time_seconds = 1
    result_percentage = 0
    result_eta = 0
    time_estimated = np.interp(power_uptake, power_x, time_y)
    if (time_estimated - elapsed_time_seconds) > 0:
        result_eta = time_estimated - elapsed_time_seconds
        result_percentage = elapsed_time_seconds / time_estimated
    else:
        result_percentage = 0.99
        result_eta = 1

    return result_percentage, result_eta
