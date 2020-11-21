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

`--config`
: Specify a configuration file to load. See the section `Configuration file`
  for details on the syntax. Command line options given in addition to the
  config file override settings in the config file.

`--mqtt-host`
: The MQTT host name to connect to. This is a required parameter.

  Config file: Section `general`, `mqtt-host`

`--mqtt-port`
: The MQTT port number to connect to. Defaults to 1883.

  Config file: Section `general`, `mqtt-port`

`--buffer-size`
: The size of the buffer (in number of measurements) that can be locally
  saved when the MQTT server is unavailable. The buffer is not persistent,
  and will be lost when the program exits. Defaults to 100000. When sizing,
  take into consideration that each tag will send a measurement approximately
  every second.

  Config file: Section `general`, `buffer-size`

`--mqtt-topic`
: The MQTT topic to publish the information to. This is a string that is put
  through python formatting, and can contain references to the variables `mac`
  and `name`. `mac` will contain the MAC address of the tag, in lower case letters,
  and without colons. `name` will contain the name assigned to the tag, if any
  (see `--mac-name`), otherwise a copy of the `mac` field. The default is
  `ruuvi-mqtt/tele/%(mac)s/%(name)s/SENSOR`.

  Config file: Section `general`, `mqtt-topic`

`--mqtt-client-id`
: The client identifier used when connecting to the MQTT gateway. This needs
  to be unique for all clients connecting to the same gateway, only one
  client can be connected with the same name at a time. The default is
  `ruuvi-mqtt-gateway`.

  Config file: Section `general`, `mqtt-client-id`

`--mac-name`
: This allows assigning a human readable name to a tag. The format of this
  parameter is `mac/name`, where `mac` is the MAC address of the tag, in colon
  separated form. `name` is a human readable name for the tag. It must not
  contain whitespace. This paramter can be used multiple times, to assign names
  to multiple tags.

  Config file: `name` in the tag specific section

`--filter-mac-name`
: Filter traffic from Ruuvi tags to only accept messages from tags which have
  a name assigned via `--mac-name`, and ignore all other tags.

  Config file: Section `general`, `filter-mac-name`

`--offset-poly`
: This defines a polynomial offset function to be applied to a certain measurement
  from a certain tag. The format of this parameter is `mac/measurement/constants`.
  `mac` is the MAC address of the tag, in colon separated form. `measurement` is
  the name of the measurement the polynomial is applied to. `constants` is a
  comma separated list of floats, representing the polynomial constants. These
  are given in descending order. See the section `Polynomial offset functions`
  for more details.

  Config file: An option called `offset-<measurement>` in the tag specific
  section

`--dewpoint`
: This will add a calculated, approximate dew point temperature to the
  data set under the `ruuvi_mqtt_dewpoint` key, based on temperature and
  humidity. See the section `Dew point temperature` for more details.

  Config file: Section `general`, `dewpoint`

## Configuration file
The program supports a configuration file to define behaviour. The
configuration file is in .ini file syntax, and can contain multiple sections.
The `[general]` section contains settings that define overall program
behaviour.

Other sections are named after the MAC address of a Ruuvi tag (in colon
separated form), and contain settings for this specific tag.


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

## Dew point temperature
The program can calculate an approximate dewpoint temperature Tdp, given a
temperature T and relative humidity H.

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

## Data pushed to MQTT

The script pushes the data received from the Ruuvi tags to MQTT as a JSON
string. The original structure as received from `ruuvitag-sensor` is
preserved, with the below changes.

- a `ruuvi_mqtt_timestamp` field is added, containing the time the datagram
  was received via BLE. This field is the UNIX epoch, in milliseconds.

- a `ruuvi_mqtt_name` field is added, containing the human readable name
  of the tag as defined with `--mac-name`. If no name is defined for a tag
  a copy of the `mac` field is used.

- a `ruuvi_mqtt_dewpoint` field with a calculated dew point temperature
  is added when the `--dewpoint` option is given on the command line.

- For each field that was modified through a `--offset-poly` function, the
  original value is preserved in a field called `ruuvi_mqtt_raw_<field>`.
