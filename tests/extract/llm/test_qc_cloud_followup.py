"""QC second attempt: one cloud re-extraction inside the reopened backlog window.

A post whose newest Jev audit still flags the extraction its single local
feedback re-extract produced gets exactly one cloud re-extraction attempt per
post body (its chunks resume; nothing is re-sent), only through the existing
stored-post lane and all of its gates. Synthetic fixtures, temp DB and a
stubbed cloud transport only.
"""
import dataclasses
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


def _attempt_records(ledger):
    return [json.loads(a['content']) for a in ledger.artifacts(worker.CLOUD_ATTEMPT_KIND)]


def _attempts(ledger):
    """The body's cloud attempt as (purpose, state) steps; each paid request of an open attempt has its own record."""
    steps = [(row['purpose'], row['state']) for row in _attempt_records(ledger)]
    return [step for i, step in enumerate(steps) if i == 0 or step != steps[i - 1]]


def _prune_accounting(ledger):
    """What maintenance does to cloud usage rows after CLOUD_USAGE_KEEP_S."""
    with ledger.db:
        ledger.db.execute("DELETE FROM artifacts WHERE kind IN ('cloud_usage_reserve','cloud_usage_result',"
                          "'cloud_usage_disposition')")


def _still_flagged(ledger, artifact_id):
    """The newest current audit still flags this extraction (no re-audit needed)."""
    flagged = ledger.db.execute(f"SELECT ({worker._qc_flagged_sql()}) FROM messages m WHERE m.message_id=1")
    return flagged.fetchone()[0] == artifact_id


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
    assert _attempts(ledger) == [('qc', 'open'), ('qc', 'closed')]
    # Still flagged after the cloud attempt: nothing selects it again, in either lane.
    _flag_current(ledger)
    assert _cloud_pass(ledger)['done'] == 0
    report = _local_pass(ledger)
    assert report['selected'] == 0 and report['done'] == 0
    assert len(cloud_calls) == 1 and len(local_calls) == 2
    assert _current(ledger)[0]['artifact_id'] == current['artifact_id']


def test_failed_paid_second_attempt_ends_without_restoring_or_resending(qc_window):
    ledger, cfg, local_calls, cloud_calls, answers = qc_window
    _locally_fixed(ledger)
    _flag_current(ledger)
    before, _meta = _current(ledger)
    _open_window(cfg)
    answers['cloud'] = ["完全合成の不正な形"]   # parses, but not an extraction object
    assert _cloud_pass(ledger)['done'] == 0
    assert cloud_calls and all(NOTE in _sent_text(body) for body in cloud_calls)
    sent = len(cloud_calls)
    # Nothing is re-stored: the audited extraction and its flag stay current.
    current, meta = _current(ledger)
    assert current['artifact_id'] == before['artifact_id'] and "attempt" not in meta["qc_fix"]
    assert _still_flagged(ledger, before['artifact_id'])
    assert _attempts(ledger) == [('qc', 'open'), ('qc', 'closed')]
    # The ended attempt outlives the accounting rows maintenance prunes.
    _prune_accounting(ledger)
    with ledger.db:
        ledger.db.execute("DELETE FROM fetch_jobs WHERE kind='extract_claim'")
    assert worker._select_pending(ledger, 5, None, None, False, cloud_only=True)[0] == []
    assert _cloud_pass(ledger)['done'] == 0 and len(cloud_calls) == sent
    assert _local_pass(ledger)['selected'] == 0 and len(local_calls) == 2
    assert _current(ledger)[0]['artifact_id'] == before['artifact_id']


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


def _revise(ledger, message, receipt):
    """Store a revised post as already-collected history inside the stored-post scope."""
    ledger.save_messages([message])
    with ledger.db:
        ledger.db.execute('UPDATE message_revisions SET observed_at=?', (time.time() - 1200,))
        cloud_policy.capture_history_tx(ledger.db, receipt, time.time())


def _reflag_after_local_fix(ledger, cfg):
    cfg['cloud_llm']['mode'] = 'off'
    assert _local_pass(ledger)['done'] == 1
    _flag_current(ledger)
    assert _local_pass(ledger)['done'] == 1
    _flag_current(ledger)
    cfg['cloud_llm']['mode'] = 'on'


def test_a_metadata_revision_earns_no_new_attempt_but_a_body_edit_does(qc_window):
    ledger, cfg, _local_calls, cloud_calls, answers = qc_window
    _locally_fixed(ledger)
    _flag_current(ledger)
    flagged, _meta = _current(ledger)
    _open_window(cfg)
    answers['cloud'] = ["完全合成の不正な形"]   # the paid attempt fails; the flag stays current
    assert _cloud_pass(ledger)['done'] == 0
    sent = len(cloud_calls)
    answers['cloud'] = {'symptoms': []}
    # A later source revision of the same body (metadata only) moves the
    # revision boundary but not the body: the attempt stays spent.
    with ledger.db:
        ledger.db.execute("INSERT INTO message_revisions(message_id,seq,content_hash,prev_content_hash,"
                          "body_state,observed_at) SELECT m.message_id,(SELECT COALESCE(MAX(seq),0)+1 "
                          "FROM message_revisions),m.content_hash,m.content_hash,'full',? "
                          "FROM messages m WHERE m.message_id=1", (time.time(),))
        ledger.db.execute("DELETE FROM fetch_jobs WHERE kind='extract_claim'")
    assert _still_flagged(ledger, flagged['artifact_id'])
    assert worker._select_pending(ledger, 5, None, None, False, cloud_only=True)[0] == []
    assert _cloud_pass(ledger)['done'] == 0 and len(cloud_calls) == sent, "same body, same single attempt"
    with ledger.db:   # the earlier body's sends precede the (backdated) edit below
        ledger.db.execute("UPDATE artifacts SET created_at=created_at-3600 WHERE kind LIKE 'cloud_usage_%'")
    _revise(ledger, _message(body='完全合成の訂正後の経過記録です。'), 'synthetic-body-edit')
    _reflag_after_local_fix(ledger, cfg)
    assert _cloud_pass(ledger)['done'] == 1 and len(cloud_calls) == sent + 1, "a changed body earns its own attempt"
    assert _current(ledger)[1]["qc_fix"]["attempt"] == 2


