import threading
import time
from types import SimpleNamespace

from conftest import LOCAL
from meshalerts.config import MeshtasticConfig, TxConfig
from meshalerts.mesh import (
    FFCollapsingStream, MeshConnection, NoSerialPort, NotConnected, ff_fix_enabled, select_serial_port,
)

import pytest


class FakeIface:
    def __init__(self):
        self.myInfo = SimpleNamespace(my_node_num=LOCAL)
        self.isConnected = threading.Event()
        self.isConnected.set()
        self.nodesByNum = {1: {"num": 1}}
        self.socket = None
        self.sent = []
        self.closed = False

    def sendData(self, data, **kw):
        self.sent.append((data, kw))
        return SimpleNamespace(id=77)

    def sendHeartbeat(self):
        pass

    def close(self):
        self.closed = True


def make(factory):
    got = {"text": [], "connected": []}
    conn = MeshConnection(
        MeshtasticConfig(host="x", reconnect_max_backoff_s=60), TxConfig(),
        on_text=got["text"].append, on_position=lambda p: None, on_nodeinfo=lambda p: None,
        on_connected=got["connected"].append, iface_factory=factory, subscribe=False,
    )
    return conn, got


def test_send_text_uses_senddata_with_ack_permitted():
    iface = FakeIface()
    conn, _ = make(lambda h, p: iface)
    conn._iface, conn._local_num = iface, LOCAL
    pid = conn.send_text(0xA1, "Привіт", lambda p: None)
    data, kw = iface.sent[0]
    assert pid == 77
    assert data == "Привіт".encode()
    assert kw["wantAck"] is True and kw["onResponseAckPermitted"] is True
    assert kw["destinationId"] == 0xA1


def test_send_text_when_down_raises():
    conn, _ = make(lambda h, p: None)
    with pytest.raises(NotConnected):
        conn.send_text(1, "x", lambda p: None)


def test_events_from_other_interface_or_self_are_ignored():
    iface = FakeIface()
    conn, _ = make(lambda h, p: iface)
    conn._iface, conn._local_num = iface, LOCAL
    conn._rx_text({"from": 5}, FakeIface())
    conn._rx_text({"from": LOCAL}, iface)
    conn._rx_text({"from": 5}, iface)
    assert conn._inbound.qsize() == 1


def test_supervisor_connects_and_calls_back():
    iface = FakeIface()
    conn, got = make(lambda h, p: iface)
    conn.start()
    deadline = time.time() + 5
    while not got["connected"] and time.time() < deadline:
        time.sleep(0.05)
    assert got["connected"] == [iface]
    assert conn.is_connected() and conn.local_node_id == "!00001000"
    assert conn.known_nodes() == {1: {"num": 1}}
    conn.stop()
    assert iface.closed


def test_supervisor_retries_after_failure():
    calls = []

    def factory(host, port):
        calls.append(1)
        raise OSError("refused")

    conn, _ = make(factory)
    conn.start()
    time.sleep(0.5)
    conn.stop()
    assert len(calls) == 1
    assert "refused" in conn.last_error


# serial transport

def port(device, vid=None):
    return SimpleNamespace(device=device, vid=vid)


def scfg(**kw):
    return MeshtasticConfig(transport="serial", reconnect_max_backoff_s=60, **kw)


def test_select_glob_match():
    ports = [port("/dev/cu.usbserial-1", 0x0403), port("/dev/cu.wchusbserial2110", 0x1A86)]
    assert select_serial_port(scfg(), ports) == "/dev/cu.wchusbserial2110"


def test_select_falls_back_to_known_vids():
    ports = [port("/dev/cu.usbmodem1", 0x303A), port("/dev/cu.SLAB_USBtoUART", 0x10C4),
             port("/dev/cu.Bluetooth-Incoming-Port")]
    assert select_serial_port(scfg(), ports) == "/dev/cu.SLAB_USBtoUART"


def test_select_never_picks_espressif():
    ports = [port("/dev/cu.usbmodem1101", 0x303A)]
    with pytest.raises(NoSerialPort):
        select_serial_port(scfg(serial_port_glob="/dev/cu.usbmodem*"), ports)


def test_select_prefers_cu_over_tty():
    ports = [port("/dev/tty.usbserial-1", 0x0403), port("/dev/cu.usbserial-1", 0x0403)]
    assert select_serial_port(scfg(serial_port_glob="/nomatch*"), ports) == "/dev/cu.usbserial-1"


