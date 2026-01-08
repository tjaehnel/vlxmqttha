#!/usr/bin/env python3
"""
KLF200 to MQTT bridge application for Homeassistant integration.
"""
#  Copyright (c) 2023 Tobias Jaehnel, Ulrich Frank
#  This code is published under the MIT license

import os
import sys
import signal
import logging
import configparser
import paho.mqtt.client as mqtt
from paho.mqtt.client import CallbackAPIVersion  # type: ignore[attr-defined]
import argparse
import asyncio
import time
from threading import Semaphore, Lock
from pathlib import Path
from typing import Optional, Dict, Coroutine, Any
from contextlib import asynccontextmanager

from pyvlx import Position, PyVLX, OpeningDevice, Window, Blind, Awning, RollerShutter, GarageDoor, Gate, Blade  # type: ignore[attr-defined]
from pyvlx.log import PYVLXLOG

from ha_mqtt.ha_device import HaDevice
from ha_mqtt.mqtt_device_base import MqttDeviceSettings
from ha_mqtt.util import HaCoverDeviceClass
from mqtt_cover import MqttCover
from mqtt_switch_with_icon import MqttSwitchWithIcon

parser = argparse.ArgumentParser(
    formatter_class=argparse.RawDescriptionHelpFormatter,
    description="Allows to control devices paired with Velux KLF200 via MQTT.\n"
                "Registers the devices to Homeassistant using MQTT Autodiscovery."
)
parser.add_argument('config_file', metavar="<config_file>", help="configuration file")
args = parser.parse_args()

# Configuration loading with validation
def load_config(config_file: str) -> configparser.RawConfigParser:
    """Load and validate configuration file."""
    config = configparser.RawConfigParser(inline_comment_prefixes=('#',))
    files_read = config.read(config_file)
    if not files_read:
        raise FileNotFoundError(f"Config file not found: {config_file}")
    
    # Validate required sections
    required_sections = ["mqtt", "velux"]
    for section in required_sections:
        if not config.has_section(section):
            raise ValueError(f"Missing required config section: [{section}]")
    
    # Validate required options
    required_options = {
        "mqtt": ["host", "port"],
        "velux": ["host", "password"]
    }
    for section, options in required_options.items():
        for option in options:
            if not config.has_option(section, option):
                raise ValueError(f"Missing required option {option} in section [{section}]")
    
    return config

try:
    config = load_config(args.config_file)
except (FileNotFoundError, ValueError) as e:
    logging.error(f"Configuration error: {e}")
    sys.exit(1)

# [mqtt]
MQTT_HOST: str = config.get("mqtt", "host")
MQTT_PORT: int = config.getint("mqtt", "port")
MQTT_LOGIN: Optional[str] = config.get("mqtt", "login", fallback=None)
MQTT_PASSWORD: Optional[str] = config.get("mqtt", "password", fallback=None)
# [homeassistant]
HA_PREFIX: str = config.get("homeassistant", "prefix", fallback="")
HA_INVERT_AWNING: bool = config.getboolean("homeassistant", "invert_awning", fallback=False)
# [velux]
VLX_HOST: str = config.get("velux", "host")
VLX_PW: str = config.get("velux", "password")
# [log]
VERBOSE: bool = config.getboolean("log", "verbose", fallback=False)
KLF200LOG: bool = config.getboolean("log", "klf200", fallback=False)
LOGFILE: Optional[str] = config.get("log", "logfile", fallback=None)
# [restart]
RESTART_INTERVAL: int = config.getint("restart", "restart_interval", fallback=0)
HEALTH_CHECK_INTERVAL: int = config.getint("restart", "health_check_interval", fallback=0)
RESTART_ON_ERROR: bool = config.getboolean("restart", "restart_on_error", fallback=False)

APPNAME = "vlxmqttha"
HEALTH_CHECK_FAILURE_THRESHOLD = 2.0  # Times health check interval

# Logging setup with rotation support
from logging.handlers import RotatingFileHandler

LOGFORMAT = '%(asctime)-15s %(message)s'

loglevel = logging.DEBUG if VERBOSE else logging.INFO
pyvlxLogLevel = logging.DEBUG if KLF200LOG else logging.INFO

