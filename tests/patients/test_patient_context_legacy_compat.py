"""Legacy identifier reads for the all-day patient-context pipeline."""
import json

import patient_context_refresh_state
import patient_context_scan
import cloud_backlog
import mcs_util
import pytest

from extract_testkit import _ledger
from inference_version import CURRENT_VERSION, VERSION_KEY


def _meta(**extra):
    return {VERSION_KEY: CURRENT_VERSION, **extra}


def _insert(led, kind, pid=1, content=None, meta=None):
    return led.db.execute(
        "INSERT INTO artifacts(kind,project_id,content,meta,created_at) VALUES(?,?,?,?,?)",
        (kind, pid, json.dumps(content or {}),
         json.dumps(meta if meta is not None else _meta()), 0)).lastrowid


def test_options_reads_legacy_key_when_new_absent():
    cfg = {'extract': {'patient_context_nightly': {'enabled': True, 'publication': False}}}
    assert patient_context_refresh_state.options(cfg) == {'enabled': True, 'publication': False}


def test_options_prefers_new_key_over_legacy():
    cfg = {'extract': {'patient_context': {'enabled': False, 'publication': True},
                       'patient_context_nightly': {'enabled': True, 'publication': True}}}
    assert patient_context_refresh_state.options(cfg) == {'enabled': False, 'publication': True}


def test_options_malformed_new_key_stays_closed_despite_legacy():
    cfg = {'extract': {'patient_context': 'yes',
                       'patient_context_nightly': {'enabled': True, 'publication': True}}}
    assert patient_context_refresh_state.options(cfg) == {'enabled': False, 'publication': False}


def test_options_malformed_legacy_key_stays_closed():
    cfg = {'extract': {'patient_context_nightly': {'enabled': 'yes'}}}
    assert patient_context_refresh_state.options(cfg) == {'enabled': False, 'publication': False}


def test_options_defaults_and_non_dict_config():
    assert patient_context_refresh_state.options({}) == {'enabled': True, 'publication': True}
    assert patient_context_refresh_state.options({'extract': {}}) == {'enabled': True, 'publication': True}
    assert patient_context_refresh_state.options({'extract': {'patient_context': {}}}) == \
        {'enabled': True, 'publication': True}
    assert patient_context_refresh_state.options(None) == {'enabled': False, 'publication': False}


def test_latest_reads_legacy_kind_and_prefers_newer_current(tmp_path):
    led = _ledger(tmp_path)
    try:
        _insert(led, patient_context_refresh_state.LEGACY_KIND, content={'v': 'legacy'})
        content, _meta_row = patient_context_refresh_state._latest(
            led.db, patient_context_refresh_state.KIND, 1)
        assert content == {'v': 'legacy'}
        _insert(led, patient_context_refresh_state.KIND, content={'v': 'current'})
        content, _ = patient_context_refresh_state._latest(
            led.db, patient_context_refresh_state.KIND, 1)
        assert content == {'v': 'current'}
    finally:
        led.close()


def test_replace_migrates_legacy_row_to_current_kind(tmp_path):
    led = _ledger(tmp_path)
    try:
        _insert(led, patient_context_refresh_state.LEGACY_KIND, content={'v': 'legacy'})
        patient_context_refresh_state._replace(
            led, patient_context_refresh_state.KIND, 1, {'v': 'current'}, {})
        rows = led.db.execute(
            "SELECT kind,content FROM artifacts WHERE project_id=1 ORDER BY artifact_id").fetchall()
        assert [(r[0], json.loads(r[1])) for r in rows] == [
            (patient_context_refresh_state.KIND, {'v': 'current'})]
        content, _ = patient_context_refresh_state._latest(
            led.db, patient_context_refresh_state.KIND, 1)
        assert content == {'v': 'current'}
    finally:
        led.close()


def test_published_version_counts_legacy_kind(tmp_path):
    led = _ledger(tmp_path)
    try:
        legacy_id = _insert(led, patient_context_refresh_state.LEGACY_KIND)
        assert patient_context_refresh_state._published_version(led.db, 1) == legacy_id
        current_id = _insert(led, patient_context_refresh_state.KIND)
        assert patient_context_refresh_state._published_version(led.db, 1) == current_id
    finally:
        led.close()


def test_facts_signature_ignores_workday_bookkeeping_fields():
    base = {'id': 'f1', 'text': 'x', 'category': 'living'}
    variants = [dict(base, found_workday_id='2026-10-17'),
                dict(base, found_night_id='2026-10-17'),
                dict(base, found_workday_id='2026-10-18', found_night_id='2026-10-17',
                     found_at=1, discovery='scan')]
    signatures = {patient_context_scan.facts_signature([fact]) for fact in variants}
    assert len(signatures) == 1


@pytest.mark.parametrize('code', [
    'patient_context_source_changed', 'patient_context_source_repair_unresolved',
    'patient_context_length_stop', 'patient_context_audit_mismatch'])
def test_safe_error_code_echoes_current_failure_family(code):
    assert mcs_util.safe_error_code(ValueError(code)) == code


@pytest.mark.parametrize('code', [
    'nightly_source_changed', 'nightly_source_repair_unresolved',
    'nightly_length_stop', 'nightly_audit_mismatch'])
def test_safe_error_code_echoes_legacy_failure_family(code):
    assert mcs_util.safe_error_code(ValueError(code)) == code


@pytest.mark.parametrize('reason', [
    'patient_context_cloud_not_sent', 'patient_context_source_repair_lock',
    'nightly_cloud_not_sent', 'nightly_source_repair_lock'])
def test_context_pass_reasons_cover_both_failure_families(reason):
    assert reason in cloud_backlog._CONTEXT_PASS_REASONS
