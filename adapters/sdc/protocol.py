"""Build fixed DTOs and authenticate exact raw request bytes."""
import hashlib
import hmac
import json
import math
import re
import secrets
import time

PATHS = frozenset({'/v2/enrollments/claim', '/v2/enrollments/status', '/v2/enrollments/confirm', '/v2/heartbeats', '/v2/device/suspend', '/v2/keys/prepare', '/v2/keys/commit', '/v2/keys/status'})
CONSENT = 'sdc-self-monitoring-v1'
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
    stamp = str(int(time.time() if now is None else now))
    nonce = nonce or secrets.token_hex(16)
    canonical = '\n'.join(['SDC-HMAC-v1', config['audience'], 'POST', path, state['device_id'], state['key_id'], stamp, nonce, hashlib.sha256(raw).hexdigest()])
    return {'X-SDC-Device': state['device_id'], 'X-SDC-Key-ID': state['key_id'], 'X-SDC-Time': stamp, 'X-SDC-Nonce': nonce, 'X-SDC-Signature': hmac.new(bytes.fromhex(key), canonical.encode(), hashlib.sha256).hexdigest()}


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
