"""Phase-isolated synthetic measurement of the incomplete-parts observation.

Builds temporary ledgers only — no production DB, services or credentials.
Each run records per-phase elapsed time, processed counts and SQL statement
counts so an ``observation_limit`` can be attributed to a concrete phase
without wall-clock threshold assertions. Audit JSON goes to
``MCS_OBSERVE_PHASES_DIR`` when set; the compact summary always prints.
"""
import json
import os
import time
from collections import Counter
from pathlib import Path

import pytest

import health_report
import notify_scope
import notify_tracking_ack
from ingest_testkit import _ledger
from mcs_requests import canonical, payload_hash
from notify_testkit import _uuid

NOW = 200_000.0
COMMAND = notify_tracking_ack.COMMAND
SLACK_SCOPE = {"profile": "synthetic-slack", "application_id": "A_SYNTHETIC",
               "team_id": "T_SYNTHETIC", "channel_id": "C_SYNTHETIC"}
SLACK = {"notify": {"interactive": "slack", "route_epoch": 1,
                    "slack": dict(SLACK_SCOPE)}}
_HASH = "a" * 64
_PART_COUNT = 64


@pytest.fixture
def db(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        yield ledger.db
    finally:
        ledger.close()


def _card(db, key, *, message_id):
    return db.execute(
        "INSERT INTO notification_cards(card_key,kind,anchor_key,transport,delivery_state,"
        "profile,application_id,guild_id,team_id,channel_id,message_id,created_at,updated_at) "
        "VALUES(?,'thread','synthetic-anchor','slack','delivered',?,?,?,?,?,?,?,?)",
        (key, SLACK_SCOPE["profile"], SLACK_SCOPE["application_id"], None,
         SLACK_SCOPE["team_id"], SLACK_SCOPE["channel_id"], message_id,
         NOW - 100, NOW)).lastrowid


def _render(db, *, delivery_id, card_id, rev, state="delivered", spec_json=None):
    db.execute(
        "INSERT INTO notification_renders(delivery_id,card_id,op,render_rev,route_epoch,transport,"
        "payload_hash,correlation,state,profile,application_id,guild_id,team_id,channel_id,"
        "spec_json,created_at,updated_at) VALUES(?,?,'update',?,1,'slack',?,?,?,?,?,?,?,?,?,?,?)",
        (delivery_id, card_id, rev, _HASH, delivery_id, state,
         SLACK_SCOPE["profile"], SLACK_SCOPE["application_id"], None,
         SLACK_SCOPE["team_id"], SLACK_SCOPE["channel_id"], spec_json, NOW - 100, NOW))


def _part(db, *, delivery_id, part_id, kind="body_part", idx=0, state="not_sent",
          error="msg_too_long", attempt_id=None, remote_id=None):
    db.execute(
        "INSERT INTO notification_render_parts(delivery_id,part_id,kind,idx,state,error_code,name,"
        "payload_sha256,attachment_id,attempt_id,remote_id,remote_file_id,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (delivery_id, part_id, kind, idx, state, error, f"m:100#{idx + 1}",
         f"{idx + 1:064x}", idx + 1 if kind == "attachment_part" else None,
         attempt_id, remote_id, None, NOW - 60))


def _plain_incomplete(db, count, *, first=0, per_render=1):
    """Delivered slack renders holding unfinished parts (no ack targets)."""
    renders = (count + per_render - 1) // per_render
    for r in range(renders):
        n = first + r
        card = _card(db, f"synthetic-plain-{n}", message_id=f"root-{n}")
        delivery = _uuid(400_000 + n)
        _render(db, delivery_id=delivery, card_id=card, rev=1)
        for index in range(min(per_render, count - r * per_render)):
            _part(db, delivery_id=delivery, part_id=f"part-{index}", state="not_sent",
                  error="synthetic_failure", idx=index, attempt_id=None)


