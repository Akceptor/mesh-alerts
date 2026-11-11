from conftest import LOCAL, NODE_A, NODE_ID, Env, all_quiet
from meshalerts.db import Database


def quiet_env():
    env = Env()
    env.poll(all_quiet())
    return env


def test_start_private_subscribes_and_replies():
    env = quiet_env()
    env.send_command("!start")
    node = env.db.get_node(NODE_ID)
    assert node["subscribed"] == 1
    assert node["registered_at"] == int(env.clock())
    assert len(env.mesh.sent) == 1
    assert env.mesh.sent[0]["dest"] == NODE_A
    assert env.mesh.texts()[0].startswith("Alerts enabled. Region: unknown (no GPS position)")


def test_start_command_case_and_spaces():
    env = quiet_env()
    env.send_command("  !START  ")
    assert env.db.get_node(NODE_ID)["subscribed"] == 1


def test_start_with_position_in_node_db_gives_region_and_status():
    env = quiet_env()
    env.mesh.nodes[NODE_A] = {
        "num": NODE_A, "user": {"id": NODE_ID, "longName": "T-Deck One"},
        "position": {"latitude": 49.5, "longitude": 24.5, "time": int(env.clock())},
        "lastHeard": int(env.clock()),
    }
    env.send_command("!start")
    node = env.db.get_node(NODE_ID)
    assert node["oblast"] == "Львівська область"
    assert node["node_name"] == "T-Deck One"
    assert env.mesh.texts() == ["Alerts enabled. Region: Львівська область. Status: No alert."]
    assert node["last_notified_state"] == 0


def test_start_outside_ukraine():
    env = quiet_env()
    env.config.gps.reject_outside_ukraine = False
    env.mesh.nodes[NODE_A] = {"position": {"latitude": 10.0, "longitude": 10.0}, "lastHeard": int(env.clock())}
    env.send_command("!start")
    assert env.mesh.texts() == ["Alerts enabled. Region: outside Ukraine."]


def test_start_with_spoofed_node_db_position_is_no_position():
    env = quiet_env()
    env.mesh.nodes[NODE_A] = {"position": {"latitude": -12.0464, "longitude": -77.0428}, "lastHeard": int(env.clock())}
    env.send_command("!start")
    assert env.mesh.texts()[0].startswith("Alerts enabled. Region: unknown (no GPS position)")
    assert env.db.get_node(NODE_ID)["rejected_positions"] == 1


def test_location_reports_rejected_fix():
    env = quiet_env()
    env.send_command("!start")
    env.position(NODE_ID, 49.5, 24.5)
    env.clock.advance(30)
    env.position(NODE_ID, -12.0464, -77.0428)
    env.send_command("!location")
    text = env.mesh.texts()[-1]
    assert "Position: 49.50000, 24.50000." in text
    assert text.endswith("Last GPS fix rejected (outside Ukraine).")
    env.clock.advance(30)
    env.position(NODE_ID, 49.5, 24.6)
    env.clock.advance(30)
    env.send_command("!location")
    assert "rejected" not in env.mesh.texts()[-1]


def test_start_before_any_api_data_says_no_data():
    env = Env()
    env.mesh.nodes[NODE_A] = {"position": {"latitude": 49.5, "longitude": 24.5}, "lastHeard": int(env.clock())}
    env.send_command("!start")
    assert env.mesh.texts() == ["Alerts enabled. Region: Львівська область. Status: no data."]


def test_start_broadcast_is_ignored():
    env = quiet_env()
    env.send_command("!start", to=0xFFFFFFFF)
    assert env.db.get_node(NODE_ID) is None
    assert env.mesh.sent == []


def test_start_to_other_node_is_ignored():
    env = quiet_env()
    env.send_command("!start", to=0x2222)
    assert env.mesh.sent == []


def test_stop_unsubscribes_and_stops_history():
    env = quiet_env()
    env.send_command("!start")
    env.position(NODE_ID, 49.5, 24.5)
    assert env.db.position_count(NODE_ID) == 1
    env.clock.advance(20)
    env.send_command("!stop")
    assert env.db.get_node(NODE_ID)["subscribed"] == 0
    assert env.mesh.texts()[-1] == "Alerts disabled. Send !start to enable."
    env.clock.advance(10)
    assert env.position(NODE_ID, 49.6, 24.6) is None
    assert env.db.position_count(NODE_ID) == 1


