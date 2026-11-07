"""Adversarial partial-context coverage: the included scope stays frozen,
everything else stays unreflected, and a revoked basis can never read back
as current.

Counterexamples for the partial patient-context contract pinned by task 13:
``_snapshot(..., source_keys=[...])`` freezes the included sources into
``source_scope``/``source_coverage`` and ``_inference_ready(...,
source_keys=...)`` only proves the selected materials' local/QC/deps/
attachments completion. Tests assert the documented contract at the real
call seams; they do not weaken assertions to fit an implementation.
"""
import hashlib
import json
import uuid

import pytest

import extract_llm
import extract_llm_runner
import inference_dependency_loss as dependency_loss
import mcs_requests
import patient_context_refresh as context_refresh
import patient_context_refresh_readiness
import patient_context_refresh_sources
import patient_context_refresh_state
import patient_context_plan as context_plan
from adapters.common import envelopes
from extract_testkit import _hash, _message
from inference_version import CURRENT_VERSION, VERSION_KEY
from test_inference_v1_context import _clinical_source, _delete_supported_stop
from test_patient_context_refresh import CFG, document, runtime as runtime, seed, step


def _scoped_snapshot(led, pid, keys, cfg=CFG):
    return patient_context_refresh_sources._snapshot(
        led.db, pid, cfg, hydrate=False, source_keys=keys)


def _scoped_ready(led, pid, keys, clock, cfg=CFG, **kwargs):
    return patient_context_refresh_readiness._inference_ready(
        led, pid, cfg, deadline=clock.mono + 60, source_keys=keys, **kwargs)


def _full_ready(led, pid, clock, cfg=CFG):
    return patient_context_refresh_readiness._inference_ready(
        led, pid, cfg, deadline=clock.mono + 60)


def _whole_snapshot(led, pid, cfg=CFG):
    return patient_context_refresh_sources._snapshot(led.db, pid, cfg, hydrate=False)


def _keys(snapshot):
    return [context_plan.source_key(source) for source in snapshot["sources"]]


def test_ready_subset_is_allowed_while_the_rest_stays_unreflected(runtime):
    """Acceptance 1: completed source 1 + unprocessed source 1 allows the
    partial scope only for the ready member."""
    led, clock, _ = runtime
    seed(led, mid=1, pid=1)
    seed(led, mid=2, pid=1, proof=False)
    scoped = _scoped_snapshot(led, 1, [1])
    assert _keys(scoped) == [1]
    assert scoped["source_scope"] == [1]
    assert scoped["source_coverage"] == {"included": 1, "observed": 2,
                                       "unreflected": 1, "acquisition_complete": True}
    assert _scoped_ready(led, 1, [1], clock)
    assert not _scoped_ready(led, 1, [2], clock)
    assert not _scoped_ready(led, 1, [1, 2], clock)
    assert not _full_ready(led, 1, clock)


def test_late_arrival_stays_unreflected_against_the_frozen_scope(runtime):
    """Acceptance 2: a later arrival leaves the frozen basis readable —
    its signature is unchanged and the new source shows up only as
    unreflected coverage, never as part of the old context."""
    led, clock, _ = runtime
    seed(led, mid=1, pid=1)
    seed(led, mid=2, pid=1, proof=False)
    frozen = _scoped_snapshot(led, 1, [1])
    seed(led, mid=3, pid=1, proof=False)
    later = _scoped_snapshot(led, 1, [1])
    assert later["source_sig"] == frozen["source_sig"]
    assert later["source_scope"] == [1]
    assert later["source_coverage"] == {"included": 1, "observed": 3,
                                      "unreflected": 2, "acquisition_complete": True}
    whole = _whole_snapshot(led, 1)
    assert whole["source_sig"] != frozen["source_sig"]
    assert not _full_ready(led, 1, clock)


