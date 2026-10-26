"""Build fixed DTOs and authenticate exact raw request bytes."""
import hashlib
import hmac
import json
import math
import re
import secrets
import time

PATHS = frozenset({'/v2/enrollments/claim', '/v2/enrollments/status', '/v2/enrollments/confirm', '/v2/heartbeats', '/v2/device/suspend', '/v2/keys/prepare', '/v2/keys/commit', '/v2/keys/status', '/v2/inventory/pages'})
SUPPORT_PATHS = frozenset({'/v2/support/cases/preview', '/v2/support/cases/accept',
                           '/v2/support/requests/poll', '/v2/support/operations/admit',
                           '/v2/support/operations/grant', '/v2/support/operations/report',
                           '/v2/support/operations/status'})
PROVISION_ACK = '/v2/provisioning/ack'
PROVISION_STATUS = '/v2/provisioning/status'
PROVISION_PATHS = frozenset((PROVISION_ACK, PROVISION_STATUS))
DIAGNOSTIC_EVENTS_REPORT = '/v2/diagnostics/events/report'
DIAGNOSTIC_EVENTS_STATUS = '/v2/diagnostics/events/status'
DIAGNOSTIC_ARTIFACT_SUBMIT = '/v2/diagnostics/case-artifacts/submit'
DIAGNOSTIC_PATHS = frozenset((DIAGNOSTIC_EVENTS_REPORT, DIAGNOSTIC_EVENTS_STATUS,
                              DIAGNOSTIC_ARTIFACT_SUBMIT))
_HMAC_HEADERS = frozenset({'X-SDC-Device', 'X-SDC-Key-ID', 'X-SDC-Time', 'X-SDC-Nonce', 'X-SDC-Signature'})
_UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}'
CONSENT = 'sdc-self-monitoring-v1'
CUSTOMER_CONSENT = 'sdc-customer-monitoring-v1'
VERSION = '0.1.0'


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('SDC_INVALID_JSON')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('SDC_INVALID_JSON')
    try:
        obj = json.loads(raw.decode('utf-8', errors='strict'), object_pairs_hook=pairs, parse_constant=constant)
    except RecursionError:
        raise ValueError('SDC_INVALID_JSON') from None
    def depth(value, level=0):
        if level > 8 or isinstance(value, float) and not math.isfinite(value):
            raise ValueError('SDC_INVALID_JSON')
        for child in value.values() if isinstance(value, dict) else value if isinstance(value, list) else ():
            depth(child, level + 1)
    depth(obj)
    return obj


def endpoint(config):
    if not isinstance(config, dict) or set(config) != {'origin', 'audience', 'environment'} or not re.fullmatch(r'https://[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', config.get('origin', '')) or '..' in config['origin']:
        raise ValueError('SDC_ENDPOINT_NOT_APPROVED')
    if not re.fullmatch(r'[A-Za-z0-9:_-]{1,100}', config.get('audience', '')) or not re.fullmatch(r'[a-z0-9_-]{1,32}', config.get('environment', '')):
        raise ValueError('SDC_ENDPOINT_NOT_APPROVED')
    return config