def test_persistence_after_reopen(tmp_path):
    path = tmp_path / "t.db"
    env = Env(db_path=str(path))
    env.send_command("!start")
    env.db.close()
    again = Database(path)
    assert again.get_node(NODE_ID)["subscribed"] == 1


def test_node_id_is_key_not_name():
    env = quiet_env()
    packet = env.text_packet("!start")
    packet["decoded"]["longName"] = "Other"
    env.commands.handle_text(packet)
    assert [n["node_id"] for n in env.db.list_nodes()] == [NODE_ID]


def test_from_id_missing_is_derived_from_num():
    env = quiet_env()
    packet = env.text_packet("!start")
    packet["fromId"] = None
    env.commands.handle_text(packet)
    env.drain()
    assert env.db.get_node(NODE_ID)["subscribed"] == 1


def test_malformed_packets_ignored():
    env = quiet_env()
    for packet in [
        {},
        {"from": NODE_A, "to": LOCAL},
        {"from": NODE_A, "to": LOCAL, "decoded": {}},
        {"from": NODE_A, "to": LOCAL, "decoded": {"text": 5}},
        {"from": "x", "to": LOCAL, "decoded": {"text": "!start"}},
        {"to": LOCAL, "decoded": {"text": "!start"}},
        {"from": NODE_A, "to": LOCAL, "decoded": {"text": "!start" + "x" * 300}},
        {"from": LOCAL, "to": LOCAL, "decoded": {"text": "!start"}},
    ]:
        env.commands.handle_text(packet)
    env.drain()
    assert env.mesh.sent == []
    assert env.db.list_nodes() == []


def test_unknown_text_gets_no_reply():
    env = quiet_env()
    env.send_command("hello there")
    env.send_command("")
    env.send_command("!")
    env.send_command("!!!")
    env.send_command("!a b c d")
    assert env.mesh.sent == []


def test_cooldown_for_identical_commands():
    env = quiet_env()
    env.send_command("!help")
    env.send_command("!help")
    assert len(env.mesh.sent) == 1
    env.clock.advance(11)
    env.send_command("!help")
    assert len(env.mesh.sent) == 2


def test_different_commands_not_blocked_by_cooldown():
    env = quiet_env()
    env.send_command("!help")
    env.send_command("!status")
    assert len(env.mesh.sent) == 2


def test_status_with_alert_and_location():
    env = quiet_env()
    data = all_quiet()
    data["states"]["Львівська область"]["alertnow"] = True
    env.send_command("!start")
    env.position(NODE_ID, 49.5, 24.5)
    env.poll(data)
    env.drain()
    env.clock.advance(20)
    env.send_command("!status")
    assert "Region: Львівська область. Status: ПОВІТРЯНА ТРИВОГА." in env.mesh.texts()[-1]
    env.clock.advance(20)
    env.send_command("!location")
    last = env.mesh.texts()[-1]
    assert last.startswith("Position: 49.50000, 24.50000. Time: ")
    assert "Region: Львівська область." in last


def test_status_stale_data_and_position_notices():
    env = quiet_env()
    env.send_command("!start")
    env.position(NODE_ID, 49.5, 24.5)
    env.clock.advance(7 * 3600)
    env.send_command("!status")
    text = env.mesh.texts()[-1]
    assert "Position is old (420 min)." in text
    assert "Data is old (420 min)." in text


def test_location_without_position():
    env = quiet_env()
    env.send_command("!location")
    assert env.mesh.texts() == ["No position known."]


def test_location_not_subscribed_uses_node_db_without_storing():
    env = quiet_env()
    env.mesh.nodes[NODE_A] = {"position": {"latitude": 49.5, "longitude": 24.5, "time": int(env.clock()) - 120}}
    env.send_command("!location")
    assert "Position: 49.50000, 24.50000." in env.mesh.texts()[0]
    assert "(2 min ago)" in env.mesh.texts()[0]
    node = env.db.get_node(NODE_ID)
    assert node["latitude"] is None
    assert node["subscribed"] == 0


def test_reply_texts_fit_limit():
    env = quiet_env()
    env.send_command("!help")
    assert all(len(t.encode()) <= 200 for t in env.mesh.texts())
