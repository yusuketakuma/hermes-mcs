"""Synthetic acceptance of the receipted patient-context reset: erase, carry spending over, rebuild."""
import copy
from datetime import datetime
from decimal import Decimal
import json
import os
from types import SimpleNamespace

import pytest

import cloud_policy
import patient_context_quota
import patient_context_refresh_readiness as readiness
import patient_context_refresh_state as state
import patient_context_reset as reset
import semantic_policy
import semantic_store
from test_context_cloud_update_identity import cloud, finish, real_cloud, runtime  # noqa: F401  (fixtures)

BACKUP = {"name": "patient-context-reset-synthetic.db", "bytes": 7, "sha256": "a" * 64}


def _disabled(cfg):
    out = copy.deepcopy(cfg)
    out.setdefault("extract", {}).setdefault("patient_context", {})["enabled"] = False
    return out


def _request(preview_hash, **kw):
    return {"version": 1, "cmd": reset.COMMAND, "command_id": "00000000-0000-4000-8000-000000000001",
            "actor": "synthetic-human", "human_confirmed": True, "project_id": None,
            "reason": "synthetic rebuild", "preview_hash": preview_hash, "backup": BACKUP, **kw}


def _apply(ledger, cfg, req, now, tmp_path):
    with ledger.db:
        ledger.db.execute("BEGIN IMMEDIATE")
        return reset.apply_tx(ledger.db, req, now, cfg=cfg, backup_dir=tmp_path)


def _copy_to_other_route(ledger):
    """One settled reextract pair, cloned from a real patient-context pair, that the reset must keep."""
    reserve = ledger.artifacts("cloud_usage_reserve")[0]
    rid = json.loads(reserve["meta"])["reservation_id"]
    result = next(row for row in ledger.artifacts("cloud_usage_result")
                  if json.loads(row["meta"])["reservation_id"] == rid)
    with ledger.db:
        for row in (reserve, result):
            meta = {**json.loads(row["meta"]), "route": "reextract", "reservation_id": "f" * 32}
            ledger.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                              "VALUES(?,?,?,?,?,?,?)", (row["kind"], row["project_id"], row["message_id"],
                                                         row["content"], row["model"], json.dumps(meta),
                                                         row["created_at"]))


def _usage(ledger):
    return {period: cloud_policy.usage_report(SimpleNamespace(db=ledger.db), period=period)
            for period in ("day", "month")}


@pytest.fixture
def built(runtime, real_cloud, tmp_path):  # noqa: F811
    ledger, clock, _ = runtime
    cfg, posts = real_cloud
    finish(runtime, cfg)
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg) is not None
    backup = tmp_path / BACKUP["name"]
    backup.write_bytes(b"backup!")
    os.utime(backup, (clock.wall, clock.wall))   # taken just now, on the synthetic clock
    return ledger, clock, cfg, posts


def test_reset_refuses_until_generation_is_disabled_and_cloud_is_paused(built, tmp_path):
    ledger, clock, cfg, _ = built
    assert reset.preview(ledger.db, cfg=cfg, now=clock.wall)["refusal"] == "patient_context_enabled"
    assert reset.preview(ledger.db, cfg=_disabled(cfg), now=clock.wall)["refusal"] == "cloud_not_paused"
    cloud_policy.stop(ledger)
    found = reset.preview(ledger.db, cfg=_disabled(cfg), now=clock.wall)
    assert found["refusal"] is None and found["reservations"] > 0
    assert _apply(ledger, _disabled(cfg), _request("b" * 64), clock.wall, tmp_path)[0] == "preview_changed"
    missing = _request(found["preview_hash"], backup={**BACKUP, "name": "absent.db"})
    assert _apply(ledger, _disabled(cfg), missing, clock.wall, tmp_path)[0] == "backup_missing"
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg) is not None, "a refused reset changes nothing"