def test_select_explicit_path_wins_even_for_espressif():
    ports = [port("/dev/cu.wchusbserial2110", 0x1A86), port("/dev/cu.usbmodem1101", 0x303A)]
    assert select_serial_port(scfg(serial_port="/dev/cu.usbmodem1101"), ports) == "/dev/cu.usbmodem1101"


def test_select_several_uses_first_sorted():
    ports = [port("/dev/cu.wchusbserial2", 0x1A86), port("/dev/cu.wchusbserial1", 0x1A86)]
    assert select_serial_port(scfg(), ports) == "/dev/cu.wchusbserial1"


def make_serial(factory, lister, **cfg):
    got = {"connected": []}
    conn = MeshConnection(
        scfg(**cfg), TxConfig(),
        on_text=lambda p: None, on_position=lambda p: None, on_nodeinfo=lambda p: None,
        on_connected=got["connected"].append, subscribe=False,
        serial_factory=factory, port_lister=lister,
    )
    return conn, got


def wait_for(cond, timeout=5):
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        time.sleep(0.05)
    return cond()


def test_serial_connects_to_detected_port():
    iface = FakeIface()
    opened = []
    conn, got = make_serial(lambda path, ff_fix=False: opened.append(path) or iface,
                            lambda: [port("/dev/cu.wchusbserial2110", 0x1A86)],
                            expected_node_id="!00001000")
    conn.start()
    assert wait_for(lambda: got["connected"])
    assert opened == ["/dev/cu.wchusbserial2110"] and conn.active_port == opened[0]
    assert conn.is_connected()
    conn.stop()


def test_wrong_node_id_is_rejected(caplog):
    iface = FakeIface()
    conn, got = make_serial(lambda path, ff_fix=False: iface, lambda: [port("/dev/cu.wchusbserial1", 0x1A86)],
                            expected_node_id="!5f4e7f34")
    with caplog.at_level("ERROR"):
        conn.start()
        assert wait_for(lambda: iface.closed)
    conn.stop()
    assert got["connected"] == []
    assert not conn.is_connected()
    assert any("Wrong radio" in r.message and r.levelname == "ERROR" for r in caplog.records)


def test_expected_node_id_applies_to_tcp_too():
    iface = FakeIface()
    got = []
    conn = MeshConnection(
        MeshtasticConfig(host="x", expected_node_id="!5f4e7f34"), TxConfig(),
        on_text=lambda p: None, on_position=lambda p: None, on_nodeinfo=lambda p: None,
        on_connected=got.append, iface_factory=lambda h, p: iface, subscribe=False,
    )
    conn.start()
    assert wait_for(lambda: iface.closed)
    conn.stop()
    assert got == []


def test_busy_port_logs_and_retries(caplog, monkeypatch):
    monkeypatch.setattr("meshalerts.mesh.INITIAL_BACKOFF_S", 0.05)
    calls = []

    def factory(path, ff_fix=False):
        calls.append(path)
        raise OSError(16, "[Errno 16] Resource busy")

    conn, _ = make_serial(factory, lambda: [port("/dev/cu.wchusbserial1", 0x1A86)])
    with caplog.at_level("WARNING"):
        conn.start()
        assert wait_for(lambda: len(calls) >= 2)
    conn.stop()
    assert any("Serial port is busy. Close other programs" in r.message for r in caplog.records)


def test_unplug_then_reconnect_redetects_new_path(monkeypatch):
    monkeypatch.setattr("meshalerts.mesh.INITIAL_BACKOFF_S", 0.05)
    first, second = FakeIface(), FakeIface()
    ifaces = [first, second]
    paths = [["/dev/cu.wchusbserial2110"], ["/dev/cu.wchusbserial3120"]]
    opened = []

    def lister():
        return [port(d, 0x1A86) for d in (paths[0] if len(opened) < 1 else paths[1])]

    def factory(path, ff_fix=False):
        opened.append(path)
        return ifaces[len(opened) - 1]

    conn, got = make_serial(factory, lister)
    conn.start()
    assert wait_for(lambda: len(got["connected"]) == 1)
    first.isConnected.clear()
    assert wait_for(lambda: len(got["connected"]) == 2)
    assert opened == ["/dev/cu.wchusbserial2110", "/dev/cu.wchusbserial3120"]
    assert conn.active_port == "/dev/cu.wchusbserial3120"
    conn.stop()
    assert first.closed


