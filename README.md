# Mesh Alerts

Mesh Alerts sends Ukrainian air alert messages to Meshtastic nodes.

## What it does

- A Mac connects to a ThinkNode M7 (stock Meshtastic firmware) over USB serial or over TCP port 4403.
- A node sends a private command to the M7: `!start`, `!stop`, `!status`, `!location`, `!<region>`, `!gps`, `!regions`, or `!help`.
- The service tracks the GPS position of each enrolled node.
- The service maps each position to an oblast. It uses a local GeoJSON file and Shapely.
- The service polls the Ubilling Aerial Alerts API every 15 seconds.
- The service sends a private message when an alert starts or ends in the oblast of a node.
- The service tracks the ACK for each message. It never sends the same state twice.
- A web dashboard shows the state. You can enroll nodes and send messages from it.

## Architecture

```
 T-Deck / nodes  <--LoRa-->  ThinkNode M7  <--USB serial (CH34x)-->  Mac
                                           <--TCP :4403 (alternative)-->
                                                             |
   +---------------------------------------------------------+
   |  meshalerts
   |
   |  mesh.py (supervisor, pubsub; transport serial or tcp) --> commands.py --> gps.py --> oblast.py (GeoJSON)
   |        ^                              |              |
   |        |                              v              v
   |   notifier.py (queue, rate limit, ACK, dedup) <-- SQLite (db.py)
   |        ^
   |        |
   |   alerts.py (poller) <-- HTTPS --  Ubilling API
   |
   |  dashboard.py (Flask, 127.0.0.1:8080)
   +---------------------------------------------------------
```

## Install on macOS

You need Python 3.11 or newer.

```
cd mesh-alerts
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

To run the tests:

```
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

## Prepare the GeoJSON file

The service needs the oblast borders. They come from geoBoundaries (license ODbL 1.0).

```
.venv/bin/python scripts/download_geojson.py          # simplified file, about 700 KB
.venv/bin/python scripts/download_geojson.py --full   # full resolution
```

The script reads the geoBoundaries metadata API. It checks for 27 features. It checks that every `shapeISO` code is known.
It writes `data/geoBoundaries-UKR-ADM1.geojson`.

The service stops at start if the file is missing.

## Connection: USB serial

Use `transport = "serial"` (the default in `config.example.toml`). The M7 connects to the Mac with a USB cable.

1. macOS needs the WCH CH34x driver for the M7 USB chip (VID 0x1a86, PID 0x7522).
   Download `CH34xVCPDriver.pkg` from the official WCH GitHub repository `WCHSoftGroup/ch34xser_macos` and install it.
   The Homebrew cask downloads from `file.wch.cn`. That host can time out.
2. Allow the driver extension in System Settings (Privacy & Security). Restart the Mac if macOS asks.
3. Plug in the M7. The port appears as `/dev/cu.wchusbserial*`. The number depends on the USB socket.
4. Close the web flasher and the Meshtastic web client before you start the service.
   The service opens the port exclusively. If another program holds it, the log shows
   "Serial port is busy. Close other programs that use it (web flasher, Meshtastic web client)." and the service retries.
5. Keep the M7 plugged in. A powered USB port is fine.
6. Set `expected_node_id` (for example `"!5f4e7f34"`) so that the service never uses another radio.

Port selection (`serial_port = ""`):

- The service takes ports that match `serial_port_glob` (default `/dev/cu.wchusbserial*`).
- If none matches, it takes ports with a known USB-UART chip: WCH 0x1a86, CP210x 0x10c4, FTDI 0x0403.
- It never takes Espressif (VID 0x303a, for example a T-Deck on `/dev/cu.usbmodem*`) unless `serial_port` names it.
- It prefers `/dev/cu.*` over `/dev/tty.*`. With several candidates it logs them and uses the first in sorted order.
- It detects the port again on each reconnect, because the path can change after a replug.

Set `serial_port` to a path to skip detection.

Intermittent 0xFF doubling (WCH macOS driver, suspected):

- In one session, every 0xFF byte from the M7 arrived as 0xFF 0xFF. Later captures (handshake and live LoRa traffic) showed no doubling. The cause is not known.
- When doubling occurs, the Meshtastic frame reader trusts the declared frame length, and the extra bytes cut the end of the frame.
  The log shows `DecodeError: Wire format was corrupt` and the service loses packets, including `!start` commands.