def test_a_preview_taken_before_invalid_rows_existed_is_refused_cleanly(built, tmp_path):
    """An old-format preview (outcomes without invalid_rows) never matches and erases nothing."""
    from mcs_util import payload_hash
    ledger, clock, cfg, _ = built
    off = _disabled(cfg)
    cloud_policy.stop(ledger)
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    assert found["refusal"] is None and found["outcomes"]["invalid_rows"] == 0
    body = {key: found[key] for key in found if key not in ("refusal", "preview_hash")}
    body["outcomes"] = {key: value for key, value in body["outcomes"].items() if key != "invalid_rows"}
    kinds = {kind: len(ledger.artifacts(kind)) for kind in reset.ERASED_KINDS}
    assert _apply(ledger, off, _request(payload_hash(body)), clock.wall, tmp_path)[0] == "preview_changed"
    assert {kind: len(ledger.artifacts(kind)) for kind in reset.ERASED_KINDS} == kinds
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg) is not None, "a refused reset changes nothing"


def test_reset_erases_results_and_accounting_but_keeps_spending(built, tmp_path):
    ledger, clock, cfg, _ = built
    off = _disabled(cfg)
    _copy_to_other_route(ledger)
    cloud_policy.stop(ledger)
    before = _usage(ledger)
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    error, extra = _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path)
    assert error is None and extra["erased"]["accounting_rows"] >= 2 and extra["carryover"]
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg) is None, "published contexts are hidden at once"
    for kind in reset.ERASED_KINDS:
        assert not ledger.artifacts(kind), kind
    routes = {json.loads(row["meta"])["route"] for row in ledger.artifacts("cloud_usage_reserve")}
    assert routes == {"reextract"}
    assert _usage(ledger) == before, "every limit keeps counting what was already spent"


def test_reset_keeps_the_identity_of_each_patients_published_facts(built, tmp_path):
    import patient_context_view as view
    ledger, clock, cfg, _ = built
    doc, _meta = state._latest(ledger.db, state.KIND, 1)
    expected = sorted({view.reset_fact_key(fact) for fact in doc["facts"]})
    assert expected
    off = _disabled(cfg)
    cloud_policy.stop(ledger)
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    assert _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path)[0] is None
    rows = {row["project_id"]: json.loads(row["content"]) for row in ledger.artifacts(view.RESET_PRIOR_KIND)}
    assert rows[1] == expected
    unpublished = {row[0] for row in ledger.db.execute("SELECT DISTINCT project_id FROM messages")} - {1}
    assert all(rows[pid] == [] for pid in unpublished), "a patient without a context holds no known facts"
    assert all(fact.get("text") not in json.dumps(keys) for keys in rows.values() for fact in doc["facts"]
               if fact.get("text"))


def test_context_rebuilds_after_reset(built, tmp_path):
    ledger, clock, cfg, posts = built
    sent = len(posts)
    cloud_policy.stop(ledger)
    off = _disabled(cfg)
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    assert _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path)[0] is None
    posts.clear()   # the synthetic wire counts reservations against its own sends since the reset
    cloud_policy.resume(ledger)
    finish(clock_runtime(ledger, clock), cfg)
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg) is not None
    assert len(posts) == sent, "the rebuild sends the same work again"


def test_the_rebuilt_view_finds_again_exactly_the_facts_the_reset_recorded(built, tmp_path):
    import patient_context_view as view
    ledger, clock, cfg, posts = built
    before = [item for item in view.evidence(ledger.db, 1, cfg=cfg)["items"] if item.get("discovery")]
    assert before, "the synthetic context publishes facts the view merges into its items"
    cloud_policy.stop(ledger)
    off = _disabled(cfg)
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    assert _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path)[0] is None
    prior = set(json.loads(ledger.artifacts(view.RESET_PRIOR_KIND)[0]["content"]))
    posts.clear()   # the synthetic wire counts reservations against its own sends since the reset
    cloud_policy.resume(ledger)
    finish(clock_runtime(ledger, clock), cfg)
    after = [item for item in view.evidence(ledger.db, 1, cfg=cfg)["items"] if item.get("discovery")]
    assert after and all(view.reset_fact_key(item) in prior for item in after + before)


def clock_runtime(ledger, clock):
    return ledger, clock, []


def _reset_now(ledger, clock, cfg, tmp_path):
    import patient_context_view as view
    off = _disabled(cfg)
    cloud_policy.stop(ledger)
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    assert _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path)[0] is None
    return {row["project_id"]: set(json.loads(row["content"])) for row in ledger.artifacts(view.RESET_PRIOR_KIND)}


def _published_keys(ledger):
    import patient_context_view as view
    doc, _meta = state._latest(ledger.db, state.KIND, 1)
    return {view.reset_fact_key(fact) for fact in doc["facts"]}


