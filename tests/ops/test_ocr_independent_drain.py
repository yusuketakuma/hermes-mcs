"""The scheduled OCR job drains saved local work without collection or duplicate ownership."""
from types import SimpleNamespace

import pytest

import attachment_inference
import maintenance
import mcs_ocr
import snapshots
from test_attachment_inference import page_store  # noqa: F401
from test_attachment_inference_checkpoints import _long_page
from test_cloud_after_local import local_then_cloud  # noqa: F401
from test_anthropic_runtime import routed  # noqa: F401

__all__ = ['page_store']

CFG = {'attachment_ocr': {'enabled': True, 'executable': '/synthetic/yomitoku'}}


@pytest.fixture
def job(tmp_path, monkeypatch):
    database = tmp_path / 'synthetic.db'
    database.touch()
    state = SimpleNamespace(closed=False, snapshots=0, local=[], raw=[])
    monkeypatch.setattr(mcs_ocr, 'DB', str(database))
    monkeypatch.setattr(mcs_ocr, 'DATA', str(tmp_path))
    monkeypatch.setattr(mcs_ocr.writer_lock, 'updater_state', lambda: (False, 1))
    monkeypatch.setattr(mcs_ocr.writer_lock, 'wait_run_lock',
                        lambda path, **kwargs: mcs_ocr.acquire_run_lock(path))
    monkeypatch.setattr(mcs_ocr, 'Ledger', lambda _: SimpleNamespace(
        close=lambda: setattr(state, 'closed', True)))

    def refresh(*args, **kwargs):
        state.snapshots += 1
        return 'synthetic snapshot'

    monkeypatch.setattr(maintenance, 'refresh_snapshot', refresh)
    monkeypatch.setattr(attachment_inference, 'run_pending', lambda *args, **kwargs: {'done': 0, 'deferred': 0})
    monkeypatch.setattr(mcs_ocr.attachment_ocr, 'run_pending', lambda *args, **kwargs: {'complete': 0})
    return state


def test_drain_is_local_first_and_finishes_multiple_units_with_one_publication(job, monkeypatch):
    events = []
    local = iter([1, 1, 1, 1, 1, 0, 0])
    raw = iter([1, 1, 0])

    def infer(*args, **kwargs):
        events.append('local')
        return {'done': next(local), 'deferred': 0,
                'paid_recovery_pending': len(events) == 1}

    def ocr(*args, **kwargs):
        events.append('ocr')
        return {'complete': next(raw)}

    monkeypatch.setattr(attachment_inference, 'run_pending', infer)
    monkeypatch.setattr(mcs_ocr.attachment_ocr, 'run_pending', ocr)
    result = mcs_ocr.run(CFG)
    assert result['local_done'] == 5 and result['complete'] == 2
    assert result['paid_recovery_pending'] is True
    assert events[:4] == ['local', 'local', 'local', 'ocr']
    assert job.closed and job.snapshots == 1


def test_raw_failure_does_not_stop_remaining_saved_local_pages(job, monkeypatch):
    calls = iter([1, 1, 1, 1, 0])
    monkeypatch.setattr(attachment_inference, 'run_pending', lambda *args, **kwargs: {
        'done': next(calls), 'deferred': 0})

    def ocr(*args, **kwargs):
        raise OSError('synthetic private input failure')

    monkeypatch.setattr(mcs_ocr.attachment_ocr, 'run_pending', ocr)
    result = mcs_ocr.run(CFG)
    assert result['local_done'] == 4 and result['failed'] == 1
    assert result['reason'] == 'local_processing_failed' and job.snapshots == 1


def test_local_failure_does_not_stop_raw_ocr(job, monkeypatch):
    def local(*args, **kwargs):
        raise ValueError('synthetic local checkpoint failure')

    raw = iter([{'complete': 1}, {'complete': 0}])
    monkeypatch.setattr(attachment_inference, 'run_pending', local)
    monkeypatch.setattr(mcs_ocr.attachment_ocr, 'run_pending', lambda *args, **kwargs: next(raw))
    result = mcs_ocr.run(CFG)
    assert result['local_deferred'] == 1 and result['complete'] == 1
    assert result['reason'] == 'local_processing_failed' and job.snapshots == 1


