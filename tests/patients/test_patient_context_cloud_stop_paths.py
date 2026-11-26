"""A stopped cloud unit waits for a transient reason and finishes locally after a definitive one."""
from contextlib import nullcontext
import hashlib
import inspect
import json

import pytest

import anthropic_client
import cloud_backlog
import cloud_client
import cloud_inference
import cloud_policy
import extract_llm
import extract_llm_provider
import local_llm
import mcs_queries
import patient_context_artifacts
import patient_context_plan as plan
import patient_context_quota as quota
import patient_context_refresh as refresh
import patient_context_refresh_readiness as readiness
import patient_context_refresh_state as state
from inference_version import CURRENT_VERSION, VERSION_KEY
from llm_retry import CLOUD_MAX_ATTEMPTS
from test_cloud_entry_recovery import unrelated_reservation
from test_context_cloud_update_identity import cloud, finish, real_cloud, runtime
from test_patient_context_refresh import seed, step
from common_testkit import follow_cloud_mode

__all__ = ['cloud', 'real_cloud', 'runtime']

BODY = '本人は合成の自宅で生活しています。'
LONG = ''.join(f'本人は合成施設{index}で生活しています。' for index in range(20))
_USAGE = "SELECT * FROM artifacts WHERE kind IN ('cloud_usage_reserve','cloud_usage_result') ORDER BY artifact_id"


def _answer(payload, model):
    if 'source' in payload:
        answer = {'patient_context': [{'category': 'living', 'text': payload['source'],
            'evidence': payload['source'], 'subject': 'unspecified', 'details': []}]}
    else:
        answer = {'fact_ids': [item['id'] for item in payload['reports']]}
    response = {'model': model, 'stop_reason': 'end_turn',
        'content': [{'type': 'text', 'text': json.dumps(answer)}],
        'usage': {'input_tokens': 10, 'output_tokens': 2}}
    return 200, {'Request-Id': 'synthetic-stop'}, json.dumps(response).encode()


def _wire(monkeypatch, script):
    """Replace the synthetic transport: ``script(index, payload, limit)`` returns None for a
    normal answer, a status for a rejection, a response tuple, or an exception to raise."""
    sent = []

    def request(_client, method, body, timeout, *, deadline, max_bytes):
        payload = json.loads(body['messages'][0]['content'])
        sent.append(payload)
        outcome = script(len(sent) - 1, payload, max_bytes)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, int):
            return outcome, {}, b'{"error":{"type":"synthetic_rejection"}}'
        return outcome if outcome is not None else _answer(payload, body['model'])

    monkeypatch.setattr(anthropic_client.AnthropicClient, 'request', request)
    monkeypatch.setattr(cloud_client, 'time', cloud_policy.time)
    return sent


def _usage(ledger):
    return [tuple(row) for row in ledger.db.execute(_USAGE)]


def _reservations(ledger, unit=None):
    rows = [json.loads(row['meta']) for row in ledger.artifacts('cloud_usage_reserve')]
    return [row['reservation_id'] for row in rows if unit is None or row['unit_key'] == unit]


def _dispositions(ledger):
    return [(meta['reservation_id'], meta['disposition']) for meta in (
        json.loads(row['meta']) for row in ledger.artifacts('cloud_usage_disposition'))]


def _scan_rows(ledger):
    return [(json.loads(row['content']), json.loads(row['meta'])) for row in ledger.artifacts(state.SCAN)]


def _scan_done(ledger):
    rows = _scan_rows(ledger)
    return bool(rows) and rows[-1][0].get('complete') is True


def _drain(runtime, cfg, *, limit=40, until=None, **kw):
    """Step until the context is complete (or ``until`` holds); the outcomes of every pass."""
    ledger, clock, _ = runtime
    outcomes = []
    clock = runtime[1]
    for _ in range(limit):
        outcome = step(runtime, cfg=cfg, **kw)
        outcomes.append(outcome)
        assert outcome['status'] in ('progressed', 'deferred', 'complete'), outcome
        if (until(outcome) if until is not None else outcome['status'] == 'complete'):
            return outcomes
        clock.wall += 301
        clock.mono += 301
    pytest.fail(repr(([(o['status'], o.get('reason'), o.get('lane'), o.get('guard_error'), o.get('cloud_refusal'))
                       for o in outcomes[-12:]], _dispositions(ledger))))


def _switch_off(cfg, monkeypatch):
    """The owner sets cloud_llm.mode off: the real mode check answers from the configuration."""
    cfg['cloud_llm']['mode'] = 'off'
    monkeypatch.setattr(extract_llm, '_cloud_mode', extract_llm_provider._cloud_mode)


def _local_sources(local_calls):
    return [payload for payload in local_calls if 'source' in payload]


def test_multi_send_unit_with_later_503_finishes_locally(runtime, real_cloud, monkeypatch):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    seed(ledger, body=BODY * 170 + '\n\n' + '合成の通院記録です。' * 170)
    # Proof-bound extract_llm summaries cap at 60 chars; a large one arrives via extract_v1
    # and makes the first unit ask twice.
    body_hash = ledger.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0]
    ledger.artifact_add("extract_v1", json.dumps({"summary": '完全合成の補助要約。' * 1500}, ensure_ascii=False),
        project_id=1, message_id=1, model="synthetic-rules", meta={VERSION_KEY: CURRENT_VERSION, "hash": body_hash})
    # the second prompt fails on every one of its bounded attempts while the
    # cloud otherwise answers: the retry contract exhausts its ceiling on that
    # prompt (posts 1..cap) before the unit falls back to local
    sent = _wire(monkeypatch, lambda index, payload, limit: 503 if 1 <= index <= 6 else None)
    first = step(runtime, cfg=cfg)
    scans = [payload for payload in sent if 'source' in payload]
    cap = 6  # CLOUD_MAX_ATTEMPTS: one initial send plus five bounded retries
    assert len(scans) == 1 + cap and all(payload.get('derived_summaries') for payload in scans)
    assert first['status'] == 'progressed' and first['cloud_refusal'] == 'http_5xx'
    paid = _usage(ledger)
    assert len(paid) == 2 + cap * 2
    reservations = _reservations(ledger, 'scan:1')
    answered, rejected = reservations[0], reservations[1:]
    assert len(rejected) == cap
    assert _dispositions(ledger) == [(answered, 'superseded_local')]
    clock = runtime[1]
    for _ in range(40):
        outcome = step(runtime, cfg=cfg)
        if outcome['status'] == 'complete':
            break
        clock.wall += 301
        clock.mono += 301
    else:
        pytest.fail('drain with clock advancement did not complete')
    assert outcome['status'] == 'complete'
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg)
    assert readiness.inference_coverage(ledger.db, 1, cfg)['complete']
    # The source was asked once plus the bounded retries and never again;
    # local work answered both prompts.
    assert len([payload for payload in sent if 'source' in payload]) == 1 + cap
    assert _reservations(ledger, 'scan:1') == reservations
    assert [payload.get('derived_summaries') for payload in _local_sources(local_calls)[:2]] == [
        payload['derived_summaries'] for payload in scans[:2]]
    assert _dispositions(ledger) == [(answered, 'superseded_local')]
    assert _usage(ledger)[:len(paid)] == paid


def _partial_cloud_scan(runtime, cfg, monkeypatch, *, adopted=1, script=None):
    """A source of several chunks whose first ``adopted`` chunks the cloud answered and saved."""
    ledger, _, local_calls = runtime
    monkeypatch.setattr(plan, 'SCAN_CHUNK', 80)
    sent = _wire(monkeypatch, script or (lambda index, payload, limit: None))
    seed(ledger, body=LONG)
    for _ in range(adopted):
        assert step(runtime, cfg=cfg)['status'] == 'progressed'
    content, meta = _scan_rows(ledger)[-1]
    assert meta['provider'] and content['cursor'] == adopted and not content['complete']
    assert len(sent) == adopted and not local_calls
    return sent


@pytest.mark.parametrize('adopted', [1, 2, 3])
def test_adopted_partial_scan_then_503_rebuilds_locally(runtime, real_cloud, monkeypatch, adopted):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    # the chunk after the adopted ones is rejected on every bounded attempt:
    # posts adopted..adopted+5 are its initial send plus five retries
    sent = _partial_cloud_scan(runtime, cfg, monkeypatch, adopted=adopted,
                               script=lambda index, payload, limit: 503 if adopted <= index < adopted + 6 else None)
    kept = _reservations(ledger, 'scan:1')
    outcomes = _drain(runtime, cfg)
    assert all(outcome['status'] != 'failed' for outcome in outcomes)
    assert readiness.inference_coverage(ledger.db, 1, cfg)['complete']
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg)
    # The cloud was asked the bounded ceiling for the rejected chunk and never for this source again.
    assert len([payload for payload in sent if 'source' in payload]) == adopted + 6
    spans = [tuple(payload['source_identity']['span']) for payload in _local_sources(local_calls)]
    assert len(spans) == len(set(spans)) and ''.join(
        payload['source'] for payload in _local_sources(local_calls)) == LONG
    # Each adopted cloud chunk is recorded as replaced exactly once; the rejected one needs no row.
    assert sorted(_dispositions(ledger)) == sorted((rid, 'superseded_local') for rid in kept)
    content, meta = _scan_rows(ledger)[-1]
    assert meta['local_fallback'] is True and content['complete']
    # The replaced cloud scan and its units are out of every selection, not deleted.
    retired = [row_meta for kind in (state.SCAN, state.CHUNK) for row_meta in (
        json.loads(row['meta']) for row in ledger.artifacts(kind)) if row_meta.get('provider')]
    assert len(retired) == adopted + 1 and all(row_meta['invalidated'] is True for row_meta in retired)


def test_local_continuation_never_redoes_a_unit_whose_checkpoint_was_lost(runtime, real_cloud, monkeypatch):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    _partial_cloud_scan(runtime, cfg, monkeypatch, adopted=2,
                        script=lambda index, payload, limit: 503 if 2 <= index < 8 else None)
    _drain(runtime, cfg, until=lambda outcome: len(_local_sources(local_calls)) == 2)
    row = ledger.artifacts(state.SCAN)[-1]
    content, meta = json.loads(row['content']), json.loads(row['meta'])
    assert meta['local_fallback'] is True and content['cursor'] == 2 and not content['complete']
    # The checkpoint falls back one unit although that unit's local result is stored.
    with ledger.db:
        ledger.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?',
                          (json.dumps({**content, 'cursor': 1}, ensure_ascii=False), row['artifact_id']))
    chunks = [tuple(item) for item in ledger.db.execute(
        'SELECT artifact_id,content,meta FROM artifacts WHERE kind=? ORDER BY artifact_id', (state.CHUNK,))]
    outcome = step(runtime, cfg=cfg)
    # A stored unit is not a new right to run it again: the source is held for a person.
    assert (outcome['status'], outcome['reason']) == ('failed', 'patient_context_original_unit_unverified'), outcome
    assert len(_local_sources(local_calls)) == 2
    assert [tuple(item) for item in ledger.db.execute(
        'SELECT artifact_id,content,meta FROM artifacts WHERE kind=? ORDER BY artifact_id', (state.CHUNK,))] == chunks


@pytest.mark.parametrize('failure', ['transport', 'timeout', 'too_large', 'no_usage'])
def test_unknown_result_after_send_goes_local_once(runtime, real_cloud, monkeypatch, failure):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    seed(ledger, body=BODY)

    model = state._route(cfg, 1)['provider']['model']

    def script(index, payload, limit):
        persistent = failure in ('transport', 'timeout')
        # An uncertain failure permits bounded in-pass retries, each billed: the
        # fallback path is exercised only when every bounded attempt fails.
        if index >= (CLOUD_MAX_ATTEMPTS if persistent else 1):
            return None
        if failure == 'transport':
            return OSError('synthetic uncertain transport')
        if failure == 'timeout':
            return TimeoutError('synthetic timeout')
        if failure == 'too_large':
            return 200, {}, b'x' * (limit + 1)
        return 200, {}, json.dumps({'model': model, 'stop_reason': 'end_turn',
            'content': [{'type': 'text', 'text': '{"patient_context":[]}'}]}).encode()
    sent = _wire(monkeypatch, script)
    first = step(runtime, cfg=cfg)
    assert first['status'] == 'progressed', first
    assert first['cloud_refusal'] == {'too_large': 'response_too_large', 'no_usage': 'usage_unknown'}.get(failure, failure)
    attempts = CLOUD_MAX_ATTEMPTS if failure in ('transport', 'timeout') else 1
    assert len(sent) == attempts and len(_local_sources(local_calls)) == 1
    assert _local_sources(local_calls)[0] == sent[0]
    lost = _reservations(ledger, 'scan:1')
    assert len(lost) == len(sent)   # every send holds its own reservation
    assert sorted(_dispositions(ledger)) == sorted((rid, 'superseded_local') for rid in lost)
    paid = _usage(ledger)
    if failure == 'no_usage':
        # A 200 without usage latches the pricing circuit, which only a person resumes.
        cloud_policy.resume(ledger)
        paid = _usage(ledger)
    _drain(runtime, cfg)
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg)
    assert len([payload for payload in sent if 'source' in payload]) == attempts
    assert sorted(_dispositions(ledger)) == sorted((rid, 'superseded_local') for rid in lost)
    assert _usage(ledger)[:len(paid)] == paid