def test_reset_keeps_scan_facts_the_selection_did_not_publish(built, tmp_path):
    ledger, clock, cfg, _ = built
    expected = _published_keys(ledger)
    assert expected
    with ledger.db:   # a selection that kept none of the scanned facts
        for row in ledger.artifacts(state.KIND):
            doc = json.loads(row["content"])
            doc["facts"], doc["summary"] = [], []
            ledger.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                              (json.dumps(doc), row["artifact_id"]))
    assert expected <= _reset_now(ledger, clock, cfg, tmp_path)[1]


def test_reset_keeps_the_facts_of_an_unpublished_context(built, tmp_path):
    ledger, clock, cfg, _ = built
    expected = _published_keys(ledger)
    assert expected
    with ledger.db:
        for row in ledger.artifacts(state.KIND):
            meta = {**json.loads(row["meta"]), "published": False}
            ledger.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                              (json.dumps(meta), row["artifact_id"]))
        # without scans only the unpublished context itself can supply the keys
        ledger.db.execute("DELETE FROM artifacts WHERE kind IN (?,?)", (state.SCAN, state.CHUNK))
    assert expected <= _reset_now(ledger, clock, cfg, tmp_path)[1]


def test_reset_keeps_scan_facts_after_the_cloud_setting_changed(built, tmp_path):
    """The scans were made on the published route: switching cloud off before the reset must not hide them."""
    ledger, clock, cfg, _ = built
    expected = _published_keys(ledger)
    with ledger.db:
        for row in ledger.artifacts(state.KIND):
            doc = json.loads(row["content"])
            doc["facts"], doc["summary"] = [], []
            ledger.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                              (json.dumps(doc), row["artifact_id"]))
    off = _disabled(cfg)
    off["cloud_llm"] = {**off["cloud_llm"], "mode": "off"}
    cloud_policy.stop(ledger)
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    assert _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path)[0] is None
    kept = {row["project_id"]: set(json.loads(row["content"])) for row in ledger.artifacts(view_prior_kind())}
    assert expected <= kept[1]


def view_prior_kind():
    import patient_context_view as view
    return view.RESET_PRIOR_KIND


def test_reset_counts_the_patients_whose_scan_facts_could_not_be_read(built, tmp_path, monkeypatch):
    import cloud_inference
    import patient_context_refresh_sources as sources
    ledger, clock, cfg, _ = built
    expected = _published_keys(ledger)

    def failing(*args, **kwargs):
        raise cloud_inference.CloudFallback("synthetic")

    monkeypatch.setattr(sources, "_snapshot", failing)
    off = _disabled(cfg)
    cloud_policy.stop(ledger)
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    error, extra = _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path)
    assert error is None and extra["erased"]["prior_facts_unreadable"] >= 1
    kept = {row["project_id"]: set(json.loads(row["content"])) for row in ledger.artifacts(view_prior_kind())}
    assert expected <= kept[1], "the published facts are still recorded"


def test_a_second_reset_before_the_rebuild_keeps_the_first_records(built, tmp_path):
    ledger, clock, cfg, _ = built
    first = _reset_now(ledger, clock, cfg, tmp_path)
    assert first[1]
    assert first[1] <= _reset_now(ledger, clock, cfg, tmp_path)[1]


def _reserves(ledger):
    return [row["artifact_id"] for row in ledger.artifacts("cloud_usage_reserve")]


def test_reset_keeps_a_period_budget_that_starts_mid_day(built, tmp_path):
    """A period start between two reservations of one day keeps its count: the carry-over is
    split at that start, so neither the earlier spending joins it nor the later leaves it."""
    ledger, clock, cfg, _ = built
    first = min(_reserves(ledger))
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET created_at=? WHERE artifact_id=?", (clock.wall - 3 * 3600, first))
    start = datetime.fromtimestamp(clock.wall - 3600, cloud_policy.JST).isoformat()
    off = _disabled(cfg)
    off["cloud_llm"] = {**off["cloud_llm"], "window_start": start, "window_budget_usd": 50}
    cloud_policy.stop(ledger)
    usage = SimpleNamespace(db=ledger.db)
    window = cloud_policy.window_spend(usage, off)
    whole_day = copy.deepcopy(off)
    whole_day["cloud_llm"]["window_start"] = datetime.fromtimestamp(clock.wall - 4 * 3600, cloud_policy.JST).isoformat()
    assert 0 < window < cloud_policy.window_spend(usage, whole_day), "the earlier reservation is outside the period"
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    assert sum(entry["requests"] for entry in found["carryover"]) == found["reservations"]
    assert _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path)[0] is None
    assert cloud_policy.window_spend(usage, off) == window, "budget_block_reason compares this same total"


