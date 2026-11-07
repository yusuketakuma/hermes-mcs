"""Search patient originals in bounded pages and reopen exact source occurrences."""
from datetime import datetime, timedelta
from contextlib import contextmanager
import time

import attachment_ocr
import inference_dependency_loss
import original_source_keys
from mcs_queries import JST, source_patient_context, attachment_context_items
from mcs_util import payload_hash, positive, search_fold, valid_hash
from patient_context import LABELS
from patient_read_scope import patient_scope_valid

PAGE_SIZE = 5
SCAN_LIMIT = 64
READ_SECONDS = 1.0
SOURCE_PAGE_CHARS = 4000
UNKNOWN_TIME = -(2**63)
_FILTERS = {'資料': 'source', '分類': 'category', '開始': 'after', '終了': 'before'}
_SOURCES = {'全て': 'all', '投稿': 'chat', '添付': 'ocr', 'all': 'all', 'chat': 'chat', 'ocr': 'ocr'}


def parse_query(query):
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise ValueError('bad_query')
    terms, filters, seen = [], {'source': 'all'}, set()
    for word in query.split():
        key, separator, value = word.partition(':')
        key = _FILTERS.get(key, key)
        if separator and key in ('source', 'category', 'after', 'before'):
            if key in seen:
                raise ValueError('bad_search_filter')
            seen.add(key)
            if key == 'source':
                value = _SOURCES.get(value)
                if value is None:
                    raise ValueError('bad_search_filter')
            elif key == 'category':
                value = next((k for k, labels in LABELS.items() if value in (k, *labels)), None)
                if value is None:
                    raise ValueError('bad_search_filter')
            else:
                try:
                    date = datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=JST)
                    if date.strftime('%Y-%m-%d') != value:
                        raise ValueError
                    value = int((date + (timedelta(days=1) if key == 'before' else timedelta())).timestamp())
                except (ValueError, OverflowError):
                    raise ValueError('bad_search_filter') from None
            filters[key] = value
        elif search_fold(word):
            terms.append(search_fold(word))
    if not terms or filters.get('after', UNKNOWN_TIME) >= filters.get('before', 2**63 - 1):
        raise ValueError('bad_query')
    return terms, filters


def _position(cursor, binding):
    if cursor is None:
        return None
    if (not isinstance(cursor, dict) or set(cursor) != {'binding', 'position'}
            or cursor['binding'] != binding or not isinstance(cursor['position'], list)
            or len(cursor['position']) != 4
            or any(type(v) is not int or not -(2**63) <= v < 2**63 for v in cursor['position'])
            or cursor['position'][1] <= 0 or cursor['position'][2] < 0
            or not 0 <= cursor['position'][3] <= attachment_ocr.MAX_PAGES):
        raise ValueError('search_cursor_invalid')
    return tuple(cursor['position'])


def _record(db, pid, key, cfg):
    if type(key) is int and 0 < key < 2**63:
        row = db.execute('SELECT * FROM messages WHERE project_id=? AND message_id=? '
                         "AND body_state='full'", (pid, key)).fetchone()
        if row is None or not isinstance(row['body_text'], str):
            return None
        result = dict(row)
        result.update(source='chat', source_key=key,
                      _source_binding=inference_dependency_loss.source_binding(db, pid, key))
        return result
    if (isinstance(key, str) and original_source_keys.valid(key)
            and original_source_keys.parse(key)[0] == 'attachment_ocr'
            and isinstance(cfg.get('attachment_ocr'), dict)
            and cfg['attachment_ocr'].get('enabled') is True and not attachment_ocr.config_error(cfg)):
        record = original_source_keys.source(db, pid, key)
        if record is not None:
            parent = db.execute('SELECT posted_at,profession FROM messages WHERE project_id=? AND message_id=?',
                                (pid, record['parent_message_id'])).fetchone()
            record.update(posted_at=parent['posted_at'], profession=parent['profession'])
        return record
    return None


def _facts(db, record, cfg):
    if record['source'] == 'chat':
        return source_patient_context(db, record)
    import attachment_extraction
    current = attachment_extraction.current(db, record['project_id'], record['source_key'], cfg=cfg)
    if not current or current.get('ready') is not True:
        return []
    return attachment_context_items(current['document'], record)


def _candidates(db, pid, filters, boundary):
    args = [pid]
    where = "m.project_id=? AND m.body_state IS NOT 'deleted'"
    for key, operator in (('after', '>='), ('before', '<')):
        if key in filters:
            where += f' AND m.posted_at_ts{operator}?'
            args.append(filters[key])
    if boundary is not None:
        operator = '<' if boundary[2] == 2**63 - 1 else '<='
        if boundary[0] == UNKNOWN_TIME:
            where += f' AND m.posted_at_ts IS NULL AND m.message_id{operator}?'
            args.append(boundary[1])
        else:
            where += f' AND ((m.posted_at_ts,m.message_id){operator}(?,?) OR m.posted_at_ts IS NULL)'
            args.extend(boundary[:2])
    # LIMIT bounds candidates, not matches: continuation can advance even through an empty page.
    return db.execute('SELECT m.message_id,COALESCE(m.posted_at_ts,?) AS stamp,m.posted_at,m.profession '
        f'FROM messages m WHERE {where} ORDER BY m.posted_at_ts DESC,m.message_id DESC LIMIT ?',
        (UNKNOWN_TIME, *args, SCAN_LIMIT + 1))


