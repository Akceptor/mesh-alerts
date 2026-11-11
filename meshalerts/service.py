"""Wire all parts together. Start and stop threads."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from .alerts import AlertPoller
from .commands import CommandHandler
from .config import Config
from .dashboard import DashboardServer
from .db import Database, node_id_from_num
from .gps import GpsTracker
from .mesh import MeshConnection
from .notifier import Notifier
from .oblast import OblastIndex

log = logging.getLogger(__name__)

MAINTENANCE_INTERVAL_S = 86400


class Service:
    def __init__(self, config: Config, clock: Callable[[], float] = time.time, mesh=None):
        self.config = config
        self.clock = clock
        self.db = Database(config.storage.db_path)
        self.oblasts = OblastIndex(
            config.gps.geojson_path, config.gps.border_margin_m, config.gps.outside_margin_m
        )
        self.mesh = mesh or MeshConnection(
            config.meshtastic, config.tx,
            on_text=self._on_text,
            on_position=self._on_position,
            on_nodeinfo=self._on_nodeinfo,
            on_connected=self._on_connected,
            settings=self.db,
        )
        self.notifier: Notifier
        self.gps = GpsTracker(
            self.db, self.oblasts, config.gps, clock,
            on_region_change=lambda node, old, new: self.notifier.on_region_change(node, old, new),
        )
        self.alerts = AlertPoller(
            config.alerts, self.db,
            on_transition=lambda t: self.notifier.on_transition(t),
            clock=clock,
            on_first_valid_poll=lambda: self.notifier.reconcile(),
        )
        self.notifier = Notifier(
            self.db, self.mesh, config.tx, config.alerts, clock,
            state_of=self.alerts.current_state, oblasts=self.oblasts,
        )
        self.commands = CommandHandler(
            self.db, self.gps, self.oblasts, self.alerts, self.notifier,
            self.mesh, config.tx, config.alerts, clock,
        )
        self.dashboard = DashboardServer(self, config.dashboard) if config.dashboard.enabled else None
        self._stop = threading.Event()

    # mesh handlers
    @staticmethod
    def _node_id(packet: dict) -> str | None:
        node_id = packet.get("fromId")
        if isinstance(node_id, str) and node_id.startswith("!"):
            return node_id
        num = packet.get("from")
        if isinstance(num, int) and not isinstance(num, bool):
            return node_id_from_num(num)
        return None

    def _touch(self, node_id: str) -> None:
        if self.db.get_node(node_id) is not None:
            self.db.touch_node(node_id, int(self.clock()))

    def _on_text(self, packet: dict) -> None:
        node_id = self._node_id(packet)
        if node_id:
            self._touch(node_id)
        self.commands.handle_text(packet)

    def _on_position(self, packet: dict) -> None:
        node_id = self._node_id(packet)
        position = (packet.get("decoded") or {}).get("position")
        if not node_id or not isinstance(position, dict):
            return
        self._touch(node_id)
        self.gps.handle_position(node_id, position, packet.get("rxTime"))

    def _on_nodeinfo(self, packet: dict) -> None:
        node_id = self._node_id(packet)
        user = (packet.get("decoded") or {}).get("user")
        if not node_id or self.db.get_node(node_id) is None:
            return
        self._touch(node_id)
        name = isinstance(user, dict) and (user.get("longName") or user.get("shortName"))
        if name:
            self.db.update_node(node_id, node_name=name)

    def _on_connected(self, iface) -> None:
        self.gps.seed_from_node_db(self.mesh.known_nodes())

    def _maintenance_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.gps.maintenance()
            except Exception:
                log.exception("Maintenance failed.")
            self._stop.wait(MAINTENANCE_INTERVAL_S)

    def start(self) -> None:
        self.alerts.start()
        self.notifier.start()
        self.mesh.start()
        if self.dashboard:
            try:
                self.dashboard.start()
            except OSError as exc:
                log.error("Dashboard cannot start: %s", exc)
        threading.Thread(target=self._maintenance_loop, name="maintenance", daemon=True).start()

    def stop(self) -> None:
        log.info("Stop service.")
        self._stop.set()
        if self.dashboard:
            self.dashboard.stop()
        self.mesh.stop()
        self.notifier.stop()
        self.alerts.stop()
        self.db.close()

    def run_forever(self) -> None:
        import signal

        def handler(signum, frame):
            log.info("Received signal %d.", signum)
            self._stop.set()

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)
        self.start()
        while not self._stop.wait(1):
            pass
        self.stop()
