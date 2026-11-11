# Meshtastic Air Alert Service — Design

Language rule: ASD-STE100 Simplified Technical English for docs, comments, log
messages, and English replies. Air alert notifications are in Ukrainian.

## 1. Verified external facts (checked 2026-11-10)

### 1.1 Ubilling Aerial Alerts API
- URL: `https://ubilling.net.ua/aerialalerts/` (GET, JSON, no auth).
- Rate limit: 2 requests/s per host, HTTP 429 when exceeded. Server cache 3 s.
- Response (real sample, trimmed):
  ```json
  {"source": "Vadym Klymenko API (default)",
   "cachedat": "2026-11-10 10:47:27",
   "states": {
     "Львівська область": {"alertnow": false, "changed": "2026-11-10 10:47:27"},
     "Київська область":  {"alertnow": true,  "changed": "2026-11-10 10:06:43"},
     "м. Київ":           {"alertnow": false, "changed": "2026-11-10 10:47:27"},
     "Севастополь":       {"alertnow": true,  "changed": "2022-12-11 00:22:00"}
   }}
  ```
- 26 keys: 24 oblasts named "<X> область", plus "м. Київ" and "Севастополь".
  **No key for Crimea (Автономна Республіка Крим).**
- `changed` for inactive regions often equals `cachedat` → not a reliable
  change time. Use only `alertnow` for transitions.
- The API says it gives no reliability guarantee. Note this in README.
- A full sample is in `tests/fixtures/ubilling_sample.json` (copy of the live response).

Validation: response must be a dict with `states` dict; each value must have a
bool `alertnow`. If HTTP error, timeout, JSON error, schema error, or fewer than
`min_regions` (config, default 20) valid regions → poll FAILED: keep last valid
state, log warning, no transitions. A region missing from one valid response
keeps its last state (do not treat as all-clear).

### 1.2 geoBoundaries UKR ADM1
- Metadata: `https://www.geoboundaries.org/api/current/gbOpen/UKR/ADM1/`
  (fields `gjDownloadURL`, `simplifiedGeometryGeoJSON`). License ODbL 1.0.
- Current files:
  - full: `https://github.com/wmgeolab/geoBoundaries/raw/9469f09/releaseData/gbOpen/UKR/ADM1/geoBoundaries-UKR-ADM1.geojson`
  - simplified (761 KB): `..._simplified.geojson`
- 27 features. Properties: `shapeName` (English, e.g. "Lviv Oblast"), `shapeISO`
  (e.g. "UA-46"), `shapeID`, `shapeGroup`, `shapeType`.
- Map by `shapeISO` (stable), not by name:

  | ISO | Ubilling name |
  |---|---|
  | UA-05 | Вінницька область |
  | UA-07 | Волинська область |
  | UA-09 | Луганська область |
  | UA-12 | Дніпропетровська область |
  | UA-14 | Донецька область |
  | UA-18 | Житомирська область |
  | UA-21 | Закарпатська область |
  | UA-23 | Запорізька область |
  | UA-26 | Івано-Франківська область |
  | UA-30 | м. Київ |
  | UA-32 | Київська область |
  | UA-35 | Кіровоградська область |
  | UA-40 | Севастополь |
  | UA-43 | Автономна Республіка Крим (no API data) |
  | UA-46 | Львівська область |
  | UA-48 | Миколаївська область |
  | UA-51 | Одеська область |
  | UA-53 | Полтавська область |
  | UA-56 | Рівненська область |
  | UA-59 | Сумська область |
  | UA-61 | Тернопільська область |
  | UA-63 | Харківська область |
  | UA-65 | Херсонська область |
  | UA-68 | Хмельницька область |
  | UA-71 | Черкаська область |
  | UA-74 | Чернігівська область |
  | UA-77 | Чернівецька область |

- Verified with Shapely 2.x: Kyiv (50.45, 30.52) → only UA-30; Brovary → UA-32;
  Lviv → UA-46; Sevastopol → UA-40; Black Sea point → none. All geometries valid.
- GeoJSON coordinates are (lon, lat). `Point(lon, lat)`.
- For a region with no API data (Crimea): `!status` answers "no data"; no alerts sent.

