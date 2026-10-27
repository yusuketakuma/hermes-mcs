"""Extraction audit covers eligible source rows, including missing and failed outputs."""
import json
import sqlite3

import pytest

import export_schema
import extract_llm
import extraction_audit as audit
import mcs_stats
import stats_contract
from semantic_projection import PROJECTION_VERSION
from views_testkit import SCHEMA, SNAP_TS, _msg
from inference_version import CURRENT_VERSION, LEGACY_VERSION, VERSION_KEY


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


def message(db, mid, *, pid=1, state="full", body="本人の薬は合成ボックスで保管しています。"):
    _msg(db, mid, pid=pid, chash=f"h{mid}", state=state, prof="薬剤師")
    db.execute("UPDATE messages SET body_text=? WHERE message_id=?", (body, mid))


def artifact(db, mid, *, kind="extract_llm", content=None, meta=None, pid=1):
    db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,meta,created_at) VALUES(?,?,?,?,?,?)",
        (kind, pid, mid, json.dumps(content if content is not None else {}),
         json.dumps(meta if meta is not None else {VERSION_KEY: CURRENT_VERSION, "hash": f"h{mid}", "extract_version": 7}), SNAP_TS - 10))


def stats(db, **args):
    return mcs_stats.run_stats(db, SNAP_TS, {"preset": "extraction_audit", **args})["stats"]


def test_source_population_reader_priority_and_no_output_reasons(db):
    for mid in range(1, 10):
        message(db, mid, state=None if mid == 1 else "deleted" if mid == 7 else "full")
    db.execute("UPDATE messages SET body_text='' WHERE message_id=8")
    artifact(db, 1)
    artifact(db, 2)
    artifact(db, 2, kind="canonical_projection", content={"canonical_facts": []},
             meta={VERSION_KEY: CURRENT_VERSION, "hash": "h2", "projection_version": PROJECTION_VERSION})
    artifact(db, 4, meta={VERSION_KEY: CURRENT_VERSION, "hash": "old-h4"})
    artifact(db, 5, meta={VERSION_KEY: CURRENT_VERSION, "hash": "h5", "error": "synthetic"})
    artifact(db, 6, pid=99)  # a different patient's output cannot become current
    digest = 'a' * 64
    db.execute('UPDATE messages SET content_hash=? WHERE message_id=9', (digest,))
    artifact(db, 9, meta={VERSION_KEY: CURRENT_VERSION, 'hash': digest, 'extract_version': 7})
    artifact(db, 9, kind="extract_upgrade_v1", content={"state": "held_prior", "hash": digest,
        "prior_artifact_id": db.execute("SELECT MAX(artifact_id) FROM artifacts").fetchone()[0], "target_version": 8})
    rows = list(audit.iter_audit_rows(db, stats_contract.resolve_scope({"limit": 1}, SNAP_TS),
                                     target_version=8, hold_mode='historical'))
    assert len(rows) == 7
    assert [row["reader"] for row in rows] == ["reader_extract", "reader_projection", *["no_output"] * 4, "reader_extract"]
    assert [row["reason"] for row in rows[2:6]] == ["missing", "stale", "failed", "missing"]
    assert rows[-1]["held_prior"] and rows[-1]["quality"] == "attention"
    population = stats(db)["omission_proxy"]["population"]
    assert population == {"cue_posts": 7, "reader_extract": 2, "reader_projection": 1,
        "no_output": 4, "no_output_missing": 2, "no_output_stale": 1, "no_output_failed": 1}


