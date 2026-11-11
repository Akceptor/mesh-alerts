"""Flask web dashboard."""

from __future__ import annotations

import hmac
import ipaddress
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request
from werkzeug.serving import make_server

from . import messages
from .db import node_num_from_id
from .mesh import NotConnected
from .oblast import API_NAMES

log = logging.getLogger(__name__)

NODE_ID_RE = re.compile(r"^![0-9a-f]{8}$")
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "meshalerts"
TEMPLATE = Path(__file__).parent / "templates" / "dashboard.html"


UK_ALPHABET = "абвгґдеєжзиіїйклмнопрстуфхцчшщьюя"
UK_ORDER = {c: i for i, c in enumerate(UK_ALPHABET)}


def oblast_sort_key(name: str) -> tuple:
    """Oblasts in Ukrainian alphabet order, then cities (м. Київ, Севастополь) by name."""
    is_city = not name.endswith("область")
    bare = name.removeprefix("м. ").lower()
    return (is_city, [UK_ORDER.get(c, len(UK_ORDER)) for c in bare], bare)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9_]([A-Za-z0-9_-]{0,62})(\.[A-Za-z0-9_]([A-Za-z0-9_-]{0,62}))*\.?$")


def parse_tcp_target(host: Any, port: Any) -> tuple[str, int, str | None]:
    """Validate a radio TCP address. Returns (host, port, error)."""
    if not isinstance(host, str) or not host.strip():
        return "", 0, "host must be a non-empty string."
    host = host.strip()
    if len(host) > 253:
        return "", 0, "host is too long."
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not HOSTNAME_RE.match(host):
            return "", 0, "host must be an IPv4 or IPv6 address or a hostname (no scheme, path or spaces)."
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return "", 0, "port must be an integer from 1 to 65535."
    return host, port, None