@contextmanager
def _snapshot(db):
    before = db.total_changes
    db.execute('SAVEPOINT patient_search_read')
    try:
        yield
        if db.total_changes != before:
            raise RuntimeError('patient_search_read_changed')
    except BaseException:
        db.execute('ROLLBACK TO patient_search_read')
        db.execute('RELEASE patient_search_read')
        raise
    db.execute('RELEASE patient_search_read')


def search(db, pid, query, *, cfg=None, cursor=None, limit=PAGE_SIZE):
    parse_query(query)
    with _snapshot(db):
        return _search(db, pid, query, cfg=cfg, cursor=cursor, limit=limit)


def _search(db, pid, query, *, cfg=None, cursor=None, limit=PAGE_SIZE):
    """Read a resumable range; no inference, exact total scan or durable search index."""
    terms, filters = parse_query(query)
    if not positive(pid) or type(limit) is not int or not 1 <= limit <= PAGE_SIZE:
        raise ValueError('bad_search_scope')
    cfg = cfg or {}
    binding = payload_hash([pid, terms, filters])
    boundary = _position(cursor, binding)
    if not patient_scope_valid(db, pid):
        raise ValueError('patient_scope_unavailable')
    deadline = time.monotonic() + READ_SECONDS
    items, scanned, pending, unavailable, last, limited = [], 0, 0, 0, boundary, False
    ocr_enabled = (not attachment_ocr.config_error(cfg)
                   and isinstance(cfg.get('attachment_ocr'), dict)
                   and cfg['attachment_ocr'].get('enabled') is True)

    def exhausted():
        return scanned >= SCAN_LIMIT or scanned > 0 and time.monotonic() >= deadline

    def accept(key, position, row):
        nonlocal scanned, unavailable, last
        if exhausted() or len(items) >= limit:
            return False
        scanned += 1
        record = _record(db, pid, key, cfg)
        last = position
        if record is None:
            unavailable += 1
            return True
        if not all(term in search_fold(record['body_text']) for term in terms):
            return True
        facts = _facts(db, record, cfg)
        if filters.get('category') and not any(f.get('category') == filters['category'] for f in facts):
            return True
        items.append({'source': record['source'], 'message_id': row['message_id'],
                      'posted_at': row['posted_at'], 'profession': row['profession'],
                      'page': record.get('page'), 'body_text': record['body_text'],
                      'original_available': record.get('original_available', True),
                      'facts': facts, 'source_ref': {'key': key, 'binding': record['_source_binding']}})
        return True

    for index, row in enumerate(_candidates(db, pid, filters, boundary)):
        if index >= SCAN_LIMIT or exhausted() or len(items) >= limit:
            limited = True
            break
        stamp, mid = row['stamp'], row['message_id']
        same = boundary is not None and (stamp, mid) == boundary[:2]
        if not same and filters['source'] != 'ocr':
            if not accept(mid, (stamp, mid, 0, 0), row):
                limited = True
                break
        if filters['source'] != 'chat' and ocr_enabled:
            rows = db.execute('SELECT attachment_id FROM attachments WHERE message_id=? '
                              "AND state IN ('downloaded','pruned') AND attachment_id>=? "
                              'ORDER BY attachment_id LIMIT ?',
                              (mid, boundary[2] if same else 0, SCAN_LIMIT + 1))
            for attachment in rows:
                aid = attachment[0]
                if same and aid == boundary[2] and boundary[3] == attachment_ocr.MAX_PAGES:
                    continue
                if exhausted() or len(items) >= limit:
                    limited = True
                    break
                document = attachment_ocr.current(db, aid, project_id=pid)
                if document is None:
                    scanned += 1
                    pending += 1
                    last = (stamp, mid, aid, attachment_ocr.MAX_PAGES)
                    continue
                for page in document['pages']:
                    position = (stamp, mid, aid, page['page'])
                    if same and position[2:] <= boundary[2:]:
                        continue
                    key = f"attachment_ocr:{document['artifact_id']}:{page['page']}"
                    if not accept(key, position, row):
                        limited = True
                        break
                if limited:
                    break
        if limited:
            break
        # An OCR-only search also advances over messages without attachments.
        last = (stamp, mid, 2**63 - 1, attachment_ocr.MAX_PAGES)
        if filters['source'] == 'ocr' and not ocr_enabled:
            scanned += 1
    return {'items': items, 'scanned': scanned, 'pending': pending, 'unavailable': unavailable,
            'range_complete': not limited, 'limited': limited,
            'ocr_available': ocr_enabled, 'filters': filters,
            'next_cursor': {'binding': binding, 'position': list(last)} if limited and last else None}


def read_source(db, pid, reference, *, cfg=None):
    """Reauthorize a private search result and reject a changed source occurrence."""
    if (not isinstance(reference, dict) or set(reference) != {'key', 'binding'}
            or not valid_hash(reference['binding']) or not positive(pid)):
        raise ValueError('search_source_invalid')
    with _snapshot(db):
        if not patient_scope_valid(db, pid):
            raise ValueError('patient_scope_unavailable')
        record = _record(db, pid, reference['key'], cfg or {})
        if record is None or record['_source_binding'] != reference['binding']:
            raise ValueError('search_source_changed')
        return record
