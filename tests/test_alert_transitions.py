import requests

from conftest import FakeResponse, Env, all_quiet, sample_response
from meshalerts.alerts import MAX_BACKOFF_S, Transition, diff_states, parse_response, PollError

import pytest

LVIV = "Львівська область"


def collect(env):
    seen = []
    env.poller.on_transition = seen.append
    return seen


def with_lviv(active):
    data = all_quiet()
    data["states"][LVIV]["alertnow"] = active
    return data


def test_diff_states_pure():
    old = {"a": False, "b": True, "c": False}
    new = {"a": True, "b": True, "c": False, "d": True}
    assert diff_states(old, new, 5) == [Transition("a", True, 5)]


def test_baseline_has_no_events(env):
    seen = collect(env)
    assert env.poll(sample_response())
    assert seen == []
    assert len(env.db.get_alert_states()) == 26
    assert env.poller.current_state("Київська область") is True


def test_start_and_end_events(env):
    seen = collect(env)
    env.poll(with_lviv(False))
    env.poll(with_lviv(True))
    env.poll(with_lviv(True))
    env.poll(with_lviv(False))
    assert [(t.oblast, t.active) for t in seen] == [(LVIV, True), (LVIV, False)]
    assert env.db.get_alert_states()[LVIV]["active"] == 0


def test_api_error_keeps_state_and_no_events(env):
    seen = collect(env)
    env.poll(with_lviv(True))
    env.session.queue.append(FakeResponse(status=500))
    assert env.poller.poll_once() is False
    env.session.queue.append(requests.ConnectTimeout("slow"))
    assert env.poller.poll_once() is False
    assert env.poller.current_state(LVIV) is True
    assert seen == []
    assert "slow" in env.poller.last_error


def test_malformed_json_keeps_state(env):
    seen = collect(env)
    env.poll(with_lviv(True))
    env.session.queue.append(FakeResponse(text="<html>"))
    assert env.poller.poll_once() is False
    assert env.poller.current_state(LVIV) is True
    assert seen == []


@pytest.mark.parametrize("bad", [[], {"states": []}, {"states": {"x": {"alertnow": "yes"}}}, {}])
def test_schema_error_is_failed_poll(env, bad):
    assert env.poll(bad) is False


def test_too_few_regions_is_failed_poll(env):
    seen = collect(env)
    env.poll(with_lviv(False))
    small = {"states": {k: v for k, v in list(with_lviv(True)["states"].items())[:10]}}
    assert env.poll(small) is False
    assert env.poller.current_state(LVIV) is False
    assert seen == []


def test_region_missing_keeps_last_state(env):
    seen = collect(env)
    env.poll(with_lviv(True))
    data = with_lviv(True)
    del data["states"][LVIV]
    assert env.poll(data) is True
    assert env.poller.current_state(LVIV) is True
    assert seen == []


def test_rate_limit_backoff_and_reset(env):
    base = env.poller.delay
    for expected in (base * 2, base * 4, base * 8):
        env.session.queue.append(FakeResponse(status=429))
        assert env.poller.poll_once() is False
        assert env.poller.delay == expected
    for _ in range(10):
        env.session.queue.append(FakeResponse(status=429))
        env.poller.poll_once()
    assert env.poller.delay == MAX_BACKOFF_S
    env.poll(all_quiet())
    assert env.poller.delay == base


def test_poll_interval_never_below_five():
    env = Env()
    env.config.alerts.poll_interval_s = 1
    env.build()
    assert env.poller.base_interval == 5


def test_restart_state_from_db_gives_one_transition(tmp_path):
    env = Env(db_path=str(tmp_path / "a.db"))
    env.poll(with_lviv(False))
    env.restart()
    seen = collect(env)
    env.poll(with_lviv(True))
    env.poll(with_lviv(True))
    assert [(t.oblast, t.active) for t in seen] == [(LVIV, True)]


def test_restart_without_change_gives_none(tmp_path):
    env = Env(db_path=str(tmp_path / "a.db"))
    env.poll(with_lviv(True))
    env.restart()
    seen = collect(env)
    env.poll(with_lviv(True))
    assert seen == []


def test_parse_response_counts_valid_only():
    data = sample_response()
    data["states"]["junk"] = {"alertnow": "maybe"}
    assert len(parse_response(data, 20)) == 26
    with pytest.raises(PollError):
        parse_response(data, 27)


def test_freshness(env):
    assert not env.poller.is_fresh()
    env.poll(all_quiet())
    assert env.poller.is_fresh()
    env.clock.advance(env.config.alerts.stale_after_s + 1)
    assert not env.poller.is_fresh()


def test_user_agent_is_set(env):
    assert env.session.headers["User-Agent"] == "meshalerts/1.0"