def test_a_changed_spending_total_rolls_the_whole_reset_back(built, tmp_path, monkeypatch):
    ledger, clock, cfg, _ = built
    off = _disabled(cfg)
    cloud_policy.stop(ledger)
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    reserves = _reserves(ledger)
    real, seen = reset._spend, []

    def drifting(db, cfg):
        seen.append(real(db, cfg))
        return seen[-1] if len(seen) == 1 else {**seen[-1], "day": None}

    monkeypatch.setattr(reset, "_spend", drifting)
    assert _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path) == ("spend_changed", {})
    assert _reserves(ledger) == reserves and not ledger.artifacts(cloud_policy.CARRYOVER_KIND)
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg) is not None


def _quality_usage(ledger, created_at, requests):
    meta = {"operation": "patient_summary_quality", "jev_requests": requests, "quality_result": "pass"}
    with ledger.db:
        ledger.db.execute("INSERT INTO artifacts(kind,content,model,meta,created_at) VALUES(?,?,?,?,?)",
                          (semantic_policy.KIND_USAGE, "", "", json.dumps(meta), created_at))


def test_todays_quality_jev_requests_keep_counting_after_the_reset(built, tmp_path):
    ledger, clock, cfg, _ = built
    existing = len(reset._quality_usage(ledger.db))
    _quality_usage(ledger, clock.wall - 1200, 2)
    _quality_usage(ledger, clock.wall - 2 * 86400, 5)
    before = semantic_store.jev_usage_today(ledger)
    off = _disabled(cfg)
    cloud_policy.stop(ledger)
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    assert found["quality_usage_rows"] == existing + 2
    assert _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path)[0] is None
    vars(ledger).pop("_jev_usage_cache", None)
    assert semantic_store.jev_usage_today(ledger) == before
    operations = [json.loads(row["meta"]).get("operation") for row in ledger.artifacts(semantic_policy.KIND_USAGE)]
    assert operations.count(reset.CARRIED_JEV_OPERATION) >= 1
    assert not set(operations) & set(reset.QUALITY_OPERATIONS)


def _claim(ledger, now):
    with ledger.db:
        ledger.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,next_try,created_at,updated_at) "
                          "VALUES(?,?,?,?,?,?)", (state.QUALITY_CLAIM, 1, 0, now + 60, now, now))


GENERATING = 900   # a synthetic patient without records, so its generation slot is the only row


def _reserve_generation(ledger, at, *, lease=60, started=False):
    """A readable generation claim written by the quota module itself, as a worker writes it."""
    with ledger.db:
        ledger.db.execute("BEGIN IMMEDIATE")
        claim = patient_context_quota.reserve_tx(ledger.db, GENERATING, "c" * 64, now=at,
                                                 lease_until=at + lease)["claim"]
        assert claim is not None
        if started:
            assert patient_context_quota.begin_tx(ledger.db, GENERATING, claim, now=at)
    return claim


def _generating(ledger, now):
    _reserve_generation(ledger, now)


def _generating_started(ledger, now):
    _reserve_generation(ledger, now, started=True)


def _generation_row(ledger, content, now, *, meta="{}"):
    with ledger.db:
        ledger.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                          "VALUES(?,?,NULL,?,?,?,?)", (patient_context_quota.KIND, GENERATING, content, "", meta, now))


def _sending(ledger, now):
    reserve = ledger.artifacts("cloud_usage_reserve")[0]
    meta = {**json.loads(reserve["meta"]), "reservation_id": "e" * 32}
    with ledger.db:
        ledger.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                          "VALUES(?,?,?,?,?,?,?)", (reserve["kind"], reserve["project_id"], reserve["message_id"],
                                                    reserve["content"], reserve["model"], json.dumps(meta), now))


@pytest.mark.parametrize("busy, refusal", [(_claim, "quality_claim_active"), (_generating, "generation_active"),
                                           (_generating_started, "generation_active"),
                                           (_sending, "cloud_in_flight")])
