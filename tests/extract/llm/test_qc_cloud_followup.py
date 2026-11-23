"""QC second attempt: one cloud re-extraction inside the reopened backlog window.

A post whose newest Jev audit still flags the extraction its single local
feedback re-extract produced gets exactly one cloud re-extraction, only
through the existing stored-post lane and all of its gates. Synthetic
fixtures, temp DB and a stubbed cloud transport only.
"""
import json
import time

import pytest

import cloud_policy
import extract_llm as worker
import external_transfer
import semantic_qc
from cloud_policy import block_reason as real_block_reason
from extract_testkit import _hash, _message, _qc_artifact
from test_cloud_after_local import (_cloud_pass, _current, _local_pass, _stale_prior,
                                    local_then_cloud, routed)

__all__ = ['local_then_cloud', 'routed']

FLAG = {"qc": "done", "items": [{"section": "symptoms", "index": 0,
                                 "item": {"text": "完全合成の幻の症状"},
                                 "verdict": "NO_MATCH", "noul": 0.1}]}
NOTE = "symptoms項目「完全合成の幻の症状」は本文に裏付けがありません"
WINDOW = {"provider": "anthropic", "daily_cost_limit_usd": 30, "monthly_cost_limit_usd": 300,
          "provider_budgets": {"anthropic": {"daily_cost_limit_usd": 15, "monthly_cost_limit_usd": 150}},
          "reextract_backlog_until": "2999-01-01T00:00:00+09:00"}


@pytest.fixture
def qc_window(local_then_cloud, monkeypatch):
    """The stored-post lane with its real window, stop switch and scope gates."""
    ledger, cfg, local_calls, cloud_calls, answers = local_then_cloud
    monkeypatch.setattr(cloud_policy, "block_reason", real_block_reason)
    monkeypatch.setenv(cloud_policy.BACKLOG_WORKER_ENV, "1")
    return ledger, cfg, local_calls, cloud_calls, answers


def _flag_current(ledger):
    """Jev audits the newest extraction and still finds an unsupported item."""
    current, _meta = _current(ledger)
    return _qc_artifact(ledger, 1, current["artifact_id"], _hash(ledger), FLAG)


def _locally_fixed(ledger):
    """First pass, a flagging audit, then the single local feedback re-extract."""
    assert _local_pass(ledger)['done'] == 1
    _flag_current(ledger)
    assert _local_pass(ledger)['done'] == 1
    _current_row, meta = _current(ledger)
    assert meta["qc_fix"]["applied"] is True and "attempt" not in meta["qc_fix"]
    assert meta['provider_provenance']['providers'] == ['local']


def _open_window(cfg):
    cfg['cloud_llm'].update(mode='on', **WINDOW)


def _financial_rows(ledger):
    return ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind IN ('cloud_usage_reserve',"
                             "'cloud_usage_result','cloud_usage_disposition')").fetchone()[0]


def _sent_text(body):
    return json.dumps(body, ensure_ascii=False)


def test_still_flagged_local_fix_gets_one_cloud_attempt_with_audit_notes(qc_window):
    ledger, cfg, local_calls, cloud_calls, _answers = qc_window
    _locally_fixed(ledger)
    _flag_current(ledger)
    _open_window(cfg)
    assert _cloud_pass(ledger)['done'] == 1
    assert len(cloud_calls) == 1 and NOTE in _sent_text(cloud_calls[0])
    current, meta = _current(ledger)
    assert meta["qc_fix"]["attempt"] == 2 and meta["qc_fix"]["applied"] is True
    assert meta['provider_provenance']['providers'] == ['anthropic']
    # Still flagged after the cloud attempt: nothing selects it again, in either lane.
    _flag_current(ledger)
    assert _cloud_pass(ledger)['done'] == 0
    report = _local_pass(ledger)
    assert report['selected'] == 0 and report['done'] == 0
    assert len(cloud_calls) == 1 and len(local_calls) == 2
    assert _current(ledger)[0]['artifact_id'] == current['artifact_id']


