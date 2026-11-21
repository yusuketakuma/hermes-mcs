"""Collect fixed allowlisted telemetry sections for sdc.device-telemetry.v1."""
import json
import math
import os
import platform
import re
import subprocess
import time
import uuid

SCHEMA = 'sdc.device-telemetry.v1'
SECTIONS = ('hardware', 'operations', 'logs', 'mcs')
MAX_REPORT_BYTES = 16384
MAX_LOG_BYTES = 131072
LOG_WINDOW_SECONDS = 3600
MAX_LOG_ENTRIES = 50
_TOKEN = re.compile(r'^[a-z0-9][a-z0-9_.-]{0,63}$')
_EVENT = re.compile(r'^[a-z0-9][a-z0-9_.-]{0,63}$')
_HEALTH_VALUES = {'ok', 'degraded', 'failed', 'unknown'}
_RUN_VALUES = {'ok', 'partial', 'failed', 'session_expired', 'unknown'}


def _sysctl(name, *, timeout=2):
    try:
        result = subprocess.run(['/usr/sbin/sysctl', '-n', name], capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return result.stdout.decode('utf-8').strip() or None
    except UnicodeDecodeError:
        return None


def _int_text(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,20}', value):
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    return number if 0 <= number <= 2**53 - 1 else None


def collect_hardware():
    """Fixed macOS hardware/OS facts only; missing sources stay absent, never guessed."""
    section = {'schema_version': 'sdc.telemetry.hardware.v1'}
    chip = _sysctl('machdep.cpu.brand_string')
    if chip and len(chip) <= 64 and '\n' not in chip and '\r' not in chip:
        section['chip'] = chip
    model = _sysctl('hw.model')
    if model and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9,(). -]{0,31}', model):
        section['model'] = model
    memory = _int_text(_sysctl('hw.memsize') or '')
    if memory is not None:
        section['memory_bytes'] = memory
    name, version, _machine = platform.mac_ver()
    if name and len(name) <= 32:
        section['os_name'] = 'macOS'
        if version and len(version) <= 64 and '\n' not in version and '\r' not in version:
            section['os_version'] = version
    try:
        stats = os.statvfs('/')
        total = stats.f_blocks * stats.f_frsize
        free = stats.f_bavail * stats.f_frsize
        if 0 < total <= 2**53 - 1:
            section['disk_total_bytes'] = total
        if 0 <= free <= 2**53 - 1:
            section['disk_free_bytes'] = free
    except OSError:
        pass
    return section if len(section) > 1 else None


def _service_state(value):
    return value if value in ('running', 'stopped', 'unknown', 'not_running', 'waiting') else 'unknown'


def collect_operations(facts, sdc_state, *, boot_time=None):
    """Uptime/boot id plus fixed service names only; facts come from sdc_cli's fixed probes."""
    section = {'schema_version': 'sdc.telemetry.operations.v1'}
    if isinstance(boot_time, (int, float)) and not isinstance(boot_time, bool) and math.isfinite(boot_time) and boot_time > 0:
        uptime = int(time.time() - boot_time)
        if uptime >= 0:
            section['uptime_seconds'] = uptime
    boot_id = _sysctl('kern.bootsessionuuid')
    if boot_id and re.fullmatch(r'[0-9a-zA-Z][0-9a-zA-Z-]{0,63}', boot_id):
        section['boot_id'] = boot_id
    services = []
    mode = facts.get('mode') if isinstance(facts, dict) else None
    process = facts.get('mcs_process') if isinstance(facts, dict) else None
    if mode in ('hermes', 'standalone') and process in ('running', 'stopped'):
        services.append({'name': 'mcs', 'state': _service_state(process)})
    if isinstance(sdc_state, dict) and sdc_state.get('state') == 'active':
        services.append({'name': 'sdc', 'state': 'running'})
    elif isinstance(sdc_state, dict) and sdc_state.get('state') in ('suspended', 'revoked', 'pending'):
        services.append({'name': 'sdc', 'state': _service_state(sdc_state['state'])})
    if services:
        section['services'] = services[:16]
    return section if len(section) > 1 else None


def _log_level(event):
    if event.endswith('_error') or event.endswith('_failed') or event.endswith('_critical') or event in ('error', 'critical'):
        return 'error'
    if event.endswith('_warn') or event.endswith('_warning') or event.endswith('_degraded') or 'degraded' in event:
        return 'warning'
    return 'info'