def test_idle_and_busy_stages_end_without_repeating_calls(job, monkeypatch):
    def local(*args, **kwargs):
        job.local.append(True)
        return {'done': 0, 'deferred': 1}

    def ocr(*args, **kwargs):
        job.raw.append(True)
        return {'reason': 'busy', 'complete': 0}

    monkeypatch.setattr(attachment_inference, 'run_pending', local)
    monkeypatch.setattr(mcs_ocr.attachment_ocr, 'run_pending', ocr)
    result = mcs_ocr.run(CFG)
    assert result['local_deferred'] == 1 and job.local == job.raw == [True]
    assert job.closed and job.snapshots == 0


def test_overlapping_job_is_busy_before_database_or_model_work(job):
    lock = mcs_ocr.acquire_run_lock(mcs_ocr.os.path.join(mcs_ocr.DATA, 'attachment_ocr_job.lock'))
    try:
        assert mcs_ocr.run(CFG) == {'reason': 'busy'}
        assert not job.closed and job.snapshots == 0
    finally:
        mcs_ocr.os.close(lock)


def test_total_deadline_bounds_a_continuously_productive_queue(job, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(mcs_ocr.time, 'monotonic', lambda: clock[0])

    def local(*args, deadline, **kwargs):
        assert 0 < deadline - clock[0] <= mcs_ocr.LOCAL_CALL_S
        clock[0] = min(deadline, clock[0] + 100)
        job.local.append(True)
        return {'done': 1, 'deferred': 0}

    monkeypatch.setattr(attachment_inference, 'run_pending', local)
    result = mcs_ocr.run(CFG)
    assert result['queue_status'] == 'partial' and len(job.local) == 9
    assert clock[0] <= 100 + mcs_ocr.DRAIN_BUDGET_S
    assert job.closed


def test_live_disable_stops_the_next_unit_without_losing_saved_counts(job, monkeypatch):
    configs = iter([CFG, CFG, {'attachment_ocr': {'enabled': False}}])
    monkeypatch.setattr(mcs_ocr, 'load_config', lambda: next(configs))
    monkeypatch.setattr(attachment_inference, 'run_pending', lambda *args, **kwargs: {'done': 1, 'deferred': 0})
    result = mcs_ocr.run()
    assert result['local_done'] == 1 and result['reason'] == 'disabled'
    assert job.snapshots == 1 and job.closed


def test_long_ocr_keeps_its_full_timeout_without_consuming_an_attempt_near_job_deadline(job, monkeypatch):
    clock = [100.0]
    cfg = {'attachment_ocr': {**CFG['attachment_ocr'], 'timeout_seconds': 600}}
    monkeypatch.setattr(mcs_ocr.time, 'monotonic', lambda: clock[0])

    def local(*args, deadline, **kwargs):
        clock[0] = min(deadline, clock[0] + 120)
        return {'done': 1, 'deferred': 0}

    def raw(*args, deadline, **kwargs):
        assert deadline - clock[0] >= 600
        job.raw.append(True)
        clock[0] += 600
        return {'complete': 1}

    monkeypatch.setattr(attachment_inference, 'run_pending', local)
    monkeypatch.setattr(mcs_ocr.attachment_ocr, 'run_pending', raw)
    result = mcs_ocr.run(cfg)
    assert result['complete'] == 1 and job.raw == [True]
    assert result.get('failed', 0) == 0 and result['queue_status'] == 'partial'


def test_live_config_failure_stops_units_without_erasing_saved_counts(job, monkeypatch):
    configs = iter([CFG, CFG])

    def config():
        try:
            return next(configs)
        except StopIteration:
            raise mcs_ocr.ProviderSettingsUnreadable('synthetic private settings failure') from None

    monkeypatch.setattr(mcs_ocr, 'load_config', config)
    monkeypatch.setattr(attachment_inference, 'run_pending', lambda *args, **kwargs: {'done': 1, 'deferred': 0})
    result = mcs_ocr.run()
    assert result['local_done'] == 1 and result['reason'] == 'config_invalid'
    assert job.snapshots == 0 and job.closed


def test_snapshot_publication_keeps_absolute_deadline_behind_size_reserve(job, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(mcs_ocr.time, 'monotonic', lambda: clock[0])
    work_deadline = snapshots.snapshot_work_deadline(
        clock[0] + mcs_ocr.DRAIN_BUDGET_S, mcs_ocr.DB)
    local_deadlines, raw_deadlines, refresh_deadlines = [], [], []
    local_calls = []

    def local(*args, **kwargs):
        local_deadlines.append(kwargs['deadline'])
        local_calls.append(True)
        return {'done': 1 if len(local_calls) == 1 else 0, 'deferred': 0}

    def raw(*args, **kwargs):
        raw_deadlines.append(kwargs['deadline'])
        return {'complete': 0}

    def refresh(*args, **kwargs):
        refresh_deadlines.append(kwargs['deadline'])
        return 'published'

    monkeypatch.setattr(attachment_inference, 'run_pending', local)
    monkeypatch.setattr(mcs_ocr.attachment_ocr, 'run_pending', raw)
    monkeypatch.setattr(maintenance, 'refresh_snapshot', refresh)
    result = mcs_ocr.run(CFG)
    assert result.get('reason') != 'snapshot_deferred'
    assert local_deadlines and all(d <= work_deadline - 5 for d in local_deadlines)
    assert raw_deadlines == [work_deadline - 5]
    assert refresh_deadlines == [clock[0] + mcs_ocr.DRAIN_BUDGET_S]


@pytest.mark.parametrize('grow', [True, False])
def test_snapshot_budget_reevaluates_source_size_before_publish(job, monkeypatch, grow):
    clock = [100.0]
    monkeypatch.setattr(mcs_ocr.time, 'monotonic', lambda: clock[0])
    grown, calls = [], []

    def local(*args, **kwargs):
        clock[0] = 850.0
        calls.append(True)
        if grow and not grown:
            grown.append(True)
            with open(mcs_ocr.DB, 'r+b') as stream:
                stream.truncate(800 * 1024 * 1024)
        return {'done': 1 if len(calls) == 1 else 0, 'deferred': 0}

    monkeypatch.setattr(attachment_inference, 'run_pending', local)
    monkeypatch.setattr(mcs_ocr.attachment_ocr, 'run_pending',
                        lambda *args, **kwargs: {'complete': 0})
    result = mcs_ocr.run(CFG)
    if grow:
        assert result['reason'] == 'snapshot_deferred' and job.snapshots == 0
    else:
        assert result.get('reason') is None and job.snapshots == 1


def test_local_page_budget_completes_two_slow_chunks_in_one_drain(page_store, monkeypatch):
    ledger, cfg, key, calls, _, answers = page_store
    body = '合成記録A。眠気はありません。\n合成記録B。頭痛はありません。\n'
    _long_page(ledger, key, body)
    size = len(body) // 2
    monkeypatch.setattr(attachment_inference.extract_llm, '_CHUNK_SIZE', size)
    assert len(attachment_inference.extract_llm.plan_chunks(body, size)) == 2
    clock = [1000.0]
    monkeypatch.setattr(attachment_inference.time, 'monotonic', lambda: clock[0])
    original = attachment_inference.extract_llm.local_llm.chat

    def slow(prompt, **kwargs):
        target = prompt.rsplit('<<<', 1)[1].split('>>>')[0]
        symptom = '眠気' if '眠気' in target else '頭痛'
        answers['local'] = {'symptoms': [{'text': symptom, 'negated': True, 'subject': 'patient',
            'status': 'ongoing', 'evidence': symptom + 'はありません。'}]}
        response = original(prompt, **kwargs)
        clock[0] += 80
        return response

    monkeypatch.setattr(attachment_inference.extract_llm.local_llm, 'chat', slow)
    monkeypatch.setattr(attachment_inference.extract_llm.local_llm, 'admitted_chat',
                        lambda client, prompt, **kw: slow(prompt, **kw))
    monkeypatch.setattr(mcs_ocr.writer_lock, 'updater_state', lambda: (False, 1))
    monkeypatch.setattr(mcs_ocr.attachment_ocr, 'run_pending',
                        lambda *args, **kwargs: {'complete': 0})
    result = mcs_ocr._drain(ledger, cfg, (False, 1), clock[0] + mcs_ocr.DRAIN_BUDGET_S,
                            live_config=False)
    assert result['local_done'] == 1 and result['local_deferred'] == 0
    assert len(calls) == 2
    assert attachment_inference.current(ledger.db, 1, key, cfg=cfg)['ready'] is True
