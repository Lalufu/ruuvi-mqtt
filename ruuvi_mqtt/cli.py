"""
This file contains the CLI script entry points
"""

import argparse
import codecs
import configparser
import logging
import multiprocessing
import os
import re
import textwrap
import time
from typing import Any, Callable, Dict, List

from .mqtt import mqtt_main
from .ruuvi import ruuvi_main

if "INVOCATION_ID" in os.environ:
    # Running under systemd
    logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.INFO)
else:
    logging.basicConfig(
        format="%(asctime)-15s %(levelname)s: %(message)s", level=logging.INFO
    )
LOGGER = logging.getLogger(__name__)


def process_mac_names(namelist: List[str], config: Dict[str, Any]) -> None:
    """
    Given a list of mac/name pairs from the CLI, parse the list,
    validate the entries, and produce a mac->name dict
    """

    if namelist is None:
        return

    for entry in namelist:
        try:
            mac, name = entry[0].split("/", 2)
            if not re.match(r"^(?:[a-fA-F0-9]{2}:){5}[a-fA-F0-9]{2}$", mac):
                raise ValueError("%s is not a valid MAC" % (mac,))
            if re.match(r"\s", name):
                raise ValueError("Name %s contains whitespace" % (name,))

            mac = mac.lower()
            if mac in config["macnames"]:
                LOGGER.warning("Duplicate name definition for mac %s", mac)

            config["macnames"][mac] = name
        except Exception as exc:
            LOGGER.error("Error parsing %s: %s", entry, exc)
            raise SystemExit(1) from exc

    return


def mkpoly(*constants: float) -> Callable[[float], float]:
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

    def poly(arg):
        return sum(((arg ** e) * c) for e, c in enumerate(reversed(cconstants)))

    return poly


def process_offset_poly(polylist: List[str], config: Dict[str, Any]) -> None:
    """
    Given a list of offset definitions, parse the definitions and
    add to config
    """

    if polylist is None:
        return

    for entry in polylist:
        try:
            mac, measurement, constants = entry[0].split("/", 3)
            if not re.match(r"^(?:[a-fA-F0-9]{2}:){5}[a-fA-F0-9]{2}$", mac):
                raise ValueError("%s is not a valid MAC" % (mac,))
            if re.match(r"\s", measurement):
                raise ValueError("measurement %s contains whitespace" % (measurement,))

            # Turn constants into floats
            fconstants = [float(x) for x in constants.split(",")]

            mac = mac.lower()
            measurement = measurement.lower()
            if mac not in config["offset_poly"]:
                config["offset_poly"][mac] = {}

            if measurement in config["offset_poly"][mac]:
                LOGGER.warning(
                    "Duplicate offset definition for %s/%s", mac, measurement
                )

            config["offset_poly"][mac][measurement] = mkpoly(*fconstants)
        except Exception as exc:
            LOGGER.error("Error parsing %s: %s", entry, exc)
            raise SystemExit(1) from exc

    return


def load_config_file(filename: str) -> Dict[str, Any]:
    """
    Load the ini style config file given by `filename`
    """

    config: Dict[str, Any] = {
        "filter": [],
        "macnames": {},
        "offset_poly": {},
        "filter_mac_name": False,
    }
    ini = configparser.ConfigParser()
    try:
        with codecs.open(filename, encoding="utf-8") as configfile:
            ini.read_file(configfile)
    except Exception as exc:
        LOGGER.error("Could not read config file %s: %s", filename, exc)
        raise SystemExit(1) from exc

    if ini.has_option("general", "mqtt-host"):
        config["mqtt_host"] = ini.get("general", "mqtt-host")

    try:
        if ini.has_option("general", "mqtt-port"):
            config["mqtt_port"] = ini.getint("general", "mqtt-port")
    except ValueError as exc:
        LOGGER.error(
            "%s: %s is not a valid value for mqtt-port",
            filename,
            ini.get("general", "mqtt-port"),
        )
        raise SystemExit(1) from exc

    if ini.has_option("general", "mqtt-client-id"):
        config["mqtt_client_id"] = ini.get("general", "mqtt-client-id")

    try:
        if ini.has_option("general", "dewpoint"):
            config["dewpoint"] = ini.getboolean("general", "dewpoint")
    except ValueError as exc:
        LOGGER.error(
            "%s: %s is not a valid value for dewpoint",
            filename,
            ini.get("general", "dewpoint"),
        )
        raise SystemExit(1) from exc

    try:
        if ini.has_option("general", "filter-mac-name"):
            config["filter_mac_name"] = ini.getboolean("general", "filter-mac-name")
    except ValueError as exc:
        LOGGER.error(
            "%s: %s is not a valid value for filter-mac-name",
            filename,
            ini.get("general", "filter-mac-name"),
        )
        raise SystemExit(1) from exc

    try:
        if ini.has_option("general", "buffer-size"):
            config["buffer_size"] = ini.getint("general", "buffer-size")
    except ValueError as exc:
        LOGGER.error(
            "%s: %s is not a valid value for buffer-size",
            filename,
            ini.get("general", "buffer-size"),
        )
        raise SystemExit(1) from exc

    # Loop through other sections, and treat their names as MAC addresses
    for section in ini.sections():
        lsection = section.lower()
        if lsection == "general":
            continue

        if not re.match(r"^(?:[a-fA-F0-9]{2}:){5}[a-fA-F0-9]{2}$", section):
            LOGGER.error("%s: %s is not a valid MAC", filename, section)
            raise SystemExit(1)

        # Handle names
        if ini.has_option(section, "name"):
            config["macnames"][lsection] = ini.get(section, "name")

        # Handle offset definitions
        for option in ini.options(section):
            if not option.startswith("offset-"):
                continue

            measurement = option[7:]
            try:
                constants = ini.get(section, option).split(",")
                fconstants = [float(x) for x in constants]
            except Exception as exc:
                LOGGER.error("Error parsing %s: %s", constants, exc)
                raise SystemExit(1) from exc

            if lsection not in config["offset_poly"]:
                config["offset_poly"][lsection] = {}

            config["offset_poly"][lsection][measurement] = mkpoly(*fconstants)

    return config