def test_current_and_historical_hold_are_evaluated_against_their_own_target(db):
    message(db, 1)
    digest = 'a' * 64
    db.execute('UPDATE messages SET content_hash=? WHERE message_id=1', (digest,))
    meta = {VERSION_KEY: CURRENT_VERSION, 'hash': digest, 'extract_version': 7,
            'integrity': {'completeness_chunks': 1, 'completeness': {
                '0': {'state': 'hint_gaps_resolved', 'remaining_hint_candidates': 0}}}}
    artifact(db, 1, meta=meta)
    prior = db.execute('SELECT MAX(artifact_id) FROM artifacts').fetchone()[0]
    scope = stats_contract.resolve_scope({}, SNAP_TS)
    assert next(audit.iter_audit_rows(db, scope))['quality'] == 'complete'

    def add_hold(target, **changes):
        receipt = {'state': 'held_prior', 'hash': digest, 'prior_artifact_id': prior,
                   'target_version': target, **changes}
        artifact(db, 1, kind='extract_upgrade_v1', content=receipt)

    add_hold(8)
    legacy = next(audit.iter_audit_rows(db, scope, target_version=8, hold_mode='historical'))
    current = next(audit.iter_audit_rows(db, scope))
    assert (legacy['held_prior'], legacy['quality']) == (True, 'attention')
    assert (current['held_prior'], current['quality']) == (False, 'complete')
    assert extract_llm.EXTRACT_VERSION != 8

    add_hold(extract_llm.EXTRACT_VERSION)
    current = next(audit.iter_audit_rows(db, scope))
    assert (current['held_prior'], current['hold_unknown'], current['quality']) == (False, True, 'attention')
    assert next(audit.iter_audit_rows(db, scope, target_version=8, hold_mode='historical'))['held_prior']
    result = stats(db)['omission_proxy']
    assert result['status'] == 'partial' and result['reason'] == 'hold_status_unverified'
    assert result['hold_unknown_posts'] == 1


@pytest.mark.parametrize('changes', [
    {'hash': 'another'}, {'prior_artifact_id': -1}, {'state': 'applied'},
    {'target_version': True}, {'target_version': '10'},
])
def test_current_hold_requires_matching_receipt_identity(db, changes):
    message(db, 1)
    artifact(db, 1)
    prior = db.execute('SELECT MAX(artifact_id) FROM artifacts').fetchone()[0]
    receipt = {'state': 'held_prior', 'hash': 'h1', 'prior_artifact_id': prior,
               'target_version': extract_llm.EXTRACT_VERSION, **changes}
    artifact(db, 1, kind='extract_upgrade_v1', content=receipt)
    result = next(audit.iter_audit_rows(db, stats_contract.resolve_scope({}, SNAP_TS)))
    assert not result['held_prior']


def test_audit_definition_binds_target_version():
    assert audit.definition_fingerprint(target_version=8) != audit.definition_fingerprint(
        target_version=extract_llm.EXTRACT_VERSION)
    assert audit.definition_fingerprint() == audit.definition_fingerprint(target_version=extract_llm.EXTRACT_VERSION)
    with pytest.raises(ValueError, match='audit_target_version_invalid'):
        audit.definition_fingerprint(target_version=True)


@pytest.mark.parametrize('module_name,function', [
    ('inference', 'read_version'), ('inference', '_valid_version'),
    ('inference', '_target_version'), ('mcs_queries', 'current_meta_pred'),
    ('mcs_queries', 'valid_meta_pred'), ('audit', 'strict_json'), ('audit', 'valid_hash'),
])
def test_audit_definition_binds_version_json_and_hash_readers(monkeypatch, module_name, function):
    import mcs_queries
    import inference_version
    module = {'inference': inference_version, 'mcs_queries': mcs_queries, 'audit': audit}[module_name]
    before = audit.definition_fingerprint()
    def changed(*args, **kwargs):
        return None
    monkeypatch.setattr(module, function, changed)
    assert audit.definition_fingerprint() != before


@pytest.mark.parametrize('name,value', [
    ('VERSION_KEY', 'another_version'), ('LEGACY_VERSION', '0.0'), ('CURRENT_VERSION', '1.1'),
    ('LLM_EXTRACT_VERSION', 11), ('_MAX_VERSION_LENGTH', 31),
])
def test_audit_definition_binds_version_constants(monkeypatch, name, value):
    import inference_version
    target = LEGACY_VERSION if name == 'CURRENT_VERSION' else CURRENT_VERSION
    before = audit.definition_fingerprint(target_version=8, inference_target=target)
    monkeypatch.setattr(inference_version, name, value)
    assert audit.definition_fingerprint(target_version=8, inference_target=target) != before


@pytest.mark.parametrize('name', ['_VERSION', '_COLUMN'])
def test_audit_definition_binds_version_syntax(monkeypatch, name):
    import re
    import inference_version
    before = audit.definition_fingerprint()
    monkeypatch.setattr(inference_version, name, re.compile(r'\d+\.\d+'))
    assert audit.definition_fingerprint() != before