def test_failed_paid_second_attempt_settles_without_local_rerun(qc_window):
    ledger, cfg, local_calls, cloud_calls, answers = qc_window
    _locally_fixed(ledger)
    _flag_current(ledger)
    before, _meta = _current(ledger)
    _open_window(cfg)
    answers['cloud'] = ["完全合成の不正な形"]   # parses, but not an extraction object
    assert _cloud_pass(ledger)['done'] == 0
    assert cloud_calls and all(NOTE in _sent_text(body) for body in cloud_calls)
    sent = len(cloud_calls)
    current, meta = _current(ledger)
    assert meta["qc_fix"]["attempt"] == 2 and meta["qc_fix"]["applied"] is False
    assert current['content'] == before['content'] and current['artifact_id'] != before['artifact_id']
    assert meta['provider_provenance']['providers'] == ['local']
    _flag_current(ledger)
    assert _cloud_pass(ledger)['done'] == 0 and len(cloud_calls) == sent
    assert _local_pass(ledger)['selected'] == 0 and len(local_calls) == 2


def test_local_fix_without_a_new_audit_is_not_plain_backlog(qc_window):
    ledger, cfg, _local_calls, cloud_calls, _answers = qc_window
    _locally_fixed(ledger)
    _open_window(cfg)
    assert _cloud_pass(ledger)['done'] == 0
    assert not cloud_calls and not _financial_rows(ledger)


def test_cleanly_reaudited_local_fix_is_not_plain_backlog(qc_window):
    ledger, cfg, _local_calls, cloud_calls, _answers = qc_window
    _locally_fixed(ledger)
    current, _meta = _current(ledger)
    _qc_artifact(ledger, 1, current["artifact_id"], _hash(ledger), {"qc": "done", "items": []})
    _open_window(cfg)
    assert _cloud_pass(ledger)['done'] == 0
    assert not cloud_calls and not _financial_rows(ledger)


def test_flag_without_a_local_fix_stays_local_work(qc_window):
    ledger, cfg, local_calls, cloud_calls, _answers = qc_window
    assert _local_pass(ledger)['done'] == 1
    _flag_current(ledger)
    _open_window(cfg)
    assert _cloud_pass(ledger)['done'] == 0
    assert not cloud_calls and not _financial_rows(ledger)
    assert _local_pass(ledger)['done'] == 1 and len(local_calls) == 2


def _no_window(ledger, cfg, monkeypatch):
    cfg['cloud_llm'].pop('reextract_backlog_until')


def _expired_window(ledger, cfg, monkeypatch):
    cfg['cloud_llm']['reextract_backlog_until'] = "2026-01-01T00:00:00+09:00"


def _not_backlog_worker(ledger, cfg, monkeypatch):
    monkeypatch.delenv(cloud_policy.BACKLOG_WORKER_ENV)


def _stopped(ledger, cfg, monkeypatch):
    ledger.artifact_add("cloud_control", "stopped")


def _project_excluded(ledger, cfg, monkeypatch):
    cfg['cloud_llm']['exclude_project_ids'] = [1]


def _project_outside(ledger, cfg, monkeypatch):
    cfg['cloud_llm']['project_ids'] = [2]


def _held_prior(ledger, cfg, monkeypatch):
    current, _meta = _current(ledger)
    with ledger.db:
        worker._record_hold_tx(ledger, dict(ledger.db.execute(
            'SELECT * FROM messages WHERE message_id=1').fetchone()), current, 'protected_loss')


def _aged_out(ledger, cfg, monkeypatch):
    # The synthetic post is an hour old: a shorter QC window leaves it out of scope.
    monkeypatch.setattr(semantic_qc, "QC_REALTIME_MAX_AGE_S", 60)


def _provider_not_approved(ledger, cfg, monkeypatch):
    monkeypatch.setattr(external_transfer, "status", lambda root=None: {"state": "pending", "auto_enable": False})


