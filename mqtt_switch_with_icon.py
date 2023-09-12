"""
this module adds an icon to MQTT switches
"""
#  Copyright (c) 2023 - Tobias Jaehnel
#  This code is published under the MIT license

from ha_mqtt.mqtt_switch import MqttSwitch
from ha_mqtt.mqtt_device_base import MqttDeviceSettings

class MqttSwitchWithIcon (MqttSwitch):
    def __init__(self, settings: MqttDeviceSettings, icon):
        self.icon = icon
        super().__init__(settings)

    def pre_discovery(self):
        self.add_config_option("icon", self.icon)
        super().pre_discovery()
