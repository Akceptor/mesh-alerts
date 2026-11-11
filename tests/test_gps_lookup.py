import json

import pytest

from conftest import FIXTURES, KYIV, LVIV, ROOT, TERNOPIL, Env, all_quiet
from meshalerts.gps import extract_latlon
from meshalerts.oblast import API_NAMES, ISO_TO_NAME, NAME_TO_ISO, OblastIndex


def subscribed_env():
    env = Env()
    env.poll(all_quiet())
    env.db.ensure_node("!a1b2c3d4")
    env.db.update_node("!a1b2c3d4", subscribed=1)
    return env


def test_inside_polygons(env):
    assert env.oblasts.resolve(49.5, 24.5).oblast == LVIV
    assert env.oblasts.resolve(49.5, 25.5).oblast == TERNOPIL
    assert env.oblasts.resolve(50.5, 24.5).oblast == KYIV
    res = env.oblasts.resolve(49.5, 24.5)
    assert res.status == "inside"


def test_border_hysteresis_does_not_flap(env):
    ix = env.oblasts
    assert ix.resolve(49.5, 25.004).oblast == TERNOPIL
    assert ix.resolve(49.5, 25.004, current=LVIV).oblast == LVIV
    assert ix.resolve(49.5, 25.004, current=LVIV).status == "near_border"
    assert ix.resolve(49.5, 24.996).oblast == LVIV
    assert ix.resolve(49.5, 24.996, current=TERNOPIL).oblast == TERNOPIL


def test_hysteresis_released_far_from_border(env):
    assert env.oblasts.resolve(49.5, 25.5, current=LVIV).oblast == TERNOPIL


def test_outside_nearby_and_outside_ukraine(env):
    near = env.oblasts.resolve(49.5, 23.99)
    assert near.oblast == LVIV
    assert near.status == "outside_nearby"
    far = env.oblasts.resolve(49.5, 22.0)
    assert far.oblast is None
    assert far.status == "outside_ukraine"


def test_candidates_near_border(env):
    res = env.oblasts.resolve(49.5, 24.998)
    assert set(res.candidates) == {LVIV, TERNOPIL}


@pytest.mark.parametrize(
    "position",
    [
        {}, {"latitude": 0, "longitude": 0}, {"latitudeI": 0, "longitudeI": 0},
        {"latitude": 91, "longitude": 10}, {"latitude": 10, "longitude": 181},
        {"latitude": 10}, {"latitude": float("nan"), "longitude": 10},
        {"latitude": "x", "longitude": 10}, {"latitude": None, "longitude": None},
    ],
)
def test_invalid_positions_rejected(position):
    assert extract_latlon(position) is None


def test_integer_coordinates_used_when_float_missing():
    assert extract_latlon({"latitudeI": 495000000, "longitudeI": 245000000}) == (49.5, 24.5)


def test_invalid_position_is_ignored_by_tracker():
    env = subscribed_env()
    assert env.gps.handle_position("!a1b2c3d4", {"latitude": 0, "longitude": 0}) is None
    assert env.db.get_node("!a1b2c3d4")["oblast"] is None


def test_position_updates_oblast_and_history():
    env = subscribed_env()
    env.position("!a1b2c3d4", 49.5, 24.5)
    node = env.db.get_node("!a1b2c3d4")
    assert node["oblast"] == LVIV
    assert env.db.position_count("!a1b2c3d4") == 1


def test_history_can_be_disabled():
    env = subscribed_env()
    env.config.gps.position_history = False
    env.position("!a1b2c3d4", 49.5, 24.5)
    assert env.db.position_count("!a1b2c3d4") == 0


def test_older_timestamp_ignored():
    env = subscribed_env()
    now = int(env.clock())
    env.position("!a1b2c3d4", 49.5, 24.5, ts=now)
    assert env.position("!a1b2c3d4", 49.5, 25.5, ts=now - 100) is None
    assert env.db.get_node("!a1b2c3d4")["oblast"] == LVIV


def test_future_or_zero_time_falls_back_to_rx_time_then_now():
    env = subscribed_env()
    now = int(env.clock())
    env.gps.handle_position("!a1b2c3d4", {"latitude": 49.5, "longitude": 24.5, "time": now + 10_000}, rx_time=now - 5)
    assert env.db.get_node("!a1b2c3d4")["position_at"] == now - 5
    env.gps.handle_position("!a1b2c3d4", {"latitude": 49.5, "longitude": 24.5, "time": 0})
    assert env.db.get_node("!a1b2c3d4")["position_at"] == now


def test_stale_flag():
    env = subscribed_env()
    env.position("!a1b2c3d4", 49.5, 24.5)
    node = env.db.get_node("!a1b2c3d4")
    assert not env.gps.is_stale(node)
    env.clock.advance(env.config.gps.stale_after_s + 1)
    assert env.gps.is_stale(node)
    assert env.db.get_node("!a1b2c3d4")["oblast"] == LVIV


