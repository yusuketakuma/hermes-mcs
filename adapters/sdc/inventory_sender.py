"""Send one immutable inventory page after the existing heartbeat tick."""
import hashlib
import json
import re
import time
import uuid

from .connector import Connector
from .inventory import MAX_PAGE_BYTES, MAX_PAGES, _name, _raw, _valid_snapshot, _version, collect_inventory, inventory_pages
from .protocol import endpoint, strict_json
from .transport import TransportError

CONSENT = 'sdc-public-inventory-v1'
POLICY_SCHEMA = 'sdc.inventory.policy.v1'
MAX_PUBLIC_PACKAGES = 1000
INTERVAL = 6 * 3600
PATH = '/v2/inventory/pages'
RESULTS = frozenset({'SDC_INVENTORY_DISABLED', 'SDC_INVENTORY_WAITING', 'SDC_INVENTORY_CLOCK_UNTRUSTED',
                     'SDC_INVENTORY_RUNTIME_UNKNOWN', 'SDC_INVENTORY_BACKOFF', 'SDC_INVENTORY_STORED',
                     'SDC_INVENTORY_ACCEPTED', 'SDC_INVENTORY_PARTIAL', 'SDC_INVENTORY_RETRY_LATER',
                     'SDC_INVENTORY_REQUEST_DENIED', 'SDC_INVENTORY_PROTOCOL_ERROR', 'SDC_INVENTORY_UNAVAILABLE'})


def validate_policy(policy):
    fields = {'schema_version', 'enabled', 'consent_revision', 'public_packages', 'lock_entries', 'revision', 'approved_at', 'device_id', 'endpoint_hash'}
    if not isinstance(policy, dict) or set(policy) != fields or policy['schema_version'] != POLICY_SCHEMA or type(policy['enabled']) is not bool or policy['consent_revision'] != CONSENT:
        raise ValueError('SDC_INVENTORY_POLICY_INVALID')
    names, locks = policy['public_packages'], policy['lock_entries']
    try:
        if not isinstance(names, list) or not 1 <= len(names) <= MAX_PUBLIC_PACKAGES or any(_name(name) != name for name in names) or len(set(names)) != len(names):
            raise ValueError
        if str(uuid.UUID(policy['revision'])) != policy['revision'] or type(policy['approved_at']) is not int or policy['approved_at'] < 0:
            raise ValueError
        if str(uuid.UUID(policy['device_id'])) != policy['device_id'] or not isinstance(policy['endpoint_hash'], str) or len(policy['endpoint_hash']) != 64 or any(value not in '0123456789abcdef' for value in policy['endpoint_hash']):
            raise ValueError
        if locks is not None:
            if not isinstance(locks, dict) or len(locks) > MAX_PUBLIC_PACKAGES:
                raise ValueError
            for name, value in locks.items():
                if _name(name) != name or name not in names:
                    raise ValueError
                _version(value)
    except (ValueError, TypeError, AttributeError):
        raise ValueError('SDC_INVENTORY_POLICY_INVALID') from None
    return policy