### 1.3 meshtastic Python library (installed 2.7.8)
- `meshtastic.tcp_interface.TCPInterface(hostname, portNumber=4403, connectNow=True, noNodes=False, timeout=300)`.
- pubsub topics (pypubsub, `from pubsub import pub`), listener signature `(packet, interface)`:
  - `meshtastic.receive.text` — `packet["decoded"]["text"]` (str, may be missing on bad UTF-8)
  - `meshtastic.receive.position` — `packet["decoded"]["position"]` dict with
    `latitudeI`, `longitudeI` (int, 1e-7 deg), and after library fixup `latitude`,
    `longitude` (float); optional `time`, `altitude`, `precisionBits`, `PDOP`, `satsInView`.
    Missing or 0 lat/lon → invalid.
  - `meshtastic.receive.user` — NodeInfo; `packet["decoded"]["user"]` has `id`, `longName`, `shortName`.
  - `meshtastic.connection.established` `(interface)`, `meshtastic.connection.lost` `(interface)`.
  - `meshtastic.node.updated` `(node, interface)`.
- Packet dict keys: `from` (int nodenum), `to` (int), `fromId` (e.g. "!a1b2c3d4" or None),
  `toId` ("^all" for broadcast), `channel` (int, may be absent = 0), `id`, `rxTime`,
  `decoded.portnum` (string like "TEXT_MESSAGE_APP").
- Broadcast address: `meshtastic.BROADCAST_NUM` = 0xFFFFFFFF. A private message
  to the M7 has `packet["to"] == interface.myInfo.my_node_num`.
- Node DB: `interface.nodesByNum` → dict num → `{"num", "user": {...}, "position": {...}, "lastHeard"}`.
  `interface.nodes` keyed by node ID string.
- Node ID from num: `f"!{num:08x}"`. Use node ID string as primary key.
- Sending: `interface.sendData(text.encode("utf-8"), destinationId=<num or "!id">,
  portNum=portnums_pb2.PortNum.TEXT_MESSAGE_APP, wantAck=True, onResponse=cb,
  onResponseAckPermitted=True, channelIndex=0)` returns MeshPacket with `.id`.
  `sendText` does NOT expose `onResponseAckPermitted`, and without it the
  library drops ACKs and calls the callback only for NAKs. So use `sendData`.
- ACK callback receives a packet dict with `decoded.routing` (maybe `errorReason`)
  and `from`:
  - `errorReason` missing or "NONE" and `from == destination num` → `delivered`
    (radio ACK from the node; NOT proof that a person read it).
  - `errorReason` "NONE" and `from == local node num` → `implicit_ack` (M7 heard a
    rebroadcast only; not delivery).
  - other `errorReason` (e.g. "MAX_RETRANSMIT", "NO_ROUTE", "TIMEOUT") → `failed`.
  - No callback within `ack_timeout_s` → `no_ack`.
- Payload limit: keep each text ≤ 200 UTF-8 bytes (Cyrillic = 2 bytes/char).
  Truncate safely on UTF-8 boundary for dashboard free text; reject in UI with message.
- USB: `meshtastic.serial_interface.SerialInterface(devPath, noNodes=False, timeout=300)`.
  It opens the port exclusively (`[Errno 16] Resource busy` if another program holds it).
  It has no `.socket`: no TCP keepalive. Select the port in the app, never with `devPath=None`.
- `interface.sendHeartbeat()` exists. Use it as a liveness probe.
- Reconnect weakness: on EOF the library tries one reconnect inside its reader
  thread; if the M7 is down, the reader thread dies. If the cable is pulled, recv
  can block forever. So the app must run its own supervisor (see §3.1).
- pubsub listeners are global. Filter every event by `interface is current_iface`.
  Keep strong references to listener functions (pypubsub holds weak refs).
- pubsub callbacks run on the library's publishing thread. Do not block there:
  put work in queues.

## 2. Repository layout