- `serial_ff_fix = "off"` (default) reads bytes unchanged. Keep it off: when no doubling occurs, the fix corrupts real 0xFF runs.
- If the log shows many `Wire format was corrupt` errors, first restart the service and replug the M7.
  Set `"on"` only when a raw capture confirms that every 0xFF is doubled. `"auto"` turns the fix on for `wchusbserial` ports or USB VID 0x1a86.
  The log shows "Serial 0xFF fix is on" at connect when the fix is active.

## Set up the ThinkNode M7 for TCP

Skip this section for USB serial.

1. Keep the M7 on stock Meshtastic firmware. The service uses the standard TCP API.
2. Enable the network on the M7. Connect it with Ethernet or Wi-Fi.
3. Make sure the TCP API is on port 4403.
4. Give the M7 a static IP address, or a DHCP reservation in your router.
5. Put this address in `[meshtastic] host` in `config.toml`.
6. Do not open the M7 in the Meshtastic app over TCP at the same time. Use one TCP client only.
7. Make sure the T-Deck nodes share their position on the channel (see Limitations).

## Configure

```
cp config.example.toml config.toml
```

Edit `config.toml`. `config.toml` is not tracked in git.

## Config reference

| Section | Option | Default | Meaning |
|---|---|---|---|
| meshtastic | transport | tcp (`serial` in the example config) | `serial` (USB) or `tcp` |
| meshtastic | serial_port | (empty) | Serial port path. Empty = auto-detect. |
| meshtastic | serial_port_glob | /dev/cu.wchusbserial* | Auto-detection prefers ports that match this pattern |
| meshtastic | serial_ff_fix | off | `off`, `on` or `auto`. Collapses doubled 0xFF bytes. Keep off unless doubling is confirmed. |
| meshtastic | expected_node_id | (empty) | Node ID such as `!5f4e7f34`. The service rejects any other radio. |
| meshtastic | host | 192.168.1.50 | M7 IP address (tcp only) |
| meshtastic | port | 4403 | M7 TCP API port (tcp only) |
| meshtastic | heartbeat_interval_s | 60 | Time between liveness probes |
| meshtastic | reconnect_max_backoff_s | 60 | Maximum wait between connection tries (starts at 5 s, doubles) |
| alerts | url | Ubilling URL | Alert API |
| alerts | poll_interval_s | 15 | Poll interval. Minimum is 5. |
| alerts | request_timeout_s | 10 | HTTP read timeout |
| alerts | stale_after_s | 120 | Alert data is old after this time |
| alerts | min_regions | 20 | A response with fewer valid regions is a failed poll |
| alerts | border_policy | assigned | `assigned`: only the oblast of the node. `any_nearby`: also oblasts within `border_margin_m`. |
| alerts | notify_region_change_quiet | false | Send a message when the node enters a quiet oblast |
| gps | geojson_path | data/geoBoundaries-UKR-ADM1.geojson | Border file |
| gps | stale_after_s | 21600 | A position is old after this time. The oblast stays. |
| gps | drop_oblast_after_s | 0 | Clear the oblast of very old positions. 0 = never. |
| gps | border_margin_m | 1500 | Border zone for hysteresis and `any_nearby` |
| gps | outside_margin_m | 3000 | A point this near an oblast still belongs to it |
| gps | reject_outside_ukraine | true | Ignore fixes farther than `outside_margin_m` from every oblast |
| gps | position_history | true | Store each position |
| gps | history_days | 30 | Keep position history this long |
| tx | min_interval_s | 3 | Minimum time between sends (LoRa airtime) |
| tx | ack_timeout_s | 120 | Time to wait for an ACK |
| tx | max_retries | 2 | Retries for failed alert messages |
| tx | retry_delay_s | 60 | Wait before a retry |
| tx | channel_index | 0 | Channel for outgoing messages |
| tx | command_cooldown_s | 10 | One reply per node for the same command in this time |
| storage | db_path | data/meshalerts.db | SQLite file |
| logging | file | logs/meshalerts.log | Log file (5 MB, 5 files). Logs also go to stderr. |
| logging | level | INFO | Log level |
| dashboard | enabled | true | Start the dashboard |
| dashboard | host | 127.0.0.1 | Bind address |
| dashboard | port | 8080 | Port |
| dashboard | password | (empty) | Basic auth password. User name is `admin`. |

## Run

```
.venv/bin/python -m meshalerts --config config.toml
```

Press Ctrl+C or send SIGTERM to stop it.

## Run as a launchd agent

