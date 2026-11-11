"""TCP or USB serial connection to the Meshtastic node, with its own supervisor."""

from __future__ import annotations

import copy
import fnmatch
import logging
import queue
import socket
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable

from .db import node_id_from_num

log = logging.getLogger(__name__)

INITIAL_BACKOFF_S = 5
STABLE_AFTER_S = 60

# Espressif (0x303a) is deliberately absent. A T-Deck on non-Meshtastic firmware uses it,
# and auto-detection must never open that device.
ESPRESSIF_VID = 0x303A
USB_UART_VIDS = (0x1A86, 0x10C4, 0x0403)  # WCH CH34x, CP210x, FTDI


class NotConnected(Exception):
    """The radio connection is down."""


class NoSerialPort(Exception):
    """Auto-detection found no usable serial port."""


class WrongDevice(Exception):
    """The connected radio is not the expected node."""


class Reconfigured(Exception):
    """connect() or disconnect() replaced the connection settings."""


SETTING_MODE = "radio.mode"
SETTING_PORT = "radio.port"
SETTING_DISCONNECTED = "radio.disconnected_by_user"


def default_factory(host: str, port: int):
    from meshtastic.tcp_interface import TCPInterface

    return TCPInterface(hostname=host, portNumber=port, timeout=30)


class FFCollapsingStream:
    """Read-side wrapper that collapses each 0xFF 0xFF pair into one 0xFF.

    The WCH macOS DriverKit driver (CH34xVCPDriver, CH340 family, VID 0x1a86) doubles every
    0xFF byte it receives from the device. termios is clean (no PARMRK/ISTRIP), so this is
    the driver itself. The Meshtastic stream framing trusts the declared frame length, so
    the extra bytes truncate the frame tail and the protobuf fails with "Wire format was
    corrupt". Every real 0xFF arrives as exactly two, so pairing is lossless.
    TX is not affected.
    """

    def __init__(self, stream):
        self._stream = stream
        self._out = bytearray()
        self._held = False  # a trailing 0xFF waiting for its partner

    def __getattr__(self, name):
        return getattr(self._stream, name)

    def _feed(self, raw: bytes) -> None:
        for b in raw:
            if self._held:
                self._held = False
                if b == 0xFF:
                    self._out.append(0xFF)
                    continue
                log.debug("Serial 0xFF fix: unpaired 0xFF.")
                self._out.append(0xFF)
            if b == 0xFF:
                self._held = True
            else:
                self._out.append(b)

    def read(self, size: int = 1) -> bytes:
        while not self._out:
            raw = self._stream.read(max(size, 1))
            if not raw:
                break
            self._feed(raw)
        data = bytes(self._out[:size])
        del self._out[:size]
        return data


def default_serial_factory(path: str, ff_fix: bool = False):
    from meshtastic.serial_interface import SerialInterface

    class FFFixSerialInterface(SerialInterface):
        # connect() starts the reader thread inside super().connect(), and the base class
        # creates self.stream there, so wrapping in connect() would race the reader.
        # _readBytes is the single read path, so wrap lazily there.
        def _readBytes(self, length):
            stream = self.stream
            if stream is None:
                return None
            if not isinstance(stream, FFCollapsingStream):
                stream = self.stream = FFCollapsingStream(stream)
            return stream.read(length)

    cls = FFFixSerialInterface if ff_fix else SerialInterface
    # Pass the path. With devPath=None the library scans ports itself and fails on several.
    return cls(devPath=path, timeout=30)


def ff_fix_enabled(config, path: str, comports) -> bool:
    mode = config.serial_ff_fix
    if mode != "auto":
        return mode == "on"
    if "wchusbserial" in path:
        return True
    return any(getattr(p, "device", None) == path and getattr(p, "vid", None) == 0x1A86 for p in comports)


def default_port_lister():
    from serial.tools import list_ports

    return list(list_ports.comports())


