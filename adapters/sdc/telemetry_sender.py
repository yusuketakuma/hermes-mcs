"""Send one bounded telemetry report per interval after separate consent."""
import time
import uuid

from .connector import Connector
from .inventory_sender import endpoint_hash
from .telemetry import MAX_REPORT_BYTES, SECTIONS, build_report, collect_sections
from .transport import TransportError

CONSENT = 'sdc-device-telemetry-v1'
POLICY_SCHEMA = 'sdc.telemetry.policy.v1'
INTERVAL = 3600
PATH = '/v2/device/telemetry'
RESULTS = frozenset({'SDC_TELEMETRY_DISABLED', 'SDC_TELEMETRY_WAITING', 'SDC_TELEMETRY_CLOCK_UNTRUSTED',
                     'SDC_TELEMETRY_RUNTIME_UNKNOWN', 'SDC_TELEMETRY_BACKOFF', 'SDC_TELEMETRY_STORED',
                     'SDC_TELEMETRY_ACCEPTED', 'SDC_TELEMETRY_RETRY_LATER',
                     'SDC_TELEMETRY_REQUEST_DENIED', 'SDC_TELEMETRY_PROTOCOL_ERROR',
                     'SDC_TELEMETRY_UNAVAILABLE'})


def validate_policy(policy):
    fields = {'schema_version', 'enabled', 'consent_revision', 'sections', 'revision', 'approved_at', 'device_id', 'endpoint_hash'}
    if not isinstance(policy, dict) or set(policy) != fields or policy['schema_version'] != POLICY_SCHEMA or type(policy['enabled']) is not bool or policy['consent_revision'] != CONSENT:
        raise ValueError('SDC_TELEMETRY_POLICY_INVALID')
    sections = policy['sections']
    try:
        if not isinstance(sections, list) or not sections or len(set(sections)) != len(sections) or any(name not in SECTIONS for name in sections):
            raise ValueError
        if str(uuid.UUID(policy['revision'])) != policy['revision'] or type(policy['approved_at']) is not int or policy['approved_at'] < 0:
            raise ValueError
        if str(uuid.UUID(policy['device_id'])) != policy['device_id'] or not isinstance(policy['endpoint_hash'], str) or len(policy['endpoint_hash']) != 64 or any(value not in '0123456789abcdef' for value in policy['endpoint_hash']):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError('SDC_TELEMETRY_POLICY_INVALID') from None
    return policy


def initial_status(revision):
    return {'policy_revision': revision, 'device_id': None, 'sequence': 0, 'next_due': 0, 'failures': 0,
            'last_result': 'SDC_TELEMETRY_WAITING', 'stopped': False, 'last_report_id': None, 'sent_at': None}


