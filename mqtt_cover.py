"""
MQTT Cover device implementation for Homeassistant autodiscovery.
"""
#  Copyright (c) 2023 Tobias Jaehnel, Ulrich Frank
#  This code is published under the MIT license

import threading
from typing import Callable, Optional

from paho.mqtt.client import Client, MQTTMessage

from ha_mqtt import mqtt_device_base
from ha_mqtt.util import HaCoverDeviceClass
from ha_mqtt.mqtt_device_base import MqttDeviceSettings


class MqttCover(mqtt_device_base.MqttDeviceBase):
    """
    MQTT Cover device for Homeassistant integration.

    Implements cover with open/close/stop/position controls and callbacks
    that execute in separate threads.

    Attributes:
        callback_open: Called when OPEN command received
        callback_close: Called when CLOSE command received
        callback_stop: Called when STOP command received
        callback_position: Called with position (0-100) when position command received
    
    Warning:
        Callbacks run in separate threads. Ensure thread-safety in implementations.
    """

    device_type = "cover"

    def __init__(
        self,
        settings: MqttDeviceSettings,
        device_class: HaCoverDeviceClass,
        inverse_position: bool = False
    ) -> None:
        """Initialize MQTT cover.
        
        Args:
            settings: MQTT device settings
            device_class: Cover device class for Homeassistant
            inverse_position: If True, invert open/close behavior
        """
        # Callbacks that will be set by the application
        self.callback_open: Callable[[], None] = lambda: None
        self.callback_close: Callable[[], None] = lambda: None
        self.callback_stop: Callable[[], None] = lambda: None
        self.callback_position: Callable[[int], None] = lambda position: None
        
        self.command_topic: str = ""
        self.position_topic: str = ""
        self.device_class: HaCoverDeviceClass = device_class
        self.inverse_position: bool = inverse_position

        super().__init__(settings)

    def stop(self) -> None:
        """Unsubscribe and cleanup."""
        if self.command_topic:
            self._client.unsubscribe(self.command_topic)
        super().stop()

    def pre_discovery(self) -> None:
        """Configure MQTT topics and device class for Homeassistant discovery."""
        self.position_topic = f"{self.base_topic}/position"
        self.command_topic = f"{self.base_topic}/set"
        availability_topic = f"{self.base_topic}/available"

        self.add_config_option("position_topic", self.position_topic)
        self.add_config_option("command_topic", self.command_topic)
        self.add_config_option("set_position_topic", self.command_topic)
        self.add_config_option("availability_topic", availability_topic)
        self.add_config_option("payload_available", "online")
        self.add_config_option("payload_not_available", "offline")
        
        if self.inverse_position:
            self.add_config_option("position_open", "100")
            self.add_config_option("position_closed", "0")
        else:
            self.add_config_option("position_open", "0")
            self.add_config_option("position_closed", "100")
        
        self.add_config_option("device_class", self.device_class.value)

        self._client.subscribe(self.command_topic)
        self._client.message_callback_add(self.command_topic, self.command_callback)

    def publish_position(self, position: int, retain: bool = True) -> None:
        """Publish cover position to MQTT.
        
        Args:
            position: Position in percent (0-100)
            retain: Whether to retain the message
        """
        self._logger.debug(f"Publishing position {position}% for {self._unique_id}")
        self._client.publish(self.position_topic, str(position), retain=retain)

    def publish_availability(self, available: bool = True) -> None:
        """Publish device availability status to MQTT.
        
        Args:
            available: True for online, False for offline
        """
        availability_topic = f"{self.base_topic}/available"
        status = "online" if available else "offline"
        self._logger.debug(f"Publishing availability {status} for {self._unique_id}")
        self._client.publish(availability_topic, status, retain=True)

    def command_callback(
        self,
        client: Client,  # pylint: disable=unused-argument
        userdata: object,  # pylint: disable=unused-argument
        msg: MQTTMessage
    ) -> None:
        """Process MQTT commands (OPEN, CLOSE, STOP, or position 0-100).
        
        Args:
            client: MQTT client (unused)
            userdata: User data (unused)
            msg: MQTT message with command payload
        """
        payload = msg.payload
        payload_str = payload.decode("utf-8") if isinstance(payload, bytes) else str(payload)
        self._logger.debug(f"Received command {payload_str} for {self._unique_id}")
        
        try:
            if payload == b'OPEN':
                threading.Thread(
                    target=self.callback_open,
                    name="mqtt_callback_open",
                    daemon=True
                ).start()
            elif payload == b'CLOSE':
                threading.Thread(
                    target=self.callback_close,
                    name="mqtt_callback_close",
                    daemon=True
                ).start()
            elif payload == b'STOP':
                threading.Thread(
                    target=self.callback_stop,
                    name="mqtt_callback_stop",
                    daemon=True
                ).start()
            else:
                # Try to parse as position
                try:
                    position = int(payload)
                    if 0 <= position <= 100:
                        threading.Thread(
                            target=self.callback_position,
                            args=(position,),
                            name="mqtt_callback_position",
                            daemon=True
                        ).start()
                    else:
                        self._logger.error(
                            f"Invalid position {position} for {self._unique_id} (must be 0-100)"
                        )
                except ValueError:
                    self._logger.error(
                        f"Unknown command '{payload.decode('utf-8', errors='replace')}' "
                        f"for {self._unique_id}"
                    )
        except (ValueError, TypeError) as e:
            payload_str = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)
            self._logger.error(
                f"Invalid command payload '{payload_str}' for {self._unique_id}: {e}",
                exc_info=True
            )
        except Exception as e:
            payload_str = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)
            self._logger.exception(
                f"Unexpected error processing command '{payload_str}' for {self._unique_id}"
            )
 