```
mesh-alerts/
  README.md                  # STE; macOS install/run, launchd, GeoJSON prep
  requirements.txt
  config.example.toml        # tomllib (stdlib, Python 3.11+)
  scripts/download_geojson.py
  scripts/com.meshalerts.plist.example   # launchd agent
  data/                      # GeoJSON + SQLite (gitignored)
  meshalerts/
    __init__.py
    __main__.py              # python -m meshalerts --config config.toml
    config.py                # dataclasses, load + validate TOML
    db.py                    # schema, init, migrations, all SQL (thread-safe)
    alerts.py                # Ubilling poller + transition detection
    oblast.py                # GeoJSON load, ISO→name map, lookup with border logic
    gps.py                   # position validation, staleness, oblast update
    mesh.py                  # TCPInterface supervisor, pubsub, send with ACK
    commands.py              # !start !stop !status !location (+ !help)
    notifier.py              # outgoing queue, rate limit, ACK tracking, dedup
    messages.py              # all user-facing text templates
    dashboard.py             # Flask web dashboard
    templates/dashboard.html
    service.py               # wiring, threads, shutdown
  tests/
    fixtures/ubilling_sample.json
    fixtures/mini_ukraine.geojson   # small synthetic polygons for tests
    test_registration.py
    test_gps_lookup.py
    test_alert_transitions.py
    test_duplicates.py
    test_notifier.py
    test_dashboard.py
```

Requirements: `meshtastic>=2.7,<3`, `requests>=2.31`, `shapely>=2.0`,
`pypubsub>=4.0.3`, `flask>=3.0`, dev: `pytest`. Python ≥ 3.11 (tomllib).

## 3. Components

### 3.1 mesh.py — connection supervisor
- `MeshConnection(config, on_text, on_position, on_nodeinfo, on_connected)`.
- Supervisor thread loop:
  1. Create `TCPInterface(host, portNumber=port)` (blocks until config done).
     On exception → log, sleep backoff (5 s → ×2 → max 60 s), retry.
  1a. With `transport = "serial"`: detect the port on every attempt (glob, then known
     USB-UART VIDs, never Espressif 0x303a, `cu.*` before `tty.*`), then open `SerialInterface(path)`.
     `[meshtastic] expected_node_id`, if set, must equal the local node ID (both transports),
     else log ERROR, close, retry with backoff.
  2. After connect (tcp only): enable TCP keepalive on `iface.socket`
     (`SO_KEEPALIVE`; on macOS `socket.TCP_KEEPALIVE` = idle seconds if attr exists;
     `TCP_KEEPINTVL`, `TCP_KEEPCNT` if exist). Store `my_node_num`.
  3. Call `on_connected(iface)` (seed GPS from node DB).
  4. Every `heartbeat_interval_s` (60): `iface.sendHeartbeat()`; if it raises,
     or `connection.lost` event arrived for this iface, or `iface.isConnected`
     is not set, or reader thread is dead → close iface (suppress errors), go to 1.
- `send_text(dest_num, text, on_ack) -> packet_id | raises NotConnected`.
  Thread-safe (lock around send).
- `is_connected()`, `local_node_num`, `known_nodes()` snapshot (copy of
  nodesByNum under lock, for dashboard and startup seeding).
- Inbound packet filter: ignore packets with `from == local node num`.

### 3.2 commands.py
- Accept only TEXT packets where `to == local_node_num` (private). If `to` is
  broadcast or another node → ignore commands (log at debug level).
- Text: strip, lowercase the first token. Commands: `!start`, `!stop`, `!status`,
  `!location`, `!help`, `!gps` (alias `!auto`), `!region <alias>`, `!regions`. Any other
  `!word` (at most 3 tokens) is a region alias (see "Fixed region" below). Ignore text that
  does not start with `!` (do not reply, to avoid loops with other bots). Max accepted text length 200 chars. Reject when `fromId` cannot be
  computed → derive `f"!{from:08x}"`.
- Per-node command rate limit: max 1 reply per `command_cooldown_s` (10) per
  node for identical commands; drop extras with a log entry.
- `!start`: upsert node subscribed=1, set registered_at on each !start. Seed position from node DB if no
  position stored. Determine oblast. Reply (English, STE):
  `Alerts enabled. Region: <oblast>. Status: Active.` /
  `... Status: No alert.` / if no position: `Alerts enabled. Region: unknown (no GPS position). Send a position to get alerts.`
  / only rejected (outside Ukraine) fixes: treat as no position.
  / no API data yet or region with no data: `Status: no data.`
  Mark node's "last notified state" for its oblast = current state, so the
  confirmation counts as the initial info and no separate alert follows.
- `!stop`: subscribed=0, reply `Alerts disabled. Send !start to enable.`
  Position history stops (gps.py checks subscribed).
- `!status`: `Region: <oblast>. Status: ПОВІТРЯНА ТРИВОГА.` / `Status: no alert.`
  / unknown region / stale position notice `Position is old (<N> min).`