def test_historical_evaluation_separates_the_public_inference_generation(db):
    message(db, 1)
    artifact(db, 1, meta={VERSION_KEY: LEGACY_VERSION, 'hash': 'h1', 'extract_version': 7})
    row = next(audit.iter_audit_rows(db, stats_contract.resolve_scope({}, SNAP_TS), target_version=8))
    assert row['reader'] == 'no_output' and row['extract_version'] is None
    historical = next(audit.iter_audit_rows(db, stats_contract.resolve_scope({}, SNAP_TS),
        target_version=8, inference_target=LEGACY_VERSION, hold_mode='historical'))
    assert historical['reader'] == 'reader_extract' and historical['extract_version'] == 7
    assert audit.definition_fingerprint(target_version=8, inference_target=LEGACY_VERSION,
        hold_mode='historical') != audit.definition_fingerprint(target_version=8,
        inference_target=CURRENT_VERSION, hold_mode='historical')


def test_historical_hold_receipts_are_bound_to_public_generation_and_unknown_is_not_clear(db):
    message(db, 1)
    digest = 'a' * 64
    db.execute('UPDATE messages SET content_hash=? WHERE message_id=1', (digest,))
    # A missing public-version key is the historical 0.1 encoding.
    artifact(db, 1, meta={'hash': digest, 'extract_version': 7})
    prior = db.execute('SELECT MAX(artifact_id) FROM artifacts').fetchone()[0]
    receipt = {'state': 'held_prior', 'hash': digest, 'prior_artifact_id': prior, 'target_version': 8}
    artifact(db, 1, kind='extract_upgrade_v1', content=receipt,
             meta={VERSION_KEY: CURRENT_VERSION, 'hash': digest})
    scope = stats_contract.resolve_scope({}, SNAP_TS)
    def read():
        return next(audit.iter_audit_rows(db, scope, target_version=8,
            inference_target=LEGACY_VERSION, hold_mode='historical'))
    current_receipt_only = read()
    assert current_receipt_only['quality'] == 'complete'
    assert not current_receipt_only['held_prior'] and not current_receipt_only['hold_unknown']

    artifact(db, 1, kind='extract_upgrade_v1', content=receipt, meta={VERSION_KEY: None})
    malformed = read()
    assert not malformed['held_prior'] and malformed['hold_unknown'] and malformed['quality'] == 'attention'

    artifact(db, 1, kind='extract_upgrade_v1', content=receipt, meta={'hash': digest})
    both = read()
    assert both['held_prior'] and both['hold_unknown'] and both['quality'] == 'attention'


def test_historical_hold_with_malformed_json_metadata_is_unknown(db):
    message(db, 1)
    artifact(db, 1, meta={'hash': 'h1', 'extract_version': 7})
    prior = db.execute('SELECT MAX(artifact_id) FROM artifacts').fetchone()[0]
    artifact(db, 1, kind='extract_upgrade_v1', content={
        'state': 'held_prior', 'hash': 'h1', 'prior_artifact_id': prior, 'target_version': 8})
    db.execute("UPDATE artifacts SET meta='{bad' WHERE kind='extract_upgrade_v1'")
    row = next(audit.iter_audit_rows(db, stats_contract.resolve_scope({}, SNAP_TS),
        target_version=8, inference_target=LEGACY_VERSION, hold_mode='historical'))
    assert not row['held_prior'] and row['hold_unknown'] and row['quality'] == 'attention'


@pytest.mark.parametrize('broken', ['content', 'state', 'target_version', 'hash', 'duplicate_content', 'duplicate_meta'])
def test_historical_same_generation_unreadable_receipt_is_not_a_clear_result(db, broken):
    message(db, 1)
    artifact(db, 1, meta={'hash': 'h1', 'extract_version': 7})
    prior = db.execute('SELECT MAX(artifact_id) FROM artifacts').fetchone()[0]
    receipt = {'state': 'held_prior', 'hash': 'h1', 'prior_artifact_id': prior, 'target_version': 8}
    if broken in ('state', 'target_version', 'hash'):
        receipt.pop(broken)
    artifact(db, 1, kind='extract_upgrade_v1', content=receipt, meta={'hash': 'h1'})
    if broken == 'content':
        db.execute("UPDATE artifacts SET content='{bad' WHERE kind='extract_upgrade_v1'")
    elif broken == 'duplicate_content':
        db.execute("UPDATE artifacts SET content=? WHERE kind='extract_upgrade_v1'",
            ('{"state":"held_prior","hash":"h1","prior_artifact_id":%d,"target_version":9,"target_version":8}' % prior,))
    elif broken == 'duplicate_meta':
        db.execute("UPDATE artifacts SET meta=? WHERE kind='extract_upgrade_v1'",
            ('{"inference_engine_version":"0.1","inference_engine_version":"1.0"}',))
    row = next(audit.iter_audit_rows(db, stats_contract.resolve_scope({}, SNAP_TS),
        target_version=8, inference_target=LEGACY_VERSION, hold_mode='historical'))
    assert not row['held_prior'] and row['hold_unknown'] and row['quality'] == 'attention'


