"""
this module contains all code for MQTT covers
"""
#  Copyright (c) 2023 - Tobias Jaehnel
#  This code is published under the MIT license

import threading
import time

from paho.mqtt.client import Client, MQTTMessage

from ha_mqtt import mqtt_device_base
from ha_mqtt.util import HaDeviceClass
from ha_mqtt.mqtt_device_base import MqttDeviceSettings


class MqttCover(mqtt_device_base.MqttDeviceBase):
    """
    MQTT Cover class.
    Implements a binary switch, that knows the states ON and OFF

    Usage:
    assign custom functions to the `callback_on` and `callback_off` members.
    These functions get executed in a separate thread once the according payload was received

    .. attention::
       Each callback spawns a new thread which is automatically destroyed once the function finishes.
       Be aware of that if you do non threadsafe stuff in your callbacks
    """

    device_type = "cover"
    #initial_state = util.OFF

    def __init__(self, settings: MqttDeviceSettings, device_class: HaDeviceClass, inverse_position : bool = False):
        # internal tracker of the state
        #self.state: bool = self.__class__.initial_state

        # callback executed when an OPEN command is received via MQTT
        self.callback_open = lambda: None
        # callback executed when a CLOSE command is received via MQTT
        self.callback_close = lambda: None
        # callback executed when a STOP command is received via MQTT
        self.callback_stop = lambda: None
        # callback executed when an opening position is received via MQTT
        self.callback_position = lambda: None
        
        self.command_topic = ""
        self.position_topic = ""

        self.device_class = device_class
        self.inverse_position = inverse_position

        super().__init__(settings)

    def close(self):
        self._client.unsubscribe(self.command_topic)
        super().close()

    def pre_discovery(self):
        self.position_topic = f"{self.base_topic}/position"
        self.command_topic = f"{self.base_topic}/set"

        self.add_config_option("position_topic", self.position_topic)
        self.add_config_option("command_topic", self.command_topic)
        self.add_config_option("set_position_topic", self.command_topic)
        if self.inverse_position:
            self.add_config_option("position_open", 100)
            self.add_config_option("position_closed", 0)
        else:
            self.add_config_option("position_open", 0)
            self.add_config_option("position_closed", 100)
        self.add_config_option("device_class", self.device_class.value)

        self._client.subscribe(self.command_topic)
        self._client.message_callback_add(self.command_topic, self.command_callback)

    def publish_position(self, position: int, retain: bool = True):
        self._logger.debug("publishing position '%s' for %s", position, self._unique_id)

        self._client.publish(self.position_topic, position, retain=retain)
        time.sleep(0.01)


    def command_callback(self, client: Client, userdata: object, msg: MQTTMessage):  # pylint: disable=W0613
        """
        callback that is executed when a message on the *command* channel is received

        :param client: client who received the message
        :param userdata: user defined data of any type that is passed as the userdata parameter to callbacks.
          It may be updated at a later point with the user_data_set() function.
        :param msg: actual message sent

        """

        self._logger.debug("Received command '%s' for %s: ", str(msg.payload), self._unique_id)
        try:
            if(msg.payload == b'OPEN'):
                threading.Thread(target=self.callback_open, name="callback_thread").start()
            elif(msg.payload == b'CLOSE'):
                threading.Thread(target=self.callback_close, name="callback_thread").start()
            elif(msg.payload == b'STOP'):
                threading.Thread(target=self.callback_stop, name="callback_thread").start()
            elif int(msg.payload) >= 0:
                threading.Thread(target=self.callback_position, name="callback_thread", args=[int(msg.payload)]).start()
            else:
                self._logger.error("Unknown command '%s' for %s", str(msg.payload), self._unique_id)
        except Exception as e:
            self._logger.error("Exception while processing received command '%s' for %s: ", str(msg.payload), self._unique_id, e)
 