if LOGFILE:
    handler = RotatingFileHandler(
        LOGFILE,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5
    )
    handler.setFormatter(logging.Formatter(LOGFORMAT))
    handler.setLevel(loglevel)
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(loglevel)
else:
    logging.basicConfig(stream=sys.stdout, format=LOGFORMAT, level=loglevel)

logging.info(f"Starting {APPNAME}")
logging.debug(f"Configuration loaded: VERBOSE={VERBOSE}, KLF200LOG={KLF200LOG}")

PYVLXLOG.setLevel(pyvlxLogLevel)
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(pyvlxLogLevel)
PYVLXLOG.addHandler(ch)

# Global state management with thread safety
klf_command_semaphore: Semaphore = Semaphore(2)
state_lock: Lock = Lock()

_last_successful_klf_contact: float = time.time()
_last_restart_time: float = time.time()

def call_async_blocking(coroutine: Coroutine[Any, Any, Any]) -> None:
    """Execute async coroutine in blocking manner with semaphore protection."""
    klf_command_semaphore.acquire()
    try:
        future = asyncio.run_coroutine_threadsafe(coroutine, LOOP)
        future.result(timeout=30)  # Add timeout to prevent hanging
    except asyncio.TimeoutError:
        logging.error("KLF200 command timed out after 30 seconds")
    except Exception as e:
        logging.error(f"KLF200 command error: {e}", exc_info=True)
    finally:
        klf_command_semaphore.release()


def trigger_restart() -> None:
    """Trigger application restart by stopping the event loop."""
    logging.info("Stopping event loop to trigger restart")
    LOOP.call_soon_threadsafe(LOOP.stop)


def record_klf_contact() -> None:
    """Record successful contact with KLF200 (thread-safe)."""
    global _last_successful_klf_contact
    with state_lock:
        _last_successful_klf_contact = time.time()

