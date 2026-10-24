"""独立診断同意と不変batchを私有保存し、取消と不明な送信を保持します。"""
import copy
import hashlib
import json
import re
import uuid

from .diagnostic_contract import COMPONENTS, LIMITS, validate_diagnostic, validate_diagnostic_shape
from .protocol import DIAGNOSTIC_EVENTS_REPORT as REPORT, DIAGNOSTIC_EVENTS_STATUS as STATUS
from .support_contract import MAX_SAFE_INTEGER, canonical

NAME = 'diagnostic-state.json'
ANCHOR = 'diagnostic-anchor.json'
SCHEMA = 'sdc.diagnostics.local.v1'
DAY7 = LIMITS['spool_age_ms']
MAX_BYTES = LIMITS['spool_bytes']
MAX_ROWS = LIMITS['spool_events']
MAX_ATTEMPTS = 1 + LIMITS['max_retries']
_RESULTS = ('waiting', 'ready', 'unknown', 'accepted', 'denied', 'cancelled',
            'expired', 'epoch_changed', 'inactive', 'capacity', 'backoff', 'collected')
_BINDING = ('root_fingerprint monitor_endpoint_hash support_endpoint_hash root_id tenant '
            'installation realm_epoch device_epoch device_id claim_id key_id environment audience')
_ROW = ('batch_id body_hash consent_epoch event_count dropped_events partial attempts result recorded_at '
        'receipt content_state local_receipt_id generation last_status_attempt')
_STABLE_SCOPE = ('root_fingerprint monitor_endpoint_hash support_endpoint_hash root_id tenant '
                 'installation device_id claim_id environment audience').split()


class DiagnosticStateError(ValueError):
    def __init__(self, code='STATE_INVALID'):
        if code not in ('STATE_INVALID', 'STATE_MISSING', 'STATE_CONFLICT', 'STORAGE_UNCERTAIN',
                        'HUMAN_REQUIRED', 'RETIRED', 'EXPIRED', 'CAPACITY', 'RESPONSE_INVALID'):
            code = 'STATE_INVALID'
        self.code = 'SDC_DIAGNOSTIC_' + code
        super().__init__(self.code)


def _check(ok, code='STATE_INVALID'):
    if not ok:
        raise DiagnosticStateError(code)


def _fields(value, names):
    _check(type(value) is dict and set(value) == set(names.split()))


def _integer(value, low=0, high=MAX_SAFE_INTEGER):
    _check(type(value) is int and low <= value <= min(high, MAX_SAFE_INTEGER))


def _match(value, pattern):
    _check(type(value) is str and re.fullmatch(pattern, value) is not None)


def _uuid(value):
    validate_diagnostic_shape('UUID', value)


def _hash(value):
    validate_diagnostic_shape('SHA256', value)


def validate_binding(value):
    """Validate independent target metadata; never acquire a credential or authority."""
    base = set(_BINDING.split())
    _check(type(value) is dict and set(value) in (base, base | {'support_bundle_hash'}))
    if 'support_bundle_hash' in value:
        _hash(value['support_bundle_hash'])
    for key in ('root_fingerprint', 'monitor_endpoint_hash', 'support_endpoint_hash'):
        _hash(value[key])
    for key in ('root_id', 'device_id', 'claim_id', 'key_id'):
        _uuid(value[key])
    for key in ('tenant', 'installation'):
        _match(value[key], r'[a-z0-9][a-z0-9_-]{0,63}')
    _match(value['realm_epoch'], r'[A-Za-z0-9][A-Za-z0-9:_-]{0,127}')
    _match(value['environment'], r'[a-z0-9_-]{1,32}')
    _match(value['audience'], r'[A-Za-z0-9:_-]{1,100}')
    _integer(value['device_epoch'], 1)
    return dict(value)


