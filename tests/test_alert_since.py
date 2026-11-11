import sqlite3
from datetime import datetime, timezone

from conftest import Env, all_quiet
from meshalerts.alerts import parse_since

LVIV = "Львівська область"
LUHANSK = "Луганська область"


def utc(*a):
    return int(datetime(*a, tzinfo=timezone.utc).timestamp())


def active_lviv(env, changed, active=True):
    data = all_quiet()
    data["states"][LVIV] = {"alertnow": active, "changed": changed}
    return data


def test_parse_since_winter_is_utc_plus_2():
    assert parse_since("2026-01-15 12:00:00", utc(2026, 1, 16)) == utc(2026, 1, 15, 10)


def test_parse_since_summer_is_utc_plus_3():
    assert parse_since("2026-07-15 12:00:00", utc(2026, 7, 16)) == utc(2026, 7, 15, 9)


def test_parse_since_around_dst_change():
    # Kyiv DST starts 2026-03-29 03:00 (+2 -> +3), ends 2026-10-25 04:00 (+3 -> +2).
    now = utc(2027, 1, 1)
    assert parse_since("2026-03-29 02:30:00", now) == utc(2026, 3, 29, 0, 30)
    assert parse_since("2026-03-29 12:00:00", now) == utc(2026, 3, 29, 9)
    assert parse_since("2026-10-25 12:00:00", now) == utc(2026, 10, 25, 10)


def test_parse_since_bad_values():
    now = utc(2026, 1, 1)
    for bad in (None, 5, "", "garbage", "2026-13-40 00:00:00", "2025-12-31T10:00:00"):
        assert parse_since(bad, now) is None


def test_parse_since_future_tolerance():
    now = utc(2026, 1, 15, 10)
    assert parse_since("2026-01-15 12:04:00", now) == utc(2026, 1, 15, 10, 4)
    assert parse_since("2026-01-15 12:06:00", now) is None


def test_active_since_uses_changed_not_first_seen(env):
    env.clock.t = utc(2026, 11, 10, 12)
    env.poll(active_lviv(env, "2026-11-10 10:06:43"))
    assert env.db.get_alert_states()[LVIV]["since"] == utc(2026, 11, 10, 8, 6, 43)
    assert env.db.get_alert_states()[LVIV]["updated_at"] == int(env.clock())


def test_old_since_kept_after_restart(env):
    env.clock.t = utc(2026, 11, 10, 12)
    data = all_quiet()
    data["states"][LUHANSK] = {"alertnow": True, "changed": "2022-04-04 10:00:00"}
    env.poll(data)
    env.clock.advance(3600)
    env.build()
    data["states"][LUHANSK]["alertnow"] = True
    assert env.poll(data)
    assert env.db.get_alert_states()[LUHANSK]["since"] == utc(2022, 4, 4, 7)


def test_transition_to_active_sets_since_and_inactive_clears(env):
    env.clock.t = utc(2026, 11, 10, 12)
    env.poll(active_lviv(env, "2026-11-10 13:00:00", False))
    assert env.db.get_alert_states()[LVIV]["since"] is None
    env.clock.advance(60)
    env.poll(active_lviv(env, "2026-11-10 13:30:00", True))
    assert env.db.get_alert_states()[LVIV]["since"] == utc(2026, 11, 10, 11, 30)
    env.clock.advance(60)
    env.poll(active_lviv(env, "2026-11-10 13:40:00", False))
    row = env.db.get_alert_states()[LVIV]
    assert row["active"] == 0 and row["since"] is None


def test_inactive_changed_never_used(env):
    env.poll(active_lviv(env, "2026-11-10 10:47:27", False))
    assert env.db.get_alert_states()[LVIV]["since"] is None


def test_bad_or_future_changed_falls_back_to_updated_at(env):
    env.clock.t = utc(2026, 11, 10, 12)
    env.poll(active_lviv(env, "not a date"))
    row = env.db.get_alert_states()[LVIV]
    assert row["since"] == row["updated_at"] == int(env.clock())
    env.clock.advance(3600)
    env.poll(active_lviv(env, "2030-01-01 00:00:00"))
    assert env.db.get_alert_states()[LVIV]["since"] == utc(2026, 11, 10, 12)


def test_transition_logic_ignores_changed(env):
    seen = []
    env.poller.on_transition = seen.append
    env.poll(active_lviv(env, "2026-11-10 10:00:00", True))
    env.poll(active_lviv(env, "2026-11-10 11:00:00", True))
    assert seen == []


def test_since_backfilled_for_pre_migration_active_row(env):
    env.clock.t = utc(2026, 11, 10, 12)
    env.poll(active_lviv(env, "2026-11-10 10:00:00"))
    env.db._exec("UPDATE alert_states SET since=NULL WHERE oblast=?", (LVIV,))
    env.poll(active_lviv(env, "2026-11-10 10:00:00"))
    assert env.db.get_alert_states()[LVIV]["since"] == utc(2026, 11, 10, 8)


def test_migration_v2_adds_since(tmp_path):
    from meshalerts.db import Database, SCHEMA_VERSION
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE alert_states (oblast TEXT PRIMARY KEY, active INTEGER NOT NULL, updated_at INTEGER NOT NULL);"
        "INSERT INTO alert_states VALUES('X', 1, 100);"
        "PRAGMA user_version=2;"
    )
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.get_alert_states()["X"] == {"oblast": "X", "active": 1, "updated_at": 100, "since": None}
    assert db._one("PRAGMA user_version")["user_version"] == SCHEMA_VERSION
    db.close()
    Database(path).close()
