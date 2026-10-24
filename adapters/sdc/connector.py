"""Persist immutable sends and isolate one synchronous monitoring tick."""
from datetime import datetime, timezone
import hashlib
import json
import secrets
import time
import uuid

from .protocol import CONSENT, CUSTOMER_CONSENT, VERSION, observation, raw_body, sign
from .transport import TransportError


class Connector:
    def __init__(self, store, transport, *, clock=time.time, admit=None):
        self.store, self.transport, self.clock = store, transport, clock
        self.admit = admit or (lambda: True)
        self.config = store.read('endpoint.json')
        self.consent = CUSTOMER_CONSENT if store.customer else CONSENT

    def secret(self, state, credential):
        if not isinstance(credential, dict):
            raise ValueError('SDC_CREDENTIAL_REQUIRED')
        if state.get('key_id') == credential.get('next_key_id'):
            return credential['next_key']
        if state.get('key_id') != credential.get('key_id'):
            raise ValueError('SDC_CREDENTIAL_IDENTITY_MISMATCH')
        return credential['key']

    def save_credential(self, credential, purpose, state):
        """Save a credential whose identities are already fixed in RAM `state`."""
        if self.store.customer:
            # The Store records the intent and publishes these identities after the native write.
            self.store.write('credential', credential, mutation={'purpose': purpose, 'after': {
                name: state.get(name) for name in ('claim_id', 'device_id', 'key_id', 'rotation_id')}})
        else:
            self.store.write('credential', credential)
            self.store.write('state.json', state)

    def request(self, path, body, state, key):
        raw = body if isinstance(body, bytes) else raw_body(body)
        headers = sign(self.config, path, raw, state, key, now=self.clock())
        self.store.guard_credential()  # Metadata only: no send past an unfinished credential change.
        return self.transport.send(path, raw, headers)

    def connect(self, token=None):
        state = self.store.read('state.json', {})
        credential = self.store.read('credential', {})
        from .provision import guard_registration
        guard_registration(self.store, state, credential)
        if state.get('state') in ('active', 'suspended', 'revoked'):
            if self.store.customer and state.get('state') == 'active':
                self.secret(state, credential)
            return {'state': state['state']}
        if not credential:
            credential = {'key': secrets.token_hex(32)}
            if state.get('claim_id'):
                self.store.write('credential', credential)  # Self-mode resume; a customer root rejects this.
            else:
                state = {'claim_id': str(uuid.uuid4()), 'state': 'pending', 'sequence': 0, 'enabled': False}
                self.save_credential(credential, 'initial-key', state)
        if not state.get('claim_id'):
            state = {'claim_id': str(uuid.uuid4()), 'state': 'pending', 'sequence': 0, 'enabled': False}
            self.store.write('state.json', state)
        if not state.get('device_id'):
            if token is None:
                raise ValueError('SDC_TOKEN_REQUIRED')
            body = {'claim_id': state['claim_id'], 'token': token, 'key': credential['key'], 'connector_version': VERSION}
            if self.store.customer:
                body['credential_storage'] = 'keychain'
            self.store.guard_credential()
            result = self.transport.send('/v2/enrollments/claim', raw_body(body))
            if (result.get('claim_id') != state['claim_id'] or result.get('audience') != self.config['audience']
                    or credential.get('key_id') is not None and result.get('key_id') != credential['key_id']):
                raise ValueError('SDC_IDENTITY_MISMATCH')
            for name in ('device_id', 'key_id'):
                value = result.get(name)
                if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                    raise ValueError('SDC_IDENTITY_MISMATCH')
                state[name] = value
            credential['key_id'] = state['key_id']
            self.save_credential(credential, 'claim-key-id', state)
        result = self.request('/v2/enrollments/status', {'claim_id': state['claim_id']}, state, self.secret(state, credential))
        fingerprint = hashlib.sha256(bytes.fromhex(credential['key'])).hexdigest()[:16]
        if result.get('fingerprint') != fingerprint or result.get('environment') != self.config['environment'] or result.get('consent_revision') != self.consent or result.get('data_scope') != 'version-process-status-only':
            raise ValueError('SDC_IDENTITY_MISMATCH')
        self._enrollment_identity(result, state, self.secret(state, credential))
        return result

    def confirm(self):
        state = self.store.read('state.json')
        key = self.secret(state, self.store.read('credential'))
        receipt = self.store.read('enrollment-consent.json', {})
        if receipt:
            receipt = self._consent_receipt(state, key, allow_missing_expiry=True)
            if receipt['pending_expires_at'] is not None and int(self.clock() * 1000) >= receipt['pending_expires_at']:
                raise ValueError('SDC_ENROLLMENT_EXPIRED')
        result = self.request('/v2/enrollments/confirm', {'claim_id': state['claim_id'], 'consent_revision': self.consent}, state, key)
        if result.get('state') != 'active' or result.get('device_id') != state['device_id'] or self.store.customer and (result.get('consent_revision') != self.consent or result.get('data_scope') != 'version-process-status-only'):
            raise ValueError('SDC_CONFIRM_UNCERTAIN')
        if receipt and self._enrollment_identity(result, state, key) != receipt.get('identity'):
            raise ValueError('SDC_CONFIRM_UNCERTAIN')
        state.update(state='active', enabled=True)
        self.store.write('state.json', state)

    def _enrollment_identity(self, result, state, key):
        expected = {name: state[name] for name in ('claim_id', 'device_id', 'key_id')}
        expected.update(audience=self.config['audience'], environment=self.config['environment'],
                        fingerprint=hashlib.sha256(bytes.fromhex(key)).hexdigest()[:16],
                        consent_revision=self.consent, data_scope='version-process-status-only')
        if any(result.get(name) != value for name, value in expected.items()):
            raise ValueError('SDC_IDENTITY_MISMATCH')
        for name in ('tenant_name', 'alias'):
            value = result.get(name)
            if not isinstance(value, str) or not value or len(value) > 512 or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError('SDC_IDENTITY_MISMATCH')
            expected[name] = value
        if result.get('state') not in ('claimed', 'admin_approved', 'active'):
            raise ValueError('SDC_ENROLLMENT_HELD')
        return expected

    def enrollment_status(self):
        """Authenticate the original enrollment without the active-state shortcut."""
        state = self.store.read('state.json')
        if state.get('disconnected') or state.get('rotation_id') or state.get('state') not in ('pending', 'active'):
            raise ValueError('SDC_ENROLLMENT_HELD')
        key = self.secret(state, self.store.read('credential'))
        result = self.request('/v2/enrollments/status', {'claim_id': state['claim_id']}, state, key)
        self._enrollment_identity(result, state, key)
        return result

    def save_enrollment_consent(self, result, *, before_save=None):
        """Persist explicit consent before enabling polling or confirming enrollment."""
        state = self.store.read('state.json')
        identity = self._enrollment_identity(result, state, self.secret(state, self.store.read('credential')))
        expires = result.get('pending_expires_at')
        if expires is not None and (type(expires) is not int or not int(self.clock() * 1000) < expires <= 2**53 - 1):
            raise ValueError('SDC_ENROLLMENT_EXPIRED')
        receipt = {'schema_version': 'sdc.enrollment-consent.v1', 'accepted': True, **self.store.enrollment_binding(),
                   'key_sha256': hashlib.sha256(bytes.fromhex(self.secret(state, self.store.read('credential')))).hexdigest(),
                   'identity': identity, 'pending_expires_at': expires, 'accepted_at': int(self.clock() * 1000)}
        if before_save is not None:
            before_save(receipt)
        self.store.write('enrollment-consent.json', receipt)
        # Read back before any confirm or service installation; a failed write is never consent.
        if self.store.read('enrollment-consent.json') != receipt:
            raise ValueError('SDC_CONSENT_UNSAVED')
        return expires is not None

    def _consent_receipt(self, state, key, *, allow_missing_expiry=False):
        receipt = self.store.read('enrollment-consent.json', {})
        fields = {'schema_version', 'accepted', 'root_fingerprint', 'endpoint_hash', 'key_sha256', 'identity', 'pending_expires_at', 'accepted_at'}
        accepted, expires, now = receipt.get('accepted_at'), receipt.get('pending_expires_at'), int(self.clock() * 1000)
        if set(receipt) != fields or receipt.get('schema_version') != 'sdc.enrollment-consent.v1' or receipt.get('accepted') is not True:
            raise ValueError('SDC_CONSENT_REQUIRED')
        if type(accepted) is not int or not 0 <= accepted <= now <= 2**53 - 1:
            raise ValueError('SDC_CONSENT_REQUIRED')
        if expires is None:
            if not allow_missing_expiry:
                raise ValueError('SDC_CONSENT_REQUIRED')
        elif type(expires) is not int or not accepted < expires <= 2**53 - 1:
            raise ValueError('SDC_CONSENT_REQUIRED')
        if any(receipt.get(name) != value for name, value in self.store.enrollment_binding().items()):
            raise ValueError('SDC_CONSENT_REQUIRED')
        if receipt.get('key_sha256') != hashlib.sha256(bytes.fromhex(key)).hexdigest():
            raise ValueError('SDC_CONSENT_REQUIRED')
        identity = receipt.get('identity')
        if not isinstance(identity, dict) or self._enrollment_identity({**identity, 'state': 'claimed'}, state, key) != identity:
            raise ValueError('SDC_CONSENT_REQUIRED')
        return receipt

    @staticmethod
    def _receipt_hash(receipt):
        return hashlib.sha256(json.dumps(receipt, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()

    def poll_enrollment(self):
        """Resolve only a consented pending registration; never emit observations here."""
        state = self.store.read('state.json', {})
        if self.admit() is not True:
            return 'SDC_ENROLLMENT_DISABLED'
        if state.get('state') != 'pending' or state.get('enabled') or state.get('disconnected') or state.get('rotation_id') or state.get('enrollment_hold'):
            return 'SDC_ENROLLMENT_DISABLED'
        if any(not state.get(name) for name in ('claim_id', 'device_id', 'key_id')):
            return 'SDC_ENROLLMENT_CONSENT_REQUIRED'
        key = self.secret(state, self.store.read('credential'))
        try:
            receipt = self._consent_receipt(state, key)
        except (ValueError, KeyError, TypeError):
            return 'SDC_ENROLLMENT_CONSENT_REQUIRED'
        expires, now = receipt['pending_expires_at'], int(self.clock() * 1000)
        attempt = state.get('enrollment_confirmation')
        existing_attempt = (isinstance(attempt, dict) and set(attempt) == {'receipt_hash', 'started_at'}
                            and attempt.get('receipt_hash') == self._receipt_hash(receipt)
                            and type(attempt.get('started_at')) is int
                            and receipt['accepted_at'] <= attempt['started_at'] < expires
                            and attempt['started_at'] <= now)
        if now >= expires and not existing_attempt:
            return 'SDC_ENROLLMENT_EXPIRED'
        due, failures = state.get('enrollment_next_due', 0), state.get('enrollment_failures', 0)
        if type(due) not in (int, float) or not 0 <= due <= 2**53 - 1 or type(failures) is not int or not 0 <= failures <= 4:
            return 'SDC_ENROLLMENT_CONSENT_REQUIRED'
        if self.clock() < due:
            return 'SDC_ENROLLMENT_NOT_DUE'
        try:
            result = self.enrollment_status()
            if self._enrollment_identity(result, state, key) != receipt.get('identity') or result.get('pending_expires_at') != expires:
                raise ValueError('SDC_IDENTITY_MISMATCH')
            state.update(enrollment_failures=0, enrollment_next_due=self.clock() + 300)
            self.store.write('state.json', state)
            if result['state'] == 'claimed':
                return 'SDC_ENROLLMENT_PENDING'
            if result['state'] == 'admin_approved':
                if int(self.clock() * 1000) >= expires:
                    return 'SDC_ENROLLMENT_EXPIRED'
                if not existing_attempt:
                    state['enrollment_confirmation'] = {'receipt_hash': self._receipt_hash(receipt), 'started_at': int(self.clock() * 1000)}
                    self.store.write('state.json', state)
                if self.admit() is not True or int(self.clock() * 1000) >= expires:
                    return 'SDC_ENROLLMENT_DISABLED'
                result = self.request('/v2/enrollments/confirm', {'claim_id': state['claim_id'], 'consent_revision': self.consent}, state, key)
                if self._enrollment_identity(result, state, key) != receipt['identity'] or result.get('state') != 'active' or result.get('pending_expires_at') != expires:
                    raise ValueError('SDC_CONFIRM_UNCERTAIN')
            elif not existing_attempt:
                raise ValueError('SDC_CONFIRM_UNCERTAIN')
            if self.admit() is not True:
                return 'SDC_ENROLLMENT_DISABLED'
            state.update(state='active', enabled=True)
            self.store.write('state.json', state)
            return 'SDC_ENROLLMENT_ACTIVE'
        except TransportError as error:
            if error.status in (429, 503) or error.status >= 500:
                state['enrollment_failures'] = min(state.get('enrollment_failures', 0) + 1, 4)
                delay = (300, 600, 1200, 1800)[state['enrollment_failures'] - 1]
                state['enrollment_next_due'] = self.clock() + max(error.retry_after, delay * (0.8 + secrets.randbelow(401) / 1000))
                state['enrollment_last_result'] = 'SDC_ENROLLMENT_RETRY_LATER'
                self.store.write('state.json', state)
            else:
                state['enrollment_hold'] = 'SDC_ENROLLMENT_DENIED'
                self.store.write('state.json', state)
            raise
        except (ValueError, KeyError, TypeError):
            state['enrollment_hold'] = 'SDC_ENROLLMENT_IDENTITY_UNCERTAIN'
            self.store.write('state.json', state)
            raise

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
            purpose = 'rotation-replacement' if state.get('rotation_id') else 'rotation-candidate'
            state['rotation_id'] = str(uuid.uuid4())
            self.save_credential(credential, purpose, state)
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
            state['rotation_id'] = str(uuid.uuid4())
            self.save_credential(credential, 'rotation-replacement', state)
            return False
        next_id = status.get('key_id')
        if not isinstance(next_id, str) or str(uuid.UUID(next_id)) != next_id:
            raise ValueError('SDC_ROTATION_UNCERTAIN')
        credential['next_key_id'] = next_id
        self.save_credential(credential, 'rotation-key-id', state)
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
        state.pop('rotation_id', None)
        self.save_credential(credential, 'rotation-finalize', state)

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
        credential = self.store.read('credential') if not self.store.customer else None
        state['enabled'] = False
        state['state'] = 'suspended'
        state['disconnected'] = True
        self.store.write('state.json', state)
        try:
            result = self.request('/v2/device/suspend', {}, state, self.secret(state, credential if credential is not None else self.store.read('credential')))
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
        credential = self.store.read('credential')
        self.secret(state, credential)
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
            result = self.request('/v2/heartbeats', state['sending'].encode(), state, self.secret(state, credential))
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