def test_editing_an_included_source_revokes_the_frozen_basis(runtime):
    """Acceptance 3: editing an included source changes its signature, so
    the old citation is refused instead of silently re-scoped."""
    led, clock, _ = runtime
    seed(led, mid=1, pid=1)
    seed(led, mid=2, pid=1, proof=False)
    frozen = _scoped_snapshot(led, 1, [1])
    body = "生活環境: 編集後の居住記録。"
    with led.db:
        led.db.execute("UPDATE messages SET body_text=?, body_html=?, content_hash=? "
                       "WHERE project_id=1 AND message_id=1",
                       (body, body, hashlib.sha256(body.encode()).hexdigest()))
    changed = _scoped_snapshot(led, 1, [1])
    assert changed["source_sig"] != frozen["source_sig"]
    assert not _scoped_ready(led, 1, [1], clock)


def test_deleting_an_included_source_revokes_the_frozen_basis(runtime):
    """Acceptance 3: deleting an included source cannot keep the old
    citation — the frozen signature no longer matches."""
    led, clock, _ = runtime
    seed(led, mid=1, pid=1)
    seed(led, mid=2, pid=1, proof=False)
    frozen = _scoped_snapshot(led, 1, [1])
    with led.db:
        led.db.execute("UPDATE messages SET body_state='deleted', body_text='', "
                       "content_hash='' WHERE project_id=1 AND message_id=1")
    changed = _scoped_snapshot(led, 1, [1])
    assert changed["source_sig"] != frozen["source_sig"]


def test_other_patient_and_permission_loss_refuse_the_scope(runtime):
    """Acceptance 4: another patient's source, a missing patient row, and a
    permission revocation each refuse the whole scoped read."""
    led, clock, _ = runtime
    seed(led, mid=1, pid=1)
    led.save_messages([_message(7, "他患者の記録", "full", project_id=2)])
    assert not _scoped_ready(led, 2, [1], clock)
    assert not _scoped_ready(led, 99, [1], clock)
    with led.db:
        led.db.execute("UPDATE patients SET fetch_reason='access_revoked' WHERE project_id=1")
    assert not _scoped_ready(led, 1, [1], clock)
    with led.db:
        led.db.execute("UPDATE patients SET fetch_reason=NULL WHERE project_id=1")
    assert _scoped_ready(led, 1, [1], clock)


@pytest.mark.parametrize("keys", [[], [1, 1], [0], [-3], ["bogus"], [None], [1.5], "1"])
def test_source_scope_rejects_invalid_or_duplicate_keys(runtime, keys):
    """Scoped reads fail closed on malformed scope lists instead of
    widening to everything or guessing a source."""
    led, _, _ = runtime
    seed(led, mid=1, pid=1)
    with pytest.raises(ValueError, match="source_scope_invalid"):
        _scoped_snapshot(led, 1, keys)


def test_pending_ocr_on_another_source_cannot_block_or_leak(runtime):
    """Acceptance 5: an unfinished OCR attachment on an unselected source
    neither blocks the ready scope nor leaks its raw text into it."""
    led, clock, _ = runtime
    cfg = {**CFG, 'attachment_ocr': {'enabled': True, 'executable': '/synthetic/ocr'}}
    seed(led, mid=1, pid=1)
    seed(led, mid=2, pid=1, body="添付つきの記録")
    with led.db:
        led.db.execute("INSERT INTO attachments(message_id,file_id,state) "
                       "VALUES(2,'synthetic','pending')")
    scoped = _scoped_snapshot(led, 1, [1], cfg)
    assert _keys(scoped) == [1]
    assert scoped["attachment_coverage"]["complete"]
    assert _scoped_ready(led, 1, [1], clock, cfg=cfg)
    blocked = _scoped_snapshot(led, 1, [2], cfg)
    assert not blocked["attachment_coverage"]["complete"]
    assert not _scoped_ready(led, 1, [2], clock, cfg=cfg)
    assert _keys(blocked) == [2]           # OCR未完の添付は source として現れない
    assert not _full_ready(led, 1, clock, cfg=cfg)


def test_scope_covering_only_selected_sources_never_claims_full_coverage(runtime):
    """Acceptance 6: coverage over the partial scope counts observed
    sources honestly — a scoped snapshot must not read as 100% while
    other sources stay unanalysed."""
    led, clock, _ = runtime
    seed(led, mid=1, pid=1)
    seed(led, mid=2, pid=1, proof=False)
    scoped = _scoped_snapshot(led, 1, [1])
    coverage = scoped["source_coverage"]
    assert coverage["unreflected"] == 1
    assert coverage["included"] < coverage["observed"]
    whole = _whole_snapshot(led, 1)
    assert len(whole["sources"]) == coverage["observed"] == 2
    report = patient_context_refresh_readiness.inference_coverage(led.db, 1, CFG)
    assert report["complete"] is False