def _local_fallback_scans(ledger):
    return [meta for _, meta in _scan_rows(ledger) if meta.get('local_fallback') is True]


@pytest.mark.parametrize('progress', [False, True])
def test_capacity_refusal_defers_without_pinning_local(runtime, real_cloud, monkeypatch, progress):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    if progress:
        sent = _partial_cloud_scan(runtime, cfg, monkeypatch)
    else:
        sent = _wire(monkeypatch, lambda index, payload, limit: None)
        seed(ledger, body=BODY)
    before = len(sent)
    monkeypatch.setattr(cloud_backlog, 'request_slot', lambda *_args: nullcontext(False))
    for _ in range(3):
        outcome = step(runtime, cfg=cfg)
        assert (outcome['status'], outcome['reason']) == ('deferred', 'patient_context_cloud_deferred:cloud_capacity')
        assert outcome['cloud_refusal'] == 'cloud_capacity'
    assert not local_calls and len(sent) == before and not _local_fallback_scans(ledger)
    assert len(ledger.artifacts('cloud_usage_reserve')) == before and not _dispositions(ledger)
    monkeypatch.setattr(cloud_backlog, 'request_slot', lambda *_args: nullcontext(True))
    finish(runtime, cfg)
    # The cloud answered every body exactly once; nothing was done locally in between.
    bodies = [json.dumps(payload, sort_keys=True) for payload in sent]
    assert not local_calls and len(bodies) == len(set(bodies))
    assert not _local_fallback_scans(ledger) and not _dispositions(ledger)


@pytest.mark.parametrize('progress', [False, True])
def test_daily_cap_defers_then_resumes_cloud_next_day(runtime, real_cloud, monkeypatch, progress):
    ledger, clock, local_calls = runtime
    cfg, _ = real_cloud
    if progress:
        sent = _partial_cloud_scan(runtime, cfg, monkeypatch)
    else:
        sent = _wire(monkeypatch, lambda index, payload, limit: None)
        seed(ledger, body=BODY)
        unrelated_reservation(ledger, cfg, 'patient_context')   # another unit used up the day
    before = len(sent)
    monkeypatch.setitem(cfg['cloud_llm'], 'daily_request_budget', 1)
    for _ in range(3):
        outcome = step(runtime, cfg=cfg)
        # A stored cloud cursor keeps its existing reason; both wait without local work.
        assert (outcome['status'], outcome['reason']) == ('deferred', 'patient_context_cloud_budget'
            if progress else 'patient_context_cloud_deferred:daily_cap'), outcome
        assert outcome['cloud_refusal'] == 'daily_cap'
        clock.wall += 60
    assert not local_calls and len(sent) == before and not _local_fallback_scans(ledger)
    if progress:
        content, meta = _scan_rows(ledger)[-1]
        assert meta['provider'] and content['cursor'] == 1
    clock.wall += 86400
    assert step(runtime, cfg=cfg)['status'] == 'progressed'
    assert len(sent) == before + 1 and not local_calls
    if progress:
        assert sent[-1]['source_identity']['span'][0] == sent[0]['source_identity']['span'][1]
    assert not _dispositions(ledger)


@pytest.mark.parametrize('progress', [False, True])
def test_cap_reached_at_reservation_defers_without_local_work(runtime, real_cloud, monkeypatch, progress):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    if progress:
        sent = _partial_cloud_scan(runtime, cfg, monkeypatch)
    else:
        sent = _wire(monkeypatch, lambda index, payload, limit: None)
        seed(ledger, body=BODY)
    before, reserved, scans = len(sent), len(ledger.artifacts('cloud_usage_reserve')), _scan_rows(ledger)
    seen = []

    def budget(*args, **kwargs):
        # The send check passes; another request takes the day's last room before the reservation.
        if 'input_bound' not in kwargs:
            return None   # provider-order probe inside resolve_profile: not an admission check
        seen.append(True)
        return None if len(seen) == 1 else 'daily_cost_cap'
    monkeypatch.setattr(cloud_policy, 'budget_block_reason', budget)
    for _ in range(3):
        seen.clear()
        outcome = step(runtime, cfg=cfg)
        assert (outcome['status'], outcome['reason']) == (
            'deferred', 'patient_context_cloud_deferred:daily_cost_cap'), outcome
        assert outcome['cloud_refusal'] == 'daily_cost_cap' and len(seen) == 2
    # Nothing was sent, reserved, answered locally or pinned to local while the cap held.
    assert not local_calls and len(sent) == before and _scan_rows(ledger) == scans
    assert len(ledger.artifacts('cloud_usage_reserve')) == reserved and not _dispositions(ledger)


def _key_problem(monkeypatch, code):
    monkeypatch.setattr(cloud_policy, 'provider_key', lambda *args: None)
    monkeypatch.setattr(cloud_policy, 'private_env_problem', lambda *args: code)
    monkeypatch.setattr(extract_llm, '_cloud_mode', extract_llm_provider._cloud_mode)


def _generation_records(ledger):
    return [json.loads(row['content']) for row in ledger.artifacts(quota.KIND)]


@pytest.mark.parametrize('progress', ['fresh', 'cursor', 'summary'])
def test_unreadable_key_file_defers(runtime, real_cloud, monkeypatch, progress):
    ledger, clock, local_calls = runtime
    cfg, _ = real_cloud
    if progress == 'cursor':
        sent = _partial_cloud_scan(runtime, cfg, monkeypatch)
    else:
        sent = _wire(monkeypatch, lambda index, payload, limit: None)
        seed(ledger, body=BODY)
        if progress == 'summary':
            # Every source is scanned and checked; only the overview is left to generate.
            _drain(runtime, cfg, until=lambda outcome: all(
                refresh._scan_ready(scan) for scan in refresh._current_scans(
                    ledger.db, 1, refresh._snapshot(ledger.db, 1, cfg, hydrate=False)['sources'],
                    refresh._snapshot(ledger.db, 1, cfg, hydrate=False)['route'], cfg=cfg).values()))
            assert not ledger.artifacts(state.KIND)
    before, scans, records = len(sent), _scan_rows(ledger), _generation_records(ledger)
    _key_problem(monkeypatch, 'env_file_unreadable')
    for _ in range(4):
        outcome = step(runtime, cfg=cfg)
        assert (outcome['status'], outcome['reason']) == (
            'deferred', 'patient_context_cloud_deferred:env_file_unreadable'), outcome
        clock.wall += 61
    # Neither a local answer nor a generation slot was taken while the key could not be read.
    assert not local_calls and len(sent) == before and _scan_rows(ledger) == scans
    assert _generation_records(ledger) == records and not ledger.artifacts(state.KIND)


@pytest.mark.parametrize('code', ['env_file_unreadable', 'env_file_missing'])
def test_key_file_problem_does_not_stop_a_patient_the_cloud_was_never_asked_for(runtime, monkeypatch, code):
    from test_patient_context_refresh import CFG as local_cfg
    ledger, _, local_calls = runtime
    seed(ledger, body=BODY)
    # No cloud_llm block: the mode is computed from the key, and without a usable key it is off.
    monkeypatch.setattr(cloud_policy, 'provider_key', lambda *args: None)
    monkeypatch.setattr(cloud_policy, 'private_env_problem', lambda *args: code)
    assert extract_llm_provider.cloud_off_reason(None, local_cfg, 'patient_context', 1) == code
    assert not state._route(local_cfg, 1)['provider']
    # A local-only installation has nothing to wait for: it keeps working as before.
    outcomes = _drain(runtime, local_cfg)
    assert _local_sources(local_calls) and readiness.context_view_input(ledger.db, 1, cfg=local_cfg)
    assert all('cloud_refusal' not in outcome for outcome in outcomes), outcomes
    assert not _local_fallback_scans(ledger) and not ledger.artifacts('cloud_usage_reserve')


@pytest.mark.parametrize('code', ['window_cap', 'monthly_cap', 'expired'])
def test_definitive_stop_mid_source_rebuilds_locally(runtime, real_cloud, monkeypatch, code):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    sent = _partial_cloud_scan(runtime, cfg, monkeypatch)
    adopted, = _reservations(ledger, 'scan:1')
    paid = _usage(ledger)
    monkeypatch.setattr(cloud_policy, 'budget_block_reason', lambda *args, **kwargs: code)
    first = step(runtime, cfg=cfg)
    assert first['status'] == 'progressed' and first['cloud_refusal'] == code, first
    _drain(runtime, cfg)
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg)
    assert len(sent) == 1 and _usage(ledger) == paid
    assert ''.join(payload['source'] for payload in _local_sources(local_calls)) == LONG
    assert _dispositions(ledger) == [(adopted, 'superseded_local')]


@pytest.mark.parametrize('progress', [False, True])
def test_paused_goes_local(runtime, real_cloud, monkeypatch, progress):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    if progress:
        sent = _partial_cloud_scan(runtime, cfg, monkeypatch)
    else:
        sent = _wire(monkeypatch, lambda index, payload, limit: None)
        seed(ledger, body=BODY)
    before, kept = len(sent), _reservations(ledger, 'scan:1')
    cloud_policy.stop(ledger)
    # cloud-llm stop finishes the source scans locally; the summary quality audit
    # (a Jev send) is deferred until resume, so the pass waits, not completes.
    _drain(runtime, cfg, until=lambda outcome: _scan_done(ledger)
           or outcome['status'] == 'complete')
    assert _scan_done(ledger)
    assert len(sent) == before and local_calls
    assert _dispositions(ledger) == [(rid, 'superseded_local') for rid in kept]
    cloud_policy.resume(ledger)
    _drain(runtime, cfg)
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg)


_COOLDOWN_CLASSES = ('circuit_open', 'rate_limited', 'http_429', 'transport', 'timeout', 'http_5xx',
                     'http_529', 'http_402', 'contract_error')


@pytest.mark.parametrize('progress', [False, True])
@pytest.mark.parametrize('failure_class', _COOLDOWN_CLASSES)
def test_circuit_before_send_defers_for_every_cooldown_class(runtime, real_cloud, monkeypatch, failure_class, progress):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    if progress:
        sent = _partial_cloud_scan(runtime, cfg, monkeypatch)
    else:
        sent = _wire(monkeypatch, lambda index, payload, limit: None)
        seed(ledger, body=BODY)
    before = len(sent)
    monkeypatch.setattr(cloud_policy, 'block_reason', lambda *args, **kwargs: failure_class)
    for _ in range(2):
        outcome = step(runtime, cfg=cfg)
        assert outcome['status'] == 'deferred' and outcome['cloud_refusal'] == failure_class, outcome
        assert outcome['reason'] == ('patient_context_cloud_budget' if progress
                                     else 'patient_context_cloud_deferred:' + failure_class)
    assert not local_calls and len(sent) == before and not _dispositions(ledger)