def _outside_history_scope(ledger, cfg, monkeypatch):
    with ledger.db:
        ledger.db.execute("DELETE FROM artifacts WHERE kind LIKE 'cloud_history%'")


@pytest.mark.parametrize("gate", [_no_window, _expired_window, _not_backlog_worker, _stopped,
                                  _project_excluded, _project_outside, _held_prior, _aged_out,
                                  _provider_not_approved, _outside_history_scope])
def test_every_existing_gate_keeps_the_second_attempt_unsent(qc_window, monkeypatch, gate):
    ledger, cfg, _local_calls, cloud_calls, _answers = qc_window
    _locally_fixed(ledger)
    _flag_current(ledger)
    _open_window(cfg)
    gate(ledger, cfg, monkeypatch)
    before, _meta = _current(ledger)
    assert _cloud_pass(ledger)['done'] == 0
    assert not cloud_calls and not _financial_rows(ledger), 'nothing selected, reserved or sent'
    current, meta = _current(ledger)
    assert current['artifact_id'] == before['artifact_id'] and "attempt" not in meta["qc_fix"]


def test_a_new_body_or_generation_restarts_the_sequence(qc_window):
    ledger, cfg, local_calls, cloud_calls, _answers = qc_window
    _locally_fixed(ledger)
    _flag_current(ledger)
    _open_window(cfg)
    assert _cloud_pass(ledger)['done'] == 1
    _flag_current(ledger)
    cfg['cloud_llm']['mode'] = 'off'
    # A new generation re-extracts locally; its own audit earns one local fix again.
    current, _meta = _current(ledger)
    _stale_prior(ledger, current['artifact_id'])
    assert _local_pass(ledger)['done'] == 1
    _flag_current(ledger)
    assert _local_pass(ledger)['done'] == 1
    _current_row, meta = _current(ledger)
    assert meta["qc_fix"]["applied"] is True and "attempt" not in meta["qc_fix"]
    # So does a changed body.
    ledger.save_messages([_message(body='完全合成の訂正後の経過記録です。')])
    assert _local_pass(ledger)['done'] == 1
    _flag_current(ledger)
    assert _local_pass(ledger)['done'] == 1
    _current_row, meta = _current(ledger)
    assert meta["qc_fix"]["applied"] is True and "attempt" not in meta["qc_fix"]
    assert len(cloud_calls) == 1 and len(local_calls) == 6


def test_claim_rereads_the_lane_filter_and_the_second_attempt(qc_window, monkeypatch):
    ledger, cfg, _local_calls, _cloud_calls, _answers = qc_window
    _locally_fixed(ledger)
    flagged_source, _meta = _current(ledger)
    _flag_current(ledger)
    _open_window(cfg)
    rows, _ = worker._select_pending(ledger, 5, None, None, False, cloud_only=True)
    assert [(row["message_id"], row["qc_cloud"], row["qc_src"]) for row in rows] == [
        (1, flagged_source['artifact_id'], flagged_source['artifact_id'])]
    assert all(row["qc_cloud"] is None for row in worker._select_pending(ledger, 5, None, None, False)[0])
    jobs, _skipped, blocked = worker._prepare_jobs(ledger, rows, ("", "synthetic-local"), False,
                                                   background=True, cloud_only=True, load_checkpoints=False)
    assert blocked == 0 and len(jobs) == 1
    row = jobs[0][0]
    assert row["_cloud_mode"] == "on" and row["_required_cloud_contract"]
    # The post leaves the QC window between selection and claim: the claim refuses it.
    with monkeypatch.context() as aged:
        aged.setattr(semantic_qc, "QC_REALTIME_MAX_AGE_S", 60)
        assert worker._claim(ledger, dict(row)) is None
    assert not ledger.db.execute("SELECT 1 FROM fetch_jobs WHERE kind='extract_claim'").fetchone()
    assert type(worker._claim(ledger, dict(row))) is float