def _ack_scene(db, card_no, part_count):
    """One card with an old delivered render, ``part_count`` eligible not-sent
    body parts and a later delivered successor carrying the card witness."""
    old_delivery, new_delivery = _uuid(10_000 + card_no), _uuid(20_000 + card_no)
    card = _card(db, f"synthetic-ack-{card_no}", message_id=f"root-ack-{card_no}")
    _render(db, delivery_id=old_delivery, card_id=card, rev=1)
    _render(db, delivery_id=new_delivery, card_id=card, rev=2)
    stem = old_delivery.replace("-", "")
    for index in range(part_count):
        part_id = f"part-{index}"
        _part(db, delivery_id=old_delivery, part_id=part_id,
              attempt_id=f"p:{stem}:{part_id}")
    _part(db, delivery_id=new_delivery, part_id="card", kind="card", idx=0,
          state="delivered", error=None, remote_id=f"root-ack-{card_no}")
    db.execute(
        "INSERT INTO notification_delivery_attempts(attempt_id,delivery_id,begin_command_id,"
        "state,message_id,error_code,worker_id,created_at,finished_at) "
        "VALUES(?,?,?,'delivered',NULL,NULL,'synthetic-worker',?,?)",
        (f"att-{card_no}", old_delivery, f"cmd-{card_no}", NOW - 90, NOW - 50))
    items = [notify_tracking_ack.target(db, old_delivery, f"part-{index}", cfg=SLACK,
                                      successor_id=new_delivery)
             for index in range(part_count)]
    assert all("error" not in item for item in items), items
    return items


def _receipt(command_no, targets, *, processed_at):
    req = {"version": 1, "cmd": COMMAND, "command_id": _uuid(100_000 + command_no),
           "actor": "synthetic-operator", "reason": "synthetic phase measurement",
           "human_confirmed": True, "targets": targets}
    digest = payload_hash(req)
    receipt = {"kind": COMMAND, "version": 1, "command_id": req["command_id"],
               "payload_hash": digest, "outcome": "applied", "actor": req["actor"],
               "reason": req["reason"], "human_confirmed": True, "targets": targets,
               "processed_at": processed_at, "delivery_outcome": "not_sent",
               "residual": "unrecovered", "followup": "none"}
    return (req["command_id"], digest, canonical(receipt).decode(), processed_at)


def _insert_receipts(db, rows):
    db.executemany(
        "INSERT INTO command_receipts(command_id,payload_hash,project_id,request_id,"
        "outcome,receipt_json,processed_at) VALUES(?,?,NULL,NULL,'applied',?,?)", rows)


def _filler_receipts(count, *, first=0):
    """Valid applied receipts whose targets match no stored render."""
    rows = []
    for i in range(count):
        n = first + i
        targets = [{"delivery_id": _uuid(500_000 + n), "part_id": "part-0",
                    "successor_id": _uuid(600_000 + n), "proof": _HASH}]
        rows.append(_receipt(300_000 + n, targets, processed_at=NOW - (n % 1000)))
    return rows


def _other_receipts(count, *, first=0):
    """Applied receipts of unrelated command kinds — they never reach the
    Python validator but every row still pays the SQL kind extraction."""
    rows = []
    for i in range(count):
        n = first + i
        receipt = {"kind": "ops.card_part_resolve", "version": 1,
                   "command_id": _uuid(700_000 + n), "payload_hash": _HASH,
                   "outcome": "applied", "processed_at": NOW - (n % 1000)}
        rows.append((_uuid(700_000 + n), _HASH, canonical(receipt).decode(),
                     NOW - (n % 1000)))
    return rows


def _ack_receipts(scenes):
    """One receipt per scene carrying that scene's real re-proved targets."""
    return [_receipt(card_no, items, processed_at=NOW - card_no)
            for card_no, items in enumerate(scenes)]