def test_rate_limit_after_send_waits_when_the_unit_already_holds_cloud_work(runtime, real_cloud, monkeypatch):
    ledger, clock, local_calls = runtime
    cfg, _ = real_cloud
    limited = [True]
    sent = _partial_cloud_scan(runtime, cfg, monkeypatch,
                               script=lambda index, payload, limit: 429 if index and limited[0] else None)
    kept = _scan_rows(ledger)
    first = step(runtime, cfg=cfg)
    # A rate limit says nothing about the paid chunk: the unit waits instead of redoing it locally.
    assert (first['status'], first['reason']) == ('deferred', 'patient_context_cloud_deferred:http_429'), first
    assert first['cloud_refusal'] == 'http_429' and len(sent) > 1
    asked = len(sent)
    for _ in range(3):
        outcome = step(runtime, cfg=cfg)
        assert outcome['status'] == 'deferred' and outcome['reason'] in (
            'patient_context_cloud_deferred:http_429', 'patient_context_cloud_budget'), outcome
    assert not local_calls and not _dispositions(ledger) and _scan_rows(ledger) == kept
    # Once the limit clears, the cloud continues from its stored position.
    limited[0] = False
    clock.wall += 301
    clock.mono += 301
    assert step(runtime, cfg=cfg)['status'] == 'progressed'
    assert sent[-1]['source_identity']['span'][0] == sent[0]['source_identity']['span'][1]
    assert not local_calls and not _local_fallback_scans(ledger) and not _dispositions(ledger)
    assert len(sent) >= asked + 1 and _scan_rows(ledger)[-1][0]['cursor'] == 2


@pytest.mark.parametrize('progress', [False, True])
@pytest.mark.parametrize('failure_class', ['auth_error', 'payment_required'])
def test_latched_circuit_waits_only_for_units_with_cloud_work(runtime, real_cloud, monkeypatch, failure_class, progress):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    if progress:
        sent = _partial_cloud_scan(runtime, cfg, monkeypatch)
    else:
        sent = _wire(monkeypatch, lambda index, payload, limit: None)
        seed(ledger, body=BODY)
    before = len(sent)
    monkeypatch.setattr(cloud_policy, 'block_reason', lambda *args, **kwargs: failure_class)
    outcome = step(runtime, cfg=cfg)
    assert outcome['cloud_refusal'] == failure_class and len(sent) == before and not _dispositions(ledger)
    if progress:
        # Only a person resumes this circuit: paid progress waits for them.
        assert (outcome['status'], outcome['reason']) == ('deferred', 'patient_context_cloud_budget')
        assert not local_calls
    else:
        # A unit that never met the cloud keeps the earlier path and is answered locally.
        assert outcome['status'] == 'progressed' and _local_sources(local_calls)
        assert _local_fallback_scans(ledger)


@pytest.mark.parametrize('code', ['key_missing', 'env_file_missing', 'env_file_mode_not_0600'])
def test_missing_key_with_partial_cloud_cursor_holds(runtime, real_cloud, monkeypatch, code):
    ledger, clock, local_calls = runtime
    cfg, _ = real_cloud
    sent = _partial_cloud_scan(runtime, cfg, monkeypatch)
    paid, scans = _usage(ledger), _scan_rows(ledger)
    _key_problem(monkeypatch, code)
    for _ in range(4):
        outcome = step(runtime, cfg=cfg)
        assert (outcome['status'], outcome['reason']) == ('deferred', 'patient_context_cloud_not_sent'), outcome
        assert outcome['cloud_refusal'] == 'repeat_success' and outcome['guard_error'] == 'budget_exceeded'
        clock.wall += 61
    assert not local_calls and len(sent) == 1
    assert _usage(ledger) == paid and _scan_rows(ledger) == scans and not _dispositions(ledger)


def _progress(ledger, pid=1):
    return state._latest(ledger.db, state.PROGRESS, pid)[0]


def _overview(ledger):
    row = ledger.artifacts(state.KIND)[-1]
    return row['artifact_id'], json.loads(row['meta'])


def _cloud_context(runtime, real_cloud):
    """A context the cloud built and published completely; its paid rows and scan reservations."""
    ledger, _, local_calls = runtime
    cfg, posts = real_cloud
    finish(runtime, cfg)
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg) and not local_calls
    assert _overview(ledger)[1]['provider']
    return _usage(ledger), _reservations(ledger, 'scan:1'), len(posts)


def test_mode_off_after_cloud_adoption_rebuilds_locally_and_publishes_without_waiting(runtime, real_cloud, monkeypatch):
    ledger, clock, local_calls = runtime
    cfg, posts = real_cloud
    paid, scans, count = _cloud_context(runtime, real_cloud)
    cloud_overview = _overview(ledger)[0]
    started = clock.wall
    _switch_off(cfg, monkeypatch)
    visible, flagged = [], []
    for _ in range(40):
        outcome = step(runtime, cfg=cfg)
        assert outcome['status'] in ('progressed', 'deferred', 'complete'), outcome
        visible.append(bool(readiness.context_view_input(ledger.db, 1, cfg=cfg)))
        flagged.append(_progress(ledger).get('rebuild_summary'))
        if outcome['status'] == 'complete':
            break
    else:
        pytest.fail(repr((outcome, visible)))
    # The context is hidden from the first local result only until its own overview is
    # rebuilt, which needs no wait for the generation interval.
    hidden = visible.count(False)
    assert hidden and visible == [False] * hidden + [True] * (len(visible) - hidden)
    assert clock.wall - started < 60 < quota.INTERVAL
    assert flagged[0] is True and flagged[-1] is None
    overview, meta = _overview(ledger)
    assert overview != cloud_overview and not meta.get('provider') and not meta.get('cloud_receipts')
    assert local_calls and len(posts) == count and _usage(ledger) == paid
    replaced = _dispositions(ledger)
    assert {disposition for _, disposition in replaced} == {'superseded_local'}
    assert set(scans) <= {rid for rid, _ in replaced} and len(replaced) == len({rid for rid, _ in replaced})
    assert all(row_meta.get('invalidated') is True for _, row_meta in _scan_rows(ledger) if row_meta.get('provider'))
    # A second rebuild that replaces no paid request has no skip: its overview waits the interval.
    monkeypatch.setattr(local_llm, 'resolve', lambda cfg: ('http://invalid.test', 'synthetic-next'))
    calls = len(local_calls)
    waits = [step(runtime, cfg=cfg) for _ in range(12)]
    assert len(local_calls) > calls and _dispositions(ledger) == replaced
    assert _overview(ledger)[0] == overview and _progress(ledger).get('rebuild_summary') is None
    assert waits[-1]['status'] == 'deferred' and waits[-1]['wait_generation_quota'] == 1, waits[-1]
    clock.wall += quota.INTERVAL
    clock.mono += quota.INTERVAL
    _drain(runtime, cfg)
    assert _overview(ledger)[0] != overview and readiness.context_view_input(ledger.db, 1, cfg=cfg)
    assert len(posts) == count and _usage(ledger) == paid and _dispositions(ledger) == replaced


def test_overview_of_a_part_of_the_rebuilt_sources_does_not_take_the_interval_skip(runtime, real_cloud, monkeypatch):
    ledger, clock, local_calls = runtime
    cfg, posts = real_cloud
    second = '本人は合成の通所施設を週に二回利用しています。'
    seed(ledger, mid=2, body=second)
    _cloud_context(runtime, real_cloud)
    records, started, cloud_overview = _generation_records(ledger), clock.wall, _overview(ledger)[0]
    _switch_off(cfg, monkeypatch)
    flagged, overviews = [], []
    for _ in range(60):
        outcome = step(runtime, cfg=cfg)
        assert outcome['status'] in ('progressed', 'deferred', 'complete'), outcome
        flagged.append(_progress(ledger).get('rebuild_summary'))
        overviews.append(_overview(ledger))
        if outcome['status'] == 'complete':
            break
    else:
        pytest.fail(repr((outcome, flagged)))
    # While one source is rebuilt and the other is not, an overview of that part is possible.
    # It waits the interval like any other, so the one skip is still there for the overview of
    # all rebuilt sources, and the patient's context comes back without that wait.
    assert clock.wall - started < 60 < quota.INTERVAL
    rebuilt = next(index for index, (overview, _) in enumerate(overviews) if overview != cloud_overview)
    assert rebuilt >= 4 and flagged == [True] * rebuilt + [None] * (len(flagged) - rebuilt)
    assert overviews[rebuilt][1]['source_scope'] == [1, 2] and not overviews[rebuilt][1]['source_coverage']['unreflected']
    assert [record['state'] for record in _generation_records(ledger)[len(records):]] == ['complete']
    view = readiness.context_view_input(ledger.db, 1, cfg=cfg)
    assert view and {fact['quote'] for fact in view['facts']} >= {second}
    assert not _overview(ledger)[1].get('provider') and local_calls


def test_superseded_request_is_never_resent_when_cloud_returns(runtime, real_cloud, monkeypatch):
    ledger, clock, local_calls = runtime
    cfg, posts = real_cloud
    paid, scans, count = _cloud_context(runtime, real_cloud)
    _switch_off(cfg, monkeypatch)
    _drain(runtime, cfg)
    replaced = _dispositions(ledger)
    assert set(scans) <= {rid for rid, _ in replaced}
    cfg['cloud_llm']['mode'] = 'on'
    for _ in range(60):
        outcome = step(runtime, cfg=cfg)
        assert outcome['status'] in ('progressed', 'deferred', 'complete'), outcome
        if outcome['status'] == 'complete':
            break
        if outcome.get('wait_generation_quota'):
            clock.wall += quota.INTERVAL
            clock.mono += quota.INTERVAL
    else:
        pytest.fail(repr((outcome, readiness.inference_coverage(ledger.db, 1, cfg)['status'])))
    # No replaced request was sent again, and the patient is not left behind the old cloud scan.
    assert len(posts) == count and _usage(ledger) == paid and _dispositions(ledger) == replaced
    assert readiness.inference_coverage(ledger.db, 1, cfg)['complete']
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg)
    assert all(meta.get('local_fallback') is True for _, meta in _scan_rows(ledger) if not meta.get('invalidated'))
    # A later change of the source is new input: the cloud takes it and the overview follows.
    changed = '本人は合成の集合住宅へ転居しました。'
    seed(ledger, body=changed)
    clock.wall += quota.INTERVAL
    clock.mono += quota.INTERVAL
    finish(runtime, cfg)
    assert [payload['source'] for payload in posts[count:] if 'source' in payload] == [changed]
    view = readiness.context_view_input(ledger.db, 1, cfg=cfg)
    assert view and any(fact['quote'] == changed for fact in view['facts'])
    assert _usage(ledger)[:len(paid)] == paid and _dispositions(ledger) == replaced


class _Interrupted(BaseException):
    pass


def test_young_missing_result_defers_and_stale_goes_local_without_adopting_a_late_answer(runtime, real_cloud, monkeypatch):
    ledger, clock, local_calls = runtime
    cfg, _ = real_cloud
    seed(ledger, body=BODY)
    sent = _wire(monkeypatch, lambda index, payload, limit: _Interrupted() if index == 0 else None)
    with pytest.raises(_Interrupted):
        step(runtime, cfg=cfg)
    row, = ledger.artifacts('cloud_usage_reserve')
    reservation = json.loads(row['meta'])
    assert not ledger.artifacts('cloud_usage_result')
    for _ in range(3):
        outcome = step(runtime, cfg=cfg)
        assert (outcome['status'], outcome['reason']) == (
            'deferred', 'patient_context_cloud_deferred:result_pending'), outcome
        assert outcome['cloud_refusal'] == 'result_missing'
    stale = row['created_at'] + reservation['timeout_seconds'] + cloud_policy.RESULT_STALE_MARGIN_S
    clock.wall = stale - 1
    assert step(runtime, cfg=cfg)['reason'] == 'patient_context_cloud_deferred:result_pending'
    assert not local_calls and len(sent) == 1 and not _dispositions(ledger) and not _scan_rows(ledger)
    assert clock.wall == stale
    first = step(runtime, cfg=cfg)
    assert first['status'] == 'progressed' and _local_sources(local_calls) == [sent[0]]
    assert _dispositions(ledger) == [(reservation['reservation_id'], 'superseded_local')]
    # The answer arrives after all: it settles the reservation and is never taken up.
    status, _, raw = _answer(sent[0], reservation['model'])
    text = json.loads(raw)['content'][0]['text']
    cloud_policy.record_result(ledger, reservation, {
        'model': reservation['model'], 'request_fp': reservation['request_fp'],
        'usage': {'input_tokens': 10, 'output_tokens': 2}, 'http_class': 'http_200', 'request_id': None,
        'error_kind': None, 'answer_text_sha256_v1': __import__('hashlib').sha256(text.encode()).hexdigest(),
        'answer_finish_reason_v1': 'stop'}, answer={'text': text, 'finish_reason': 'stop'})
    assert len(ledger.artifacts('cloud_usage_result')) == 1
    _drain(runtime, cfg)
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg)
    assert len([payload for payload in sent if 'source' in payload]) == 1
    assert all(meta.get('local_fallback') is True and not meta.get('cloud_receipts') for _, meta in _scan_rows(ledger))
    assert _dispositions(ledger) == [(reservation['reservation_id'], 'superseded_local')]