- `!location`: `Position: <lat 5dp>, <lon 5dp>. Time: <HH:MM UTC> (<age> min ago). Region: <oblast>.`
  or `No position known.` For not-subscribed nodes: use the node DB position
  if present, but do not store it.
- `!help`: list commands.
- Fixed region (schema v5: `nodes.region_mode` 'gps'|'fixed', `fixed_oblast`, `gps_oblast`;
  `nodes.oblast` stays the effective oblast):
  - `!<alias>` / `!region <alias>`: lookup in `regions.py` (one table keyed by Ubilling name;
    normalize = lowercase, keep only letters/digits, try the full key, then without a
    trailing oblast/obl/область/обл). Subscribe if needed, set mode fixed, `oblast = fixed_oblast`,
    set dedup to (new oblast, current state). No region-change message. Reply
    `Alerts enabled. Region: <oblast> (fixed). Status: <No alert|Active|no data>.`
    Crimea: reply "no data", no change. Unknown: `Unknown region. Send !regions for the list.`
  - `!gps`/`!auto`: mode gps, `oblast = gps_oblast` recomputed from the stored position
    (honors `drop_oblast_after_s`), dedup reset the same way. Reply `Region from GPS: ...`.
  - `!regions`: canonical Latin short names, packed into messages of at most 200 bytes.
  - `!start` and `!stop` keep mode and `fixed_oblast`. `!status`/`!location` add `(fixed)`.
  - Cooldown key for all alias forms is `!region`.
- All replies go through the notifier queue with kind="reply" and higher priority.

### 3.3 gps.py
- `handle_position(node_id, position_dict, rx_time)`:
  - Validate: lat/lon present, not both 0, -90..90/-180..180, finite. Use
    `latitudeI`/`longitudeI` if float keys missing. Timestamp = `position.time`
    if plausible (not 0, not > now+300 s, not older than 30 days) else rxTime
    else now.
  - Spoofing filter: GPS spoofing/jamming in Ukraine often reports positions
    far away (e.g. Lima, Peru). Resolve the oblast first. If status is
    `outside_ukraine` (farther than `gps.outside_margin_m` from every oblast)
    → REJECT the packet: do not store it, do not add to history, do not change
    oblast, position_at, or stale timers. Keep last valid position and oblast.
    Log WARNING "Position rejected: outside Ukraine (possible spoofing)" with
    node ID and coords; rate-limit this log to 1 per node per 10 min.
    Count rejections per node (in-memory counter + `nodes.rejected_positions`
    INTEGER column, `nodes.last_rejected_at`) and show both in the dashboard.
    Same rule for node DB positions at startup seeding and for `!start`.
    `!location` shows the last valid position and adds
    `Last GPS fix rejected (outside Ukraine).` if the most recent fix was rejected.
    Config `gps.reject_outside_ukraine = true` (default). If false → old
    behaviour (oblast None, status outside Ukraine).
  - Only for subscribed nodes: update nodes.latitude/longitude/position_at; if
    `position_history` enabled → insert into positions. Ignore if timestamp older
    than stored position_at.
  - Run oblast resolution (oblast.py) with current oblast for hysteresis. Always store
    `gps_oblast`. In gps mode, if oblast changes → update DB, log, call
    `notifier.on_region_change(node, old, new)`. In fixed mode `oblast` is not touched,
    no callback (DEBUG log); the spoofing filter still applies.
- Stale: `position_at` older than `gps.stale_after_s` (default 6 h) → stale.
  Stale nodes keep their oblast and keep getting alerts (best known region), but
  `!status`/`!location`/dashboard show stale flag. Config option
  `gps.drop_oblast_after_s` (default 0 = never) to clear oblast for very old positions.
- Startup seeding: on connect, for each subscribed node with entry in node DB
  `position`, call handle_position with node DB data (time from position.time
  or lastHeard). Also update node_name/last_seen from node DB.
- `last_seen` updated on any packet from a known node (text, position, nodeinfo).
- Position history retention: delete rows older than `gps.history_days` (30) daily.

### 3.4 oblast.py
- Load GeoJSON at startup; fail fast with clear message if file missing.
- Build list of (iso, ubilling_name, prepared geometry) + `shapely.STRtree`.
  Unknown ISO in file → log warning, skip.
