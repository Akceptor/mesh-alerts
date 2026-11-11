import pytest

from conftest import LOCAL, LVIV, NODE_A, NODE_ID, Env, enroll, lviv
from meshalerts.messages import alert_start, truncate_utf8
from meshalerts.notifier import classify_ack


def row(env, notif_id=None):
    return env.db.list_notifications(1)[0] if notif_id is None else env.db.get_notification(notif_id)


def alert_one(env):
    enroll(env)
    env.poll(lviv(True))
    env.drain()
    assert len(env.mesh.sent) == 1


def test_classify_ack():
    assert classify_ack({"from": NODE_A, "decoded": {"routing": {}}}, NODE_A, LOCAL) == "delivered"
    assert classify_ack({"from": NODE_A, "decoded": {"routing": {"errorReason": "NONE"}}}, NODE_A, LOCAL) == "delivered"
    assert classify_ack({"from": LOCAL, "decoded": {"routing": {"errorReason": "NONE"}}}, NODE_A, LOCAL) == "implicit_ack"
    assert classify_ack({"from": LOCAL, "decoded": {"routing": {"errorReason": "MAX_RETRANSMIT"}}}, NODE_A, LOCAL) == "failed:MAX_RETRANSMIT"
    assert classify_ack({"from": NODE_A, "decoded": {"routing": {"errorReason": "NO_ROUTE"}}}, NODE_A, LOCAL) == "failed:NO_ROUTE"


def test_sent_status_then_delivered(env):
    alert_one(env)
    assert row(env)["acknowledgement_status"] == "sent"
    assert row(env)["packet_id"] == env.mesh.sent[0]["id"]
    env.ack(0, NODE_A)
    assert row(env)["acknowledgement_status"] == "delivered"


def test_implicit_ack(env):
    alert_one(env)
    env.ack(0, LOCAL, "NONE")
    assert row(env)["acknowledgement_status"] == "implicit_ack"


def test_delivered_overrides_implicit_but_not_reverse(env):
    alert_one(env)
    env.ack(0, LOCAL, "NONE")
    env.ack(0, NODE_A)
    assert row(env)["acknowledgement_status"] == "delivered"
    env.ack(0, LOCAL, "NONE")
    assert row(env)["acknowledgement_status"] == "delivered"


def test_failed_ack_and_retry(env):
    env.config.tx.retry_delay_s = 60
    alert_one(env)
    env.ack(0, NODE_A, "MAX_RETRANSMIT")
    assert row(env, 2)["acknowledgement_status"] == "failed:MAX_RETRANSMIT"
    env.drain()
    assert len(env.mesh.sent) == 1
    env.clock.advance(61)
    env.drain()
    assert len(env.mesh.sent) == 2
    assert env.mesh.sent[1]["text"] == alert_start(LVIV)
    env.ack(1, NODE_A, "NO_ROUTE")
    env.clock.advance(61)
    env.drain()
    assert len(env.mesh.sent) == 3
    env.ack(2, NODE_A, "NO_ROUTE")
    env.clock.advance(61)
    env.drain()
    assert len(env.mesh.sent) == 3


def test_retry_dropped_when_state_changed(env):
    alert_one(env)
    env.ack(0, NODE_A, "MAX_RETRANSMIT")
    env.poll(lviv(False))
    env.clock.advance(61)
    env.drain()
    texts = env.mesh.texts()
    assert alert_start(LVIV) not in texts[1:]


def test_no_ack_timeout_marks_and_retries(env):
    alert_one(env)
    env.clock.advance(env.config.tx.ack_timeout_s + 1)
    env.drain()
    assert row(env, 2)["acknowledgement_status"] == "no_ack"
    env.clock.advance(61)
    env.drain()
    assert len(env.mesh.sent) == 2


def test_late_ack_after_timeout_still_recorded(env):
    alert_one(env)
    env.clock.advance(env.config.tx.ack_timeout_s + 1)
    env.drain()
    env.ack(0, NODE_A)
    assert row(env, 2)["acknowledgement_status"] == "delivered"


def test_ack_before_mark_sent_is_not_lost(env):
    enroll(env)
    original = env.mesh.send_text

    def fast_ack(dest, text, cb):
        pid = original(dest, text, cb)
        cb({"from": dest, "decoded": {"routing": {}}})
        return pid

    env.mesh.send_text = fast_ack
    env.poll(lviv(True))
    env.drain()
    assert row(env)["acknowledgement_status"] == "delivered"


def test_one_failed_send_does_not_block_next(env):
    enroll(env)
    env.notifier.enqueue(NODE_ID, "first", "manual")
    env.notifier.enqueue(NODE_ID, "second", "manual")
    env.mesh.fail_next = RuntimeError("boom")
    env.drain()
    assert env.mesh.texts() == ["second"]
    statuses = {n["text"]: n["acknowledgement_status"] for n in env.db.list_notifications()}
    assert statuses["first"] == "failed:RuntimeError"
    assert statuses["second"] == "sent"


def test_queue_waits_while_disconnected(env):
    enroll(env)
    env.mesh.connected = False
    env.notifier.enqueue(NODE_ID, "hello", "manual")
    env.drain()
    assert env.mesh.sent == []
    assert env.notifier.queue_length() == 1
    env.mesh.connected = True
    env.drain()
    assert env.mesh.texts() == ["hello"]
    assert env.notifier.queue_length() == 0


def test_not_connected_error_requeues_item(env):
    from meshalerts.mesh import NotConnected

    enroll(env)
    env.notifier.enqueue(NODE_ID, "hello", "manual")
    env.mesh.fail_next = NotConnected("race")
    env.drain()
    assert env.notifier.queue_length() == 1
    env.drain()
    assert env.mesh.texts() == ["hello"]


def test_rate_limit_between_sends():
    env = Env()
    env.config.tx.min_interval_s = 3
    enroll(env)
    env.notifier.enqueue(NODE_ID, "a", "manual")
    env.notifier.enqueue(NODE_ID, "b", "manual")
    env.drain()
    assert env.mesh.texts() == ["a"]
    env.clock.advance(3)
    env.drain()
    assert env.mesh.texts() == ["a", "b"]


def test_priority_order(env):
    enroll(env)
    env.notifier.enqueue(NODE_ID, "manual", "manual")
    env.notifier.enqueue(NODE_ID, "reply", "reply")
    env.drain()
    assert env.mesh.texts() == ["reply", "manual"]


def test_text_over_limit_is_truncated_on_utf8_boundary(env):
    enroll(env)
    env.notifier.enqueue(NODE_ID, "ї" * 150, "manual")
    env.drain()
    assert len(env.mesh.sent[0]["text"].encode()) <= 200
    assert truncate_utf8("ї" * 150, 199) == "ї" * 99


def test_manual_message_is_not_retried(env):
    enroll(env)
    env.notifier.enqueue(NODE_ID, "hi", "manual")
    env.drain()
    env.ack(0, NODE_A, "NO_ROUTE")
    env.clock.advance(120)
    env.drain()
    assert len(env.mesh.sent) == 1


def test_alert_text_fits_limit_for_longest_oblast(env):
    assert len(alert_start("Івано-Франківська область").encode()) <= 200