def test_tcp_path_unchanged_uses_host_port_and_keepalive():
    iface = FakeIface()
    seen = []
    iface.socket = SimpleNamespace(setsockopt=lambda *a: calls.append(a))
    calls = []
    conn, got = make(lambda h, p: seen.append((h, p)) or iface)
    conn.start()
    assert wait_for(lambda: got["connected"])
    conn.stop()
    assert seen == [("x", 4403)]
    assert calls


# 0xFF doubling (WCH driver)

class ChunkStream:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = False

    def read(self, n=1):
        if not self.chunks:
            return b""
        head = self.chunks.pop(0)
        if len(head) > n:
            self.chunks.insert(0, head[n:])
            head = head[:n]
        return head

    def write(self, data):
        return len(data)


def drain(stream, size):
    out = b""
    while True:
        b = stream.read(size)
        if not b:
            return out
        out += b


@pytest.mark.parametrize("size", [1, 2, 3, 100])
def test_ff_pair_split_across_reads(size):
    s = FFCollapsingStream(ChunkStream([b"a\xff", b"\xffb"]))
    assert drain(s, size) == b"a\xffb"


def test_ff_odd_trailing_held_back_until_next_read():
    s = FFCollapsingStream(ChunkStream([b"a\xff"]))
    assert s.read(10) == b"a"
    assert s.read(10) == b""
    s._stream.chunks.append(b"\xffz")
    assert s.read(10) == b"\xffz"


def test_ff_long_runs():
    s = FFCollapsingStream(ChunkStream([b"\xff" * 16, b"\xff" * 4, b"x"]))
    assert drain(s, 4) == b"\xff" * 10 + b"x"


def test_ff_unpaired_emitted_and_logged(caplog):
    s = FFCollapsingStream(ChunkStream([b"\xffa\xff\xffb"]))
    with caplog.at_level("DEBUG", logger="meshalerts.mesh"):
        assert drain(s, 10) == b"\xffa\xffb"
    assert "unpaired 0xFF" in caplog.text


def test_ff_size_limit_respected():
    s = FFCollapsingStream(ChunkStream([b"abcdef\xff\xff\xff\xffgh"]))
    sizes = []
    while True:
        b = s.read(3)
        if not b:
            break
        assert len(b) <= 3
        sizes.append(b)
    assert b"".join(sizes) == b"abcdef\xff\xffgh"


def test_ff_passthrough_attributes():
    s = FFCollapsingStream(ChunkStream([]))
    assert s.write(b"abc") == 3
    assert s.closed is False


def _frame_of(msg):
    return msg.SerializeToString()


def make_from_radio():
    from meshtastic import mesh_pb2

    fr = mesh_pb2.FromRadio()
    fr.packet.id = 0x12345678
    setattr(fr.packet, "from", 0x5F4E7F34)
    fr.packet.to = 0xFFFFFFFF
    fr.packet.rx_rssi = -31
    fr.packet.rx_snr = 5.5
    fr.packet.decoded.portnum = 1
    fr.packet.decoded.payload = b"!start"
    return fr


def test_doubled_frame_decodes_after_fix():
    from meshtastic import mesh_pb2

    raw = _frame_of(make_from_radio())
    assert b"\xff" in raw
    doubled = raw.replace(b"\xff", b"\xff\xff")
    with pytest.raises(Exception):
        mesh_pb2.FromRadio.FromString(doubled[: len(raw)])
    chunks = [doubled[i:i + 7] for i in range(0, len(doubled), 7)]
    fixed = drain(FFCollapsingStream(ChunkStream(chunks)), 1)
    assert fixed == raw
    msg = mesh_pb2.FromRadio.FromString(fixed)
    assert msg.packet.decoded.payload.decode() == "!start"


TRUNCATED = (
    b'\x12e\r\xddXA\x05\x154\x7fN_"\x0c\x08\x01\x12\x06!startH\x005\r\xe2\x88Q=\xef\xfb\xf2jE\x00\x000AH\x01P\x01`\xe1'
    + b'\xff' * 16
    + b'\x01x\x01\x82\x01 bD&\xb2\xddq\x0b\x96\xed\x9c5\xe2\xabRs\xd5\x92\x93\xe4\x84\x85;\xc2lBH\x96\x8f\x02\x95\xfb\x02\x88\x01'
)