class _Profiler:
    """Time the three read phases, count their calls and tag SQL per phase."""

    def __init__(self, db):
        self.phase = "other"
        self.elapsed = {}
        self.calls = {}
        self._sql = []
        db.set_trace_callback(lambda _sql: self._sql.append(self.phase))

    def wrap(self, module, name, key, *, materialize=False):
        original = getattr(module, name)
        profiler = self

        def timed(*args, **kwargs):
            profiler.calls[key] = profiler.calls.get(key, 0) + 1
            previous = profiler.phase
            profiler.phase = key
            started = time.perf_counter()
            try:
                result = original(*args, **kwargs)
            finally:
                profiler.elapsed[key] = (profiler.elapsed.get(key, 0.0)
                                         + time.perf_counter() - started)
            profiler.phase = previous
            if not materialize or not hasattr(result, "__next__"):
                return result
            # Generator results keep streaming order: each next() is timed
            # under this phase and consumer work between yields stays in the
            # outer phase — never materialize the whole stream up front.
            iterator = iter(result)

            def measured():
                try:
                    while True:
                        profiler.phase = key
                        item_started = time.perf_counter()
                        try:
                            item = next(iterator)
                        except StopIteration:
                            return
                        finally:
                            profiler.elapsed[key] = (profiler.elapsed.get(key, 0.0)
                                                     + time.perf_counter() - item_started)
                            profiler.phase = previous
                        yield item
                finally:
                    profiler.phase = previous
            return measured()
        return timed

    def count_only(self, module, name):
        original = getattr(module, name)
        counter = self.calls

        def wrapped(*args, **kwargs):
            counter["target"] = counter.get("target", 0) + 1
            return original(*args, **kwargs)
        return wrapped

    def snapshot(self):
        return {"elapsed_s": {k: round(v, 6) for k, v in self.elapsed.items()},
                "calls": dict(self.calls),
                "sql_statements": dict(Counter(self._sql))}


def _observe(db, monkeypatch, *, seconds=health_report.NOTIFICATION_OBSERVATION_S):
    """Install phase wrappers and run the production bounded read once.

    Patches are scoped to this call via ``monkeypatch.context()`` so autouse
    fixture patches stay untouched and each observe wraps the originals —
    never a broad ``monkeypatch.undo()``.
    """
    profiler = _Profiler(db)
    observation = {}
    with monkeypatch.context() as scoped:
        scoped.setattr(notify_tracking_ack, "receipt_targets",
                       profiler.wrap(notify_tracking_ack, "receipt_targets",
                                     "receipt_targets"))
        scoped.setattr(notify_scope, "_incomplete_part_rows",
                       profiler.wrap(notify_scope, "_incomplete_part_rows",
                                     "part_rows", materialize=True))
        scoped.setattr(notify_tracking_ack, "acknowledged",
                       profiler.wrap(notify_tracking_ack, "acknowledged",
                                     "acknowledged"))
        scoped.setattr(notify_tracking_ack, "target",
                       profiler.count_only(notify_tracking_ack, "target"))
        started = time.perf_counter()
        try:
            report = health_report.bounded_health_read(
                db, lambda _check: notify_scope.incomplete_parts_health(db, NOW, SLACK),
                seconds=seconds, observation=observation)
            outcome = "complete"
        except Exception as exc:
            report, outcome = None, f"{type(exc).__name__}: {exc}"
        total = time.perf_counter() - started
    return report, outcome, total, profiler.snapshot(), observation


def _dump(name, payload):
    out = os.environ.get("MCS_OBSERVE_PHASES_DIR")
    if out:
        path = Path(out)
        path.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"OBSERVE {name}: " + json.dumps(payload, sort_keys=True))


class _FakeClock:
    """Deterministic monotonic source for ``health_report``'s bounded read.

    Only ``health_report.time`` is swapped, so the budget clock is fully
    controlled while every other module keeps the real clock. ``time.time``
    and friends delegate to the real module.
    """

    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds

    def __getattr__(self, name):
        return getattr(time, name)