1. Copy `scripts/com.meshalerts.plist.example` to `~/Library/LaunchAgents/com.meshalerts.plist`.
2. Replace every `/ABSOLUTE/PATH/TO/mesh-alerts` with the real path.
3. Make the log folder: `mkdir -p logs`.
4. Load it:

```
launchctl load ~/Library/LaunchAgents/com.meshalerts.plist
```

To stop it, run `launchctl unload ~/Library/LaunchAgents/com.meshalerts.plist`.
launchd restarts the service if it exits (`KeepAlive`) and starts it at login (`RunAtLoad`).

The Mac must not sleep. Run `sudo pmset -a sleep 0`, or use `caffeinate -s` in a second launchd job.

## Dashboard

Open `http://127.0.0.1:8080/`. The page refreshes every 5 seconds.

- Header: M7 connection (`USB <port>` or host:port), alert data freshness, queue length.
- Radio connection card: choose the serial port and connect or disconnect the M7 (see below).
- Nodes: enroll, unenroll, send a direct message, send a test message. Add a node by its ID (`!a1b2c3d4`).
  The table shows the number of rejected GPS fixes for each node.
- Alert states: all regions. Active alerts have a red row.
- Notifications: the message log with ACK status.

### Radio connection card

The card sits under the header. It shows the link state and lets you choose the serial port.

- **Serial port**: `Auto-detect` uses the normal rules (`serial_port`, then `serial_port_glob`, then known USB-UART chips).
  Auto-detect never opens an Espressif device (VID `303a`).
  Each other entry shows the description and the path. `likely` marks a port that matches the glob or a known USB-UART chip.
  `active` marks the port in use. Choose an Espressif port by hand if you need it.
- **Refresh ports**: read the port list again. The page also reads it on every refresh.
- **Connect**: close the current link and open the chosen port at once. The service does not wait for the retry delay.
  A manual choice always uses serial, even when `transport` is `tcp`.
- **Disconnect**: close the link. The service stays disconnected until you press Connect.
  Use this to free the port for the web flasher.
- **Accept any node**: skip the `expected_node_id` check for this one connection. It is off by default.
  The service does not save it. A later reconnect checks the node again. The service writes a warning to the log when you use it.
- **Status line**: `Connected to !id (hwModel, firmware) on <port>`, `Connecting…`, `Disconnected by user`,
  or `Error: <message> · retry in N s`.

The service saves the port choice (auto or manual with a path) and "disconnected by user" in the database.
After a restart it uses the saved choice. With no saved choice it uses the config file.

API:

| Request | Body | Result |
|---|---|---|
| `GET /api/ports` | none | `{"ports": [{path, description, manufacturer, vid, pid, serial_number, likely, espressif, selected, active}]}` |
| `POST /api/connection/connect` | `{"port": "<path>" or null, "allow_any_node": false}` | `{"ok": true}`. The port must be in the list, or `null` for auto-detect. Unknown port: 400. |
| `POST /api/connection/disconnect` | none | `{"ok": true}` |

`GET /api/state` adds these fields to `connection`: `mode` (`auto` or `manual`), `chosen_port`, `connecting`,
`disconnected_by_user`, `next_retry_at` (Unix time or null), `node_id`, `hw_model`, `firmware`.

Security:

- The dashboard binds to `127.0.0.1` by default.
- Set `dashboard.password` before you bind to a LAN address. The user name is `admin`.
- If the host is not a loopback address and no password is set, the dashboard does not start.
- Basic auth sends the password in clear text. Use the dashboard on a trusted LAN only.
- All POST requests need the header `X-Requested-With: meshalerts`.
- The dashboard can send direct messages only. It has no broadcast.

## Commands

A node sends a command as a private message to the M7. Messages on a channel or to other nodes are ignored.

| Command | Action |
|---|---|
| `!start` | Enroll the node. The reply shows the oblast and the alert state. Keeps a fixed region. |
| `!stop` | Unenroll the node. The service stops position history. A fixed region is kept for the next `!start`. |
| `!status` | Show the oblast and alert state. Warns if the position or data is old. Shows `(fixed)` for a fixed region. |
| `!location` | Show the last valid position and its age, and the region. |
| `!<region>` | Enroll the node (if needed) and fix its region, for example `!lviv`. |
| `!region <name>` | Same as `!<region>`. |
| `!gps` (or `!auto`) | Return to the region from GPS. |
| `!regions` | List the short names (2 messages). |
| `!help` | List the commands. |

Notification texts:

- `ПОВІТРЯНА ТРИВОГА! <oblast>` when an alert starts.
- `ВІДБІЙ ТРИВОГИ. <oblast>` when it ends.
- `Ви в зоні тривоги: <oblast>. Тривога триває.` when the node moves into an oblast with an active alert.
- `Ви вийшли із зони тривоги. <oblast>: тривоги немає.` when the node leaves an active oblast.

## Fixed region

A node can pin itself to one oblast, for example when its GPS is jammed or it sits at a border.

- `!lviv` enrolls the node (if it is not enrolled) and fixes the region to Львівська область.
  Reply: `Alerts enabled. Region: Львівська область (fixed). Status: No alert.` (or `Active.`, or `no data.`).
- While the region is fixed, GPS fixes are still checked (spoof filter) and stored, but they never change the region.
  The service sends no "region change" message.
- `!gps` returns to GPS. It recomputes the region from the last valid stored position.
  Reply: `Region from GPS: <oblast>. Status: <...>.` or `Region from GPS: unknown (no GPS position).`
  If the node is not enrolled, `!gps` enrolls it.
- Alerts follow the fixed region. `border_policy = any_nearby` applies only in GPS mode.
- A command that changes the region replies with the state of the new region. No separate alert message follows.
- `!stop` keeps the fixed region. `!start` keeps it too. `!status` and `!location` show `(fixed)`.
- Names are not case-sensitive. The service ignores apostrophes, hyphens, spaces, underscores, and a trailing `oblast`, `obl`, `область`, or `обл`.
  It accepts Latin transliterations (`lviv`, `kharkiv`, `kharkov`, `zaporizhzhia`, `zaporozhye`), Ukrainian forms (`львів`, `львівська`), and English names.
  Any `!word` that is not a command is checked as a region name. Unknown: `Unknown region. Send !regions for the list.`
- Kyiv: `!kyiv`, `!kiev`, `!київ` mean the city (м. Київ). `!kyivska`, `!kyivobl`, `!kyivoblast`, `!київська` mean Київська область.
- Crimea (`!crimea`, `!krym`, `!крим`): the reply says the API has no data. The region does not change.
- The short names live in `meshalerts/regions.py`. No name equals a command name.
- Dashboard: each node has a region list (`GPS` or a region). It calls `POST /api/nodes/<id>/region` with
  `{"region": "<name from the alert API>" or null, "notify": true|false}`. `null` means GPS.
  The reply text goes to the node when `notify` is true. A fixed node shows a `fixed` tag.
  `GET /api/state` adds `region_mode`, `fixed_oblast`, and `gps_oblast` to each node.

## GPS handling

- The service stores positions for enrolled nodes only.
- A position farther than `outside_margin_m` from every oblast is rejected as possible GPS spoofing (for example a fix in Lima, Peru).
  The service does not store it. It does not change the oblast. It keeps the last valid position.
  It counts the rejection per node and writes a rate-limited warning to the log. The dashboard shows the count.
  `!location` adds `Last GPS fix rejected (outside Ukraine).` when the latest fix was rejected.
- Set `reject_outside_ukraine = false` to turn this off.
- Near an oblast border, a node keeps its old oblast until it is more than `border_margin_m` inside the new one. This stops flapping from GPS noise.

## Limitations

- Ubilling has no data for Crimea (UA-43). `!status` answers "no data" there. The service sends no alerts for it.
- The Ubilling API gives no reliability guarantee. If a poll fails, the service keeps the last state and sends nothing.
- An ACK means the radio delivered the message to the device. It does not mean that a person read it.
- An implicit ACK means the M7 only heard a rebroadcast. It does not prove delivery.
- LoRa delivery is not guaranteed. Do not use this service as the only warning.
- GPS precision depends on the position precision setting of the node (`precisionBits`). A node with reduced precision can get the wrong oblast near a border.
- The T-Deck must share its position on the channel. Position packets on a channel with position precision 0 are not sent.
- Positions in the M7 node database are only as new as the last packet the M7 heard.
- Spoofed GPS fixes inside Ukraine cannot be detected. Only fixes far outside Ukraine are rejected.
- macOS sleep stops the service. Use `caffeinate` or turn off sleep.
- ThinkNode M7 unit !5f4e7f34: Ethernet link never comes up (hardware fault suspected); use USB serial.
- The library reconnect is not reliable. The service has its own supervisor that reconnects with backoff.

## Data sources and licenses

- Alerts: Ubilling Aerial Alerts API (`https://ubilling.net.ua/aerialalerts/`). The limit is 2 requests per second.
- Borders: geoBoundaries (ODbL 1.0).