def endpoint_hash(config):
    return hashlib.sha256(json.dumps(endpoint(config), sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _valid_spool(spool, policy):
    if not isinstance(spool, dict) or set(spool) != {'policy_revision', 'device_id', 'endpoint_hash', 'snapshot_id', 'pages'}:
        return False
    pages = spool['pages']
    if not isinstance(pages, list) or not 1 <= len(pages) <= MAX_PAGES:
        return False
    try:
        if str(uuid.UUID(spool['snapshot_id'])) != spool['snapshot_id']:
            return False
        decoded = []
        for index, text in enumerate(pages, 1):
            if not isinstance(text, str) or len(text.encode('ascii')) > MAX_PAGE_BYTES:
                return False
            page = strict_json(text.encode('ascii'))
            if not isinstance(page, dict) or page.get('schema_version') != 'sdc.inventory.page.v1' or page.get('snapshot_id') != spool['snapshot_id'] or type(page.get('page')) is not int or page['page'] != index or type(page.get('page_count')) is not int or page['page_count'] != len(pages):
                return False
            if not isinstance(page.get('packages'), list) or not isinstance(page.get('lock_discrepancies'), list) or len(page['packages']) + len(page['lock_discrepancies']) > 100:
                return False
            decoded.append(page)
        first = decoded[0]
        header = {key: value for key, value in first.items() if key not in ('packages', 'lock_discrepancies', 'page')}
        if any({key: value for key, value in page.items() if key not in ('packages', 'lock_discrepancies', 'page')} != header for page in decoded):
            return False
        snapshot = {key: value for key, value in first.items() if key not in ('snapshot_id', 'snapshot_hash', 'page', 'page_count')}
        snapshot.update(schema_version='sdc.inventory.v1', hash=first['snapshot_hash'],
                        packages=[item for page in decoded for item in page['packages']],
                        lock_discrepancies=[item for page in decoded for item in page['lock_discrepancies']])
        return _valid_snapshot(snapshot) and hashlib.sha256(_raw({key: value for key, value in snapshot.items() if key != 'hash'})).hexdigest() == snapshot['hash'] and all(item['name'] in policy['public_packages'] for field in ('packages', 'lock_discrepancies') for item in snapshot[field])
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError):
        return False


def initial_status(revision):
    return {'policy_revision': revision, 'device_id': None, 'snapshot_id': None, 'next_page': 0,
            'next_due': 0, 'failures': 0, 'last_result': 'SDC_INVENTORY_WAITING', 'stopped': False,
            'completed_at': None, 'collected_at': None, 'source_revision': None, 'runtime': None}


class InventorySender:
    def __init__(self, store, transport, *, clock=time.time, collector=collect_inventory):
        self.store, self.transport, self.clock, self.collector = store, transport, clock, collector

    def _save(self, status, code, *, stopped=False):
        status.update(last_result=code, stopped=stopped)
        self.store.write('inventory-status.json', status)
        return code

    def tick(self, facts):
        """Caller owns Store.lock; never change heartbeat state or sequence."""
        policy = self.store.read('inventory-policy.json')
        if policy is None:
            return 'SDC_INVENTORY_DISABLED'
        validate_policy(policy)
        if not policy['enabled']:
            return 'SDC_INVENTORY_DISABLED'
        state = self.store.read('state.json', {})
        if state.get('state') != 'active' or not state.get('enabled') or state.get('disconnected') or state.get('central_suspended') or state.get('rotation_id'):
            return 'SDC_INVENTORY_WAITING'
        now = self.clock()
        if state.get('clock_status') != 'ok' or not 0 <= now - state.get('clock_checked_at', -1e10) <= 900:
            return 'SDC_INVENTORY_CLOCK_UNTRUSTED'
        if facts.get('mode') not in ('hermes', 'standalone'):
            return 'SDC_INVENTORY_RUNTIME_UNKNOWN'
        status = self.store.read('inventory-status.json') or initial_status(policy['revision'])
        if status.get('policy_revision') != policy['revision']:
            status = initial_status(policy['revision'])
        if status.get('stopped'):
            return status['last_result'] if status.get('last_result') in RESULTS else 'SDC_INVENTORY_UNAVAILABLE'
        previous_runtime = status.get('runtime') or {}
        revision, python = facts.get('mcs_commit'), facts.get('python_version')
        changed = status.get('completed_at') is not None and status.get('last_result') in ('SDC_INVENTORY_ACCEPTED', 'SDC_INVENTORY_PARTIAL') and (
            isinstance(revision, str) and re.fullmatch('[0-9a-f]{40}', revision) and revision != status.get('source_revision')
            or facts['mode'] != previous_runtime.get('mode')
            or isinstance(python, str) and re.fullmatch(r'[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}', python) and python != previous_runtime.get('python_version'))
        if status.get('next_due', 0) > now and not changed:
            return 'SDC_INVENTORY_BACKOFF'
        target_hash = endpoint_hash(self.store.read('endpoint.json'))
        if policy['device_id'] != state.get('device_id') or policy['endpoint_hash'] != target_hash:
            return 'SDC_INVENTORY_WAITING'
        spool = self.store.read('inventory-spool.json')
        current = isinstance(spool, dict) and spool.get('policy_revision') == policy['revision'] and spool.get('device_id') == state.get('device_id') and spool.get('endpoint_hash') == target_hash
        if current and status.get('snapshot_id') != spool.get('snapshot_id'):
            if not _valid_spool(spool, policy):
                return self._save(status, 'SDC_INVENTORY_PROTOCOL_ERROR', stopped=True)
            # A crash between spool and status fsync must reuse the reserved bytes.
            first = strict_json(spool['pages'][0].encode('ascii'))
            status = initial_status(policy['revision'])
            status.update(device_id=state['device_id'], snapshot_id=spool['snapshot_id'], collected_at=now,
                          source_revision=first['source_revision'], runtime=first['runtime'])
            self.store.write('inventory-status.json', status)
        if not current or status.get('completed_at') is not None:
            try:
                snapshot = self.collector(public_packages=policy['public_packages'], runtime_mode=facts['mode'],
                                          source_revision=facts.get('mcs_commit'), lock_entries=policy['lock_entries'], clock=self.clock)
                snapshot_id = str(uuid.uuid4())
                pages = inventory_pages(snapshot, snapshot_id)
            except TimeoutError:
                raise
            except Exception:
                status['next_due'] = now + INTERVAL
                return self._save(status, 'SDC_INVENTORY_UNAVAILABLE')
            spool = {'policy_revision': policy['revision'], 'device_id': state['device_id'], 'endpoint_hash': target_hash,
                     'snapshot_id': snapshot_id, 'pages': [raw.decode('ascii') for raw in pages]}
            self.store.write('inventory-spool.json', spool)
            status = initial_status(policy['revision'])
            status.update(device_id=state['device_id'], snapshot_id=snapshot_id, collected_at=now,
                          source_revision=snapshot['source_revision'], runtime=snapshot['runtime'])
            self.store.write('inventory-status.json', status)
        pages = spool.get('pages')
        index = status.get('next_page')
        if not _valid_spool(spool, policy) or type(index) is not int or not 0 <= index < len(pages) or spool.get('snapshot_id') != status.get('snapshot_id'):
            return self._save(status, 'SDC_INVENTORY_PROTOCOL_ERROR', stopped=True)
        raw = pages[index].encode('ascii')
        if len(raw) > MAX_PAGE_BYTES:
            return self._save(status, 'SDC_INVENTORY_PROTOCOL_ERROR', stopped=True)
        page = strict_json(raw)
        if page.get('snapshot_id') != spool['snapshot_id'] or page.get('page') != index + 1 or page.get('page_count') != len(pages):
            return self._save(status, 'SDC_INVENTORY_PROTOCOL_ERROR', stopped=True)
        # Recheck current local cancellation after collection and immediately before dispatch.
        current_policy = self.store.read('inventory-policy.json')
        current_state = self.store.read('state.json', {})
        if current_policy != policy or current_state.get('state') != 'active' or not current_state.get('enabled') or current_state.get('central_suspended') or current_state.get('disconnected') or current_state.get('device_id') != policy['device_id'] or current_state.get('rotation_id'):
            return 'SDC_INVENTORY_WAITING'
        if current_state.get('clock_status') != 'ok' or not 0 <= self.clock() - current_state.get('clock_checked_at', -1e10) <= 900:
            return 'SDC_INVENTORY_CLOCK_UNTRUSTED'
        if endpoint_hash(self.store.read('endpoint.json')) != policy['endpoint_hash']:
            return 'SDC_INVENTORY_WAITING'
        try:
            connector = Connector(self.store, self.transport, clock=self.clock)
            result = connector.request(PATH, raw, current_state, connector.secret(current_state, self.store.read('credential')))
        except TransportError as error:
            if error.status in (429, 503) or error.status >= 500:
                status['failures'] = min(status.get('failures', 0) + 1, 4)
                status['next_due'] = now + max(min(error.retry_after, 86400), (300, 600, 1200, 1800)[status['failures'] - 1])
                return self._save(status, 'SDC_INVENTORY_RETRY_LATER')
            return self._save(status, 'SDC_INVENTORY_REQUEST_DENIED', stopped=True)
        try:
            request_id = result.get('request_id') if isinstance(result, dict) else None
            valid_request_id = isinstance(request_id, str) and str(uuid.UUID(request_id)) == request_id and uuid.UUID(request_id).version == 4
        except ValueError:
            valid_request_id = False
        if not isinstance(result, dict) or set(result) != {'code', 'snapshot_id', 'state', 'request_id'} or not valid_request_id or result['snapshot_id'] != spool['snapshot_id']:
            return self._save(status, 'SDC_INVENTORY_PROTOCOL_ERROR', stopped=True)
        if result['code'] in ('accepted', 'duplicate') and result['state'] in ('complete', 'partial'):
            if result['state'] != ('complete' if page['complete'] else 'partial'):
                return self._save(status, 'SDC_INVENTORY_PROTOCOL_ERROR', stopped=True)
            status.update(next_page=len(pages), next_due=now + INTERVAL, failures=0, completed_at=now)
            return self._save(status, 'SDC_INVENTORY_ACCEPTED' if result['state'] == 'complete' else 'SDC_INVENTORY_PARTIAL')
        if result['code'] != 'stored' or result['state'] != 'receiving':
            return self._save(status, 'SDC_INVENTORY_PROTOCOL_ERROR', stopped=True)
        status['failures'] = 0
        if index + 1 == len(pages):
            status['next_due'] = now + 300
            return self._save(status, 'SDC_INVENTORY_RETRY_LATER')
        status.update(next_page=index + 1, next_due=0)
        return self._save(status, 'SDC_INVENTORY_STORED')
