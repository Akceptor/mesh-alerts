"""Position validation, staleness, and oblast update."""

from __future__ import annotations

import logging
import math
from typing import Any, Callable

from .db import Database
from .oblast import OUTSIDE_UKRAINE, OblastIndex, Resolution

log = logging.getLogger(__name__)

MAX_FUTURE_S = 300
MAX_AGE_S = 30 * 86400
REJECT_LOG_INTERVAL_S = 600


def extract_latlon(position: dict) -> tuple[float, float] | None:
    lat = position.get("latitude")
    lon = position.get("longitude")
    if lat is None and position.get("latitudeI") is not None:
        lat = position["latitudeI"] / 1e7
    if lon is None and position.get("longitudeI") is not None:
        lon = position["longitudeI"] / 1e7
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    if isinstance(lat, bool) or isinstance(lon, bool):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if lat == 0 and lon == 0:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return float(lat), float(lon)


class GpsTracker:
    def __init__(
        self,
        db: Database,
        oblasts: OblastIndex,
        config,
        clock: Callable[[], float],
        on_region_change: Callable[[dict, str | None, str | None], None] | None = None,
    ):
        self.db = db
        self.oblasts = oblasts
        self.config = config
        self.clock = clock
        self.on_region_change = on_region_change
        self.rejected: dict[str, int] = {}
        self._reject_logged: dict[str, float] = {}

    def _timestamp(self, position: dict, rx_time: Any) -> int:
        now = self.clock()
        for raw in (position.get("time"), rx_time):
            if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
                if now - MAX_AGE_S <= raw <= now + MAX_FUTURE_S:
                    return int(raw)
        return int(now)

    def handle_position(
        self,
        node_id: str,
        position: dict,
        rx_time: Any = None,
        notify: bool = True,
    ) -> Resolution | None:
        """Store a position for a subscribed node. Return the oblast resolution."""
        node = self.db.get_node(node_id)
        if node is None or not node["subscribed"]:
            return None
        latlon = extract_latlon(position)
        if latlon is None:
            log.debug("Ignore invalid position from %s.", node_id)
            return None
        lat, lon = latlon
        if self.config.reject_outside_ukraine and self.oblasts.resolve(lat, lon).status == OUTSIDE_UKRAINE:
            self._reject(node_id, lat, lon)
            return None
        ts = self._timestamp(position, rx_time)
        if node["position_at"] is not None and ts < node["position_at"]:
            log.debug("Ignore old position from %s.", node_id)
            return None

        fixed = node["region_mode"] == "fixed"
        resolution = self.oblasts.resolve(
            lat, lon, current=node["gps_oblast"] if fixed else node["oblast"]
        )
        fields = {"latitude": lat, "longitude": lon, "position_at": ts, "gps_oblast": resolution.oblast}
        if not fixed:
            fields["oblast"] = resolution.oblast
        self.db.update_node(node_id, **fields)
        if self.config.position_history:
            self.db.insert_position(node_id, lat, lon, ts)
        log.info(
            "GPS update %s: %.5f, %.5f, region %s (%s).",
            node_id, lat, lon, resolution.oblast, resolution.status,
        )
        if fixed:
            log.debug(
                "Region of %s is fixed (%s). GPS region %s is stored only.",
                node_id, node["fixed_oblast"], resolution.oblast,
            )
        elif resolution.oblast != node["oblast"]:
            log.info("Region change %s: %s -> %s.", node_id, node["oblast"], resolution.oblast)
            if notify and self.on_region_change is not None:
                updated = self.db.get_node(node_id)
                self.on_region_change(updated, node["oblast"], resolution.oblast)
        return resolution

    def _reject(self, node_id: str, lat: float, lon: float) -> None:
        now = self.clock()
        self.rejected[node_id] = self.rejected.get(node_id, 0) + 1
        self.db.record_rejection(node_id, int(now))
        last = self._reject_logged.get(node_id)
        if last is None or now - last >= REJECT_LOG_INTERVAL_S:
            self._reject_logged[node_id] = now
            log.warning(
                "Position rejected: outside Ukraine (possible spoofing). Node %s, %.5f, %.5f.",
                node_id, lat, lon,
            )

    def last_fix_rejected(self, node: dict) -> bool:
        rejected_at = node.get("last_rejected_at")
        if rejected_at is None:
            return False
        return node.get("position_at") is None or rejected_at >= node["position_at"]

    def is_stale(self, node: dict) -> bool:
        if node.get("position_at") is None:
            return False
        return self.clock() - node["position_at"] > self.config.stale_after_s

    def seed_from_node_db(self, known_nodes: dict[int, dict]) -> None:
        """Use positions in the radio node database for subscribed nodes."""
        for node in self.db.subscribed_nodes():
            entry = known_nodes.get(node["node_num"])
            if not entry:
                continue
            self.seed_node(node["node_id"], entry)

    def seed_node(self, node_id: str, entry: dict, notify: bool = True) -> Resolution | None:
        user = entry.get("user") or {}
        name = user.get("longName") or user.get("shortName")
        if name:
            self.db.update_node(node_id, node_name=name)
        last_heard = entry.get("lastHeard")
        if last_heard:
            self.db.touch_node(node_id, int(last_heard))
        position = dict(entry.get("position") or {})
        if not position:
            return None
        position.setdefault("time", last_heard)
        return self.handle_position(node_id, position, last_heard, notify=notify)

    def maintenance(self) -> None:
        now = int(self.clock())
        removed = self.db.prune_positions(now - self.config.history_days * 86400)
        if removed:
            log.info("Removed %d old position rows.", removed)
        drop = self.config.drop_oblast_after_s
        if drop > 0:
            for node in self.db.subscribed_nodes():
                if node["gps_oblast"] and node["position_at"] and now - node["position_at"] > drop:
                    log.info("Position of %s is too old. Clear GPS region.", node["node_id"])
                    fields = {"gps_oblast": None}
                    if node["region_mode"] != "fixed":
                        fields["oblast"] = None
                    self.db.update_node(node["node_id"], **fields)
