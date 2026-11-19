"""Receipts cited by current patient context keep their whole reservation past retention."""
import json
import time

import pytest

import maintenance
from inference_version import CURRENT_VERSION, LEGACY_VERSION, VERSION_KEY
from test_maintenance_retention_cleanup import _ledger, _repair_accounting, _expire_repair_accounting

CHUNK, SCAN, SUMMARY = 'patient_context_scan_chunk_v1', 'patient_context_scan_v1', 'patient_context_v1'
REPAIR = 'patient_context_summary_quality_repair_v1'
CANDIDATE = 'patient_context_summary_quality_repair_candidate_v1'
RECEIPTS = ('reserve', 'result', 'disposition')
INPUT_KEY = '1' * 64


def _cite(db, pid, rows, kind=CHUNK, *, version=CURRENT_VERSION, **extra):
    meta = {'cloud_receipts': [{'artifact_id': rows['reserve'], 'witness': '0' * 64}], **extra}
    if version is not None:
        meta[VERSION_KEY] = version
    return db.artifact_add(kind, '{}', project_id=pid, meta=meta)


def _spend_repair(db, pid, rows, marker):
    return db.artifact_add(REPAIR, '{}', project_id=pid, meta={
        VERSION_KEY: CURRENT_VERSION, 'input_key': INPUT_KEY, 'candidate_key': '2' * 64,
        'reservation_artifact_id': rows['reserve'], 'reservation_id': marker * 32, 'request_fp': 'f' * 64})


def _candidate(db, pid):
    return db.artifact_add(CANDIDATE, '{"fact_ids":[]}', project_id=pid, meta={
        VERSION_KEY: CURRENT_VERSION, 'input_key': INPUT_KEY, 'candidate_key': '3' * 64,
        'audit_key': '4' * 64, 'original_failure_key': '5' * 64, 'repair_version': 1})


def _publish_repaired(db, pid, candidate_id):
    return db.artifact_add(SUMMARY, '{}', project_id=pid, meta={
        VERSION_KEY: CURRENT_VERSION, 'quality_repair_ref': {'artifact_id': candidate_id, 'digest': '6' * 64}})


def _prune(db, now):
    _expire_repair_accounting(db, now)
    maintenance._prune_cloud_usage(db.db, now - maintenance.CLOUD_USAGE_KEEP_S)
    return {row[0] for row in db.db.execute('SELECT artifact_id FROM artifacts')}


def _kept(rows):
    return {rows[key] for key in RECEIPTS}


@pytest.mark.parametrize('batch', [1, 5000])
@pytest.mark.parametrize('kind', [CHUNK, SCAN, SUMMARY])
def test_current_citation_pins_the_whole_reservation_and_nothing_else(tmp_path, monkeypatch, kind, batch):
    db = _ledger(tmp_path)
    try:
        now = time.time()
        cited = _repair_accounting(db, 1, 'a', header=False, slot=False)
        same_patient = _repair_accounting(db, 1, 'b', header=False, slot=False)
        other_patient = _repair_accounting(db, 2, 'c', header=False, slot=False)
        citation = _cite(db, 1, cited, kind)
        monkeypatch.setattr(maintenance, 'RETENTION_BATCH', batch)
        remaining = _prune(db, now)
        assert _kept(cited) <= remaining and citation in remaining
        assert not {cited['dryrun'], cited['compare']}.intersection(remaining)
        assert not set(same_patient.values()).intersection(remaining)
        assert not set(other_patient.values()).intersection(remaining)
    finally:
        db.close()


@pytest.mark.parametrize('version', [LEGACY_VERSION, None, '0.9'])
def test_citation_of_another_engine_version_pins_nothing(tmp_path, version):
    db = _ledger(tmp_path)
    try:
        now = time.time()
        cited = _repair_accounting(db, 1, 'a', header=False, slot=False)
        _cite(db, 1, cited, version=version)
        assert not set(cited.values()).intersection(_prune(db, now))
    finally:
        db.close()


@pytest.mark.parametrize('batch', [1, 5000])
@pytest.mark.parametrize('published', [False, True])
def test_spent_summary_repair_pins_its_reservation_with_or_without_a_published_reference(
        tmp_path, monkeypatch, published, batch):
    db = _ledger(tmp_path)
    try:
        now = time.time()
        repaired = _repair_accounting(db, 1, 'a', header=False, slot=False)
        same_patient = _repair_accounting(db, 1, 'b', header=False, slot=False)
        _spend_repair(db, 1, repaired, 'a')
        candidate = _candidate(db, 1)
        if published:
            _publish_repaired(db, 1, candidate)
        monkeypatch.setattr(maintenance, 'RETENTION_BATCH', batch)
        remaining = _prune(db, now)
        assert _kept(repaired) <= remaining
        assert not {repaired['dryrun'], repaired['compare']}.intersection(remaining)
        assert not set(same_patient.values()).intersection(remaining)
    finally:
        db.close()


@pytest.mark.parametrize('batch', [1, 5000])
def test_same_reservation_id_in_another_scope_is_retained_as_rejection_evidence(tmp_path, monkeypatch, batch):
    db = _ledger(tmp_path)
    try:
        now = time.time()
        cited = _repair_accounting(db, 1, 'a', header=False, slot=False)
        foreign = _repair_accounting(db, 2, 'a', header=False, slot=False)
        unrelated = _repair_accounting(db, 2, 'c', header=False, slot=False)
        _cite(db, 1, cited)
        monkeypatch.setattr(maintenance, 'RETENTION_BATCH', batch)
        remaining = _prune(db, now)
        assert _kept(cited) | _kept(foreign) <= remaining
        assert not set(unrelated.values()).intersection(remaining)
    finally:
        db.close()