def select_serial_port(config, comports) -> str:
    """Pick the serial port path. An explicit serial_port always wins."""
    explicit = config.serial_port.strip()
    if explicit:
        return explicit
    ports = [p for p in comports if getattr(p, "vid", None) != ESPRESSIF_VID]
    glob = config.serial_port_glob.strip()
    candidates = [p.device for p in ports if glob and fnmatch.fnmatch(p.device, glob)]
    if not candidates:
        candidates = [p.device for p in ports if getattr(p, "vid", None) in USB_UART_VIDS]
    if any(d.startswith("/dev/cu.") for d in candidates):
        candidates = [d for d in candidates if not d.startswith("/dev/tty.")]
    candidates = sorted(set(candidates))
    if not candidates:
        raise NoSerialPort("No USB serial port found.")
    if len(candidates) > 1:
        log.warning("Several serial ports match: %s. Using %s.", ", ".join(candidates), candidates[0])
    return candidates[0]


def is_port_busy(exc: BaseException) -> bool:
    text = str(exc).lower()
    return getattr(exc, "errno", None) == 16 or "errno 16" in text or "resource busy" in text


def enable_keepalive(sock: Any) -> None:
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        # macOS names the idle option TCP_KEEPALIVE. Linux names it TCP_KEEPIDLE.
        idle = getattr(socket, "TCP_KEEPALIVE", None) or getattr(socket, "TCP_KEEPIDLE", None)
        if idle is not None:
            sock.setsockopt(socket.IPPROTO_TCP, idle, 30)
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
    except OSError as exc:
        log.warning("Cannot set TCP keepalive: %s", exc)


