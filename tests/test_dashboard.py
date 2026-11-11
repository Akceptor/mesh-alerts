import base64

import pytest

from conftest import LVIV, NODE_ID, Env, all_quiet, enroll, lviv
from meshalerts.alerts import parse_since
from meshalerts.config import DashboardConfig
from meshalerts.dashboard import DashboardServer, create_app, is_loopback

H = {"X-Requested-With": "meshalerts"}


class Svc:
    def __init__(self, env):
        self.db, self.mesh, self.alerts = env.db, env.mesh, env.poller
        self.notifier, self.commands, self.gps, self.clock = env.notifier, env.commands, env.gps, env.clock


def make(env, password=""):
    app = create_app(Svc(env), DashboardConfig(password=password))
    return app.test_client()


def auth(password, user="admin"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def test_index_and_state(env):
    enroll(env)
    c = make(env)
    assert c.get("/").status_code == 200
    state = c.get("/api/state").get_json()
    assert state["connection"]["connected"] is True
    assert state["connection"]["local_node_id"] == "!00001000"
    assert len(state["alerts"]) == 26
    node = [n for n in state["nodes"] if n["node_id"] == NODE_ID][0]
    assert node["subscribed"] and node["oblast"] == LVIV
    assert node["rejected_positions"] == 0


def test_state_includes_mesh_only_nodes(env):
    env.mesh.nodes[0x1234ABCD] = {"num": 0x1234ABCD, "user": {"id": "!1234abcd", "longName": "Other"}}
    state = make(env).get("/api/state").get_json()
    node = [n for n in state["nodes"] if n["node_id"] == "!1234abcd"][0]
    assert node["subscribed"] is False and node["name"] == "Other"


def test_state_shows_rejected_fixes(env):
    enroll(env)
    env.position(NODE_ID, -12.0464, -77.0428)
    node = make(env).get("/api/state").get_json()["nodes"][0]
    assert node["rejected_positions"] == 1


def test_state_summary_fields(env):
    enroll(env)
    env.position(NODE_ID, -12.0464, -77.0428)
    c = make(env)
    state = c.get("/api/state").get_json()
    assert state["connection"]["host"] is None and state["connection"]["port"] is None
    node = [n for n in state["nodes"] if n["node_id"] == NODE_ID][0]
    assert node["last_fix_rejected"] is True
    assert all("since" in a for a in state["alerts"])
    assert all(a["since"] is None for a in state["alerts"] if not a["active"])
    base = state["messages_today"]["total"]

    now = int(env.clock())
    ids = [env.db.insert_notification(NODE_ID, "manual", "x", now) for _ in range(6)]
    env.db._exec("UPDATE notifications SET acknowledgement_status='superseded' WHERE id<?", (ids[0],))
    for i, status in zip(ids, ["delivered", "implicit_ack", "no_ack", "failed:MAX_RETRANSMIT", "failed:NO_ROUTE"]):
        env.db.set_status(i, status)
    env.db.insert_notification(NODE_ID, "manual", "old", now - 3 * 86400, status="delivered")
    assert c.get("/api/state").get_json()["messages_today"] == {
        "total": base + 6, "delivered": 2, "no_ack": 1, "failed": 2,
    }


def test_state_alert_since(env):
    enroll(env)
    env.poll(lviv(True))
    alerts = {a["oblast"]: a for a in make(env).get("/api/state").get_json()["alerts"]}
    assert alerts[LVIV]["active"] is True and alerts[LVIV]["since"] == parse_since(lviv(True)["states"][LVIV]["changed"], int(env.clock()))


def test_state_alert_order_ukrainian(env):
    env.poll(all_quiet())
    names = [a["oblast"] for a in make(env).get("/api/state").get_json()["alerts"]]
    assert names[0] == "Вінницька область"
    assert names.index("Івано-Франківська область") < names.index("Київська область")
    assert names[-2:] == ["м. Київ", "Севастополь"]


def test_oblast_sort_key():
    from meshalerts.dashboard import oblast_sort_key
    got = sorted(
        ["Харківська область", "Київська область", "Івано-Франківська область", "Чернівецька область",
         "Одеська область", "м. Київ", "Севастополь", "Закарпатська область", "Житомирська область",
         "Вінницька область", "Єдина область"],
        key=oblast_sort_key,
    )
    assert got == [
        "Вінницька область", "Єдина область", "Житомирська область", "Закарпатська область",
        "Івано-Франківська область", "Київська область", "Одеська область", "Харківська область",
        "Чернівецька область", "м. Київ", "Севастополь",
    ]


def test_auth_required_when_password_set(env):
    c = make(env, "secret")
    assert c.get("/api/state").status_code == 401
    assert c.get("/api/state", headers=auth("wrong")).status_code == 401
    assert c.get("/api/state", headers=auth("secret", "bob")).status_code == 401
    assert c.get("/api/state", headers=auth("secret")).status_code == 200
    assert c.post("/api/send", json={}, headers=H).status_code == 401


def test_csrf_header_required(env):
    enroll(env)
    c = make(env)
    r = c.post("/api/send", json={"node_id": NODE_ID, "text": "hi"})
    assert r.status_code == 403
    r = c.post("/api/send", json={"node_id": NODE_ID, "text": "hi"}, headers={"X-Requested-With": "x"})
    assert r.status_code == 403
    assert env.notifier.queue_length() == 0


def test_send_queues_direct_message(env):
    enroll(env)
    c = make(env)
    r = c.post("/api/send", json={"node_id": NODE_ID, "text": " Привіт "}, headers=H)
    assert r.status_code == 200
    env.drain()
    assert env.mesh.texts() == ["Привіт"]
    notes = c.get("/api/notifications?limit=5").get_json()["notifications"]
    assert notes[0]["kind"] == "manual" and notes[0]["id"] == r.get_json()["notification_id"]


def test_send_limits(env):
    enroll(env)
    c = make(env)
    ok = c.post("/api/send", json={"node_id": NODE_ID, "text": "a" * 200}, headers=H)
    assert ok.status_code == 200
    assert c.post("/api/send", json={"node_id": NODE_ID, "text": "a" * 201}, headers=H).status_code == 400
    assert c.post("/api/send", json={"node_id": NODE_ID, "text": "ї" * 101}, headers=H).status_code == 400
    assert c.post("/api/send", json={"node_id": NODE_ID, "text": "   "}, headers=H).status_code == 400
    assert c.post("/api/send", json={"node_id": NODE_ID}, headers=H).status_code == 400
    assert c.post("/api/send", json={"node_id": "^all", "text": "x"}, headers=H).status_code == 400
    assert c.post("/api/send", json={"text": "x"}, headers=H).status_code == 400


def test_unsubscribe_and_subscribe(env):
    enroll(env)
    c = make(env)
    r = c.post(f"/api/nodes/{NODE_ID}/unsubscribe", json={"notify": False}, headers=H)
    assert r.status_code == 200
    assert env.db.get_node(NODE_ID)["subscribed"] == 0
    assert env.notifier.queue_length() == 0
    r = c.post(f"/api/nodes/{NODE_ID}/subscribe", json={"notify": True}, headers=H)
    assert r.status_code == 200
    assert env.db.get_node(NODE_ID)["subscribed"] == 1
    env.drain()
    assert env.mesh.texts()[0].startswith("Alerts enabled.")


def test_subscribe_validation(env):
    c = make(env)
    assert c.post("/api/nodes/bad/subscribe", json={}, headers=H).status_code == 400
    assert c.post("/api/nodes/!ABCDEF12/subscribe", json={}, headers=H).status_code == 400
    assert c.post("/api/nodes/!a1b2c3d4/unsubscribe", json={}, headers=H).status_code == 404


def test_add_node_validation(env):
    c = make(env)
    for bad in ["", "a1b2c3d4", "!A1B2C3D4", "!a1b2c3", "!a1b2c3d4e", "!zzzzzzzz", None, 5]:
        assert c.post("/api/nodes", json={"node_id": bad}, headers=H).status_code == 400, bad
    assert c.post("/api/nodes", json={"node_id": "!a1b2c3d4"}, headers=H).status_code == 200
    node = env.db.get_node("!a1b2c3d4")
    assert node["subscribed"] == 1 and node["node_num"] == 0xA1B2C3D4


def test_test_alert_does_not_change_dedup(env):
    enroll(env)
    before = env.db.get_node(NODE_ID)
    r = make(env).post("/api/test-alert", json={"node_id": NODE_ID}, headers=H)
    assert r.status_code == 200
    env.drain()
    assert env.mesh.texts() == [f"ТЕСТ. Перевірка сповіщень. {LVIV}"]
    after = env.db.get_node(NODE_ID)
    assert after["last_notified_state"] == before["last_notified_state"]
    assert after["last_notified_oblast"] == before["last_notified_oblast"]


def test_notifications_limit_validation(env):
    assert make(env).get("/api/notifications?limit=abc").status_code == 400


def test_non_loopback_without_password_refuses_to_start(env):
    server = DashboardServer(Svc(env), DashboardConfig(host="0.0.0.0", port=0, password=""))
    assert server.start() is False


def test_loopback_detection():
    assert is_loopback("127.0.0.1") and is_loopback("::1") and is_loopback("localhost")
    assert not is_loopback("0.0.0.0") and not is_loopback("192.168.1.2")


def test_server_starts_and_stops(env):
    server = DashboardServer(Svc(env), DashboardConfig(port=0))
    assert server.start() is True
    server.stop()


# radio connection control

PORT_ROW = {"path": "/dev/cu.wchusbserial2110", "description": "USB Serial", "likely": True}


def test_ports_endpoint(env):
    env.mesh.ports = [PORT_ROW]
    assert make(env).get("/api/ports").get_json() == {"ports": [PORT_ROW]}


def test_ports_and_connection_need_auth(env):
    c = make(env, password="pw")
    assert c.get("/api/ports").status_code == 401
    assert c.post("/api/connection/connect", headers=H, json={"port": None}).status_code == 401
    assert c.post("/api/connection/disconnect", headers=H).status_code == 401
    ok = {**auth("pw"), **H}
    assert c.get("/api/ports", headers=auth("pw")).status_code == 200
    assert c.post("/api/connection/disconnect", headers=ok).status_code == 200
    assert env.mesh.calls == [("disconnect",)]


def test_connection_posts_require_csrf_header(env):
    c = make(env)
    assert c.post("/api/connection/connect", json={"port": None}).status_code == 403
    assert c.post("/api/connection/disconnect").status_code == 403
    assert env.mesh.calls == []


def test_connect_known_port(env):
    env.mesh.ports = [PORT_ROW]
    r = make(env).post("/api/connection/connect", headers=H,
                       json={"port": PORT_ROW["path"], "allow_any_node": True})
    assert r.status_code == 200
    assert env.mesh.calls == [("connect", PORT_ROW["path"], True)]


def test_connect_null_means_auto(env):
    r = make(env).post("/api/connection/connect", headers=H, json={"port": None})
    assert r.status_code == 200
    assert env.mesh.calls == [("connect", None, False)]


def test_connect_rejects_unknown_port_and_bad_types(env):
    env.mesh.ports = [PORT_ROW]
    c = make(env)
    for payload in ({"port": "/dev/cu.nope"}, {"port": 5}, {"port": None, "allow_any_node": "yes"}):
        assert c.post("/api/connection/connect", headers=H, json=payload).status_code == 400, payload
    assert env.mesh.calls == []


def test_disconnect_endpoint(env):
    assert make(env).post("/api/connection/disconnect", headers=H).status_code == 200
    assert env.mesh.calls == [("disconnect",)]


def test_state_connection_fields(env):
    env.mesh.link = {
        "mode": "manual", "chosen_port": "/dev/cu.wchusbserial2110",
        "active_port": "/dev/cu.wchusbserial2110", "connecting": False, "next_retry_at": 1800000030.0,
        "node_id": "!00001000", "hw_model": "HELTEC_V3", "firmware": "2.7.0", "transport": "serial",
        "last_error": "boom", "disconnected_by_user": False,
    }
    conn = make(env).get("/api/state").get_json()["connection"]
    assert conn["mode"] == "manual" and conn["chosen_port"] == "/dev/cu.wchusbserial2110"
    assert conn["connecting"] is False and conn["next_retry_at"] == 1800000030.0
    assert (conn["node_id"], conn["hw_model"], conn["firmware"]) == ("!00001000", "HELTEC_V3", "2.7.0")
    assert conn["serial_port"] == "/dev/cu.wchusbserial2110" and conn["last_error"] == "boom"
    assert conn["transport"] == "serial" and conn["disconnected_by_user"] is False


def test_connect_tcp(env):
    r = make(env).post("/api/connection/connect", headers=H,
                       json={"transport": "tcp", "host": " 192.168.50.20 ", "port": 4404, "allow_any_node": True})
    assert r.status_code == 200
    assert env.mesh.calls == [("connect_tcp", "192.168.50.20", 4404, True)]


@pytest.mark.parametrize("host", ["radio.local", "fe80::1", "m7", "10.0.0.1."])
def test_connect_tcp_accepts_hosts_and_default_port(env, host):
    r = make(env).post("/api/connection/connect", headers=H, json={"transport": "tcp", "host": host})
    assert r.status_code == 200
    assert env.mesh.calls == [("connect_tcp", host, 4403, False)]


@pytest.mark.parametrize("payload", [
    {"transport": "tcp"},
    {"transport": "tcp", "host": ""},
    {"transport": "tcp", "host": "   "},
    {"transport": "tcp", "host": 5},
    {"transport": "tcp", "host": "my host"},
    {"transport": "tcp", "host": "http://10.0.0.1"},
    {"transport": "tcp", "host": "10.0.0.1/x"},
    {"transport": "tcp", "host": "10.0.0.1:4403"},
    {"transport": "tcp", "host": "a" * 254},
    {"transport": "tcp", "host": "10.0.0.1", "port": 0},
    {"transport": "tcp", "host": "10.0.0.1", "port": 65536},
    {"transport": "tcp", "host": "10.0.0.1", "port": "4403"},
    {"transport": "tcp", "host": "10.0.0.1", "port": True},
    {"transport": "tcp", "host": "10.0.0.1", "port": 1.5},
    {"transport": "tcp", "host": "10.0.0.1", "allow_any_node": "yes"},
    {"transport": "usb"},
])
def test_connect_tcp_validation(env, payload):
    r = make(env).post("/api/connection/connect", headers=H, json=payload)
    assert r.status_code == 400 and r.get_json()["error"]
    assert env.mesh.calls == []


def test_connect_explicit_serial_transport_unchanged(env):
    env.mesh.ports = [PORT_ROW]
    r = make(env).post("/api/connection/connect", headers=H,
                       json={"transport": "serial", "port": PORT_ROW["path"]})
    assert r.status_code == 200
    assert env.mesh.calls == [("connect", PORT_ROW["path"], False)]


def test_state_reports_address_in_use(env):
    env.mesh.link = {"transport": "tcp", "host": "10.1.2.3", "port": 4500}
    conn = make(env).get("/api/state").get_json()["connection"]
    assert (conn["host"], conn["port"]) == ("10.1.2.3", 4500)


def test_request_position_ok(env):
    c = make(env)
    r = c.post(f"/api/nodes/{NODE_ID}/request-position", json={}, headers=H)
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert env.mesh.calls[-1] == ("request_position", int(NODE_ID[1:], 16))


def test_request_position_bad_id(env):
    c = make(env)
    assert c.post("/api/nodes/bad/request-position", json={}, headers=H).status_code == 400
    assert not env.mesh.calls


def test_request_position_disconnected(env):
    env.mesh.connected = False
    r = make(env).post(f"/api/nodes/{NODE_ID}/request-position", json={}, headers=H)
    assert r.status_code == 409


def test_position_reply_updates_node(env):
    from types import SimpleNamespace

    from meshalerts.service import Service

    enroll(env)
    env.clock.advance(100)
    stub = SimpleNamespace(
        _node_id=Service._node_id, _touch=lambda n: None, db=env.db, gps=env.gps,
    )
    packet = {"fromId": NODE_ID, "rxTime": int(env.clock()),
              "decoded": {"position": {"latitude": 50.25, "longitude": 24.75}}}
    Service._on_position(stub, packet)
    node = env.db.get_node(NODE_ID)
    assert node["latitude"] == pytest.approx(50.25) and node["longitude"] == pytest.approx(24.75)
