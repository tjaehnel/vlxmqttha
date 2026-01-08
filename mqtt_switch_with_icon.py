"""
MQTT switch with custom icon for Homeassistant autodiscovery.
"""
#  Copyright (c) 2023 Tobias Jaehnel, Ulrich Frank
#  This code is published under the MIT license

from typing import Optional

from ha_mqtt.mqtt_switch import MqttSwitch
from ha_mqtt.mqtt_device_base import MqttDeviceSettings


class MqttSwitchWithIcon(MqttSwitch):
    """MQTT switch with custom icon support.
    
    Attributes:
        icon: Material Design Icons icon name (e.g., 'mdi:lock-outline')
    """
    
    def __init__(self, settings: MqttDeviceSettings, icon: str) -> None:
        """Initialize MQTT switch with icon.
        
        Args:
            settings: MQTT device settings
            icon: Material Design Icons icon name
        """
        self.icon: str = icon
        super().__init__(settings)

    def pre_discovery(self) -> None:
        """Configure icon and availability for Homeassistant discovery."""
        self.add_config_option("icon", self.icon)
        availability_topic = f"{self.base_topic}/available"
        self.add_config_option("availability_topic", availability_topic)
        self.add_config_option("payload_available", "online")
        self.add_config_option("payload_not_available", "offline")
        super().pre_discovery()

    def publish_availability(self, available: bool = True) -> None:
        """Publish device availability status to MQTT.
        
        Args:
            available: True for online, False for offline
        """
        availability_topic = f"{self.base_topic}/available"
        status = "online" if available else "offline"
        self._client.publish(availability_topic, status, retain=True)