def _echo_providers(qc_window, monkeypatch):
    """Both providers keyed and approved, each answering under the model it was asked for -> OpenAI request bodies."""
    import openai_client
    from test_anthropic_runtime import AnthropicClient
    _ledger, _cfg, _local_calls, cloud_calls, answers = qc_window
    monkeypatch.setattr(cloud_policy, "provider_key", lambda name, root=None: "synthetic-key")

    def anthropic(self, method, body, timeout, **kwargs):
        cloud_calls.append(body)
        return 200, {}, json.dumps({'model': body['model'], 'stop_reason': 'end_turn',
            'content': [{'type': 'text', 'text': json.dumps(answers['cloud'], ensure_ascii=False)}],
            'usage': {'input_tokens': 10, 'output_tokens': 2}}).encode()
    openai_calls = []

    def openai(self, method, body, timeout, **kwargs):
        openai_calls.append(body)
        return 200, {}, json.dumps({'status': 'completed', 'model': body['model'], 'service_tier': 'default',
            'output': [{'type': 'message', 'role': 'assistant', 'content': [
                {'type': 'output_text', 'text': json.dumps(answers['cloud'], ensure_ascii=False)}]}],
            'usage': {'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12,
                      'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}}}).encode()
    monkeypatch.setattr(AnthropicClient, 'request', anthropic)
    monkeypatch.setattr(openai_client.OpenAIClient, 'request', openai)
    return openai_calls


def _both_providers(cfg, order=("anthropic", "openai")):
    """The window with automatic provider choice over both providers, each under its own caps."""
    cfg['cloud_llm'].update(provider="auto", provider_order=list(order), provider_budgets={
        name: {"daily_cost_limit_usd": 15, "monthly_cost_limit_usd": 150} for name in ("anthropic", "openai")})


def _long_flagged_post(ledger):
    body = "".join(f"完全合成の長い経過記録{i:03d}です。" for i in range(300))
    assert len(body) > worker._CHUNK_SIZE
    _revise(ledger, _message(body=body), 'synthetic-long-body')
    _locally_fixed(ledger)
    _flag_current(ledger)
    return _current(ledger)[0]


def _capped_after_the_first_chunk(qc_window, monkeypatch, *, providers=False):
    """A long flagged post whose second attempt sends chunk 1, then meets the real daily request cap -> lift."""
    ledger, cfg, _local_calls, cloud_calls, _answers = qc_window
    openai_calls = _echo_providers(qc_window, monkeypatch)
    before = _long_flagged_post(ledger)
    _open_window(cfg)
    if providers:
        _both_providers(cfg)
    cfg['cloud_llm']['daily_request_budget'] = 1   # the first chunk fits, the next one is refused before sending
    assert _cloud_pass(ledger)['done'] == 0
    assert len(cloud_calls) == 1 and _reservations(ledger) == 1 and not openai_calls
    assert _attempts(ledger) == [('qc', 'open')], "a cap refusal leaves the attempt to resume"
    current, meta = _current(ledger)
    assert current['artifact_id'] == before['artifact_id'] and "attempt" not in meta["qc_fix"]
    assert _still_flagged(ledger, before['artifact_id'])
    with ledger.db:
        ledger.db.execute("DELETE FROM fetch_jobs WHERE kind='extract_claim'")

    def lift():
        cfg['cloud_llm'].pop('daily_request_budget')
        with ledger.db:
            ledger.db.execute("DELETE FROM fetch_jobs WHERE kind='extract_claim'")
    return openai_calls, lift


def _request_fps(ledger):
    return [json.loads(row['meta'])['request_fp'] for row in ledger.artifacts('cloud_usage_reserve')]


def test_a_cap_blocked_chunk_resumes_the_same_attempt_without_resending(qc_window, monkeypatch):
    ledger, _cfg, _local_calls, cloud_calls, _answers = qc_window
    _openai_calls, lift = _capped_after_the_first_chunk(qc_window, monkeypatch)
    assert _cloud_pass(ledger)['done'] == 0 and len(cloud_calls) == 1, "still capped: it waits, open"
    assert _attempts(ledger) == [('qc', 'open')]
    lift()
    assert _cloud_pass(ledger)['done'] == 1
    sent = [_sent_text(call) for call in cloud_calls]
    assert len(sent) >= 2 and len(set(sent)) == len(sent), "the saved first chunk is reused, not resent"
    assert _reservations(ledger) == len(sent) == len(set(_request_fps(ledger)))
    assert _current(ledger)[1]["qc_fix"]["attempt"] == 2
    assert _attempts(ledger) == [('qc', 'open'), ('qc', 'closed')]
    assert sum(row['state'] == 'open' for row in _attempt_records(ledger)) == len(sent), "one record per paid request"
    _flag_current(ledger)
    assert _cloud_pass(ledger)['done'] == 0 and len(cloud_calls) == len(sent)


def test_a_flipped_provider_preference_resumes_on_the_original_provider_once_its_cap_clears(qc_window, monkeypatch):
    ledger, cfg, _local_calls, cloud_calls, _answers = qc_window
    openai_calls, lift = _capped_after_the_first_chunk(qc_window, monkeypatch, providers=True)
    pin = json.loads(ledger.artifacts(worker.CLOUD_ATTEMPT_KIND)[0]['content'])['contract']
    assert pin['backlog']['provider'] == 'anthropic' and cloud_calls[0]['model'] == pin['backlog']['model']
    _both_providers(cfg, order=("openai", "anthropic"))
    preferred = cloud_policy.resolve_profile(cfg, route="reextract", purpose="backlog", root=worker.HOME,
                                             ledger=ledger, ordered=True)[0]
    assert preferred['provider'] == 'openai', "real ordered resolution now prefers the other provider"
    assert _cloud_pass(ledger)['done'] == 0, "the pinned provider is still capped: it waits"
    assert len(cloud_calls) == 1 and not openai_calls and _attempts(ledger) == [('qc', 'open')]
    lift()
    assert _cloud_pass(ledger)['done'] == 1
    assert not openai_calls, "nothing moves to the preferred provider"
    assert {call['model'] for call in cloud_calls} == {pin['backlog']['model']}
    assert _reservations(ledger) == len(cloud_calls) == len(set(_request_fps(ledger))), (
        "no chunk is reserved twice")
    assert _current(ledger)[1]['provider_provenance']['providers'] == ['anthropic']
    assert _attempts(ledger) == [('qc', 'open'), ('qc', 'closed')]


def test_a_single_provider_pin_without_the_original_provider_ends_the_attempt_unsent(qc_window, monkeypatch):
    ledger, cfg, _local_calls, cloud_calls, _answers = qc_window
    openai_calls, lift = _capped_after_the_first_chunk(qc_window, monkeypatch, providers=True)
    cfg['cloud_llm']['provider'] = 'openai'   # anthropic stays listed in provider_order
    assert cfg['cloud_llm']['provider_order'] == ['anthropic', 'openai']
    lift()
    assert _cloud_pass(ledger)['done'] == 0
    assert len(cloud_calls) == 1 and not openai_calls and _reservations(ledger) == 1, "nothing is sent"
    assert _attempts(ledger) == [('qc', 'open'), ('qc', 'closed')], "the attempt ends (確認候補)"
    assert worker._select_pending(ledger, 5, None, None, False, cloud_only=True)[0] == []
    assert _cloud_pass(ledger)['done'] == 0 and len(cloud_calls) == 1 and not openai_calls


def test_a_cap_refusal_before_the_first_request_leaves_the_post_eligible_under_a_new_model(qc_window, monkeypatch):
    ledger, cfg, _local_calls, cloud_calls, _answers = qc_window
    openai_calls = _echo_providers(qc_window, monkeypatch)
    _locally_fixed(ledger)
    _flag_current(ledger)
    _open_window(cfg)
    first = cloud_policy.resolve_profile(cfg, route="reextract", purpose="backlog", root=worker.HOME)[0]
    # The provider cap admits the profile's bare maximum (the prepare check) but not the real request.
    bare = cloud_policy.max_cost(first['max_tokens'], input_bound=cloud_policy.INPUT_RESERVATION_MARGIN,
                                 model=first['model'])
    cfg['cloud_llm']['provider_budgets']['anthropic']['daily_cost_limit_usd'] = float(bare) * 1.01
    assert _cloud_pass(ledger)['done'] == 0
    assert not cloud_calls and not _financial_rows(ledger), "refused before any reservation"
    assert not ledger.artifacts(worker.CLOUD_ATTEMPT_KIND), "and nothing records an attempt"
    cfg['cloud_llm'].update(provider_profiles={'anthropic': {'backlog': 'sonnet-high'}})
    cfg['cloud_llm']['provider_budgets']['anthropic']['daily_cost_limit_usd'] = 15
    with ledger.db:
        ledger.db.execute("DELETE FROM fetch_jobs WHERE kind='extract_claim'")
    assert _cloud_pass(ledger)['done'] == 1
    assert [call['model'] for call in cloud_calls] == ['claude-sonnet-5-5'] and not openai_calls
    assert _reservations(ledger) == 1
    records = _attempt_records(ledger)
    assert [row['state'] for row in records] == ['open', 'closed']
    assert records[0]['contract']['backlog']['model'] == 'claude-sonnet-5-5'
    assert _current(ledger)[1]["qc_fix"]["attempt"] == 2


def test_a_pinned_quality_repair_keeps_its_contract_and_is_never_paid_twice(qc_window, monkeypatch):
    import cloud_inference
    ledger, cfg, _local_calls, cloud_calls, _answers = qc_window
    openai_calls = _echo_providers(qc_window, monkeypatch)
    _open_window(cfg)
    _both_providers(cfg)
    selection = cloud_policy.resolve_profile(cfg, route="reextract", purpose="backlog", root=worker.HOME,
                                             ledger=ledger, ordered=True)[0]
    pin = cloud_policy.route_contract(cfg, selection, route="reextract", purpose="backlog", root=worker.HOME)
    assert pin['quality_repair']['provider'] == 'anthropic'

    def repair(unit_pin=pin):
        unit = cloud_inference.CloudInferenceUnit(ledger, cfg, route="reextract", project_id=1, message_id=1,
            unit_key="1", deadline=time.monotonic() + 180, current=lambda: True,
            dispatch=lambda fn, **kwargs: fn(), owner={}, selection=selection, input_binding="0" * 64,
            pin=unit_pin)
        return unit.spawn_quality_repair()
    prompt = "完全合成の修復依頼です。"
    first = repair()
    assert cloud_policy.selection_contract(first.selection) == pin['quality_repair']
    assert first(prompt, deadline=time.monotonic() + 60, max_tokens=64)['text']
    assert len(cloud_calls) == 1 and _reservations(ledger) == 1
    # The preference flips: an unpinned repair would now go to the other provider...
    _both_providers(cfg, order=("openai", "anthropic"))
    assert repair(None).selection['provider'] == 'openai'
    # ...the pinned one keeps its contract and reuses its paid answer.
    again = repair()
    assert cloud_policy.selection_contract(again.selection) == pin['quality_repair']
    assert again(prompt, deadline=time.monotonic() + 60, max_tokens=64)['reused_response'] is True
    assert len(cloud_calls) == 1 and _reservations(ledger) == 1 and not openai_calls
    # A changed repair profile no longer offers the contract, for the repair and the attempt it belongs to.
    cfg['cloud_llm']['provider_profiles'] = {'anthropic': {'quality_repair': 'haiku-medium'}}
    assert [cloud_policy.resolve_contract(cfg, pin, route="reextract", purpose=purpose, root=worker.HOME).state
            for purpose in ("backlog", "quality_repair")] == ['gone', 'gone']
    with pytest.raises(cloud_inference.CloudFallback):
        repair()("完全合成の別の修復依頼です。", deadline=time.monotonic() + 60, max_tokens=64)
    assert len(cloud_calls) == 1 and _reservations(ledger) == 1 and not openai_calls


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
    # ...and leaves it before its first paid request: that request's reservation refuses it.
    import cloud_client
    import extract_llm_store

    def reserve():
        with ledger.db:
            ledger.db.execute("BEGIN IMMEDIATE")
            return extract_llm_store._record_cloud_attempt_tx(ledger, row, {"reservation_id": "b" * 32})
    with monkeypatch.context() as aged:
        aged.setattr(semantic_qc, "QC_REALTIME_MAX_AGE_S", 60)
        with pytest.raises(cloud_client.CloudError):
            reserve()
    assert not ledger.artifacts(worker.CLOUD_ATTEMPT_KIND)
    assert reserve() == 1
    assert _attempts(ledger) == [('qc', 'open')]
    assert json.loads(ledger.artifacts(worker.CLOUD_ATTEMPT_KIND)[0]['content'])['contract'] == row['_cloud_pin']


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
    assert _attempts(ledger) == [('qc', 'open'), ('qc', 'closed')]
    _prune_accounting(ledger)
    assert worker._select_pending(ledger, 5, None, None, False, cloud_only=True)[0] == []


def test_a_rejected_commit_resumes_with_the_paid_answer_and_never_resends(qc_window, monkeypatch):
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
    assert _attempts(ledger) == [('qc', 'open')], "the unfinished attempt stays open"
    # The same attempt resumes and adopts the stored paid answer; nothing is sent again.
    assert _cloud_pass(ledger)['done'] == 1
    assert len(cloud_calls) == 1 and _reservations(ledger) == 1
    current, meta = _current(ledger)
    assert current['artifact_id'] != before['artifact_id'] and meta["qc_fix"]["attempt"] == 2
    assert _attempts(ledger) == [('qc', 'open'), ('qc', 'closed')]
    _flag_current(ledger)
    assert _cloud_pass(ledger)['done'] == 0 and len(cloud_calls) == 1


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
    assert _attempts(ledger) == [('backlog', 'closed')]
    _prune_accounting(ledger)
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


def test_one_attempt_per_body_survives_metadata_revisions_and_restarts_on_a_body_edit(tmp_path):
    from extract_testkit import _ledger
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(body='完全合成の記録です。')])
        spent = f"SELECT {worker.cloud_attempt_spent_sql('m', '?')} FROM messages m WHERE m.message_id=1"
        now = time.time()

        def is_spent(src=7):
            return db.db.execute(spent, (src,)).fetchone()[0]

        def revision():
            return db.db.execute(f"SELECT {worker.source_revision_sql()} FROM messages m").fetchone()[0]

        def insert(kind, content, meta, at):
            with db.db:
                db.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at)"
                              " VALUES(?,1,1,?,'m',?,?)", (kind, content, meta, at))

        def reserve(at, route='reextract', reservation_id=None):
            insert('cloud_usage_reserve', '{}', json.dumps({"route": route, "reservation_id": reservation_id}), at)

        def attempt(state, at, **binding):
            insert(worker.CLOUD_ATTEMPT_KIND, json.dumps({"hash": _hash(db), "purpose": "qc",
                                                          "state": state, **binding}), '{}', at)
        with db.db:
            db.db.execute("UPDATE messages SET first_seen=? WHERE message_id=1", (now - 300,))
        reserve(now - 400)                      # before this body was first seen
        reserve(now - 100, route='patient_context')
        assert is_spent() == 0
        reserve(now - 100)
        assert is_spent() == 1, "a stored-post send that left no record spends the body"
        # A metadata-only revision keeps the body and its spent attempt.
        db.save_messages([dataclasses.replace(_message(body='完全合成の記録です。'),
                                              sender_name='synthetic-renamed-sender')])
        assert revision() >= 1 and is_spent() == 1
        # An open second attempt resumes only for its own extraction and revision.
        with db.db:
            db.db.execute("DELETE FROM artifacts WHERE kind='cloud_usage_reserve'")
        reserve(now - 50, reservation_id='a' * 32)   # the open attempt's own first request
        attempt('open', now - 50, source_artifact_id=7, revision=revision(), reservation_id='a' * 32)
        assert is_spent(7) == 0 and is_spent(8) == 1
        reserve(now - 45)
        assert is_spent(7) == 1, "a request no attempt record names spends the body"
        with db.db:
            db.db.execute("DELETE FROM artifacts WHERE kind='cloud_usage_reserve' AND json_extract(meta,"
                          "'$.reservation_id') IS NULL")
        db.save_messages([dataclasses.replace(_message(body='完全合成の記録です。'),
                                              sender_name='synthetic-third-name')])
        assert is_spent(7) == 1, "a metadata-only revision ends the open attempt"
        attempt('open', now - 30, source_artifact_id=7, revision=revision())
        assert is_spent(7) == 1, "the earlier open record of the same body still counts"
        with db.db:
            db.db.execute("DELETE FROM artifacts WHERE kind=?", (worker.CLOUD_ATTEMPT_KIND,))
        assert is_spent(7) == 1, "a request whose record is gone counts"
        with db.db:
            db.db.execute("DELETE FROM artifacts WHERE kind='cloud_usage_reserve'")
        attempt('open', now - 50, source_artifact_id=7, revision=revision())
        assert is_spent(7) == 0
        for broken in ({"source_artifact_id": 7}, {"source_artifact_id": 7, "revision": None},
                       {"source_artifact_id": 7, "revision": "x"}, {"revision": revision()}):
            with db.db:
                db.db.execute("DELETE FROM artifacts WHERE kind=?", (worker.CLOUD_ATTEMPT_KIND,))
            attempt('open', now - 50, **broken)
            assert is_spent(7) == 1, f"an open record without a full binding never resumes: {broken}"
        # A changed body earns its own attempt; a record of another body does not count.
        db.save_messages([_message(body='完全合成の訂正後の記録です。')])
        assert is_spent(7) == 0
        attempt('closed', now - 10)
        assert is_spent(7) == 1, "a closed attempt of the body spends it"
        with db.db:
            db.db.execute("DELETE FROM artifacts WHERE kind=?", (worker.CLOUD_ATTEMPT_KIND,))
        insert(worker.CLOUD_ATTEMPT_KIND, 'not json', '{}', now)
        assert is_spent(7) == 1, "an unreadable attempt record counts"
        with db.db:
            db.db.execute("DELETE FROM artifacts WHERE kind=?", (worker.CLOUD_ATTEMPT_KIND,))
        assert is_spent(7) == 0
        insert('cloud_usage_reserve', '{}', 'not json', time.time() + 1)
        assert is_spent(7) == 1, "an unreadable reservation counts"
    finally:
        db.close()