- Distances: convert to metres with local equirectangular approximation, or
  project the point and geometry neighbourhood. Simple approach acceptable:
  degrees distance × 111_320 m (lat) and × 111_320·cos(lat) (lon) — implement
  via `shapely.ops.transform` on a buffer-free basis: compute
  `geom.distance(point)` in a locally scaled coordinate system (scale lon by
  cos(lat)) then ×111_320.
- `resolve(lat, lon, current=None) -> Resolution(oblast|None, status, distance_to_border_m, candidates)`
  - status ∈ {"inside", "near_border", "outside_nearby", "outside_ukraine"}.
  - Inside polygon P: if `current` is a neighbour within `border_margin_m`
    (default 1500) of the point → keep `current` (hysteresis, avoid flapping on
    GPS noise). Else return P.
  - Not inside any polygon (sea, outside, or gap): nearest polygon within
    `outside_margin_m` (default 3000) → that oblast, status outside_nearby.
    Else None, status outside_ukraine (gps.py rejects it as spoofing, §3.3).
  - `candidates`: all oblasts within `border_margin_m` (for logs/dashboard).
- Border alerts policy (config `alerts.border_policy`):
  - `"assigned"` (default): node gets alerts only for its assigned oblast.
  - `"any_nearby"` (gps mode only): node near a border (within margin) also counts as in alert
    if any candidate oblast is in alert. Keep implementation simple: compute an
    "effective state" = OR over candidates. Document both.
  (Implement both; default "assigned".)

### 3.5 alerts.py
- `AlertPoller(config, db, on_transition)` thread; interval `alerts.poll_interval_s`
  (15). Never less than 5 s. On HTTP 429 → back off ×2 up to 120 s.
- `requests.Session`, timeout (5 s connect, 10 s read), User-Agent
  `meshalerts/1.0` (no email).
- On valid poll: for each region, compare `alertnow` with stored state:
  - No stored state (first run ever) → store, no transition event (initial
    baseline). Log "baseline".
  - Different → store (active, updated_at=now), emit Transition(oblast, active,
    at). Log.
- State lives in `alert_states` table → survives restart. On restart, first
  poll compares with DB state, so an alert that started while app was down
  generates one transition (correct: subscribers were not told).
- Expose `current_state(oblast) -> bool|None`, `last_success_at`, `last_error`,
  `is_fresh()` (last success < `alerts.stale_after_s` 120 s). If data is stale,
  `!status` replies add `Data is old (<N> min).`
- Pure function `diff_states(old: dict, new: dict) -> list[Transition]` for tests.

### 3.6 notifier.py — dedup + queue
- Dedup is the core rule: per node, store `last_notified_oblast` and
  `last_notified_state` (columns on nodes table, see §4). A notification is
  sent only when (oblast, state) differs from the last value sent to this node.
  This gives: no duplicates across restarts, no duplicates when poller emits
  the same state twice, correct behavior on region change.
- `on_transition(t)`: for each subscribed node with oblast == t.oblast (or
  candidate under any_nearby), if dedup says changed → enqueue:
  - active → `ПОВІТРЯНА ТРИВОГА! <oblast>`
  - ended → `ВІДБІЙ ТРИВОГИ. <oblast>`
- `on_region_change(node, old, new)`:
  - new oblast state active → `Ви в зоні тривоги: <oblast>. Тривога триває.`
    (existing alert; NOT "ПОВІТРЯНА ТРИВОГА!" which means a new alert).
  - new state inactive and last notified state for old oblast was active →
    `Ви вийшли із зони тривоги. <new oblast>: тривоги немає.`
  - new state inactive, old not active → no message (only log). Configurable
    `notify_region_change_quiet` (default false); if true send
    `Новий регіон: <oblast>. Тривоги немає.`
  - new None (outside) → no message by default.
- Queue: `queue.PriorityQueue`; one sender thread. Priority: replies and alert
  start (0) before alert end (1) before region info (2) before dashboard (1).
  Rate: min `tx.min_interval_s` (default 3 s) between sends (LoRa airtime).
  Coalesce: if a newer message for the same (node, oblast) kind=alert is queued
  and an older not-yet-sent one exists → drop the older (log "superseded"),
  e.g. start+end within seconds sends only the final state.
- Each send: insert notifications row (status "queued"), on send → "sent",
  ACK callback → "delivered" / "implicit_ack" / "failed:<reason>", timeout
  thread → "no_ack". ACK callback runs on library thread: only update DB.
