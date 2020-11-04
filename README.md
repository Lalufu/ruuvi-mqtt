# Ruuvi-MQTT gateway

This is a simple application that reads BLE broadcasts from 
[Ruuvi tags](https://www.ruuvi.com) and sends them to an MQTT gateway.

It's a thin wrapper around [ruuvitag-sensor](https://pypi.org/project/ruuvitag-sensor/)
for reading from the sensors, and [paho-mqtt](https://pypi.org/project/paho-mqtt/)
for talking to MQTT.

It also can do some data enrichment and data calibration.

## Installation

This is a single script program that can be run directly from a git
checkout. The only external dependencies are `ruuvitag-sensor` and `paho-mqtt`.
These can be installed in a venv if required via the enclosed `requirements.txt`.

See the installation instructions of `ruuvitag-sensor` for information on
how to set up bluez, which it uses under the hood.

## Running

The program has the following command line parameters:

`--mqtt-host`
: The MQTT host name to connect to. This is a required parameter.

`--mqtt-port`
: The MQTT port number to connect to. Defaults to 1883.

`--mqtt-topic`
: The MQTT topic to publish the information to. This is a string that is put
  through python formatting, and can contain references to the variables `mac`
  and `name`. `mac` will contain the MAC address of the tag, in lower case letters,
  and without colons. `name` will contain the name assigned to the tag, if any
  (see `--mac-name`), otherwise the empty string. The default is
  `ruuvi-mqtt/tele/%(mac)s/%(name)s/SENSOR`.

`--mac-name`
: This allows assigning a human readable name to a tag. The format of this
  parameter is `mac/name`, where `mac` is the MAC address of the tag, in colon
  separated form. `name` is a human readable name for the tag. It must not
  contain whitespace. This paramter can be used multiple times, to assign names
  to multiple tags.

`--filter-mac-name`
: Filter traffic from Ruuvi tags to only accept messages from tags which have
  a name assigned via `--mac-name`, and ignore all other tags.

`--offset-poly`
: This defines a polynomial offset function to be applied to a certain measurement
  from a certain tag. The format of this parameter is `mac/measurement/constants`.
  `mac` is the MAC address of the tag, in colon separated form. `measurement` is
  the name of the measurement the polynomial is applied to. `constants` is a
  comma separated list of floats, representing the polynomial constants. These
  are given in descending order. See the section `Polynomial offset functions`
  for more details.

## Polynomial offset functions
Polynomial offset functions are offered for multiple measurements,
to assist with calibrating measurements across multiple tags.

The way these work is to define a polynomial of arbitrary size.
The raw measurement is passed through the polynomial, and the
resulting value is then sent to mqtt.

A polynomial has the general form

f(x) = an * x^n + .... + a2 * x^2 + a1 * x^1 + a0

Where an...a0 are the so called polynomial constants.

### Example

aa:bb:cc:dd:ee:ff/temperature/1,1.5

This will apply the polynomial f(x) = 1 * x + 1.5 to the
temperature measurement from the tag with mac aa:bb:cc:dd:ee:ff.
This will just add 1.5 to all temperature measurements, and thus
represent a constant offset.


aa:bb:cc:dd:ee:ff/humidity/0.98,1.01,0

This will apply the polynomial f(x) = 0.98 * x^2 + 1.01 * x to
the humidity measurement from the tag with mac aa:bb:cc:dd:ee:ff.
Note that all constants need to be given, even if they are 0.