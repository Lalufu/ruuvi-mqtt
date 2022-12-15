"""
Ruuvi-to-mqtt gateway

This file contains the mqtt specific code
"""

import json
import logging
import multiprocessing
import threading
import time
from typing import Any, Dict

import paho.mqtt  # type: ignore
import paho.mqtt.client as mqtt  # type: ignore

LOGGER = logging.getLogger(__name__)


def mqtt_main(ruuvi_queue: multiprocessing.Queue, config: Dict[str, Any]) -> None:
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
                LOGGER.info("Connected to MQTT")
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

    LOGGER.info("mqtt process starting, paho.mqtt version %s", paho.mqtt.__version__)

    # connected is tracking the connection state to MQTT.
    # connected_cv is a condition variable that is protecing
    # access to connected, because it is modified from a different
    # thread.
    connected = False
    connected_cv = threading.Condition()

    client = mqtt.Client(config["mqtt_client_id"])
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

        data = ruuvi_queue.get(block=True)
        LOGGER.debug("Read from queue: %s", data)

        # The paho thread may have died (see
        # https://github.com/eclipse/paho.mqtt.python/pull/674)
        # Check for this and die completely if true
        if not client._thread.is_alive():  # pylint: disable=protected-access
            LOGGER.error("mqtt publishing thread died, bailing out")
            raise SystemExit(1)

        client.publish(
            config["mqtt_topic"]
            % {"mac": data["mac"], "name": data["ruuvi_mqtt_name"]},
            json.dumps(data),
        )