def test_lane_filter_admits_only_a_reaudited_flag(qc_window):
    """The shared filter the lane and the cloud estimate both apply."""
    ledger, cfg, _local_calls, _cloud_calls, _answers = qc_window
    _locally_fixed(ledger)
    lane, params = worker.cloud_lane_sql(time.time())
    query = worker._selection_sql(ledger.db, "DESC", lane, cloud_followup=True)
    args = (worker.KIND, worker.EXTRACT_VERSION, *params, None, None, None, time.time(),
            worker.KIND, worker.EXTRACT_VERSION, time.time(), 10)
    assert ledger.db.execute(query, args).fetchall() == []
    _flag_current(ledger)
    assert [row["message_id"] for row in ledger.db.execute(query, args)] == [1]



def _seal_answer_as_protected_loss(monkeypatch):
    """The paid second-attempt answer would drop a protected fact: it is held, not adopted."""
    import extract_llm_store
    quality = extract_llm_store.extract_quality
    originals = (quality.protected_lost, quality.protected_loss_classes)
    monkeypatch.setattr(quality, "protected_lost", lambda old, new, body: {"meds": 1} if old != new else {})
    monkeypatch.setattr(quality, "protected_loss_classes", lambda *args, **kwargs: {"meds": 1})

    def restore():
        monkeypatch.setattr(quality, "protected_lost", originals[0])
        monkeypatch.setattr(quality, "protected_loss_classes", originals[1])
    return restore


def _reservations(ledger):
    return ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='cloud_usage_reserve'").fetchone()[0]


def test_a_held_second_attempt_is_never_resent_after_its_hold_is_released(qc_window, monkeypatch):
    import extract_llm_qc
    ledger, cfg, _local_calls, cloud_calls, answers = qc_window
    _locally_fixed(ledger)
    _flag_current(ledger)
    before, _meta = _current(ledger)
    _open_window(cfg)
    answers['cloud'] = {'symptoms': [], 'summary': '完全合成の別の要約'}
    restore = _seal_answer_as_protected_loss(monkeypatch)
    _cloud_pass(ledger)
    assert len(cloud_calls) == 1 and _reservations(ledger) == 1
    assert len(ledger.artifacts(worker.UPGRADE_KIND)) == 1, "the paid answer was held"
    current, meta = _current(ledger)
    assert current['artifact_id'] == before['artifact_id'] and "attempt" not in meta["qc_fix"]
    # A code update releases the automatic hold; the reservation still records the attempt.
    restore()
    assert ledger.db.execute(f"SELECT EXISTS ({worker._hold_sql()}) FROM messages m").fetchone()[0] == 1
    monkeypatch.setattr(extract_llm_qc, "_upgrade_contract", lambda: "synthetic-later-contract")
    assert ledger.db.execute(f"SELECT EXISTS ({worker._hold_sql()}) FROM messages m").fetchone()[0] == 0
    assert _cloud_pass(ledger)['done'] == 0
    assert len(cloud_calls) == 1 and _reservations(ledger) == 1
    rows, _ = worker._select_pending(ledger, 5, None, None, False, cloud_only=True)
    assert rows == []


def test_a_rejected_commit_after_the_paid_answer_is_never_resent(qc_window, monkeypatch):
    ledger, cfg, _local_calls, cloud_calls, _answers = qc_window
    _locally_fixed(ledger)
    _flag_current(ledger)
    before, _meta = _current(ledger)
    _open_window(cfg)
    import extract_llm_runner
    original = extract_llm_runner._commit_extraction_atomic
    rejected = []

    def reject_once(ledger_, r, *args, **kwargs):
        if not rejected:
            rejected.append(r["message_id"])
            return worker._commit_rejected(r, "context")
        return original(ledger_, r, *args, **kwargs)
    monkeypatch.setattr(extract_llm_runner, "_commit_extraction_atomic", reject_once)
    assert _cloud_pass(ledger)['done'] == 0
    assert rejected == [1] and len(cloud_calls) == 1 and _reservations(ledger) == 1
    with ledger.db:   # the kept claim expires before the next passes
        ledger.db.execute("DELETE FROM fetch_jobs WHERE kind='extract_claim'")
    for _ in range(2):
        assert _cloud_pass(ledger)['done'] == 0
    assert len(cloud_calls) == 1 and _reservations(ledger) == 1
    assert _current(ledger)[0]['artifact_id'] == before['artifact_id']


