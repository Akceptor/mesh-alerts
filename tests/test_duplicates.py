from conftest import LVIV, NODE_ID, TERNOPIL, Env, all_quiet, enroll, lviv


def test_same_state_twice_sends_one_message(env):
    enroll(env)
    env.poll(lviv(True))
    env.drain()
    env.notifier.on_transition(type("T", (), {"oblast": LVIV, "active": True})())
    env.drain()
    assert env.mesh.texts() == [f"ПОВІТРЯНА ТРИВОГА! {LVIV}"]


def test_end_message_text(env):
    enroll(env)
    env.poll(lviv(True))
    env.drain()
    env.poll(lviv(False))
    env.drain()
    assert env.mesh.texts() == [f"ПОВІТРЯНА ТРИВОГА! {LVIV}", f"ВІДБІЙ ТРИВОГИ. {LVIV}"]


def test_other_oblast_does_not_notify(env):
    enroll(env)
    data = all_quiet()
    data["states"]["Київська область"]["alertnow"] = True
    env.poll(data)
    env.drain()
    assert env.mesh.sent == []


def test_unsubscribed_node_gets_nothing(env):
    enroll(env)
    env.send_command("!stop")
    env.mesh.sent.clear()
    env.poll(lviv(True))
    env.drain()
    assert env.mesh.sent == []


def test_restart_reconcile_does_not_resend(tmp_path):
    env = Env(db_path=str(tmp_path / "a.db"))
    enroll(env)
    env.poll(lviv(True))
    env.drain()
    assert len(env.mesh.sent) == 1
    env.restart()
    env.poll(lviv(True))
    env.drain()
    assert env.mesh.sent == []


def test_alert_during_downtime_sent_once_after_restart(tmp_path):
    env = Env(db_path=str(tmp_path / "a.db"))
    enroll(env)
    env.restart()
    env.poll(lviv(True))
    env.drain()
    assert env.mesh.texts() == [f"ПОВІТРЯНА ТРИВОГА! {LVIV}"]
    env.restart()
    env.poll(lviv(True))
    env.drain()
    assert env.mesh.sent == []


def test_reconcile_baseline_when_never_notified():
    env = Env()
    env.poll(lviv(True))
    env.db.ensure_node(NODE_ID)
    env.db.update_node(NODE_ID, subscribed=1, oblast=LVIV)
    env.notifier.reconcile()
    env.drain()
    assert env.mesh.sent == []
    assert env.db.get_node(NODE_ID)["last_notified_state"] == 1


def test_start_then_end_while_queued_is_superseded(env):
    enroll(env)
    env.poll(lviv(True))
    env.poll(lviv(False))
    env.drain()
    assert env.mesh.sent == []
    statuses = [n["acknowledgement_status"] for n in env.db.list_notifications() if n["kind"] == "alert"]
    assert sorted(statuses) == ["dropped", "superseded"]


def test_end_then_start_while_queued_sends_final_state(env):
    enroll(env)
    env.poll(lviv(True))
    env.drain()
    env.mesh.connected = False
    env.poll(lviv(False))
    env.poll(lviv(True))
    env.mesh.connected = True
    env.drain()
    assert len(env.mesh.sent) == 1


def test_region_change_into_active_oblast_uses_existing_alert_text(env):
    enroll(env)
    data = all_quiet()
    data["states"][TERNOPIL]["alertnow"] = True
    env.poll(data)
    env.position(NODE_ID, 49.5, 25.5)
    env.drain()
    assert env.mesh.texts() == [f"Ви в зоні тривоги: {TERNOPIL}. Тривога триває."]
    assert not env.mesh.texts()[0].startswith("ПОВІТРЯНА ТРИВОГА!")
    env.poll(data)
    env.drain()
    assert len(env.mesh.sent) == 1


def test_leaving_alert_zone_message(env):
    env.poll(lviv(True))
    enroll(env, quiet=False)
    assert env.db.get_node(NODE_ID)["last_notified_state"] == 1
    env.position(NODE_ID, 49.5, 25.5)
    env.drain()
    assert env.mesh.texts() == [f"Ви вийшли із зони тривоги. {TERNOPIL}: тривоги немає."]


def test_region_change_quiet_no_message_by_default_and_dedup_updated(env):
    enroll(env)
    env.position(NODE_ID, 49.5, 25.5)
    env.drain()
    assert env.mesh.sent == []
    node = env.db.get_node(NODE_ID)
    assert (node["last_notified_oblast"], node["last_notified_state"]) == (TERNOPIL, 0)


def test_region_change_quiet_option_sends_info(env):
    env.config.alerts.notify_region_change_quiet = True
    enroll(env)
    env.position(NODE_ID, 49.5, 25.5)
    env.drain()
    assert env.mesh.texts() == [f"Новий регіон: {TERNOPIL}. Тривоги немає."]


def test_registration_confirmation_sets_dedup_so_no_extra_alert(env):
    env.poll(lviv(True))
    env.mesh.nodes[0xA1B2C3D4] = {"position": {"latitude": 49.5, "longitude": 24.5}, "lastHeard": int(env.clock())}
    env.send_command("!start")
    assert env.mesh.texts() == [f"Alerts enabled. Region: {LVIV}. Status: Active."]
    env.poll(lviv(True))
    env.notifier.reconcile()
    env.drain()
    assert len(env.mesh.sent) == 1


def test_any_nearby_uses_or_over_candidates():
    env = Env(policy="any_nearby")
    enroll(env, lat=49.5, lon=24.998)
    data = all_quiet()
    data["states"][TERNOPIL]["alertnow"] = True
    env.poll(data)
    env.drain()
    assert env.mesh.texts() == [f"ПОВІТРЯНА ТРИВОГА! {TERNOPIL}"]
    env.poll(lviv(True))
    env.drain()
    assert len(env.mesh.sent) == 1
    env.poll(all_quiet())
    env.drain()
    assert env.mesh.texts()[-1].startswith("ВІДБІЙ ТРИВОГИ.")


def test_assigned_policy_ignores_neighbour():
    env = Env(policy="assigned")
    enroll(env, lat=49.5, lon=24.998)
    data = all_quiet()
    data["states"][TERNOPIL]["alertnow"] = True
    env.poll(data)
    env.drain()
    assert env.mesh.sent == []