def test_receipt_sweep_scales_the_indexing_phase(tmp_path, monkeypatch):
    """receipt_targets cost grows with applied receipt count while the part
    scan and per-row work stay fixed."""
    results = []
    for count in (0, 512, 2048, 4096):
        root = tmp_path / f"receipts-{count}"
        root.mkdir()
        ledger = _ledger(root)
        db = ledger.db
        try:
            _plain_incomplete(db, _PART_COUNT)
            if count:
                _insert_receipts(db, _filler_receipts(count))
            db.commit()

            report, outcome, elapsed, snap, observation = _observe(db, monkeypatch)
            assert outcome == "complete"
            index = notify_tracking_ack.receipt_targets(db)
            assert len(index) == count
            assert snap["calls"].get("acknowledged") == _PART_COUNT
            assert snap["calls"].get("target", 0) == 0
            assert report["total"] == _PART_COUNT and report["cards"] == _PART_COUNT
            results.append({"receipts": count, "parts": _PART_COUNT,
                            "total_elapsed_s": round(elapsed, 6),
                            "phases": snap, "observation": observation,
                            "report_total": report["total"],
                            "report_cards": report["cards"]})
        finally:
            ledger.close()
    _dump("receipt_sweep.json", {"budget_s": health_report.NOTIFICATION_OBSERVATION_S,
                                 "runs": results})


def test_ack_hit_reproof_scales_per_target(tmp_path, monkeypatch):
    """Each stored ack target forces a full target() re-proof; removing the
    receipts on the same rows isolates index-miss from re-proof cost."""
    ledger = _ledger(tmp_path)
    db = ledger.db
    try:
        scenes = [_ack_scene(db, card_no, 20) for card_no in range(50)]
        _insert_receipts(db, _ack_receipts(scenes))
        db.commit()

        report, outcome, total_with, snap_with, obs_with = _observe(db, monkeypatch)
        assert outcome == "complete"
        assert snap_with["calls"].get("acknowledged") == 1000
        assert snap_with["calls"].get("target") == 1000
        assert report["acknowledged_unresolved_total"] == 1000
        assert report["active_total"] == 0 and report["total"] == 1000

        db.execute("DELETE FROM command_receipts")
        db.commit()
        report_wo, outcome_wo, total_wo, snap_wo, obs_wo = _observe(db, monkeypatch)
        assert outcome_wo == "complete"
        assert snap_wo["calls"].get("acknowledged") == 1000
        assert snap_wo["calls"].get("target", 0) == 0
        assert report_wo["acknowledged_unresolved_total"] == 0
        assert report_wo["active_total"] == 1000

        _dump("ack_hits.json", {
            "hits": 1000, "scenes": 50, "parts_per_scene": 20,
            "with_receipts": {"total_elapsed_s": round(total_with, 6),
                              "phases": snap_with, "observation": obs_with,
                              "report": {k: report[k] for k in
                                         ("total", "active_total",
                                          "acknowledged_unresolved_total")}},
            "without_receipts": {"total_elapsed_s": round(total_wo, 6),
                                 "phases": snap_wo, "observation": obs_wo,
                                 "report": {k: report_wo[k] for k in
                                            ("total", "active_total",
                                             "acknowledged_unresolved_total")}}})
    finally:
        ledger.close()


def test_combined_sweep_synthetic_stress_shape(tmp_path, monkeypatch):
    """A synthetic stress shape — 4096 filler receipts plus 1000 live ack
    hits — sized to exceed the observation budget. It is not a measured
    production shape; the bounded pass records what the real 2s window does
    with it."""
    ledger = _ledger(tmp_path)
    db = ledger.db
    try:
        scenes = [_ack_scene(db, card_no, 20) for card_no in range(50)]
        _plain_incomplete(db, _PART_COUNT, first=1000)
        _insert_receipts(db, _filler_receipts(4046) + _ack_receipts(scenes))
        db.commit()

        # Unbounded pass pins the reproducible quantities; the bounded pass
        # records whether the production 2s window is sufficient.
        report_full, outcome_full, total_full, snap_full, _ = _observe(
            db, monkeypatch, seconds=30.0)
        assert outcome_full == "complete"
        assert snap_full["calls"].get("acknowledged") == 1064
        assert snap_full["calls"].get("target") == 1000
        assert report_full["acknowledged_unresolved_total"] == 1000
        assert report_full["total"] == 1064

        report, outcome, total, snap, observation = _observe(db, monkeypatch)
        index = notify_tracking_ack.receipt_targets(db)
        assert len(index) == 5046
        _dump("combined.json", {
            "receipts": 4096, "ack_hits": 1000, "plain_parts": _PART_COUNT,
            "budget_s": health_report.NOTIFICATION_OBSERVATION_S,
            "unbounded": {"total_elapsed_s": round(total_full, 6),
                          "phases": snap_full},
            "bounded": {"outcome": outcome, "total_elapsed_s": round(total, 6),
                        "phases": snap, "observation": observation},
            "report": ({k: report.get(k) for k in
                        ("status", "reason", "total", "active_total",
                         "acknowledged_unresolved_total", "cards")}
                       if report else None)})
    finally:
        ledger.close()


