#!/usr/bin/python3
"""
Ruuvi-to-mqtt gateway
"""

import logging
import argparse

import re

import queue
import time

import multiprocessing
import json

from collections import defaultdict

from ruuvitag_sensor.ruuvi import RuuviTagSensor
import paho.mqtt.client as mqtt

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
        """
        nonlocal connected
        LOGGER.debug("mqtt on_conncet called, flags=%s, rc=%d", flags, rc)
        connected = rc == 0

    def mqtt_on_disconnect(client, userdata, rc):
        """
        Callback for the on_disconnect event
        """
        nonlocal connected
        LOGGER.debug("mqtt on_disconnect called, rc=%d", rc)
        if rc != 0:
            # Unexpected disconnect
            LOGGER.error("Unexpected disconnect from MQTT")

        connected = False

    LOGGER.info("mqtt process starting")

    connected = False
    client = mqtt.Client("ruuvi-mqtt-gateway")
    client.on_connect = mqtt_on_connect
    client.on_disconnect = mqtt_on_disconnect

    client.connect("10.200.254.5")

    # This will spawn a thread that handles events and reconnects
    client.loop_start()

    while True:
        data = queue.get(block=True)
        LOGGER.debug("Read from queue: %s", data)

        if connected:
            client.publish(
                config["mqtt_topic"]
                % {"mac": data["mac"], "name": data["ruuvi_mqtt_name"]},
                json.dumps(data),
            )
        else:
            # We're not connected, push the data back onto the queue, if
            # possible. If not the data is lost, and that's life.
            try:
                queue.put(data, block=False)
            except queue.Full:
                # Ignore this
                LOGGER.debug("MQTT not connected and queue full, data lost")
                pass
            time.sleep(1)


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

        if not "measurement_sequence_number" in data or "mac" not in data:
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

        if cur_seq < last_seq:
            # Reboot?
            LOGGER.info(
                "Sequence number going backwards (%d -> %d) for %s, reboot?",
                cur_seq,
                last_seq,
                data["mac"],
            )

        last_measurement[data["mac"]] = cur_seq

        # Find the device name, if any
        data["ruuvi_mqtt_name"] = config["macnames"].get(lmac, "")

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
            LOGGER.error("Error parsing %s: %s" % (entry, exc))
            raise SystemExit(1)

    return ret


def main():
    """
    Main function
    """
    config = {"filter": []}

    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--mac-name",
        action="append",
        nargs="*",
        help="Assign a name to a ruuvi tag mac address. Format: mac/name. The mac address must be entered with colons, the name must not contain spaces.",
    )
    parser.add_argument(
        "--filter-mac-name",
        action="store_true",
        help="Build a MAC filter list from defined --mac-name pairs",
    )
    parser.add_argument(
        "--mqtt-topic",
        type=str,
        default="ruuvi-mqtt/tele/%(mac)s/%(name)s/SENSOR",
        help="MQTT topic to publish to. May contain python format string references to variables `name` and `mac`. `mac` will not contain colons.",
    )

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    config["macnames"] = process_mac_names(args.mac_name)

    if args.filter_mac_name:
        config["filter"].extend(list(config["macnames"].keys()))

    config["mqtt_topic"] = args.mqtt_topic

    LOGGER.debug("Completed config: %s", config)

    ruuvi_mqtt_queue = multiprocessing.Queue(maxsize=100000)

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