class VeluxMqttCover:
    """
    Bridge between MQTT cover device and actual Velux cover.
    
    Manages registration in MQTT (Homeassistant AutoDiscovery) and forwards
    commands/state changes between KLF200 and MQTT.

    Attributes:
        vlxnode: PyVLX object for cover communication
        mqttc: MQTT client instance
        mqttid: Unique MQTT device identifier
        haDevice: Homeassistant device representation
        coverDevice: MQTT cover entity
        limitSwitchDevice: MQTT limit switch entity
    """
    
    def __init__(self, mqttc: mqtt.Client, vlxnode: OpeningDevice, mqttid: str) -> None:
        logging.debug(f"Registering {vlxnode.name} to Homeassistant (Type: {type(vlxnode).__name__})")
        self.vlxnode: OpeningDevice = vlxnode
        self.mqttc: mqtt.Client = mqttc
        self.mqttid: str = mqttid
        self.haDevice: HaDevice = HaDevice(HA_PREFIX + vlxnode.name, HA_PREFIX + mqttid)
        self.coverDevice: MqttCover = self.makeMqttCover()
        self.limitSwitchDevice: MqttSwitchWithIcon = self.makeMqttKeepOpenSwitch()
        self.last_state: Optional[str] = None  # Track last state to detect changes
    
    def makeMqttCover(self) -> MqttCover:
        """Create MQTT cover device with appropriate device class."""
        return MqttCover(
            MqttDeviceSettings("", HA_PREFIX + self.mqttid, self.mqttc, self.haDevice),
            self.getHaDeviceClassFromVlxNode(self.vlxnode)
        )

    def makeMqttKeepOpenSwitch(self) -> MqttSwitchWithIcon:
        """Create keep-open limitation switch."""
        return MqttSwitchWithIcon(
            MqttDeviceSettings("Keep open", HA_PREFIX + self.mqttid + "-keepopen", self.mqttc, self.haDevice),
            "mdi:lock-outline"
        )

    def getHaDeviceClassFromVlxNode(self, vlxnode: OpeningDevice) -> HaCoverDeviceClass:
        """Map VLX device type to Homeassistant cover device class."""
        device_class_map = {
            Window: HaCoverDeviceClass.WINDOW,
            Blind: HaCoverDeviceClass.BLIND,
            Awning: HaCoverDeviceClass.AWNING,
            RollerShutter: HaCoverDeviceClass.SHUTTER,
            GarageDoor: HaCoverDeviceClass.GARAGE,
            Gate: HaCoverDeviceClass.GATE,
            Blade: HaCoverDeviceClass.SHADE,
        }
        
        for device_type, ha_class in device_class_map.items():
            if isinstance(vlxnode, device_type):
                return ha_class
        
        logging.warning(f"Unknown device type: {type(vlxnode).__name__}, defaulting to NONE")
        return HaCoverDeviceClass.NONE
        
    async def registerMqttCallbacks(self) -> None:
        """Register MQTT command callbacks."""
        # Ensure MQTT topics are initialized
        self.coverDevice.pre_discovery()
        self.limitSwitchDevice.pre_discovery()
        
        # Publish initial online status
        self.coverDevice.publish_availability(True)
        self.limitSwitchDevice.publish_availability(True)
        
        self.coverDevice.callback_open = self.mqtt_callback_open
        self.coverDevice.callback_close = self.mqtt_callback_close
        self.coverDevice.callback_stop = self.mqtt_callback_stop
        self.coverDevice.callback_position = self.mqtt_callback_position
        self.limitSwitchDevice.callback_on = self.mqtt_callback_keepopen_on
        self.limitSwitchDevice.callback_off = self.mqtt_callback_keepopen_off
        
    def updateNode(self) -> None:
        """Callback for node state changes from KLF200."""
        logging.debug(f"Updating {self.vlxnode.name}")
        self.updateCover()
        self.updateLimitSwitch()
        # Publish online status when node is updated
        self.coverDevice.publish_availability(True)
        self.limitSwitchDevice.publish_availability(True)
        
    def updateCover(self) -> None:
        """Update cover state based on VLX node."""
        position = self.vlxnode.position.position_percent
        target_position = self.vlxnode.target_position.position_percent
        
        # Ensure position is in valid range (0-100)
        # If out of range, use current position as fallback
        if position < 0 or position > 100:
            logging.warning(f"{self.vlxnode.name}: Invalid position {position}, using fallback")
            position = 0
            
        if target_position < 0 or target_position > 100:
            logging.debug(f"{self.vlxnode.name}: Invalid target position {target_position}, assuming stopped at {position}")
            target_position = position

        self.coverDevice.publish_position(position)
        
        # Determine state based on position and target position
        # First check if we're at the target position (stopped)
        if position == target_position:
            # Device has stopped - determine final state
            if position == 100:
                mqtt_state = "closed"
            else:
                mqtt_state = "open"
        # Then check if we're moving towards a target
        elif target_position < position:
            mqtt_state = "opening"
        elif target_position > position:
            mqtt_state = "closing"
        else:
            # Fallback (shouldn't reach here)
            mqtt_state = "open"
        
        # Always update state to ensure it reflects current conditions
        # This prevents state from getting stuck
        self.coverDevice.update_state(mqtt_state)
        if mqtt_state != self.last_state:
            self.last_state = mqtt_state
            logging.debug(f"{self.vlxnode.name}: position={position}%, target={target_position}%, state={mqtt_state}")
        else:
            logging.debug(f"{self.vlxnode.name}: position={position}%, target={target_position}%, state={mqtt_state} (unchanged)")

    def updateLimitSwitch(self) -> None:
        """Update keep-open switch state."""
        try:
            # limitation_max shows the upper limit position
            # If limitation is set (not IgnorePosition), switch is 'on'
            max_position = self.vlxnode.limitation_max.position_percent
            self.limitSwitchDevice.update_state('on' if max_position < 100 else 'off')
        except (AttributeError, ValueError):
            # If limitation_max is not properly set, assume fully open
            logging.debug(f"limitation_max not available or invalid for {self.vlxnode.name}")
            self.limitSwitchDevice.update_state('off')
                
    def mqtt_callback_open(self) -> None:
        """Handle MQTT open command."""
        logging.debug(f"Opening {self.vlxnode.name}")
        call_async_blocking(self.vlxnode.open(wait_for_completion=False))  # type: ignore[no-untyped-call]

    def mqtt_callback_close(self) -> None:
        """Handle MQTT close command."""
        logging.debug(f"Closing {self.vlxnode.name}")
        call_async_blocking(self.vlxnode.close(wait_for_completion=False))  # type: ignore[no-untyped-call]

    def mqtt_callback_stop(self) -> None:
        """Handle MQTT stop command."""
        logging.debug(f"Stopping {self.vlxnode.name}")
        call_async_blocking(self.vlxnode.stop(wait_for_completion=False))  # type: ignore[no-untyped-call]

    def mqtt_callback_position(self, position: int) -> None:
        """Handle MQTT position command."""
        logging.debug(f"Moving {self.vlxnode.name} to position {position}%")
        call_async_blocking(
            self.vlxnode.set_position(  # type: ignore[no-untyped-call]
                Position(position_percent=int(position)),  # type: ignore[no-untyped-call]
                wait_for_completion=False
            )
        )

    def mqtt_callback_keepopen_on(self) -> None:
        """Enable keep-open limitation."""
        logging.debug(f"Enable 'keep open' limitation of {self.vlxnode.name}")
        try:
            call_async_blocking(
                self.vlxnode.set_position_limitations(  # type: ignore[no-untyped-call]
                    position_min=Position(position_percent=0),  # type: ignore[no-untyped-call]
                    position_max=Position(position_percent=0)  # type: ignore[no-untyped-call]
                )
            )
        except Exception as e:
            logging.warning(f"Failed to set position limitations: {e}")

    def mqtt_callback_keepopen_off(self) -> None:
        """Disable keep-open limitation."""
        logging.debug(f"Disable 'keep open' limitation of {self.vlxnode.name}")
        try:
            call_async_blocking(self.vlxnode.clear_position_limitations())  # type: ignore[no-untyped-call]
        except Exception as e:
            logging.warning(f"Failed to clear position limitations: {e}")

    def close(self) -> None:
        """Properly close and cleanup device."""
        try:
            self.coverDevice.stop()
        except Exception as e:
            logging.error(f"Error closing cover device {self.vlxnode.name}: {e}", exc_info=True)

    def stop(self) -> None:
        """Alias for close() for compatibility."""
        self.close()

    def __del__(self) -> None:
        """Cleanup on deletion."""
        try:
            self.close()
        except Exception:
            pass  # Avoid exceptions in __del__