def test_scoped_snapshot_requires_every_selected_source(runtime):
    """A scope naming a source that does not exist in this patient's
    records is refused whole, never silently trimmed."""
    led, clock, _ = runtime
    seed(led, mid=1, pid=1)
    ghost = _scoped_snapshot(led, 1, [1, 4242])
    assert _keys(ghost) == [1]                       # absent member is not fabricated
    assert not _scoped_ready(led, 1, [1, 4242], clock)
    foreign = _scoped_snapshot(led, 1, [1, 2])       # mid=2 belongs to nobody here
    assert _keys(foreign) == [1]
    led.save_messages([_message(2, "他患者の記録", "full", project_id=2)])
    still_mine = _scoped_snapshot(led, 1, [1, 2])
    assert _keys(still_mine) == [1]                  # another patient's key never joins
    assert not _scoped_ready(led, 1, [1, 2], clock)


def test_missing_scope_member_is_refused_at_quality_and_publication(runtime):
    """Snapshot read keeps requested keys in source_scope; the refusal
    lands at the exact-count checks of the inference/quality/publication
    side, so a missing member can never narrow the scope quietly."""
    led, clock, _ = runtime
    seed(led, mid=1, pid=1)
    ghost = _scoped_snapshot(led, 1, [1, 4242])
    assert ghost["source_scope"] == [1, 4242]        # scope keeps the request
    assert _keys(ghost) == [1]
    assert ghost["source_coverage"]["included"] == 1
    assert not _scoped_ready(led, 1, [1, 4242], clock)
    with led.db:
        with pytest.raises(ValueError, match="quality_source_incomplete"):
            patient_context_refresh_sources._quality_capture(
                led.db, 1, CFG, "2026-10-17", [], {}, {}, source_keys=[1, 4242])


def test_unfinished_scan_chunks_never_enter_the_generated_context(runtime, monkeypatch):
    """Audited-only fact adoption: a scan that produced one chunk and then
    failed permanently is a partial artifact — its chunk-derived facts
    must never reach the generated context, even though the CHUNK row
    exists and the source itself stays usable."""
    led, clock, _ = runtime
    unit = 'チャンク混入判定用の独自本文。'
    seed(led, mid=1, pid=1, body='生活環境: 合格側の合成記録。')
    # >SCAN_CHUNK body: the seeded item proves only a unique tail sentence,
    # so the marker inside chunk 0 can only ever arrive through the scan.
    filler = 'QA不合格側の合成記録。'
    seed(led, mid=2, pid=1, body=filler * 200 + unit + filler * 200 + '専用の末尾証拠文。',
         semantic_quote='専用の末尾証拠文。')
    original = context_refresh.local_llm.admitted_chat
    leaked = [True]

    def poison(owner, text, **kwargs):
        try:
            src = json.loads(text).get('source', '')
        except (ValueError, TypeError):
            src = ''
        if isinstance(src, str) and filler in src:
            if leaked[0]:
                leaked[0] = False
                return {'status': 200, 'finish_reason': 'stop', 'text': json.dumps(
                    {'patient_context': [{'category': 'living', 'text': unit,
                                          'evidence': unit,
                                          'subject': 'unspecified', 'details': []}]})}
            return {'status': 200, 'finish_reason': 'length', 'text': '{}'}
        return original(owner, text, **kwargs)
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', poison)
    for _ in range(40):
        result = step(runtime)
        if document(led) is not None and result['status'] != 'progressed':
            break
    chunk = led.db.execute(
        "SELECT content FROM artifacts WHERE kind=? AND message_id=2",
        (patient_context_refresh_state.CHUNK,)).fetchone()
    assert chunk is not None and unit in chunk[0]   # the partial artifact exists
    doc = document(led)
    assert doc is not None and doc['facts']
    assert any(fact['message_id'] == 1 for fact in doc['facts'])
    assert unit not in json.dumps(doc, ensure_ascii=False)
    serialized_summary = json.dumps(doc.get('summary') or [], ensure_ascii=False)
    assert 'QA不合格側の合成記録' not in serialized_summary
    scans = {row['message_id']: json.loads(row['content']) for row in led.db.execute(
        'SELECT message_id,content FROM artifacts WHERE kind=? AND project_id=1',
        (patient_context_refresh_state.SCAN,))}
    assert 2 in scans and not scans[2].get('complete') and not scans[2].get('audited')
    assert not patient_context_refresh_readiness.inference_coverage(led.db, 1, CFG)['complete']