@pytest.mark.parametrize('raw_meta,raw_content', [
    ('{"inference_engine_version":"1.0","inference_engine_version":"0.1","hash":"h1"}', '{}'),
    ('{"inference_engine_version":"0.1","inference_engine_version":"1.0","hash":"h1"}', '{}'),
    ('{"inference_engine_version":"1.0","hash":"h1","nested":{"x":1,"x":2}}', '{}'),
    ('{"inference_engine_version":"1.0","hash":"h1"}', '{"summary":"one","summary":"two"}'),
])
def test_duplicate_artifact_keys_are_unknown_for_selected_or_rejected_generation(db, raw_meta, raw_content):
    message(db, 1)
    artifact(db, 1)
    db.execute('UPDATE artifacts SET meta=?, content=? WHERE message_id=1', (raw_meta, raw_content))
    row = next(audit.iter_audit_rows(db, stats_contract.resolve_scope({}, SNAP_TS)))
    result = stats(db)['omission_proxy']
    assert row['reader'] == 'no_output' and row['reason'] == 'unknown'
    assert result['status'] == 'partial' and result['reason'] == 'attempt_generation_unknown'
    assert result['population']['no_output_unknown'] == 1


def test_current_hold_with_duplicate_json_keys_is_unknown(db):
    message(db, 1)
    artifact(db, 1)
    prior = db.execute('SELECT MAX(artifact_id) FROM artifacts').fetchone()[0]
    artifact(db, 1, kind='extract_upgrade_v1', content={'state': 'held_prior',
        'hash': 'h1', 'prior_artifact_id': prior, 'target_version': extract_llm.EXTRACT_VERSION})
    db.execute("UPDATE artifacts SET content=? WHERE kind='extract_upgrade_v1'",
        ('{"state":"held_prior","hash":"different","hash":"h1"}',))
    row = next(audit.iter_audit_rows(db, stats_contract.resolve_scope({}, SNAP_TS)))
    assert not row['held_prior'] and row['hold_unknown'] and row['quality'] == 'attention'
    assert stats(db)['omission_proxy']['status'] == 'partial'


@pytest.mark.parametrize('case,expected', [
    ('invalid_meta', True), ('duplicate_meta', True), ('unknown_generation', True),
    ('invalid_content', True), ('foreign', False), ('older', False),
    ('other_generation', False), ('invalidated', False), ('different_hash', False),
])
def test_newer_unclassified_artifact_does_not_silently_leave_audit_complete(db, case, expected):
    message(db, 1)
    digest = 'a' * 64
    db.execute('UPDATE messages SET content_hash=? WHERE message_id=1', (digest,))
    good_meta = {VERSION_KEY: CURRENT_VERSION, 'hash': digest, 'extract_version': 7,
                 'integrity': {'completeness_chunks': 1, 'completeness': {
                     '0': {'state': 'hint_gaps_resolved', 'remaining_hint_candidates': 0}}}}
    bad_meta = {VERSION_KEY: CURRENT_VERSION, 'hash': digest, 'extract_version': 7}
    if case == 'other_generation':
        bad_meta[VERSION_KEY] = LEGACY_VERSION
    elif case == 'unknown_generation':
        bad_meta[VERSION_KEY] = None
    elif case == 'invalidated':
        bad_meta['invalidated'] = True
    elif case == 'different_hash':
        bad_meta['hash'] = 'b' * 64

    def add_unclassified():
        artifact(db, 1, pid=2 if case == 'foreign' else 1, meta=bad_meta)
        bad_id = db.execute('SELECT MAX(artifact_id) FROM artifacts').fetchone()[0]
        if case in ('invalid_meta', 'foreign', 'older'):
            db.execute("UPDATE artifacts SET meta='{bad' WHERE artifact_id=?", (bad_id,))
        elif case == 'duplicate_meta':
            db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?',
                ('{"inference_engine_version":"0.1","inference_engine_version":"1.0"}', bad_id))
        elif case == 'invalid_content':
            db.execute("UPDATE artifacts SET content='{bad' WHERE artifact_id=?", (bad_id,))

    if case == 'older':
        add_unclassified()
    artifact(db, 1, content={'summary': '合成の既存結果'}, meta=good_meta)
    good_id = db.execute('SELECT MAX(artifact_id) FROM artifacts').fetchone()[0]
    if case != 'older':
        add_unclassified()
    row = next(audit.iter_audit_rows(db, stats_contract.resolve_scope({}, SNAP_TS)))
    result = stats(db)['omission_proxy']
    assert row['reader'] == 'reader_extract' and row['_document']['summary'] == '合成の既存結果'
    assert db.execute('SELECT artifact_id FROM artifacts WHERE artifact_id=?', (good_id,)).fetchone()
    assert row['newer_artifact_unknown'] is expected
    assert row['quality'] == ('attention' if expected else 'complete')
    assert result['status'] == ('partial' if expected else 'ok')
    assert result.get('newer_artifact_unknown_posts', 0) == int(expected)
    if expected:
        assert result['reason'] == 'newer_artifact_unverified'