def test_a_paid_repair_whose_answer_was_saved_before_an_interruption_is_never_sent_again(qc_window, monkeypatch):
    """Through the real runner and engine: the repair's start is checkpointed before it is
    dispatched, and a started repair is never dispatched again. A resumed attempt keeps
    the chunk's first answer (completeness ``dispatch_unknown``, attention) and does not
    adopt the repair answer that was paid and saved just before the interruption."""
    from completeness_quality_testkit import OBSERVATIONS
    ledger, cfg, _local_calls, cloud_calls, answers = qc_window
    openai_calls = _echo_providers(qc_window, monkeypatch)
    _revise(ledger, _message(body='本人に発熱と咳があります。本人の血圧120/80です。'), 'synthetic-vital-body')
    _locally_fixed(ledger)
    _flag_current(ledger)
    _open_window(cfg)
    answers['cloud'] = OBSERVATIONS   # leaves the blood pressure out: the chunk needs a quality repair
    real_record = cloud_policy.record_result

    def interrupted(ledger_, reservation, metadata, **kwargs):
        value = real_record(ledger_, reservation, metadata, **kwargs)
        if reservation['max_tokens'] == cloud_policy.PURPOSE_MAX_TOKENS['quality_repair']:
            raise KeyboardInterrupt("synthetic interruption after the paid repair answer was saved")
        return value
    monkeypatch.setattr(cloud_policy, "record_result", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _cloud_pass(ledger)
    monkeypatch.setattr(cloud_policy, "record_result", real_record)
    assert len(cloud_calls) == 2 and _reservations(ledger) == 2, "the chunk and its repair were paid"
    assert ledger.artifacts('cloud_usage_result') and len(ledger.artifacts('cloud_usage_result')) == 2
    with ledger.db:
        ledger.db.execute("DELETE FROM fetch_jobs WHERE kind='extract_claim'")
    assert _attempts(ledger) == [('qc', 'open')]
    assert _cloud_pass(ledger)['done'] == 1
    assert len(cloud_calls) == 2 and _reservations(ledger) == 2 and not openai_calls, "nothing is sent again"
    _current_row, meta = _current(ledger)
    assert meta["qc_fix"]["attempt"] == 2
    assert {entry["state"] for entry in meta["integrity"]["completeness"].values()} == {"dispatch_unknown"}
    assert _attempts(ledger) == [('qc', 'open'), ('qc', 'closed')]


def _review(state, services=()):
    """A facility review record as external_transfer.status reports it."""
    if state == "acknowledged":
        return {"state": "acknowledged", "auto_enable": True, "acknowledged": {"services": list(services)}}
    return dict(external_transfer.UNREADABLE) if state == "unreadable" else {"state": state, "auto_enable": False}


def test_a_withdrawn_only_provider_ends_the_attempt_in_a_local_pass_without_sending(qc_window, monkeypatch):
    import cloud_inference
    ledger, cfg, local_calls, cloud_calls, _answers = qc_window
    openai_calls, lift = _capped_after_the_first_chunk(qc_window, monkeypatch)
    lift()
    # The recorded review now leaves out the only provider: no cloud worker can send any more.
    monkeypatch.setattr(external_transfer, "status", lambda root=None: _review("acknowledged", ["openai"]))
    assert cloud_inference.resolve_cloud_settings(cfg)["mode"] == "off"
    cfg['cloud_llm']['mode'] = 'off'   # and no cloud pass runs at all
    calls = len(local_calls)
    report = _local_pass(ledger)
    assert report['selected'] == 0 and len(local_calls) == calls
    assert _attempts(ledger) == [('qc', 'open'), ('qc', 'closed')], "the local pass ends it (確認候補)"
    assert len(cloud_calls) == 1 and _reservations(ledger) == 1 and not openai_calls
    cfg['cloud_llm']['mode'] = 'on'
    monkeypatch.setattr(external_transfer, "status", lambda root=None: _review("not_required"))
    assert _cloud_pass(ledger)['done'] == 0 and len(cloud_calls) == 1, "an ended attempt never resumes"


def test_a_busy_write_lock_defers_ending_a_withdrawn_attempt_without_delaying_the_pass(qc_window, monkeypatch):
    import extract_llm_errors
    import extract_llm_source_contract
    ledger, cfg, _local_calls, cloud_calls, _answers = qc_window
    _openai_calls, lift = _capped_after_the_first_chunk(qc_window, monkeypatch)
    lift()
    monkeypatch.setattr(external_transfer, "status", lambda root=None: _review("acknowledged", ["openai"]))
    cfg['cloud_llm']['mode'] = 'off'
    clock = {'value': 100.0}
    monkeypatch.setattr(time, 'monotonic', lambda: clock['value'])
    monkeypatch.setattr(time, 'sleep', lambda seconds: clock.update(value=clock['value'] + seconds))
    real_acquire = extract_llm_errors.acquire_run_lock
    monkeypatch.setattr(extract_llm_errors, 'acquire_run_lock', lambda: None)   # another writer holds it

    def local_pass():
        return worker.run_pending(ledger, limit=1, workers=1, budget_s=180, allow_cloud=False,
                                  per_write_lock=True, write_wait_s=60)

    local_pass()
    assert clock['value'] <= 100 + extract_llm_source_contract._STALE_GC_BUDGET_S, "waited only the short budget"
    assert _attempts(ledger) == [('qc', 'open')], "not closed without the lock; the next pass retries"
    monkeypatch.setattr(extract_llm_errors, 'acquire_run_lock', real_acquire)
    local_pass()
    assert _attempts(ledger) == [('qc', 'open'), ('qc', 'closed')]
    assert len(cloud_calls) == 1 and _reservations(ledger) == 1


@pytest.mark.parametrize("hold", ["unreadable", "pending", "stopped", "mode_off", "key_unreadable"])
def test_a_temporary_stop_keeps_the_attempt_open_and_unsent(qc_window, monkeypatch, hold):
    ledger, cfg, _local_calls, cloud_calls, _answers = qc_window
    openai_calls, lift = _capped_after_the_first_chunk(qc_window, monkeypatch)
    lift()
    if hold in ("unreadable", "pending"):
        monkeypatch.setattr(external_transfer, "status", lambda root=None: _review(hold))
    elif hold == "stopped":
        ledger.artifact_add("cloud_control", "stopped")
    elif hold == "mode_off":
        cfg['cloud_llm']['mode'] = 'off'
    else:   # the key file cannot be read: not proof that the key was removed
        monkeypatch.setattr(cloud_policy, "provider_key", lambda name, root=None: None)
    _local_pass(ledger)
    _cloud_pass(ledger)
    assert _attempts(ledger) == [('qc', 'open')], "a stop that may be temporary keeps the attempt"
    assert len(cloud_calls) == 1 and _reservations(ledger) == 1 and not openai_calls


def test_a_qc_attempt_without_a_paid_request_is_never_closed(qc_window):
    import extract_llm_store
    ledger, _cfg, _local_calls, _cloud_calls, _answers = qc_window
    _locally_fixed(ledger)
    _flag_current(ledger)
    source, _meta = _current(ledger)
    row = {"project_id": 1, "message_id": 1, "content_hash": _hash(ledger), "qc_cloud": source['artifact_id']}
    assert extract_llm_store._close_cloud_attempt(ledger, row) is False
    assert not ledger.artifacts(worker.CLOUD_ATTEMPT_KIND), "nothing was spent: the post stays eligible"
    assert extract_llm_store._close_cloud_attempt(ledger, {**row, "qc_cloud": None}) is True
    assert _attempts(ledger) == [('backlog', 'closed')], "a plain backlog attempt still closes"


def _write_split(root, raw):
    """Write the legacy-shaped ``raw`` settings as private split provider settings under ``root``."""
    import hashlib
    import provider_settings
    import semantic_jev
    owners = {model: name for name, model in cloud_policy.PROVIDERS.items()}
    common, providers = provider_settings.split_legacy(json.loads(json.dumps(raw)), owners,
                                                       jev_model=semantic_jev.JEV_MODEL)

    def private(path, value):
        data = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()
        path.write_bytes(data)
        path.chmod(0o600)
        return hashlib.sha256(data).hexdigest()
    root.chmod(0o700)
    (root / "data" / "provider_settings").mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / "data").chmod(0o700)
    private(root / "config.json", common)
    hashes = {name: private(root / "data" / "provider_settings" / (name + ".json"), providers.get(name, {}))
              for name in provider_settings.PROVIDERS}
    private(root / "data" / ".provider-settings-authority.json", {
        "version": 2, "state": "committed", "files": hashes,
        "common_policy_sha256": provider_settings.common_policy_digest(common)})