def test_logged_truncated_frame_fails_without_fix_and_parses_with_it():
    from google.protobuf.message import DecodeError
    from meshtastic import mesh_pb2

    with pytest.raises(DecodeError):
        mesh_pb2.FromRadio.FromString(TRUNCATED)
    fixed = drain(FFCollapsingStream(ChunkStream([TRUNCATED])), 1)
    assert len(fixed) == len(TRUNCATED) - 8
    # The logged frame was cut at the declared length (101 bytes), so it still lacks its tail.
    # Parsing the full doubled payload of this size is covered by test_doubled_frame_decodes_after_fix.
    assert fixed.count(b"\xff") == 8


def test_ff_fix_auto_selection():
    def cfg(mode="auto"):
        return scfg(serial_ff_fix=mode)

    assert ff_fix_enabled(cfg(), "/dev/cu.wchusbserial2110", [])
    assert ff_fix_enabled(cfg(), "/dev/cu.usbserial-1", [port("/dev/cu.usbserial-1", 0x1A86)])
    assert not ff_fix_enabled(cfg(), "/dev/cu.usbserial-1", [port("/dev/cu.usbserial-1", 0x0403)])
    assert not ff_fix_enabled(cfg(), "/dev/cu.usbmodem1", [])
    assert ff_fix_enabled(cfg("on"), "/dev/cu.usbmodem1", [])
    assert not ff_fix_enabled(cfg("off"), "/dev/cu.wchusbserial1", [])


def test_serial_factory_gets_ff_fix_flag_when_on():
    calls = []
    iface = FakeIface()
    conn, got = make_serial(lambda path, ff_fix=False: calls.append(ff_fix) or iface,
                            lambda: [port("/dev/cu.wchusbserial2110", 0x1A86)],
                            expected_node_id="!00001000", serial_ff_fix="auto")
    conn.start()
    assert wait_for(lambda: got["connected"])
    conn.stop()
    assert calls == [True]


# radio connection control

def full_port(device, vid=None, pid=None, description="n/a", **kw):
    return SimpleNamespace(device=device, vid=vid, pid=pid, description=description,
                           manufacturer=kw.get("manufacturer"), serial_number=kw.get("serial_number"))


PORTS = [
    full_port("/dev/cu.usbmodem101", 0x303A, 0x1001, "T-Deck", manufacturer="Espressif"),
    full_port("/dev/cu.wchusbserial2110", 0x1A86, 0x7523, "USB Serial", serial_number="ABC"),
    full_port("/dev/tty.wchusbserial2110", 0x1A86, 0x7523, "USB Serial"),
    full_port("/dev/cu.Bluetooth-Incoming-Port"),
]


def test_list_ports_flags():
    conn, _ = make_serial(lambda path, ff_fix=False: FakeIface(), lambda: PORTS)
    by_path = {p["path"]: p for p in conn.list_ports()}
    assert "/dev/tty.wchusbserial2110" not in by_path
    wch = by_path["/dev/cu.wchusbserial2110"]
    assert (wch["vid"], wch["pid"], wch["serial_number"]) == ("1a86", "7523", "ABC")
    assert wch["likely"] and not wch["espressif"] and not wch["selected"] and not wch["active"]
    deck = by_path["/dev/cu.usbmodem101"]
    assert deck["espressif"] and not deck["likely"] and deck["manufacturer"] == "Espressif"
    bt = by_path["/dev/cu.Bluetooth-Incoming-Port"]
    assert bt["vid"] is None and bt["description"] is None and not bt["likely"]
    assert conn.list_ports()[0]["path"] == "/dev/cu.wchusbserial2110"


def test_list_ports_selected_and_active():
    iface = FakeIface()
    conn, got = make_serial(lambda path, ff_fix=False: iface, lambda: PORTS)
    conn.start()
    conn.connect("/dev/cu.wchusbserial2110")
    assert wait_for(lambda: got["connected"])
    by_path = {p["path"]: p for p in conn.list_ports()}
    assert by_path["/dev/cu.wchusbserial2110"]["selected"] and by_path["/dev/cu.wchusbserial2110"]["active"]
    assert not by_path["/dev/cu.usbmodem101"]["selected"]
    conn.stop()