def test_newer_broken_lower_precedence_does_not_override_canonical(db):
    message(db, 1)
    artifact(db, 1, kind='canonical_projection', content={'canonical_facts': []},
        meta={VERSION_KEY: CURRENT_VERSION, 'hash': 'h1', 'projection_version': PROJECTION_VERSION})
    artifact(db, 1)
    db.execute("UPDATE artifacts SET meta='{bad' WHERE kind='extract_llm'")
    row = next(audit.iter_audit_rows(db, stats_contract.resolve_scope({}, SNAP_TS)))
    assert row['reader'] == 'reader_projection' and not row['newer_artifact_unknown']


def test_newer_broken_higher_precedence_makes_audit_unverified(db):
    message(db, 1)
    artifact(db, 1)
    artifact(db, 1, kind='semantic_facts_v4')
    db.execute("UPDATE artifacts SET meta='{bad' WHERE kind='semantic_facts_v4'")
    row = next(audit.iter_audit_rows(db, stats_contract.resolve_scope({}, SNAP_TS)))
    assert row['reader'] == 'reader_extract' and row['newer_artifact_unknown']
    assert row['quality'] == 'attention' and stats(db)['omission_proxy']['status'] == 'partial'


@pytest.mark.parametrize('pid,meta', [(1, {}), (1, {'invalidated': True}),
    (1, {VERSION_KEY: LEGACY_VERSION}), (None, {}), (2, {})])
def test_current_audit_never_proves_hold_from_unattested_or_foreign_receipt(db, pid, meta):
    message(db, 1)
    artifact(db, 1)
    prior = db.execute('SELECT MAX(artifact_id) FROM artifacts').fetchone()[0]
    artifact(db, 1, kind='extract_upgrade_v1', pid=pid, meta=meta, content={
        'state': 'held_prior', 'hash': 'h1', 'prior_artifact_id': prior,
        'target_version': extract_llm.EXTRACT_VERSION, 'reason': 'protected_loss', 'contract': 'f' * 64})
    row = next(audit.iter_audit_rows(db, stats_contract.resolve_scope({}, SNAP_TS)))
    assert not row['held_prior']
    assert row['hold_unknown'] is (pid == 1)


@pytest.mark.parametrize('change,expected', [
    ({'hash': None}, True), ({'hash': 1}, True), ({'hash': 'invalid'}, True),
    ({'target_version': None}, True), ({'target_version': '10'}, True),
    ({'target_version': True}, True), ({'target_version': 8.0}, True), ({'target_version': 0}, True),
    ({'target_version': -1}, True), ({'state': None}, True), ({'state': 1}, True),
    ({'state': ''}, True), ({'state': 'applied'}, True), ({'state': 'unknown_future'}, True),
    ({'hash': 'b' * 64}, False), ({'target_version': 8}, False),
    ({'hash': None, 'target_version': 8}, False),
    ({'hash': 'b' * 64, 'target_version': None}, False),
])
def test_unknown_hold_when_receipt_identity_is_unclassifiable(db, change, expected):
    message(db, 1)
    digest = 'a' * 64
    db.execute('UPDATE messages SET content_hash=? WHERE message_id=1', (digest,))
    meta = {VERSION_KEY: CURRENT_VERSION, 'hash': digest, 'extract_version': 7,
            'integrity': {'completeness_chunks': 1, 'completeness': {
                '0': {'state': 'hint_gaps_resolved', 'remaining_hint_candidates': 0}}}}
    artifact(db, 1, meta=meta)
    prior = db.execute('SELECT MAX(artifact_id) FROM artifacts').fetchone()[0]
    receipt = {'state': 'held_prior', 'hash': digest, 'prior_artifact_id': prior,
               'target_version': extract_llm.EXTRACT_VERSION, **change}
    artifact(db, 1, kind='extract_upgrade_v1', content=receipt)
    scope = stats_contract.resolve_scope({}, SNAP_TS)
    row = next(audit.iter_audit_rows(db, scope))
    result = stats(db)['omission_proxy']
    assert row['hold_unknown'] is expected and not row['held_prior']
    assert row['quality'] == ('attention' if expected else 'complete')
    assert result['status'] == ('partial' if expected else 'ok')
    assert result.get('hold_unknown_posts', 0) == int(expected)


