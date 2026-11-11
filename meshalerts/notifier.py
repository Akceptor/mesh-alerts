"""Outgoing queue, rate limit, ACK tracking, and duplicate prevention."""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from . import messages
from .db import Database
from .mesh import NotConnected

log = logging.getLogger(__name__)

PRIO_HIGH = 0
PRIO_ALERT_END = 1
PRIO_MANUAL = 1
PRIO_REGION = 2

STATE_KINDS = ("alert", "region")


@dataclass
class Item:
    notif_id: int
    node_id: str
    node_num: int
    text: str
    kind: str
    priority: int
    oblast: str | None = None
    alert_state: bool | None = None
    attempt: int = 0
    retry: bool = False
    seq: int = 0


@dataclass
class Pending:
    item: Item
    deadline: float


def classify_ack(packet: dict, dest_num: int, local_num: int | None) -> str:
    routing = (packet.get("decoded") or {}).get("routing") or {}
    reason = routing.get("errorReason", "NONE")
    if reason not in (None, "NONE"):
        return f"failed:{reason}"
    sender = packet.get("from")
    if sender == dest_num:
        return "delivered"
    if local_num is not None and sender == local_num:
        return "implicit_ack"
    return "failed:unexpected_source"


class Notifier:
    def __init__(
        self,
        db: Database,
        mesh,
        config,
        alerts_config,
        clock: Callable[[], float],
        state_of: Callable[[str], bool | None],
        oblasts=None,
    ):
        self.db = db
        self.mesh = mesh
        self.tx = config
        self.alerts_config = alerts_config
        self.clock = clock
        self.state_of = state_of
        self.oblasts = oblasts
        self._heap: list[tuple[int, int, Item]] = []
        self._seq = itertools.count()
        self._cv = threading.Condition()
        self._pending: dict[int, Pending] = {}
        self._retries: list[tuple[float, Item]] = []
        self._marked: set[int] = set()
        self._early: dict[int, dict] = {}
        self._last_send = float("-inf")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # queue
    def queue_length(self) -> int:
        with self._cv:
            return len(self._heap)

    def enqueue(
        self,
        node_id: str,
        text: str,
        kind: str,
        priority: int | None = None,
        oblast: str | None = None,
        alert_state: bool | None = None,
        attempt: int = 0,
        retry: bool = False,
    ) -> int:
        node = self.db.get_node(node_id)
        if node is None:
            raise KeyError(node_id)
        if priority is None:
            priority = PRIO_HIGH if kind == "reply" else PRIO_MANUAL
        text = messages.fit(text)
        notif_id = self.db.insert_notification(
            node_id, kind, text, int(self.clock()), oblast, alert_state
        )
        item = Item(
            notif_id, node_id, node["node_num"], text, kind, priority,
            oblast, alert_state, attempt, retry,
        )
        with self._cv:
            if kind in STATE_KINDS:
                self._supersede(item)
            item.seq = next(self._seq)
            heapq.heappush(self._heap, (item.priority, item.seq, item))
            self._cv.notify()
        log.info("Queued %s message %d for %s: %s", kind, notif_id, node_id, text)
        return notif_id

    def _supersede(self, new: Item) -> None:
        keep = []
        for entry in self._heap:
            old = entry[2]
            replace = (
                old.node_id == new.node_id
                and old.kind in STATE_KINDS
                and (new.kind == "region" or (old.kind == "alert" and old.oblast == new.oblast))
            )
            if replace:
                self.db.set_status(old.notif_id, "superseded")
                log.info("Message %d for %s superseded by newer message.", old.notif_id, old.node_id)
            else:
                keep.append(entry)
        if len(keep) != len(self._heap):
            self._heap = keep
            heapq.heapify(self._heap)

    def _has_queued_state(self, node_id: str) -> bool:
        with self._cv:
            return any(e[2].node_id == node_id and e[2].kind in STATE_KINDS for e in self._heap)

    # region logic
    def _regions(self, node: dict) -> list[str]:
        regions = [node["oblast"]] if node["oblast"] else []
        if (
            node.get("region_mode") != "fixed"
            and self.alerts_config.border_policy == "any_nearby"
            and self.oblasts is not None
            and node["latitude"] is not None
        ):
            res = self.oblasts.resolve(node["latitude"], node["longitude"], node["oblast"])
            regions += [c for c in res.candidates if c not in regions]
        return regions

    def effective_state(self, node: dict) -> bool | None:
        states = [self.state_of(r) for r in self._regions(node)]
        known = [s for s in states if s is not None]
        if not known:
            return None
        return any(known)

    def _dedup_equal(self, node: dict, oblast: str | None, state: bool) -> bool:
        return (
            node["last_notified_oblast"] == oblast
            and node["last_notified_state"] is not None
            and bool(node["last_notified_state"]) == state
        )

    def set_dedup(self, node_id: str, oblast: str | None, state: bool | None) -> None:
        self.db.update_node(
            node_id,
            last_notified_oblast=oblast,
            last_notified_state=None if state is None else int(state),
        )

    def on_transition(self, t) -> None:
        for node in self.db.subscribed_nodes():
            if not node["oblast"] or t.oblast not in self._regions(node):
                continue
            state = self.effective_state(node)
            if state is None:
                continue
            if self._dedup_equal(node, node["oblast"], state) and not self._has_queued_state(node["node_id"]):
                log.info("Skip duplicate %s for %s.", t.oblast, node["node_id"])
                continue
            self._enqueue_state(node, t.oblast, state)

    def _enqueue_state(self, node: dict, text_oblast: str, state: bool) -> None:
        text = messages.alert_start(text_oblast) if state else messages.alert_end(text_oblast)
        self.enqueue(
            node["node_id"], text, "alert",
            priority=PRIO_HIGH if state else PRIO_ALERT_END,
            oblast=node["oblast"], alert_state=state,
        )

    def on_region_change(self, node: dict, old: str | None, new: str | None) -> None:
        if not node["subscribed"] or new is None:
            return
        state = self.effective_state(node)
        if state is None:
            return
        node_id = node["node_id"]
        if state:
            self.enqueue(node_id, messages.region_in_alert(new), "region",
                         priority=PRIO_REGION, oblast=new, alert_state=True)
            return
        was_active = bool(node["last_notified_state"]) and node["last_notified_oblast"] == old
        if was_active:
            self.enqueue(node_id, messages.region_left_alert(new), "region",
                         priority=PRIO_REGION, oblast=new, alert_state=False)
        elif self.alerts_config.notify_region_change_quiet:
            self.enqueue(node_id, messages.region_new_quiet(new), "region",
                         priority=PRIO_REGION, oblast=new, alert_state=False)
        else:
            log.info("Region of %s changed to %s. No alert. No message.", node_id, new)
            self.set_dedup(node_id, new, False)

    def reconcile(self) -> None:
        for node in self.db.subscribed_nodes():
            if not node["oblast"]:
                continue
            state = self.effective_state(node)
            if state is None:
                continue
            if node["last_notified_state"] is None:
                log.info("Baseline for %s: %s.", node["node_id"], state)
                self.set_dedup(node["node_id"], node["oblast"], state)
            elif node["last_notified_oblast"] != node["oblast"]:
                self.on_region_change(node, node["last_notified_oblast"], node["oblast"])
            elif not self._dedup_equal(node, node["oblast"], state):
                log.info("Reconcile %s: state changed while service was off.", node["node_id"])
                self._enqueue_state(node, node["oblast"], state)

    # sending
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="notifier", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        if self._thread:
            self._thread.join(timeout=10)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.step()
            except Exception:
                log.exception("Unexpected error in notifier.")
                worked = False
            if not worked:
                with self._cv:
                    self._cv.wait(0.5)

    def step(self) -> bool:
        """Do one unit of work. Return True if a message left the queue."""
        self._release_retries()
        self.check_timeouts()
        with self._cv:
            if not self._heap:
                return False
            if self.clock() - self._last_send < self.tx.min_interval_s:
                return False
            if not self.mesh.is_connected():
                return False
            _, _, item = heapq.heappop(self._heap)
        return self._send(item)

    def _requeue(self, item: Item) -> None:
        with self._cv:
            heapq.heappush(self._heap, (item.priority, item.seq, item))

    def _send(self, item: Item) -> bool:
        node = self.db.get_node(item.node_id)
        if node is None or (item.kind in STATE_KINDS and not node["subscribed"]):
            self.db.set_status(item.notif_id, "dropped")
            log.info("Drop message %d: node is not subscribed.", item.notif_id)
            return True
        if (
            item.kind in STATE_KINDS
            and not item.retry
            and item.alert_state is not None
            and self._dedup_equal(node, item.oblast, item.alert_state)
        ):
            self.db.set_status(item.notif_id, "dropped")
            log.info("Drop message %d for %s: duplicate state.", item.notif_id, item.node_id)
            return True
        try:
            packet_id = self.mesh.send_text(
                item.node_num, item.text, lambda pkt, i=item: self._on_ack(i, pkt)
            )
        except NotConnected:
            self._requeue(item)
            return False
        except Exception as exc:
            log.exception("Send failed for message %d.", item.notif_id)
            self.db.set_status(item.notif_id, f"failed:{type(exc).__name__}")
            self._schedule_retry(item)
            return True

        now = self.clock()
        self._last_send = now
        self.db.mark_sent(item.notif_id, packet_id, int(now))
        if item.kind in STATE_KINDS and item.alert_state is not None:
            self.set_dedup(item.node_id, item.oblast, item.alert_state)
        log.info("Sent message %d to %s (packet %s).", item.notif_id, item.node_id, packet_id)
        with self._cv:
            self._pending[item.notif_id] = Pending(item, now + self.tx.ack_timeout_s)
            self._marked.add(item.notif_id)
            early = self._early.pop(item.notif_id, None)
        if early is not None:
            self._on_ack(item, early)
        return True

    # ACK handling. The library thread calls _on_ack. It only updates the database.
    def _on_ack(self, item: Item, packet: dict) -> None:
        with self._cv:
            if item.notif_id not in self._marked:
                self._early[item.notif_id] = packet
                return
            self._pending.pop(item.notif_id, None)
        status = classify_ack(packet, item.node_num, self.mesh.local_node_num)
        changed = self.db.set_ack_status(item.notif_id, status)
        log.info("ACK for message %d (%s): %s.", item.notif_id, item.node_id, status)
        if changed and status.startswith("failed:"):
            self._schedule_retry(item)

    def check_timeouts(self) -> None:
        now = self.clock()
        with self._cv:
            expired = [p for p in self._pending.values() if p.deadline <= now]
            for p in expired:
                self._pending.pop(p.item.notif_id, None)
        for p in expired:
            if self.db.set_ack_status(p.item.notif_id, "no_ack"):
                log.warning("No ACK for message %d (%s).", p.item.notif_id, p.item.node_id)
                self._schedule_retry(p.item)

    def _schedule_retry(self, item: Item) -> None:
        if item.kind not in STATE_KINDS or item.alert_state is None:
            return
        if item.attempt >= self.tx.max_retries:
            log.warning("Give up on message %d for %s after %d retries.", item.notif_id, item.node_id, item.attempt)
            return
        with self._cv:
            self._retries.append((self.clock() + self.tx.retry_delay_s, item))

    def _still_current(self, item: Item) -> bool:
        node = self.db.get_node(item.node_id)
        if node is None or not node["subscribed"]:
            return False
        return self._dedup_equal(node, item.oblast, bool(item.alert_state)) and (
            self.effective_state(node) == item.alert_state
        )

    def _release_retries(self) -> None:
        now = self.clock()
        with self._cv:
            due = [r for r in self._retries if r[0] <= now]
            self._retries = [r for r in self._retries if r[0] > now]
        for _, item in due:
            if not self._still_current(item):
                log.info("Drop retry of message %d: state changed.", item.notif_id)
                continue
            log.info("Retry message %d (attempt %d).", item.notif_id, item.attempt + 1)
            self.enqueue(
                item.node_id, item.text, item.kind, item.priority,
                item.oblast, item.alert_state, item.attempt + 1, retry=True,
            )
