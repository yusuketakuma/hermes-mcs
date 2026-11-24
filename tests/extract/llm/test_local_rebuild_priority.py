"""An approved local rebuild is pending backlog that must not be buried: the
drainer's normal pass may lift it ahead of posted_at order while every
dedicated turn keeps its own domain — oldest/fair/archive turns pick their
own oldest candidate, recent-only turns pick actual arrivals, and the
non-background paths never preempt. The admission set, the permit re-proof,
the local-only dispatch and the paid-history fence all stay in force (fully
synthetic)."""
import json
import time

import extract_llm as worker
import extract_llm_engine
import extract_llm_local_rebuild as local_rebuild
import mcs_queries
from extract_testkit import _hash, _message, _v2_artifact
from test_anthropic_runtime import routed
from test_local_rebuild_execution import _approved, _no_cloud_unit, _paid, _usage

__all__ = ['routed']


def _freshes(ledger, mids, posted_at=None):
    for mid in mids:
        ledger.save_messages([_message(mid, f"完全合成の新着本文{mid}",
                                     posted_at=posted_at)])


def _aged(ledger, mid, posted_at=None):
    """A previously-extracted row left pending by a version bump (the backlog's
    usual non-fresh shape)."""
    ledger.save_messages([_message(mid, f"完全合成の履歴{mid}", posted_at=posted_at)])
    ledger.artifact_add(worker.KIND, "{}", project_id=1, message_id=mid,
                        meta={"hash": _hash(ledger, mid),
                              "extract_version": worker.EXTRACT_VERSION - 1})


def _approved_without_artifact(ledger, monkeypatch, mid=2):
    """An ACTIVE approval whose source never produced an extract artifact:
    paid history alone proves the unit."""
    ledger.save_messages([_message(mid, f"完全合成の記録{mid}")])
    _paid(ledger, monkeypatch, mid)
    import uuid
    import mcs_requests
    plan = local_rebuild.preview(ledger)['plan']
    receipt = mcs_requests.apply_command(ledger, {
        'version': 1, 'cmd': local_rebuild.COMMAND, 'command_id': str(uuid.uuid4()),
        'actor': 'synthetic-human', 'human_confirmed': True, 'project_id': None,
        'reason': '完全合成の確認', 'plan': plan})
    assert receipt['outcome'] == 'applied'
    return local_rebuild.approved_units(ledger.db)[(1, mid)]


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


def test_an_approved_unit_heads_only_a_normal_background_pass(routed, monkeypatch):
    """Opted-in normal pass: the approval lifts. Without the flag the plain
    walk keeps its posted_at order — nothing preempts it."""
    ledger, cfg, calls, reserved, results = routed
    _approved(ledger, monkeypatch)         # mid=1: stale-version final + paid history + receipt
    _freshes(ledger, (2, 3, 4))            # never-extracted: every plain walk prefers them
    rows, _ = worker._select_pending(ledger, 1, None, None, False)
    assert [row["message_id"] for row in rows] != [1]
    rows, _ = worker._select_pending(ledger, 1, None, None, False,
                                   approved_first=True)
    assert [row["message_id"] for row in rows] == [1]
    rows, _ = worker._select_pending(ledger, 3, None, None, False,
                                   approved_first=True)
    assert [row["message_id"] for row in rows] == [1, 4, 3]


def test_an_oldest_turn_keeps_its_oldest_candidate(routed, monkeypatch):
    """An approval never preempts the oldest turn's own selection."""
    ledger, cfg, calls, reserved, results = routed
    _approved(ledger, monkeypatch)                          # mid=1, recent posted_at
    for i, mid in enumerate(range(100, 150)):               # old history, all older than 1
        _aged(ledger, mid, posted_at=1_000_000 + i)
    rows, _ = worker._select_pending(ledger, 1, None, None, True,
                                   fair_oldest=True, approved_first=True)
    assert [row["message_id"] for row in rows] == [100]


def test_a_recent_only_turn_serves_the_arrival_then_the_approval(routed, monkeypatch):
    """A notified approval keeps its arrival rank inside the recent window;
    an unnotified approval never outranks an actual arrival and takes only
    the slots the arrivals leave."""
    ledger, cfg, calls, reserved, results = routed
    _approved(ledger, monkeypatch, mid=2)                     # notified + approved
    _approved(ledger, monkeypatch, mid=3, body="完全合成の別文3")  # approved, never notified
    with ledger.db:
        ledger.db.execute("UPDATE messages SET notified_at=0 WHERE message_id=3")
        ledger.db.execute("UPDATE messages SET notified_at=? WHERE message_id=2", (time.time(),))
    # the window is the caller's narrowed admission — mid 2 keeps its rank,
    # mid 3 is not an arrival and cannot take its slot
    rows, _ = worker._select_pending(ledger, 1, {2}, None, False,
                                     approved_fill=True, approved_adm_ids={2, 3})
    assert [row["message_id"] for row in rows] == [2]
    # once the arrival is served (or absent), the leftover slot is the approval's
    _v2_artifact(ledger, 2, _hash(ledger, 2), {"summary": "合成"})
    rows, _ = worker._select_pending(ledger, 1, set(), None, False,
                                     approved_fill=True, approved_adm_ids={2, 3})
    assert [row["message_id"] for row in rows] == [3]
    # the un-windowed cohort still gates the take: outside it grants nothing
    rows, _ = worker._select_pending(ledger, 1, set(), None, False,
                                     approved_fill=True, approved_adm_ids={2})
    assert rows == []
    rows, _ = worker._select_pending(ledger, 1, set(), None, False,
                                     approved_fill=True, approved_adm_ids=set())
    assert rows == []                        # an explicitly empty cohort stays closed


