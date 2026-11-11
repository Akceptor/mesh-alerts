import sqlite3

import pytest

from conftest import KYIV, LVIV, NODE_A, NODE_ID, TERNOPIL, Env, all_quiet, enroll, lviv
from meshalerts import messages, regions
from meshalerts.commands import COMMANDS
from meshalerts.dashboard import create_app
from meshalerts.config import DashboardConfig
from meshalerts.db import SCHEMA_VERSION, Database
from meshalerts.oblast import API_NAMES, NO_API_DATA

from test_dashboard import H, Svc

CRIMEA = next(iter(NO_API_DATA))
KYIV_OBLAST = "Київська область"
SEVASTOPOL = "Севастополь"
LVIV_POS = (49.5, 24.5)
TERNOPIL_POS = (49.5, 25.5)
KYIV_POS = (50.5, 24.5)


def active(*names):
    data = all_quiet()
    for name in names:
        data["states"][name]["alertnow"] = True
    return data


def texts(env):
    return env.mesh.texts()


# alias table

def test_every_api_region_has_aliases():
    assert set(regions.REGION_ALIASES) == API_NAMES
    assert len(API_NAMES) == 26
    for name in API_NAMES:
        assert regions.lookup(regions.CANONICAL[name]) == name


@pytest.mark.parametrize("text,expected", [
    ("lviv", LVIV), ("Lvivska", LVIV), ("lvivskaoblast", LVIV), ("Lviv Oblast", LVIV),
    ("lviv_obl", LVIV), ("львів", LVIV), ("Львівська", LVIV), ("львівська обл.", LVIV),
    ("львівська область", LVIV), ("lvov", LVIV),
    ("kharkiv", "Харківська область"), ("kharkov", "Харківська область"),
    ("odesa", "Одеська область"), ("odessa", "Одеська область"),
    ("zaporizhzhia", "Запорізька область"), ("zaporizhia", "Запорізька область"),
    ("zaporozhye", "Запорізька область"),
    ("dnipro", "Дніпропетровська область"), ("dnipropetrovsk", "Дніпропетровська область"),
    ("ivanofrankivsk", "Івано-Франківська область"), ("Ivano-Frankivsk", "Івано-Франківська область"),
    ("frankivsk", "Івано-Франківська область"), ("івано-франківська", "Івано-Франківська область"),
    ("kropyvnytskyi", "Кіровоградська область"), ("kirovohrad", "Кіровоградська область"),
    ("khmelnytskyi", "Хмельницька область"), ("Khmel'nyts'kyi", "Хмельницька область"),
    ("uzhhorod", "Закарпатська область"), ("zakarpattia", "Закарпатська область"),
    ("lutsk", "Волинська область"), ("volyn", "Волинська область"),
    ("rivne", "Рівненська область"), ("ternopil", TERNOPIL), ("chernivtsi", "Чернівецька область"),
    ("chernihiv", "Чернігівська область"), ("cherkasy", "Черкаська область"),
    ("vinnytsia", "Вінницька область"), ("zhytomyr", "Житомирська область"),
    ("poltava", "Полтавська область"), ("sumy", "Сумська область"),
    ("mykolaiv", "Миколаївська область"), ("kherson", "Херсонська область"),
    ("donetsk", "Донецька область"), ("luhansk", "Луганська область"),
    ("sevastopol", SEVASTOPOL), ("севастополь", SEVASTOPOL),
    ("L'viv", LVIV), ("Lʼviv", LVIV), ("L’viv", LVIV),
])
def test_alias_lookup(text, expected):
    assert regions.lookup(text) == expected


def test_kyiv_city_and_oblast_are_different():
    for text in ("kyiv", "kiev", "Київ", "м. Київ", "KYIV"):
        assert regions.lookup(text) == KYIV
    for text in ("kyivska", "kyivobl", "kyivoblast", "Kyiv Oblast", "київська", "Київська область",
                 "kyivskaoblast"):
        assert regions.lookup(text) == KYIV_OBLAST


