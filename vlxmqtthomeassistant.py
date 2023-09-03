#!/usr/bin/env python3
import os
import sys
import signal
import logging
import configparser
import paho.mqtt.client as mqtt
import argparse
import asyncio
from pyvlx import Position, PyVLX, OpeningDevice
from pyvlx.log import PYVLXLOG

from ha_mqtt.ha_device import HaDevice
from ha_mqtt.mqtt_device_base import MqttDeviceSettings
from mqtt_cover import MqttCover

parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter,
                                 description="Allows to control devices paired with Velux KLF200 via MQTT.\n" \
                                    "Registers the devices to Homeassistant using MQTT Autodiscovery.")
parser.add_argument('config_file', metavar="<config_file>", help="configuration file")
args = parser.parse_args()

# read and parse config file
config = configparser.RawConfigParser()
config.read(args.config_file)
# [mqtt]
MQTT_HOST = config.get("mqtt", "host")
MQTT_PORT = config.getint("mqtt", "port")
MQTT_LOGIN = config.get("mqtt", "login", fallback=None)
MQTT_PASSWORD = config.get("mqtt", "password", fallback=None)
# [velux]
VLX_HOST = config.get("velux", "host")
VLX_PW = config.get("velux", "password")
# [log]
VERBOSE = config.get("log", "verbose")
LOGFILE = config.get("log", "logfile", fallback=None)

APPNAME = "veluxmqtthomeassistant"

# init logging 
LOGFORMAT = '%(asctime)-15s %(message)s'

if VERBOSE:
    loglevel = logging.DEBUG
else:
    loglevel = logging.INFO

if LOGFILE:
    logging.basicConfig(filename=LOGFILE, format=LOGFORMAT, level=loglevel)
else:
    logging.basicConfig(stream=sys.stdout, format=LOGFORMAT, level=loglevel)

logging.info("Starting " + APPNAME)
if VERBOSE:
    logging.info("DEBUG MODE")
else:
    logging.debug("INFO MODE")

PYVLXLOG.setLevel(logging.FATAL)
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.FATAL)
PYVLXLOG.addHandler(ch)

class VeluxMqttCover:
    def __init__(self, mqttc, vlxnode, mqttid):
        logging.debug("Registering %s to Homeassistant" % (vlxnode.name))
        self.vlxnode = vlxnode
        self.haDevice = HaDevice("DEV-" + vlxnode.name, "dev-" + mqttid)
        self.coverDevice = MqttCover(MqttDeviceSettings("cover", "dev-" + mqttid + "-cover", mqttc, self.haDevice))

    async def registerMqttCallbacks(self):
        self.coverDevice.callback_open = self.mqtt_callback_open
        self.coverDevice.callback_close = self.mqtt_callback_close
        self.coverDevice.callback_stop = self.mqtt_callback_stop
        self.coverDevice.callback_position = self.mqtt_callback_position
        
    def updateNode(self):
        position = self.vlxnode.position.position_percent
        self.coverDevice.publish_position(position)
        if position < 50:
            self.coverDevice.publish_state('open')
        else:
            self.coverDevice.publish_state('closed')
    
    def mqtt_callback_open(self):
        logging.debug("Opening %s", self.vlxnode.name)
        asyncio.run(self.vlxnode.open(wait_for_completion=False))

    def mqtt_callback_close(self):
        logging.debug("Closing %s", self.vlxnode.name)
        asyncio.run(self.vlxnode.close(wait_for_completion=False))

    def mqtt_callback_stop(self):
        logging.debug("Stopping %s", self.vlxnode.name)
        asyncio.run(self.vlxnode.stop(wait_for_completion=False))

    def mqtt_callback_position(self, position):
        logging.debug("Moving %s to position %s" % (self.vlxnode.name, position))
        asyncio.run(self.vlxnode.set_position(Position(position_percent=int(position)), wait_for_completion=False))

    def __del__(self):
        logging.debug("Unregistering %s from Homeassistant" % (self.vlxnode.name))
        self.coverDevice.close()