def _encoded(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('ascii')


def _marks(value):
    _check(type(value) is dict and set(value) <= set(COMPONENTS))
    for row in value.values():
        _fields(row, 'observed_at signature')
        _integer(row['observed_at'])
        _hash(row['signature'])


def _receipt(value, row):
    receipt = validate_diagnostic_shape('ReceiptResponse', value)
    for name in ('batch_id', 'consent_epoch', 'event_count', 'dropped_events', 'partial'):
        _check(receipt[name] == row[name], 'RESPONSE_INVALID')
    return receipt


def _local_receipt(receipt):
    _fields(receipt, 'local_receipt_id generation accepted_at expires_at reason')
    _uuid(receipt['local_receipt_id'])
    _uuid(receipt['generation'])
    _integer(receipt['accepted_at'])
    _integer(receipt['expires_at'])
    _check(60000 <= receipt['expires_at'] - receipt['accepted_at'] <= LIMITS['consent_max_ms'])
    _match(receipt['reason'], r'[\x20-\x7e]{1,128}')
    _check(receipt['reason'].strip() != '')


def _validated(value):
    _fields(value, 'schema_version anchor_id binding revision generation enabled receipt cancelled_at '
            'consent_epoch central_expires_at queue pending history consent_history '
            'dropped_events partial source_marks result next_due')
    _check(value['schema_version'] == SCHEMA and type(value['enabled']) is bool)
    _uuid(value['anchor_id'])
    _uuid(value['generation'])
    validate_binding(value['binding'])
    _integer(value['revision'])
    _local_receipt(value['receipt'])
    _check(not value['enabled'] or value['generation'] == value['receipt']['generation'])
    if value['cancelled_at'] is not None:
        _integer(value['cancelled_at'])
        _check(not value['enabled'])
    _check((value['consent_epoch'] is None) == (value['central_expires_at'] is None))
    if value['consent_epoch'] is not None:
        _integer(value['consent_epoch'], 1)
        _integer(value['central_expires_at'])
    _integer(value['dropped_events'], 0, LIMITS['dropped_max'])
    _check(type(value['partial']) is bool and value['result'] in _RESULTS)
    _integer(value['next_due'])
    _marks(value['source_marks'])
    _check(type(value['consent_history']) is list and len(value['consent_history']) <= MAX_ROWS)
    receipt_ids = {value['receipt']['local_receipt_id']: value['receipt']['generation']}
    for retired in value['consent_history']:
        _fields(retired, 'receipt binding consent_epoch ended_at dropped_events result')
        _local_receipt(retired['receipt'])
        validate_binding(retired['binding'])
        _check(all(retired['binding'][k] == value['binding'][k] for k in _STABLE_SCOPE))
        _check(retired['receipt']['local_receipt_id'] not in receipt_ids)
        receipt_ids[retired['receipt']['local_receipt_id']] = retired['receipt']['generation']
        if retired['consent_epoch'] is not None:
            _integer(retired['consent_epoch'], 1)
        _integer(retired['ended_at'])
        _integer(retired['dropped_events'], 0, LIMITS['dropped_max'])
        _check(retired['result'] in (*_RESULTS, 'renewed'))
    _check(type(value['queue']) is list and len(value['queue']) <= MAX_ROWS)
    for event in value['queue']:
        validate_diagnostic_shape('Event', event)
    _check(type(value['history']) is list and len(value['history']) + len(value['consent_history']) <= MAX_ROWS)
    identifiers = set()
    for row in value['history']:
        _fields(row, _ROW)
        _uuid(row['batch_id'])
        _hash(row['body_hash'])
        _check(row['batch_id'] not in identifiers)
        identifiers.add(row['batch_id'])
        _integer(row['consent_epoch'], 1)
        _integer(row['event_count'], 1, 64)
        _integer(row['dropped_events'], 0, LIMITS['dropped_max'])
        _integer(row['attempts'], 0, MAX_ATTEMPTS)
        _integer(row['recorded_at'])
        _uuid(row['local_receipt_id'])
        _uuid(row['generation'])
        _check(receipt_ids.get(row['local_receipt_id']) == row['generation'])
        _integer(row['last_status_attempt'], 0, value['revision'])
        _check(type(row['partial']) is bool and row['result'] in ('accepted', 'unknown'))
        _check(row['content_state'] in (None, 'stored', 'expired', 'deleted'))
        _check(row['result'] == 'accepted' or row['receipt'] is None and row['content_state'] is None)
        _check(row['result'] != 'accepted' or row['content_state'] is not None)
        if row['receipt'] is not None:
            _receipt(row['receipt'], row)
    pending = value['pending']
    if pending is not None:
        _fields(pending, 'batch_id body_hash consent_epoch event_count dropped_events partial raw '
                'created_at expires_at attempts phase last_attempt_at local_receipt_id generation')
        body = json.loads(pending['raw'])
        validate_diagnostic_shape('Batch', body)
        _check(canonical(body, max_bytes=16384) == pending['raw'])
        _check(hashlib.sha256(pending['raw'].encode('ascii')).hexdigest() == pending['body_hash'])
        for key in ('batch_id', 'consent_epoch', 'dropped_events', 'partial'):
            _check(pending[key] == body[key])
        _check(pending['event_count'] == len(body['events']) and pending['batch_id'] not in identifiers)
        _integer(pending['event_count'], 1, 64)
        _integer(pending['consent_epoch'], 1)
        _integer(pending['dropped_events'], 0, LIMITS['dropped_max'])
        _check(type(pending['partial']) is bool)
        _check(len(value['queue']) + pending['event_count'] <= MAX_ROWS)
        _check(len(value['history']) + len(value['consent_history']) < MAX_ROWS)
        _check(pending['local_receipt_id'] == value['receipt']['local_receipt_id']
               and pending['generation'] == value['receipt']['generation'])
        _integer(pending['created_at'])
        _integer(pending['expires_at'])
        _integer(pending['attempts'], 0, MAX_ATTEMPTS)
        _check(pending['phase'] in ('ready', 'unknown', 'replay', 'blocked'))
        if pending['last_attempt_at'] is not None:
            _integer(pending['last_attempt_at'])
        _check((pending['last_attempt_at'] is None) == (pending['attempts'] == 0))
        _check(pending['attempts'] == 0 if pending['phase'] == 'ready' else pending['attempts'] > 0)
        _check(pending['consent_epoch'] == value['consent_epoch'])
        _check(pending['created_at'] >= value['receipt']['accepted_at'])
        validate_diagnostic('Batch', body, now_ms=pending['created_at'])
        _check(pending['expires_at'] == min(pending['created_at'] + DAY7,
                                           *(e['bucket_start'] + DAY7 for e in body['events'])))
    # Even a full cancellation must fit the fixed dropped counter without rounding.
    _check(value['dropped_events'] + len(value['queue'])
           + (pending['event_count'] if pending else 0) <= LIMITS['dropped_max'], 'CAPACITY')
    _check(len(_encoded(value)) <= MAX_BYTES, 'CAPACITY')
    return value


def _read(store, *, missing=False):
    try:
        _check(not getattr(store, '_diagnostic_uncertain', False), 'STORAGE_UNCERTAIN')
        anchor, value = store.read(ANCHOR), store.read(NAME)
        if anchor is None and value is None and missing:
            return None
        _check(anchor is not None and value is not None, 'STATE_MISSING')
        _fields(anchor, 'schema_version anchor_id root_fingerprint')
        _check(anchor['schema_version'] == 'sdc.diagnostics.anchor.v1')
        _uuid(anchor['anchor_id'])
        _hash(anchor['root_fingerprint'])
        value = _validated(value)
        _check(anchor['anchor_id'] == value['anchor_id'] and anchor['root_fingerprint']
               == value['binding']['root_fingerprint'] == store.root_fingerprint(), 'STATE_CONFLICT')
        return copy.deepcopy(value)
    except (DiagnosticStateError, TimeoutError):
        raise
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise DiagnosticStateError() from None


def _save(store, value):
    _integer(value['revision'] + 1)
    value['revision'] += 1
    _validated(value)
    try:
        _check(store.root_fingerprint() == value['binding']['root_fingerprint'], 'STATE_CONFLICT')
        store.write(NAME, value)
        _check(store.root_fingerprint() == value['binding']['root_fingerprint']
               and store.read(NAME) == value, 'STATE_CONFLICT')
    except BaseException as error:
        store._diagnostic_uncertain = True
        if isinstance(error, TimeoutError):
            raise
        if isinstance(error, (OSError, ValueError, TypeError)):
            raise DiagnosticStateError('STORAGE_UNCERTAIN') from None
        raise


def _summary(value):
    pending = value['pending']
    last = max((row for row in value['history'] if row['receipt'] is not None),
               key=lambda row: (row['recorded_at'], row['batch_id']), default=None)
    return {'enabled': value['enabled'], 'local_receipt_id': value['receipt']['local_receipt_id'],
            'consent_epoch': value['consent_epoch'], 'queued_events': len(value['queue']),
            'pending_batch_id': pending['batch_id'] if pending else None,
            'attempts': pending['attempts'] if pending else 0, 'result': value['result'],
            'dropped_events': value['dropped_events'],
            'unknown_batches': sum(r['result'] == 'unknown' for r in value['history']),
            'last_receipt': copy.deepcopy(last['receipt']) if last else None,
            'last_receipt_local_receipt_id': last['local_receipt_id'] if last else None,
            'last_receipt_generation': last['generation'] if last else None}


def _history_space(value, now_ms, needed=0):
    value['history'] = [r for r in value['history']
                        if r['result'] != 'accepted' or now_ms <= r['recorded_at'] + DAY7]
    while len(value['history']) + len(value['consent_history']) + needed > MAX_ROWS:
        index = min((i for i, r in enumerate(value['history']) if r['result'] == 'accepted'),
                    key=lambda i: (value['history'][i]['recorded_at'], value['history'][i]['batch_id']), default=None)
        _check(index is not None, 'CAPACITY')
        value['history'].pop(index)


def _drop(value, count):
    _integer(count)
    _check(value['dropped_events'] + count <= LIMITS['dropped_max'], 'CAPACITY')
    value['dropped_events'] += count
    value['partial'] = value['partial'] or count > 0


def _history_row(pending, now_ms, *, accepted=False, receipt=None, content_state=None):
    row = {k: pending[k] for k in ('batch_id', 'body_hash', 'consent_epoch', 'event_count',
                                   'dropped_events', 'partial', 'attempts', 'local_receipt_id', 'generation')}
    row.update(result='accepted' if accepted else 'unknown', recorded_at=now_ms,
               receipt=copy.deepcopy(receipt), content_state=content_state, last_status_attempt=0)
    return row


def _archive(value, now_ms, *, accepted=False, receipt=None, content_state=None):
    pending = value['pending']
    if pending is None:
        return
    _history_space(value, now_ms, 1)
    value['history'].append(_history_row(pending, now_ms, accepted=accepted,
                                        receipt=receipt, content_state=content_state))
    if not accepted:
        _drop(value, pending['event_count'])
    value['pending'] = None


def _retire(value, now_ms, result):
    _archive(value, now_ms)
    _drop(value, len(value['queue']))
    value.update(queue=[], enabled=False, cancelled_at=now_ms, generation=str(uuid.uuid4()), result=result)


def _fits_transitions(value):
    """Measure storage forms only; this projection grants no authority and drops no evidence."""
    projected = copy.deepcopy(value)
    def fits(form):
        # Reserve widths for later reservations/cancellation, not a new time allowance.
        form.update(revision=MAX_SAFE_INTEGER, next_due=MAX_SAFE_INTEGER,
                    dropped_events=LIMITS['dropped_max'], result='epoch_changed',
                    enabled=False, cancelled_at=MAX_SAFE_INTEGER)
        for row in form['history']:
            row.update(last_status_attempt=MAX_SAFE_INTEGER, content_state='expired', result='accepted')
        return len(_encoded(form)) <= MAX_BYTES
    pending = projected['pending']
    if pending:
        pending.update(phase='unknown', last_attempt_at=MAX_SAFE_INTEGER,
                       created_at=MAX_SAFE_INTEGER, expires_at=MAX_SAFE_INTEGER)
    if not fits(projected):
        return False
    # An in-flight batch must also fit after its payload is removed and its
    # unknown metadata is archived. Do not prune history while measuring this.
    cancelled = copy.deepcopy(projected)
    cancelled.update(queue=[], pending=None, enabled=False, cancelled_at=MAX_SAFE_INTEGER)
    if pending:
        cancelled['history'].append(_history_row(pending, MAX_SAFE_INTEGER))
    if not fits(cancelled):
        return False
    if pending:
        # A genuine ACK may be larger than a tiny batch. Reserve its closed
        # receipt shape; the synthetic values below are never saved or returned.
        receipt = {k: pending[k] for k in ('batch_id', 'consent_epoch', 'event_count', 'dropped_events', 'partial')}
        receipt.update(schema_version='sdc.diagnostics.receipt.v1', request_id=pending['batch_id'],
                       state='accepted', content_state='expired', accepted_at=MAX_SAFE_INTEGER,
                       content_expires_at=MAX_SAFE_INTEGER)
        projected['history'].append(_history_row(pending, MAX_SAFE_INTEGER, accepted=True,
                                                 receipt=receipt, content_state='expired'))
        projected['pending'] = None
        if not fits(projected):
            return False
    return True


def _maintain(value, now_ms):
    _integer(now_ms)
    _check(now_ms >= value['receipt']['accepted_at'], 'EXPIRED')
    _history_space(value, now_ms, int(value['pending'] is not None))
    if value['enabled'] and (now_ms >= value['receipt']['expires_at'] or
                            value['central_expires_at'] is not None and now_ms >= value['central_expires_at']):
        _retire(value, now_ms, 'expired')
    pending = value['pending']
    if pending and now_ms > pending['expires_at']:
        _archive(value, now_ms)
        value['result'] = 'unknown'
    fresh = [e for e in value['queue'] if now_ms <= e['bucket_start'] + DAY7]
    _drop(value, len(value['queue']) - len(fresh))
    value['queue'] = fresh


def enable(store, *, binding, now_ms, expires_at, confirm_human=False, reason=None):
    """Explicit first local admission only; an existing anchor is never repaired or replaced."""
    _check(confirm_human is True, 'HUMAN_REQUIRED')
    _match(reason, r'[\x20-\x7e]{1,128}')
    _check(bool(reason.strip()), 'HUMAN_REQUIRED')
    _integer(now_ms)
    _integer(expires_at, now_ms + 60000, now_ms + LIMITS['consent_max_ms'])
    current = validate_binding(binding)
    _check(current['root_fingerprint'] == store.root_fingerprint(), 'STATE_CONFLICT')
    with store.diagnostic_state_lock():
        existing = _read(store, missing=True)
        if existing is not None:
            _maintain(existing, now_ms)
            _check(existing['enabled'], 'RETIRED')
            _check(existing['binding'] == current and existing['receipt']['expires_at'] == expires_at,
                   'STATE_CONFLICT')
            return _summary(existing)
        anchor = {'schema_version': 'sdc.diagnostics.anchor.v1', 'anchor_id': str(uuid.uuid4()),
                  'root_fingerprint': current['root_fingerprint']}
        try:
            store.create_diagnostic_anchor(anchor)
        except TimeoutError:
            store._diagnostic_uncertain = True
            raise
        except (OSError, ValueError, TypeError):
            raise DiagnosticStateError('STORAGE_UNCERTAIN') from None
        generation = str(uuid.uuid4())
        value = {'schema_version': SCHEMA, 'anchor_id': anchor['anchor_id'], 'binding': current,
                 'revision': 0, 'generation': generation, 'enabled': True,
                 'receipt': {'local_receipt_id': str(uuid.uuid4()), 'generation': generation, 'accepted_at': now_ms,
                             'expires_at': expires_at, 'reason': reason}, 'cancelled_at': None,
                 'consent_epoch': None, 'central_expires_at': None, 'queue': [], 'pending': None,
                 'history': [], 'consent_history': [], 'dropped_events': 0, 'partial': False, 'source_marks': {},
                 'result': 'waiting', 'next_due': 0}
        _save(store, value)
        return _summary(value)


def renew(store, *, binding, now_ms, expires_at, confirm_human=False, reason=None):
    """Explicit same-target re-consent; retain old evidence and await current central consent."""
    _check(confirm_human is True, 'HUMAN_REQUIRED')
    _match(reason, r'[\x20-\x7e]{1,128}')
    _check(bool(reason.strip()), 'HUMAN_REQUIRED')
    _integer(now_ms)
    _integer(expires_at, now_ms + 60000, now_ms + LIMITS['consent_max_ms'])
    current = validate_binding(binding)
    with store.diagnostic_state_lock():
        value = _read(store)
        _check(all(value['binding'][k] == current[k] for k in _STABLE_SCOPE), 'STATE_CONFLICT')
        _maintain(value, now_ms)
        # Never push out receipt/unknown metadata to make a new consent fit.
        _check(len(value['history']) + len(value['consent_history'])
               + int(value['pending'] is not None) + 1 <= MAX_ROWS, 'CAPACITY')
        retired = {'receipt': copy.deepcopy(value['receipt']), 'binding': copy.deepcopy(value['binding']),
                   'consent_epoch': value['consent_epoch'], 'ended_at': now_ms,
                   'result': 'renewed' if value['enabled'] else value['result']}
        _retire(value, now_ms, 'cancelled')
        retired['dropped_events'] = value['dropped_events']
        value['consent_history'].append(retired)
        generation = str(uuid.uuid4())
        value.update(binding=current, generation=generation, enabled=True, cancelled_at=None,
                     consent_epoch=None, central_expires_at=None, dropped_events=0, partial=False,
                     source_marks={}, result='waiting', next_due=0,
                     receipt={'local_receipt_id': str(uuid.uuid4()), 'generation': generation,
                              'accepted_at': now_ms, 'expires_at': expires_at, 'reason': reason})
        _save(store, value)
        return _summary(value)


def disable(store, *, now_ms):
    """Retire locally without tick lock, credentials or network; late ACKs cannot re-enable."""
    with store.diagnostic_state_lock():
        value = _read(store)
        _integer(now_ms)
        if value['enabled']:
            _retire(value, now_ms, 'cancelled')
            _save(store, value)
        return _summary(value)


def status(store, *, now_ms):
    """Return bounded metadata only; expired contents are removed under the same local lock."""
    with store.diagnostic_state_lock():
        value = _read(store, missing=True)
        if value is None:
            return {'enabled': False, 'local_receipt_id': None, 'consent_epoch': None,
                    'queued_events': 0, 'pending_batch_id': None, 'attempts': 0,
                    'result': 'waiting', 'dropped_events': 0, 'unknown_batches': 0, 'last_receipt': None,
                    'last_receipt_local_receipt_id': None, 'last_receipt_generation': None}
        original = copy.deepcopy(value)
        _maintain(value, now_ms)
        if value != original:
            _save(store, value)
        return _summary(value)


def _batch(value, now_ms, *, batch_id=None):
    _history_space(value, now_ms, 1)
    events, consumed, positions = [], 0, {}
    for event in value['queue']:
        key = tuple(event[k] for k in ('component', 'stage', 'code', 'severity', 'bucket_start'))
        if key in positions:
            prior = events[positions[key]]
            if prior['count'] + event['count'] > LIMITS['count_max']:
                break
            prior['count'] += event['count']
        else:
            if len(events) == LIMITS['events_per_batch']:
                break
            positions[key] = len(events)
            events.append(dict(event))
        consumed += 1
    body = {'schema_version': 'sdc.diagnostics.events.v1', 'batch_id': batch_id or str(uuid.uuid4()),
            'consent_epoch': value['consent_epoch'], 'events': events,
            'dropped_events': value['dropped_events'], 'partial': value['partial']}
    validate_diagnostic('Batch', body, now_ms=now_ms)
    raw = canonical(body, max_bytes=16384)
    value['pending'] = {k: body[k] for k in ('batch_id', 'consent_epoch', 'dropped_events', 'partial')}
    value['pending'].update(raw=raw, body_hash=hashlib.sha256(raw.encode('ascii')).hexdigest(),
                            event_count=len(events), created_at=now_ms,
                            expires_at=min(now_ms + DAY7, *(e['bucket_start'] + DAY7 for e in events)),
                            attempts=0, phase='ready', last_attempt_at=None,
                            local_receipt_id=value['receipt']['local_receipt_id'], generation=value['generation'])
    value.update(queue=value['queue'][consumed:], dropped_events=0, partial=False)


def _fit_batch(value, now_ms):
    """Trim only unfixed rows before allocating the real batch ID/raw once."""
    rows = value['queue']
    def fits(dropped):
        draft = copy.deepcopy(value)
        draft['queue'] = rows[dropped:]
        _drop(draft, dropped)
        _batch(draft, now_ms, batch_id='00000000-0000-4000-8000-000000000000')
        draft['pending'].update(attempts=1, phase='unknown', last_attempt_at=now_ms)
        return _fits_transitions(draft)
    low, high = 0, len(rows) - 1
    if not fits(low):
        if not fits(high):
            _retire(value, now_ms, 'capacity')
            return False
        # A bounded search avoids repeatedly serializing every near-limit row.
        low = 1
        while low < high:
            middle = (low + high) // 2
            if fits(middle):
                high = middle
            else:
                low = middle + 1
    value['queue'] = rows[low:]
    _drop(value, low)
    _batch(value, now_ms)
    return True


def admit(store, *, current, now_ms):
    """Reserve one HTTP before dispatch; caller owns tick.lock and the original deadline."""
    current = validate_binding(current)
    with store.diagnostic_state_lock():
        value = _read(store)
        _maintain(value, now_ms)
        if not value['enabled']:
            _save(store, value)
            return None
        if value['binding'] != current:
            _retire(value, now_ms, 'epoch_changed')
            _save(store, value)
            return None
        if now_ms < value['next_due']:
            return None
        room = True
        try:
            _history_space(value, now_ms, 1)
        except DiagnosticStateError:
            value['result'] = 'capacity'
            room = False  # Existing IDs may still be reconciled without admitting any new content.
        if value['pending'] is None and value['queue'] and room:
            if not _fit_batch(value, now_ms):
                _save(store, value)
                return None
        pending = value['pending']
        if pending and pending['phase'] == 'blocked':
            return None
        report = pending is not None and pending['phase'] in ('ready', 'replay')
        if report:
            _check(pending['attempts'] < MAX_ATTEMPTS, 'RETIRED')
            raw = pending['raw']
            pending.update(attempts=pending['attempts'] + 1, phase='unknown', last_attempt_at=now_ms)
            value['result'] = 'unknown'
        else:
            history = sorted((r for r in value['history'] if r['result'] == 'unknown'),
                             key=lambda row: (row['last_status_attempt'], row['recorded_at'], row['batch_id']))
            selected = history[:16 - int(pending is not None)]
            for row in selected:
                row['last_status_attempt'] = value['revision'] + 1
            rows = ([pending] if pending else []) + selected
            raw = canonical({'schema_version': 'sdc.diagnostics.status-request.v1',
                             'batch_ids': [row['batch_id'] for row in rows]}, max_bytes=16384)
        value['next_due'] = now_ms + 300000
        _save(store, value)
        return {'path': REPORT if report else STATUS, 'raw': raw.encode('ascii'),
                'revision': value['revision'], 'generation': value['generation'],
                'binding': copy.deepcopy(current), 'batch_id': pending['batch_id'] if pending else None,
                'body_hash': pending['body_hash'] if pending else None,
                'matches': {row['batch_id']: row['body_hash'] for row in rows} if not report else {}}


def check_dispatch(store, request, *, current, now_ms):
    """Check authority after the admission fsync; an unused reserved attempt is not refunded."""
    current = validate_binding(current)
    with store.diagnostic_state_lock():
        value = _read(store)
        original = copy.deepcopy(value)
        _maintain(value, now_ms)
        if value != original:
            _save(store, value)
        return (value['enabled'] and value['revision'] == request['revision']
                and value['generation'] == request['generation']
                and value['binding'] == current == request['binding'])


class _Sample:
    def __init__(self, store, value):
        self.store, self.revision, self.generation = store, value['revision'], value['generation']
        self.used = False
        self.since_ms = value['receipt']['accepted_at']
        self.previous_marks = copy.deepcopy(value['source_marks'])


def sample_context(store, permit, *, current, now_ms):
    """Recheck local cancellation immediately before the bounded provider admission."""
    _check(type(permit) is _Sample and permit.store is store and not permit.used, 'STATE_CONFLICT')
    current = validate_binding(current)
    with store.diagnostic_state_lock():
        value = _read(store)
        _maintain(value, now_ms)
        _check(value['enabled'] and value['revision'] == permit.revision
               and value['generation'] == permit.generation and value['binding'] == current, 'STATE_CONFLICT')
        return {'previous_marks': copy.deepcopy(permit.previous_marks), 'since_ms': permit.since_ms,
                'now_ms': now_ms}


def _locate(value, request):
    rows = ([value['pending']] if value['pending'] else []) + value['history']
    return next((r for r in rows if r['batch_id'] == request['batch_id']
                 and r['body_hash'] == request['body_hash']), None)


def record_response(store, request, response, *, current, now_ms):
    """Persist joined evidence; only this generation's live current STATUS can permit sampling."""
    current = validate_binding(current) if current is not None else None
    definition = 'ReceiptResponse' if request['path'] == REPORT else 'StatusResponse'
    response = validate_diagnostic(definition, response, now_ms=now_ms)
    with store.diagnostic_state_lock():
        value = _read(store)
        row = _locate(value, request) if request['batch_id'] is not None else None
        same = (value['enabled'] and value['revision'] == request['revision']
                and value['generation'] == request['generation'] and value['binding'] == current == request['binding'])
        if request['path'] == REPORT:
            _check(row is not None, 'STATE_CONFLICT')
            receipt = _receipt(response, row)
            if row is value['pending']:
                _archive(value, now_ms, accepted=True, receipt=receipt, content_state=receipt['content_state'])
            else:
                at = row['recorded_at'] if row['result'] == 'accepted' else now_ms
                row.update(result='accepted', recorded_at=at, receipt=receipt, content_state=receipt['content_state'])
            if value['enabled'] and request['generation'] == value['generation']:
                value['result'] = 'accepted'
            _maintain(value, now_ms)
            if current is not None and value['enabled'] and value['binding'] != current:
                _retire(value, now_ms, 'epoch_changed')
            _save(store, value)
            return None
        _check(request['path'] == STATUS, 'RESPONSE_INVALID')
        expected = json.loads(request['raw'])['batch_ids']
        _check(set(request['matches']) == set(expected), 'RESPONSE_INVALID')
        _check([r['batch_id'] for r in response['batches']] == expected, 'RESPONSE_INVALID')
        for observed in response['batches']:
            row = _locate(value, {'batch_id': observed['batch_id'],
                                  'body_hash': request['matches'][observed['batch_id']]})
            _check(row is not None, 'STATE_CONFLICT')
            if observed['state'] == 'accepted':
                current_receipt = row['local_receipt_id'] == value['receipt']['local_receipt_id']
                if row is value['pending']:
                    _archive(value, now_ms, accepted=True, content_state=observed['content_state'])
                else:
                    at = row['recorded_at'] if row['result'] == 'accepted' else now_ms
                    row.update(result='accepted', recorded_at=at, content_state=observed['content_state'])
                if value['enabled'] and request['generation'] == value['generation'] and current_receipt:
                    value['result'] = 'accepted'
        _maintain(value, now_ms)
        if not same or not value['enabled']:
            _save(store, value)
            return None
        consent = response['consent']
        if consent['state'] != 'active' or consent['expires_at'] <= now_ms:
            if value['consent_epoch'] is None:
                value['result'] = 'waiting'  # No central grant observed yet; this is not a revocation.
            else:
                _retire(value, now_ms, 'inactive')
        elif value['consent_epoch'] is not None and value['consent_epoch'] != consent['consent_epoch']:
            _retire(value, now_ms, 'epoch_changed')
        else:
            value['consent_epoch'] = consent['consent_epoch']
            value['central_expires_at'] = (min(value['central_expires_at'], consent['expires_at'])
                                            if value['central_expires_at'] is not None else consent['expires_at'])
            if value['pending'] is not None:
                if value['pending']['attempts'] >= MAX_ATTEMPTS:
                    _archive(value, now_ms)
                    value['result'] = 'unknown'
                else:
                    value['pending']['phase'] = 'replay'
        may_sample = (request['batch_id'] is None and value['pending'] is None and not value['queue']
                      and value['enabled'] and value['consent_epoch'] is not None
                      and consent['state'] == 'active' and consent['expires_at'] > now_ms)
        if may_sample:
            try:
                _history_space(value, now_ms, 1)
            except DiagnosticStateError:
                value['result'], may_sample = 'capacity', False
        _save(store, value)
        return _Sample(store, value) if may_sample else None


def collect(store, permit, sample, *, current, now_ms):
    """Consume one same-Store RAM permit; never recreate it from saved STATUS metadata."""
    _check(type(permit) is _Sample and permit.store is store and not permit.used, 'STATE_CONFLICT')
    permit.used = True
    current = validate_binding(current)
    _fields(sample, 'events dropped_events partial observed_at source_marks')
    _check(type(sample['events']) is list and len(sample['events']) <= 2 * MAX_ROWS)
    _integer(sample['dropped_events'], 0, LIMITS['dropped_max'])
    _check(type(sample['partial']) is bool)
    _integer(sample['observed_at'], permit.since_ms, now_ms)
    _marks(sample['source_marks'])
    for component, mark in sample['source_marks'].items():
        prior = permit.previous_marks.get(component)
        _check(permit.since_ms <= mark['observed_at'] <= sample['observed_at'])
        _check(prior is None or mark['observed_at'] >= prior['observed_at'], 'STATE_CONFLICT')
        _check(prior is None or mark['observed_at'] != prior['observed_at']
               or mark['signature'] == prior['signature'], 'STATE_CONFLICT')
    for event in sample['events']:
        validate_diagnostic('Event', event, now_ms=now_ms)
        mark = sample['source_marks'].get(event['component'])
        previous = permit.previous_marks.get(event['component'])
        _check(mark is not None and (previous is None or mark['signature'] != previous['signature']), 'STATE_CONFLICT')
    with store.diagnostic_state_lock():
        value = _read(store)
        _maintain(value, now_ms)
        _check(value['enabled'] and value['revision'] == permit.revision
               and value['generation'] == permit.generation and value['binding'] == current, 'STATE_CONFLICT')
        _history_space(value, now_ms, 1)
        if (value['dropped_events'] + sample['dropped_events'] + len(value['queue'])
                + len(sample['events']) + (value['pending']['event_count'] if value['pending'] else 0)
                > LIMITS['dropped_max']):
            _retire(value, now_ms, 'capacity')
            _save(store, value)
            raise DiagnosticStateError('CAPACITY')
        value['queue'].extend(copy.deepcopy(sample['events']))
        value['queue'].sort(key=lambda event: event['bucket_start'])
        _drop(value, sample['dropped_events'])
        value['partial'] = value['partial'] or sample['partial']
        value['source_marks'].update(copy.deepcopy(sample['source_marks']))
        while len(value['queue']) + (value['pending']['event_count'] if value['pending'] else 0) > MAX_ROWS:
            if value['pending']:
                _archive(value, now_ms)
            else:
                value['queue'].pop(0)
                _drop(value, 1)
        while not _fits_transitions(value):
            if value['pending']:
                _archive(value, now_ms)
            elif value['queue']:
                # Find the smallest oldest prefix to drop without serializing a
                # near-1MiB record once for every one of its 1000 event rows.
                rows, dropped, partial = value['queue'], value['dropped_events'], value['partial']
                low, high = 1, len(rows)
                while low < high:
                    middle = (low + high) // 2
                    value.update(queue=rows[middle:], dropped_events=dropped + middle, partial=True)
                    if _fits_transitions(value):
                        high = middle
                    else:
                        low = middle + 1
                value.update(queue=rows[low:], dropped_events=dropped, partial=partial)
                _drop(value, low)
            else:
                _check(False, 'CAPACITY')
        value['result'] = 'collected'
        _save(store, value)


def record_failure(store, request, *, now_ms, status=None, retry_after=0):
    """Unknown stays reserved; an observed refusal is never converted into permission."""
    with store.diagnostic_state_lock():
        value = _read(store)
        _maintain(value, now_ms)
        if not value['enabled'] or value['generation'] != request['generation']:
            return
        if type(status) is int and 400 <= status < 500 and status != 429:
            _retire(value, now_ms, 'denied')
        elif status == 429 or type(status) is int and status >= 500 or status is None:
            _integer(retry_after, 0, 86400)
            value['next_due'] = max(value['next_due'], now_ms + retry_after * 1000)
            value['result'] = 'unknown' if request['path'] == REPORT else 'backoff'
        _save(store, value)