def _split_window(qc_window, monkeypatch, tmp_path, raw_update):
    """The capped window served from split provider settings under a private root -> (openai_calls, raw, apply).

    ``apply()`` rewrites the files from ``raw`` and serves their runtime view as
    the workers' configuration, exactly as mcs_util composes it."""
    import copy
    import cloud_inference
    import extract_llm_prepare
    import extract_llm_store
    import mcs_util
    ledger, cfg, _local_calls, _cloud_calls, _answers = qc_window
    openai_calls = _echo_providers(qc_window, monkeypatch)
    root = tmp_path / "split-root"
    root.mkdir()
    for module in (extract_llm_prepare, extract_llm_store, cloud_inference):
        monkeypatch.setattr(module, "HOME", str(root))
    _long_flagged_post(ledger)
    raw = copy.deepcopy(cfg)
    raw['cloud_llm'].update(mode='on', **WINDOW)
    _both_providers(raw)
    # Split settings validate the common file alone, so the window carries its own period budget.
    raw['cloud_llm'].update(window_start=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 3600)),
                            window_budget_usd=100, daily_request_budget=1, **raw_update)

    monkeypatch.setattr(extract_llm_store, "load_config_checked",
                        lambda: mcs_util.load_config_checked(str(root / "config.json")))

    def apply():
        _write_split(root, raw)
        view = mcs_util.load_config_checked(str(root / "config.json"))
        assert view is not None
        cfg.clear()
        cfg.update(view)
        with ledger.db:
            ledger.db.execute("DELETE FROM fetch_jobs WHERE kind='extract_claim'")
    apply()
    return openai_calls, raw, apply