class VeluxMqttCoverInverted(VeluxMqttCover):
    """Inverted cover (e.g., awnings that work opposite to shutters)."""
    
    def makeMqttCover(self) -> MqttCover:
        """Create MQTT cover with inverted position."""
        return MqttCover(
            MqttDeviceSettings("", HA_PREFIX + self.mqttid, self.mqttc, self.haDevice),
            self.getHaDeviceClassFromVlxNode(self.vlxnode),
            True
        )

    def mqtt_callback_open(self) -> None:
        """Handle inverted open (closes device)."""
        logging.debug(f"Opening {self.vlxnode.name} (inverted)")
        call_async_blocking(self.vlxnode.close(wait_for_completion=False))  # type: ignore[no-untyped-call]

    def mqtt_callback_close(self) -> None:
        """Handle inverted close (opens device)."""
        logging.debug(f"Closing {self.vlxnode.name} (inverted)")
        call_async_blocking(self.vlxnode.open(wait_for_completion=False))  # type: ignore[no-untyped-call]

    def updateCover(self) -> None:
        """Update cover with inverted state."""
        position = self.vlxnode.position.position_percent
        target_position = self.vlxnode.target_position.position_percent
        
        # Ensure position is in valid range (0-100)
        # If out of range, use current position as fallback
        if position < 0 or position > 100:
            logging.warning(f"{self.vlxnode.name}: Invalid position {position}, using fallback")
            position = 0
            
        if target_position < 0 or target_position > 100:
            logging.debug(f"{self.vlxnode.name}: Invalid target position {target_position}, assuming stopped at {position}")
            target_position = position

        self.coverDevice.publish_position(position)
        
        # Determine state based on position and target position (inverted logic)
        # First check if we're at the target position (stopped)
        if position == target_position:
            # Device has stopped - determine final state (inverted)
            if position == 0:
                mqtt_state = "closed"
            else:
                mqtt_state = "open"
        # Then check if we're moving towards a target (inverted)
        elif target_position < position:
            mqtt_state = "closing"
        elif target_position > position:
            mqtt_state = "opening"
        else:
            # Fallback (shouldn't reach here)
            mqtt_state = "open"
        
        # Always update state to ensure it reflects current conditions
        self.coverDevice.update_state(mqtt_state)
        if mqtt_state != self.last_state:
            self.last_state = mqtt_state
            logging.debug(f"{self.vlxnode.name} (inverted): position={position}%, target={target_position}%, state={mqtt_state}")
        else:
            logging.debug(f"{self.vlxnode.name} (inverted): position={position}%, target={target_position}%, state={mqtt_state} (unchanged)")




