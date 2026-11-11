import json
import sys
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from meshalerts.alerts import AlertPoller  # noqa: E402
from meshalerts.commands import CommandHandler  # noqa: E402
from meshalerts.config import Config  # noqa: E402
from meshalerts.db import Database  # noqa: E402
from meshalerts.gps import GpsTracker  # noqa: E402
from meshalerts.mesh import NotConnected  # noqa: E402
from meshalerts.notifier import Notifier  # noqa: E402
from meshalerts.oblast import OblastIndex  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
LOCAL = 0x1000
NODE_A = 0xA1B2C3D4
LVIV = "Львівська область"
TERNOPIL = "Тернопільська область"
KYIV = "м. Київ"


def sample_response() -> dict:
    return json.loads((FIXTURES / "ubilling_sample.json").read_text(encoding="utf-8"))


def all_quiet() -> dict:
    data = sample_response()
    for v in data["states"].values():
        v["alertnow"] = False
    return data


class FakeResponse:
    def __init__(self, data=None, status=200, text=None):
        self.status_code = status
        self._data = data
        self._text = text

    def json(self):
        if self._text is not None:
            raise ValueError("bad json")
        return self._data


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.queue = []
        self.calls = 0

    def get(self, url, timeout=None):
        self.calls += 1
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeMesh:
    local_node_num = LOCAL
    local_node_id = "!00001000"
    last_error = None

    def __init__(self):
        self.connected = True
        self.sent = []
        self.nodes = {}
        self.fail_next = None
        self._next_id = 100
        self.ports = []
        self.calls = []
        self.link = {}

    def is_connected(self):
        return self.connected

    def known_nodes(self):
        return self.nodes

    def status(self):
        base = {
            "mode": "auto", "chosen_port": None, "active_port": None, "connected": self.connected,
            "connecting": False, "disconnected_by_user": False, "last_error": None,
            "next_retry_at": None, "transport": None, "node_id": None, "hw_model": None, "firmware": None,
        }
        return {**base, **self.link}

    def list_ports(self):
        return self.ports

    def connect(self, path=None, allow_any_node=False, tcp=None):
        if tcp is not None:
            self.calls.append(("connect_tcp", tcp[0], tcp[1], allow_any_node))
        else:
            self.calls.append(("connect", path, allow_any_node))

    def disconnect(self):
        self.calls.append(("disconnect",))

    def send_text(self, dest_num, text, on_ack):
        if not self.connected:
            raise NotConnected("down")
        if self.fail_next:
            exc, self.fail_next = self.fail_next, None
            raise exc
        self._next_id += 1
        self.sent.append({"dest": dest_num, "text": text, "cb": on_ack, "id": self._next_id})
        return self._next_id

    def request_position(self, dest_num):
        if not self.connected:
            raise NotConnected("down")
        self.calls.append(("request_position", dest_num))

    def texts(self):
        return [s["text"] for s in self.sent]


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class Env:
    def __init__(self, tmp_path=None, policy="assigned", db_path=":memory:", geojson="mini_ukraine.geojson"):
        self.config = Config()
        self.config.tx.min_interval_s = 0
        self.config.tx.command_cooldown_s = 10
        self.config.alerts.border_policy = policy
        self.clock = Clock()
        self.db = Database(db_path)
        self.mesh = FakeMesh()
        self.oblasts = OblastIndex(FIXTURES / geojson, 1500, 3000)
        self.session = FakeSession()
        self.build()

    def build(self):
        c = self.config
        self.poller = AlertPoller(
            c.alerts, self.db, lambda t: self.notifier.on_transition(t), self.clock,
            session=self.session, on_first_valid_poll=lambda: self.notifier.reconcile(),
        )
        self.notifier = Notifier(
            self.db, self.mesh, c.tx, c.alerts, self.clock,
            state_of=self.poller.current_state, oblasts=self.oblasts,
        )
        self.gps = GpsTracker(
            self.db, self.oblasts, c.gps, self.clock,
            on_region_change=lambda n, o, new: self.notifier.on_region_change(n, o, new),
        )
        self.commands = CommandHandler(
            self.db, self.gps, self.oblasts, self.poller, self.notifier,
            self.mesh, c.tx, c.alerts, self.clock,
        )

    def restart(self):
        """New objects on the same database. Keeps the DB, loses memory state."""
        self.session = FakeSession()
        self.mesh.sent = []
        self.build()

    def poll(self, data):
        self.session.queue.append(FakeResponse(data))
        return self.poller.poll_once()

    def drain(self):
        while self.notifier.step():
            pass

    def text_packet(self, text, sender=NODE_A, to=LOCAL, **extra):
        p = {
            "from": sender, "to": to, "fromId": f"!{sender:08x}",
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": text},
        }
        p.update(extra)
        return p

    def send_command(self, text, **kw):
        self.commands.handle_text(self.text_packet(text, **kw))
        self.drain()

    def position(self, node_id, lat, lon, ts=None, **kw):
        pos = {"latitude": lat, "longitude": lon, "time": int(ts or self.clock())}
        return self.gps.handle_position(node_id, pos, **kw)

    def ack(self, index, sender, error=None):
        routing = {} if error is None else {"errorReason": error}
        self.mesh.sent[index]["cb"]({"from": sender, "decoded": {"routing": routing}})


NODE_ID = f"!{NODE_A:08x}"


@pytest.fixture
def env():
    return Env()


def enroll(env, lat=49.5, lon=24.5, node_num=NODE_A, quiet=True):
    """Enroll a node in a quiet country and clear the sent list."""
    if quiet:
        env.poll(all_quiet())
    env.mesh.nodes[node_num] = {
        "position": {"latitude": lat, "longitude": lon, "time": int(env.clock())},
        "lastHeard": int(env.clock()),
    }
    env.send_command("!start", sender=node_num)
    env.mesh.sent.clear()
    env.clock.advance(11)


def region_data(**active):
    data = all_quiet()
    for name, value in active.items():
        data["states"][name.replace("_", " ")]["alertnow"] = value
    return data


def lviv(active):
    data = all_quiet()
    data["states"]["Львівська область"]["alertnow"] = active
    return data