def test_crimea_aliases():
    for text in ("crimea", "Krym", "крим"):
        assert regions.lookup(text) in NO_API_DATA


def test_unknown_alias():
    for text in ("", "obl", "oblast", "atlantis", "lviv2"):
        assert regions.lookup(text) is None


def test_aliases_do_not_collide_with_commands():
    names = {c[1:] for c in COMMANDS} | {"start", "stop", "status", "location", "help",
                                          "gps", "auto", "region", "regions"}
    assert not names & set(regions.ALIAS_TO_NAME)
    for name in names:
        assert regions.lookup(name) is None


# commands

def test_region_command_subscribes_and_fixes(env):
    env.poll(all_quiet())
    env.send_command("!lviv")
    assert texts(env) == [f"Alerts enabled. Region: {LVIV} (fixed). Status: No alert."]
    node = env.db.get_node(NODE_ID)
    assert node["subscribed"] == 1
    assert (node["region_mode"], node["fixed_oblast"], node["oblast"]) == ("fixed", LVIV, LVIV)


def test_region_status_active_and_no_data(env):
    env.poll(lviv(True))
    env.send_command("!lviv")
    assert texts(env)[-1].endswith("Status: Active.")
    env.clock.advance(11)
    env.send_command("!sumy", sender=0xB0B0B0B0)
    assert texts(env)[-1].endswith("Status: No alert.")

    fresh = Env()
    fresh.send_command("!lviv")
    assert texts(fresh)[-1] == f"Alerts enabled. Region: {LVIV} (fixed). Status: no data."


def test_region_command_forms_are_equal(env):
    env.poll(all_quiet())
    env.send_command("!region ivano frankivsk")
    assert "Івано-Франківська область (fixed)" in texts(env)[-1]
    env.clock.advance(11)
    env.send_command("!REGION Львів")
    assert LVIV in texts(env)[-1]
    env.clock.advance(11)
    env.send_command("!kyiv oblast")
    assert KYIV_OBLAST in texts(env)[-1]


def test_kyiv_city_command(env):
    env.poll(all_quiet())
    env.send_command("!kyiv")
    assert env.db.get_node(NODE_ID)["fixed_oblast"] == KYIV
    env.clock.advance(11)
    env.send_command("!kyivska")
    assert env.db.get_node(NODE_ID)["fixed_oblast"] == KYIV_OBLAST


def test_crimea_reply_keeps_region(env):
    enroll(env)
    env.send_command("!crimea")
    assert texts(env) == [messages.CRIMEA_NO_DATA]
    node = env.db.get_node(NODE_ID)
    assert node["region_mode"] == "gps" and node["oblast"] == LVIV
    env.clock.advance(11)
    env.send_command("!region krym")
    assert texts(env)[-1] == messages.CRIMEA_NO_DATA


def test_unknown_alias_reply_and_no_change(env):
    enroll(env)
    env.send_command("!atlantis")
    env.clock.advance(11)
    env.send_command("!region")
    assert texts(env) == ["Unknown region. Send !regions for the list."] * 2
    assert env.db.get_node(NODE_ID)["region_mode"] == "gps"


def test_alias_cooldown_is_shared(env):
    env.poll(all_quiet())
    env.send_command("!lviv")
    env.send_command("!sumy")
    assert len(texts(env)) == 1


def test_fixed_mode_ignores_gps_but_stores_position(env):
    enroll(env)
    env.send_command("!ternopil")
    env.mesh.sent.clear()
    env.poll(active(KYIV))
    env.position(NODE_ID, *KYIV_POS, ts=env.clock() + 1)
    node = env.db.get_node(NODE_ID)
    assert node["oblast"] == TERNOPIL and node["gps_oblast"] == KYIV
    assert (node["latitude"], node["longitude"]) == KYIV_POS
    assert env.db.position_count(NODE_ID) >= 1
    env.drain()
    assert texts(env) == []