def test_an_unlimited_cohort_fills_even_with_an_empty_window(routed, monkeypatch):
    """Unlimited admission (adm=None) with no recent arrivals: the take's
    cohort is unlimited too, so an unnotified ACTIVE approval still fills —
    distinct from an unset argument, which inherits the empty window."""
    ledger, cfg, calls, reserved, results = routed
    _approved(ledger, monkeypatch, mid=3, body="完全合成の別文3")
    with ledger.db:
        ledger.db.execute("UPDATE messages SET notified_at=0 WHERE message_id=3")
    rows, _ = worker._select_pending(ledger, 1, set(), None, False,
                                     approved_fill=True, approved_adm_ids=None)
    assert [row["message_id"] for row in rows] == [3]
    rows, _ = worker._select_pending(ledger, 1, set(), None, False,
                                     approved_fill=True)
    assert rows == []                        # default inherits the empty window


def test_the_fill_never_retakes_a_row_the_walk_already_picked(routed, monkeypatch):
    """mid2 is a notified approval inside the window, mid3 an unnotified one:
    two slots must return each exactly once — a fill that ignored the rows
    already selected would return [2, 2] and charge mid2's fresh count twice."""
    ledger, cfg, calls, reserved, results = routed
    _approved(ledger, monkeypatch, mid=2)
    _approved(ledger, monkeypatch, mid=3, body="完全合成の別文3")
    with ledger.db:
        ledger.db.execute("UPDATE messages SET notified_at=0 WHERE message_id=3")
        ledger.db.execute("UPDATE messages SET notified_at=? WHERE message_id=2", (time.time(),))
    rows, fresh = worker._select_pending(ledger, 2, {2}, None, False,
                                         approved_fill=True, approved_adm_ids={2, 3})
    assert [row["message_id"] for row in rows] == [2, 3]
    assert fresh == 0                        # both re-extractions — and neither double-counted


def test_the_fill_take_advances_past_lapsed_approvals(routed, monkeypatch):
    """A run of lapsed approvals must not pin the bounded re-proof forever:
    skipped candidates stay skipped for the rest of the process (keyed by
    command_id), so the next take reaches the ACTIVE tail however much time
    passes between passes."""
    ledger, cfg, calls, reserved, results = routed
    import uuid
    import mcs_requests
    for i, mid in enumerate((10, 11, 12, 13)):
        # newer posted_at than every other row: the lapsed units lead the take
        ledger.save_messages([_message(mid, f"完全合成の失效{mid}",
                                     posted_at=f"2027-01-0{i + 1}T00:00:00+09:00")])
        ledger.artifact_add(worker.KIND, "{}", project_id=1, message_id=mid,
                            meta={"hash": _hash(ledger, mid),
                                  "extract_version": worker.EXTRACT_VERSION - 1})
        _paid(ledger, monkeypatch, mid)
        plan = local_rebuild.preview(ledger)['plan']
        receipt = mcs_requests.apply_command(ledger, {
            'version': 1, 'cmd': local_rebuild.COMMAND, 'command_id': str(uuid.uuid4()),
            'actor': 'synthetic-human', 'human_confirmed': True, 'project_id': None,
            'reason': '完全合成の確認', 'plan': plan})
        assert receipt['outcome'] == 'applied'
        _paid(ledger, monkeypatch, mid)          # usage after approval: proof changed
    _approved(ledger, monkeypatch, mid=1)        # ACTIVE, the walk-order tail
    adm = {1, 10, 11, 12, 13}
    rows, _ = worker._select_pending(ledger, 1, set(), None, False,
                                   approved_fill=True, approved_adm_ids=adm)
    assert rows == []                            # the four lapsed heads were re-proven once
    rows, _ = worker._select_pending(ledger, 1, set(), None, False,
                                   approved_fill=True, approved_adm_ids=adm)
    assert [row["message_id"] for row in rows] == [1]


def test_an_archive_turn_keeps_its_archived_candidate(routed, monkeypatch):
    ledger, cfg, calls, reserved, results = routed
    _approved(ledger, monkeypatch)
    ledger.ensure_patient(2)
    ledger.save_messages([_message(12, "完全合成の履歴12", project_id=2)])
    ledger.artifact_add(worker.KIND, "{}", project_id=2, message_id=12,
                        meta={"hash": _hash(ledger, 12),
                              "extract_version": worker.EXTRACT_VERSION - 1})
    with ledger.db:
        ledger.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=2")
    rows, _ = worker._select_pending(ledger, 1, None, None, True,
                                   archived_first=True, fair_oldest=True,
                                   approved_first=True)
    assert [row["message_id"] for row in rows] == [12]