def test_manual_connect_uses_exact_path_even_for_espressif():
    opened = []
    conn, got = make_serial(lambda path, ff_fix=False: opened.append(path) or FakeIface(), lambda: PORTS)
    conn.connect("/dev/cu.usbmodem101")
    conn.start()
    assert wait_for(lambda: got["connected"])
    assert opened == ["/dev/cu.usbmodem101"]
    st = conn.status()
    assert st["mode"] == "manual" and st["chosen_port"] == "/dev/cu.usbmodem101"
    assert st["connected"] and st["node_id"] == "!00001000" and st["transport"] == "serial"
    conn.stop()


def test_manual_connect_overrides_tcp_transport():
    opened = []
    got = []
    conn = MeshConnection(
        MeshtasticConfig(transport="tcp", host="x"), TxConfig(),
        on_text=lambda p: None, on_position=lambda p: None, on_nodeinfo=lambda p: None,
        on_connected=got.append, subscribe=False,
        iface_factory=lambda h, p: pytest.fail("tcp must not be used"),
        serial_factory=lambda path, ff_fix=False: opened.append(path) or FakeIface(),
        port_lister=lambda: PORTS,
    )
    conn.connect("/dev/cu.wchusbserial2110")
    conn.start()
    assert wait_for(lambda: got)
    assert opened == ["/dev/cu.wchusbserial2110"]
    conn.stop()


def test_connect_wakes_supervisor_without_backoff_wait():
    attempts = []
    ifaces = []

    def factory(path, ff_fix=False):
        attempts.append(path)
        if len(attempts) == 1:
            raise OSError(16, "[Errno 16] Resource busy")
        ifaces.append(FakeIface())
        return ifaces[-1]

    conn, got = make_serial(factory, lambda: PORTS)
    conn.start()
    assert wait_for(lambda: len(attempts) == 1)
    assert wait_for(lambda: conn.status()["next_retry_at"] is not None)
    assert conn.status()["last_error"]
    t0 = time.time()
    conn.connect("/dev/cu.wchusbserial2110")
    assert wait_for(lambda: got["connected"], timeout=3)
    assert time.time() - t0 < 3
    assert conn.status()["next_retry_at"] is None and conn.status()["last_error"] is None
    conn.stop()


def test_connect_replaces_live_connection():
    first, second = FakeIface(), FakeIface()
    queue_ = [first, second]
    conn, got = make_serial(lambda path, ff_fix=False: queue_.pop(0), lambda: PORTS)
    conn.start()
    assert wait_for(lambda: len(got["connected"]) == 1)
    conn.connect("/dev/cu.wchusbserial2110")
    assert wait_for(lambda: len(got["connected"]) == 2, timeout=3)
    assert first.closed and not second.closed
    conn.stop()


def test_disconnect_closes_and_stays_down(monkeypatch):
    monkeypatch.setattr("meshalerts.mesh.INITIAL_BACKOFF_S", 0.05)
    opened = []
    iface = FakeIface()
    conn, got = make_serial(lambda path, ff_fix=False: opened.append(path) or iface, lambda: PORTS)
    conn.start()
    assert wait_for(lambda: got["connected"])
    conn.disconnect()
    assert wait_for(lambda: iface.closed, timeout=3)
    time.sleep(0.4)
    assert len(opened) == 1 and not conn.is_connected()
    st = conn.status()
    assert st["disconnected_by_user"] and not st["connecting"] and st["active_port"] is None
    conn.connect(None)
    assert wait_for(lambda: len(opened) == 2, timeout=3)
    assert not conn.status()["disconnected_by_user"]
    conn.stop()


def test_settings_persist_across_restart(tmp_path):
    from meshalerts.db import Database

    path = tmp_path / "s.db"
    db = Database(path)
    conn, _ = make_serial(lambda p, ff_fix=False: FakeIface(), lambda: PORTS, )
    conn._settings = db
    conn.connect("/dev/cu.wchusbserial2110")
    conn.disconnect()
    db.close()

    def reopened():
        c = MeshConnection(
            scfg(), TxConfig(), on_text=lambda p: None, on_position=lambda p: None,
            on_nodeinfo=lambda p: None, on_connected=lambda i: None, subscribe=False,
            port_lister=lambda: PORTS, settings=Database(path),
        )
        return c

    st = reopened().status()
    assert (st["mode"], st["chosen_port"], st["disconnected_by_user"]) == ("manual", "/dev/cu.wchusbserial2110", True)
    c = reopened()
    c._settings.set_setting("radio.disconnected_by_user", "0")
    c.connect(None)
    st = reopened().status()
    assert (st["mode"], st["chosen_port"], st["disconnected_by_user"]) == ("auto", None, False)