def test_fixed_mode_still_rejects_spoofed_position(env):
    enroll(env)
    env.send_command("!ternopil")
    assert env.position(NODE_ID, -12.0464, -77.0428) is None
    node = env.db.get_node(NODE_ID)
    assert node["rejected_positions"] == 1 and node["latitude"] == LVIV_POS[0]


def test_no_region_change_message_for_command(env):
    enroll(env)
    env.poll(active(TERNOPIL))
    env.drain()
    env.mesh.sent.clear()
    env.send_command("!ternopil")
    assert len(texts(env)) == 1 and texts(env)[0].endswith("Status: Active.")
    assert not any("Ви в зоні" in t for t in texts(env))


def test_gps_restores_region_from_stored_position(env):
    enroll(env)
    env.send_command("!ternopil")
    env.position(NODE_ID, *KYIV_POS, ts=env.clock() + 1)
    env.clock.advance(11)
    env.send_command("!gps")
    assert texts(env)[-1] == f"Region from GPS: {KYIV}. Status: No alert."
    node = env.db.get_node(NODE_ID)
    assert (node["region_mode"], node["fixed_oblast"], node["oblast"]) == ("gps", None, KYIV)


def test_auto_alias_and_active_status(env):
    enroll(env)
    env.send_command("!ternopil")
    env.poll(lviv(True))
    env.drain()
    env.mesh.sent.clear()
    env.clock.advance(11)
    env.send_command("!auto")
    assert texts(env) == [f"Region from GPS: {LVIV}. Status: Active."]


def test_gps_without_position(env):
    env.poll(all_quiet())
    env.send_command("!lviv")
    env.clock.advance(11)
    env.send_command("!gps")
    assert texts(env)[-1] == "Region from GPS: unknown (no GPS position)."
    node = env.db.get_node(NODE_ID)
    assert node["region_mode"] == "gps" and node["oblast"] is None


def test_gps_after_fix_from_other_region_does_not_notify(env):
    enroll(env)
    env.poll(active(TERNOPIL))
    env.send_command("!ternopil")
    env.clock.advance(11)
    env.send_command("!gps")
    env.drain()
    assert texts(env)[-1] == f"Region from GPS: {LVIV}. Status: No alert."
    assert len(texts(env)) == 2


def test_dedup_after_fixing_no_duplicate_alert(env):
    enroll(env)
    env.poll(active(TERNOPIL))
    env.drain()
    env.mesh.sent.clear()
    env.send_command("!ternopil")
    assert len(texts(env)) == 1
    node = env.db.get_node(NODE_ID)
    assert (node["last_notified_oblast"], node["last_notified_state"]) == (TERNOPIL, 1)
    env.poll(active(TERNOPIL))
    env.restart()
    env.poll(active(TERNOPIL))
    env.drain()
    assert texts(env) == []


def test_dedup_state_none_when_no_data(env):
    env.send_command("!lviv")
    node = env.db.get_node(NODE_ID)
    assert node["last_notified_state"] is None


def test_alerts_follow_fixed_region(env):
    enroll(env)
    env.send_command("!ternopil")
    env.mesh.sent.clear()
    env.poll(active(LVIV))
    env.drain()
    assert texts(env) == []
    env.poll(active(LVIV, TERNOPIL))
    env.drain()
    assert texts(env) == [f"ПОВІТРЯНА ТРИВОГА! {TERNOPIL}"]
    env.poll(active(LVIV))
    env.drain()
    assert texts(env)[-1] == f"ВІДБІЙ ТРИВОГИ. {TERNOPIL}"


def test_any_nearby_applies_only_in_gps_mode():
    env = Env(policy="any_nearby")
    enroll(env, lat=49.5, lon=24.998)
    env.poll(active(TERNOPIL))
    env.drain()
    assert texts(env) == [f"ПОВІТРЯНА ТРИВОГА! {TERNOPIL}"]
    env.send_command("!lviv")
    env.poll(all_quiet())
    env.drain()
    env.mesh.sent.clear()
    env.poll(active(TERNOPIL))
    env.drain()
    assert texts(env) == []