class VeluxMqttHomeassistant:
    """
    Manages connections to KLF200 and MQTT broker.
    
    Attributes:
        mqttc: MQTT client
        pyvlx: PyVLX instance for KLF200 communication
        mqttDevices: Dictionary of registered MQTT devices
    """
    
    def __init__(self) -> None:
        mqtt_client_id = f"{APPNAME}_{os.getpid()}"
        self.mqttc: mqtt.Client = mqtt.Client(CallbackAPIVersion.VERSION2, mqtt_client_id)
        self.pyvlx: Optional[PyVLX] = None
        self.mqttDevices: Dict[str, VeluxMqttCover] = {}

    async def connect_mqtt(self, max_retries: int = 10) -> None:
        """Connect to MQTT broker with exponential backoff retry."""
        logging.debug(f"Connecting to MQTT broker: {MQTT_HOST}:{MQTT_PORT}")
        
        if MQTT_LOGIN:
            logging.debug(f"  Login: {MQTT_LOGIN}")
            self.mqttc.username_pw_set(MQTT_LOGIN, MQTT_PASSWORD)

        for attempt in range(max_retries):
            try:
                result = self.mqttc.connect(MQTT_HOST, MQTT_PORT, 60)
                if result == 0:
                    self.mqttc.loop_start()
                    await asyncio.sleep(1)
                    logging.info("Connected to MQTT broker")
                    return
                else:
                    logging.warning(f"MQTT connection attempt {attempt + 1}: error code {result}")
            except Exception as e:
                logging.warning(f"MQTT connection attempt {attempt + 1} failed: {e}")
            
            if attempt < max_retries - 1:
                wait_time = 10 * (attempt + 1)  # Exponential backoff
                logging.info(f"Retrying MQTT connection in {wait_time} seconds")
                await asyncio.sleep(wait_time)
        
        raise ConnectionError(f"Failed to connect to MQTT after {max_retries} attempts")

    async def connect_klf200(self, loop: asyncio.AbstractEventLoop) -> None:
        """Connect to KLF200 gateway."""
        logging.debug(f"Connecting to KLF200: {VLX_HOST}")
        self.pyvlx = PyVLX(host=VLX_HOST, password=VLX_PW, loop=loop)  # type: ignore[no-untyped-call]
        await self.pyvlx.load_nodes()  # type: ignore[union-attr,no-untyped-call]
        record_klf_contact()

        logging.info(f"Connected to KLF200, found {len(self.pyvlx.nodes)} nodes")  # type: ignore[union-attr]
        for node in self.pyvlx.nodes:  # type: ignore[union-attr]
            logging.debug(f"  - {node.name}")

    async def register_devices(self) -> None:
        """Register all VLX devices as MQTT devices."""
        if not self.pyvlx:
            logging.error("PyVLX not initialized")
            return
        
        for vlxnode in self.pyvlx.nodes:  # type: ignore[attr-defined]
            if isinstance(vlxnode, OpeningDevice):
                vlxnode.register_device_updated_cb(self.vlxnode_callback)  # type: ignore[no-untyped-call]
                mqttid = self.generate_id(vlxnode)
                
                if isinstance(vlxnode, Awning) and HA_INVERT_AWNING:
                    cover: VeluxMqttCover = VeluxMqttCoverInverted(self.mqttc, vlxnode, mqttid)
                else:
                    cover = VeluxMqttCover(self.mqttc, vlxnode, mqttid)
                
                self.mqttDevices[mqttid] = cover
                await cover.registerMqttCallbacks()
                logging.debug(f"Watching: {vlxnode.name}")
                
                # Initialize target position to current position to ensure valid state
                # This prevents showing "closing" with an invalid target position
                try:
                    await vlxnode.stop(wait_for_completion=False)  # type: ignore[no-untyped-call]
                except Exception as e:
                    logging.debug(f"Could not initialize {vlxnode.name} with stop: {e}")
    
    async def update_device_state(self) -> None:
        """Request initial state of all devices."""
        if not self.pyvlx:
            return
        for vlxnode in self.pyvlx.nodes:  # type: ignore[attr-defined]
            if isinstance(vlxnode, OpeningDevice):
                await self.pyvlx.get_limitation(vlxnode.node_id)  # type: ignore[union-attr,no-untyped-call]        

    async def vlxnode_callback(self, vlxnode: OpeningDevice) -> None:
        """Handle VLX node state update."""
        logging.debug(f"{vlxnode.name}: {vlxnode.position.position_percent}%")
        record_klf_contact()
        mqttid = self.generate_id(vlxnode)
        mqttDevice = self.mqttDevices.get(mqttid)
        if mqttDevice:
            mqttDevice.updateNode()

    def generate_id(self, vlxnode: OpeningDevice) -> str:
        """Generate unique MQTT ID from VLX node name."""
        umlauts = {ord('ä'): 'ae', ord('ü'): 'ue', ord('ö'): 'oe', ord('ß'): 'ss'}
        node_name = str(vlxnode.name)  # type: ignore[union-attr]
        return "vlx-" + node_name.replace(" ", "-").lower().translate(umlauts)

    def close(self) -> None:
        """Properly close all connections."""
        for device in list(self.mqttDevices.values()):
            try:
                device.stop()
            except Exception as e:
                logging.error(f"Error closing device: {e}", exc_info=True)
        
        self.mqttDevices.clear()
        
        try:
            self.mqttc.disconnect()
            self.mqttc.loop_stop()
            logging.info("Disconnected from MQTT broker")
        except Exception as e:
            logging.error(f"Error disconnecting from MQTT: {e}", exc_info=True)
        
        if self.pyvlx:
            try:
                if not LOOP.is_closed():
                    LOOP.run_until_complete(self.pyvlx.disconnect())  # type: ignore[no-untyped-call]
                logging.info("Disconnected from KLF200")
            except Exception as e:
                logging.error(f"Error disconnecting from KLF200: {e}", exc_info=True)

    def __del__(self) -> None:
        """Cleanup on deletion."""
        try:
            self.close()
        except Exception:
            pass  # Avoid exceptions in __del__