def test_no_settings_uses_config_default():
    conn, _ = make_serial(lambda p, ff_fix=False: FakeIface(), lambda: PORTS)
    st = conn.status()
    assert st["mode"] == "auto" and st["chosen_port"] is None and not st["disconnected_by_user"]


def test_restart_with_user_disconnect_does_not_connect(tmp_path):
    from meshalerts.db import Database

    db = Database(tmp_path / "d.db")
    db.set_setting("radio.disconnected_by_user", "1")
    opened = []
    conn = MeshConnection(
        scfg(), TxConfig(), on_text=lambda p: None, on_position=lambda p: None,
        on_nodeinfo=lambda p: None, on_connected=lambda i: None, subscribe=False,
        serial_factory=lambda p, ff_fix=False: opened.append(p) or FakeIface(),
        port_lister=lambda: PORTS, settings=db,
    )
    conn.start()
    time.sleep(0.4)
    assert opened == []
    conn.stop()


def test_migration_adds_settings_table(tmp_path):
    import sqlite3
    from meshalerts.db import SCHEMA_VERSION, Database

    path = tmp_path / "v3.db"
    raw = sqlite3.connect(path)
    raw.executescript("CREATE TABLE alert_states (oblast TEXT PRIMARY KEY, active INTEGER NOT NULL, "
                      "updated_at INTEGER NOT NULL, since INTEGER); PRAGMA user_version=3;")
    raw.close()
    db = Database(path)
    assert db._one("PRAGMA user_version")["user_version"] == SCHEMA_VERSION == 5
    assert db.get_setting("x") is None and db.get_setting("x", "d") == "d"
    db.set_setting("x", "1")
    db.set_setting("x", "2")
    assert db.get_setting("x") == "2"


def test_wrong_node_message_names_both_ids():
    iface = FakeIface()
    conn, _ = make_serial(lambda path, ff_fix=False: iface, lambda: PORTS, expected_node_id="!5f4e7f34")
    conn.start()
    assert wait_for(lambda: conn.status()["last_error"])
    err = conn.status()["last_error"]
    conn.stop()
    assert "!00001000" in err and "!5f4e7f34" in err


def test_allow_any_node_skips_check_once(caplog, monkeypatch):
    monkeypatch.setattr("meshalerts.mesh.INITIAL_BACKOFF_S", 0.05)
    first, second = FakeIface(), FakeIface()
    queue_ = [first, second]
    conn, got = make_serial(lambda path, ff_fix=False: queue_.pop(0), lambda: PORTS, expected_node_id="!5f4e7f34")
    with caplog.at_level("WARNING"):
        conn.connect("/dev/cu.wchusbserial2110", allow_any_node=True)
        conn.start()
        assert wait_for(lambda: len(got["connected"]) == 1)
    assert any("Accept any node" in r.message and r.levelname == "WARNING" for r in caplog.records)
    first.isConnected.clear()
    assert wait_for(lambda: second.closed)
    assert len(got["connected"]) == 1
    assert "Wrong radio" in conn.status()["last_error"]
    conn.stop()


def test_without_allow_any_wrong_node_is_rejected_even_when_manual():
    iface = FakeIface()
    conn, got = make_serial(lambda path, ff_fix=False: iface, lambda: PORTS, expected_node_id="!5f4e7f34")
    conn.connect("/dev/cu.wchusbserial2110")
    conn.start()
    assert wait_for(lambda: iface.closed)
    assert got["connected"] == []
    conn.stop()


def test_allow_any_not_persisted(tmp_path):
    from meshalerts.db import Database

    db = Database(tmp_path / "a.db")
    conn, _ = make_serial(lambda p, ff_fix=False: FakeIface(), lambda: PORTS)
    conn._settings = db
    conn.connect("/dev/cu.wchusbserial2110", allow_any_node=True)
    assert conn._allow_any is True
    c2 = MeshConnection(scfg(), TxConfig(), on_text=lambda p: None, on_position=lambda p: None,
                        on_nodeinfo=lambda p: None, on_connected=lambda i: None, subscribe=False,
                        settings=db)
    assert c2._allow_any is False and c2.status()["chosen_port"] == "/dev/cu.wchusbserial2110"
