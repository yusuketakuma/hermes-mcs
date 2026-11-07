"""Synthetic physical-slot scheduling, with no model or network access."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import json
import os
import threading
import time

import pytest

import llm_admission
import llm_slot_schedule as schedule
import local_llm


def turn(root, *, realtime=False, preferred=1, seconds=0.5, **kwargs):
    return schedule.turn(root, realtime=realtime, preferred=preferred,
                         deadline=time.monotonic() + seconds, **kwargs)


@pytest.mark.parametrize("utc_hour,utc_minute,expected", [
    (22, 59, True), (23, 0, False), (10, 59, False), (11, 0, True),
])
def test_schedule_uses_jst_boundaries(monkeypatch, utc_hour, utc_minute, expected):
    instant = datetime(2026, 11, 7, utc_hour, utc_minute, tzinfo=timezone.utc)

    class Clock:
        @staticmethod
        def now(zone):
            return instant.astimezone(zone)

    monkeypatch.setattr(schedule, "datetime", Clock)
    assert schedule.night() is expected


def test_day_has_two_background_slots_and_active_rt_allows_both(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "night", lambda: False)
    with turn(tmp_path) as bg1, turn(tmp_path) as bg2:
        assert {bg1, bg2} == {1, 2}
        with turn(tmp_path, seconds=0.02) as blocked:
            assert blocked is None
        with turn(tmp_path, realtime=True) as rt:
            assert rt == 0
    # An active RT must not globally exclude BACKLOG.
    with turn(tmp_path, realtime=True) as rt, turn(tmp_path) as bg1, turn(tmp_path) as bg2:
        assert {rt, bg1, bg2} == {0, 1, 2}


def test_night_borrows_three_slots_and_waiting_rt_gets_next_free(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "night", lambda: True)
    rt_started, release_rt, bg_started = threading.Event(), threading.Event(), threading.Event()
    entered = []
    original_open = schedule._open
    intent_opened = threading.Event()

    def opened(directory, name):
        fd = original_open(directory, name)
        if name == "llm_realtime_active.lock" and threading.current_thread().name.startswith("rt"):
            intent_opened.set()
        return fd

    monkeypatch.setattr(schedule, "_open", opened)

    def realtime():
        with turn(tmp_path, realtime=True, seconds=3) as slot:
            entered.append(("rt", slot))
            rt_started.set()
            assert release_rt.wait(3)

    def background():
        with turn(tmp_path, seconds=3) as slot:
            entered.append(("bg", slot))
            bg_started.set()

    holders = [turn(tmp_path, preferred=slot) for slot in (0, 1, 2)]
    assert [holder.__enter__() for holder in holders] == [0, 1, 2]
    rt = threading.Thread(target=realtime, name="rt-intent")
    bg = threading.Thread(target=background)
    try:
        rt.start()
        assert intent_opened.wait(1)
        # Observe the actual shared intent lock before releasing a physical slot.
        with os.fdopen(original_open(tmp_path, "llm_realtime.lock"), "w") as probe:
            until = time.monotonic() + 1
            while schedule._try(probe.fileno()):
                fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
                assert time.monotonic() < until
                time.sleep(0.005)
        bg.start()
        holders[1].__exit__(None, None, None)
        holders[1] = None
        assert rt_started.wait(1) and entered == [("rt", 1)]
        # While RT is active another completed BG slot can be refilled.
        holders[2].__exit__(None, None, None)
        holders[2] = None
        assert bg_started.wait(1) and entered == [("rt", 1), ("bg", 2)]
    finally:
        release_rt.set()
        for holder in holders:
            if holder is not None:
                holder.__exit__(None, None, None)
        rt.join(4)
        if bg.ident is not None:
            bg.join(4)
    assert not rt.is_alive() and not bg.is_alive()


def test_legacy_exclusive_turn_drains_before_new_physical_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "night", lambda: True)
    fd = schedule._open(tmp_path, "llm_turn.lock")
    try:
        assert schedule._try(fd)
        with turn(tmp_path, seconds=0.02) as slot:
            assert slot is None
    finally:
        os.close(fd)
    with turn(tmp_path) as slot:
        assert slot in schedule.SLOTS


def test_interruption_releases_only_owned_slots_and_intent(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "night", lambda: True)
    with turn(tmp_path, preferred=2) as other:
        with pytest.raises(KeyboardInterrupt):
            with turn(tmp_path, realtime=True) as rt:
                assert rt == 0
                raise KeyboardInterrupt
        with turn(tmp_path, realtime=True) as next_rt, turn(tmp_path) as bg:
            assert {other, next_rt, bg} == {0, 1, 2}


def test_slot_symlink_is_not_followed(tmp_path):
    target = tmp_path / "unrelated"
    target.write_text("untouched")
    (tmp_path / "llm_slot_0.lock").symlink_to(target)
    with pytest.raises(OSError):
        with turn(tmp_path):
            pytest.fail("untrusted slot admitted")
    assert target.read_text() == "untouched"


def test_actual_transport_never_sends_two_requests_to_one_physical_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "night", lambda: True)
    monkeypatch.setenv(local_llm.SCHEDULE_ENV, str(tmp_path))
    monkeypatch.setenv(local_llm.WORK_CLASS_ENV, "BACKLOG")
    active, maximum, all_slots = set(), [0], set()
    lock = threading.Lock()
    first_three = threading.Barrier(3)
    count = [0]

    def post(_url, method, body, _timeout, _deadline):
        assert method == "POST"
        slot = body["id_slot"]
        with lock:
            assert slot not in active
            active.add(slot)
            all_slots.add(slot)
            maximum[0] = max(maximum[0], len(active))
            count[0] += 1
            ordinal = count[0]
        if ordinal <= 3:
            first_three.wait(2)
        with lock:
            active.remove(slot)
        return 200, {}, json.dumps({"choices": [{"message": {"content": "synthetic"},
                                               "finish_reason": "stop"}]}).encode()

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda _: local_llm.chat(
            "synthetic", timeout=3, request_fn=post), range(6)))
    assert all(response["status"] == 200 for response in responses)
    assert maximum == [3] and all_slots == {0, 1, 2} and count == [6]


@pytest.fixture
def broker(tmp_path):
    value = llm_admission.Broker(str(tmp_path / "admission.db"), slots=3)
    value.open_epoch(lambda: True)
    yield value
    value.close()


def test_bound_permits_mix_classes_without_reusing_an_unknown_slot(broker):
    rt = broker.acquire("mcs.realtime", "RT", physical_slot=0)
    bg = broker.acquire("mcs.extract", "BACKLOG", physical_slot=1)
    bg2 = broker.acquire("mcs.semantic", "BACKLOG", physical_slot=2)
    for permit in (rt, bg, bg2):
        assert permit["admitted"] and broker.sent(permit["permit_id"])["sent"]
    assert not broker.overlap() and not broker.status()["overlap"]
    broker.mark_unknown(bg["permit_id"], "synthetic transport timeout")
    broker.terminal(rt["permit_id"], "done")
    broker.terminal(bg2["permit_id"], "done")
    assert broker.held_slots() == {1}
    assert broker.acquire("mcs.extract", "BACKLOG", physical_slot=1)["reason"] == "slot_occupied"
    assert broker.acquire("mcs.realtime", "RT", physical_slot=0)["admitted"]
    assert broker._permit(bg["permit_id"])["state"] == "unknown"


def test_legacy_unknown_holds_all_slots_until_existing_terminal_proof(broker):
    old = broker.acquire("mcs.extract", "BACKLOG")
    broker.sent(old["permit_id"])
    broker.mark_unknown(old["permit_id"], "synthetic")
    assert broker.held_slots() == {0, 1, 2}
    assert broker.acquire("mcs.realtime", "RT", physical_slot=0)["reason"] == "legacy_inflight"
    broker.terminal(old["permit_id"], "done", proof="synthetic_backend_receipt")
    assert broker.acquire("mcs.realtime", "RT", physical_slot=0)["admitted"]


@pytest.mark.parametrize("route,work_class", [("mcs.extract", "BACKLOG"), ("mcs.realtime", "RT")])
def test_bound_work_prevents_legacy_same_class_admission(broker, route, work_class):
    bound = broker.acquire(route, work_class, physical_slot=0)
    assert bound["admitted"] and broker.sent(bound["permit_id"])["sent"]
    legacy = broker.acquire(route, work_class)
    assert legacy["reason"] == "physical_inflight" and not legacy["admitted"]
    broker.mark_unknown(bound["permit_id"], "synthetic")
    assert broker.acquire(route, work_class)["reason"] == "physical_inflight"
    broker.terminal(bound["permit_id"], "done", proof="synthetic_backend_receipt")
    legacy = broker.acquire(route, work_class)
    assert legacy["admitted"] and broker.sent(legacy["permit_id"])["sent"]


def test_legacy_rt_waiting_drains_all_bound_work_before_promotion(broker):
    rt = broker.acquire("mcs.realtime", "RT", physical_slot=0)
    bg = broker.acquire("mcs.extract", "BACKLOG", physical_slot=1)
    assert broker.sent(rt["permit_id"])["sent"] and broker.sent(bg["permit_id"])["sent"]
    waiting = broker.acquire("hermes.interactive", "RT")
    assert waiting["reason"] == "waiting" and broker.held_slots() == {0, 1, 2}
    broker.terminal(bg["permit_id"], "done")
    assert broker.poll(waiting["permit_id"])["state"] == "waiting"
    assert broker.sent(waiting["permit_id"])["reason"] == "state_waiting"
    broker.terminal(rt["permit_id"], "done")
    assert broker.poll(waiting["permit_id"])["state"] == "admitted"
    assert broker.sent(waiting["permit_id"])["sent"]


@pytest.mark.parametrize("route,work_class", [("mcs.extract", "BACKLOG"), ("mcs.realtime", "RT")])
def test_legacy_send_rechecks_bound_permits_after_admission(broker, route, work_class):
    legacy = broker.acquire(route, work_class)
    # Simulate a row from an older or interrupted writer between admission
    # and send; even this mixed state must never mint a legacy backend token.
    with broker._write_tx():
        bound = broker._admit(broker.epoch(), route, work_class, None, None, physical_slot=0)
    assert broker.sent(legacy["permit_id"])["reason"] == "physical_inflight"
    assert broker._permit(legacy["permit_id"])["token"] is None
    assert broker.sent(bound["permit_id"])["reason"] == "physical_slot_conflict"


@pytest.mark.parametrize("work_class,route,slot", [("RT", "mcs.realtime", 0),
                                                  ("BACKLOG", "mcs.extract", 1)])
def test_scheduled_transport_binds_route_permit_and_wire_slot(
        tmp_path, monkeypatch, broker, work_class, route, slot):
    monkeypatch.setenv(local_llm.SCHEDULE_ENV, str(tmp_path))
    monkeypatch.setenv(local_llm.WORK_CLASS_ENV, work_class)
    monkeypatch.setattr(local_llm, "_broker", lambda _: broker)
    wire = []

    def post(_url, _method, body, _timeout, _deadline):
        token = body["admission_token"]
        valid = broker.token_valid(token, work_class)
        assert valid["ok"]
        permit = broker._permit(valid["permit_id"])
        assert permit["physical_slot"] == body["id_slot"]
        wire.append((permit["client"], body["id_slot"]))
        return 200, {}, b'{"choices":[{"message":{"content":"synthetic"},"finish_reason":"stop"}]}'

    result = local_llm.admitted_chat("mcs.extract", "synthetic", request_fn=post, timeout=1)
    assert result["status"] == 200 and wire == [(route, slot)]
    assert broker.held_slots() == set()
    denied = local_llm.admitted_chat("mcs.unregistered", "synthetic", request_fn=post, timeout=1)
    assert denied["admission"] == "unknown_client" and len(wire) == 1


def test_scheduled_realtime_reaches_intent_instead_of_early_busy_skip(tmp_path, monkeypatch):
    import extract_llm_provider
    monkeypatch.setattr(local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(extract_llm_provider, "_slots_busy", lambda *a: pytest.fail("idle probe"))
    with local_llm.scheduling_scope(tmp_path, realtime=True):
        assert not extract_llm_provider.pinned_slot_busy(time.monotonic() + 1)
        assert os.environ[local_llm.WORK_CLASS_ENV] == "RT"


def test_bound_unknown_defers_scheduled_send_without_retiring_or_posting(tmp_path, monkeypatch, broker):
    monkeypatch.setattr(schedule, "night", lambda: False)
    monkeypatch.setenv(local_llm.SCHEDULE_ENV, str(tmp_path))
    monkeypatch.setenv(local_llm.WORK_CLASS_ENV, "BACKLOG")
    monkeypatch.setattr(local_llm, "_broker", lambda _: broker)
    ids = []
    for slot in (1, 2):
        permit = broker.acquire("mcs.extract", "BACKLOG", physical_slot=slot)
        ids.append(permit["permit_id"])
        broker.sent(ids[-1])
        broker.mark_unknown(ids[-1], "synthetic")
    result = local_llm.admitted_chat("mcs.extract", "synthetic", timeout=0.02,
                                    request_fn=lambda *a: pytest.fail("unknown slot reused"))
    assert result["admission"] == "completion_unproven"
    assert [broker._permit(pid)["state"] for pid in ids] == ["unknown", "unknown"]


def test_disabled_schedule_keeps_legacy_turn_and_route_behavior(tmp_path, monkeypatch):
    monkeypatch.delenv(local_llm.SCHEDULE_ENV, raising=False)
    monkeypatch.setenv(local_llm.TURN_LOCK_ENV, str(tmp_path / "legacy"))
    with ExitStack() as stack:
        stack.enter_context(local_llm._turn(None))
        errors = {}
        assert local_llm.chat("synthetic", deadline=time.monotonic() + .01,
                              error_out=errors, request_fn=lambda *a: pytest.fail("legacy lock bypassed")) is None
        assert errors == {"kind": "busy"}