def test_a_custom_profile_of_a_non_preferred_provider_survives_an_off_pass_and_resumes(qc_window, monkeypatch, tmp_path):
    ledger, cfg, local_calls, cloud_calls, _answers = qc_window
    # Both keys, the facility approves only OpenAI, Anthropic comes first: the attempt starts on OpenAI's own profile.
    monkeypatch.setattr(external_transfer, "status", lambda root=None: _review("acknowledged", ["openai"]))
    openai_calls, raw, apply = _split_window(qc_window, monkeypatch, tmp_path,
                                             {"provider_profiles": {"openai": {"backlog": "sol-medium"}}})
    assert _cloud_pass(ledger)['done'] == 0
    assert [call['model'] for call in openai_calls] == ['gpt-6.1-sol'] and not cloud_calls
    assert _attempts(ledger) == [('qc', 'open')]
    raw['cloud_llm']['mode'] = 'off'
    apply()
    assert "openai" not in json.dumps(cfg['cloud_llm'].get('provider_profiles', {})), \
        "the off view is composed for Anthropic and lacks OpenAI's profile"
    calls = len(local_calls)
    _local_pass(ledger)
    assert _attempts(ledger) == [('qc', 'open')], "an off pass judges OpenAI's own profile: unchanged, so it waits"
    assert len(local_calls) == calls and len(openai_calls) == 1 and not cloud_calls
    raw['cloud_llm']['mode'] = 'on'
    raw['cloud_llm'].pop('daily_request_budget')
    apply()
    assert _cloud_pass(ledger)['done'] == 1
    sent = [_sent_text(call) for call in openai_calls]
    assert len(sent) >= 2 and len(set(sent)) == len(sent), "the saved first chunk is reused, not resent"
    assert {call['model'] for call in openai_calls} == {'gpt-6.1-sol'} and not cloud_calls
    assert _attempts(ledger) == [('qc', 'open'), ('qc', 'closed')]


