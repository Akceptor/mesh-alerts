"""Ubilling poller and transition detection."""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from typing import Any, Callable

import requests

from .db import Database

log = logging.getLogger(__name__)

MIN_POLL_INTERVAL_S = 5
MAX_BACKOFF_S = 120
USER_AGENT = "meshalerts/1.0"
KYIV_TZ = ZoneInfo("Europe/Kyiv")
MAX_FUTURE_SKEW_S = 300


@dataclass(frozen=True)
class Transition:
    oblast: str
    active: bool
    at: int


class PollError(Exception):
    """A poll failed. Keep the last valid state."""


def diff_states(old: dict[str, bool], new: dict[str, bool], at: int = 0) -> list[Transition]:
    """Return changes for regions present in both. New regions are a baseline."""
    return [
        Transition(name, active, at)
        for name, active in new.items()
        if name in old and old[name] != active
    ]


def parse_response(data: Any, min_regions: int) -> dict[str, bool]:
    if not isinstance(data, dict) or not isinstance(data.get("states"), dict):
        raise PollError("Response has no states object.")
    valid = {
        name: v["alertnow"]
        for name, v in data["states"].items()
        if isinstance(name, str) and isinstance(v, dict) and isinstance(v.get("alertnow"), bool)
    }
    if len(valid) < min_regions:
        raise PollError(f"Response has {len(valid)} valid regions. Need {min_regions}.")
    return valid


def parse_since(changed: Any, now: int) -> int | None:
    """Epoch seconds for an API `changed` value (Kyiv local time), or None if unusable."""
    if not isinstance(changed, str):
        return None
    try:
        local = datetime.strptime(changed.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    # fold=0 picks the first occurrence of the repeated hour at the autumn DST change.
    ts = int(local.replace(tzinfo=KYIV_TZ).timestamp())
    if ts > now + MAX_FUTURE_SKEW_S:
        return None
    return ts


class AlertPoller:
    def __init__(
        self,
        config,
        db: Database,
        on_transition: Callable[[Transition], None],
        clock: Callable[[], float],
        session: requests.Session | None = None,
        on_first_valid_poll: Callable[[], None] | None = None,
    ):
        self.config = config
        self.db = db
        self.on_transition = on_transition
        self.on_first_valid_poll = on_first_valid_poll
        self.clock = clock
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.base_interval = max(MIN_POLL_INTERVAL_S, config.poll_interval_s)
        self.delay = self.base_interval
        self.last_success_at: float | None = None
        self.last_error: str | None = None
        self.source: str | None = None
        self._states: dict[str, bool] = {
            name: bool(row["active"]) for name, row in db.get_alert_states().items()
        }
        self._changed: dict[str, Any] = {}
        self._first_done = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def current_state(self, oblast: str) -> bool | None:
        with self._lock:
            return self._states.get(oblast)

    def all_states(self) -> dict[str, bool]:
        with self._lock:
            return dict(self._states)

    def is_fresh(self) -> bool:
        return (
            self.last_success_at is not None
            and self.clock() - self.last_success_at < self.config.stale_after_s
        )

    def data_age_min(self) -> int | None:
        if self.last_success_at is None:
            return None
        return int((self.clock() - self.last_success_at) // 60)

    def _fetch(self) -> dict[str, bool]:
        timeout = (5, self.config.request_timeout_s)
        try:
            resp = self.session.get(self.config.url, timeout=timeout)
        except requests.RequestException as exc:
            raise PollError(f"Request failed: {exc}") from exc
        if resp.status_code == 429:
            self.delay = min(self.delay * 2, MAX_BACKOFF_S)
            raise PollError("HTTP 429: rate limit. Back off.")
        if resp.status_code != 200:
            raise PollError(f"HTTP {resp.status_code}.")
        try:
            data = resp.json()
        except ValueError as exc:
            raise PollError(f"Response is not valid JSON: {exc}") from exc
        states = parse_response(data, self.config.min_regions)
        self._changed = {
            name: v.get("changed") for name, v in data["states"].items() if name in states
        }
        self.source = data.get("source") if isinstance(data.get("source"), str) else None
        return states

    def poll_once(self) -> bool:
        try:
            new = self._fetch()
        except PollError as exc:
            self.last_error = str(exc)
            log.warning("Alert poll failed: %s Keep last state.", exc)
            return False
        self.delay = self.base_interval
        self.last_error = None
        now = int(self.clock())
        self.last_success_at = self.clock()

        with self._lock:
            old = dict(self._states)
        transitions = diff_states(old, new, now)
        stored = self.db.get_alert_states()
        since = {
            name: parse_since(self._changed.get(name), now) for name, active in new.items() if active
        }
        for name, active in new.items():
            if name not in old:
                log.info("Alert baseline: %s = %s.", name, active)
                self.db.set_alert_state(name, active, now, since.get(name) or now)
            elif active and old[name]:
                row = stored.get(name) or {}
                fixed = since[name] or row.get("since") or row.get("updated_at") or now
                if fixed != row.get("since"):
                    self.db.set_alert_since(name, fixed)
        for t in transitions:
            self.db.set_alert_state(t.oblast, t.active, now, since.get(t.oblast) or now)
            log.info("Alert transition: %s -> %s.", t.oblast, "ACTIVE" if t.active else "ended")
        with self._lock:
            self._states.update(new)
        for t in transitions:
            try:
                self.on_transition(t)
            except Exception:
                log.exception("Transition handler failed for %s.", t.oblast)
        if not self._first_done:
            self._first_done = True
            if self.on_first_valid_poll is not None:
                try:
                    self.on_first_valid_poll()
                except Exception:
                    log.exception("First-poll handler failed.")
        return True

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="alert-poller", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                log.exception("Unexpected error in alert poller.")
            self._stop.wait(self.delay)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=15)