def test_stop_and_start_keep_fixed_mode(env):
    enroll(env)
    env.send_command("!ternopil")
    env.clock.advance(11)
    env.send_command("!stop")
    node = env.db.get_node(NODE_ID)
    assert node["subscribed"] == 0 and node["region_mode"] == "fixed" and node["fixed_oblast"] == TERNOPIL
    env.clock.advance(11)
    env.send_command("!start")
    assert texts(env)[-1] == f"Alerts enabled. Region: {TERNOPIL} (fixed). Status: No alert."
    assert env.db.get_node(NODE_ID)["oblast"] == TERNOPIL


def test_start_does_not_reset_fixed_region(env):
    enroll(env)
    env.send_command("!ternopil")
    env.clock.advance(11)
    env.send_command("!start")
    assert env.db.get_node(NODE_ID)["region_mode"] == "fixed"


def test_status_and_location_show_fixed(env):
    enroll(env)
    env.send_command("!ternopil")
    env.clock.advance(11)
    env.send_command("!status")
    assert texts(env)[-1] == f"Region: {TERNOPIL} (fixed). Status: no alert."
    env.clock.advance(11)
    env.send_command("!location")
    assert "49.50000, 24.50000" in texts(env)[-1]
    assert texts(env)[-1].endswith(f"Region: {TERNOPIL} (fixed).")


def test_status_fixed_ignores_stale_position(env):
    enroll(env)
    env.send_command("!ternopil")
    env.clock.advance(7 * 3600)
    env.poll(all_quiet())
    env.send_command("!status")
    assert "Position is old" not in texts(env)[-1]


def test_location_fixed_without_position(env):
    env.poll(all_quiet())
    env.send_command("!lviv")
    env.clock.advance(11)
    env.send_command("!location")
    assert texts(env)[-1] == f"No position known. Region: {LVIV} (fixed)."


def test_fixed_region_survives_drop_oblast_maintenance(env):
    env.config.gps.drop_oblast_after_s = 3600
    enroll(env)
    env.send_command("!ternopil")
    env.clock.advance(7200)
    env.gps.maintenance()
    node = env.db.get_node(NODE_ID)
    assert node["oblast"] == TERNOPIL and node["gps_oblast"] is None


def test_regions_command_lists_all_within_limit(env):
    env.send_command("!regions")
    sent = texts(env)
    assert 1 <= len(sent) <= 3
    joined = " ".join(sent)
    for short in regions.CANONICAL.values():
        assert short in joined.split()
    assert "kyivska" in joined and "Kyiv city" in joined
    assert all(len(t.encode()) <= 200 for t in sent)
    assert env.db.get_node(NODE_ID)["subscribed"] == 0


def test_help_text(env):
    env.send_command("!help")
    assert texts(env) == ["Commands: !start !stop !status !location !<region> (e.g. !lviv) !gps !regions"]
    assert len(messages.HELP.encode()) <= 200


def test_all_new_messages_fit_in_200_bytes():
    longest = max(regions.REGION_ALIASES, key=lambda n: len(n.encode()))
    for active_state in (True, False, None):
        assert len(messages.alerts_enabled(longest, active_state, True, True).encode()) <= 200
        assert len(messages.gps_region_reply(longest, active_state).encode()) <= 200
        assert len(messages.status_reply(longest, active_state, True, 999, 999, True, True, True).encode()) <= 200
    for text in (messages.UNKNOWN_REGION, messages.CRIMEA_NO_DATA, messages.REGIONS_NOTE):
        assert len(text.encode()) <= 200
    assert all(len(t.encode()) <= 200 for t in messages.regions_lists(list(regions.CANONICAL.values())))


def test_long_fixed_location_reply_fits(env):
    env.poll(all_quiet())
    env.send_command("!frankivsk")
    env.position(NODE_ID, -12.0464, -77.0428)
    env.db.update_node(NODE_ID, latitude=49.5, longitude=24.5, position_at=int(env.clock()) - 100000)
    env.clock.advance(11)
    env.send_command("!location")
    assert len(texts(env)[-1].encode("utf-8")) <= 200
    assert "Last GPS fix rejected" in texts(env)[-1]