def test_receipt_table_sweep_scales_the_sql_kind_filter(tmp_path, monkeypatch):
    """receipt_targets' SELECT extracts the JSON kind for every applied row —
    unrelated command receipts dominate that scan in a real ledger."""
    results = []
    for others in (0, 8192, 32768, 131072):
        root = tmp_path / f"others-{others}"
        root.mkdir()
        ledger = _ledger(root)
        db = ledger.db
        try:
            _plain_incomplete(db, _PART_COUNT)
            _insert_receipts(db, _filler_receipts(512) + _other_receipts(others))
            db.commit()

            report_full, outcome_full, total_full, snap_full, _ = _observe(
                db, monkeypatch, seconds=30.0)
            assert outcome_full == "complete"
            index = notify_tracking_ack.receipt_targets(db)
            assert len(index) == 512
    
            report, outcome, total, snap, observation = _observe(db, monkeypatch)
            results.append({"other_receipts": others, "tracking_receipts": 512,
                            "parts": _PART_COUNT,
                            "unbounded": {"total_elapsed_s": round(total_full, 6),
                                          "phases": snap_full},
                            "bounded": {"outcome": outcome,
                                        "total_elapsed_s": round(total, 6),
                                        "phases": snap,
                                        "observation": observation},
                            "report": ({k: report.get(k) for k in
                                        ("status", "reason", "total")}
                                       if report else None)})
        finally:
            ledger.close()
    _dump("receipt_table_sweep.json",
          {"budget_s": health_report.NOTIFICATION_OBSERVATION_S, "runs": results})