def test_a_send_from_an_earlier_interval_of_the_same_body_still_spends_it(tmp_path):
    from extract_testkit import _ledger
    db = _ledger(tmp_path)
    try:
        now = time.time()
        for body in ('完全合成の記録Aです。', '完全合成の記録Bです。', '完全合成の記録Aです。'):
            db.save_messages([_message(body=body)])
        seqs = [row[0] for row in db.db.execute("SELECT seq FROM message_revisions WHERE message_id=1 ORDER BY seq")]
        assert len(seqs) == 3
        with db.db:
            db.db.execute("UPDATE messages SET first_seen=? WHERE message_id=1", (now - 300,))
            for seq, at in zip(seqs, (now - 300, now - 200, now - 100)):
                db.db.execute("UPDATE message_revisions SET observed_at=? WHERE message_id=1 AND seq=?", (at, seq))
        spent = f"SELECT {worker.cloud_attempt_spent_sql('m', '?')} FROM messages m WHERE m.message_id=1"

        def reserve_only_at(at):
            with db.db:
                db.db.execute("DELETE FROM artifacts WHERE kind='cloud_usage_reserve'")
                db.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at)"
                              " VALUES('cloud_usage_reserve',1,1,'{}','m',?,?)",
                              (json.dumps({"route": "reextract"}), at))
            return db.db.execute(spent, (7,)).fetchone()[0]
        assert reserve_only_at(now - 250) == 1, "a send that no record names, made while the body was A, spends A"
        assert reserve_only_at(now - 150) == 0, "a send made while the body was B does not spend A"
        assert reserve_only_at(now - 50) == 1
        assert reserve_only_at(now - 400) == 0, "before the message was first seen"
    finally:
        db.close()