def test_takeover_keeps_waiting_for_a_missing_result_even_once_stale(runtime, real_cloud, monkeypatch):
    """Known limitation (spec, change note): with no context child, a unit whose request lost
    its result waits under the takeover whether the result is young or provably stale. Only a
    child pass replaces it, once and without a resend; the owner's other remedy is mode off."""
    ledger, clock, local_calls = runtime
    cfg, _ = real_cloud
    seed(ledger, body=BODY)
    sent = _wire(monkeypatch, lambda index, payload, limit: _Interrupted() if index == 0 else None)
    with pytest.raises(_Interrupted):
        step(runtime, cfg=cfg)   # the child stops after its reservation and send
    row, = ledger.artifacts('cloud_usage_reserve')
    reservation = json.loads(row['meta'])
    stale = row['created_at'] + reservation['timeout_seconds'] + cloud_policy.RESULT_STALE_MARGIN_S
    for wall in (clock.wall, stale + 1, stale + 86400):
        clock.wall = wall
        clock.mono += refresh.CHILD_WAIT_S   # past the takeover's wait for this patient
        outcome = step(runtime, cfg=cfg, role='local_takeover')
        assert (outcome['status'], outcome['reason'], outcome['cloud_refusal']) == (
            'deferred', 'patient_context_cloud_deferred:cloud_worker_unavailable', 'cloud_worker_unavailable'), outcome
    assert not local_calls and len(sent) == 1 and not _dispositions(ledger) and not _scan_rows(ledger)
    # A child pass after the stale threshold replaces the request once, without sending it again.
    assert step(runtime, cfg=cfg)['status'] == 'progressed'
    assert _local_sources(local_calls) == [sent[0]] and len(sent) == 1
    assert _dispositions(ledger) == [(reservation['reservation_id'], 'superseded_local')]


def test_repeated_cloud_defers_do_not_consume_generation_quota(runtime, real_cloud, monkeypatch):
    ledger, clock, local_calls = runtime
    cfg, posts = real_cloud
    _cloud_context(runtime, real_cloud)
    count, records = len(posts), _generation_records(ledger)
    assert [record['state'] for record in records] == ['complete']
    # A changed source is scanned by the cloud; then the day's cap stops its new overview.
    seed(ledger, body='本人は合成の集合住宅へ転居しました。')
    clock.wall += quota.INTERVAL
    clock.mono += quota.INTERVAL
    _drain(runtime, cfg, until=lambda outcome: outcome.get('lane') == 'C' and outcome['status'] == 'progressed')
    scanned = len(posts)
    assert scanned > count and not local_calls
    monkeypatch.setitem(cfg['cloud_llm'], 'daily_request_budget', len(ledger.artifacts('cloud_usage_reserve')))
    outcomes = []
    for _ in range(6):
        outcomes.append(step(runtime, cfg=cfg))
        clock.wall += 61
    waited = [outcome for outcome in outcomes if outcome.get('reason') == 'patient_context_cloud_deferred:daily_cap']
    assert waited and all(outcome['status'] == 'deferred' and outcome['lane'] == 'A' for outcome in waited)
    assert all(outcome['status'] == 'deferred' for outcome in outcomes), outcomes
    # No slot was taken, so nothing is left unknown and another input is not made to wait.
    assert _generation_records(ledger) == records
    assert quota.inspect(ledger.db, 1, 'a' * 64, now=clock.wall)['allowed'] is True
    assert len(posts) == scanned and not local_calls and not _dispositions(ledger)


def test_cloud_wait_after_a_claim_returns_the_generation_slot(runtime, real_cloud, monkeypatch):
    ledger, clock, local_calls = runtime
    cfg, posts = real_cloud
    _drain(runtime, cfg, until=lambda outcome: outcome.get('lane') == 'C' and outcome['status'] == 'progressed')
    assert not ledger.artifacts(state.KIND) and not _generation_records(ledger)
    # The send slot fills only after the slot of the overview was claimed.
    monkeypatch.setattr(cloud_backlog, 'request_slot', lambda *_args: nullcontext(False))
    outcome = step(runtime, cfg=cfg)
    assert (outcome['status'], outcome['reason'], outcome['lane']) == (
        'deferred', 'patient_context_cloud_deferred:cloud_capacity', 'A'), outcome
    assert [record['state'] for record in _generation_records(ledger)] == ['released']
    assert quota.inspect(ledger.db, 1, 'a' * 64, now=clock.wall)['allowed'] is True
    assert not local_calls


def test_paid_overview_answer_is_taken_up_while_the_cloud_waits(runtime, real_cloud, monkeypatch):
    ledger, clock, local_calls = runtime
    cfg, posts = real_cloud
    _drain(runtime, cfg, until=lambda outcome: outcome.get('lane') == 'C' and outcome['status'] == 'progressed')
    original = state._replace

    def broken(ledger, kind, *args, **kwargs):
        if kind == state.KIND:
            raise OSError('synthetic write failure')
        return original(ledger, kind, *args, **kwargs)
    # The overview is asked and answered, but its result is not saved.
    monkeypatch.setattr(state, '_replace', broken)
    with pytest.raises(OSError):
        step(runtime, cfg=cfg)
    monkeypatch.setattr(state, '_replace', original)
    asked = len(posts)
    assert 'reports' in posts[-1] and not ledger.artifacts(state.KIND)
    assert [record['state'] for record in _generation_records(ledger)] == ['started']
    paid = _usage(ledger)
    # That request used up the day. The answer is already paid for: taking it up sends nothing,
    # so the cap that makes every new request wait does not hold it back.
    monkeypatch.setitem(cfg['cloud_llm'], 'daily_request_budget', len(ledger.artifacts('cloud_usage_reserve')))
    clock.wall += 700
    clock.mono += 700
    outcome = step(runtime, cfg=cfg)
    assert (outcome['status'], outcome['lane']) == ('progressed', 'A'), outcome
    assert len(posts) == asked and not local_calls and _usage(ledger) == paid
    assert _overview(ledger)[1]['provider'] and not _dispositions(ledger)
    assert [record['state'] for record in _generation_records(ledger)] == ['complete']


@pytest.mark.parametrize('break_at', ['generation_changed', 'scan_write', 'progress_write'])
def test_supersede_is_rolled_back_with_a_failed_commit(runtime, real_cloud, monkeypatch, break_at):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    if break_at == 'generation_changed':
        # The overview's own request is lost after sending; its local replacement cannot commit.
        seed(ledger, body=BODY)
        _wire(monkeypatch, lambda index, payload, limit: OSError('synthetic') if 'reports' in payload else None)
        _drain(runtime, cfg, until=lambda outcome: outcome.get('lane') == 'C' and outcome['status'] == 'progressed')
        monkeypatch.setattr(refresh.context_quota, 'finish_tx', lambda *args, **kwargs: False)
    else:
        # the second chunk is rejected on every bounded attempt: posts 1..6 are
        # its initial send plus five retries, after which local work replaces it
        _partial_cloud_scan(runtime, cfg, monkeypatch,
                            script=lambda index, payload, limit: 503 if 1 <= index < 7 else None)
        original = state._replace

        def broken(ledger, kind, *args, **kwargs):
            if kind == (state.SCAN if break_at == 'scan_write' else state.PROGRESS) and local_calls:
                raise OSError('synthetic write failure')
            return original(ledger, kind, *args, **kwargs)
        monkeypatch.setattr(state, '_replace', broken)
    kept = "SELECT * FROM artifacts WHERE kind IN (?,?,?) ORDER BY artifact_id"
    before = [tuple(row) for row in ledger.db.execute(kept, (state.SCAN, state.CHUNK, state.KIND))]
    try:
        outcome = step(runtime, cfg=cfg)
    except OSError:
        outcome = None
    assert local_calls and (outcome is None or outcome['status'] == 'source_changed')
    # Neither the replacement record nor the retired cloud scan outlives the failed commit.
    assert not ledger.db.in_transaction and not _dispositions(ledger)
    assert [tuple(row) for row in ledger.db.execute(kept, (state.SCAN, state.CHUNK, state.KIND))] == before


def _answered_then_another_publisher(runtime, cfg, monkeypatch, kind):
    """Run until one paid answer of ``kind`` is in memory while another publisher writes the
    patient's progress; that pass cannot save. Returns the requests sent and the pass outcome."""
    ledger, _, _ = runtime
    sent = _wire(monkeypatch, lambda index, payload, limit: None)
    seed(ledger, body=BODY)
    original = cloud_inference.CloudInferenceUnit.__call__
    moved = []

    def answered_then_moved(self, prompt, **kwargs):
        answer = original(self, prompt, **kwargs)
        if kind in json.loads(prompt) and not moved:
            moved.append(True)
            with ledger.db:
                state._replace(ledger, state.PROGRESS, 1, {**_progress(ledger), 'synthetic_publisher': True}, {})
        return answer

    with monkeypatch.context() as patch:
        patch.setattr(cloud_inference.CloudInferenceUnit, '__call__', answered_then_moved)
        for _ in range(20):
            outcome = step(runtime, cfg=cfg)
            if moved:
                return sent, outcome
    pytest.fail('the unit was never asked')


@pytest.mark.parametrize('unit', ['scan:1', 'summary'])
def test_discarded_unit_finishes_locally_without_new_rows(runtime, real_cloud, monkeypatch, unit):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    kind = 'source' if unit == 'scan:1' else 'reports'
    sent, outcome = _answered_then_another_publisher(runtime, cfg, monkeypatch, kind)
    discarded = _reservations(ledger, unit)[-1]
    if unit == 'scan:1':
        # A scan's answer outlives a change elsewhere in the patient (next test); here it was
        # ended for another reason, such as its scope being withdrawn while it was in memory.
        row, = [row for row in ledger.artifacts('cloud_usage_reserve')
                if json.loads(row['meta'])['reservation_id'] == discarded]
        with ledger.db:
            ledger.db.execute('BEGIN IMMEDIATE')
            cloud_policy.record_disposition_tx(ledger, cloud_policy._accounting_meta(row['meta']), 'discarded_scope')
    else:
        # The overview reads every source and the patient's progress: its answer is stale.
        assert outcome['status'] == 'source_changed'
    assert _dispositions(ledger) == [(discarded, 'discarded_scope')]
    paid, asked = _usage(ledger), len(sent)
    # The discarded answer can never be adopted, so local work takes the unit; the first
    # disposition stands and no second row is written for it, also when the kept local
    # overview is checked again by its later quality pass.
    outcomes = _drain(runtime, cfg)
    assert outcomes[0]['status'] == 'progressed' and outcomes[0]['cloud_refusal'] == 'cloud_result_discarded'
    assert [payload for payload in local_calls if kind in payload] == [
        payload for payload in sent if kind in payload]
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg)
    assert len([payload for payload in sent if kind in payload]) == 1 and len(sent) - asked <= 1
    assert _dispositions(ledger) == [(discarded, 'discarded_scope')] and _usage(ledger)[:len(paid)] == paid


def test_scan_answer_is_kept_when_another_publisher_writes_the_patient_during_its_pass(runtime, real_cloud, monkeypatch):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    sent, outcome = _answered_then_another_publisher(runtime, cfg, monkeypatch, 'source')
    answered, = _reservations(ledger, 'scan:1')
    paid = _usage(ledger)
    # The pass does not save over the other publisher, and does not end the answer either:
    # the source it asked about is as it was.
    assert (outcome['status'], outcome['reason']) == ('deferred', 'patient_context_readiness_unverified'), outcome
    assert not _dispositions(ledger) and not _scan_rows(ledger) and len(sent) == 1
    assert step(runtime, cfg=cfg)['status'] == 'progressed'
    content, meta = _scan_rows(ledger)[-1]
    assert meta['provider'] and content['complete'] and not local_calls and not _dispositions(ledger)
    assert len(sent) == 1 and _reservations(ledger, 'scan:1') == [answered] and _usage(ledger) == paid


