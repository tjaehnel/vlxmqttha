VlxMqttHa - Velux KLF200 to MQTT Bridge using Homeassistant Auto-Discovery
============================================

This python exposes the API of the Velux KLF200 via MQTT. It uses the Homeassistant Auto-Discovery feature to integrate with Homeassistant. This allows controlling io-homecontrol devices e.g. from Velux or Somfy.

There comes already a [KLF200 integration](https://www.home-assistant.io/integrations/velux/) with Homeassistant using the same underlying library [pyvlx](https://github.com/Julius2342/pyvlx.git) to communicate with the KLF200. This brige is an external application that uses MQTT for various reasons:

* It's a bit easier to develop and test than an integration
* You just need to restart that separate application instead of the whole Homeassistant if there are connection issues with the KLF200.
* It's easier to integrate [my own extended version of pyvlx](https://github.com/tjaehnel/pyvlx).
* I don't need to wait or PRs to get accepted and can just use an unmodified Homeassistant

NOTE: For now this integration only supports Cover devices!
It has the following additional features over the default integration:
* Creates one HA device per cover instead of only using entities
* Adds a switch to each entity which keeps the cover open. This is helpful to create automations that prevent closing shutters on open terrace doors.
* When the cover is moving reports "opening" and "closing" state respectively (thanks to https://github.com/TilmanK/pyvlx)

This integration was inpired by https://github.com/nbartels/vlx2mqtt which also offers an MQTT bridge but does not support Homeassistant AutoDiscovery.

Configuration and start
-----------------------
Rename configuration file **vlxmqttha.conf.template** to **vlxmqttha.conf** and change the parameters according to your needs.

Then start the server using

    # python3 ./vlxmqttha.py vlxmqttha.conf


Docker image
------------
In order to run vlxmqttha as a docker container, you can use the provided docker-compose.yml file.
Simply do your configuration in **vlxmqttha.conf** and the execute

     # docker-compose up -d