def test_drop_oblast_after_long_silence():
    env = subscribed_env()
    env.config.gps.drop_oblast_after_s = 3600
    env.position("!a1b2c3d4", 49.5, 24.5)
    env.clock.advance(7200)
    env.gps.maintenance()
    assert env.db.get_node("!a1b2c3d4")["oblast"] is None


def test_unsubscribed_node_position_not_stored():
    env = Env()
    env.db.ensure_node("!a1b2c3d4")
    assert env.position("!a1b2c3d4", 49.5, 24.5) is None
    assert env.db.get_node("!a1b2c3d4")["latitude"] is None


def test_seed_from_node_db():
    env = subscribed_env()
    env.mesh.nodes[0xA1B2C3D4] = {
        "user": {"longName": "Deck"}, "lastHeard": int(env.clock()) - 10,
        "position": {"latitudeI": 495000000, "longitudeI": 245000000},
    }
    env.gps.seed_from_node_db(env.mesh.nodes)
    node = env.db.get_node("!a1b2c3d4")
    assert node["oblast"] == LVIV
    assert node["node_name"] == "Deck"


def test_iso_map_covers_all_api_names():
    fixture = json.loads((FIXTURES / "ubilling_sample.json").read_text(encoding="utf-8"))
    assert len(API_NAMES) == 26
    assert len(ISO_TO_NAME) == 27
    for name in fixture["states"]:
        assert name in NAME_TO_ISO, name
    assert set(fixture["states"]) == API_NAMES


def test_missing_geojson_fails_fast(tmp_path):
    from meshalerts.oblast import OblastDataError

    with pytest.raises(OblastDataError):
        OblastIndex(tmp_path / "none.geojson")


REAL = next((ROOT / "data").glob("geoBoundaries-UKR-ADM1*.geojson"), None)


@pytest.mark.skipif(REAL is None, reason="real GeoJSON not present")
def test_real_geojson():
    ix = OblastIndex(REAL)
    assert len(ix.names) == 27
    assert ix.resolve(50.45, 30.52).oblast == "м. Київ"
    assert ix.resolve(49.84, 24.03).oblast == "Львівська область"
    assert ix.resolve(50.51, 30.79).oblast == "Київська область"
    assert ix.resolve(45.5, 31.5).oblast is None
    assert ix.resolve(44.6, 33.5).oblast == "Севастополь"


def test_spoofed_fix_in_lima_is_rejected(caplog):
    env = subscribed_env()
    data = all_quiet()
    env.position("!a1b2c3d4", 49.5, 24.5)
    before = env.db.get_node("!a1b2c3d4")
    changes = []
    env.gps.on_region_change = lambda n, o, new: changes.append((o, new))
    env.clock.advance(60)
    with caplog.at_level("WARNING"):
        assert env.position("!a1b2c3d4", -12.0464, -77.0428) is None
        env.clock.advance(60)
        env.position("!a1b2c3d4", -12.0464, -77.0428)
    after = env.db.get_node("!a1b2c3d4")
    for key in ("latitude", "longitude", "position_at", "oblast"):
        assert after[key] == before[key]
    assert env.db.position_count("!a1b2c3d4") == 1
    assert changes == []
    assert after["rejected_positions"] == 2
    assert after["last_rejected_at"] == int(env.clock())
    assert env.gps.rejected["!a1b2c3d4"] == 2
    warnings = [r for r in caplog.records if "possible spoofing" in r.getMessage()]
    assert len(warnings) == 1
    assert env.mesh.sent == []


def test_spoof_filter_can_be_disabled():
    env = subscribed_env()
    env.config.gps.reject_outside_ukraine = False
    res = env.position("!a1b2c3d4", -12.0464, -77.0428)
    assert res.status == "outside_ukraine"
    assert env.db.get_node("!a1b2c3d4")["latitude"] == -12.0464


def test_old_database_gets_new_columns(tmp_path):
    import sqlite3
    from meshalerts.db import Database

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE nodes (node_id TEXT PRIMARY KEY, node_num INTEGER NOT NULL, node_name TEXT,"
                 " subscribed INTEGER NOT NULL DEFAULT 0, registered_at INTEGER, last_seen INTEGER, latitude REAL,"
                 " longitude REAL, position_at INTEGER, oblast TEXT, last_notified_oblast TEXT, last_notified_state INTEGER)")
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    conn.close()
    db = Database(path)
    db.ensure_node("!a1b2c3d4")
    db.record_rejection("!a1b2c3d4", 5)
    assert db.get_node("!a1b2c3d4")["rejected_positions"] == 1