def ruuvi_mqtt() -> None:
    """
    Main function
    """

    class CustomFormatter(
        argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter
    ):
        """
        A custom formatter that allows fixed formatting for the epilog,
        while auto-formatting the normal argument help text.

        This is from https://stackoverflow.com/questions/18462610/argumentparser-epilog-and-description-formatting-in-conjunction-with-argumentdef
        and I have no idea why this works.
        """

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
    parser.add_argument("--config", type=str, help="Configuration file to load")
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
        default=None,
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
        default=None,
    )
    parser.add_argument(
        "--mqtt-topic",
        type=str,
        default=None,
        help="MQTT topic to publish to. May contain python format string "
        "references to variables `name` and `mac`. `mac` will not contain "
        "colons. (Default: ruuvi-mqtt/tele/%(mac)s/%(name)s/SENSOR)",
    )
    parser.add_argument("--mqtt-host", type=str, help="MQTT server to connect to")
    parser.add_argument(
        "--mqtt-port", type=int, default=None, help="MQTT port to connect to"
    )
    parser.add_argument(
        "--mqtt-client-id",
        type=str,
        default=None,
        help="MQTT client ID. Needs to be unique between all clients connecting "
        "to the same broker",
    )
    parser.add_argument(
        "--buffer-size",
        type=int,
        default=None,
        help="How many measurements to buffer if the MQTT "
        "server should be unavailable. This buffer is not "
        "persistent across program restarts.",
    )

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.config:
        config = load_config_file(args.config)
    else:
        config = {"filter": [], "filter_mac_name": False}

    LOGGER.debug("Config after loading config file: %s", config)

    process_mac_names(args.mac_name, config)

    process_offset_poly(args.offset_poly, config)

    if config["filter_mac_name"] or args.filter_mac_name:
        config["filter"].extend(list(config["macnames"].keys()))

    if args.mqtt_topic:
        config["mqtt_topic"] = args.mqtt_topic
    elif "mqtt_topic" not in config:
        # Not set through config file, not set through CLI, use default
        config["mqtt_topic"] = "ruuvi-mqtt/tele/%(mac)s/%(name)s/SENSOR"
    if args.mqtt_host:
        config["mqtt_host"] = args.mqtt_host

    if args.mqtt_port:
        config["mqtt_port"] = args.mqtt_port
    elif "mqtt_port" not in config:
        # Not set through config file, not set through CLI, use default
        config["mqtt_port"] = 1883

    if args.mqtt_client_id:
        config["mqtt_client_id"] = args.mqtt_client_id
    elif "mqtt_client_id" not in config:
        # Not set through config file, not set through CLI, use default
        config["mqtt_client_id"] = "ruuvi-mqtt-gateway"

    if args.buffer_size:
        config["buffer_size"] = args.buffer_size
    elif "buffer_size" not in config:
        # Not set through config file, not set through CLI, use default
        config["buffer_size"] = 100000

    if args.dewpoint:
        config["dewpoint"] = args.dewpoint

    LOGGER.debug("Completed config: %s", config)

    if "mqtt_host" not in config:
        LOGGER.error("No MQTT host given")
        raise SystemExit(1)

    ruuvi_mqtt_queue: multiprocessing.Queue = multiprocessing.Queue(
        maxsize=config["buffer_size"]
    )

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
    run = True
    while run:
        for proc in procs:
            if not proc.is_alive():
                LOGGER.error("Child process died, terminating program")
                run = False
        time.sleep(1)

    for proc in procs:
        proc.terminate()
    raise SystemExit(1)