class MeshConnection:
    def __init__(
        self,
        config,
        tx_config,
        on_text: Callable[[dict], None],
        on_position: Callable[[dict], None],
        on_nodeinfo: Callable[[dict], None],
        on_connected: Callable[[Any], None],
        iface_factory: Callable[[str, int], Any] = default_factory,
        subscribe: bool = True,
        serial_factory: Callable[..., Any] = default_serial_factory,
        port_lister: Callable[[], list] = default_port_lister,
        settings: Any = None,
    ):
        self.config = config
        self.tx_config = tx_config
        self._handlers = {
            "text": on_text,
            "position": on_position,
            "nodeinfo": on_nodeinfo,
        }
        self._on_connected = on_connected
        self._factory = iface_factory
        self._serial_factory = serial_factory
        self._port_lister = port_lister
        self._settings = settings
        self.active_port: str | None = None
        self.next_retry_at: float | None = None
        self._ctl = threading.Lock()
        self._wake = threading.Event()
        self._gen = 0
        self._mode = "auto"
        self._chosen_port: str | None = None
        self._user_disconnected = False
        self._allow_any = False
        self._connecting = False
        self._radio: dict[str, str | None] = {}
        self._load_settings()
        self._iface: Any = None
        self._local_num: int | None = None
        self._lost = threading.Event()
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._inbound: queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []
        self.last_error: str | None = None
        self.connected_since: float | None = None
        self.reconnects = 0
        # pypubsub keeps weak references. These strong references keep the listeners alive.
        self._listeners = [
            self._rx_text, self._rx_position, self._rx_user, self._conn_lost,
        ]
        if subscribe:
            from pubsub import pub

            pub.subscribe(self._rx_text, "meshtastic.receive.text")
            pub.subscribe(self._rx_position, "meshtastic.receive.position")
            pub.subscribe(self._rx_user, "meshtastic.receive.user")
            pub.subscribe(self._conn_lost, "meshtastic.connection.lost")

    def _load_settings(self) -> None:
        if self._settings is None:
            return
        mode = self._settings.get_setting(SETTING_MODE)
        port = self._settings.get_setting(SETTING_PORT)
        if mode == "manual" and port:
            self._mode, self._chosen_port = "manual", port
        self._user_disconnected = self._settings.get_setting(SETTING_DISCONNECTED) == "1"

    def _persist(self) -> None:
        if self._settings is None:
            return
        self._settings.set_setting(SETTING_MODE, self._mode)
        self._settings.set_setting(SETTING_PORT, self._chosen_port or "")
        self._settings.set_setting(SETTING_DISCONNECTED, "1" if self._user_disconnected else "0")

    def _bump(self) -> None:
        """Publish new settings. Callers hold _ctl. The supervisor drops the attempt in progress."""
        self._gen += 1
        self.last_error = None
        self.next_retry_at = None
        self._persist()
        self._wake.set()

    def connect(self, path: str | None = None, allow_any_node: bool = False) -> None:
        """Connect now. path None means auto-detect. Returns at once. The supervisor does the work."""
        with self._ctl:
            self._mode = "auto" if path is None else "manual"
            self._chosen_port = path
            self._user_disconnected = False
            self._allow_any = bool(allow_any_node) and path is not None
            self._connecting = True
            self._bump()

    def disconnect(self) -> None:
        """Close the radio and stay down until connect() is called."""
        with self._ctl:
            self._user_disconnected = True
            self._allow_any = False
            self._connecting = False
            self._bump()

    def list_ports(self) -> list[dict]:
        comports = list(self._port_lister())
        devices = {getattr(p, "device", None) for p in comports}
        with self._ctl:
            chosen = self._chosen_port if self._mode == "manual" else None
        glob = self.config.serial_port_glob.strip()
        connected = self.is_connected()
        out = []
        for p in comports:
            path = p.device
            # macOS lists every port as cu.X and tty.X. Only cu.X is useful here.
            if path.startswith("/dev/tty.") and "/dev/cu." + path[len("/dev/tty."):] in devices:
                continue
            vid, pid = getattr(p, "vid", None), getattr(p, "pid", None)
            espressif = vid == ESPRESSIF_VID
            likely = not espressif and (
                bool(glob and fnmatch.fnmatch(path, glob)) or vid in USB_UART_VIDS
            )

            def text(name):
                v = getattr(p, name, None)
                return None if v in (None, "", "n/a") else str(v)

            out.append({
                "path": path,
                "description": text("description"),
                "manufacturer": text("manufacturer"),
                "vid": None if vid is None else f"{vid:04x}",
                "pid": None if pid is None else f"{pid:04x}",
                "serial_number": text("serial_number"),
                "likely": likely,
                "espressif": espressif,
                "selected": path == chosen,
                "active": connected and path == self.active_port,
            })
        out.sort(key=lambda d: (not d["likely"], d["path"]))
        return out

    def status(self) -> dict:
        with self._ctl:
            mode, chosen, user_down = self._mode, self._chosen_port, self._user_disconnected
            connecting, radio = self._connecting, dict(self._radio)
        connected = self.is_connected()
        return {
            "mode": mode,
            "chosen_port": chosen,
            "active_port": self.active_port,
            "connected": connected,
            "connecting": connecting and not connected,
            "disconnected_by_user": user_down,
            "last_error": self.last_error,
            "next_retry_at": self.next_retry_at,
            "transport": "serial" if mode == "manual" else self.config.transport,
            "node_id": radio.get("node_id"),
            "hw_model": radio.get("hw_model"),
            "firmware": radio.get("firmware"),
        }

    # pubsub listeners. They run on the library thread. They must not block.
    def _accept(self, packet: dict, interface: Any) -> bool:
        iface = self._iface
        if iface is None or interface is not iface:
            return False
        if not isinstance(packet, dict):
            return False
        if packet.get("from") == self._local_num:
            return False
        return True

    def _rx_text(self, packet, interface):
        if self._accept(packet, interface):
            self._inbound.put(("text", packet))

    def _rx_position(self, packet, interface):
        if self._accept(packet, interface):
            self._inbound.put(("position", packet))

    def _rx_user(self, packet, interface):
        if self._accept(packet, interface):
            self._inbound.put(("nodeinfo", packet))

    def _conn_lost(self, interface):
        if interface is self._iface:
            log.warning("Radio connection lost event.")
            self._lost.set()

    def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                kind, packet = self._inbound.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._handlers[kind](packet)
            except Exception:
                log.exception("Handler for %s packet failed.", kind)

    # state
    def is_connected(self) -> bool:
        iface = self._iface
        return iface is not None and self._local_num is not None and iface.isConnected.is_set()

    @property
    def local_node_num(self) -> int | None:
        return self._local_num

    @property
    def local_node_id(self) -> str | None:
        return None if self._local_num is None else node_id_from_num(self._local_num)

    def known_nodes(self) -> dict[int, dict]:
        iface = self._iface
        if iface is None:
            return {}
        for _ in range(3):
            try:
                with self._state_lock:
                    return {n: copy.deepcopy(v) for n, v in dict(iface.nodesByNum or {}).items()}
            except RuntimeError:
                continue
        return {}

    def send_text(self, dest_num: int, text: str, on_ack: Callable[[dict], None]) -> int:
        from meshtastic.protobuf import portnums_pb2

        data = text.encode("utf-8")
        with self._send_lock:
            if not self.is_connected():
                raise NotConnected("Radio is not connected.")
            packet = self._iface.sendData(
                data,
                destinationId=dest_num,
                portNum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
                wantAck=True,
                onResponse=on_ack,
                onResponseAckPermitted=True,
                channelIndex=self.tx_config.channel_index,
            )
        return packet.id

    # supervisor
    def start(self) -> None:
        for target, name in ((self._dispatch_loop, "mesh-dispatch"), (self._supervise, "mesh-supervisor")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        iface, self._iface = self._iface, None
        if iface is not None:
            self._close(iface)
        for t in self._threads:
            t.join(timeout=5)

    def _snapshot(self) -> SimpleNamespace:
        with self._ctl:
            self._wake.clear()
            return SimpleNamespace(
                gen=self._gen,
                manual=self._mode == "manual",
                path=self._chosen_port,
                down=self._user_disconnected,
                serial=self._mode == "manual" or self.config.transport == "serial",
            )

    def _connect(self, snap):
        result: dict[str, Any] = {}
        serial = snap.serial
        guard = threading.Lock()
        abandoned = threading.Event()

        def target():
            try:
                if serial:
                    # The path can change after a replug, so detect it on every attempt.
                    comports = self._port_lister()
                    path = snap.path if snap.manual else select_serial_port(self.config, comports)
                    self.active_port = path
                    log.info("Opening serial port %s.", path)
                    ff_fix = ff_fix_enabled(self.config, path, comports)
                    if ff_fix:
                        log.info("Serial 0xFF fix is on (WCH driver doubles 0xFF bytes).")
                    iface = self._serial_factory(path, ff_fix=ff_fix)
                else:
                    iface = self._factory(self.config.host, self.config.port)
            except Exception as exc:
                result["error"] = exc
                return
            with guard:
                result["iface"] = iface
                drop = abandoned.is_set() or self._stop.is_set()
            if drop:
                self._close(iface)

        worker = threading.Thread(target=target, name="mesh-connect", daemon=True)
        worker.start()
        while worker.is_alive():
            if self._stop.is_set() or self._gen != snap.gen:
                with guard:
                    abandoned.set()
                    iface = result.pop("iface", None)
                if iface is not None:
                    self._close(iface)
                return None
            worker.join(0.2)
        if "error" in result:
            self.last_error = str(result["error"])
            exc = result["error"]
            if serial and is_port_busy(exc):
                log.warning(
                    "Serial port is busy. Close other programs that use it "
                    "(web flasher, Meshtastic web client). %s", exc,
                )
            elif serial:
                log.warning("Cannot open serial port: %s", exc)
            else:
                log.warning("Cannot connect to %s:%s: %s", self.config.host, self.config.port, exc)
            return None
        return result.get("iface")

    @staticmethod
    def _close(iface) -> None:
        # close() can block when the cable is pulled, so it runs in a helper thread.
        t = threading.Thread(target=_safe_close, args=(iface,), daemon=True, name="mesh-close")
        t.start()
        t.join(10)

    def _supervise(self) -> None:
        backoff = INITIAL_BACKOFF_S
        while not self._stop.is_set():
            snap = self._snapshot()
            if snap.down:
                self._connecting = False
                self.active_port = None
                log.info("Radio is disconnected by the user. Waiting for connect.")
                self._wake.wait()
                backoff = INITIAL_BACKOFF_S
                continue
            started = None
            self._connecting = True
            iface = self._connect(snap)
            if iface is not None:
                started = self._now()
                try:
                    self._serve(iface, snap)
                except Reconfigured:
                    log.info("Radio connection settings changed.")
                except WrongDevice as exc:
                    self.last_error = str(exc)
                    log.error("%s", exc)
                except Exception as exc:
                    self.last_error = str(exc)
                    log.exception("Radio connection failed.")
                finally:
                    self._iface = None
                    self._local_num = None
                    self.connected_since = None
                    self._radio = {}
                    self._close(iface)
                    self.reconnects += 1
            self._connecting = False
            if self._gen != snap.gen:
                backoff = INITIAL_BACKOFF_S
                continue
            if started is not None and self._now() - started >= STABLE_AFTER_S:
                backoff = INITIAL_BACKOFF_S
            if self._stop.is_set():
                break
            log.info("Retry radio connection in %d s.", backoff)
            self.next_retry_at = time.time() + backoff
            self._wake.wait(backoff)
            self.next_retry_at = None
            if self._gen != snap.gen:
                backoff = INITIAL_BACKOFF_S
            else:
                backoff = min(backoff * 2, self.config.reconnect_max_backoff_s)

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    def _take_allow_any(self, gen: int) -> bool:
        with self._ctl:
            if self._gen != gen or not self._allow_any:
                return False
            self._allow_any = False
            return True

    def _serve(self, iface, snap) -> None:
        my_info = getattr(iface, "myInfo", None)
        if my_info is None:
            raise RuntimeError("Radio gave no node info.")
        num = int(my_info.my_node_num)
        found = node_id_from_num(num)
        expected = self.config.expected_node_id.strip().lower()
        if expected and found != expected:
            if self._take_allow_any(snap.gen):
                log.warning(
                    "Accept any node is on. Using node %s although %s is expected. "
                    "This applies to this connection only.", found, expected,
                )
            else:
                raise WrongDevice(
                    f"Wrong radio: found node {found}, expected {expected}. "
                    "Pick another port, or tick Accept any node."
                )
        elif self._gen == snap.gen:
            self._take_allow_any(snap.gen)
        self._lost.clear()
        self._local_num = num
        self._iface = iface
        self.connected_since = self._now()
        self.last_error = None
        if not snap.serial:
            enable_keepalive(getattr(iface, "socket", None))
        hw, fw = _describe(iface, num)
        self._radio = {"node_id": found, "hw_model": hw, "firmware": fw}
        self._connecting = False
        log.info("Connected to radio %s (hwModel %s, firmware %s).", self.local_node_id, hw, fw)
        self._on_connected(iface)

        next_beat = self._now() + self.config.heartbeat_interval_s
        while not self._stop.is_set():
            if self._wake.wait(1):
                if self._gen != snap.gen:
                    raise Reconfigured()
                if self._stop.is_set():
                    break
                self._wake.clear()
            if self._lost.is_set():
                raise RuntimeError("Connection lost.")
            if not iface.isConnected.is_set():
                raise RuntimeError("Interface is not connected.")
            # _rxThread is private. No public call tells that the reader thread died.
            reader = getattr(iface, "_rxThread", None)
            if reader is not None and not reader.is_alive():
                raise RuntimeError("Reader thread stopped.")
            if self._now() >= next_beat:
                with self._send_lock:
                    iface.sendHeartbeat()
                next_beat = self._now() + self.config.heartbeat_interval_s


def _describe(iface, num: int) -> tuple[str, str]:
    hw = fw = "unknown"
    try:
        user = (getattr(iface, "nodesByNum", None) or {}).get(num, {}).get("user", {})
        hw = user.get("hwModel") or hw
        meta = getattr(iface, "metadata", None)
        if meta is not None:
            fw = getattr(meta, "firmware_version", None) or fw
            if hw == "unknown" and getattr(meta, "hw_model", None):
                from meshtastic.protobuf import mesh_pb2

                hw = mesh_pb2.HardwareModel.Name(meta.hw_model)
    except Exception as exc:
        log.debug("Cannot read radio details: %s", exc)
    return str(hw), str(fw)


def _safe_close(iface) -> None:
    try:
        iface.close()
    except Exception as exc:
        log.debug("Error on close (ignored): %s", exc)