async def state_update_task(homeassistant_instance: VeluxMqttHomeassistant) -> None:
    """Periodically update device states from KLF200."""
    STATE_UPDATE_INTERVAL = 5  # Update state every 5 seconds
    
    logging.info(f"State update task enabled: every {STATE_UPDATE_INTERVAL} seconds")
    
    while True:
        try:
            await asyncio.sleep(STATE_UPDATE_INTERVAL)
            
            # Update state for all registered devices
            for mqttid, mqttDevice in homeassistant_instance.mqttDevices.items():
                try:
                    mqttDevice.updateNode()
                except Exception as e:
                    logging.debug(f"Error updating state for {mqttDevice.vlxnode.name}: {e}")
        except asyncio.CancelledError:
            logging.info("State update task cancelled")
            break
        except Exception as e:
            logging.error(f"Error in state update task: {e}", exc_info=True)


async def health_check_task() -> None:
    """Periodically check KLF200 health and trigger restart if necessary."""
    if HEALTH_CHECK_INTERVAL <= 0:
        return
    
    logging.info(f"Health check enabled: every {HEALTH_CHECK_INTERVAL} seconds")
    
    while True:
        try:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            
            with state_lock:
                time_since_contact = time.time() - _last_successful_klf_contact
            
            threshold = HEALTH_CHECK_INTERVAL * HEALTH_CHECK_FAILURE_THRESHOLD
            if time_since_contact > threshold:
                logging.warning(
                    f"KLF200 health check failed: No contact for {time_since_contact:.0f} seconds "
                    f"(threshold: {threshold:.0f} seconds)"
                )
                
                if RESTART_ON_ERROR:
                    logging.warning("Triggering automatic restart due to health check failure")
                    trigger_restart()
                    break
        except asyncio.CancelledError:
            logging.info("Health check task cancelled")
            break
        except Exception as e:
            logging.error(f"Error in health check task: {e}", exc_info=True)