def _valid_pin(qc_window, monkeypatch):
    ledger, cfg, *_rest = qc_window
    monkeypatch.setattr(cloud_policy, "provider_key", lambda name, root=None: "synthetic-key")
    _open_window(cfg)
    selection, errors = cloud_policy.resolve_profile(cfg, route="reextract", purpose="backlog")
    assert selection is not None and not errors
    pin = cloud_policy.route_contract(cfg, selection, route="reextract", purpose="backlog")
    assert cloud_policy.resolve_contract(cfg, pin, route="reextract", purpose="backlog").state == "usable"
    return cfg, pin


@pytest.mark.parametrize("bad", [[], {}, 1, None, ["anthropic"]])
def test_a_malformed_provider_is_an_invalid_contract_never_an_error(qc_window, monkeypatch, bad):
    import copy
    cfg, pin = _valid_pin(qc_window, monkeypatch)
    broken = {name: {**contract, "provider": bad} for name, contract in pin.items()}
    state = cloud_policy.resolve_contract(cfg, broken, route="reextract", purpose="backlog")
    assert (state.state, state.reason) == ("gone", "contract_invalid")
    assert cloud_policy.route_contract(cfg, {"provider": bad}, route="reextract", purpose="backlog") is None
    configured = copy.deepcopy(cfg)
    configured['cloud_llm']['provider'] = bad
    assert cloud_policy.resolve_contract(configured, pin, route="reextract", purpose="backlog").state != "usable"