@pytest.mark.parametrize('missing', ['hash', 'target_version', 'state'])
def test_missing_hold_identity_cannot_count_as_confirmed_or_clear(db, missing):
    message(db, 1)
    artifact(db, 1)
    prior = db.execute('SELECT MAX(artifact_id) FROM artifacts').fetchone()[0]
    receipt = {'state': 'held_prior', 'hash': 'h1', 'prior_artifact_id': prior,
               'target_version': extract_llm.EXTRACT_VERSION}
    receipt.pop(missing)
    artifact(db, 1, kind='extract_upgrade_v1', content=receipt)
    row = next(audit.iter_audit_rows(db, stats_contract.resolve_scope({}, SNAP_TS)))
    assert row['hold_unknown'] and not row['held_prior'] and row['quality'] == 'attention'
    assert stats(db)['omission_proxy']['status'] == 'partial'


def test_historical_equal_but_malformed_hash_is_unknown(db):
    message(db, 1)
    artifact(db, 1, meta={'hash': 'h1', 'extract_version': 7})
    prior = db.execute('SELECT MAX(artifact_id) FROM artifacts').fetchone()[0]
    artifact(db, 1, kind='extract_upgrade_v1', meta={'hash': 'h1'}, content={
        'state': 'held_prior', 'hash': 'h1', 'prior_artifact_id': prior, 'target_version': 8})
    row = next(audit.iter_audit_rows(db, stats_contract.resolve_scope({}, SNAP_TS),
        target_version=8, inference_target=LEGACY_VERSION, hold_mode='historical'))
    assert not row['held_prior'] and row['hold_unknown'] and row['quality'] == 'attention'


@pytest.mark.parametrize('case,reason', [
    ('other_generation', 'missing'), ('same_generation_error', 'failed'),
    ('same_generation_stale', 'stale'), ('unknown_generation', 'unknown'),
    ('unreadable_meta', 'unknown'),
])
def test_unselected_attempts_are_scoped_to_public_generation(db, case, reason):
    message(db, 1)
    meta = {VERSION_KEY: LEGACY_VERSION if case == 'other_generation' else CURRENT_VERSION,
            'hash': 'older' if case == 'same_generation_stale' else 'h1', 'error': True}
    if case == 'unknown_generation':
        meta[VERSION_KEY] = None
    artifact(db, 1, meta=meta)
    if case == 'unreadable_meta':
        db.execute("UPDATE artifacts SET meta='{bad' WHERE message_id=1")
    row = next(audit.iter_audit_rows(db, stats_contract.resolve_scope({}, SNAP_TS)))
    assert row['reader'] == 'no_output' and row['reason'] == reason
    if reason == 'unknown':
        result = stats(db)['omission_proxy']
        assert result['status'] == 'partial' and result['reason'] == 'attempt_generation_unknown'
        assert result['population']['no_output_unknown'] == 1


def test_historical_generation_does_not_count_current_attempt_as_failed(db):
    message(db, 1)
    artifact(db, 1, meta={VERSION_KEY: CURRENT_VERSION, 'hash': 'h1', 'error': True})
    row = next(audit.iter_audit_rows(db, stats_contract.resolve_scope({}, SNAP_TS),
        target_version=8, inference_target=LEGACY_VERSION, hold_mode='historical'))
    assert row['reader'] == 'no_output' and row['reason'] == 'missing'