def test_capacity_mid_unit_keeps_the_received_answer_recoverable(runtime, real_cloud, monkeypatch):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    seed(ledger, body=BODY * 170 + '\n\n' + '合成の通院記録です。' * 170)
    body_hash = ledger.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0]
    ledger.artifact_add("extract_v1", json.dumps({"summary": '完全合成の補助要約。' * 1500}, ensure_ascii=False),
        project_id=1, message_id=1, model="synthetic-rules", meta={VERSION_KEY: CURRENT_VERSION, "hash": body_hash})
    sent = _wire(monkeypatch, lambda index, payload, limit: None)
    slots = iter([True])
    monkeypatch.setattr(cloud_backlog, 'request_slot', lambda *_args: nullcontext(next(slots, False)))
    outcome = step(runtime, cfg=cfg)
    # The unit's first request was answered; the send slot was full for its second one.
    assert (outcome['status'], outcome['reason']) == ('deferred', 'patient_context_cloud_deferred:cloud_capacity')
    assert len(sent) == 1 and not local_calls and not _dispositions(ledger) and not _scan_rows(ledger)
    monkeypatch.setattr(cloud_backlog, 'request_slot', lambda *_args: nullcontext(True))
    assert step(runtime, cfg=cfg)['status'] == 'progressed'
    # The answer already paid for is taken up without a second send; only the rest was asked.
    assert len(sent) == 2 and sent[0] != sent[1] and not local_calls and not _dispositions(ledger)
    assert len(ledger.artifacts('cloud_usage_reserve')) == 2 and _scan_rows(ledger)[-1][1]['provider']


def _unsaved_answer(runtime, cfg, monkeypatch, script=None):
    """A unit of two requests whose first one the cloud answered. The send slot was full for the
    second, so no position is stored: the unit's cloud work is that answered request alone."""
    ledger, _, local_calls = runtime
    seed(ledger, body=BODY * 170 + '\n\n' + '合成の通院記録です。' * 170)
    body_hash = ledger.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0]
    ledger.artifact_add("extract_v1", json.dumps({"summary": '完全合成の補助要約。' * 1500}, ensure_ascii=False),
        project_id=1, message_id=1, model="synthetic-rules", meta={VERSION_KEY: CURRENT_VERSION, "hash": body_hash})
    sent = _wire(monkeypatch, script or (lambda index, payload, limit: None))
    slots = iter([True])
    monkeypatch.setattr(cloud_backlog, 'request_slot', lambda *_args: nullcontext(next(slots, False)))
    assert step(runtime, cfg=cfg)['reason'] == 'patient_context_cloud_deferred:cloud_capacity'
    monkeypatch.setattr(cloud_backlog, 'request_slot', lambda *_args: nullcontext(True))
    assert len(sent) == 1 and len(_reservations(ledger, 'scan:1')) == 1
    assert not local_calls and not _scan_rows(ledger) and not _dispositions(ledger)
    return sent


def test_rate_limit_waits_for_a_unit_whose_paid_answer_of_this_input_is_not_saved_yet(runtime, real_cloud, monkeypatch):
    ledger, clock, local_calls = runtime
    cfg, _ = real_cloud
    limited = [True]
    sent = _unsaved_answer(runtime, cfg, monkeypatch,
                           script=lambda index, payload, limit: 429 if index and limited[0] else None)
    answered, = _reservations(ledger, 'scan:1')
    outcome = step(runtime, cfg=cfg)
    assert (outcome['status'], outcome['reason'], outcome['cloud_refusal']) == (
        'deferred', 'patient_context_cloud_deferred:http_429', 'http_429'), outcome
    assert len(sent) > 1 and not local_calls and not _dispositions(ledger) and not _scan_rows(ledger)
    # Once the limit clears, the paid answer is taken up and only the rest is asked.
    limited[0] = False
    clock.wall += 301
    clock.mono += 301
    asked = len(sent)
    assert step(runtime, cfg=cfg)['status'] == 'progressed'
    assert len(sent) == asked + 1 and sent[-1] != sent[0] and not local_calls and not _dispositions(ledger)
    assert _reservations(ledger, 'scan:1')[0] == answered and _scan_rows(ledger)[-1][1]['provider']


# Rows of the decision table that no other test here drives through a refresh pass, with the
# owner's decision written out (with cloud progress, fresh), independent of the classifier.
_PRE_SEND_ROWS = {
    'not_started': ('defer', 'hold'), 'deadline': ('defer', 'hold'),
    'owner_transaction_active': ('defer', 'hold'), 'checkpoint_unverified': ('defer', 'hold'),
    'pricing_unverified': ('hold', 'hold'), 'provider_budget_missing': ('hold', 'hold'),
    'window_history_pruned': ('hold', 'hold'),
    'price_expired': ('local', 'local'), 'window_history_aged': ('local', 'local'),
    'provider_monthly_cap': ('local', 'local'), 'provider_daily_cap': ('defer', 'defer'),
}


@pytest.mark.parametrize('held', [False, True])
@pytest.mark.parametrize('code', sorted(_PRE_SEND_ROWS))
def test_stop_before_send_follows_the_decision_table(runtime, real_cloud, monkeypatch, code, held):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    if held:
        sent = _unsaved_answer(runtime, cfg, monkeypatch)
    else:
        sent = _wire(monkeypatch, lambda index, payload, limit: None)
        seed(ledger, body=BODY)
    answered, before, paid = _reservations(ledger, 'scan:1'), len(sent), _usage(ledger)
    monkeypatch.setattr(cloud_policy, 'block_reason', lambda *args, **kwargs: code)
    monkeypatch.setattr(cloud_policy, 'refusal_code', lambda cfg, named, **kwargs: named)
    verdict = _PRE_SEND_ROWS[code][0 if held else 1]
    outcome = step(runtime, cfg=cfg)
    assert len(sent) == before and _usage(ledger) == paid
    if verdict == 'defer':
        assert (outcome['status'], outcome['reason'], outcome['cloud_refusal']) == (
            'deferred', 'patient_context_cloud_deferred:' + code, code), outcome
        assert not local_calls and not _dispositions(ledger) and not _scan_rows(ledger)
    elif verdict == 'local' or not held:
        # A definitive stop replaces what the cloud answered. A unit that holds nothing keeps
        # the earlier path for every other reason: local work answers it.
        assert outcome['status'] == 'progressed' and outcome['cloud_refusal'] == code, outcome
        assert _local_sources(local_calls) and _local_fallback_scans(ledger)
        assert _dispositions(ledger) == [(rid, 'superseded_local') for rid in answered]
    else:
        # Neither a wait nor a replacement: the paid answer keeps the unit for a person.
        assert (outcome['status'], outcome['reason']) == ('deferred', 'patient_context_cloud_not_sent'), outcome
        assert outcome['cloud_refusal'] == 'repeat_success'
        assert not local_calls and not _dispositions(ledger) and not _scan_rows(ledger)


@pytest.mark.parametrize('code,verdict', [('scope', 'local'), ('route_closed', 'local'), ('profile_invalid', 'hold')])
def test_reason_the_cloud_is_off_follows_the_decision_table_for_a_unit_with_cloud_work(
        runtime, real_cloud, monkeypatch, code, verdict):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    sent = _partial_cloud_scan(runtime, cfg, monkeypatch)
    adopted, = _reservations(ledger, 'scan:1')
    paid = _usage(ledger)
    monkeypatch.setattr(extract_llm, '_cloud_mode', lambda *args: 'off')
    follow_cloud_mode(monkeypatch, extract_llm)
    monkeypatch.setattr(extract_llm, 'cloud_off_reason', lambda *args: code)
    outcome = step(runtime, cfg=cfg)
    assert len(sent) == 1 and _usage(ledger) == paid
    if verdict == 'local':
        assert outcome['status'] == 'progressed' and outcome['cloud_refusal'] == code, outcome
        assert _local_sources(local_calls) and _dispositions(ledger) == [(adopted, 'superseded_local')]
    else:
        assert (outcome['status'], outcome['reason']) == ('deferred', 'patient_context_cloud_not_sent'), outcome
        assert outcome['cloud_refusal'] == 'repeat_success' and not local_calls and not _dispositions(ledger)


def test_a_key_read_that_fails_once_waits_instead_of_holding(runtime, real_cloud, monkeypatch):
    """Reproduction: the mode and the reason the cloud is off were read separately. The first
    key read failed and the second succeeded, so the unit held as an unknown stop. One reading
    names the temporary read failure, and the unit waits for the cloud."""
    ledger, _, local_calls = runtime
    cfg, posts = real_cloud
    monkeypatch.setattr(extract_llm, '_cloud_mode', extract_llm_provider._cloud_mode)
    seed(ledger, body=BODY)
    reads = []
    real_key = cloud_policy.selected_key

    def selected_key(root=None, cfg=None):
        # The unit's first reading of its cloud mode fails to read the key; every other read
        # (the selection's, the route's, later ones) succeeds.
        stack = [frame.function for frame in inspect.stack()]
        callers = [name for name in stack[stack.index('_cloud_route_state') + 1:]
                   if name not in ('_cloud_mode', 'cloud_route_mode', 'cloud_off_reason', 'route_mode')]
        unit = callers[:1] == ['_run_due_unit']
        reads.append('unit' if unit else 'other')
        return None if unit and reads.count('unit') == 1 else real_key(root, cfg)

    monkeypatch.setattr(cloud_policy, 'selected_key', selected_key)
    monkeypatch.setattr(cloud_policy, 'selected_key_problem', lambda root, cfg: reads.append('problem'))
    outcome = step(runtime, cfg=cfg)
    assert (outcome['status'], outcome.get('reason'), outcome.get('cloud_refusal')) == (
        'deferred', 'patient_context_cloud_deferred:env_file_unreadable', 'env_file_unreadable'), (outcome, reads)
    assert not local_calls and not posts and not _scan_rows(ledger)


@pytest.mark.parametrize('role', ['any', 'local_takeover'])
def test_patient_taken_out_of_the_cloud_scope_rebuilds_locally(runtime, real_cloud, monkeypatch, role):
    """A takeover does not replace the real reason the cloud is off for a patient outside its
    scope: the unit with cloud progress goes local as before, it does not wait for a child."""
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    sent = _partial_cloud_scan(runtime, cfg, monkeypatch)
    adopted, = _reservations(ledger, 'scan:1')
    paid = _usage(ledger)
    # The owner narrows the cloud scope to another patient; the real mode check reads it.
    cfg['cloud_llm']['project_ids'] = [2]
    monkeypatch.setattr(extract_llm, '_cloud_mode', extract_llm_provider._cloud_mode)
    assert extract_llm_provider.cloud_off_reason(None, cfg, 'patient_context', 1) == 'scope'
    assert not state._route(cfg, 1)['provider']
    first = step(runtime, cfg=cfg, **({'role': role} if role != 'any' else {}))
    assert first['status'] == 'progressed' and first['cloud_refusal'] == 'scope', first
    _drain(runtime, cfg)
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg)
    assert len(sent) == 1 and _usage(ledger) == paid
    assert ''.join(payload['source'] for payload in _local_sources(local_calls)) == LONG
    assert _dispositions(ledger) == [(adopted, 'superseded_local')]


def _response(model, **fields):
    return 200, {}, json.dumps({'model': model, 'stop_reason': 'end_turn',
        'content': [{'type': 'text', 'text': '{"patient_context":[]}'}],
        'usage': {'input_tokens': 10, 'output_tokens': 2}, **fields}).encode()


@pytest.mark.parametrize('failure,code', [
    (404, 'http_4xx'), (529, 'http_529'), ((200, {}, b'not json'), 'protocol_error'),
    ('refusal', 'contract_error')])
def test_failed_send_of_a_unit_with_cloud_work_goes_local_at_once(runtime, real_cloud, monkeypatch, failure, code):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    if failure == 'refusal':
        failure = _response(state._route(cfg, 1)['provider']['model'], stop_reason='refusal')
    sent = _partial_cloud_scan(runtime, cfg, monkeypatch,
                               script=lambda index, payload, limit: failure if index else None)
    adopted, = _reservations(ledger, 'scan:1')
    paid = _usage(ledger)
    outcome = step(runtime, cfg=cfg)
    # The stored cloud position does not make the unit wait. A retryable 5xx is
    # bounded-retried to the shared ceiling; auth/contract/protocol failures are not.
    assert outcome['status'] == 'progressed' and outcome['cloud_refusal'] == code, outcome
    expected_posts = 7 if code == 'http_529' else 2
    assert len(sent) == expected_posts and len(_local_sources(local_calls)) == 1 and _local_fallback_scans(ledger)
    assert _dispositions(ledger)[0] == (adopted, 'superseded_local') and _usage(ledger)[:len(paid)] == paid


