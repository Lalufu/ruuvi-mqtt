#!/usr/bin/python3
"""
Ruuvi-to-mqtt gateway

This contains the Ruuvi specific parts
"""

import logging
import math
import multiprocessing
import time
from collections import defaultdict
from typing import Any, Dict, Tuple

import ruuvitag_sensor  # type: ignore
from ruuvitag_sensor.ruuvi import RuuviTagSensor  # type: ignore

LOGGER = logging.getLogger(__name__)


def ruuvi_main(mqtt_queue: multiprocessing.Queue, config: Dict[str, Any]) -> None:
    """
    Main function for the Ruuvi process

    Read messages from BLE, and push them to the queue
    """

    # Used to track the last measurement we've seen, to avoid
    # sending duplicate ones.
    #
    # Measurement numbers go up, normally, possibly skipping entries.
    # They may also go down (when a Ruuvi reboots)
    last_measurement: Dict[str, int] = defaultdict(lambda: 0)

    # Used to track the last movement counter value we've seen,
    # to calculate deltas
    last_movement_counter: Dict[str, int] = defaultdict(lambda: None)

    def dewpoint(temperature: float, humidity: float) -> float:
        """
        Calculate an approximate dewpoint temperature Tdp, given a temperature T
        and relative humidity H.

        This uses the Magnus formula:

        N = ln(H / 100) + (( b * T ) / ( c + T ))

        Tdp = ( c * N ) / ( b - N )

        The constants b and c come from

        https://doi.org/10.1175/1520-0450(1981)020%3C1527:NEFCVP%3E2.0.CO;2

        and are
        b = 17.368
        c = 238.88

        for temperatures >= 0 degrees C and

        b = 17.966
        c = 247.15

        for temperatures < 0 degrees C
        """

        if temperature >= 0:
            b = 17.368
            c = 238.88
        else:
            b = 17.966
            c = 247.15

        N = math.log(humidity / 100) + ((b * temperature) / (c + temperature))

        return (c * N) / (b - N)

    def ruuvi_handle_data(found_data: Tuple[str, Dict[str, Any]]) -> None:
        """
        Callback function for tag data

        Enrich the data with the current time, and push
        to queue.

        If the queue is full, drop the data.
        """
        nonlocal mqtt_queue
        nonlocal last_measurement
        nonlocal last_movement_counter

        mac, data = found_data
        lmac = mac.lower()

        LOGGER.debug("Read ruuvi data from mac %s: %s", mac, data)

        if "measurement_sequence_number" not in data or "mac" not in data:
            LOGGER.error(
                "Received measurement without sequence number or mac: %s", data
            )
            return

        cur_seq = data["measurement_sequence_number"]
        last_seq = last_measurement[data["mac"]]

        if cur_seq == last_seq:
            # Duplicate entry
            LOGGER.debug(
                "Received duplicate measurement %s from %s, ignoring",
                cur_seq,
                data["mac"],
            )
            return

        last_measurement[data["mac"]] = cur_seq

        # Occasionally there are measurements that do not have a humidity
        # value. Log these and continue
        if data["humidity"] is None:
            LOGGER.error("Measurement from %s without humidity: %s", mac, data)
            return

        # Sometimes Ruuvitags send humitity values ~100% offset from the
        # "real" value. Ignore these, leaving a small window for values >
        # 100%, which might be real

        if data["humidity"] > 105:
            LOGGER.error(
                "Received invalid humidity value %.2f%%, ignoring", data["humidity"]
            )
            return

        # Process the data through offset functions
        if lmac in config["offset_poly"]:
            processed_data = {}
            for key, value in data.items():
                if key in config["offset_poly"][lmac]:
                    # Ruuvi sends data with two significant digits,
                    # round the scaled data as well
                    processed_data[key] = round(
                        config["offset_poly"][lmac][key](value), 2
                    )
                    processed_data["ruuvi_mqtt_raw_%s" % (key,)] = value
                else:
                    processed_data[key] = value

            data = processed_data

        # Add the dew point temperature, if requested
        if config["dewpoint"]:
            data["ruuvi_mqtt_dewpoint"] = round(
                dewpoint(data["temperature"], data["humidity"]), 2
            )

        # Calculate the movement_counter difference from the previous
        # measurement. This is an 8 bit unsigned counter value.
        #
        # If we don't have a previous value, set the delta to 0
        # If the delta is negative, correct for a possible overflow. If the
        # result is < 32, take this as a real measurement delta, otherwise
        # assume this was a reboot and set the delta to 0.
        if last_movement_counter[data["mac"]] is None:
            delta = 0
        else:
            delta = data["movement_counter"] - last_movement_counter[data["mac"]]
            if delta < 0:
                delta += 256
                if delta >= 32:
                    delta = 0

        last_movement_counter[data["mac"]] = data["movement_counter"]
        data["ruuvi_mqtt_movement_delta"] = delta

        LOGGER.debug("Processed ruuvi data from mac %s: %s", mac, data)

        # Find the device name, if any
        # Use the `mac` field as a fallback
        data["ruuvi_mqtt_name"] = config["macnames"].get(lmac, data["mac"])

        # Add a time stamp. This is an integer, in milliseconds
        # since epoch
        data["ruuvi_mqtt_timestamp"] = int(time.time() * 1000)

        try:
            mqtt_queue.put(data, block=False)
        except Exception:
            # Ignore this
            pass

    LOGGER.info(
        "ruuvi process starting, ruuvitag_sensor version %s",
        ruuvitag_sensor.__version__,
    )

    RuuviTagSensor.get_datas(ruuvi_handle_data, [x.upper() for x in config["filter"]])
