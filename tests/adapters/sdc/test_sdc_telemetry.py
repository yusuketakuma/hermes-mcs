"""Synthetic-only checks for fixed telemetry collectors and the report envelope."""
import json

from adapters.sdc.telemetry import (LOG_WINDOW_SECONDS, MAX_LOG_ENTRIES, MAX_REPORT_BYTES,
                                    SECTIONS, build_report, collect_hardware, collect_logs,
                                    collect_mcs, collect_operations, collect_sections)

NOW = 1792713600.0


def test_collect_sections_emit_only_contract_shaped_sections(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'health.json').write_text(json.dumps({
        'overall': 'degraded', 'run_status': 'partial',
        'state_reasons': ['semantic_backlog_stalled', 'Bad Reason!', 'x' * 100],
        'backup': {'local': {'state': 'verified'}}, 'last_ok_at': NOW - 60}), encoding='utf-8')
    (data / 'mcs.log').write_bytes(b'\n'.join([
        json.dumps({'event': 'extract_loop_error', 'ts': NOW - 30, 'detail': 'must not leak'}).encode(),
        json.dumps({'event': 'nightly', 'ts': NOW - 20}).encode(),
        b'not json at all',
        json.dumps({'event': 'Bad Event!', 'ts': NOW - 10}).encode(),
        json.dumps({'event': 'too_old', 'ts': NOW - LOG_WINDOW_SECONDS - 10}).encode(),
    ]) + b'\n')
    sections = collect_sections({'mode': 'standalone', 'mcs_process': 'running'},
                                {'state': 'active'}, data_dir=str(data), clock=lambda: NOW)
    assert set(sections) == set(SECTIONS)
    logs = sections['logs']
    assert logs['schema_version'] == 'sdc.telemetry.logs.v1' and logs['window_seconds'] == LOG_WINDOW_SECONDS
    assert [entry['source'] for entry in logs['entries']] == ['extract_loop_error', 'nightly']
    assert logs['entries'][0]['level'] == 'error' and logs['entries'][1]['level'] == 'info'
    assert all(set(entry) == {'at', 'level', 'source'} for entry in logs['entries'])
    mcs = sections['mcs']
    assert mcs['health_overall'] == 'degraded' and mcs['run_status'] == 'partial'
    assert mcs['state_reasons'] == ['semantic_backlog_stalled']
    assert mcs['backup_state'] == 'verified' and mcs['last_ok_age_seconds'] == 60
    operations = sections['operations']
    assert {'name': 'mcs', 'state': 'running'} in operations['services']
    assert {'name': 'sdc', 'state': 'running'} in operations['services']
    assert 'message' not in json.dumps(logs)


def test_collect_logs_bounds_entries_and_survives_missing_file(tmp_path):
    missing = tmp_path / 'absent.log'
    section = collect_logs(str(missing), now=NOW)
    assert section['entries'] == [] and section['dropped'] == 0
    path = tmp_path / 'mcs.log'
    lines = [json.dumps({'event': f'event_{index:03d}', 'ts': NOW - index}).encode() for index in range(MAX_LOG_ENTRIES + 10)]
    path.write_bytes(b'\n'.join(lines) + b'\n')
    section = collect_logs(str(path), now=NOW)
    assert len(section['entries']) == MAX_LOG_ENTRIES and section['dropped'] == 10
    assert section['entries'][-1]['source'] == 'event_000'


def test_collect_mcs_rejects_malformed_health(tmp_path):
    bad = tmp_path / 'health.json'
    bad.write_text('{"overall":"degraded","state_reasons":"not-a-list"}', encoding='utf-8')
    section = collect_mcs(str(bad), now=NOW)
    assert section['health_overall'] == 'degraded' and 'state_reasons' not in section
    bad.write_text('not json', encoding='utf-8')
    assert collect_mcs(str(bad), now=NOW) is None
    assert collect_mcs(str(tmp_path / 'missing.json'), now=NOW) is None


def test_build_report_envelope_is_bounded_and_deterministic(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'health.json').write_text(json.dumps({'overall': 'ok', 'run_status': 'ok'}), encoding='utf-8')
    (data / 'mcs.log').write_bytes(b'')
    sections = collect_sections({'mode': 'hermes', 'mcs_process': 'stopped'}, {'state': 'suspended'},
                                data_dir=str(data), clock=lambda: NOW)
    built = build_report(sections, sequence=3, clock=lambda: NOW)
    assert built is not None
    report, raw = built
    assert report['schema_version'] == 'sdc.device-telemetry.v1' and report['sequence'] == 3
    assert report['observed_at'] == int(NOW * 1000)
    assert len(raw) <= MAX_REPORT_BYTES
    assert set(report['sections']) <= set(SECTIONS)
    # Consent-scoped sections are filtered by the sender, not the collector.
    assert build_report({}, sequence=1, clock=lambda: NOW) is None


def test_collect_hardware_and_operations_never_guess():
    hardware = collect_hardware()
    if hardware is not None:
        assert hardware['schema_version'] == 'sdc.telemetry.hardware.v1'
        assert set(hardware) <= {'schema_version', 'chip', 'model', 'memory_bytes', 'os_name',
                                 'os_version', 'disk_total_bytes', 'disk_free_bytes'}
    section = collect_operations({'mode': 'unknown', 'mcs_process': 'unknown'}, {}, boot_time=None)
    assert section is None or set(section) <= {'schema_version', 'uptime_seconds', 'boot_id', 'services'}


def _stub_hardware_inputs(monkeypatch, mac_ver):
    monkeypatch.setattr('adapters.sdc.telemetry._sysctl', lambda *args, **kwargs: None)
    monkeypatch.setattr('platform.mac_ver', lambda: mac_ver)


def test_collect_hardware_macos_release_is_string(tmp_path, monkeypatch):
    _stub_hardware_inputs(monkeypatch, ('15.3', ('', '', ''), 'arm64'))
    hardware = collect_hardware()
    assert hardware['os_name'] == 'macOS' and hardware['os_version'] == '15.3'
    sections = collect_sections({'mode': 'standalone', 'mcs_process': 'running'}, {'state': 'active'},
                                data_dir=str(tmp_path), clock=lambda: NOW)
    assert isinstance(sections['hardware']['os_version'], str)
    built = build_report(sections, sequence=1, clock=lambda: NOW)
    assert built is not None
    assert json.loads(built[1])['sections']['hardware']['os_version'] == '15.3'


def test_collect_hardware_macos_release_missing_or_overlong(monkeypatch):
    _stub_hardware_inputs(monkeypatch, ('', ('', '', ''), 'arm64'))
    hardware = collect_hardware()
    assert 'os_name' not in hardware and 'os_version' not in hardware
    _stub_hardware_inputs(monkeypatch, ('x' * 33, ('', '', ''), 'arm64'))
    hardware = collect_hardware()
    assert 'os_name' not in hardware and 'os_version' not in hardware
    _stub_hardware_inputs(monkeypatch, ('15.3\nextra', ('', '', ''), 'arm64'))
    hardware = collect_hardware()
    assert hardware['os_name'] == 'macOS' and 'os_version' not in hardware
    _stub_hardware_inputs(monkeypatch, (('not', 'a', 'str'), ('', '', ''), 'arm64'))
    hardware = collect_hardware()
    assert 'os_name' not in hardware and 'os_version' not in hardware