def test_an_admission_set_that_excludes_the_unit_keeps_it_out(routed, monkeypatch):
    """The priority pass reuses every gate: outside the admitted set it grants nothing."""
    ledger, cfg, calls, reserved, results = routed
    _approved(ledger, monkeypatch)
    _freshes(ledger, (2,))
    rows, _ = worker._select_pending(ledger, 5, {2}, None, False,
                                   approved_first=True)
    assert [row["message_id"] for row in rows] == [2]
    prompts = []
    _local_stub(monkeypatch, prompts)
    report = worker.run_pending(ledger, limit=1, budget_s=180, admitted_ids={2},
                                approved_first=True)
    assert report['done'] == 1 and not _has_current(ledger, 1)


def test_the_approved_unit_runs_local_only_and_lands_adopted(routed, monkeypatch):
    ledger, cfg, calls, reserved, results = routed
    unit = _approved(ledger, monkeypatch)
    _freshes(ledger, (2,))
    before = [tuple(row) for row in _usage(ledger)]
    _no_cloud_unit(monkeypatch)
    prompts = []
    _local_stub(monkeypatch, prompts)
    report = worker.run_pending(ledger, limit=1, budget_s=180,
                                approved_first=True)
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
    report = worker.run_pending(ledger, limit=2, budget_s=180, approved_first=True)
    assert report['done'] == 1 and len(prompts) == 1     # only the fresh row ran
    assert local_rebuild.permit(ledger, 1, 1) == local_rebuild.PROOF_CHANGED
    assert calls == [] and reserved == [] and results == []
    held = ledger.db.execute(
        "SELECT next_try FROM fetch_jobs WHERE kind='extract_claim' AND message_id=1").fetchone()
    assert held and held[0] > time.time() + 60           # bounded hold, not a spin
    assert worker.run_pending(ledger, limit=2, budget_s=180, approved_first=True)['done'] == 0


def test_fresh_selected_follows_the_normal_freshness_rule(routed, monkeypatch):
    """An approved unit counts fresh exactly when it has no extract artifact
    at all — the lift never invents or hides freshness."""
    ledger, cfg, calls, reserved, results = routed
    _approved(ledger, monkeypatch)                  # mid=1: has an old artifact
    _approved_without_artifact(ledger, monkeypatch, 2)   # mid=2: no artifact at all
    rows, fresh_selected = worker._select_pending(ledger, 1, {1}, None, False,
                                                approved_first=True)
    assert [row["message_id"] for row in rows] == [1] and fresh_selected == 0
    rows, fresh_selected = worker._select_pending(ledger, 1, {2}, None, False,
                                                approved_first=True)
    assert [row["message_id"] for row in rows] == [2] and fresh_selected == 1


def test_a_re_pending_adopted_unit_waits_in_normal_order(routed, monkeypatch):
    """Only an ACTIVE approval may head a pass. Once a human's adopted unit
    re-pends — here an extract-version bump ages the adopted final out of
    its contract — its verdict is no longer ACTIVE: it never takes the head
    and the existing local-only machinery holds it under its own verdict."""
    ledger, cfg, calls, reserved, results = routed
    _approved(ledger, monkeypatch)
    _no_cloud_unit(monkeypatch)
    prompts = []
    _local_stub(monkeypatch, prompts)
    assert worker.run_pending(ledger, limit=1, budget_s=180,
                              approved_first=True)['done'] == 1
    assert local_rebuild.permit(ledger, 1, 1) == local_rebuild.ADOPTED
    _freshes(ledger, (2,), posted_at=time.time())
    with ledger.db:
        # a later extract-version bump leaves the adopted final non-current
        ledger.db.execute(
            "UPDATE artifacts SET meta=json_set(meta,'$.extract_version',?) "
            "WHERE kind=? AND message_id=1", (worker.EXTRACT_VERSION - 1, worker.KIND))
        ledger.db.execute("UPDATE messages SET posted_at=? WHERE message_id=1",
                          (time.time() - 1000,))
    verdict = local_rebuild.permit(ledger, 1, 1)
    assert verdict == local_rebuild.PROOF_CHANGED      # aged final: the approval no longer proves
    rows, _ = worker._select_pending(ledger, 1, None, None, False,
                                   approved_first=True)
    assert [row["message_id"] for row in rows] == [2]    # normal order, no head-of-line
    report = worker.run_pending(ledger, limit=2, budget_s=180, approved_first=True)
    assert report['done'] == 1 and len(prompts) == 2     # only the fresh row ran
    assert calls == [] and reserved == [] and results == []
    held = ledger.db.execute(
        "SELECT next_try FROM fetch_jobs WHERE kind='extract_claim' AND message_id=1").fetchone()
    assert held and held[0] > time.time() + 60           # bounded hold, not a spin