def test_reset_refuses_while_work_is_running(built, tmp_path, busy, refusal):
    ledger, clock, cfg, _ = built
    off = _disabled(cfg)
    cloud_policy.stop(ledger)
    busy(ledger, clock.wall)
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    assert found["refusal"] == refusal
    assert _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path)[0] == refusal


def _live_body(now, **change):
    body = {"version": 1, "input_key": "c" * 64, "token": "0" * 32, "state": "started", "reserved_at": now,
            "lease_until": now + 600, "started_at": now, "finished_at": None, "resumes": 0}
    return json.dumps({**body, **change})


# Generation records the quota module cannot read: none holds a processing right, so none is a live claim.
UNREADABLE = {
    "not_json": lambda now: ("x", "{}"),
    "deeply_nested": lambda now: ("[" * 100000, "{}"),
    "not_an_object": lambda now: ("[]", "{}"),
    "missing_fields": lambda now: (json.dumps({"state": "started", "lease_until": now + 600}), "{}"),
    "wrong_types": lambda now: (json.dumps({"state": ["started"], "lease_until": "later"}), "{}"),
    "nonfinite_lease": lambda now: ('{"state":"reserved","lease_until":Infinity}', "{}"),
    "huge_lease": lambda now: (json.dumps({"state": "reserved", "lease_until": 1e18}), "{}"),
    "huge_int_lease": lambda now: (_live_body(now, lease_until=10**400), "{}"),
    "huge_int_reserved": lambda now: (_live_body(now, reserved_at=10**400), "{}"),
    "live_body_wrong_columns": lambda now: (_live_body(now), "null"),
    "live_body_bad_token": lambda now: (_live_body(now, token="unverified"), "{}"),
}


@pytest.mark.parametrize("damage", sorted(UNREADABLE))
def test_unreadable_generation_records_are_erased_not_refused(built, tmp_path, damage):
    """An unreadable generation record is what the reset recovers, never an accounting failure."""
    ledger, clock, cfg, _ = built
    off = _disabled(cfg)
    cloud_policy.stop(ledger)
    content, meta = UNREADABLE[damage](clock.wall)
    _generation_row(ledger, content, clock.wall, meta=meta)
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    assert found["refusal"] is None
    assert found["generation_records"] == {"unreadable": 1, "future": 0}
    assert "lease_until" not in json.dumps(found), "the preview shows counts only"
    error, extra = _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path)
    assert error is None and extra["erased"]["generation_records"] == {"unreadable": 1, "future": 0}
    assert not ledger.artifacts(patient_context_quota.KIND)
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg) is None


def test_a_future_dated_reservation_is_erased_not_refused(built, tmp_path):
    """A claim stamped after the current clock has no processing right now (its owner fence refuses it)."""
    ledger, clock, cfg, _ = built
    off = _disabled(cfg)
    cloud_policy.stop(ledger)
    claim = _reserve_generation(ledger, clock.wall + 3600, lease=7 * 86400)
    assert not patient_context_quota.owns(ledger.db, GENERATING, claim, now=clock.wall)
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    assert found["refusal"] is None and found["generation_records"] == {"unreadable": 0, "future": 1}
    assert _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path)[0] is None
    assert not ledger.artifacts(patient_context_quota.KIND)


def test_a_live_claim_still_refuses_beside_unreadable_records(built, tmp_path):
    ledger, clock, cfg, _ = built
    off = _disabled(cfg)
    cloud_policy.stop(ledger)
    _reserve_generation(ledger, clock.wall - 30, started=True)
    _generation_row(ledger, "x", clock.wall)
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    assert found["refusal"] == "generation_active" and found["generation_records"]["unreadable"] == 1
    assert _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path)[0] == "generation_active"
    assert len(ledger.artifacts(patient_context_quota.KIND)) > 1, "a refused reset changes nothing"
    # the same claim stops refusing once its lease ends, as the quota module itself judges it
    later = clock.wall + 600
    os.utime(tmp_path / BACKUP["name"], (later, later))
    found = reset.preview(ledger.db, cfg=off, now=later)
    assert found["refusal"] is None
    assert _apply(ledger, off, _request(found["preview_hash"]), later, tmp_path)[0] is None


