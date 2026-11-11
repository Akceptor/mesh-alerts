"""SQLite storage. One shared connection, guarded by a lock."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
  node_id TEXT PRIMARY KEY,
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
  last_notified_state INTEGER,
  rejected_positions INTEGER NOT NULL DEFAULT 0,
  last_rejected_at INTEGER,
  region_mode TEXT NOT NULL DEFAULT 'gps',
  fixed_oblast TEXT,
  gps_oblast TEXT
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
  updated_at INTEGER NOT NULL,
  since INTEGER
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id TEXT NOT NULL,
  oblast TEXT,
  alert_state INTEGER,
  kind TEXT NOT NULL,
  text TEXT NOT NULL,
  packet_id INTEGER,
  queued_at INTEGER NOT NULL,
  sent_at INTEGER,
  acknowledgement_status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notifications_node ON notifications(node_id, id);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

NODE_UPDATE_FIELDS = {
    "node_num",
    "node_name",
    "subscribed",
    "registered_at",
    "last_seen",
    "latitude",
    "longitude",
    "position_at",
    "oblast",
    "last_notified_oblast",
    "last_notified_state",
    "rejected_positions",
    "last_rejected_at",
    "region_mode",
    "fixed_oblast",
    "gps_oblast",
}


def node_id_from_num(num: int) -> str:
    return f"!{num:08x}"


def node_num_from_id(node_id: str) -> int:
    return int(node_id[1:], 16)


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema version {version} is newer than this program supports."
                )
            self._conn.executescript(SCHEMA)
            columns = {r[1] for r in self._conn.execute("PRAGMA table_info(nodes)")}
            if "rejected_positions" not in columns:
                self._conn.execute(
                    "ALTER TABLE nodes ADD COLUMN rejected_positions INTEGER NOT NULL DEFAULT 0"
                )
            if "last_rejected_at" not in columns:
                self._conn.execute("ALTER TABLE nodes ADD COLUMN last_rejected_at INTEGER")
            if "region_mode" not in columns:
                self._conn.execute(
                    "ALTER TABLE nodes ADD COLUMN region_mode TEXT NOT NULL DEFAULT 'gps'"
                )
            if "fixed_oblast" not in columns:
                self._conn.execute("ALTER TABLE nodes ADD COLUMN fixed_oblast TEXT")
            if "gps_oblast" not in columns:
                self._conn.execute("ALTER TABLE nodes ADD COLUMN gps_oblast TEXT")
                self._conn.execute("UPDATE nodes SET gps_oblast=oblast")
            alert_columns = {r[1] for r in self._conn.execute("PRAGMA table_info(alert_states)")}
            if "since" not in alert_columns:
                self._conn.execute("ALTER TABLE alert_states ADD COLUMN since INTEGER")
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def _one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

    # settings
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # nodes
    def get_node(self, node_id: str) -> dict | None:
        return self._one("SELECT * FROM nodes WHERE node_id=?", (node_id,))

    def list_nodes(self) -> list[dict]:
        return self._all("SELECT * FROM nodes ORDER BY node_id")

    def subscribed_nodes(self) -> list[dict]:
        return self._all("SELECT * FROM nodes WHERE subscribed=1 ORDER BY node_id")

    def ensure_node(self, node_id: str, node_num: int | None = None, name: str | None = None) -> dict:
        if node_num is None:
            node_num = node_num_from_id(node_id)
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO nodes(node_id, node_num, node_name) VALUES(?,?,?)",
                (node_id, node_num, name),
            )
            if name:
                self._conn.execute(
                    "UPDATE nodes SET node_name=? WHERE node_id=?", (name, node_id)
                )
            self._conn.commit()
        node = self.get_node(node_id)
        assert node is not None
        return node

    def update_node(self, node_id: str, **fields: Any) -> None:
        if not fields:
            return
        bad = set(fields) - NODE_UPDATE_FIELDS
        if bad:
            raise ValueError(f"Unknown node fields: {sorted(bad)}")
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE nodes SET {sets} WHERE node_id=?", (*fields.values(), node_id))

    def record_rejection(self, node_id: str, now: int) -> None:
        self._exec(
            "UPDATE nodes SET rejected_positions=rejected_positions+1, last_rejected_at=? WHERE node_id=?",
            (now, node_id),
        )

    def touch_node(self, node_id: str, now: int) -> None:
        self._exec("UPDATE nodes SET last_seen=? WHERE node_id=?", (now, node_id))

    def insert_position(self, node_id: str, lat: float, lon: float, ts: int) -> None:
        self._exec(
            "INSERT INTO positions(node_id, latitude, longitude, timestamp) VALUES(?,?,?,?)",
            (node_id, lat, lon, ts),
        )

    def position_count(self, node_id: str) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM positions WHERE node_id=?", (node_id,))
        return row["n"] if row else 0

    def prune_positions(self, older_than: int) -> int:
        return self._exec("DELETE FROM positions WHERE timestamp<?", (older_than,)).rowcount

    # alert states
    def get_alert_states(self) -> dict[str, dict]:
        return {r["oblast"]: r for r in self._all("SELECT * FROM alert_states")}

    def set_alert_state(self, oblast: str, active: bool, now: int, since: int | None = None) -> None:
        since = since if active else None
        self._exec(
            "INSERT INTO alert_states(oblast, active, updated_at, since) VALUES(?,?,?,?) "
            "ON CONFLICT(oblast) DO UPDATE SET active=excluded.active, "
            "updated_at=excluded.updated_at, since=excluded.since",
            (oblast, int(active), now, since),
        )

    def set_alert_since(self, oblast: str, since: int) -> None:
        self._exec("UPDATE alert_states SET since=? WHERE oblast=? AND active=1", (since, oblast))

    # notifications
    def insert_notification(
        self,
        node_id: str,
        kind: str,
        text: str,
        queued_at: int,
        oblast: str | None = None,
        alert_state: bool | None = None,
        status: str = "queued",
    ) -> int:
        cur = self._exec(
            "INSERT INTO notifications(node_id, oblast, alert_state, kind, text, queued_at, "
            "acknowledgement_status) VALUES(?,?,?,?,?,?,?)",
            (
                node_id,
                oblast,
                None if alert_state is None else int(alert_state),
                kind,
                text,
                queued_at,
                status,
            ),
        )
        return int(cur.lastrowid)

    def mark_sent(self, notif_id: int, packet_id: int | None, now: int) -> None:
        self._exec(
            "UPDATE notifications SET acknowledgement_status='sent', packet_id=?, sent_at=? WHERE id=?",
            (packet_id, now, notif_id),
        )

    def set_status(self, notif_id: int, status: str) -> None:
        self._exec(
            "UPDATE notifications SET acknowledgement_status=? WHERE id=?", (status, notif_id)
        )

    def set_ack_status(self, notif_id: int, status: str) -> bool:
        """Set an ACK result. A delivered result wins. Other results never replace a final one."""
        if status == "delivered":
            cond = "acknowledgement_status IN ('sent','no_ack','implicit_ack') OR acknowledgement_status LIKE 'failed:%'"
        else:
            cond = "acknowledgement_status IN ('sent','no_ack')"
        cur = self._exec(
            f"UPDATE notifications SET acknowledgement_status=? WHERE id=? AND ({cond})",
            (status, notif_id),
        )
        return cur.rowcount > 0

    def get_notification(self, notif_id: int) -> dict | None:
        return self._one("SELECT * FROM notifications WHERE id=?", (notif_id,))

    def notification_status_counts(self, since: int) -> dict[str, int]:
        rows = self._all(
            "SELECT acknowledgement_status AS status, COUNT(*) AS n FROM notifications "
            "WHERE queued_at>=? GROUP BY acknowledgement_status",
            (since,),
        )
        return {r["status"]: r["n"] for r in rows}

    def list_notifications(self, limit: int = 100, node_id: str | None = None) -> list[dict]:
        if node_id:
            return self._all(
                "SELECT * FROM notifications WHERE node_id=? ORDER BY id DESC LIMIT ?",
                (node_id, limit),
            )
        return self._all("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,))