def create_app(svc, config) -> Flask:
    """Build the Flask app. `svc` must have db, mesh, alerts, notifier, commands, gps, clock."""
    app = Flask(__name__)
    password = config.password

    @app.before_request
    def guard():
        if password:
            auth = request.authorization
            ok = (
                auth is not None
                and auth.username == "admin"
                and hmac.compare_digest((auth.password or "").encode(), password.encode())
            )
            if not ok:
                return Response(
                    "Authentication required.", 401,
                    {"WWW-Authenticate": 'Basic realm="meshalerts"'},
                )
        if request.method == "POST" and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            return jsonify(error=f"Header {CSRF_HEADER}: {CSRF_VALUE} is required."), 403
        return None

    @app.get("/")
    def index():
        return Response(TEMPLATE.read_text(encoding="utf-8"), mimetype="text/html")

    def node_view(node: dict, mesh_entry: dict | None, now: float) -> dict:
        user = (mesh_entry or {}).get("user") or {}
        pos_at = node.get("position_at")
        return {
            "node_id": node["node_id"],
            "name": node.get("node_name") or user.get("longName") or user.get("shortName"),
            "subscribed": bool(node.get("subscribed")),
            "oblast": node.get("oblast"),
            "region_mode": node.get("region_mode") or "gps",
            "fixed_oblast": node.get("fixed_oblast"),
            "gps_oblast": node.get("gps_oblast"),
            "latitude": node.get("latitude"),
            "longitude": node.get("longitude"),
            "position_age_s": None if pos_at is None else max(0, int(now - pos_at)),
            "stale": bool(pos_at is not None and svc.gps.is_stale(node)),
            "rejected_positions": node.get("rejected_positions") or 0,
            "last_rejected_at": node.get("last_rejected_at"),
            "last_fix_rejected": bool(svc.gps.last_fix_rejected(node)),
            "last_notified_oblast": node.get("last_notified_oblast"),
            "last_notified_state": node.get("last_notified_state"),
            "last_seen": node.get("last_seen") or (mesh_entry or {}).get("lastHeard"),
            "in_database": "subscribed" in node,
        }

    @app.get("/api/state")
    def state():
        now = svc.clock()
        mesh_nodes = svc.mesh.known_nodes()
        by_id = {}
        for num, entry in mesh_nodes.items():
            by_id[(entry.get("user") or {}).get("id") or f"!{num:08x}"] = entry
        nodes = []
        seen = set()
        for node in svc.db.list_nodes():
            seen.add(node["node_id"])
            nodes.append(node_view(node, by_id.get(node["node_id"]), now))
        local_id = svc.mesh.local_node_id
        for node_id, entry in by_id.items():
            if node_id in seen or node_id == local_id:
                continue
            stub = {"node_id": node_id, "node_name": None}
            nodes.append(node_view(stub, entry, now))
        poller = svc.alerts
        states = poller.all_states()
        since = {k: v.get("since") for k, v in svc.db.get_alert_states().items()}
        midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
        counts = svc.db.notification_status_counts(int(midnight.timestamp()))
        mesh_cfg = getattr(svc.mesh, "config", None)
        link = svc.mesh.status()
        return jsonify(
            now=int(now),
            connection={
                "connected": svc.mesh.is_connected(),
                "local_node_id": local_id,
                "transport": link["transport"],
                "mode": link["mode"],
                "chosen_port": link["chosen_port"],
                "connecting": link["connecting"],
                "disconnected_by_user": link["disconnected_by_user"],
                "next_retry_at": link["next_retry_at"],
                "node_id": link["node_id"],
                "hw_model": link["hw_model"],
                "firmware": link["firmware"],
                "serial_port": link["active_port"] or getattr(mesh_cfg, "serial_port", None) or None,
                "host": link.get("host", getattr(mesh_cfg, "host", None)),
                "port": link.get("port", getattr(mesh_cfg, "port", None)),
                "last_error": link["last_error"],
            },
            poller={
                "fresh": poller.is_fresh(),
                "last_success_at": poller.last_success_at,
                "last_error": poller.last_error,
                "source": poller.source,
            },
            queue_length=svc.notifier.queue_length(),
            alerts=[
                {"oblast": k, "active": v, "since": since.get(k) if v else None}
                for k, v in sorted(states.items(), key=lambda kv: oblast_sort_key(kv[0]))
            ],
            messages_today={
                "total": sum(counts.values()),
                "delivered": counts.get("delivered", 0) + counts.get("implicit_ack", 0),
                "no_ack": counts.get("no_ack", 0),
                "failed": sum(n for k, n in counts.items() if k.startswith("failed:")),
            },
            nodes=nodes,
        )

    @app.get("/api/notifications")
    def notifications():
        try:
            limit = max(1, min(500, int(request.args.get("limit", 100))))
        except ValueError:
            return jsonify(error="limit must be an integer."), 400
        return jsonify(notifications=svc.db.list_notifications(limit))

    def body() -> dict:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    def valid_node(node_id: Any) -> bool:
        return isinstance(node_id, str) and bool(NODE_ID_RE.match(node_id))

    @app.get("/api/ports")
    def ports():
        try:
            return jsonify(ports=svc.mesh.list_ports())
        except Exception as exc:
            log.warning("Cannot list serial ports: %s", exc)
            return jsonify(error=f"Cannot list serial ports: {exc}"), 500

    @app.post("/api/connection/connect")
    def connection_connect():
        data = body()
        port = data.get("port")
        allow_any = data.get("allow_any_node", False)
        transport = data.get("transport", "serial")
        if transport not in ("serial", "tcp"):
            return jsonify(error="transport must be serial or tcp."), 400
        if not isinstance(allow_any, bool):
            return jsonify(error="allow_any_node must be true or false."), 400
        if transport == "tcp":
            host, tcp_port, err = parse_tcp_target(data.get("host"), data.get("port", 4403))
            if err:
                return jsonify(error=err), 400
            svc.mesh.connect(None, allow_any_node=allow_any, tcp=(host, tcp_port))
            return jsonify(ok=True)
        if port is not None and not isinstance(port, str):
            return jsonify(error="port must be a string or null."), 400
        if port is not None:
            try:
                known = {p["path"] for p in svc.mesh.list_ports()}
            except Exception as exc:
                return jsonify(error=f"Cannot list serial ports: {exc}"), 500
            if port not in known:
                return jsonify(error="Unknown port. Refresh the port list."), 400
        svc.mesh.connect(port, allow_any_node=allow_any)
        return jsonify(ok=True)

    @app.post("/api/connection/disconnect")
    def connection_disconnect():
        svc.mesh.disconnect()
        return jsonify(ok=True)

    @app.post("/api/nodes/<node_id>/subscribe")
    def subscribe(node_id):
        if not valid_node(node_id):
            return jsonify(error="Invalid node ID."), 400
        notify = bool(body().get("notify", False))
        reply = svc.commands.subscribe(node_id, node_num_from_id(node_id))
        notif_id = svc.notifier.enqueue(node_id, reply, "reply") if notify else None
        return jsonify(ok=True, reply=reply, notification_id=notif_id)

    @app.post("/api/nodes/<node_id>/request-position")
    def request_position(node_id):
        if not valid_node(node_id):
            return jsonify(error="Invalid node ID."), 400
        try:
            svc.mesh.request_position(node_num_from_id(node_id))
        except NotConnected:
            return jsonify(error="Radio is not connected."), 409
        except Exception as exc:
            log.warning("Position request to %s failed: %s", node_id, exc)
            return jsonify(error=f"Could not send request: {exc}"), 500
        return jsonify(ok=True)

    @app.post("/api/nodes/<node_id>/unsubscribe")
    def unsubscribe(node_id):
        if not valid_node(node_id):
            return jsonify(error="Invalid node ID."), 400
        if svc.db.get_node(node_id) is None:
            return jsonify(error="Node is not in the database."), 404
        notify = bool(body().get("notify", False))
        reply = svc.commands.unsubscribe(node_id)
        notif_id = svc.notifier.enqueue(node_id, reply, "reply") if notify else None
        return jsonify(ok=True, reply=reply, notification_id=notif_id)

    @app.post("/api/nodes/<node_id>/region")
    def set_region(node_id):
        if not valid_node(node_id):
            return jsonify(error="Invalid node ID."), 400
        data = body()
        region = data.get("region")
        notify = data.get("notify", False)
        if not isinstance(notify, bool):
            return jsonify(error="notify must be true or false."), 400
        if region is not None and (not isinstance(region, str) or region not in API_NAMES):
            return jsonify(error="region must be null or a region name from the alert API."), 400
        if region is None:
            if svc.db.get_node(node_id) is None:
                return jsonify(error="Node is not in the database."), 404
            reply = svc.commands.use_gps_region(node_id)
        else:
            reply = svc.commands.set_fixed_region(node_id, region, node_num_from_id(node_id))
        node = svc.db.get_node(node_id)
        notif_id = svc.notifier.enqueue(node_id, reply, "reply") if notify else None
        return jsonify(
            ok=True, reply=reply, notification_id=notif_id,
            region_mode=node["region_mode"], oblast=node["oblast"],
        )

    @app.post("/api/nodes")
    def add_node():
        node_id = body().get("node_id")
        if not valid_node(node_id):
            return jsonify(error="node_id must match !xxxxxxxx (8 lowercase hex digits)."), 400
        reply = svc.commands.subscribe(node_id, node_num_from_id(node_id))
        return jsonify(ok=True, reply=reply)

    def target_node():
        node_id = body().get("node_id")
        if not valid_node(node_id):
            return None, (jsonify(error="Invalid node ID."), 400)
        node = svc.db.get_node(node_id)
        if node is None:
            node = svc.db.ensure_node(node_id, node_num_from_id(node_id))
        return node, None

    @app.post("/api/send")
    def send():
        text = body().get("text")
        if not isinstance(text, str) or not text.strip():
            return jsonify(error="Text is empty."), 400
        if len(text.encode("utf-8")) > messages.MAX_TEXT_BYTES:
            return jsonify(error=f"Text is longer than {messages.MAX_TEXT_BYTES} bytes."), 400
        node, err = target_node()
        if err:
            return err
        notif_id = svc.notifier.enqueue(node["node_id"], text.strip(), "manual")
        return jsonify(ok=True, notification_id=notif_id)

    @app.post("/api/test-alert")
    def test_alert():
        node, err = target_node()
        if err:
            return err
        text = messages.test_alert(node.get("oblast"))
        notif_id = svc.notifier.enqueue(node["node_id"], text, "test")
        return jsonify(ok=True, notification_id=notif_id)

    return app


class DashboardServer:
    def __init__(self, svc, config):
        self.config = config
        self.app = create_app(svc, config)
        self._server = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if not is_loopback(self.config.host) and not self.config.password:
            log.error(
                "Dashboard is not started. Host %s is not loopback and no password is set.",
                self.config.host,
            )
            return False
        self._server = make_server(self.config.host, self.config.port, self.app, threaded=True)
        self._thread = threading.Thread(target=self._server.serve_forever, name="dashboard", daemon=True)
        self._thread.start()
        log.info("Dashboard listens on http://%s:%d/.", self.config.host, self.config.port)
        return True

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)