# database

def test_migration_v4_to_v5(tmp_path):
    path = tmp_path / "v4.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        "CREATE TABLE nodes (node_id TEXT PRIMARY KEY, node_num INTEGER NOT NULL, node_name TEXT,"
        " subscribed INTEGER NOT NULL DEFAULT 0, registered_at INTEGER, last_seen INTEGER, latitude REAL,"
        " longitude REAL, position_at INTEGER, oblast TEXT, last_notified_oblast TEXT,"
        " last_notified_state INTEGER, rejected_positions INTEGER NOT NULL DEFAULT 0, last_rejected_at INTEGER);"
        f"INSERT INTO nodes(node_id, node_num, subscribed, oblast) VALUES('{NODE_ID}', {NODE_A}, 1, '{LVIV}');"
        "PRAGMA user_version=4;"
    )
    raw.commit()
    raw.close()
    db = Database(path)
    assert SCHEMA_VERSION == 5
    assert db._one("PRAGMA user_version")["user_version"] == 5
    node = db.get_node(NODE_ID)
    assert node["region_mode"] == "gps" and node["fixed_oblast"] is None
    assert node["gps_oblast"] == LVIV and node["oblast"] == LVIV
    db.close()
    Database(path).close()


# dashboard

def post(env, node_id=NODE_ID, headers=H, **body):
    c = create_app(Svc(env), DashboardConfig()).test_client()
    return c.post(f"/api/nodes/{node_id}/region", json=body, headers=headers)


def test_dashboard_sets_fixed_region_and_notifies(env):
    enroll(env)
    r = post(env, region=TERNOPIL, notify=True)
    assert r.status_code == 200
    j = r.get_json()
    assert j["region_mode"] == "fixed" and j["oblast"] == TERNOPIL and j["notification_id"]
    assert j["reply"] == f"Alerts enabled. Region: {TERNOPIL} (fixed). Status: No alert."
    env.drain()
    assert texts(env) == [j["reply"]]
    state = create_app(Svc(env), DashboardConfig()).test_client().get("/api/state").get_json()
    node = [n for n in state["nodes"] if n["node_id"] == NODE_ID][0]
    assert (node["region_mode"], node["fixed_oblast"], node["gps_oblast"]) == ("fixed", TERNOPIL, LVIV)


def test_dashboard_without_notify_sends_nothing(env):
    enroll(env)
    j = post(env, region=TERNOPIL).get_json()
    assert j["notification_id"] is None
    env.drain()
    assert texts(env) == []


def test_dashboard_null_returns_to_gps(env):
    enroll(env)
    post(env, region=TERNOPIL)
    j = post(env, region=None, notify=True).get_json()
    assert j["region_mode"] == "gps" and j["oblast"] == LVIV
    assert j["reply"] == f"Region from GPS: {LVIV}. Status: No alert."


def test_dashboard_region_validation(env):
    enroll(env)
    for body in ({"region": "lviv"}, {"region": CRIMEA}, {"region": 5}, {"region": ""},
                 {"region": TERNOPIL, "notify": "yes"}):
        assert post(env, **body).status_code == 400, body
    assert post(env, node_id="bad", region=TERNOPIL).status_code == 400
    assert env.db.get_node(NODE_ID)["region_mode"] == "gps"


def test_dashboard_region_null_unknown_node(env):
    assert post(env, node_id="!deadbeef", region=None).status_code == 404


def test_dashboard_region_requires_csrf_header(env):
    enroll(env)
    r = post(env, headers={}, region=TERNOPIL)
    assert r.status_code == 403
    assert env.db.get_node(NODE_ID)["region_mode"] == "gps"


def test_dashboard_region_requires_auth(env):
    enroll(env)
    c = create_app(Svc(env), DashboardConfig(password="pw")).test_client()
    assert c.post(f"/api/nodes/{NODE_ID}/region", json={"region": TERNOPIL}, headers=H).status_code == 401