def test_unselected_source_loss_still_limits_selected_facts(runtime):
    """P1 boundary: a deleted stop/correction record in source2 limits
    source1's current fact at the clinical entity/axis level. A scoped
    read must keep cross-source losses and their signature contribution —
    only gap/coverage are filtered per source."""
    led, _, _ = runtime
    _clinical_source(led, 1, '本人は本日薬剤Aを5mg服用中です。', '2026-10-01', dose='5mg')
    _delete_supported_stop(led)
    full = dependency_loss.read(led.db, 1)
    scoped = dependency_loss.read(led.db, 1, source_keys=[1])
    assert scoped['losses'] == full['losses'] and scoped['losses']
    assert scoped['signature'] == full['signature']
    assert scoped['complete']
    snapshot = _scoped_snapshot(led, 1, [1])
    assert snapshot['dependencies']['losses'] == full['losses']
    facts, _, projected = patient_context_refresh_sources._all_facts(
        led.db, 1, snapshot, 'synthetic', projected=True)
    medication = next(f for f in facts if any(d['key'] == 'drug_name' for d in f['details']))
    assert medication['view_bucket'] == 'unknown'
    assert medication['id'] in projected['groups']['unknown']


def test_human_feedback_drops_the_source_until_a_new_bound_proof(runtime, monkeypatch):
    """A human ⚠ report on the CURRENT patient_context extraction re-pends
    the message once: the scoped source leaves eligibility, a stale report
    on the old artifact is refused, and only the existing re-extract/QA
    path's newly bound artifact lets it rejoin. The report note is never
    transcribed into the source material or the re-extract input."""
    led, clock, _ = runtime
    body = '服薬管理: 家族が薬を保管しています。咳あり。'
    seed(led, mid=1, pid=1, body=body)
    aid = led.artifact_add('extract_llm', json.dumps({'summary': '咳あり'}),
                           project_id=1, message_id=1,
                           meta={VERSION_KEY: CURRENT_VERSION, 'hash': _hash(led),
                                 'extract_version': extract_llm.EXTRACT_VERSION})
    assert _scoped_ready(led, 1, [1], clock)
    req = envelopes.extract_feedback(
        'synthetic-operator', {'project_id': 1, 'extract_ref': {
            'message_id': 1, 'artifact_id': aid}}, 'patient_context', '合成の確認結果')
    assert mcs_requests.apply_command(led, req)['outcome'] == 'applied'
    assert not _scoped_ready(led, 1, [1], clock)
    snapshot = _whole_snapshot(led, 1)
    assert 1 not in patient_context_refresh_readiness._eligible_source_keys(
        led, 1, CFG, snapshot, deadline=clock.mono + 60)
    calls = []
    monkeypatch.setattr(extract_llm, 'llm_extract', lambda text, **kw:
                        calls.append(text) or {'summary': '服薬管理の報告'})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    assert extract_llm.run_pending(led, limit=5, budget_s=30)['done'] == 1
    assert calls == [body]                          # the note never becomes model input
    assert led.db.execute('SELECT body_text FROM messages WHERE message_id=1').fetchone()[0] == body
    assert _scoped_ready(led, 1, [1], clock)        # new bound artifact readmits the source
    assert extract_llm.run_pending(led, limit=5, budget_s=30)['selected'] == 0
    stale = {**req, 'command_id': str(uuid.uuid4())}
    assert mcs_requests.apply_command(led, stale)['error'] == 'extraction_changed'
