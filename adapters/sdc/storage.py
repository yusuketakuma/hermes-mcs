"""Keep Connector credentials and bounded state outside the repository."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import threading
import time
import uuid

from .keychain import KeychainCredentialStore, KeychainError
from . import credential_mutation as mutation_contract

NAMES = frozenset({'endpoint.json', 'credential', 'credential-store.json', 'state.json', 'tick.lock', 'inventory-policy.json', 'inventory-spool.json', 'inventory-status.json', 'enrollment-consent.json', 'support-state.json', 'support-state.lock', 'support-session.lock', 'support-bootstrap.json', 'restart-state.json', 'restart-state.lock', 'restart-execution.json', 'restart-planning.json', 'provision-state.json', 'diagnostic-state.json', 'diagnostic-anchor.json', 'diagnostic-state.lock'})
MAX_FILE = 8 * 1024 * 1024
# Five <=8192-byte records, one JSON-escaped <=16384-byte request, fixed wrapper <1024.
RESTART_STATE_MAX_BYTES = 5 * 8192 + 2 * 16384 + 1024
# Four escaped control requests, operation/approval/binding/ACK, fixed evidence <8192.
RESTART_EXECUTION_MAX_BYTES = 4 * 2 * 16384 + 4 * 8192 + 8192
# Three escaped control requests, nine bounded DTOs/bindings, fixed wrapper <8192.
RESTART_PLANNING_MAX_BYTES = 3 * 2 * 16384 + 9 * 8192 + 8192
_MISSING = object()
LIMITS = {'state.json': MAX_FILE, 'credential': 4096, 'endpoint.json': 4096, 'tick.lock': 0,
          'credential-store.json': 1024, 'inventory-policy.json': 1024 * 1024, 'inventory-spool.json': 2 * 1024 * 1024, 'inventory-status.json': 16384, 'enrollment-consent.json': 16384, 'support-state.json': 65536, 'support-state.lock': 0, 'support-session.lock': 0, 'support-bootstrap.json': 16384, 'restart-state.json': RESTART_STATE_MAX_BYTES, 'restart-state.lock': 0, 'restart-execution.json': RESTART_EXECUTION_MAX_BYTES, 'restart-planning.json': RESTART_PLANNING_MAX_BYTES}
LIMITS['provision-state.json'] = 1024 * 1024  # File-read ceiling; the provision record enforces a tighter encoded bound.
LIMITS.update({'diagnostic-state.json': 1024 * 1024, 'diagnostic-anchor.json': 4096,
               'diagnostic-state.lock': 0})
LIMITS['credential-store.json'] = mutation_contract.MAX_MARKER
NAMES = NAMES | {'release-owner.json', 'release-owner.lock', 'release-owner-activation.json', 'release-entry.lock', 'release-bootstrap.json', 'release-authority.lock', 'release-planning.json', 'release-plan.json', 'release-change.json', 'release-successor.json', 'release-start-intent.json', 'release-migration-v3.json'}
LIMITS.update({'release-successor.json': 2 * 1024 * 1024 + 65536, 'release-change.json': 131072, 'release-plan.json': 65536, 'release-planning.json': 16384, 'release-owner.json': MAX_FILE, 'release-owner.lock': 0,
               'release-owner-activation.json': 16384, 'release-entry.lock': 0, 'release-bootstrap.json': 2 * 16384 + 4096, 'release-authority.lock': 0,
               'release-start-intent.json': 32 * 1024, 'release-migration-v3.json': 65536})
_IDS = ('claim_id', 'device_id', 'key_id', 'rotation_id')
_STOP = ('state', 'enabled', 'disconnected', 'central_suspended', 'enrollment_hold')
_OPERATIONS = {'connect': {'initial-key', 'claim-key-id', 'reenroll-key'},
               'rotate-key': {'rotation-candidate', 'rotation-replacement', 'rotation-key-id', 'rotation-finalize'},
               'disconnect': {'delete'}}


def _human(confirm_human, reason, now_ms):
    if (confirm_human is not True or type(reason) is not str or not reason.strip()
            or re.fullmatch('[ -~]{1,128}', reason) is None
            or type(now_ms) is not int or not 0 <= now_ms <= mutation_contract.MAX_INTEGER):
        raise ValueError('SDC_CREDENTIAL_APPROVAL_INVALID')


class Store:
    def __init__(self, root=None, *, create=False, create_new=False, keychain_factory=None,
                 credential_reader=None, credential_deadline=None):
        if type(create_new) is not bool:
            raise ValueError('SDC_PRIVATE_ROOT_REQUIRED')
        create = create or create_new
        self.created_new = False
        self.root = Path(root or Path.home() / '.mcs-security').absolute()
        self._keychain_factory = keychain_factory or KeychainCredentialStore
        self._injected_keychain = keychain_factory is not None
        self._credential_reader = credential_reader
        if credential_deadline is not None and (type(credential_deadline) not in (int, float)
                                                or not math.isfinite(credential_deadline)):
            raise ValueError('SDC_CREDENTIAL_DEADLINE_INVALID')
        alarm, _ = signal.getitimer(signal.ITIMER_REAL)
        alarm_deadline = time.monotonic() + alarm if alarm > 0 else None
        self._credential_deadline = (min(credential_deadline, alarm_deadline)
                                     if credential_deadline is not None and alarm_deadline is not None
                                     else credential_deadline if alarm_deadline is None else alarm_deadline)
        self._closed = False
        self._keychain_store = None
        self._keychain_account = None
        self._tick_fd = None
        self._tick_thread = None
        self._mutation_approval = None
        self._mutation_deadline = None
        self._credential_uncertain = False
        self._release_entry_fd = None
        self._release_entry_thread = None
        self._release_entry_shared = None
        self._release_authority_fd = self._release_authority_thread = None
        repo = Path(__file__).resolve().parents[2]
        if self.root.is_relative_to(repo) or '..' in self.root.parts:
            raise ValueError('SDC_PRIVATE_ROOT_REQUIRED')
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for index, part in enumerate(self.root.parts[1:]):
                if create and index == len(self.root.parts) - 2:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                        os.fsync(fd)
                        self.created_new = create_new
                    except FileExistsError:
                        if create_new:
                            raise
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            st = os.fstat(fd)
            if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
                raise ValueError('SDC_PRIVATE_ROOT_REQUIRED')
            self.fd = fd
        except BaseException:
            os.close(fd)
            raise

    def close(self):
        if self._closed:
            return
        try:
            if self._credential_reader is not None:
                self._credential_reader.close()
                if not self._credential_reader.closed:
                    raise KeychainError('SDC_KEYCHAIN_UNAVAILABLE')
        finally:
            # A failed native owner retains the borrowed root until its real cleanup is proved.
            if self._credential_reader is None or self._credential_reader.closed:
                os.close(self.fd)
                self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, _kind, error, _traceback):
        if error is None:
            self.close()
        else:
            try:
                self.close()
            except BaseException:
                # Preserve the caller's failure; close retains any unretired owner.
                pass

    def _name(self, name):
        if name not in NAMES:
            raise ValueError('SDC_INVALID_FILE')

    def _open(self, name, flags=os.O_RDONLY):
        self._name(name)
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self.fd)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600 or st.st_size > LIMITS[name] or st.st_nlink != 1:
                raise ValueError('SDC_PRIVATE_FILE_REQUIRED')
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _read_file(self, name, default=None):
        try:
            fd = self._open(name)
        except FileNotFoundError:
            return default
        with os.fdopen(fd, 'rb') as stream:
            raw = stream.read(LIMITS[name] + 1)
        if len(raw) > LIMITS[name]:
            raise ValueError('SDC_FILE_TOO_LARGE')
        from .protocol import strict_json
        value = strict_json(raw)
        if not isinstance(value, dict):
            raise ValueError('SDC_FILE_INVALID')
        return value

    def _marker(self):
        marker = self._read_file('credential-store.json', _MISSING)
        if marker is _MISSING:
            state = self._read_file('state.json', {})
            if self._keychain_account or isinstance(state, dict) and state.get('credential_storage') == 'keychain':
                raise ValueError('SDC_CREDENTIAL_STORE_REQUIRED')
            return None
        marker = mutation_contract.validate_marker(marker)
        try:
            account = uuid.UUID(marker['account']) if isinstance(marker['account'], str) else None
        except ValueError:
            account = None
        if account is None or account.version != 4 or str(account) != marker['account'] or self._keychain_account and self._keychain_account != marker['account']:
            raise ValueError('SDC_CREDENTIAL_STORE_INVALID')
        if any(not isinstance(marker[name], str) or not re.fullmatch('[0-9a-f]{64}', marker[name]) for name in ('root_fingerprint', 'endpoint_hash')):
            raise ValueError('SDC_CREDENTIAL_STORE_INVALID')
        try:
            bound = marker['root_fingerprint'] == self.root_fingerprint() and marker['endpoint_hash'] == self._endpoint_hash()
        except (OSError, ValueError, TypeError):
            raise ValueError('SDC_CREDENTIAL_STORE_INVALID') from None
        if not bound:
            raise ValueError('SDC_CREDENTIAL_STORE_INVALID')
        if marker['schema_version'] == mutation_contract.SCHEMA:
            fd = self._open('tick.lock')
            try:
                if mutation_contract.tick_lock_fingerprint(fd) != marker['tick_lock_fingerprint']:
                    raise ValueError('SDC_CREDENTIAL_STORE_INVALID')
            finally:
                os.close(fd)
        self._keychain_account = marker['account']
        return marker

    @staticmethod
    def _fingerprint(value):
        raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
        return hashlib.sha256(raw).hexdigest()

    def root_fingerprint(self):
        """Validate the current root path against its borrowed directory descriptor."""
        st = os.fstat(self.fd)
        current = os.stat(self.root, follow_symlinks=False)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700 or (current.st_dev, current.st_ino) != (st.st_dev, st.st_ino):
            raise ValueError('SDC_PRIVATE_ROOT_REQUIRED')
        return self._fingerprint({'path': str(self.root), 'uid': st.st_uid, 'device': st.st_dev, 'inode': st.st_ino})

    def _endpoint_hash(self, value=_MISSING):
        from .protocol import endpoint
        return self._fingerprint(endpoint(self.monitor_endpoint() if value is _MISSING else value))

    def _managed_or_legacy(self, name, value):
        if value is None:
            return self._read_file(name)
        try:
            fd = self._open(name)
        except FileNotFoundError:
            return value
        os.close(fd)
        raise ValueError('SDC_PROVISION_CONFLICT')

    def monitor_endpoint(self):
        """Read the one managed endpoint projection, or the unchanged legacy file."""
        from .provision import monitor_endpoint
        return self._managed_or_legacy('endpoint.json', monitor_endpoint(self))

    def enrollment_binding(self):
        """Bind local consent to this private root and its validated endpoint."""
        return {'root_fingerprint': self.root_fingerprint(), 'endpoint_hash': self._endpoint_hash()}

    @property
    def customer(self):
        return self._marker() is not None

    def enable_customer(self):
        """Select Keychain only in a new private root; never migrate self credentials."""
        if self._tick_fd is None:
            if self._marker() is None:
                # Refuse before creating the lock inode a v3 marker would bind.
                self._require_new_customer_root()
                self._endpoint_hash()
            with self.lock():
                return self.enable_customer()
        if self._marker() is not None:
            return
        self._require_new_customer_root()
        endpoint_hash = self._endpoint_hash()
        mutation_contract.write_marker(self.fd, {'schema_version': mutation_contract.SCHEMA,
            'mode': 'keychain', 'account': str(uuid.uuid4()), 'root_fingerprint': self.root_fingerprint(),
            'endpoint_hash': endpoint_hash, 'tick_lock_fingerprint': mutation_contract.tick_lock_fingerprint(self._tick_fd),
            'mutation': None}, expected_sha256=None)
        self._marker()

    def _require_new_customer_root(self):
        try:
            os.close(self._open('credential'))
        except FileNotFoundError:
            pass
        else:
            raise ValueError('SDC_NEW_CUSTOMER_ROOT_REQUIRED')
        if self._read_file('state.json', {}):
            raise ValueError('SDC_NEW_CUSTOMER_ROOT_REQUIRED')

    def _keychain(self):
        if self._credential_reader is None and not self._injected_keychain:
            from .keychain_owner import KeychainOwner
            self._credential_reader = KeychainOwner(deadline=self._credential_deadline)
        if self._credential_reader is not None:
            return self._credential_reader.for_account(self._keychain_account)
        if self._keychain_store is None:
            self._keychain_store = self._keychain_factory(self._keychain_account)
        return self._keychain_store

    def read(self, name, default=None):
        self._name(name)
        if name == 'endpoint.json':
            value = self.monitor_endpoint()
            return default if value is None else value
        if name == 'support-bootstrap.json':
            from .provision import support_bootstrap
            value = self._managed_or_legacy(name, support_bootstrap(self))
            return default if value is None else value
        if name in ('support-state.json', 'restart-state.json', 'restart-execution.json', 'restart-planning.json', 'provision-state.json', 'diagnostic-state.json', 'diagnostic-anchor.json', 'release-owner.json', 'release-owner-activation.json', 'release-bootstrap.json', 'release-start-intent.json', 'release-migration-v3.json'):
            # Local consent/disable has its own root binding and needs no monitor endpoint.
            return self._read_file(name, default)
        marker = self._marker()
        if name == 'credential-store.json':
            return marker if marker is not None else default
        if name == 'credential' and marker is not None:
            self.guard_credential()
            value = self._actual_credential(deadline=self._approved_scope_deadline())
            if self._marker() != marker:
                raise ValueError('SDC_CREDENTIAL_STORE_INVALID')
            self._observed_credential(marker, value)
            return default if value is None else value
        return self._read_file(name, default)

    def _actual_credential(self, *, deadline=None):
        try:
            # Mutation scopes pass their absolute bound; ordinary reads keep the reader's own.
            raw = self._keychain().read() if deadline is None else self._keychain().read(deadline=deadline)
        except KeychainError as error:
            if error.code != 'SDC_KEYCHAIN_NOT_FOUND':
                raise
            return None
        return mutation_contract.credential_value(raw)

    def guard_credential(self):
        """Hold every credential use while this Store is poisoned or a mutation is unfinished."""
        if self._credential_uncertain:
            raise ValueError('SDC_CREDENTIAL_UNCERTAIN')
        marker = self._marker()
        operation = marker.get('mutation') if marker is not None else None
        if operation is not None and operation['phase'] != 'complete':
            raise ValueError('SDC_CREDENTIAL_MUTATION_UNRESOLVED')

    @staticmethod
    def _observed_credential(marker, value):
        # A read never repairs: the actual value must be the last completed intent's whole credential.
        operation = marker.get('mutation')
        if operation is not None and operation['phase'] == 'complete' and operation['new_sha256'] != (
                None if value is None else mutation_contract.credential_digest(value)):
            raise ValueError('SDC_CREDENTIAL_MUTATION_MISMATCH')

    def authorize_credential_mutation(self, *, operation, confirm_human, reason, now_ms):
        """Keep one human approval in RAM; each mutation re-binds it under the tick lock."""
        _human(confirm_human, reason, now_ms)
        if type(operation) is not str or operation not in _OPERATIONS:
            raise ValueError('SDC_CREDENTIAL_APPROVAL_INVALID')
        state = self._read_file('state.json', {})
        # The identity the human saw; the first mutation under the lock must still find it.
        self._mutation_approval = {'operation': operation, 'root_fingerprint': self.root_fingerprint(),
                                   'identity': {name: state.get(name) for name in _IDS}, 'bound': False,
                                   'receipt_id': str(uuid.uuid4()), 'confirmed_at': now_ms, 'reason': reason}

    def _scope_deadline(self):
        """One absolute bound per lock scope, started at its first actual copy; later writers share it."""
        if self._mutation_deadline is None:
            from .keychain_owner import READ_BUDGET_S
            deadline = time.monotonic() + READ_BUDGET_S
            self._mutation_deadline = (deadline if self._credential_deadline is None
                                       else min(deadline, self._credential_deadline))
        self._before_deadline()
        return self._mutation_deadline

    def _approved_scope_deadline(self):
        """Inside an approved lock scope even the first ordinary copy is bound and starts the budget."""
        approval = self._mutation_approval
        if approval is None or self._tick_fd is None or self._tick_thread != threading.get_ident():
            return None
        if not approval['bound']:
            self._bind_approval(self._read_file('state.json', {}))
        return self._scope_deadline()

    def _bind_approval(self, state):
        # Changed after the human confirmation (outside this lock): hold, never re-target the approval.
        if {name: state.get(name) for name in _IDS} != self._mutation_approval['identity']:
            raise ValueError('SDC_CREDENTIAL_APPROVAL_STALE')
        self._mutation_approval['bound'] = True

    def _before_deadline(self):
        if not time.monotonic() < self._mutation_deadline:
            raise ValueError('SDC_CREDENTIAL_DEADLINE')

    @staticmethod
    def _progress(state):
        sequence, sending = state.get('sequence', 0), state.get('sending')
        if type(sequence) is not int or sending is not None and type(sending) is not str:
            raise ValueError('SDC_STATE_INVALID')
        return sequence, hashlib.sha256(sending.encode()).hexdigest() if sending else None

    def _require_lock(self):
        if self._tick_fd is None or self._tick_thread != threading.get_ident():
            raise ValueError('SDC_CREDENTIAL_LOCK_REQUIRED')

    def _stop_hash(self, state):
        return self._fingerprint({name: [name in state, state.get(name)] for name in _STOP})

    @staticmethod
    def _reflected(state, purpose, after):
        """Closed per-purpose metadata: a new claim has one fixed shape; others patch identity only."""
        if purpose in ('initial-key', 'reenroll-key'):
            return {'claim_id': after['claim_id'], 'state': 'pending', 'sequence': 0, 'enabled': False}
        value = dict(state)
        for name, item in after.items():
            if item is None:
                value.pop(name, None)
            else:
                value[name] = item
        return value

    def _publish(self, verified, now_ms):
        """Reflect a verified intent into current state (never a saved snapshot), then CAS it complete."""
        operation = verified['mutation']
        context = operation['context']
        state = self._read_file('state.json', {})
        current, progress = {name: state.get(name) for name in _IDS}, self._progress(state)
        reflected = self._reflected(state, operation['purpose'], context['after'])
        original = (context['stop_before_sha256'], (context['sequence'], context['sending_sha256']))
        # A new claim intentionally restarts the sequence; every other purpose keeps the original progress.
        published = ((self._stop_hash(reflected), self._progress(reflected))
                     if operation['purpose'] in ('initial-key', 'reenroll-key') else original)
        observed = (self._stop_hash(state), progress)
        if not (current == context['before'] and observed == original
                or current == context['after'] and observed == published):
            raise ValueError('SDC_CREDENTIAL_MUTATION_HELD')
        self.write('state.json', reflected)  # Unchanged metadata is still made durable here.
        complete = {**verified, 'mutation': {**operation, 'phase': 'complete',
                                             'completed_at': max(now_ms, operation['verified_at'])}}
        return mutation_contract.write_marker(self.fd, complete,
                                              expected_sha256=mutation_contract.marker_sha256(verified))

    def _mutate_credential(self, value, *, mutation, delete=False):
        """Run one approved write/delete: durable pending, one owned native call, state, complete."""
        self._require_lock()
        approval, purpose = self._mutation_approval, mutation.get('purpose') if type(mutation) is dict else None
        if (approval is None or purpose not in _OPERATIONS[approval['operation']]
                or approval['root_fingerprint'] != self.root_fingerprint()):
            raise ValueError('SDC_CREDENTIAL_APPROVAL_REQUIRED')
        # The Store's own wall clock orders the record; a stepped-back clock never predates the approval.
        now_ms = max(int(time.time() * 1000), approval['confirmed_at'])
        self.guard_credential()
        marker = self._marker()
        state = self._read_file('state.json', {})
        (sequence, sending), before = self._progress(state), {name: state.get(name) for name in _IDS}
        first = not approval['bound']
        if first:
            self._bind_approval(state)
        deadline = self._scope_deadline()
        context = {'before': before, 'after': before if purpose == 'delete' else mutation.get('after'),
                   'sequence': sequence, 'stop_before_sha256': self._stop_hash(state), 'sending_sha256': sending}
        raw = None if delete else mutation_contract.credential_bytes(value)
        old = self._actual_credential(deadline=deadline)
        if self._marker() != marker:
            raise ValueError('SDC_CREDENTIAL_STORE_INVALID')
        self._observed_credential(marker, old)
        # Later writers follow only this scope's published transitions; the one central commit
        # step (state key_id := the saved candidate's next_key_id) is checked against that candidate.
        tracked = approval['identity']
        committed = (purpose == 'rotation-finalize' and isinstance(old, dict) and 'next_key_id' in old
                     and before == {**tracked, 'key_id': old['next_key_id']})
        if not first and before != tracked and not committed:
            raise ValueError('SDC_CREDENTIAL_APPROVAL_STALE')
        # A new intent starts without the previous intent's reconcile receipt.
        base = {name: item for name, item in marker.items() if name != 'reconcile'}
        if marker['schema_version'] != mutation_contract.SCHEMA:
            base.update(schema_version=mutation_contract.SCHEMA, mutation=None,
                        tick_lock_fingerprint=mutation_contract.tick_lock_fingerprint(self._tick_fd))
        intent = str(uuid.uuid4())
        pending = {**base, 'mutation': {
            'id': intent, 'op': 'write' if raw is not None else 'delete', 'purpose': purpose, 'phase': 'pending',
            'old_sha256': None if old is None else mutation_contract.credential_digest(old),
            'new_sha256': None if raw is None else hashlib.sha256(raw).hexdigest(), 'started_at': now_ms,
            'context': context, 'approval': {name: approval[name] for name in ('receipt_id', 'confirmed_at', 'reason')},
            'verified_at': None, 'completed_at': None}}
        expected = mutation_contract.marker_sha256(pending)  # Closed purpose/context rules before any write.
        self._before_deadline()
        try:
            mutation_contract.write_marker(self.fd, pending, expected_sha256=mutation_contract.marker_sha256(marker))
            native, lease = self._keychain(), {'intent_id': intent, 'marker_sha256': expected, 'root_fd': self.fd,
                                               'tick_fd': self._tick_fd, 'deadline': deadline}
            result = native.delete(**lease) if raw is None else native.write(raw, **lease)
            verified = mutation_contract.read_marker(self.fd)
            if (type(result) is not dict or result.get('intent_id') != intent or verified is None
                    or verified.get('mutation') is None
                    or result.get('marker_sha256') != mutation_contract.marker_sha256(verified)
                    or verified != {**pending, 'mutation': {**pending['mutation'], 'phase': 'verified',
                                                            'verified_at': verified['mutation']['verified_at']}}):
                raise ValueError('SDC_CREDENTIAL_STORE_INVALID')
            self._before_deadline()  # A late result is never adopted.
            self._publish(verified, now_ms)
            approval['identity'] = dict(context['after'])
            # Bytes already made durable stay; a late completion is still not a success for the caller.
            self._before_deadline()
        except BaseException:
            # Unknown outcome: this Store never runs native, publishes or sends again.
            self._credential_uncertain = True
            raise

    def reconcile_credential(self, *, confirm_human, reason, now_ms):
        """Adopt a child-verified or visible complete intent after one explicit actual read; never repair."""
        _human(confirm_human, reason, now_ms)
        self._require_lock()
        if self._credential_uncertain:
            raise ValueError('SDC_CREDENTIAL_UNCERTAIN')
        marker = self._marker()
        operation = marker.get('mutation') if marker is not None else None
        if operation is None or operation['phase'] == 'pending':
            raise ValueError('SDC_CREDENTIAL_MUTATION_HELD')  # No copy: pending is never completed.
        deadline = self._scope_deadline()
        # The latest explicit reconcile approval is durable before the one actual copy; the original
        # intent and its approval stay unchanged.
        recorded = {**marker, 'reconcile': {'intent_id': operation['id'], 'receipt_id': str(uuid.uuid4()),
                                            'confirmed_at': now_ms, 'reason': reason}}
        try:
            mutation_contract.write_marker(self.fd, recorded, expected_sha256=mutation_contract.marker_sha256(marker))
            actual = self._actual_credential(deadline=deadline)
            if self._marker() != recorded or operation['new_sha256'] != (
                    None if actual is None else mutation_contract.credential_digest(actual)):
                raise ValueError('SDC_CREDENTIAL_MUTATION_HELD')
            self._before_deadline()
            if operation['phase'] == 'verified':
                result = self._publish(recorded, now_ms)
            else:
                state = self._read_file('state.json', {})
                if {name: state.get(name) for name in _IDS} != operation['context']['after']:
                    raise ValueError('SDC_CREDENTIAL_MUTATION_HELD')
                # Re-make the visible completion durable now; this claims no earlier fsync succeeded.
                self.write('state.json', state)
                result = mutation_contract.write_marker(self.fd, recorded,
                                                        expected_sha256=mutation_contract.marker_sha256(recorded))
            self._before_deadline()
            return result
        except BaseException:
            self._credential_uncertain = True
            raise

    def read_current_credential(self, *, copy_current):
        """Borrow an owned native reader without changing this Store's other callers."""
        marker = self._marker()
        if marker is None:
            value = self._read_file('credential')
            if self._marker() is not None:
                raise ValueError('SDC_CREDENTIAL_STORE_INVALID')
            return value
        self.guard_credential()
        try:
            raw = copy_current(marker['account'])
        except KeychainError as error:
            if error.code != 'SDC_KEYCHAIN_NOT_FOUND':
                raise
            raw = None
        # A concurrent rotation/root/endpoint change cannot validate an old reply.
        if self._marker() != marker:
            raise ValueError('SDC_CREDENTIAL_STORE_INVALID')
        value = mutation_contract.credential_value(raw) if raw is not None else None
        self._observed_credential(marker, value)
        return value

    def write(self, name, value, *, mutation=None):
        self._name(name)
        if name == 'diagnostic-anchor.json':
            raise ValueError('SDC_DIAGNOSTIC_ANCHOR_IMMUTABLE')
        if name == 'release-owner-activation.json':
            if self._read_file(name, _MISSING) is not _MISSING:
                raise ValueError('SDC_RELEASE_OWNER_ACTIVATION_IMMUTABLE')
            self._write_file(name, value)
            return
        if name in ('endpoint.json', 'support-bootstrap.json') and self._read_file('provision-state.json', _MISSING) is not _MISSING:
            raise ValueError('SDC_PROVISION_MANAGED_ROOT')
        if name in ('support-state.json', 'restart-state.json', 'restart-execution.json', 'restart-planning.json', 'provision-state.json', 'diagnostic-state.json', 'release-owner.json', 'release-bootstrap.json', 'release-start-intent.json', 'release-migration-v3.json'):
            self._write_file(name, value)
            return
        if name == 'credential-store.json':
            raise ValueError('SDC_CREDENTIAL_STORE_FIXED')
        marker = self._marker()
        if name == 'endpoint.json' and marker is not None and self._endpoint_hash(value) != marker['endpoint_hash']:
            raise ValueError('SDC_CREDENTIAL_STORE_INVALID')
        if name == 'state.json' and marker is not None:
            if not isinstance(value, dict):
                raise ValueError('SDC_STATE_INVALID')
            value = {**value, 'credential_storage': 'keychain'}
        if name == 'credential' and marker is not None:
            self._mutate_credential(value, mutation=mutation)
            return
        self._write_file(name, value)

    def _write_file(self, name, value):
        self._name(name)
        if not isinstance(value, dict):
            raise ValueError('SDC_FILE_INVALID')
        try:
            os.close(self._open(name))
        except FileNotFoundError:
            pass
        raw = json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode()
        if len(raw) > LIMITS[name]:
            raise ValueError('SDC_FILE_TOO_LARGE')
        temp = '.sdc-' + secrets.token_hex(16)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(temp, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    @contextmanager
    def lock(self):
        if self._tick_fd is not None:
            raise ValueError('SDC_CREDENTIAL_LOCK_REENTRY')
        with self._file_lock('tick.lock') as fd:
            self._tick_fd, self._tick_thread = fd, threading.get_ident()
            try:
                yield
            finally:
                self._tick_fd, self._tick_thread = None, None
                self._mutation_approval = self._mutation_deadline = None

    @contextmanager
    def diagnostic_state_lock(self):
        """Keep local diagnostic cancellation independent of credential and monitoring waits."""
        with self._file_lock('diagnostic-state.lock'):
            yield

    def create_diagnostic_anchor(self, value):
        """Durably create one immutable local introduction marker; never repair by deletion."""
        self.root_fingerprint()
        if type(value) is not dict:
            raise ValueError('SDC_FILE_INVALID')
        raw = json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')
        if not 1 <= len(raw) <= LIMITS['diagnostic-anchor.json']:
            raise ValueError('SDC_FILE_TOO_LARGE')
        fd = self._open('diagnostic-anchor.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(self.fd)
            self.root_fingerprint()
        except BaseException:
            self._diagnostic_uncertain = True
            raise

    @contextmanager
    def support_state_lock(self):
        """Own local support state independently of monitoring HTTP or jitter."""
        with self._file_lock('support-state.lock'):
            yield

    @contextmanager
    def support_session_lock(self):
        """Own one foreground support session without holding the monitoring lock."""
        with self._file_lock('support-session.lock'):
            yield

    @contextmanager
    def restart_state_lock(self):
        """Own one restart consent record independently of monitoring or diagnostic I/O."""
        with self._file_lock('restart-state.lock'):
            yield

    @contextmanager
    def release_authority_lock(self):
        """Serialize release consent and bootstrap before monitor tick and short owner locks."""
        if self._release_authority_fd is not None:
            raise ValueError('SDC_RELEASE_AUTHORITY_LOCK_REENTRY')
        with self._file_lock('release-authority.lock') as fd:
            self._release_authority_fd, self._release_authority_thread = fd, threading.get_ident()
            try:
                yield fd
            finally:
                self._release_authority_fd = self._release_authority_thread = None

    @contextmanager
    def release_owner_lock(self):
        """Serialize release authority separately from patient data and updater journals."""
        with self._file_lock('release-owner.lock') as fd:
            yield fd

    @contextmanager
    def release_entry_lock(self, *, shared=False):
        """Lifetime interlock only; neither a held FD nor its mode grants permission to update."""
        if type(shared) is not bool or self._release_entry_fd is not None:
            raise ValueError('SDC_RELEASE_ENTRY_LOCK_REENTRY')
        with self._file_lock('release-entry.lock', shared=shared) as fd:
            self._release_entry_fd, self._release_entry_thread = fd, threading.get_ident()
            self._release_entry_shared = shared
            try:
                yield fd
            finally:
                self._release_entry_fd = self._release_entry_thread = self._release_entry_shared = None

    @contextmanager
    def _file_lock(self, name, *, shared=False):
        flags = os.O_RDWR | os.O_CREAT
        if name == 'tick.lock':
            # A customer root must not recreate its bound lock inode. Presence alone decides, so
            # metadata-only callers take the lock without reading the credential marker.
            try:
                os.stat(mutation_contract.NAME, dir_fd=self.fd, follow_symlinks=False)
                flags = os.O_RDWR
            except FileNotFoundError:
                pass
        fd = self._open(name, flags)
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            yield fd
        finally:
            os.close(fd)

    def remove_credential(self, *, mutation=None):
        if self._marker() is not None:
            self._mutate_credential(None, mutation=mutation, delete=True)
            return
        os.close(self._open('credential'))
        os.unlink('credential', dir_fd=self.fd)
        os.fsync(self.fd)