class TelemetrySender:
    def __init__(self, store, transport, *, clock=time.time, data_dir=None, collector=collect_sections):
        self.store, self.transport, self.clock, self.collector = store, transport, clock, collector
        self.data_dir = data_dir

    def _save(self, status, code, *, stopped=False):
        status.update(last_result=code, stopped=stopped)
        self.store.write('telemetry-status.json', status)
        return code

    def _admitted(self, state):
        return (state.get('state') == 'active' and state.get('enabled') and not state.get('disconnected')
                and not state.get('central_suspended') and not state.get('rotation_id'))

    def tick(self, facts):
        """Caller owns Store.lock; never change heartbeat state or sequence."""
        policy = self.store.read('telemetry-policy.json')
        if policy is None:
            return 'SDC_TELEMETRY_DISABLED'
        validate_policy(policy)
        if not policy['enabled']:
            return 'SDC_TELEMETRY_DISABLED'
        state = self.store.read('state.json', {})
        if not self._admitted(state):
            return 'SDC_TELEMETRY_WAITING'
        now = self.clock()
        if state.get('clock_status') != 'ok' or not 0 <= now - state.get('clock_checked_at', -1e10) <= 900:
            return 'SDC_TELEMETRY_CLOCK_UNTRUSTED'
        if facts.get('mode') not in ('hermes', 'standalone'):
            return 'SDC_TELEMETRY_RUNTIME_UNKNOWN'
        status = self.store.read('telemetry-status.json') or initial_status(policy['revision'])
        if status.get('policy_revision') != policy['revision']:
            status = initial_status(policy['revision'])
        if status.get('stopped'):
            return status['last_result'] if status.get('last_result') in RESULTS else 'SDC_TELEMETRY_UNAVAILABLE'
        if status.get('next_due', 0) > now:
            return 'SDC_TELEMETRY_BACKOFF'
        target_hash = endpoint_hash(self.store.read('endpoint.json'))
        if policy['device_id'] != state.get('device_id') or policy['endpoint_hash'] != target_hash:
            return 'SDC_TELEMETRY_WAITING'
        data_dir = self.data_dir
        if data_dir is None:
            status['next_due'] = now + INTERVAL
            return self._save(status, 'SDC_TELEMETRY_UNAVAILABLE')
        allowed = set(policy['sections'])
        try:
            collected = self.collector(facts, state, data_dir=data_dir, clock=self.clock, only=allowed)
            built = build_report(collected, sequence=status.get('sequence', 0) + 1, clock=self.clock, only=allowed)
        except TimeoutError:
            raise
        except Exception:
            status['next_due'] = now + INTERVAL
            return self._save(status, 'SDC_TELEMETRY_UNAVAILABLE')
        if built is None:
            status['next_due'] = now + INTERVAL
            return self._save(status, 'SDC_TELEMETRY_UNAVAILABLE')
        report, raw = built
        if not set(report['sections']) <= allowed:
            return self._save(status, 'SDC_TELEMETRY_PROTOCOL_ERROR', stopped=True)
        if len(raw) > MAX_REPORT_BYTES:
            return self._save(status, 'SDC_TELEMETRY_PROTOCOL_ERROR', stopped=True)
        current_policy = self.store.read('telemetry-policy.json')
        current_state = self.store.read('state.json', {})
        if current_policy != policy or not self._admitted(current_state) or current_state.get('device_id') != policy['device_id'] or current_state.get('rotation_id'):
            return 'SDC_TELEMETRY_WAITING'
        if current_state.get('clock_status') != 'ok' or not 0 <= self.clock() - current_state.get('clock_checked_at', -1e10) <= 900:
            return 'SDC_TELEMETRY_CLOCK_UNTRUSTED'
        if endpoint_hash(self.store.read('endpoint.json')) != policy['endpoint_hash']:
            return 'SDC_TELEMETRY_WAITING'
        try:
            connector = Connector(self.store, self.transport, clock=self.clock)
            result = connector.request(PATH, raw, current_state, connector.secret(current_state, self.store.read('credential')))
        except TransportError as error:
            if error.status in (429, 503) or error.status >= 500:
                status['failures'] = min(status.get('failures', 0) + 1, 4)
                status['next_due'] = now + max(min(error.retry_after, 86400), (300, 600, 1200, 1800)[status['failures'] - 1])
                return self._save(status, 'SDC_TELEMETRY_RETRY_LATER')
            return self._save(status, 'SDC_TELEMETRY_REQUEST_DENIED', stopped=True)
        try:
            request_id = result.get('request_id') if isinstance(result, dict) else None
            valid_request_id = isinstance(request_id, str) and str(uuid.UUID(request_id)) == request_id and uuid.UUID(request_id).version == 4
        except (ValueError, AttributeError):
            valid_request_id = False
        if not isinstance(result, dict) or set(result) != {'code', 'report_id', 'sections', 'request_id'} or result.get('report_id') != report['report_id'] or result.get('code') not in ('accepted', 'duplicate') or not isinstance(result.get('sections'), list) or set(result['sections']) != set(report['sections']) or not valid_request_id:
            return self._save(status, 'SDC_TELEMETRY_PROTOCOL_ERROR', stopped=True)
        status.update(sequence=report['sequence'], next_due=now + INTERVAL, failures=0,
                      device_id=current_state.get('device_id'), last_report_id=report['report_id'], sent_at=int(now * 1000))
        return self._save(status, 'SDC_TELEMETRY_ACCEPTED')
