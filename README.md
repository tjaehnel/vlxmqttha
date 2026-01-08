VlxMqttHa - Velux KLF200 to MQTT Bridge using Homeassistant Auto-Discovery
============================================

This python exposes the API of the Velux KLF200 via MQTT. It uses the Homeassistant Auto-Discovery feature to integrate with Homeassistant. This allows controlling io-homecontrol devices e.g. from Velux or Somfy.

There comes already a [KLF200 integration](https://www.home-assistant.io/integrations/velux/) with Homeassistant using the same underlying library [pyvlx](https://github.com/Julius2342/pyvlx.git) to communicate with the KLF200. This brige is an external application that uses MQTT for various reasons:

* It's a bit easier to develop and test than an integration
* You just need to restart that separate application instead of the whole Homeassistant if there are connection issues with the KLF200.
* It's easier to integrate [my own extended version of pyvlx](https://github.com/tjaehnel/pyvlx).
* I don't need to wait or PRs to get accepted and can just use an unmodified Homeassistant

NOTE: For now this integration only supports Cover devices!

## Features
It has the following additional features over the default integration:
* Creates one HA device per cover instead of only using entities
* Adds a switch to each entity which keeps the cover open. This is helpful to create automations that prevent closing shutters on open terrace doors.
* When the cover is moving reports "opening" and "closing" state respectively (thanks to https://github.com/TilmanK/pyvlx)
* **Automatic health monitoring and restart**: Ensures the application stays operational by:
  * Monitoring KLF200 connection health at regular intervals
  * Automatically restarting on connection loss (configurable)
  * Periodic full restart to recover from connection issues (configurable)

This integration was inspired by https://github.com/nbartels/vlx2mqtt which also offers an MQTT bridge but does not support Homeassistant AutoDiscovery.

Configuration and start
-----------------------

### Configuration File

Rename the configuration file **vlxmqttha.conf.template** to **vlxmqttha.conf** and adjust the parameters according to your needs.

#### Configuration Sections

**[mqtt]**
* `host` - IP address of MQTT server
* `port` - Port of MQTT server
* `login` - Login name (optional)
* `password` - Password (optional)

**[homeassistant]**
* `prefix` - Prefix for all device names (optional, e.g. "DEV-")
* `invert_awning` - Invert positions and states for awnings (optional, default: false)

**[velux]**
* `host` - IP address of KLF200 gateway
* `password` - WiFi password for KLF200 API access

**[log]**
* `verbose` - Enable DEBUG level logging (optional, default: false)
* `klf200` - Enable KLF200 API communication logging (optional, default: false)
* `logfile` - Write logs to file instead of stdout (optional)

**[restart]** (Optional - for automatic restart feature)
* `restart_interval` - Auto restart after N hours (0 = disabled, default: 0)
* `health_check_interval` - Check KLF200 health every N seconds (0 = disabled, default: 0)
* `restart_on_error` - Auto restart on connection error (optional, default: false)

### Running the application

Start the server directly using:

```bash
python3 ./vlxmqttha.py vlxmqttha.conf
```


Docker image
------------

### Docker Compose

In order to run vlxmqttha as a docker container, you can use the provided docker-compose.yml file.
Configure your **vlxmqttha.conf** and then execute:

```bash
docker-compose up -d
```

View logs:

```bash
docker-compose logs -f vlxmqttha
```

Stop the container:

```bash
docker-compose down
```

### Auto-Restart in Docker

To enable automatic restart on application failure, ensure your docker-compose.yml includes:

```yaml
restart: unless-stopped
```

The application will automatically restart if the health check detects connection issues (when configured).


MQTT Topics and Messages
------------------------

### Auto-Discovery
The application publishes Homeassistant auto-discovery configurations for each device:
* `homeassistant/cover/{prefix}{device-id}/config` - Cover device configuration
* `homeassistant/switch/{prefix}{device-id}-keepopen/config` - Keep-open switch configuration

### State Publishing
Each cover publishes its current state (with retain flag):
* `{prefix}{device-id}/state` - States: `"open"`, `"closed"`, `"opening"`, `"closing"`
* `{prefix}{device-id}/position` - Position: `0-100` (percentage)

Keep-open switch:
* `{prefix}{device-id}-keepopen/state` - States: `"on"` (limited), `"off"` (unlimited)

### Commands (Subscribe)
Commands received from MQTT:
* `{prefix}{device-id}/set` - Commands: `OPEN`, `CLOSE`, `STOP`, or `0-100` (position)
* `{prefix}{device-id}-keepopen/set` - Commands: `ON` (limit), `OFF` (no limit)


Development
-----------

### Prerequisites
* Python 3.7+
* pip and venv

### Setting up the development environment

#### Step 1: Clone the repository with submodules

```bash
git clone --recurse-submodules https://github.com/tjaehnel/vlxmqttha.git
cd vlxmqttha
```

If you already cloned without submodules, initialize them:

```bash
git submodule update --init --recursive
```

#### Step 2: Create and activate a virtual environment

**macOS/Linux:**

```bash
python3 -m venv venv
source venv/bin/activate
```

**Windows:**

```bash
python3 -m venv venv
venv\Scripts\activate
```

#### Step 3: Install dependencies

```bash
pip install -r requirements.txt
```

### Working with the pyvlx submodule

The project includes a custom fork of pyvlx with extended features. The submodule points to `https://github.com/tjaehnel/pyvlx` on the `master_vlxmqttha` branch.

#### Updating the submodule

```bash
git submodule update --remote
```

#### Installing your local pyvlx version for testing

To test local changes to pyvlx without committing to GitHub, install in editable mode:

```bash
pip install -e mod/pyvlx
```

This installs the package in editable mode, so changes are immediately reflected without reinstalling.

### Running the application in development

1. Ensure your venv is activated
2. Update your configuration file if needed
3. Run the application:

```bash
python3 vlxmqttha.py vlxmqttha.conf
```

#### Enable debug logging

Create or modify **vlxmqttha.conf** and add:

```ini
[log]
verbose = true
klf200 = true
logfile = vlxmqttha.log
```

### Testing

#### Monitor MQTT topics (in another terminal)

```bash
# Subscribe to all vlxmqttha topics
mosquitto_sub -h localhost -t "vlx-*/#"

# Or with prefix
mosquitto_sub -h localhost -t "DEV-vlx-*/#"
```

#### Test publishing commands

```bash
# Open a cover (0% position)
mosquitto_pub -h localhost -t "vlx-my-cover/set" -m "OPEN"

# Close a cover (100% position)
mosquitto_pub -h localhost -t "vlx-my-cover/set" -m "CLOSE"

# Move to specific position (50%)
mosquitto_pub -h localhost -t "vlx-my-cover/set" -m "50"

# Enable keep-open limitation
mosquitto_pub -h localhost -t "vlx-my-cover-keepopen/set" -m "ON"
```