def test_detail_cap_does_not_cut_the_population_or_class_summary(db):
    for mid in range(1, 206):
        message(db, mid)
        if mid % 2:
            artifact(db, mid, content={"patient_context": [{"category": "medication_management",
                "text": "合成ボックスで保管", "evidence": "本人の薬は合成ボックスで保管しています。"}]})
    result = stats(db, limit=1)["omission_proxy"]
    item = result["by_class"]["items"][0]
    assert (item["class"], item["cue_posts"], item["captured_posts"], item["missed_posts"]) == ("storage", 205, 103, 102)
    assert result["rows"]["truncated"] and result["rows"]["returned"] == 1
    assert not result["by_class"]["truncated"]


def test_validation_unknown_is_not_zero_and_retry_conflict_counts_are_scoped(db):
    for mid in (1, 2, 3):
        message(db, mid)
    artifact(db, 1, meta={VERSION_KEY: CURRENT_VERSION, "hash": "h1", "prefilter": True, "thin_retried": True,
        "integrity": {"validation": {"items_dropped": 2, "evidence_dropped": 1}}})
    artifact(db, 2, meta={VERSION_KEY: CURRENT_VERSION, "hash": "h2", "integrity": {"validation": {
        "items_dropped": 0, "evidence_dropped": 0}}})
    artifact(db, 3, content={"_items_dropped": True})
    result = stats(db)
    assert result["validation_drops"]["status"] == "partial"
    assert result["validation_drops"]["by_class"]["items"][0] == {"class": "storage",
        "observed_posts": 2, "unknown_posts": 1, "items_dropped": 2, "evidence_dropped": 1}
    assert result["prefilter_cue_conflict"]["by_class"]["items"] == [{"class": "storage", "posts": 1}]
    assert result["thin_residual"]["by_class"]["items"] == [{"class": "storage", "posts": 1}]
    assert list(audit.iter_audit_rows(db, stats_contract.resolve_scope({}, SNAP_TS)))[2]["quality"] == "attention"


def test_projection_category_and_verified_guard(db):
    for mid in (1, 2, 3):
        message(db, mid)
        artifact(db, mid, kind="canonical_projection", content={"canonical_facts": [{
            "kind": "adherence_administration" if mid != 3 else "symptom_state",
            "statement": "合成ボックスで保管", "validation_status": "verified" if mid != 2 else "unverified"}]},
            meta={VERSION_KEY: CURRENT_VERSION, "hash": f"h{mid}", "projection_version": PROJECTION_VERSION})
    rows = list(audit.iter_audit_rows(db, stats_contract.resolve_scope({}, SNAP_TS)))
    assert [row["classes"]["storage"] for row in rows] == [True, False, False]


@pytest.mark.parametrize("completeness", [None, [], {"requests": {"state": "hint_gaps_resolved", "remaining_hint_candidates": False}},
    {"requests": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 1}}])
def test_corrupt_or_unresolved_completeness_is_attention(db, completeness):
    message(db, 1)
    artifact(db, 1, meta={VERSION_KEY: CURRENT_VERSION, "hash": "h1", "integrity": {"completeness": completeness}})
    assert next(audit.iter_audit_rows(db, stats_contract.resolve_scope({}, SNAP_TS)))["quality"] == "attention"


def test_aggregate_export_and_read_only_single_source_scan(db, monkeypatch):
    message(db, 1, body="本人の薬は合成ボックスで保管しています。合成秘密ラベル。")
    artifact(db, 1, kind="extract_feedback_v1", content={"field": "patient_context", "note": "合成秘密ラベル"})
    artifact(db, 1, kind="extract_feedback_v1", content={"field": "合成秘密ラベル"})
    original = audit.iter_audit_rows
    calls = []
    def counted(*args):
        calls.append(1)
        yield from original(*args)
    monkeypatch.setattr(audit, "iter_audit_rows", counted)
    db.execute("PRAGMA query_only=ON")
    result = stats(db)
    assert len(calls) == 1
    for name, value in result.items():
        record = {"type": "stat", "contract": "mcs-read-model/1", "snapshot_generation_id": "g",
                  "preset": "extraction_audit", "name": name, "value": value}
        export_schema.validate_record(record)
    text = json.dumps(result, ensure_ascii=False)
    assert text.isascii() and "合成秘密ラベル" not in text
    assert result["feedback_by_field"]["invalid_reports"] == 1
    assert next(r for r in result["feedback_by_field"]["rows"]["items"] if r["field"] == "patient_context")["reports"] == 1
    assert result["attachment_share"]["status"] == "unavailable"