def test_a_source_already_reextracted_on_the_cloud_gets_no_qc_second_send(qc_window):
    ledger, cfg, local_calls, cloud_calls, _answers = qc_window
    assert _local_pass(ledger)['done'] == 1
    _open_window(cfg)
    assert _cloud_pass(ledger)['done'] == 1, "plain stored-post backlog inside the window"
    assert len(cloud_calls) == 1
    _flag_current(ledger)
    cfg['cloud_llm']['mode'] = 'off'
    assert _local_pass(ledger)['done'] == 1
    _current_row, meta = _current(ledger)
    assert meta["qc_fix"]["applied"] is True and meta['provider_provenance']['providers'] == ['local']
    _flag_current(ledger)
    cfg['cloud_llm']['mode'] = 'on'
    assert _cloud_pass(ledger)['done'] == 0
    assert len(cloud_calls) == 1 and len(local_calls) == 2


def test_human_reports_and_generation_upgrades_still_reach_a_settled_qc_row(qc_window):
    ledger, cfg, local_calls, cloud_calls, _answers = qc_window
    _locally_fixed(ledger)
    fixed, _meta = _current(ledger)
    ledger.artifact_add("extract_feedback_v1", json.dumps({"artifact_id": fixed['artifact_id']}),
                        project_id=1, message_id=1, meta={})
    _open_window(cfg)
    assert _cloud_pass(ledger)['done'] == 0 and not cloud_calls, "a human report is local work"
    cfg['cloud_llm']['mode'] = 'off'
    assert _local_pass(ledger)['done'] == 1 and len(local_calls) == 3
    _current_row, meta = _current(ledger)
    assert meta["human_fix"]["applied"] is True
    # A generation upgrade of a settled QC row is ordinary stored-post backlog again.
    _stale_prior(ledger, _current(ledger)[0]['artifact_id'])
    cfg['cloud_llm']['mode'] = 'on'
    assert _cloud_pass(ledger)['done'] == 1 and len(cloud_calls) == 1
    assert _current(ledger)[1]['provider_provenance']['providers'] == ['anthropic']


def test_reservation_counts_only_for_the_current_source(tmp_path):
    from extract_testkit import _ledger
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(body='完全合成の記録です。')])
        sql = f"SELECT {worker.cloud_reextract_reserved_sql()} FROM messages m WHERE m.message_id=1"
        now = time.time()

        def reserve(at, route='reextract', message_id=1):
            with db.db:
                db.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at)"
                              " VALUES('cloud_usage_reserve',1,?,'{}','m',?,?)",
                              (message_id, json.dumps({"route": route}), at))
        with db.db:
            db.db.execute("UPDATE messages SET first_seen=? WHERE message_id=1", (now - 300,))
        reserve(now - 400)                      # before this source was first seen
        reserve(now - 100, route='patient_context')
        assert db.db.execute(sql).fetchone()[0] == 0
        reserve(now - 100)
        assert db.db.execute(sql).fetchone()[0] == 1
        db.save_messages([_message(body='完全合成の訂正後の記録です。')])
        assert db.db.execute(sql).fetchone()[0] == 0, "a changed source earns its own attempt"
        with db.db:
            db.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at)"
                          " VALUES('cloud_usage_reserve',1,1,'{}','m','not json',?)", (time.time() + 1,))
        assert db.db.execute(sql).fetchone()[0] == 1, "an unreadable reservation counts"
    finally:
        db.close()