- Dedup fields update when the message is **sent** (handed to the M7), not on
  ACK. Retries: on `failed` or `no_ack` for alert kinds, retry up to
  `tx.max_retries` (2) with delay `tx.retry_delay_s` (60), only if the message
  still reflects the current state (else drop). Log each.
- If not connected: keep items in queue (do not drop), sender waits. Queue is
  in memory; on restart, dedup state in DB ensures pending alerts are re-derived:
  on startup after first valid poll, run `reconcile()`: for each subscribed node
  with oblast, if current state != last_notified_state → enqueue the right
  message (start/end). Same logic covers missed notifications during downtime.
  Baseline rule: if last_notified_state is NULL (never notified, e.g. old DB),
  set it to current state without sending.
- Exceptions in one send → log, mark failed, continue with next item.

### 3.7 dashboard.py (Flask)
- Runs in a thread (`werkzeug` `make_server` so it can be shut down).
- Bind `dashboard.host` (default 127.0.0.1), `dashboard.port` (8080).
- Auth: if `dashboard.password` set → HTTP Basic auth on all routes (user
  `admin`). If host is not loopback and no password → refuse to start dashboard
  and log error.
- CSRF: all mutating endpoints are JSON POST and require header
  `X-Requested-With: meshalerts` (browser forms cross-site cannot set it).
- Pages/endpoints:
  - `GET /` — single-page HTML (vanilla JS, no CDN, polls `/api/state` every 5 s).
  - `GET /api/state` — connection status, local node id, poller status (last
    success, error, source), queue length, alert states (all regions), nodes
    (DB nodes + mesh node DB nodes not in DB, with subscribed flag, name,
    oblast, position, position age, stale flag, last notified state, last_seen).
  - `GET /api/notifications?limit=100` — recent rows.
  - `POST /api/nodes/<node_id>/subscribe` body `{"notify": bool}` → subscribe
    (same as !start; if notify → send the confirmation message).
  - `POST /api/nodes/<node_id>/unsubscribe` body `{"notify": bool}`.
  - `POST /api/nodes/<node_id>/region` body `{"region": "<Ubilling name>"|null, "notify": bool}`
    → fixed region, or GPS for null (same as `!<region>` / `!gps`). 400 for names not in the API list.
  - `POST /api/nodes` body `{"node_id": "!a1b2c3d4"}` → add node manually
    (validated `^![0-9a-f]{8}$`), subscribed.
  - `POST /api/send` body `{"node_id": "...", "text": "..."}` → direct message
    via queue (kind "manual"), returns notification id. Reject >200 UTF-8 bytes
    or empty. Only direct messages (no broadcast option).
  - `POST /api/test-alert` body `{"node_id": ...}` → sends a test message
    `ТЕСТ. Перевірка сповіщень. <oblast>` (does not change dedup state).
- UI: tables for Nodes (with Subscribe/Unsubscribe/Send buttons), Alert map as
  table (red rows for active), Notifications log with ACK status, header with
  M7 connection badge and API freshness. Light/dark via prefers-color-scheme.
  Escape all text (use textContent, never innerHTML with data).

### 3.8 service.py / __main__.py
- Parse `--config` (default `config.toml`), set up logging: rotating file
  (`logging.handlers.RotatingFileHandler`, 5 MB × 5) + stderr. Format with
  timestamp, level, module.
- Start order: db init → oblast load → poller → notifier → mesh supervisor →
  dashboard. SIGINT/SIGTERM → clean stop (close iface, stop threads).
- Logging categories to cover: registration, GPS updates, oblast changes,
  alert transitions, message queued/sent/ack status, connection events, API errors.

## 4. SQLite schema

WAL mode, `PRAGMA foreign_keys=ON`, one connection per thread or one shared
connection with `check_same_thread=False` + a lock. Timestamps: integer Unix
seconds UTC. Schema version in `PRAGMA user_version`.

