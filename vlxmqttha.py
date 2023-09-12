#!/usr/bin/env python3
import os
import sys
import signal
import logging
import configparser
import paho.mqtt.client as mqtt
import argparse
import asyncio
from pyvlx import Position, PyVLX, OpeningDevice, Window, Blind, Awning, RollerShutter, GarageDoor, Gate, Blade
from pyvlx.log import PYVLXLOG

from ha_mqtt.ha_device import HaDevice
from ha_mqtt.mqtt_device_base import MqttDeviceSettings
from ha_mqtt.mqtt_switch import MqttSwitch
from ha_mqtt.util import HaDeviceClass
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
MQTT_HAPREFIX = config.get("mqtt", "haprefix", fallback="")
# [velux]
VLX_HOST = config.get("velux", "host")
VLX_PW = config.get("velux", "password")
# [log]
VERBOSE = config.get("log", "verbose", fallback=False)
KLF200LOG = config.get("log", "klf200", fallback=False)
LOGFILE = config.get("log", "logfile", fallback=None)

APPNAME = "vlxmqttha"

# init logging 
LOGFORMAT = '%(asctime)-15s %(message)s'

if VERBOSE:
    loglevel = logging.DEBUG
else:
    loglevel = logging.INFO

if KLF200LOG:
    pyvlxLogLevel = logging.DEBUG
else:
    pyvlxLogLevel = logging.INFO


if LOGFILE:
    logging.basicConfig(filename=LOGFILE, format=LOGFORMAT, level=loglevel)
else:
    logging.basicConfig(stream=sys.stdout, format=LOGFORMAT, level=loglevel)

logging.info("Starting " + APPNAME)
if VERBOSE:
    logging.info("DEBUG MODE")
else:
    logging.debug("INFO MODE")

PYVLXLOG.setLevel(pyvlxLogLevel)
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(pyvlxLogLevel)
PYVLXLOG.addHandler(ch)

class VeluxMqttCover:
    """
    This class represents the bridge between one MQTT cover device and the actual cover
    
    It is in charge of triggering the registration in MQTT (using Homeassistant AutoDiscovery)
    and forwarding commands and state changes between KLF 200 and MQTT

    Attributes
    ----------
    vlxnode : 
       PyVLX Object to talk to the Cover through KLF 200
    haDevice :
       MQTT representation of the Homeassistant device
    coverDevice :
       MQTT representation of the Homeassistant cover entity
    limitSwitchDevice :
       MQTT representation of the Homeassistant limit switch entity
    """
    def __init__(self, mqttc, vlxnode, mqttid):
        logging.debug("Registering %s to Homeassistant (Type: %s)" % (vlxnode.name, type(vlxnode)))
        self.vlxnode = vlxnode
        self.haDevice = HaDevice(MQTT_HAPREFIX + vlxnode.name, MQTT_HAPREFIX + mqttid)
        self.coverDevice = MqttCover(
            MqttDeviceSettings("", MQTT_HAPREFIX + mqttid, mqttc, self.haDevice),
            self.getHaDeviceClassFromVlxNode(vlxnode))
        self.limitSwitchDevice = MqttSwitch(MqttDeviceSettings("Keep open", MQTT_HAPREFIX + mqttid + "-keepopen", mqttc, self.haDevice))

    def getHaDeviceClassFromVlxNode(self, vlxnode):
        if isinstance(vlxnode, Window):
            return HaDeviceClass.WINDOW
        if isinstance(vlxnode, Blind):
            return HaDeviceClass.BLIND
        if isinstance(vlxnode, Awning):
            return HaDeviceClass.AWNING
        if isinstance(vlxnode, RollerShutter):
            return HaDeviceClass.SHUTTER
        if isinstance(vlxnode, GarageDoor):
            return HaDeviceClass.GARAGE
        if isinstance(vlxnode, Gate):
            return HaDeviceClass.GATE
        if isinstance(vlxnode, Blade):
            return HaDeviceClass.SHADE
        
    async def registerMqttCallbacks(self):
        self.coverDevice.callback_open = self.mqtt_callback_open
        self.coverDevice.callback_close = self.mqtt_callback_close
        self.coverDevice.callback_stop = self.mqtt_callback_stop
        self.coverDevice.callback_position = self.mqtt_callback_position
        self.limitSwitchDevice.callback_on = self.mqtt_callback_keepopen_on
        self.limitSwitchDevice.callback_off = self.mqtt_callback_keepopen_off
        
    def updateNode(self):
        """ Callback for node state changes sent from KLF 200 """
        logging.debug("Updating %s", self.vlxnode.name)

        position = self.vlxnode.position.position_percent
        self.coverDevice.publish_position(position)
        if position < 50:
            self.coverDevice.publish_state('open')
        else:
            self.coverDevice.publish_state('closed')
        
        max_position = self.vlxnode.limitation_max.position
        if max_position < 100:
            self.limitSwitchDevice.publish_state('on')
        else:
            self.limitSwitchDevice.publish_state('off')
    
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

    def mqtt_callback_keepopen_on(self):
        logging.debug("Enable 'keep open' limitation of %s" % (self.vlxnode.name))
        asyncio.run(self.vlxnode.set_position_limitations(position_max=Position(position_percent=0), position_min=Position(position_percent=0)))

    def mqtt_callback_keepopen_off(self):
        logging.debug("Disable 'keep open' limitation of %s" % (self.vlxnode.name))
        asyncio.run((self.vlxnode.clear_position_limitations()))

    def __del__(self):
        logging.debug("Unregistering %s from Homeassistant" % (self.vlxnode.name))
        self.coverDevice.close()


class VeluxMqttHomeassistant:
    """
    This class manages the connections to KLF 200 and MQTT Broker and holds a list
    of all registered device objects

    Attributes
    ----------
    mqttc :
        MQTT client
    pyvlx :
        Object representing KLF 200
    mqttDevices : list<VeluxMqttCover>
        list of all registered devices
    
    
    """
    def __init__(self):
        # MQTT
        MQTT_CLIENT_ID = APPNAME + "_%d" % os.getpid()
        self.mqttc = mqtt.Client(MQTT_CLIENT_ID)
        self.pyvlx = None
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
    
    async def update_device_state(self):
        for vlxnode in self.pyvlx.nodes:
            if isinstance(vlxnode, OpeningDevice):
                await self.pyvlx.get_limitation(vlxnode.node_id)        

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
        LOOP.run_until_complete(veluxMqttHomeassistant.update_device_state())

        LOOP.run_forever()
    except KeyboardInterrupt:
        logging.info("Interrupted by keypress")
    finally:
        del veluxMqttHomeassistant
        os.unlink(pidfile)
    LOOP.close()
    sys.exit(0)