def test_part_count_sweep_scales_the_row_scan(tmp_path, monkeypatch):
    """The unfinished-part row scan and its per-row proofs grow with the
    stored part population — the axis the receipt sweep cannot show."""
    results = []
    for count in (256, 2048, 8192, 32768):
        root = tmp_path / f"parts-{count}"
        root.mkdir()
        ledger = _ledger(root)
        db = ledger.db
        try:
            _plain_incomplete(db, count, per_render=20)
            _insert_receipts(db, _filler_receipts(512))
            db.commit()

            report_full, outcome_full, total_full, snap_full, _ = _observe(
                db, monkeypatch, seconds=30.0)
            assert outcome_full == "complete"
            renders = -(-count // 20)
            assert snap_full["calls"].get("acknowledged") == count
            assert snap_full["calls"].get("target", 0) == 0
            assert report_full["total"] == count and report_full["cards"] == renders
    
            report, outcome, total, snap, observation = _observe(db, monkeypatch)
            results.append({"parts": count, "renders": renders, "receipts": 512,
                            "unbounded": {"total_elapsed_s": round(total_full, 6),
                                          "phases": snap_full},
                            "bounded": {"outcome": outcome,
                                        "total_elapsed_s": round(total, 6),
                                        "phases": snap,
                                        "observation": observation},
                            "report": ({k: report.get(k) for k in
                                        ("status", "reason", "total",
                                         "acknowledged_unresolved_total")}
                                       if report else None)})
        finally:
            ledger.close()
    _dump("part_sweep.json", {"budget_s": health_report.NOTIFICATION_OBSERVATION_S,
                              "per_render": 20, "runs": results})


def test_python_only_overrun_is_invisible_until_the_final_check(db, monkeypatch):
    """A read spending its budget in non-SQL Python cannot be interrupted by
    the progress handler; the shared deadline only stops it after return.
    A fake health_report clock makes the overrun deterministic — no wall
    clock assertions."""
    clock = _FakeClock()
    monkeypatch.setattr(health_report, "time", clock)
    observation = {}

    def read(_check):
        clock.advance(1.0)
        return {}

    with pytest.raises(RuntimeError, match="observation_limit"):
        health_report.bounded_health_read(db, read, seconds=0.05,
                                          observation=observation)
    assert observation["stop"] == "time_limit"
    assert observation["elapsed_s"] == pytest.approx(1.0)


def test_depleted_entry_budget_starts_no_read(db):
    """A shared-window remainder of zero stops before any SQL or callback."""
    observation = {}
    calls = []

    def read(checkpoint):
        calls.append(checkpoint)
        return {}

    with pytest.raises(RuntimeError, match="observation_limit"):
        health_report.bounded_health_read(db, read, seconds=0.0,
                                          observation=observation)
    assert calls == []
    assert observation["stop"] == "budget_exhausted"


def test_report_observation_marks_a_depleted_entry_budget(db, monkeypatch):
    """A shared-window remainder of zero stops before the aggregation: the
    report's own observation keeps phase=connection_setup and
    stop=budget_exhausted, distinguishable from a mid-read overrun."""
    calls = []

    def read(*_args, **_kwargs):
        calls.append(1)
        return {}

    monkeypatch.setattr(notify_scope, "incomplete_parts_health", read)
    report = health_report.incomplete_parts_health(db, NOW, SLACK, seconds=0.0)
    assert report["status"] == "unknown" and report["reason"] == "observation_limit"
    assert report["total"] is None and report["cards"] is None
    assert report["observation"]["phase"] == "connection_setup"
    assert report["observation"]["stop"] == "budget_exhausted"
    assert calls == []


def test_report_observation_marks_the_aggregation_phase_on_overrun(db, monkeypatch):
    """An overrun inside the part aggregation names the phase that was
    running, so entry-budget shortage and aggregation cost stay distinct."""
    _plain_incomplete(db, _PART_COUNT)
    db.commit()
    clock = _FakeClock()
    monkeypatch.setattr(health_report, "time", clock)
    original = notify_tracking_ack.receipt_targets

    def slow_receipt_targets(*args, **kwargs):
        clock.advance(10.0)
        return original(*args, **kwargs)

    monkeypatch.setattr(notify_tracking_ack, "receipt_targets", slow_receipt_targets)
    report = health_report.incomplete_parts_health(db, NOW, SLACK, seconds=0.05)
    assert report["status"] == "unknown" and report["reason"] == "observation_limit"
    assert report["total"] is None
    assert report["observation"]["stop"] == "time_limit"
    assert report["observation"]["phase"] in ("receipt_targets", "part_rows",
                                              "acknowledged")


def test_production_wrapper_reports_honest_unknown_on_overrun(tmp_path, monkeypatch):
    """health_report.incomplete_parts_health keeps total=None and an honest
    observation_limit reason when the bounded read stops — never zeroed.
    The overrun is driven by a fake health_report clock advanced inside a
    wrapped phase, so the bound fires deterministically on any machine."""
    ledger = _ledger(tmp_path)
    db = ledger.db
    try:
        _plain_incomplete(db, _PART_COUNT)
        db.commit()
        clock = _FakeClock()
        monkeypatch.setattr(health_report, "time", clock)
        original = notify_tracking_ack.receipt_targets

        def slow_receipt_targets(*args, **kwargs):
            clock.advance(10.0)
            return original(*args, **kwargs)

        monkeypatch.setattr(notify_tracking_ack, "receipt_targets",
                            slow_receipt_targets)
        report = health_report.incomplete_parts_health(db, NOW, SLACK, seconds=0.05)
        assert report["status"] == "unknown"
        assert report["reason"] == "observation_limit"
        assert report["total"] is None and report["cards"] is None
        assert report["by_state"] == dict.fromkeys(
            ("pending", "unknown", "not_sent", "held"))
        _dump("honest_unknown.json", {
            "parts": _PART_COUNT, "seconds": 0.05, "clock_advance_s": 10.0,
            "report": report})
    finally:
        ledger.close()
