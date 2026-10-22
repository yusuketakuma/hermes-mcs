"""Persist immutable sends and isolate one synchronous monitoring tick."""
from datetime import datetime, timezone
import hashlib
import json
import secrets
import time
import uuid

from .protocol import CONSENT, VERSION, observation, raw_body, sign
from .transport import TransportError


class Connector:
    def __init__(self, store, transport, *, clock=time.time):
        self.store, self.transport, self.clock = store, transport, clock
        self.config = store.read('endpoint.json')

    def secret(self, state, credential):
        if state.get('key_id') == credential.get('next_key_id'):
            return credential['next_key']
        if state.get('key_id') != credential.get('key_id'):
            raise ValueError('SDC_CREDENTIAL_IDENTITY_MISMATCH')
        return credential['key']

    def request(self, path, body, state, key):
        raw = body if isinstance(body, bytes) else raw_body(body)
        return self.transport.send(path, raw, sign(self.config, path, raw, state, key, now=self.clock()))

    def connect(self, token=None):
        state = self.store.read('state.json', {})
        credential = self.store.read('credential', {})
        if state.get('state') in ('active', 'suspended', 'revoked'):
            return {'state': state['state']}
        if not credential:
            credential = {'key': secrets.token_hex(32)}
            self.store.write('credential', credential)
        if not state.get('claim_id'):
            state = {'claim_id': str(uuid.uuid4()), 'state': 'pending', 'sequence': 0, 'enabled': False}
            self.store.write('state.json', state)
        if not state.get('device_id'):
            if token is None:
                raise ValueError('SDC_TOKEN_REQUIRED')
            result = self.transport.send('/v2/enrollments/claim', raw_body({'claim_id': state['claim_id'], 'token': token, 'key': credential['key'], 'connector_version': VERSION}))
            if result.get('claim_id') != state['claim_id'] or result.get('audience') != self.config['audience']:
                raise ValueError('SDC_IDENTITY_MISMATCH')
            for name in ('device_id', 'key_id'):
                value = result.get(name)
                if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                    raise ValueError('SDC_IDENTITY_MISMATCH')
                state[name] = value
            credential['key_id'] = state['key_id']
            self.store.write('credential', credential)
            self.store.write('state.json', state)
        result = self.request('/v2/enrollments/status', {'claim_id': state['claim_id']}, state, self.secret(state, credential))
        fingerprint = hashlib.sha256(bytes.fromhex(credential['key'])).hexdigest()[:16]
        if result.get('fingerprint') != fingerprint or result.get('environment') != self.config['environment'] or result.get('consent_revision') != CONSENT or result.get('data_scope') != 'version-process-status-only':
            raise ValueError('SDC_IDENTITY_MISMATCH')
        return result

    def confirm(self):
        state = self.store.read('state.json')
        key = self.secret(state, self.store.read('credential'))
        result = self.request('/v2/enrollments/confirm', {'claim_id': state['claim_id'], 'consent_revision': CONSENT}, state, key)
        if result.get('state') != 'active' or result.get('device_id') != state['device_id']:
            raise ValueError('SDC_CONFIRM_UNCERTAIN')
        state.update(state='active', enabled=True)
        self.store.write('state.json', state)

    def rotate(self):
        state = self.store.read('state.json')
        credential = self.store.read('credential')
        if state.get('disconnected') or state.get('state') != 'active' and not (state.get('state') == 'suspended' and state.get('rotation_id') and credential.get('next_key_id')):
            raise ValueError('SDC_NOT_ACTIVE')
        if state.get('rotation_id') and credential.get('completed_rotation_id') == state['rotation_id']:
            state.pop('rotation_id')
            self.store.write('state.json', state)
            return True
        if credential.get('next_key_id') == state.get('key_id'):
            self._finish_rotation(state, credential)
            return True
        if 'next_key' not in credential:
            credential['next_key'] = secrets.token_hex(32)
            self.store.write('credential', credential)
        if not state.get('rotation_id'):
            state['rotation_id'] = str(uuid.uuid4())
            self.store.write('state.json', state)
        body = {'rotation_id': state['rotation_id']}
        try:
            status = self.request('/v2/keys/status', body, state, self.secret(state, credential))
        except TransportError as error:
            if error.status not in (401, 403) or not credential.get('next_key_id'):
                raise
            candidate = {**state, 'key_id': credential['next_key_id']}
            status = self.request('/v2/keys/status', body, candidate, credential['next_key'])
        if status.get('state') == 'missing':
            status = self.request('/v2/keys/prepare', {**body, 'key': credential['next_key']}, state, self.secret(state, credential))
        if status.get('state') not in ('prepared', 'active', 'revoked'):
            raise ValueError('SDC_ROTATION_UNCERTAIN')
        if status.get('state') == 'revoked':
            credential['next_key'] = secrets.token_hex(32)
            credential.pop('next_key_id', None)
            self.store.write('credential', credential)
            state['rotation_id'] = str(uuid.uuid4())
            self.store.write('state.json', state)
            return False
        next_id = status.get('key_id')
        if not isinstance(next_id, str) or str(uuid.UUID(next_id)) != next_id:
            raise ValueError('SDC_ROTATION_UNCERTAIN')
        credential['next_key_id'] = next_id
        self.store.write('credential', credential)
        next_state = {**state, 'key_id': next_id}
        result = self.request('/v2/keys/commit', body, next_state, credential['next_key'])
        if result.get('state') != 'active' or result.get('key_id') != next_id:
            raise ValueError('SDC_ROTATION_UNCERTAIN')
        # Keep both local keys until the active key identity is durably published.
        state['key_id'] = next_id
        state['state'] = 'active'
        self.store.write('state.json', state)
        self._finish_rotation(state, credential)
        return True

    def _finish_rotation(self, state, credential):
        credential.update(key=credential['next_key'], key_id=state['key_id'], completed_rotation_id=state['rotation_id'])
        credential.pop('next_key', None)
        credential.pop('next_key_id', None)
        self.store.write('credential', credential)
        state.pop('rotation_id', None)
        self.store.write('state.json', state)

    def resume(self):
        state = self.store.read('state.json')
        if state.get('disconnected') or state.get('state') != 'active' or state.get('rotation_id'):
            raise ValueError('SDC_REENROLL_OR_ROTATION_REQUIRED')
        credential = self.store.read('credential')
        self.request('/v2/keys/status', {'rotation_id': str(uuid.uuid4())}, state, self.secret(state, credential))
        state.update(enabled=True, next_due=0, last_result='SDC_EXPLICIT_RESUME')
        self.store.write('state.json', state)

    def disconnect(self):
        state = self.store.read('state.json', {})
        state['enabled'] = False
        state['state'] = 'suspended'
        state['disconnected'] = True
        self.store.write('state.json', state)
        try:
            result = self.request('/v2/device/suspend', {}, state, self.secret(state, self.store.read('credential')))
            confirmed = result.get('code') == 'suspended'
        except (TransportError, ValueError, KeyError):
            confirmed = False
        state['central_suspended'] = confirmed
        self.store.write('state.json', state)
        return confirmed

    def _transition(self, state, code):
        now = int(self.clock())
        records = state.get('transitions', [])
        if state.get('last_result') != code:
            records.append({'at': now, 'code': code})
        state['last_result'] = code
        retained = [row for row in records if 0 <= now - row['at'] <= 7 * 86400][-1000:]
        state['dropped_events'] = state.get('dropped_events', 0) + len(records) - len(retained)
        state['transitions'] = retained
        while len(json.dumps(state).encode()) > 8 * 1024 * 1024 and retained:
            retained.pop(0)
            state['dropped_events'] += 1

    def tick(self, facts):
        state = self.store.read('state.json', {})
        if state.get('disconnected') or not state.get('enabled') or state.get('state') != 'active':
            return 'SDC_DISABLED'
        now = self.clock()
        known_clock = state.get('clock_status', 'unknown') if 0 <= now - state.get('clock_checked_at', -1e10) <= 900 else 'unknown'
        latest = observation({**facts, 'clock_status': known_clock}, 0, str(uuid.uuid4()), datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), state.get('dropped_events', 0))
        state['latest'] = latest
        if state.get('next_due', 0) > now:
            self.store.write('state.json', state)
            return 'SDC_BACKOFF'
        if not state.get('sending'):
            sequence = state.get('sequence', 0)
            if type(sequence) is not int or not 0 <= sequence < 2**53 - 1:
                raise ValueError('SDC_REENROLL_REQUIRED')
            state['sequence'] = sequence + 1
            latest['sequence'] = sequence + 1
            state['sending'] = raw_body(latest).decode()
            state['latest'] = None
        self.store.write('state.json', state)
        try:
            result = self.request('/v2/heartbeats', state['sending'].encode(), state, self.secret(state, self.store.read('credential')))
            if result.get('code') not in ('accepted', 'duplicate'):
                raise TransportError()
            try:
                if not isinstance(result.get('server_time'), str) or not result['server_time'].endswith('Z'):
                    raise ValueError
                server_time = datetime.fromisoformat(result['server_time'].replace('Z', '+00:00')).timestamp()
            except (KeyError, TypeError, ValueError, AttributeError):
                raise TransportError() from None
            state['clock_status'] = 'ok' if abs(server_time - self.clock()) <= 300 else 'skewed'
            state['clock_checked_at'] = self.clock()
            state.update(sending=None, failures=0, next_due=now + 300)
            self._transition(state, 'SDC_ACCEPTED')
        except TransportError as error:
            if error.status in (429, 503) or error.status >= 500:
                state['failures'] = min(state.get('failures', 0) + 1, 4)
                delay = (300, 600, 1200, 1800)[state['failures'] - 1]
                state['next_due'] = now + max(error.retry_after, delay * (0.8 + secrets.randbelow(401) / 1000))
                self._transition(state, 'SDC_RETRY_LATER')
            else:
                state['enabled'] = False
                if error.status == 403 or error.code in ('SEQUENCE_CONFLICT', 'BODY_CONFLICT'):
                    state['state'] = 'suspended'
                state['central_suspended'] = False
                self._transition(state, 'SDC_TIME_SKEW' if state.get('clock_status') == 'skewed' else 'SDC_REQUEST_DENIED')
        self.store.write('state.json', state)
        return state['last_result']