```sql
CREATE TABLE IF NOT EXISTS nodes (
  node_id TEXT PRIMARY KEY,          -- "!a1b2c3d4"
  node_num INTEGER NOT NULL,
  node_name TEXT,
  subscribed INTEGER NOT NULL DEFAULT 0,
  registered_at INTEGER,
  last_seen INTEGER,
  latitude REAL,
  longitude REAL,
  position_at INTEGER,
  oblast TEXT,
  last_notified_oblast TEXT,
  last_notified_state INTEGER,       -- 1 active, 0 inactive, NULL never
  rejected_positions INTEGER NOT NULL DEFAULT 0,
  last_rejected_at INTEGER,
  region_mode TEXT NOT NULL DEFAULT 'gps',  -- 'gps' | 'fixed' (schema v5)
  fixed_oblast TEXT,
  gps_oblast TEXT                    -- GPS-derived oblast; oblast = fixed_oblast when fixed
);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id TEXT NOT NULL REFERENCES nodes(node_id),
  latitude REAL NOT NULL,
  longitude REAL NOT NULL,
  timestamp INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_positions_node_time ON positions(node_id, timestamp);
CREATE TABLE IF NOT EXISTS alert_states (
  oblast TEXT PRIMARY KEY,
  active INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id TEXT NOT NULL,
  oblast TEXT,
  alert_state INTEGER,               -- 1/0, NULL for replies/manual
  kind TEXT NOT NULL,                -- alert|region|reply|manual|test
  text TEXT NOT NULL,
  packet_id INTEGER,
  queued_at INTEGER NOT NULL,
  sent_at INTEGER,
  acknowledgement_status TEXT NOT NULL  -- queued|sent|delivered|implicit_ack|no_ack|failed:<r>|superseded|dropped
);
CREATE INDEX IF NOT EXISTS idx_notifications_node ON notifications(node_id, id);
```

## 5. Config (config.example.toml)

```toml
[meshtastic]
host = "192.168.1.50"
port = 4403
heartbeat_interval_s = 60
reconnect_max_backoff_s = 60

[alerts]
url = "https://ubilling.net.ua/aerialalerts/"
poll_interval_s = 15
request_timeout_s = 10
stale_after_s = 120
min_regions = 20
border_policy = "assigned"   # or "any_nearby"

[gps]
geojson_path = "data/geoBoundaries-UKR-ADM1.geojson"
stale_after_s = 21600
drop_oblast_after_s = 0
border_margin_m = 1500
outside_margin_m = 3000
position_history = true
reject_outside_ukraine = true   # ignore spoofed fixes far from Ukraine
history_days = 30

[tx]
min_interval_s = 3
ack_timeout_s = 120
max_retries = 2
retry_delay_s = 60
channel_index = 0
command_cooldown_s = 10

[storage]
db_path = "data/meshalerts.db"

[logging]
file = "logs/meshalerts.log"
level = "INFO"

[dashboard]
enabled = true
host = "127.0.0.1"
port = 8080
password = ""
```

## 6. Tests (pytest, no network, no radio)
- Fake mesh: object with `local_node_num`, `send_text()` that records calls and
  lets tests trigger ACK callbacks; fake node DB.
- `mini_ukraine.geojson`: 3 synthetic adjacent square "oblasts" with real ISO
  codes (e.g. UA-46, UA-61, UA-30) for deterministic border tests. One test also
  runs against the real file if present (skip otherwise).
- test_registration: !start private → subscribed + reply; !start on broadcast →
  ignored; !stop → unsubscribed + no history after stop; persistence after
  reopening DB; node ID used not name; malformed packets ignored; unknown
  command ignored; cooldown.
- test_gps_lookup: inside, near border hysteresis (no flapping), outside nearby,
  spoofed fix in Lima (-12.0464, -77.0428) rejected: last valid position and
  oblast unchanged, no history row, no region-change message, counter +1, invalid coords (0,0 / out of range / missing), stale, older
  timestamp ignored, ISO→name mapping covers all 26 API names and every API name
  in fixture is mapped.
- test_alert_transitions: baseline no events; start; end; API error keeps state;
  malformed JSON; too few regions; region missing keeps state; 429 backoff;
  restart (state from DB) produces exactly one transition.
- test_duplicates: same state twice → one message; restart → reconcile does not
  resend; start+end queued → superseded; region change into active oblast →
  "existing alert" text, not "ПОВІТРЯНА ТРИВОГА!"; registration confirmation
  sets dedup so no extra alert.
- test_notifier: ACK classification (delivered / implicit_ack / failed / no_ack);
  one failed send does not block next; queue waits while disconnected; retry.
- test_dashboard: Flask test client; auth; CSRF header required; subscribe,
  unsubscribe, add node validation, send text length limits.