class VeluxMqttHomeassistant:
    def __init__(self):
        # MQTT
        MQTT_CLIENT_ID = APPNAME + "_%d" % os.getpid()
        self.mqttc = mqtt.Client(MQTT_CLIENT_ID)
        self.mqttDevices = {}

    async def connect_mqtt(self):
        logging.debug("MQTT broker : %s" % MQTT_HOST)
        if MQTT_LOGIN:
            logging.debug("  port      : %s" % (str(MQTT_PORT)))
            logging.debug("  login     : %s" % MQTT_LOGIN)

        # set login and password, if available
        if MQTT_LOGIN:
            self.mqttc.username_pw_set(MQTT_LOGIN, MQTT_PASSWORD)

        # Connect to the broker and enter the main loop
        result = self.mqttc.connect(MQTT_HOST, MQTT_PORT, 60)
        while result != 0:
            logging.info("Connection failed with error code %s. Retrying", result)
            await asyncio.sleep(10)
            result = self.mqttc.connect(MQTT_HOST, MQTT_PORT, 60)

        self.mqttc.loop_start()
        await asyncio.sleep(1)

    async def connect_klf200(self, loop):
        logging.debug("klf200      : %s" % VLX_HOST)
        self.pyvlx = PyVLX(host=VLX_HOST, password=VLX_PW, loop=loop)
        await self.pyvlx.load_nodes()

        logging.debug("vlx nodes   : %s" % (len(self.pyvlx.nodes)))
        for node in self.pyvlx.nodes:
            logging.debug("  %s" % node.name)

    async def register_devices(self):
        # register callbacks
        for vlxnode in self.pyvlx.nodes:
            if isinstance(vlxnode, OpeningDevice):
                vlxnode.register_device_updated_cb(self.vlxnode_callback)
                mqttid = self.generate_id(vlxnode)
                mqttCover = VeluxMqttCover(self.mqttc, vlxnode, mqttid)
                self.mqttDevices[mqttid] = mqttCover
                await mqttCover.registerMqttCallbacks()
                logging.debug("watching: %s" % vlxnode.name)

    async def vlxnode_callback(self, vlxnode):
        logging.debug("%s at %d%%" % (vlxnode.name, vlxnode.position.position_percent))
        mqttid = self.generate_id(vlxnode)
        mqttDevice = self.mqttDevices[mqttid]
        if mqttDevice:
            mqttDevice.updateNode()

    def generate_id(self, vlxnode):
        return "vlx-" + vlxnode.name.replace(" ", "-").lower()


    def __del__(self):
        for mqttDeviceId in self.mqttDevices:
            del self.mqttDevices[mqttDeviceId]
            self.mqttDevices.pop(mqttDeviceId)
        logging.info("Disconnecting from MQTT broker")
        self.mqttc.disconnect()
        self.mqttc.loop_stop()

        logging.info("Disconnecting from KLF200")
        self.pyvlx.disconnect()


# Use the signal module to handle signals
signal.signal(signal.SIGTERM, lambda: asyncio.get_event_loop().stop())
signal.signal(signal.SIGINT, lambda: asyncio.get_event_loop().stop())

if __name__ == '__main__':
    # pylint: disable=invalid-name
    try:
        LOOP = asyncio.get_event_loop()

        pid = str(os.getpid())
        pidfile = "/tmp/vlxmqtthomeassistant.pid"

        if os.path.isfile(pidfile):
            print("%s already exists, exiting" % pidfile)
            sys.exit()
        file = open(pidfile, 'w')
        file.write(pid)
        file.close()

        veluxMqttHomeassistant = VeluxMqttHomeassistant()
        LOOP.run_until_complete(veluxMqttHomeassistant.connect_mqtt())
        LOOP.run_until_complete(veluxMqttHomeassistant.connect_klf200(LOOP))
        LOOP.run_until_complete(veluxMqttHomeassistant.register_devices())

        LOOP.run_forever()
    except KeyboardInterrupt:
        logging.info("Interrupted by keypress")
    finally:
        del veluxMqttHomeassistant
        os.unlink(pidfile)
    LOOP.close()
    sys.exit(0)