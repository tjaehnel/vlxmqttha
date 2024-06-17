#!/usr/bin/env python3
import os
import sys
import signal
import logging
import configparser
import paho.mqtt.client as mqtt
import argparse
import asyncio
from threading import Semaphore
from pyvlx import Position, PyVLX, OpeningDevice, Window, Blind, Awning, RollerShutter, GarageDoor, Gate, Blade
from pyvlx.log import PYVLXLOG

from ha_mqtt.ha_device import HaDevice
from ha_mqtt.mqtt_device_base import MqttDeviceSettings
from ha_mqtt.util import HaDeviceClass
from mqtt_cover import MqttCover
from mqtt_switch_with_icon import MqttSwitchWithIcon

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
# [homeassistant]
HA_PREFIX = config.get("homeassistant", "prefix", fallback="")
HA_INVERT_AWNING = config.get("homeassistant", "invert_awning", fallback=False)
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

# define a semaphore as it seems the KLF200 can only handle 2 commands
# at the same time and ignores the third one sent
klf_command_semaphore = Semaphore(2)

def call_async_blocking(coroutine):
    klf_command_semaphore.acquire()
    try:
        future = asyncio.run_coroutine_threadsafe(coroutine, LOOP)
        future.result()
    except Exception as e:
        logging.error(str(e))
    klf_command_semaphore.release()


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
        self.mqttc = mqttc
        self.mqttid = mqttid
        self.haDevice = HaDevice(HA_PREFIX + vlxnode.name, HA_PREFIX + mqttid)
        self.coverDevice = self.makeMqttCover()
        self.limitSwitchDevice = self.makeMqttKeepOpenSwitch()
    
    def makeMqttCover(self):
        return MqttCover(
            MqttDeviceSettings("", HA_PREFIX + self.mqttid, self.mqttc, self.haDevice),
            self.getHaDeviceClassFromVlxNode(self.vlxnode)
        )

    def makeMqttKeepOpenSwitch(self):
        return MqttSwitchWithIcon(
            MqttDeviceSettings("Keep open", HA_PREFIX + self.mqttid + "-keepopen", self.mqttc, self.haDevice),
            "mdi:lock-outline"
        )

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

        self.updateCover()
        self.updateLimitSwitch()
        
    def updateCover(self):
        position = self.vlxnode.position.position_percent
        target_position = self.vlxnode.target_position.position_percent

        mqtt_state = ""
        self.coverDevice.publish_position(position)
        if target_position < position:
            mqtt_state = "opening"
        elif target_position > position:
            mqtt_state = "closing"
        elif position == 100:
            mqtt_state = "closed"
        else:
            mqtt_state = "open"
        
        self.coverDevice.publish_state(mqtt_state)

    def updateLimitSwitch(self):
        max_position = self.vlxnode.limitation_max.position
        if max_position < 100:
            self.limitSwitchDevice.publish_state('on')
        else:
            self.limitSwitchDevice.publish_state('off')
                
    def mqtt_callback_open(self):
        logging.debug("Opening %s", self.vlxnode.name)
        call_async_blocking(self.vlxnode.open(wait_for_completion=False))

    def mqtt_callback_close(self):
        logging.debug("Closing %s", self.vlxnode.name)
        call_async_blocking(self.vlxnode.close(wait_for_completion=False))

    def mqtt_callback_stop(self):
        logging.debug("Stopping %s", self.vlxnode.name)
        call_async_blocking(self.vlxnode.stop(wait_for_completion=False))

    def mqtt_callback_position(self, position):
        logging.debug("Moving %s to position %s" % (self.vlxnode.name, position))
        call_async_blocking(self.vlxnode.set_position(Position(position_percent=int(position)), wait_for_completion=False))

    def mqtt_callback_keepopen_on(self):
        logging.debug("Enable 'keep open' limitation of %s" % (self.vlxnode.name))
        call_async_blocking(self.vlxnode.set_position_limitations(position_max=Position(position_percent=0), position_min=Position(position_percent=0)))

    def mqtt_callback_keepopen_off(self):
        logging.debug("Disable 'keep open' limitation of %s" % (self.vlxnode.name))
        call_async_blocking((self.vlxnode.clear_position_limitations()))

    def __del__(self):
        logging.debug("Unregistering %s from Homeassistant" % (self.vlxnode.name))
        self.coverDevice.close()

class VeluxMqttCoverInverted (VeluxMqttCover):
    def __init__(self, mqttc, vlxnode, mqttid):
        super().__init__(mqttc, vlxnode, mqttid)
    
    def makeMqttCover(self):
        return MqttCover(
            MqttDeviceSettings("", HA_PREFIX + self.mqttid, self.mqttc, self.haDevice),
            self.getHaDeviceClassFromVlxNode(self.vlxnode),
            True
        )

    def mqtt_callback_open(self):
        logging.debug("Opening %s", self.vlxnode.name)
        call_async_blocking(self.vlxnode.close(wait_for_completion=False))

    def mqtt_callback_close(self):
        logging.debug("Closing %s", self.vlxnode.name)
        call_async_blocking(self.vlxnode.open(wait_for_completion=False))

    def updateCover(self):
        position = self.vlxnode.position.position_percent
        target_position = self.vlxnode.target_position.position_percent

        mqtt_state = ""
        self.coverDevice.publish_position(position)
        if target_position < position:
            mqtt_state = "closing"
        elif target_position > position:
            mqtt_state = "opening"
        elif position == 0:
            mqtt_state = "closed"
        else:
            mqtt_state = "open"
        
        self.coverDevice.publish_state(mqtt_state)




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
                mqttCover = None
                if isinstance(vlxnode, Awning) and HA_INVERT_AWNING == True:
                    mqttCover = VeluxMqttCoverInverted(self.mqttc, vlxnode, mqttid)
                else:
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
        umlauts = {ord('ä'):'ae', ord('ü'):'ue', ord('ö'):'oe', ord('ß'):'ss'}
        return "vlx-" + vlxnode.name.replace(" ", "-").lower().replace.translate(umlauts)

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