def _rejected_request(ledger, cfg, unit, mid, owner, prompt='another synthetic prompt'):
    """One settled paid-history row of a unit key: a request the provider rejected."""
    selection, errors = cloud_policy.resolve_profile(cfg, route='patient_context', purpose='patient_context',
                                                    root=extract_llm.HOME)
    assert not errors
    client = anthropic_client.AnthropicClient('synthetic-key', model=selection['model'], effort=selection['effort'])
    row = cloud_policy.usage_reserver(ledger, cfg, route='patient_context', project_id=1, message_id=mid,
        unit_key=unit, owner=owner, selection=selection)(
            client.body(prompt, selection['max_tokens'] or 100, None), 30)
    cloud_policy.record_result(ledger, row, {'model': row['model'], 'request_fp': row['request_fp'], 'usage': None,
        'http_class': 'http_404', 'request_id': None, 'error_kind': 'contract_error'})
    return row


def test_same_prompt_held_by_another_unit_key_stays_held(runtime, real_cloud, monkeypatch):
    ledger, clock, local_calls = runtime
    cfg, _ = real_cloud
    sent = _partial_cloud_scan(runtime, cfg, monkeypatch)
    # The adopted request now belongs to another unit key, and this unit keeps a reservation
    # of its own (a different prompt), so it still counts as holding cloud work.
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.unit_key','scan:other') "
                          "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result')")
    _rejected_request(ledger, cfg, 'scan:1', 1, {})
    paid = _usage(ledger)
    cloud_policy.stop(ledger)
    for _ in range(4):
        outcome = step(runtime, cfg=cfg)
        assert (outcome['status'], outcome['reason']) == ('deferred', 'patient_context_cloud_not_sent'), outcome
        assert outcome['cloud_refusal'] == 'repeat_success'
        clock.wall += 61
    assert not local_calls and len(sent) == 1 and not _dispositions(ledger) and _usage(ledger) == paid


_KEPT = "SELECT * FROM artifacts WHERE kind IN (?,?,?,'cloud_usage_disposition') ORDER BY artifact_id"


@pytest.mark.parametrize('fault', ['nothing_recorded', 'another_unit_key'])
def test_local_result_is_not_saved_unless_every_replaced_request_is_recorded(runtime, real_cloud, monkeypatch, fault):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    sent = _partial_cloud_scan(runtime, cfg, monkeypatch,
                               script=lambda index, payload, limit: 503 if index else None)
    adopted, = _reservations(ledger, 'scan:1')
    record = cloud_inference.supersede_context_local
    recorded = []

    def faulty(ledger, cfg, prompt, **request):
        if fault == 'nothing_recorded':
            return []   # the replacement goes unrecorded
        written = record(ledger, cfg, prompt, **request)
        recorded.extend(written)
        # Once the unit's own request is ended, another unit key holds an answer to this very prompt.
        ledger.db.execute(
            "INSERT INTO artifacts(kind,project_id,message_id,model,content,meta,created_at) "
            "SELECT kind,project_id,message_id,model,content,"
            "json_set(meta,'$.reservation_id',?,'$.unit_key','scan:other'),created_at FROM artifacts "
            "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result') "
            "AND json_extract(meta,'$.reservation_id')=? ORDER BY artifact_id", ('f' * 32, adopted))
        return written
    monkeypatch.setattr(cloud_inference, 'supersede_context_local', faulty)
    kept = (state.SCAN, state.CHUNK, state.KIND)
    before, paid = [tuple(row) for row in ledger.db.execute(_KEPT, kept)], _usage(ledger)
    for attempt in range(3):
        outcome = step(runtime, cfg=cfg)
        # The check after the replacement uses the unit key the replacement used: while a paid
        # request of the prompt is neither recorded as replaced nor ended, the local answer is
        # dropped with everything the pass wrote.
        assert (outcome['status'], outcome['reason']) == ('deferred', 'patient_context_cloud_not_sent'), outcome
        assert outcome['cloud_refusal'] and outcome['guard_error'] == 'budget_exceeded'
        assert not ledger.db.in_transaction and len(_local_sources(local_calls)) == attempt + 1
        assert [tuple(row) for row in ledger.db.execute(_KEPT, kept)] == before
        assert _usage(ledger)[:len(paid)] == paid and _reservations(ledger, 'scan:other') == []
    assert len(recorded) == (3 if fault == 'another_unit_key' else 0) and not _dispositions(ledger)
    assert len(sent) == 1 + CLOUD_MAX_ATTEMPTS   # adopted chunk plus the bounded rejected sends; nothing was asked again


def _facility_review(monkeypatch, review):
    """The facility review record changes: settings and the mode check read it for real."""
    import external_transfer
    monkeypatch.setattr(external_transfer, 'status', lambda root=None: dict(review))
    monkeypatch.setattr(cloud_inference, 'resolve_cloud_settings',
                        lambda value: cloud_policy.resolve_settings(value, extract_llm.HOME)[0])
    monkeypatch.setattr(extract_llm, '_cloud_mode', extract_llm_provider._cloud_mode)


def test_provider_the_facility_has_not_approved_is_never_sent_to_and_the_unit_goes_local(runtime, real_cloud, monkeypatch):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    sent = _partial_cloud_scan(runtime, cfg, monkeypatch)
    adopted, = _reservations(ledger, 'scan:1')
    paid = _usage(ledger)
    _facility_review(monkeypatch, {'state': 'pending', 'auto_enable': False})
    first = step(runtime, cfg=cfg)
    assert first['status'] == 'progressed' and first['cloud_refusal'] == 'provider_not_approved', first
    _drain(runtime, cfg)
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg)
    assert len(sent) == 1 and _usage(ledger) == paid
    assert ''.join(payload['source'] for payload in _local_sources(local_calls)) == LONG
    assert _dispositions(ledger) == [(adopted, 'superseded_local')]


@pytest.mark.parametrize('progress', [False, True])
def test_unreadable_facility_review_holds_and_replaces_nothing(runtime, real_cloud, monkeypatch, progress):
    import external_transfer
    ledger, clock, local_calls = runtime
    cfg, _ = real_cloud
    if progress:
        sent = _partial_cloud_scan(runtime, cfg, monkeypatch)
    else:
        sent = _wire(monkeypatch, lambda index, payload, limit: None)
        seed(ledger, body=BODY)
    before, paid = len(sent), _usage(ledger)
    _facility_review(monkeypatch, external_transfer.UNREADABLE)
    for _ in range(3):
        outcome = step(runtime, cfg=cfg)
        if progress:
            # Not a review decision: paid progress is neither sent again nor replaced.
            assert (outcome['status'], outcome['reason']) == ('deferred', 'patient_context_cloud_not_sent'), outcome
        clock.wall += 61
    assert len(sent) == before and _usage(ledger) == paid and not _dispositions(ledger)
    assert bool(local_calls) == (not progress)


CHANGED = '本人は合成の集合住宅へ転居しました。'


def _new_input_after_cloud_context(runtime, real_cloud, *, scanned):
    """A patient whose cloud context is published gets a changed source. The cloud served the
    patient's units before, but none of that work belongs to the new input. With ``scanned``
    the cloud has read and checked the new source, and only its overview is left."""
    ledger, clock, local_calls = runtime
    cfg, posts = real_cloud
    _cloud_context(runtime, real_cloud)
    seed(ledger, body=CHANGED)
    clock.wall += quota.INTERVAL
    clock.mono += quota.INTERVAL
    if scanned:
        _drain(runtime, cfg, until=lambda outcome: outcome.get('lane') == 'C' and outcome['status'] == 'progressed')
    assert not local_calls and not _dispositions(ledger)
    return cfg, posts


def _cloud_waits(outcomes):
    return [outcome for outcome in outcomes if (outcome.get('reason') or '').partition(':')[0] in (
        'patient_context_cloud_deferred', 'patient_context_cloud_budget')]


@pytest.mark.parametrize('scanned', [False, True])
@pytest.mark.parametrize('failure_class', ['auth_error', 'payment_required'])
def test_latched_circuit_does_not_hold_back_a_new_input_of_a_patient_the_cloud_served_before(
        runtime, real_cloud, monkeypatch, failure_class, scanned):
    ledger, _, local_calls = runtime
    cfg, posts = _new_input_after_cloud_context(runtime, real_cloud, scanned=scanned)
    asked, paid = len(posts), _usage(ledger)
    monkeypatch.setattr(cloud_policy, 'block_reason', lambda *args, **kwargs: failure_class)
    outcomes = _drain(runtime, cfg)
    # Only a person resumes this circuit. The new input holds no cloud work to keep for them,
    # so it takes the earlier path at once: local work answers it and replaces nothing.
    assert not _cloud_waits(outcomes), outcomes
    assert [(outcome['lane'], outcome['status']) for outcome in outcomes if outcome.get('cloud_refusal')] == (
        [('B', 'progressed')] * (not scanned) + [('A', 'progressed')])
    assert [payload['source'] for payload in _local_sources(local_calls)] == ([] if scanned else [CHANGED])
    assert any('reports' in payload for payload in local_calls)
    assert len(posts) == asked and _usage(ledger) == paid and not _dispositions(ledger)
    view = readiness.context_view_input(ledger.db, 1, cfg=cfg)
    assert view and any(fact['quote'] == CHANGED for fact in view['facts'])


@pytest.mark.parametrize('scanned', [False, True])
def test_first_send_rate_limit_of_a_new_input_goes_local_for_a_patient_the_cloud_served_before(
        runtime, real_cloud, monkeypatch, scanned):
    ledger, clock, local_calls = runtime
    cfg, _ = _new_input_after_cloud_context(runtime, real_cloud, scanned=scanned)
    kind = 'reports' if scanned else 'source'
    before = len(ledger.artifacts('cloud_usage_reserve'))
    sent = _wire(monkeypatch, lambda index, payload, limit: 429 if kind in payload else None)
    limited = step(runtime, cfg=cfg)
    # The limit met the unit's first request for this input: nothing paid waits behind it,
    # whatever the cloud answered for the patient's earlier inputs.
    assert (limited['status'], limited['lane'], limited['cloud_refusal']) == (
        'progressed', 'A' if scanned else 'B', 'http_429'), limited
    assert [payload for payload in local_calls if kind in payload] == [sent[0]]
    assert len(ledger.artifacts('cloud_usage_reserve')) > before and not _dispositions(ledger)
    asked = len([payload for payload in sent if kind in payload])
    # The limit also opened the circuit; once it closes, the rest of the patient is finished.
    clock.wall += 301
    clock.mono += 301
    _drain(runtime, cfg)
    view = readiness.context_view_input(ledger.db, 1, cfg=cfg)
    assert view and any(fact['quote'] == CHANGED for fact in view['facts'])
    assert len([payload for payload in sent if kind in payload]) == asked and not _dispositions(ledger)


def test_overview_request_for_the_same_sources_is_not_cloud_work_of_a_later_overview(runtime, real_cloud, monkeypatch):
    ledger, _, local_calls = runtime
    cfg, posts = _new_input_after_cloud_context(runtime, real_cloud, scanned=True)
    # The overview key is one for the patient, and the facts of an overview change while the
    # sources stay the same: an earlier overview was asked under these very sources.
    signature = refresh._snapshot(ledger.db, 1, cfg, hydrate=False, source_keys=(1,))['source_sig']
    _rejected_request(ledger, cfg, 'summary', None, {'workday_id': 'synthetic', 'source_sig': signature})
    assert cloud_policy.unit_reserved(ledger, route='patient_context', project_id=1, message_id=None,
                                      unit_key='summary', source_sig=signature)
    asked, paid = len(posts), _usage(ledger)
    monkeypatch.setattr(cloud_policy, 'block_reason', lambda *args, **kwargs: 'auth_error')
    outcome = step(runtime, cfg=cfg)
    # The new overview has not begun, so none of that is its own work: it does not wait for a person.
    assert (outcome['status'], outcome['lane'], outcome['cloud_refusal']) == ('progressed', 'A', 'auth_error'), outcome
    assert any('reports' in payload for payload in local_calls) and _overview(ledger)[1]['source_sig'] == signature
    assert len(posts) == asked and _usage(ledger) == paid and not _dispositions(ledger)