def test_unreadable_generation_records_never_mask_an_accounting_failure(built, tmp_path):
    ledger, clock, cfg, _ = built
    off = _disabled(cfg)
    cloud_policy.stop(ledger)
    _generation_row(ledger, "x", clock.wall)
    reserve = ledger.artifacts("cloud_usage_reserve")[0]
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                          (json.dumps({**json.loads(reserve["meta"]), "reservation_id": 7}), reserve["artifact_id"]))
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    assert found["refusal"] in ("accounting_unverified", "accounting_inconsistent")
    assert found["generation_records"] == {"unreadable": 1, "future": 0}
    assert _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path)[0] == found["refusal"]


def test_the_preview_hash_binds_the_unreadable_generation_count(built, tmp_path):
    """A record that became unreadable after the preview, or a preview without the count, erases nothing."""
    from mcs_util import payload_hash
    ledger, clock, cfg, _ = built
    off = _disabled(cfg)
    cloud_policy.stop(ledger)
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    body = {key: found[key] for key in found if key not in ("refusal", "preview_hash", "generation_records")}
    kinds = {kind: len(ledger.artifacts(kind)) for kind in reset.ERASED_KINDS}
    assert _apply(ledger, off, _request(payload_hash(body)), clock.wall, tmp_path)[0] == "preview_changed"
    victim = ledger.artifacts(patient_context_quota.KIND)[0]
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET content='x' WHERE artifact_id=?", (victim["artifact_id"],))
    assert reset.preview(ledger.db, cfg=off, now=clock.wall)["kinds"] == found["kinds"]
    assert _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path)[0] == "preview_changed"
    assert {kind: len(ledger.artifacts(kind)) for kind in reset.ERASED_KINDS} == kinds


def test_generation_counts_restart_after_the_reset_whether_records_were_readable_or_not(built, tmp_path):
    """The reset carries spending over, not generation slots: every patient's daily count starts again."""
    ledger, clock, cfg, _ = built
    off = _disabled(cfg)
    cloud_policy.stop(ledger)
    _generation_row(ledger, "x", clock.wall - 60)
    readable = {row["project_id"] for row in ledger.artifacts(patient_context_quota.KIND)} - {GENERATING}
    assert readable, "the synthetic build used generation slots"
    blocked = patient_context_quota.inspect(ledger.db, GENERATING, "d" * 64, now=clock.wall)
    assert blocked["used"] == 1 and blocked["unverified_records"] == 1
    before = _usage(ledger)
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    assert _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path)[0] is None
    for pid in readable | {GENERATING}:
        after = patient_context_quota.inspect(ledger.db, pid, "d" * 64, now=clock.wall)
        assert after["allowed"] and after["used"] == 0 and after["unverified_records"] == 0
    assert _usage(ledger) == before, "spending limits keep counting what was spent"


@pytest.mark.parametrize("age, error", [(reset.BACKUP_MAX_AGE_S + 1, "backup_missing"),
                                        (-reset.BACKUP_CLOCK_SKEW_S - 1, "backup_missing"),
                                        (reset.BACKUP_MAX_AGE_S - 60, None)])
def test_backup_must_be_this_resets_recent_copy(built, tmp_path, age, error):
    ledger, clock, cfg, _ = built
    assert reset.validate_operation(_request("c" * 64, backup={**BACKUP, "name": "other.db"}),
                                    set(_request("c" * 64)) - {"reason", "preview_hash", "backup"}) == "bad_backup"
    os.utime(tmp_path / BACKUP["name"], (clock.wall - age, clock.wall - age))
    off = _disabled(cfg)
    cloud_policy.stop(ledger)
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    assert _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path)[0] == error


def test_results_whose_reservation_is_gone_are_erased_too(built, tmp_path):
    ledger, clock, cfg, _ = built
    result = ledger.artifacts("cloud_usage_result")[0]
    meta = {**json.loads(result["meta"]), "reservation_id": "d" * 32}
    with ledger.db:
        ledger.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                          "VALUES(?,?,?,?,?,?,?)", (result["kind"], result["project_id"], result["message_id"],
                                                    result["content"], result["model"], json.dumps(meta),
                                                    clock.wall - 70 * 86400))
    off = _disabled(cfg)
    cloud_policy.stop(ledger)
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    assert found["refusal"] is None
    assert _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path)[0] is None
    assert not ledger.artifacts("cloud_usage_result")