async def restart_interval_task() -> None:
    """Periodically restart the application."""
    if RESTART_INTERVAL <= 0:
        return
    
    restart_seconds = RESTART_INTERVAL * 3600
    logging.info(f"Periodic restart enabled: every {RESTART_INTERVAL} hours ({restart_seconds} seconds)")
    
    while True:
        try:
            await asyncio.sleep(restart_seconds)
            
            with state_lock:
                time_since_last_restart = time.time() - _last_restart_time
            
            logging.info(f"Triggering periodic restart (uptime: {time_since_last_restart/3600:.1f} hours)")
            trigger_restart()
            break
        except asyncio.CancelledError:
            logging.info("Restart interval task cancelled")
            break
        except Exception as e:
            logging.error(f"Error in restart interval task: {e}", exc_info=True)

def get_pid_file_path() -> Path:
    """Get platform-appropriate PID file path."""
    if sys.platform == "win32":
        pid_dir = Path(os.environ.get("TEMP", "."))
    else:
        var_run = Path("/var/run")
        pid_dir = var_run if var_run.exists() and os.access(var_run, os.W_OK) else Path(tempfile.gettempdir())
    
    return pid_dir / f"{APPNAME}.pid"


# Use the signal module to handle signals
def signal_handler(signum: int, frame: Any) -> None:
    """Handle termination signals gracefully."""
    logging.info(f"Received signal {signum}, shutting down")
    if LOOP:
        LOOP.stop()


signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

if __name__ == '__main__':
    import tempfile
    
    try:
        LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(LOOP)

        pid_file_path = get_pid_file_path()
        
        # Check if already running
        if pid_file_path.exists():
            try:
                existing_pid = int(pid_file_path.read_text().strip())
                # Check if process is actually running
                os.kill(existing_pid, 0)
                logging.error(f"Application already running with PID {existing_pid}")
                sys.exit(1)
            except (ProcessLookupError, ValueError):
                # Process not running or invalid PID, remove stale file
                pid_file_path.unlink(missing_ok=True)
        
        # Write PID file
        try:
            pid_file_path.write_text(str(os.getpid()))
            logging.info(f"PID file created: {pid_file_path}")
        except Exception as e:
            logging.error(f"Failed to write PID file: {e}")
            sys.exit(1)

        # Initialize application
        veluxMqttHomeassistant = VeluxMqttHomeassistant()
        health_check_task_obj: Optional[asyncio.Task[None]] = None
        restart_interval_task_obj: Optional[asyncio.Task[None]] = None
        state_update_task_obj: Optional[asyncio.Task[None]] = None
        
        try:
            LOOP.run_until_complete(veluxMqttHomeassistant.connect_mqtt())
            LOOP.run_until_complete(veluxMqttHomeassistant.connect_klf200(LOOP))
            LOOP.run_until_complete(veluxMqttHomeassistant.register_devices())
            LOOP.run_until_complete(veluxMqttHomeassistant.update_device_state())

            # Create background tasks for health check and restart interval
            health_check_task_obj = asyncio.ensure_future(health_check_task())
            restart_interval_task_obj = asyncio.ensure_future(restart_interval_task())
            state_update_task_obj = asyncio.ensure_future(state_update_task(veluxMqttHomeassistant))

            if RESTART_INTERVAL > 0:
                logging.info(f"Scheduled restart every {RESTART_INTERVAL} hours")
            if HEALTH_CHECK_INTERVAL > 0:
                logging.info(f"Health check enabled every {HEALTH_CHECK_INTERVAL} seconds")

            logging.info("Application started successfully, entering main loop")
            LOOP.run_forever()
            
        except ConnectionError as e:
            logging.error(f"Connection failed: {e}")
            sys.exit(1)
        except Exception as e:
            logging.error(f"Application error: {e}", exc_info=True)
            sys.exit(1)
        finally:
            # Cancel background tasks
            for task in [health_check_task_obj, restart_interval_task_obj, state_update_task_obj]:
                if task and not task.done():
                    task.cancel()
            
            # Cleanup
            try:
                veluxMqttHomeassistant.close()
            except Exception as e:
                logging.error(f"Error during cleanup: {e}", exc_info=True)
            
            # Remove PID file
            try:
                pid_file_path.unlink(missing_ok=True)
                logging.info("PID file removed")
            except Exception as e:
                logging.error(f"Error removing PID file: {e}")
                
    except KeyboardInterrupt:
        logging.info("Interrupted by user")
    except Exception as e:
        logging.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        LOOP.close()
        sys.exit(0)
