"""Synthetic acceptance of the receipted patient-context reset: erase, carry spending over, rebuild."""
import copy
import json
from types import SimpleNamespace

import pytest

import cloud_policy
import patient_context_refresh_readiness as readiness
import patient_context_reset as reset
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
    (tmp_path / BACKUP["name"]).write_bytes(b"backup!")
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


def clock_runtime(ledger, clock):
    return ledger, clock, []