@pytest.mark.parametrize("fallback", [{}, {"provider_unavailable": True, "cloud_llm": {"mode": "off"}}, None])
def test_an_unreadable_configuration_keeps_a_contract_waiting(qc_window, monkeypatch, fallback):
    _cfg, pin = _valid_pin(qc_window, monkeypatch)
    state = cloud_policy.resolve_contract(fallback, pin, route="reextract", purpose="backlog")
    assert (state.state, state.reason) == ("waiting", "config_unavailable")


@pytest.mark.parametrize("fault", ["empty", "provider_unavailable"])
def test_a_pass_without_verified_settings_never_ends_the_attempt_and_recovery_resumes(
        qc_window, monkeypatch, tmp_path, fault):
    import copy
    import extract_llm_store
    import mcs_util
    ledger, cfg, _local_calls, cloud_calls, _answers = qc_window
    if fault == "empty":
        # A single config.json the runtime loader cannot read: it serves {} and the checked read fails.
        cfg['cloud_llm']['provider_profiles'] = {"anthropic": {"backlog": "sonnet-high"}}
        openai_calls, lift = _capped_after_the_first_chunk(qc_window, monkeypatch)
        lift()
        sent_before, good = cloud_calls, copy.deepcopy(cfg)
        path = tmp_path / "config.json"
        path.write_bytes(b"{")
        served = mcs_util.load_config(str(path))
        assert served == {} and mcs_util.load_config_checked(str(path)) is None

        def recover():
            cfg.clear()
            cfg.update(good)
            monkeypatch.setattr(extract_llm_store, "load_config_checked", lambda: cfg)
    else:
        # Split settings whose provider file no longer matches its authority: a synthesized, closed view.
        monkeypatch.setattr(external_transfer, "status", lambda root=None: _review("acknowledged", ["openai"]))
        openai_calls, raw, recover = _split_window(qc_window, monkeypatch, tmp_path,
                                                   {"provider_profiles": {"openai": {"backlog": "sol-medium"}}})
        assert _cloud_pass(ledger)['done'] == 0 and len(openai_calls) == 1
        raw['cloud_llm'].pop('daily_request_budget')
        sent_before = openai_calls
        root = tmp_path / "split-root"
        path = root / "config.json"
        (root / "data" / "provider_settings" / "openai.json").write_bytes(b'{"profiles": {}}\n')
        served = mcs_util.load_config(str(path))
        assert served.get("provider_unavailable") and mcs_util.load_config_checked(str(path)) is None
    calls = len(sent_before)
    cfg.clear()
    cfg.update(served)
    monkeypatch.setattr(extract_llm_store, "load_config_checked", lambda: mcs_util.load_config_checked(str(path)))
    _local_pass(ledger)
    _cloud_pass(ledger)
    assert _attempts(ledger) == [('qc', 'open')], "unverified settings never judge a contract gone"
    assert len(sent_before) == calls and len(cloud_calls) + len(openai_calls) == calls
    recover()
    assert _cloud_pass(ledger)['done'] == 1
    sent = [_sent_text(call) for call in sent_before]
    assert len(sent) >= 2 and len(set(sent)) == len(sent), "the saved first chunk is reused, not resent"
    assert _attempts(ledger) == [('qc', 'open'), ('qc', 'closed')]


def test_a_busy_database_bounds_the_close_write_like_the_stale_gc(qc_window, monkeypatch):
    import functools
    import sqlite3
    import extract_llm_errors
    import extract_llm_store
    ledger, _cfg, _local_calls, cloud_calls, _answers = qc_window
    _openai_calls, lift = _capped_after_the_first_chunk(qc_window, monkeypatch)
    lift()
    monkeypatch.setattr(external_transfer, "status", lambda root=None: _review("acknowledged", ["openai"]))
    lock = functools.partial(extract_llm_errors._write_lock, wait_s=60)
    path = ledger.db.execute("PRAGMA database_list").fetchone()[2]
    other = sqlite3.connect(path, isolation_level=None)
    try:
        other.execute("BEGIN IMMEDIATE")   # another writer holds the database
        started = time.monotonic()
        assert extract_llm_store._end_withdrawn_cloud_attempts(
            ledger, lock, False, deadline=time.monotonic() + 60) == 0
        assert time.monotonic() - started < 2, "gave up within the short budget, not the 30 s busy timeout"
        assert not ledger.db.in_transaction
        other.execute("ROLLBACK")
    finally:
        other.close()
    assert _attempts(ledger) == [('qc', 'open')]
    assert extract_llm_store._end_withdrawn_cloud_attempts(ledger, lock, False, deadline=time.monotonic() + 60) == 1
    assert _attempts(ledger) == [('qc', 'open'), ('qc', 'closed')] and len(cloud_calls) == 1