def test_empty_population_and_scope(db):
    assert stats(db)["omission_proxy"]["population"]["cue_posts"] == 0
    message(db, 1, pid=1)
    message(db, 2, pid=2)
    result = stats(db, project=2)["omission_proxy"]
    assert result["population"]["cue_posts"] == 1
    assert result["scope"]["project_id"] == 2


def test_attachment_population_includes_cue_free_posts_and_excludes_withdrawn(db):
    db.execute("CREATE TABLE attachments(message_id INTEGER,state TEXT)")
    message(db, 1)
    message(db, 2, body="合成の挨拶のみです。")
    message(db, 3, pid=2)
    db.executemany("INSERT INTO attachments VALUES (?,?)", [(1, "pending"), (1, "downloaded"),
        (2, "withdrawn"), (3, "failed")])
    result = stats(db, project=1)["attachment_share"]
    assert result["status"] == "ok" and result["population_posts"] == 2
    assert result["rows"]["items"] == [{"reader": "no_output", "length": "len_s", "posts": 2, "with_attachments": 1}]


def test_audit_failure_is_reported_once_without_repeating_the_full_scan(db, monkeypatch):
    calls = []
    def failed(*args):
        calls.append(1)
        raise ValueError("SYNTHETIC_PRIVATE_TEXT")
    monkeypatch.setattr(audit, "collect", failed)
    result = stats(db)
    assert len(calls) == 1
    assert all(value["status"] == "unavailable" for value in result.values())
    assert "SYNTHETIC_PRIVATE_TEXT" not in json.dumps(result)


def test_rule_display_context_cannot_hide_a_raw_llm_omission_or_change_its_digest(db):
    message(db, 1)
    artifact(db, 1, content={"summary": "合成の要約"})
    scope = stats_contract.resolve_scope({}, SNAP_TS)
    before = next(audit.iter_audit_rows(db, scope))
    artifact(db, 1, kind="extract_v1", content={"patient_context": [{
        "category": "medication_management", "text": "合成ボックスで保管しています",
        "evidence": "本人の薬は合成ボックスで保管しています。", "subject": "patient"}]})
    after = next(audit.iter_audit_rows(db, scope))
    assert before["classes"] == after["classes"] == {"storage": False}
    assert before["output_digest"] == after["output_digest"]
    assert "patient_context" not in after["_document"]


def test_cohort_definition_ignores_unrelated_context_or_stat_source_changes(tmp_path, monkeypatch):
    import patient_context
    before = audit.definition_fingerprint()
    changed = tmp_path / "unrelated.py"
    changed.write_text("# unrelated attribute or statistic change\n")
    monkeypatch.setattr(patient_context, "__file__", str(changed))
    monkeypatch.setattr(mcs_stats, "__file__", str(changed))
    assert audit.definition_fingerprint() == before


def test_cohort_definition_binds_the_actual_scope_implementation(monkeypatch):
    before = audit.definition_fingerprint()
    def different_scope(scope, col="m.posted_at_ts"):
        return " AND 0", []
    monkeypatch.setattr(stats_contract, "message_scope_sql", different_scope)
    assert audit.definition_fingerprint() != before


def test_cohort_definition_binds_shared_completeness_interpretation(monkeypatch):
    import extract_quality
    before = audit.definition_fingerprint()
    def different_completeness(meta):
        return True
    monkeypatch.setattr(extract_quality, "completeness_resolved", different_completeness)
    assert audit.definition_fingerprint() != before


def test_cohort_definition_binds_semantic_category_and_projection_version(monkeypatch):
    import semantic_projection
    before = audit.definition_fingerprint()
    monkeypatch.setattr(audit, "KIND_CATEGORY", {**audit.KIND_CATEGORY, "adherence_administration": "symptom_state"})
    assert audit.definition_fingerprint() != before
    monkeypatch.undo()
    monkeypatch.setattr(semantic_projection, "PROJECTION_VERSION", semantic_projection.PROJECTION_VERSION + 1)
    assert audit.definition_fingerprint() != before


def test_cohort_definition_binds_the_canonical_scope_limit(monkeypatch):
    before = audit.definition_fingerprint()
    monkeypatch.setattr(stats_contract, "DETAIL_LIMIT", stats_contract.DETAIL_LIMIT + 1)
    assert audit.definition_fingerprint() != before