def raw_body(body):
    raw = json.dumps(body, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode()
    if len(raw) > 16384:
        raise ValueError('SDC_BODY_TOO_LARGE')
    return raw


def sign(config, path, raw, state, key, *, now=None, nonce=None):
    endpoint(config)
    if path not in PATHS or not re.fullmatch(r'[0-9a-f]{64}', key):
        raise ValueError('SDC_SIGNING_INVALID')
    return _sign(config, path, raw, state, key, now=now, nonce=nonce)


def _sign(config, path, raw, state, key, *, now, nonce):
    stamp = str(int(time.time() if now is None else now))
    nonce = nonce or secrets.token_hex(16)
    canonical = '\n'.join(['SDC-HMAC-v1', config['audience'], 'POST', path, state['device_id'], state['key_id'], stamp, nonce, hashlib.sha256(raw).hexdigest()])
    return {'X-SDC-Device': state['device_id'], 'X-SDC-Key-ID': state['key_id'], 'X-SDC-Time': stamp, 'X-SDC-Nonce': nonce, 'X-SDC-Signature': hmac.new(bytes.fromhex(key), canonical.encode(), hashlib.sha256).hexdigest()}



def _support_request(path, raw, *, paths=SUPPORT_PATHS):
    from .support_contract import parse_canonical
    if type(path) is not str or path not in paths or type(raw) is not bytes or len(raw) > 16384:
        raise ValueError('SDC_REQUEST_INVALID')
    try:
        value = parse_canonical(raw, max_bytes=16384)
        if type(value) is not dict:
            raise ValueError
    except ValueError:
        raise ValueError('SDC_REQUEST_INVALID') from None


def _restart_request(path, raw):
    from .restart_control import PATHS as RESTART_PATHS
    _support_request(path, raw, paths=RESTART_PATHS)


def _provision_request(path, raw):
    from .provision_contract import parse_provision
    _support_request(path, raw, paths=PROVISION_PATHS)
    definition = 'AckRequest' if path == PROVISION_ACK else 'StatusRequest'
    parse_provision(definition, raw, now_ms=int(time.time() * 1000))


def _diagnostic_request(path, raw):
    from .diagnostic_contract import parse_diagnostic_shape
    if type(path) is not str or path not in DIAGNOSTIC_PATHS or type(raw) is not bytes or len(raw) > 16384:
        raise ValueError('SDC_REQUEST_INVALID')
    definition = {DIAGNOSTIC_EVENTS_REPORT: 'Batch', DIAGNOSTIC_EVENTS_STATUS: 'StatusRequest',
                  DIAGNOSTIC_ARTIFACT_SUBMIT: 'ArtifactSubmit'}[path]
    try:
        # Stored-ID replays keep their exact bytes; their owner checks current consent and freshness.
        parse_diagnostic_shape(definition, raw)
    except ValueError:
        raise ValueError('SDC_REQUEST_INVALID') from None


def _support_headers(headers):
    if type(headers) is not dict or set(headers) != _HMAC_HEADERS:
        return False
    if not all(type(value) is str for value in headers.values()):
        return False
    return (all(re.fullmatch(_UUID, headers[name]) for name in ('X-SDC-Device', 'X-SDC-Key-ID'))
            and re.fullmatch(r'(?:0|[1-9][0-9]{0,15})', headers['X-SDC-Time']) is not None
            and int(headers['X-SDC-Time']) <= 2**53 - 1
            and re.fullmatch(r'[0-9a-f]{32}', headers['X-SDC-Nonce']) is not None
            and re.fullmatch(r'[0-9a-f]{64}', headers['X-SDC-Signature']) is not None)


def sign_support(config, path, raw, state, key, *, now, nonce):
    """Sign only fixed support POST bytes; caller owns trusted endpoint, consent and authority."""
    _support_request(path, raw)
    return _sign_support_request(config, path, raw, state, key, now=now, nonce=nonce)


def sign_restart(config, path, raw, state, key, *, now, nonce):
    """Sign only restart POST bytes; transport identity does not authorize a service change."""
    _restart_request(path, raw)
    return _sign_support_request(config, path, raw, state, key, now=now, nonce=nonce)


def sign_provision(config, path, raw, state, key, *, now, nonce):
    """Sign the two provision POSTs without changing monitoring or support admission."""
    _provision_request(path, raw)
    return _sign_support_request(config, path, raw, state, key, now=now, nonce=nonce)


def sign_diagnostic(config, path, raw, state, key, *, now, nonce):
    """Sign the fixed diagnostic POSTs; transport identity is separate from diagnostic consent."""
    _diagnostic_request(path, raw)
    return _sign_support_request(config, path, raw, state, key, now=now, nonce=nonce)


def _sign_support_request(config, path, raw, state, key, *, now, nonce):
    try:
        if (type(config) is not dict or not all(type(value) is str for value in config.values())
                or type(state) is not dict or type(key) is not str or re.fullmatch(r'[0-9a-f]{64}', key) is None
                or type(now) not in (int, float) or not math.isfinite(now) or not 0 <= now <= 2**53 - 1
                or type(nonce) is not str or re.fullmatch(r'[0-9a-f]{32}', nonce) is None):
            raise ValueError
        if any(type(state.get(name)) is not str or re.fullmatch(_UUID, state[name]) is None
               for name in ('device_id', 'key_id')):
            raise ValueError
        endpoint(config)
        headers = _sign(config, path, raw, state, key, now=now, nonce=nonce)
        if not _support_headers(headers):
            raise ValueError
        return headers
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ValueError('SDC_SIGNING_INVALID') from None


def observation(facts, sequence, event_id, observed_at, dropped):
    # Explicit projection: never copy config, process output, paths or clinical data.
    facts = dict(facts)
    for name in ('mcs_commit', 'loaded_commit'):
        value = facts.get(name)
        facts[name] = value if isinstance(value, str) and re.fullmatch('[0-9a-f]{40}', value) else None
    for name in ('python_version', 'macos_version'):
        value = facts.get(name)
        facts[name] = value if isinstance(value, str) and re.fullmatch(r'[0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?', value) else None
    for name, allowed in {'mode': {'standalone', 'hermes', 'unknown'}, 'mcs_process': {'running', 'stopped', 'unknown'}, 'clock_status': {'ok', 'skewed', 'unknown'}}.items():
        if not isinstance(facts.get(name), str) or facts[name] not in allowed:
            facts[name] = 'unknown'
    return {'schema_version': 'sdc.heartbeat.v1', 'event_id': event_id, 'sequence': sequence, 'observed_at': observed_at, 'connector_version': VERSION,
            'installed': {'mcs_commit': facts.get('mcs_commit'), 'python_version': facts.get('python_version'), 'macos_version': facts.get('macos_version')},
            'runtime': {'mode': facts.get('mode', 'unknown'), 'mcs_process': facts.get('mcs_process', 'unknown'), 'loaded_commit': facts.get('loaded_commit')},
            'clock_status': facts.get('clock_status', 'unknown'), 'dropped_events': dropped}


def sign_release_registration(config, path, raw, state, key, *, now, nonce):
    """Sign only the dedicated registration read; old request families stay closed."""
    from .release_registration_contract import PATH, validate_request
    if path != PATH or type(raw) is not bytes:
        raise ValueError('SDC_REQUEST_INVALID')
    validate_request(raw)
    return _sign_support_request(config, path, raw, state, key, now=now, nonce=nonce)


RELEASE_APPLY_PATHS = {f'/v2/releases/apply/{path}': kind for path, kind in (
    ('plans/report', 'plan_report'), ('cases/preview', 'preview'), ('cases/accept', 'accept'),
    ('requests/poll', 'poll'), ('operations/admit', 'admit'), ('operations/grant', 'grant'),
    ('operations/report', 'report'), ('operations/status', 'status'))}


def sign_release_apply(config, path, raw, state, key, *, now, nonce):
    """Sign only the eight closed device controls, never registration or admin mutations."""
    from .release_apply_contract import validate_apply_control
    try:
        if (type(path) is not str or path not in RELEASE_APPLY_PATHS or type(raw) is not bytes
                or type(now) not in (int, float) or not math.isfinite(now)):
            raise ValueError
        validate_apply_control(RELEASE_APPLY_PATHS[path], raw, now_ms=int(now * 1000))
        return _sign_support_request(config, path, raw, state, key, now=now, nonce=nonce)
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ValueError('SDC_REQUEST_INVALID') from None
