#!/usr/bin/python3
"""
Ruuvi-to-mqtt gateway
"""

import argparse
import json
import logging
import math
import multiprocessing
import re
import textwrap
import threading
import time
from collections import defaultdict

import paho.mqtt.client as mqtt
from ruuvitag_sensor.ruuvi import RuuviTagSensor

logging.basicConfig(
    format="%(asctime)-15s %(levelname)s: %(message)s", level=logging.INFO
)
LOGGER = logging.getLogger(__name__)


def mqtt_main(queue, config):
    """
    Main function for the MQTT process

    Connect to the server, read from the queue, and publish
    messages
    """

    def mqtt_on_connect(client, userdata, flags, rc):
        """
        Callback for the on_connect event

        This is called from a different thread
        """
        nonlocal connected, connected_cv
        LOGGER.debug("mqtt on_connect called, flags=%s, rc=%d", flags, rc)

        with connected_cv:
            connected = rc == 0
            if connected:
                connected_cv.notify()

    def mqtt_on_disconnect(client, userdata, rc):
        """
        Callback for the on_disconnect event

        This is called from a different thread
        """
        nonlocal connected, connected_cv
        LOGGER.debug("mqtt on_disconnect called, rc=%d", rc)
        if rc != 0:
            # Unexpected disconnect
            LOGGER.error("Unexpected disconnect from MQTT")

        with connected_cv:
            connected = False

            # We do not have to wake up the waiter for this,
            # because they'll just go back to sleep anyway

    LOGGER.info("mqtt process starting")

    # connected is tracking the connection state to MQTT.
    # connected_cv is a condition variable that is protecing
    # access to connected, because it is modified from a different
    # thread.
    connected = False
    connected_cv = threading.Condition()

    client = mqtt.Client("ruuvi-mqtt-gateway")
    client.on_connect = mqtt_on_connect
    client.on_disconnect = mqtt_on_disconnect

    # This will spawn a thread that handles events and reconnects
    client.loop_start()

    # We're going to loop until the connection succeeds, once
    # it does the paho state machine will take care of reconnects
    while True:
        try:
            client.connect(config["mqtt_host"], port=config["mqtt_port"])
        except Exception as exc:
            LOGGER.info(
                "Could not connect to %s:%s, retrying (%s)",
                config["mqtt_host"],
                config["mqtt_port"],
                exc,
            )
            time.sleep(2)
            continue

        break

    while True:
        # This will sleep unless we're connected
        with connected_cv:
            connected_cv.wait_for(lambda: connected)

        data = queue.get(block=True)
        LOGGER.debug("Read from queue: %s", data)

        client.publish(
            config["mqtt_topic"]
            % {"mac": data["mac"], "name": data["ruuvi_mqtt_name"]},
            json.dumps(data),
        )


def ruuvi_main(queue, config):
    """
    Main function for the Ruuvi process

    Read messages from BLE, and push them to the queue
    """

    # Used to track the last measurement we've seen, to avoid
    # sending duplicate ones.
    #
    # Measurement numbers go up, normally, possibly skipping entries.
    # They may also go down (when a Ruuvi reboots)
    last_measurement = defaultdict(lambda: 0)

    def dewpoint(temperature, humidity):
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

    def ruuvi_handle_data(found_data):
        """
        Callback function for tag data

        Enrich the data with the current time, and push
        to queue.

        If the queue is full, drop the data.
        """
        nonlocal queue
        nonlocal last_measurement

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

        LOGGER.debug("Processed ruuvi data from mac %s: %s", mac, data)

        # Find the device name, if any
        # Use the `mac` field as a fallback
        data["ruuvi_mqtt_name"] = config["macnames"].get(lmac, data["mac"])

        # Add a time stamp. This is an integer, in milliseconds
        # since epoch
        data["ruuvi_mqtt_timestamp"] = int(time.time() * 1000)

        try:
            queue.put(data, block=False)
        except queue.Full:
            # Ignore this
            pass

    LOGGER.info("ruuvi process starting")

    RuuviTagSensor.get_datas(ruuvi_handle_data, [x.upper() for x in config["filter"]])


def process_mac_names(namelist):
    """
    Given a list of mac/name pairs from the CLI, parse the list,
    validate the entries, and produce a mac->name dict
    """

    ret = {}
    if namelist is None:
        return ret

    for entry in namelist:
        try:
            mac, name = entry[0].split("/", 2)
            if not re.match(r"^(?:[a-fA-F0-9]{2}:){5}[a-fA-F0-9]{2}$", mac):
                raise ValueError("%s is not a valid MAC" % (mac,))
            if re.match(r"\s", name):
                raise ValueError("Name %s contains whitespace" % (name,))

            mac = mac.lower()
            if mac in ret:
                raise ValueError("Duplicate definition for mac %s" % (mac,))

            ret[mac] = name
        except Exception as exc:
            LOGGER.error("Error parsing %s: %s", entry, exc)
            raise SystemExit(1)

    return ret