def test_amounts_compare_as_numbers():
    assert reset._amounts({"a_usd": "1.20", "providers": {"x": {"cost_usd": "0"}}, "n": 2}) == \
        {"a_usd": Decimal("1.2"), "providers": {"x": {"cost_usd": Decimal("0.0")}}, "n": 2}


@pytest.fixture
def cli(built, tmp_path, monkeypatch):
    """The cloud-llm CLI over a copy of the built ledger in a synthetic home."""
    import argparse
    import sqlite3
    import cloud_cli
    import local_backup
    import mcs_util
    ledger = built[0]
    root = tmp_path / "synthetic-root"
    (root / "data").mkdir(parents=True)
    with sqlite3.connect(str(root / "data" / "ledger.db")) as target:
        ledger.db.backup(target)
    monkeypatch.setattr(mcs_util, "HOME", str(root))
    monkeypatch.setattr(local_backup, "BACKUP_DIR", str(root / "data" / "backups"))
    monkeypatch.setattr(cloud_cli, "_context_reset_preview", lambda _cfg: {"refusal": None, "preview_hash": "d" * 64})
    parser = argparse.ArgumentParser()
    cloud_cli.add_parsers(parser.add_subparsers(dest="command"))

    def run(*extra):
        args = parser.parse_args(["cloud-llm", "context-reset", "--preview-hash", "d" * 64, "--confirm-human",
                                  "--reason", "synthetic rebuild", *extra])
        return args.fn(args)
    return run, root


def _backups(root):
    folder = root / "data" / "backups"
    return sorted(path.name for path in folder.iterdir()) if folder.exists() else []


@pytest.mark.parametrize("stored, rc, shown", [
    ({"outcome": "applied", "backup": {"name": "earlier"}, "erased": {}}, 0, {"backup": {"name": "earlier"}}),
    ({"outcome": "applied", "control": "paused"}, 1, {"error": "command_id_conflict"}),
])
def test_cli_replays_a_known_command_id_without_a_new_backup(cli, capsys, stored, rc, shown):
    import sqlite3
    run, root = cli
    command_id = "00000000-0000-4000-8000-000000000002"
    with sqlite3.connect(str(root / "data" / "ledger.db")) as db:
        db.execute("INSERT INTO command_receipts(command_id,payload_hash,outcome,receipt_json,processed_at) "
                   "VALUES(?,?,?,?,?)", (command_id, "f" * 64, "applied",
                                         json.dumps({"command_id": command_id, **stored}), 1.0))
    assert run("--command-id", command_id) == rc
    printed = json.loads(capsys.readouterr().out)
    assert {key: printed[key] for key in shown} == shown
    assert _backups(root) == []


def test_cli_removes_the_backup_of_a_rejected_reset(cli, monkeypatch, capsys):
    import mcs_requests
    run, root = cli
    monkeypatch.setattr(mcs_requests, "apply_command", lambda _ledger, req: {
        "command_id": req["command_id"], "outcome": "rejected", "error": "preview_changed"})
    assert run() == 1
    assert json.loads(capsys.readouterr().out)["error"] == "preview_changed"
    assert _backups(root) == []


def test_cli_keeps_the_backup_of_an_applied_reset(cli, monkeypatch, capsys):
    import mcs_requests
    run, root = cli
    monkeypatch.setattr(mcs_requests, "apply_command", lambda _ledger, req: {
        "command_id": req["command_id"], "outcome": "applied", "backup": req["backup"], "erased": {}})
    assert run() == 0
    printed = json.loads(capsys.readouterr().out)
    assert _backups(root) == [printed["backup"]["name"]]


def test_cli_reports_a_reader_limit_apart_from_refusals(built, monkeypatch, tmp_path):
    import sqlite3
    import cloud_cli
    import mcs_util
    ledger = built[0]
    (tmp_path / "home" / "data").mkdir(parents=True)
    with sqlite3.connect(str(tmp_path / "home" / "data" / "ledger.db")) as target:
        ledger.db.backup(target)
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cloud_cli, "RESET_READ_ROWS", 3)
    found = cloud_cli._context_reset_preview(_disabled(built[2]))
    assert found == {"refusal": "read_incomplete", "stop_reason": "reset_budget_exceeded", "preview_hash": None}
