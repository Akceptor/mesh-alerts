"""Private text commands from mesh nodes."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from . import messages, regions
from .db import Database, node_id_from_num, node_num_from_id
from .oblast import NO_API_DATA, OUTSIDE_UKRAINE

log = logging.getLogger(__name__)

COMMANDS = (
    "!start", "!stop", "!status", "!location", "!help", "!gps", "!auto", "!region", "!regions",
)
MAX_TEXT_CHARS = 200
MAX_ALIAS_TOKENS = 3


class CommandHandler:
    def __init__(self, db, gps, oblasts, alerts, notifier, mesh, tx_config, alerts_config, clock):
        self.db: Database = db
        self.gps = gps
        self.oblasts = oblasts
        self.alerts = alerts
        self.notifier = notifier
        self.mesh = mesh
        self.tx = tx_config
        self.alerts_config = alerts_config
        self.clock = clock
        self._last_reply: dict[tuple[str, str], float] = {}

    # packet entry point
    def handle_text(self, packet: dict) -> None:
        try:
            decoded = packet.get("decoded") or {}
            text = decoded.get("text")
            sender = packet.get("from")
            dest = packet.get("to")
        except AttributeError:
            return
        if not isinstance(text, str) or not isinstance(sender, int) or isinstance(sender, bool):
            return
        local = self.mesh.local_node_num
        if local is None or sender == local:
            return
        node_id = packet.get("fromId")
        if not isinstance(node_id, str) or not node_id.startswith("!"):
            node_id = node_id_from_num(sender)
        known = self.db.get_node(node_id)
        if known is not None:
            self.db.touch_node(node_id, int(self.clock()))
        if dest != local:
            log.debug("Ignore text from %s: not a private message.", node_id)
            return
        if len(text) > MAX_TEXT_CHARS:
            return
        parts = text.strip().lower().split()
        if not parts or not parts[0].startswith("!"):
            return
        command = parts[0]
        args: list[str] = parts[1:]
        if command not in COMMANDS:
            # Any other !word is a region alias. "!ivano frankivsk" is one alias in several tokens.
            if len(parts) > MAX_ALIAS_TOKENS or not regions.normalize(" ".join(parts)):
                return
            args = [" ".join(parts)[1:]]
            command = "!region"

        now = self.clock()
        key = (node_id, command)
        last = self._last_reply.get(key)
        if last is not None and now - last < self.tx.command_cooldown_s:
            log.info("Drop %s from %s: cooldown.", command, node_id)
            return
        self._last_reply[key] = now
        log.info("Command %s from %s.", command, node_id)

        self.db.ensure_node(node_id, sender)
        reply = getattr(self, "_cmd_" + command[1:])(node_id, sender, args)
        for text in [reply] if isinstance(reply, str) else reply:
            self.notifier.enqueue(node_id, text, "reply")

    # shared with the dashboard
    def subscribe(self, node_id: str, node_num: int | None = None) -> str:
        now = int(self.clock())
        entry = self._mesh_entry(node_id, node_num)
        name = ((entry or {}).get("user") or {}).get("longName")
        node = self.db.ensure_node(node_id, node_num, name)
        self.db.update_node(node_id, subscribed=1, registered_at=now, last_seen=node["last_seen"] or now)
        log.info("Registration: %s subscribed (%s).", node_id, name or "no name")
        if node["position_at"] is None and entry:
            self.gps.seed_node(node_id, entry, notify=False)
        node = self.db.get_node(node_id)
        state = self._reset_dedup(node)
        return messages.alerts_enabled(
            node["oblast"], state, node["position_at"] is not None, node["region_mode"] == "fixed"
        )

    def set_fixed_region(self, node_id: str, name: str, node_num: int | None = None) -> str:
        """Pin the node to a region. GPS keeps storing positions but no longer moves the region."""
        node = self.db.get_node(node_id)
        if node is None or not node["subscribed"]:
            self.subscribe(node_id, node_num)
        self.db.update_node(node_id, region_mode="fixed", fixed_oblast=name, oblast=name)
        log.info("Region of %s fixed to %s.", node_id, name)
        node = self.db.get_node(node_id)
        state = self._reset_dedup(node)
        return messages.alerts_enabled(name, state, True, fixed=True)

    def use_gps_region(self, node_id: str, node_num: int | None = None) -> str:
        """Return the node to the GPS region, recomputed from the last valid stored position."""
        node = self.db.get_node(node_id)
        if node is None or not node["subscribed"]:
            self.subscribe(node_id, node_num)
            node = self.db.get_node(node_id)
        drop = self.gps.config.drop_oblast_after_s
        too_old = drop > 0 and node["position_at"] is not None and self.clock() - node["position_at"] > drop
        if node["latitude"] is None or too_old:
            gps_oblast = None
        else:
            gps_oblast = self.oblasts.resolve(
                node["latitude"], node["longitude"], current=node["gps_oblast"]
            ).oblast
        self.db.update_node(
            node_id, region_mode="gps", fixed_oblast=None, gps_oblast=gps_oblast, oblast=gps_oblast
        )
        log.info("Region of %s follows GPS again: %s.", node_id, gps_oblast)
        node = self.db.get_node(node_id)
        state = self._reset_dedup(node)
        return messages.gps_region_reply(gps_oblast, state)

    def _reset_dedup(self, node: dict) -> bool | None:
        """The reply carries the state, so the next alert message is a real change."""
        state = self._state_for(node)
        if node["oblast"] is not None and state is not None:
            self.notifier.set_dedup(node["node_id"], node["oblast"], state)
        else:
            self.notifier.set_dedup(node["node_id"], None, None)
        return state

    def unsubscribe(self, node_id: str) -> str:
        node = self.db.get_node(node_id)
        if node is not None:
            self.db.update_node(node_id, subscribed=0)
            log.info("Registration: %s unsubscribed.", node_id)
        return messages.ALERTS_DISABLED

    # commands
    def _cmd_start(self, node_id: str, sender: int, args: list[str]) -> str:
        return self.subscribe(node_id, sender)

    def _cmd_stop(self, node_id: str, sender: int, args: list[str]) -> str:
        return self.unsubscribe(node_id)

    def _cmd_help(self, node_id: str, sender: int, args: list[str]) -> str:
        return messages.HELP

    def _cmd_region(self, node_id: str, sender: int, args: list[str]) -> str:
        name = regions.lookup(" ".join(args))
        if name is None:
            return messages.UNKNOWN_REGION
        if name in NO_API_DATA:
            return messages.CRIMEA_NO_DATA
        return self.set_fixed_region(node_id, name, sender)

    def _cmd_regions(self, node_id: str, sender: int, args: list[str]) -> list[str]:
        return messages.regions_lists(list(regions.CANONICAL.values()))

    def _cmd_gps(self, node_id: str, sender: int, args: list[str]) -> str:
        return self.use_gps_region(node_id, sender)

    _cmd_auto = _cmd_gps

    def _cmd_status(self, node_id: str, sender: int, args: list[str]) -> str:
        loc = self.locate(node_id, sender)
        node = loc["node"]
        state = self._state_for(node, loc["oblast"])
        age = loc["age_min"]
        stale_position = loc["stale"]
        data_age = self.alerts.data_age_min()
        return messages.status_reply(
            loc["oblast"], state, loc["lat"] is not None, age, data_age,
            stale_position and not loc["fixed"], not self.alerts.is_fresh(), loc["fixed"],
        )

    def _cmd_location(self, node_id: str, sender: int, args: list[str]) -> str:
        loc = self.locate(node_id, sender)
        tag = " (fixed)" if loc["fixed"] else ""
        if loc["lat"] is None:
            text = messages.location_reply(None, None, None, None, "")
            if loc["fixed"]:
                text += f" Region: {loc['oblast']}{tag}."
            return text + " " + messages.REJECTED_NOTE if loc["rejected"] else text
        ts = loc["time"]
        hhmm = time.strftime("%H:%M", time.gmtime(ts))
        if loc["oblast"]:
            region = loc["oblast"]
        else:
            region = "outside Ukraine"
        text = messages.location_reply(loc["lat"], loc["lon"], hhmm, loc["age_min"], region + tag)
        if loc["rejected"]:
            text += " " + messages.REJECTED_NOTE
        return text

    def locate(self, node_id: str, node_num: int | None = None) -> dict:
        """Find the best known position. Use the radio node database if nothing is stored."""
        node = self.db.get_node(node_id) or {
            "node_id": node_id, "node_num": node_num, "subscribed": 0, "latitude": None,
            "longitude": None, "position_at": None, "oblast": None,
            "last_rejected_at": None,
        }
        fixed = node.get("region_mode") == "fixed"
        out = {"node": node, "lat": None, "lon": None, "time": None,
               "oblast": None, "stale": False, "age_min": None, "fixed": fixed,
               "rejected": self.gps.last_fix_rejected(node)}
        if node["latitude"] is not None:
            out.update(lat=node["latitude"], lon=node["longitude"], time=node["position_at"],
                       oblast=node["oblast"], stale=self.gps.is_stale(node))
        else:
            entry = self._mesh_entry(node_id, node_num)
            position = (entry or {}).get("position") or {}
            from .gps import extract_latlon

            latlon = extract_latlon(position)
            if latlon and self.gps.config.reject_outside_ukraine and \
                    self.oblasts.resolve(*latlon).status == OUTSIDE_UKRAINE:
                out["rejected"] = True
            elif latlon:
                ts = position.get("time") or (entry or {}).get("lastHeard") or int(self.clock())
                res = self.oblasts.resolve(*latlon)
                out.update(lat=latlon[0], lon=latlon[1], time=int(ts), oblast=res.oblast)
                out["stale"] = self.clock() - ts > self.gps.config.stale_after_s
        if fixed:
            out["oblast"] = node["fixed_oblast"]
        if out["time"] is not None:
            out["age_min"] = max(0, int((self.clock() - out["time"]) // 60))
        return out

    def _state_for(self, node: dict, oblast: str | None = None) -> bool | None:
        if oblast is not None and oblast != node.get("oblast"):
            return self.alerts.current_state(oblast)
        if node.get("oblast") is None:
            return None
        return self.notifier.effective_state(node)

    def _mesh_entry(self, node_id: str, node_num: int | None) -> dict | None:
        num = node_num if node_num is not None else node_num_from_id(node_id)
        try:
            return self.mesh.known_nodes().get(num)
        except Exception:
            log.exception("Cannot read radio node database.")
            return None