@pytest.mark.parametrize('broken', ['not_json', 'duplicate_key', 'nonfinite', 'not_object', 'bad_version',
                                    'receipts_null', 'receipts_empty', 'receipts_shape', 'repair_row',
                                    'repair_ref_shape', 'repair_ref_missing_candidate', 'repair_ref_unspent'])
def test_unreadable_context_binding_holds_only_its_patient_receipts(tmp_path, broken):
    db = _ledger(tmp_path)
    try:
        now = time.time()
        first = _repair_accounting(db, 1, 'a', header=False, slot=False)
        second = _repair_accounting(db, 1, 'b', header=False, slot=False)
        other = _repair_accounting(db, 2, 'c', header=False, slot=False)
        current = '"%s":"%s"' % (VERSION_KEY, CURRENT_VERSION)
        raw = {'not_json': '{"broken":', 'duplicate_key': '{%s,%s}' % (current, current),
               'nonfinite': '{%s,"number":1e999}' % current, 'not_object': '[]',
               'bad_version': json.dumps({VERSION_KEY: 1}),
               'receipts_null': json.dumps({VERSION_KEY: CURRENT_VERSION, 'cloud_receipts': None}),
               'receipts_empty': json.dumps({VERSION_KEY: CURRENT_VERSION, 'cloud_receipts': []}),
               'receipts_shape': json.dumps({VERSION_KEY: CURRENT_VERSION, 'cloud_receipts': [{'artifact_id': '1'}]}),
               }.get(broken)
        if raw is not None:
            row = db.artifact_add(CHUNK, '{}', project_id=1, meta={})
            db.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (raw, row))
        elif broken == 'repair_row':
            db.artifact_add(REPAIR, '{}', project_id=1, meta={VERSION_KEY: CURRENT_VERSION, 'input_key': INPUT_KEY})
        elif broken == 'repair_ref_shape':
            db.artifact_add(SUMMARY, '{}', project_id=1,
                            meta={VERSION_KEY: CURRENT_VERSION, 'quality_repair_ref': {'artifact_id': 'x'}})
        elif broken == 'repair_ref_missing_candidate':
            _spend_repair(db, 1, first, 'a')
            _publish_repaired(db, 1, 2 ** 40)
        else:
            _publish_repaired(db, 1, _candidate(db, 1))   # the candidate's spent reservation is not recorded
        db.db.commit()
        remaining = _prune(db, now)
        assert _kept(first) | _kept(second) <= remaining
        assert not {first['dryrun'], first['compare'], second['dryrun'], second['compare']}.intersection(remaining)
        assert not set(other.values()).intersection(remaining)
    finally:
        db.close()


def test_unreadable_receipt_of_a_citing_patient_is_retained(tmp_path):
    db = _ledger(tmp_path)
    try:
        now = time.time()
        cited = _repair_accounting(db, 1, 'a', header=False, slot=False)
        damaged = _repair_accounting(db, 1, 'b', header=False, slot=False)
        elsewhere = _repair_accounting(db, 2, 'c', header=False, slot=False)
        _cite(db, 1, cited)
        db.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id IN (?,?)',
                      ('{"broken":', damaged['result'], elsewhere['result']))
        db.db.commit()
        remaining = _prune(db, now)
        # It may be this patient's cited result or disposition, so it cannot be proved unrelated.
        assert damaged['result'] in remaining and elsewhere['result'] not in remaining
        assert not {damaged['reserve'], damaged['disposition']}.intersection(remaining)
    finally:
        db.close()


@pytest.mark.parametrize('batch', [1, 5000])
def test_a_young_local_replacement_record_is_pruned_with_its_expired_reservation(tmp_path, monkeypatch, batch):
    db = _ledger(tmp_path)
    try:
        now = time.time()
        replaced = _repair_accounting(db, 1, 'a', header=False, slot=False)
        cited = _repair_accounting(db, 1, 'b', header=False, slot=False)
        discarded = _repair_accounting(db, 1, 'c', header=False, slot=False)
        foreign = _repair_accounting(db, 2, 'd', header=False, slot=False)
        _cite(db, 1, cited)
        _expire_repair_accounting(db, now)
        # Local work replaced these old paid requests only now: the record of that is young.
        for rows, value, pid in ((replaced, 'superseded_local', 1), (cited, 'superseded_local', 1),
                                 (discarded, 'discarded_scope', 1), (foreign, 'superseded_local', 2)):
            db.db.execute("UPDATE artifacts SET created_at=?,meta=json_set(meta,'$.disposition',?) "
                          "WHERE artifact_id=?", (now, value, rows['disposition']))
        # Another patient's record that names the first reservation is not this reservation's.
        stray = db.artifact_add('cloud_usage_disposition', '{}', project_id=2, meta={
            'project_id': 2, 'reservation_id': 'a' * 32, 'disposition': 'superseded_local'})
        db.db.commit()
        monkeypatch.setattr(maintenance, 'RETENTION_BATCH', batch)
        maintenance._prune_cloud_usage(db.db, now - maintenance.CLOUD_USAGE_KEEP_S)
        remaining = {row[0] for row in db.db.execute('SELECT artifact_id FROM artifacts')}
        # A replacement record says nothing once its reservation is gone: it leaves with it,
        # so no later reader meets a disposition without a reservation.
        assert not _kept(replaced).intersection(remaining)
        assert not _kept(foreign).intersection(remaining)
        # A reservation a current context still cites keeps its whole history, the record too.
        assert _kept(cited) <= remaining
        # Other dispositions and other scopes keep their own retention.
        assert {discarded['disposition'], stray} <= remaining
        assert not {discarded['reserve'], discarded['result']}.intersection(remaining)
    finally:
        db.close()