def process_offset_poly(polylist):
    """
    Given a list of offset definitions, parse the definitions and
    return a structure
    """

    def mkpoly(*constants):
        """
        Return a function that evaluates the polynomial
        given by the constants.

        Constants are passed in descending order, an ... a2, a1, a0
        for the polynomial

        f(x) = an &* x^n + ... + a2 * x^2 + a1 * x^1 + a0
        """
        # Make a copy of the constants, they must not change
        # later
        cconstants = constants[:]

        def poly(x):
            return sum(((x ** e) * c) for e, c in enumerate(reversed(cconstants)))

        return poly

    ret = {}
    if polylist is None:
        return ret

    for entry in polylist:
        try:
            mac, measurement, constants = entry[0].split("/", 3)
            if not re.match(r"^(?:[a-fA-F0-9]{2}:){5}[a-fA-F0-9]{2}$", mac):
                raise ValueError("%s is not a valid MAC" % (mac,))
            if re.match(r"\s", measurement):
                raise ValueError("measurement %s contains whitespace" % (name,))

            # Turn constants into floats
            fconstants = [float(x) for x in constants.split(",")]

            mac = mac.lower()
            measurement = measurement.lower()
            if mac not in ret:
                ret[mac] = {}

            if measurement in ret[mac]:
                raise ValueError(
                    "Duplicate offset definition for %s/%s" % (mac, measurement)
                )

            ret[mac][measurement] = mkpoly(*fconstants)
        except Exception as exc:
            LOGGER.error("Error parsing %s: %s", entry, exc)
            raise SystemExit(1)

    return ret


def main():
    """
    Main function
    """
    config = {"filter": []}

    class CustomFormatter(
        argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter
    ):
        """
        A custom formatter that allows fixed formatting for the epilog,
        while auto-formatting the normal argument help text.

        This is from https://stackoverflow.com/questions/18462610/argumentparser-epilog-and-description-formatting-in-conjunction-with-argumentdef
        and I have no idea why this works.
        """

        pass

    parser = argparse.ArgumentParser(
        formatter_class=CustomFormatter,
        epilog=textwrap.dedent(
            """
            Polynomial offset functions

            Polynomial offset functions are offered for multiple measurements,
            to assist with calibrating measurements across multiple tags.

            The way these work is to define a polynomial of arbitrary size.
            The raw measurement is passed through the polynomial, and the
            resulting value is then sent to mqtt.

            A polynomial has the general form

            f(x) = an * x^n + .... + a2 * x^2 + a1 * x^1 + a0

            Where an...a0 are the so called polynomial constants.

            The general format of this parameter is:
              mac/measurement/constants

            mac is the mac address of the tag, in aa:bb:cc:dd:ee:ff form
            measurement is the name of the measurement the polynomial is to
              be applied to
            constants is a comma separated list of floats, representing
              the polynomial constants. These are given in descending order,
              from an to a0. The number of constants given determines the
              order of the polynomial

            Example:

            aa:bb:cc:dd:ee:ff/temperature/1,1.5

            This will apply the polynomial f(x) = 1 * x + 1.5 to the
            temperature measurement from the tag with mac aa:bb:cc:dd:ee:ff.
            This will just add 1.5 to all temperature measurements, and thus
            represent a constant offset.


            aa:bb:cc:dd:ee:ff/humidity/0.98,1.01,0

            This will apply the polynomial f(x) = 0.98 * x^2 + 1.01 * x to
            the humidity measurement from the tag with mac aa:bb:cc:dd:ee:ff.
            Note that all constants need to be given, even if they are 0.
        """
        ),
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--mac-name",
        action="append",
        nargs="*",
        help="Assign a name to a ruuvi tag mac address. Format: mac/name. The mac "
        "address must be entered with colons, the name must not contain spaces.",
    )
    parser.add_argument(
        "--filter-mac-name",
        action="store_true",
        help="Build a MAC filter list from defined --mac-name pairs",
    )
    parser.add_argument(
        "--offset-poly",
        action="append",
        nargs="*",
        help="Define a polynomial offset function for a sensor and measurement",
    )
    parser.add_argument(
        "--dewpoint",
        action="store_true",
        help="Calculate an approximate dew point temperature and add it to the data "
        "as `ruuvi_mqtt_dewpoint`. This follows the Magnus formula with "
        "coefficients by Buck/1981",
    )
    parser.add_argument(
        "--mqtt-topic",
        type=str,
        default="ruuvi-mqtt/tele/%(mac)s/%(name)s/SENSOR",
        help="MQTT topic to publish to. May contain python format string "
        "references to variables `name` and `mac`. `mac` will not contain "
        "colons.",
    )
    parser.add_argument(
        "--mqtt-host", type=str, required=True, help="MQTT server to connect to"
    )
    parser.add_argument(
        "--mqtt-port", type=int, default=1883, help="MQTT port to connect to"
    )
    parser.add_argument(
        "--buffer-size",
        type=int,
        default=100000,
        help="How many measurements to buffer if the MQTT "
        "server should be unavailable. This buffer is not "
        "persistent across program restarts.",
    )

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    config["macnames"] = process_mac_names(args.mac_name)

    config["offset_poly"] = process_offset_poly(args.offset_poly)

    if args.filter_mac_name:
        config["filter"].extend(list(config["macnames"].keys()))

    config["mqtt_topic"] = args.mqtt_topic
    config["mqtt_host"] = args.mqtt_host
    config["mqtt_port"] = args.mqtt_port
    config["dewpoint"] = args.dewpoint

    LOGGER.debug("Completed config: %s", config)

    ruuvi_mqtt_queue = multiprocessing.Queue(maxsize=args.buffer_size)

    procs = []
    ruuvi_proc = multiprocessing.Process(
        target=ruuvi_main, name="ruuvi", args=(ruuvi_mqtt_queue, config)
    )
    ruuvi_proc.start()
    procs.append(ruuvi_proc)

    mqtt_proc = multiprocessing.Process(
        target=mqtt_main, name="mqtt", args=(ruuvi_mqtt_queue, config)
    )
    mqtt_proc.start()
    procs.append(mqtt_proc)

    # Wait forever for one of the threads to die. If that happens,
    # kill the whole program.
    while True:
        for proc in procs:
            if not proc.is_alive():
                LOGGER.error("Child process died, terminating program")
                for proc in procs:
                    proc.terminate()
                raise SystemExit(1)

        time.sleep(1)


if __name__ == "__main__":
    main()