def test_removed_cloud_settings_with_an_unreadable_key_file_never_wait_for_the_cloud(runtime, real_cloud, monkeypatch):
    ledger, clock, local_calls = runtime
    cfg, posts = real_cloud
    paid, _, count = _cloud_context(runtime, real_cloud)
    # The installation stops using the cloud: no cloud_llm block is left, and the mode is
    # computed from a key file that cannot be read.
    del cfg['cloud_llm']
    monkeypatch.setattr(cloud_inference, 'resolve_cloud_settings',
                        lambda value: cloud_policy.resolve_settings(value, extract_llm.HOME)[0])
    _key_problem(monkeypatch, 'env_file_unreadable')
    assert extract_llm_provider.cloud_off_reason(None, cfg, 'patient_context', 1) == 'env_file_unreadable'
    assert not state._route(cfg, 1)['provider']
    # Nothing would come back from a cloud this configuration does not ask, so nothing waits
    # for it. What the cloud answered is kept as before: not replaced without an explicit off.
    for _ in range(2):
        outcome = step(runtime, cfg=cfg)
        assert (outcome['status'], outcome['reason'], outcome['cloud_refusal']) == (
            'deferred', 'patient_context_cloud_not_sent', 'repeat_success'), outcome
    assert not local_calls and readiness.context_view_input(ledger.db, 1, cfg=cfg)
    # A new input of the same patient is finished locally like any other.
    seed(ledger, body=CHANGED)
    clock.wall += quota.INTERVAL
    clock.mono += quota.INTERVAL
    outcomes = _drain(runtime, cfg)
    assert not _cloud_waits(outcomes), outcomes
    assert [payload['source'] for payload in _local_sources(local_calls)] == [CHANGED]
    view = readiness.context_view_input(ledger.db, 1, cfg=cfg)
    assert view and any(fact['quote'] == CHANGED for fact in view['facts'])
    assert len(posts) == count and _usage(ledger) == paid and not _dispositions(ledger)


@pytest.mark.parametrize('wait', ['cloud_capacity', 'daily_cap'])
def test_unsaved_answer_of_an_unchanged_body_is_adopted_after_another_message_arrives(runtime, real_cloud, monkeypatch, wait):
    ledger, clock, local_calls = runtime
    cfg, _ = real_cloud
    sent = _unsaved_answer(runtime, cfg, monkeypatch)
    answered, = _reservations(ledger, 'scan:1')
    paid = _usage(ledger)
    # Routine ingest while the unit waits: another message of the same patient.
    seed(ledger, mid=2, body='合成の別の連絡です。')
    if wait == 'daily_cap':
        # The day's cap is reached as well: both sources wait, and nothing goes local.
        with monkeypatch.context() as patch:
            patch.setattr(cloud_policy, 'block_reason', lambda *args, **kwargs: wait)
            for _ in range(4):
                outcome = step(runtime, cfg=cfg)
                assert (outcome['status'], outcome['reason']) == (
                    'deferred', 'patient_context_cloud_deferred:daily_cap'), outcome
        assert len(sent) == 1 and _usage(ledger) == paid
        # The wait ends on the next day, in another workday than the one that asked.
        clock.wall += 86400
        clock.mono += 86400
    _drain(runtime, cfg, until=lambda outcome: any(meta['source_key'] == 1 for _, meta in _scan_rows(ledger)))
    assert not _dispositions(ledger) and not _local_sources(local_calls)
    meta, = [meta for _, meta in _scan_rows(ledger) if meta['source_key'] == 1]
    assert meta['provider'] and not meta.get('local_fallback')
    # The first request's paid answer was taken up, never sent again.
    assert _reservations(ledger, 'scan:1')[0] == answered and _usage(ledger)[:len(paid)] == paid
    assert sent.count(sent[0]) == 1


@pytest.mark.parametrize('edited', ['another_source', 'own_source'])
def test_paid_answer_is_discarded_only_when_its_own_source_changes_during_the_request(runtime, real_cloud, monkeypatch, edited):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    seed(ledger, body=BODY)
    seed(ledger, mid=2, body='合成の別の連絡です。')
    asked = []

    def script(index, payload, limit):
        if not asked:
            # A correction arrives while the first scan request is at the provider.
            asked.append(payload['source_identity']['key'])
            seed(ledger, mid=asked[0] if edited == 'own_source' else 3 - asked[0], body='合成の連絡を訂正しました。')

    sent = _wire(monkeypatch, script)
    first = step(runtime, cfg=cfg)
    key, = asked
    paid, = _reservations(ledger, f'scan:{key}')
    assert first['status'] != 'progressed' and len(sent) == 1 and not _scan_rows(ledger) and not local_calls
    if edited == 'own_source':
        # The answer is about a body that no longer exists: it is ended and never taken up.
        assert _dispositions(ledger) == [(paid, 'discarded_scope')]
    else:
        assert not _dispositions(ledger)
        assert json.loads(ledger.artifacts('cloud_usage_result')[0]['meta']).get('disposition') is None
    _drain(runtime, cfg, until=lambda outcome: any(meta['source_key'] == key for _, meta in _scan_rows(ledger)))
    content, meta = next(row for row in _scan_rows(ledger) if row[1]['source_key'] == key)
    assert meta['provider'] and not meta.get('local_fallback') and not local_calls
    asked_for_key = [payload for payload in sent if payload['source_identity']['key'] == key]
    if edited == 'own_source':
        # The corrected body is a new input: the cloud is asked about it, and its scan cites
        # that request alone.
        assert [payload['source'] for payload in asked_for_key] == [BODY, '合成の連絡を訂正しました。']
        assert [fact['text'] for fact in content['facts']] == ['合成の連絡を訂正しました。']
        assert _dispositions(ledger) == [(paid, 'discarded_scope')]
    else:
        # The unchanged body keeps its paid answer: taken up by a later pass, never asked again.
        assert len(asked_for_key) == 1 and _reservations(ledger, f'scan:{key}') == [paid]
        assert not _dispositions(ledger)


def test_changed_body_never_takes_up_the_paid_answer_of_its_earlier_body(runtime, real_cloud, monkeypatch):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    seed(ledger, body=BODY)
    sent = _wire(monkeypatch, lambda index, payload, limit: None)
    # The answer arrives, but the pass ends before it can save it.
    admitted = [True]

    def locked():
        admitted[0] = False

    with monkeypatch.context() as patch:
        patch.setattr(state, 'acquire_run_lock', locked)
        outcome = step(runtime, cfg=cfg, admit=lambda: admitted[0])
    assert (outcome['status'], outcome['reason']) == ('deferred', 'run_lock'), outcome
    answered, = _reservations(ledger, 'scan:1')
    paid = _usage(ledger)
    assert len(sent) == 1 and not _scan_rows(ledger) and not _dispositions(ledger)
    seed(ledger, body=CHANGED)
    _drain(runtime, cfg, until=lambda outcome: bool(_scan_rows(ledger)))
    content, meta = _scan_rows(ledger)[-1]
    # The corrected body is asked afresh; the earlier answer stays paid, unused and unreplaced.
    assert meta['provider'] and [fact['text'] for fact in content['facts']] == [CHANGED]
    assert [payload['source'] for payload in sent] == [BODY, CHANGED] and not local_calls
    assert not _dispositions(ledger) and _usage(ledger)[:len(paid)] == paid
    fresh, = [rid for rid in _reservations(ledger, 'scan:1') if rid != answered]
    assert {ref['artifact_id'] for ref in meta['cloud_receipts']} == {
        row['artifact_id'] for row in ledger.artifacts('cloud_usage_reserve')
        if json.loads(row['meta'])['reservation_id'] == fresh}


def test_rate_limit_still_waits_for_an_unsaved_answer_after_another_message_arrives(runtime, real_cloud, monkeypatch):
    ledger, clock, local_calls = runtime
    cfg, _ = real_cloud
    limited = [True]

    def first(payload):
        return payload.get('source_identity', {}).get('key') == 1

    sent = _unsaved_answer(runtime, cfg, monkeypatch,
        script=lambda index, payload, limit: 429 if index and limited[0] and first(payload) else None)
    answered, = _reservations(ledger, 'scan:1')
    seed(ledger, mid=2, body='合成の別の連絡です。')
    # The first source still holds cloud work for its own input: the limit makes it wait.
    waits = _drain(runtime, cfg, until=lambda outcome: outcome.get('cloud_refusal') == 'http_429')
    assert (waits[-1]['status'], waits[-1]['reason']) == ('deferred', 'patient_context_cloud_deferred:http_429')
    assert not _dispositions(ledger) and not [payload for payload in _local_sources(local_calls) if first(payload)]
    assert not [meta for _, meta in _scan_rows(ledger) if meta['source_key'] == 1]
    limited[0] = False
    clock.wall += 301
    clock.mono += 301
    _drain(runtime, cfg, until=lambda outcome: any(meta['source_key'] == 1 for _, meta in _scan_rows(ledger)))
    meta, = [meta for _, meta in _scan_rows(ledger) if meta['source_key'] == 1]
    assert meta['provider'] and not _dispositions(ledger)
    assert sent.count(sent[0]) == 1 and _reservations(ledger, 'scan:1')[0] == answered
    assert not [payload for payload in _local_sources(local_calls) if first(payload)]


def test_derived_input_change_makes_the_unsaved_answer_no_cloud_work_for_the_source(runtime, real_cloud, monkeypatch):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    sent = _unsaved_answer(runtime, cfg, monkeypatch)
    answered, = _reservations(ledger, 'scan:1')
    paid = _usage(ledger)
    # The body and its stored occurrence stay; a summary derived from it is replaced, which
    # changes what a scan of this source is asked.
    body_hash = ledger.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0]
    ledger.artifact_add("extract_v1", json.dumps({"summary": '完全合成の別の補助要約。'}, ensure_ascii=False),
        project_id=1, message_id=1, model="synthetic-rules", meta={VERSION_KEY: CURRENT_VERSION, "hash": body_hash})
    monkeypatch.setattr(cloud_policy, 'block_reason', lambda *args, **kwargs: 'auth_error')
    outcome = step(runtime, cfg=cfg)
    # A latched circuit makes only a unit with cloud work for its current input wait: the
    # earlier answer is for another input, so the source keeps the earlier path.
    assert outcome['status'] == 'progressed' and outcome['cloud_refusal'] == 'auth_error', outcome
    assert _local_sources(local_calls) and _local_fallback_scans(ledger)
    assert len(sent) == 1 and _usage(ledger) == paid and not _dispositions(ledger)
    assert _reservations(ledger, 'scan:1') == [answered]


def test_answer_reserved_under_an_earlier_binding_is_replaced_once_and_never_resent(runtime, real_cloud, monkeypatch):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    # A request stored by a version that bound a scan's answer to another value (the whole
    # patient): the current binding of the same source never equals it.
    with monkeypatch.context() as patch:
        patch.setattr(state, '_cloud_scan_binding', lambda *args: 'e' * 64)
        sent = _unsaved_answer(runtime, cfg, monkeypatch)
    answered, = _reservations(ledger, 'scan:1')
    paid = _usage(ledger)
    outcome = step(runtime, cfg=cfg)
    # Its answer cannot be taken up and its request is not sent again: local work answers the
    # same prompt and records the paid request as replaced.
    assert outcome['status'] == 'progressed' and outcome['cloud_refusal'] == 'repeat_success', outcome
    assert _dispositions(ledger) == [(answered, 'superseded_local')] and len(sent) == 1
    assert _local_sources(local_calls)[0]['source'] == sent[0]['source'] and _local_fallback_scans(ledger)
    _drain(runtime, cfg)
    assert sent.count(sent[0]) == 1 and _reservations(ledger, 'scan:1') == [answered]
    assert _dispositions(ledger) == [(answered, 'superseded_local')] and _usage(ledger)[:len(paid)] == paid