def collect_logs(path, *, now=None):
    """Tail fixed-size window of the canonical run log; event names and timestamps only, never message bodies."""
    now_ms = int((time.time() if now is None else now) * 1000)
    entries, dropped = [], 0
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as stream:
            stream.seek(max(0, size - MAX_LOG_BYTES))
            raw = stream.read(MAX_LOG_BYTES)
    except OSError:
        return {'schema_version': 'sdc.telemetry.logs.v1', 'window_seconds': LOG_WINDOW_SECONDS,
                'entries': [], 'dropped': 0}
    for line in raw.split(b'\n'):
        line = line.strip()
        if not line.startswith(b'{') or len(line) > 4096:
            continue
        try:
            record = json.loads(line.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        event, stamp = record.get('event'), record.get('ts')
        if not isinstance(event, str) or not _EVENT.fullmatch(event):
            continue
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp <= 0:
            continue
        at = int(stamp * 1000)
        if at > now_ms or now_ms - at > LOG_WINDOW_SECONDS * 1000:
            continue
        entries.append({'at': at, 'level': _log_level(event), 'source': event})
    entries.sort(key=lambda item: item['at'])
    if len(entries) > MAX_LOG_ENTRIES:
        dropped = len(entries) - MAX_LOG_ENTRIES
        entries = entries[-MAX_LOG_ENTRIES:]
    section = {'schema_version': 'sdc.telemetry.logs.v1', 'window_seconds': LOG_WINDOW_SECONDS,
               'entries': entries, 'dropped': dropped}
    return section


def collect_mcs(health_path, *, now=None):
    """Copy only fixed health.json summary fields; absent or malformed stays absent."""
    try:
        with open(health_path, 'rb') as stream:
            raw = stream.read(262144)
        health = json.loads(raw.decode('utf-8'))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(health, dict):
        return None
    section = {'schema_version': 'sdc.telemetry.mcs.v1'}
    overall = health.get('overall')
    if overall in _HEALTH_VALUES:
        section['health_overall'] = overall
    run_status = health.get('run_status')
    if run_status in _RUN_VALUES:
        section['run_status'] = run_status
    reasons = health.get('state_reasons')
    if isinstance(reasons, list):
        cleaned = [item for item in reasons if isinstance(item, str) and _TOKEN.fullmatch(item)][:16]
        if cleaned:
            section['state_reasons'] = cleaned
    backup = health.get('backup')
    if isinstance(backup, dict):
        local = backup.get('local')
        state = local.get('state') if isinstance(local, dict) else backup.get('state')
        if isinstance(state, str) and _TOKEN.fullmatch(state):
            section['backup_state'] = state
    last_ok = health.get('last_ok_at')
    if isinstance(last_ok, (int, float)) and not isinstance(last_ok, bool) and math.isfinite(last_ok) and last_ok > 0:
        now_s = time.time() if now is None else now
        age = int(now_s - last_ok)
        if age >= 0:
            section['last_ok_age_seconds'] = age
    return section if len(section) > 1 else None


def _validate_section(name, section):
    if not isinstance(section, dict) or len(json.dumps(section, ensure_ascii=True).encode('ascii')) > MAX_REPORT_BYTES:
        return False
    expected = {'hardware': 'sdc.telemetry.hardware.v1', 'operations': 'sdc.telemetry.operations.v1',
                'logs': 'sdc.telemetry.logs.v1', 'mcs': 'sdc.telemetry.mcs.v1'}[name]
    return section.get('schema_version') == expected


def build_report(sections, *, sequence, clock=time.time, only=None):
    """Assemble one bounded envelope; collectors already emit contract-shaped sections."""
    allowed = set(only) if only is not None else set(SECTIONS)
    chosen = {name: sections[name] for name in SECTIONS if name in allowed
              and isinstance(sections.get(name), dict) and _validate_section(name, sections[name])}
    if not chosen:
        return None
    now_ms = int(clock() * 1000)
    report = {'schema_version': SCHEMA, 'report_id': str(uuid.uuid4()), 'sequence': sequence,
              'observed_at': now_ms, 'sections': chosen}
    try:
        raw = json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')
    except (ValueError, TypeError, UnicodeEncodeError):
        return None
    if len(raw) > MAX_REPORT_BYTES or len(chosen) > 8:
        return None
    return report, raw


def collect_sections(facts, sdc_state, *, data_dir, clock=time.time, only=None):
    """Run each consented fixed collector; each section is independent and may be absent."""
    allowed = set(only) if only is not None else set(SECTIONS)
    boot_time = None
    raw_boot = _sysctl('kern.boottime') if 'operations' in allowed else None
    match = re.search(r'sec = (\d+)', raw_boot or '')
    if match:
        boot_time = int(match.group(1))
    return {
        'hardware': collect_hardware() if 'hardware' in allowed else None,
        'operations': collect_operations(facts, sdc_state, boot_time=boot_time) if 'operations' in allowed else None,
        'logs': collect_logs(os.path.join(data_dir, 'mcs.log'), now=clock()) if 'logs' in allowed else None,
        'mcs': collect_mcs(os.path.join(data_dir, 'health.json'), now=clock()) if 'mcs' in allowed else None,
    }
