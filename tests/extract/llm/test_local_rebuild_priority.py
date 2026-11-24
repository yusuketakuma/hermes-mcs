"""An approved local rebuild is pending backlog that must not be buried:
selection lifts it ahead of posted_at order and the resident's
new-arrival-priority turn re-admits it, while the admission set, the permit
re-proof, the local-only dispatch and the paid-history fence all stay in
force (fully synthetic)."""
import json
import time

import extract_llm as worker
import extract_llm_engine
import extract_llm_local_rebuild as local_rebuild
import extract_llm_resident as resident
import mcs_queries
from extract_testkit import _message
from test_anthropic_runtime import routed
from test_local_rebuild_execution import _approved, _no_cloud_unit, _paid, _usage

__all__ = ['routed']


def _freshes(ledger, mids):
    for mid in mids:
        ledger.save_messages([_message(mid, f"完全合成の新着本文{mid}")])


def _local_stub(monkeypatch, prompts):
    monkeypatch.setattr(extract_llm_engine, '_llm_call',
                        lambda prompt, **_kw: prompts.append(prompt)
                        or {'symptoms': [], 'summary': '完全合成の要約'})


def _has_current(ledger, mid):
    return ledger.db.execute(
        "SELECT 1 FROM artifacts a JOIN messages m ON a.message_id=m.message_id "
        "WHERE a.kind=? AND a.message_id=? "
        + mcs_queries.current_extract_pred()
        + " AND json_extract(a.meta,'$.extract_version')=? LIMIT 1",
        (worker.KIND, mid, worker.EXTRACT_VERSION)).fetchone() is not None


def test_an_approved_unit_is_selected_before_newer_backlog_rows(routed, monkeypatch):
    ledger, cfg, calls, reserved, results = routed
    _approved(ledger, monkeypatch)         # mid=1: stale-version final + paid history + receipt
    _freshes(ledger, (2, 3, 4))            # never-extracted: every plain walk prefers them
    rows, _ = worker._select_pending(ledger, 1, None, None, False)
    assert [row["message_id"] for row in rows] == [1]
    rows, _ = worker._select_pending(ledger, 3, None, None, False)
    assert [row["message_id"] for row in rows] == [1, 4, 3]


def test_an_admission_set_that_excludes_the_unit_keeps_it_out(routed, monkeypatch):
    """The priority pass reuses every gate: outside the admitted set it grants nothing."""
    ledger, cfg, calls, reserved, results = routed
    _approved(ledger, monkeypatch)
    _freshes(ledger, (2,))
    rows, _ = worker._select_pending(ledger, 5, {2}, None, False)
    assert [row["message_id"] for row in rows] == [2]
    prompts = []
    _local_stub(monkeypatch, prompts)
    report = worker.run_pending(ledger, limit=1, budget_s=180, admitted_ids={2})
    assert report['done'] == 1 and not _has_current(ledger, 1)


def test_the_recent_only_turn_re_admits_the_approved_unit(routed, monkeypatch):
    ledger, cfg, calls, reserved, results = routed
    unit = _approved(ledger, monkeypatch)
    _freshes(ledger, (2,))
    with ledger.db:
        ledger.db.execute("UPDATE messages SET notified_at=0 WHERE message_id=1")
        ledger.db.execute("UPDATE messages SET notified_at=? WHERE message_id=2", (time.time(),))
    adm = resident._priority_admitted(ledger, None)
    assert adm == {1, 2}                   # the approval is re-admitted although nothing notified it
    # A real admission gate still bounds the union: the cohort wins.
    assert resident._priority_admitted(ledger, {2, 9}) == {2}
    before = [tuple(row) for row in _usage(ledger)]
    _no_cloud_unit(monkeypatch)
    prompts = []
    _local_stub(monkeypatch, prompts)
    report = worker.run_pending(ledger, limit=1, budget_s=180, admitted_ids=adm)
    assert report['done'] == 1 and len(prompts) == 1
    assert calls == [] and reserved == [] and results == []
    assert [tuple(row) for row in _usage(ledger)] == before          # paid history byte-for-byte
    final = ledger.db.execute(
        "SELECT meta FROM artifacts WHERE kind=? AND message_id=1 ORDER BY artifact_id DESC LIMIT 1",
        (worker.KIND,)).fetchone()
    assert json.loads(final[0])['local_rebuild'] == local_rebuild.tag(unit)
    assert local_rebuild.permit(ledger, 1, 1) == local_rebuild.ADOPTED


def test_a_proof_changed_unit_fails_closed_without_stalling_the_pass(routed, monkeypatch):
    """A tampered proof is refused in prepare: the unit never dispatches, its
    claim holds, and the rest of the pass still lands."""
    ledger, cfg, calls, reserved, results = routed
    _approved(ledger, monkeypatch)
    _paid(ledger, monkeypatch)             # a newer paid row changes the unit's history proof
    _freshes(ledger, (2,))
    _no_cloud_unit(monkeypatch)
    prompts = []
    _local_stub(monkeypatch, prompts)
    report = worker.run_pending(ledger, limit=2, budget_s=180)
    assert report['done'] == 1 and len(prompts) == 1     # only the fresh row ran
    assert local_rebuild.permit(ledger, 1, 1) == local_rebuild.PROOF_CHANGED
    assert calls == [] and reserved == [] and results == []
    held = ledger.db.execute(
        "SELECT next_try FROM fetch_jobs WHERE kind='extract_claim' AND message_id=1").fetchone()
    assert held and held[0] > time.time() + 60           # bounded hold, not a spin
    assert worker.run_pending(ledger, limit=2, budget_s=180)['done'] == 0