def test_request_without_a_result_under_an_earlier_binding_waits_then_is_replaced_once(runtime, real_cloud, monkeypatch):
    """A request sent by a version that bound a scan to the whole patient, whose result never
    arrived: it waits for that result until the stale threshold, then local work replaces it
    once, nothing is sent again, and an answer arriving after all is accounted, never adopted."""
    ledger, clock, local_calls = runtime
    cfg, _ = real_cloud
    seed(ledger, body=BODY)
    sent = _wire(monkeypatch, lambda index, payload, limit: _Interrupted() if index == 0 else None)
    with monkeypatch.context() as patch:
        patch.setattr(state, '_cloud_scan_binding', lambda *args: 'e' * 64)
        with pytest.raises(_Interrupted):
            step(runtime, cfg=cfg)
    row, = ledger.artifacts('cloud_usage_reserve')
    reservation = json.loads(row['meta'])
    assert not ledger.artifacts('cloud_usage_result')
    assert reservation['owner']['source_sig'] == 'e' * 64   # the earlier, patient-wide binding
    stale = row['created_at'] + reservation['timeout_seconds'] + cloud_policy.RESULT_STALE_MARGIN_S
    for wall in (clock.wall, stale - 1):
        clock.wall = wall
        outcome = step(runtime, cfg=cfg)
        assert (outcome['status'], outcome['reason']) == (
            'deferred', 'patient_context_cloud_deferred:result_pending'), outcome
        assert len(sent) == 1 and len(ledger.artifacts('cloud_usage_reserve')) == 1
        assert not local_calls and not _dispositions(ledger) and not _scan_rows(ledger)
    clock.wall = stale
    outcome = step(runtime, cfg=cfg)
    assert outcome['status'] == 'progressed', outcome
    assert _local_sources(local_calls) == [sent[0]] and _local_fallback_scans(ledger)
    assert _dispositions(ledger) == [(reservation['reservation_id'], 'superseded_local')]
    _drain(runtime, cfg)
    # The source's request is never asked again (the overview, another unit, is asked once).
    assert len([payload for payload in sent if 'source' in payload]) == 1
    assert _reservations(ledger, 'scan:1') == [reservation['reservation_id']]
    assert _dispositions(ledger) == [(reservation['reservation_id'], 'superseded_local')]
    # The answer arrives after all: it settles the reservation and is never taken up.
    _, _, raw = _answer(sent[0], reservation['model'])
    text = json.loads(raw)['content'][0]['text']
    cloud_policy.record_result(ledger, reservation, {
        'model': reservation['model'], 'request_fp': reservation['request_fp'],
        'usage': {'input_tokens': 10, 'output_tokens': 2}, 'http_class': 'http_200', 'request_id': None,
        'error_kind': None, 'answer_text_sha256_v1': hashlib.sha256(text.encode()).hexdigest(),
        'answer_finish_reason_v1': 'stop'}, answer={'text': text, 'finish_reason': 'stop'})
    assert [json.loads(result['meta'])['reservation_id'] for result in ledger.artifacts('cloud_usage_result')
            ].count(reservation['reservation_id']) == 1
    _drain(runtime, cfg)
    assert len([payload for payload in sent if 'source' in payload]) == 1
    assert all(meta.get('local_fallback') is True and not meta.get('cloud_receipts') for _, meta in _scan_rows(ledger))
    assert _dispositions(ledger) == [(reservation['reservation_id'], 'superseded_local')]
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg)


def test_unsaved_answer_is_never_taken_up_for_another_occurrence_of_the_same_body(runtime, real_cloud, monkeypatch):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    sent = _unsaved_answer(runtime, cfg, monkeypatch)
    answered, = _reservations(ledger, 'scan:1')
    paid = _usage(ledger)
    # The body, and so the question a scan asks, is the same; the stored original is another
    # occurrence (first seen at another time), which makes it another input.
    with ledger.db:
        ledger.db.execute('UPDATE messages SET first_seen=first_seen+1 WHERE message_id=1')
    outcome = step(runtime, cfg=cfg)
    # The earlier answer is not taken up, and the same question is not paid for twice: local
    # work answers it and records the paid request as replaced.
    assert outcome['status'] == 'progressed' and outcome['cloud_refusal'] == 'repeat_success', outcome
    assert _dispositions(ledger) == [(answered, 'superseded_local')] and len(sent) == 1
    assert _local_sources(local_calls) and _local_fallback_scans(ledger)
    assert _reservations(ledger, 'scan:1') == [answered] and _usage(ledger)[:len(paid)] == paid


def test_scan_request_is_stored_under_its_own_source_input(runtime, real_cloud, monkeypatch):
    ledger, _, _ = runtime
    cfg, _ = real_cloud
    _wire(monkeypatch, lambda index, payload, limit: None)
    seed(ledger, body=BODY)
    seed(ledger, mid=2, body='合成の別の連絡です。')
    _drain(runtime, cfg, until=lambda outcome: len(_scan_rows(ledger)) == 2)
    route = state._route(cfg, 1)
    stored = {}
    for row in ledger.artifacts('cloud_usage_reserve'):
        meta = json.loads(row['meta'])
        if meta['unit_key'].startswith('scan:'):
            stored[meta['message_id']] = (meta['owner']['source_sig'], meta['input_binding'])
    expected = {}
    for source in mcs_queries.patient_context_sources(ledger.db, 1, cfg=cfg):
        expected[source['message_id']] = state._cloud_scan_binding(
            plan.source_signature(source), route['contract'], None,
            patient_context_artifacts.source_occurrence(ledger.db, 1, source))
    # Each request names the input of its own source, whatever else the patient holds: that is
    # what counts it as cloud work for the source and what its answer is taken up for.
    assert {mid: owner for mid, (owner, _) in stored.items()} == expected and len(expected) == 2
    assert len({binding for _, binding in stored.values()}) == 2
    assert all(binding != owner for owner, binding in stored.values())


def test_scan_input_changes_with_each_part_of_the_source_input():
    base = ['a' * 64, {'context': 1}, 'anthropic', {'source_binding': 'b' * 64, 'first_seen': 1}]
    changed = ['c' * 64, {'context': 2}, 'openai', {'source_binding': 'b' * 64, 'first_seen': 2}]
    bindings = {state._cloud_scan_binding(*base)}
    for index, value in enumerate(changed):
        bindings.add(state._cloud_scan_binding(*base[:index], value, *base[index + 1:]))
    assert len(bindings) == 5 and all(cloud_policy.valid_hash(binding) for binding in bindings)


def _reserved_never_sent(runtime, cfg, monkeypatch, *, latch=None, script=None):
    """A source whose one request was reserved and then stopped before it left: the checks
    that run again before the send refused it (the tick was withdrawn, or the circuit ``latch``
    opened), and the reservation is settled as never sent."""
    ledger, _, local_calls = runtime
    seed(ledger, body=BODY)
    sent = _wire(monkeypatch, script or (lambda index, payload, limit: None))
    if latch is None:
        outcome = step(runtime, cfg=cfg, admit=lambda: not ledger.artifacts('cloud_usage_reserve'))
    else:
        block = cloud_policy.block_reason
        with monkeypatch.context() as patch:
            patch.setattr(cloud_policy, 'block_reason', lambda ledger_, *args, **kwargs: (
                latch if ledger_.artifacts('cloud_usage_reserve') else block(ledger_, *args, **kwargs)))
            outcome = step(runtime, cfg=cfg)
    results = [json.loads(row['meta']) for row in ledger.artifacts('cloud_usage_result')]
    assert [(result['http_class'], result['cost_usd']) for result in results] == [('not_sent', '0')], (outcome, results)
    assert len(_reservations(ledger, 'scan:1')) == 1 and not sent and not local_calls
    assert not _scan_rows(ledger) and not _dispositions(ledger)
    return sent


@pytest.mark.parametrize('another_message', [False, True])
@pytest.mark.parametrize('stopped_by', ['admission', 'circuit'])
@pytest.mark.parametrize('failure_class', ['auth_error', 'payment_required'])
def test_request_that_never_left_is_not_cloud_work_for_a_latched_circuit(
        runtime, real_cloud, monkeypatch, failure_class, stopped_by, another_message):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    sent = _reserved_never_sent(runtime, cfg, monkeypatch,
                                latch=failure_class if stopped_by == 'circuit' else None)
    paid = _usage(ledger)
    if another_message:
        seed(ledger, mid=2, body='合成の別の連絡です。')
    monkeypatch.setattr(cloud_policy, 'block_reason', lambda *args, **kwargs: failure_class)
    outcomes = _drain(runtime, cfg, until=lambda outcome: len(_local_fallback_scans(ledger)) == 1 + another_message)
    # Nothing was sent for the source, so nothing paid waits for the person who resumes this
    # circuit: the unit keeps the earlier path and local work answers it.
    assert not _cloud_waits(outcomes), outcomes
    assert sorted(payload['source'] for payload in _local_sources(local_calls)) == sorted(
        [BODY] + ['合成の別の連絡です。'] * another_message)
    assert not sent and _usage(ledger) == paid and not _dispositions(ledger)


def test_first_request_that_leaves_is_not_made_to_wait_by_one_that_never_left(runtime, real_cloud, monkeypatch):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    sent = _reserved_never_sent(runtime, cfg, monkeypatch, script=lambda index, payload, limit: 429)
    unsent, = _reservations(ledger, 'scan:1')
    limited = step(runtime, cfg=cfg)
    # The rate limit met the first request the source ever sent: nothing paid waits behind it.
    assert (limited['status'], limited['lane'], limited['cloud_refusal']) == ('progressed', 'B', 'http_429'), limited
    assert [payload['source'] for payload in _local_sources(local_calls)] == [BODY]
    assert sent and {payload['source'] for payload in sent} == {BODY}
    assert _local_fallback_scans(ledger) and not _dispositions(ledger)
    assert _reservations(ledger, 'scan:1')[0] == unsent and len(_reservations(ledger, 'scan:1')) == 1 + len(sent)


def test_request_that_never_left_does_not_hide_the_sent_one_beside_it(runtime, real_cloud, monkeypatch):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    sent = _unsaved_answer(runtime, cfg, monkeypatch)
    answered, = _reservations(ledger, 'scan:1')
    # The source's second request is reserved and stopped before it leaves.
    outcome = step(runtime, cfg=cfg, admit=lambda: len(_reservations(ledger, 'scan:1')) < 2)
    results = [json.loads(row['meta'])['http_class'] for row in ledger.artifacts('cloud_usage_result')]
    assert results == ['http_200', 'not_sent'] and len(sent) == 1, (outcome, results)
    monkeypatch.setattr(cloud_policy, 'block_reason', lambda *args, **kwargs: 'auth_error')
    for _ in range(3):
        outcome = step(runtime, cfg=cfg)
        # The answered request is still cloud work of this input: it waits for a person.
        assert (outcome['status'], outcome['reason'], outcome['cloud_refusal']) == (
            'deferred', 'patient_context_cloud_deferred:auth_error', 'auth_error'), outcome
    assert not local_calls and not _dispositions(ledger) and not _scan_rows(ledger)
    assert _reservations(ledger, 'scan:1')[0] == answered and len(sent) == 1


def test_shadow_comparison_request_holds_a_later_pass_without_a_cloud_route(runtime, real_cloud, monkeypatch):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    seed(ledger, body=BODY)
    sent = _wire(monkeypatch, lambda index, payload, limit: None)
    cfg['cloud_llm']['mode'] = 'shadow'
    monkeypatch.setattr(extract_llm, '_cloud_mode', extract_llm_provider._cloud_mode)
    assert step(runtime, cfg=cfg)['status'] == 'progressed'
    (_, meta), = _scan_rows(ledger)
    compared, = [json.loads(row['meta']) for row in ledger.artifacts('cloud_usage_reserve')]
    assert len(sent) == 1 and len(_local_sources(local_calls)) == 1 and 'provider' not in meta
    # The comparison ran without a route provider, so its request names the very input a pass
    # has while the cloud is off for this configuration.
    unrouted = state._cloud_scan_binding(meta['source_sig'], meta['contract'], None, meta['source_occurrence'])
    assert compared['owner']['source_sig'] == unrouted
    assert cloud_policy.unit_reserved(ledger, route='patient_context', project_id=1, message_id=1,
                                      unit_key='scan:1', source_sig=unrouted, sent_only=True)
    # Synthetic loss of the stored local scan: the source is read again under the same input,
    # while the key file cannot be read for a moment.
    with ledger.db:
        ledger.db.execute('DELETE FROM artifacts WHERE kind IN (?,?)', (state.SCAN, state.CHUNK))
    _key_problem(monkeypatch, 'env_file_unreadable')
    paid = _usage(ledger)
    for _ in range(2):
        outcome = step(runtime, cfg=cfg)
        # Nothing would come back from the cloud for a compared answer: the unit does not wait
        # for the key, and the strict check holds it instead of answering the same prompt again.
        assert (outcome['status'], outcome['reason'], outcome['cloud_refusal']) == (
            'deferred', 'patient_context_cloud_not_sent', 'repeat_success'), outcome
    assert len(sent) == 1 and len(_local_sources(local_calls)) == 1
    assert _usage(ledger) == paid and not _dispositions(ledger) and not _scan_rows(ledger)
