"""Read bounded, source-verified OCR report pages with bidirectional physical cursors."""
import json
import time

import attachment_extraction
import attachment_ocr
from mcs_util import positive
from patient_read_scope import patient_scope_valid

SCAN_LIMIT = 64
READ_SECONDS = 1.0


def _cursor(value, policy=None):
    if value is None:
        return 'after', None
    if (not isinstance(value, dict) or set(value) != ({'direction', 'attachment_id', 'page'} | ({'policy_binding'} if policy else set()))
            or policy is not None and value.get('policy_binding') != policy
            or value['direction'] not in ('after', 'before')
            or not positive(value['attachment_id']) or value['attachment_id'] >= 2**63
            or type(value['page']) is not int or not 0 <= value['page'] <= attachment_ocr.MAX_PAGES):
        raise ValueError('attachment_report_cursor_invalid')
    return value['direction'], (value['attachment_id'], value['page'])


def _pin(direction, position, policy=None):
    return {'direction': direction, 'attachment_id': position[0], 'page': position[1], **({'policy_binding': policy} if policy else {})}


def _read(db, pid, mids, cfg, cursor, limit):
    policy = attachment_ocr.rejection_policy(cfg)
    direction, boundary = _cursor(cursor, policy)
    result = {'sources': [], 'prev_cursor': None, 'next_cursor': None,
              'scanned': 0, 'pending': 0, 'excluded': 0, 'limited': False,
              'state': 'unknown', 'range_complete': False, 'excluded_documents': []}
    if policy:
        result.update(policy_binding=policy, policy_rejected=0, clinical_review='not_proven')
    if (attachment_ocr.config_error(cfg)
            or not cfg.get('attachment_ocr', {}).get('enabled', False)
            or not patient_scope_valid(db, pid)):
        return result
    scope = (pid, json.dumps(mids))
    if db.execute("SELECT COUNT(*) FROM messages WHERE project_id=? AND message_id IN "
            "(SELECT value FROM json_each(?)) AND body_state IS NOT 'deleted'", scope).fetchone()[0] != len(mids):
        raise ValueError('attachment_report_scope_invalid')
    if boundary and db.execute('SELECT 1 FROM attachments a JOIN messages m USING(message_id) '
            'WHERE a.attachment_id=? AND m.project_id=? AND m.message_id IN '
            '(SELECT value FROM json_each(?))', (boundary[0], *scope)).fetchone() is None:
        raise ValueError('attachment_report_cursor_invalid')
    ascending = direction == 'after'
    query = ('SELECT a.attachment_id FROM attachments a JOIN messages m USING(message_id) '
             "WHERE m.project_id=? AND m.body_state IS NOT 'deleted' AND a.state IN ('downloaded','pruned') "
             'AND m.message_id IN (SELECT value FROM json_each(?)) ')
    params = list(scope)
    if boundary:
        inclusive = boundary[1] < attachment_ocr.MAX_PAGES if ascending else boundary[1] > 0
        operator = ('>=' if inclusive else '>') if ascending else ('<=' if inclusive else '<')
        query += f'AND a.attachment_id{operator}? '
        params.append(boundary[0])
    query += 'ORDER BY a.attachment_id ' + ('ASC' if ascending else 'DESC') + ' LIMIT ?'
    params.append(SCAN_LIMIT + 1)
    rows = db.execute(query, params).fetchall()
    deadline = time.monotonic() + READ_SECONDS
    result['state'] = 'observed'
    selected, first_seen, last_seen, more = [], None, None, False
    displayed = []

    def exhausted():
        return (result['scanned'] >= SCAN_LIMIT
                or result['scanned'] > 0 and time.monotonic() >= deadline)

    for row in rows:
        if exhausted():
            result['limited'] = more = True
            break
        aid = row['attachment_id']
        document = attachment_ocr.current(db, aid, project_id=pid)
        if document is None or not document['pages']:
            position = (aid, attachment_ocr.MAX_PAGES if ascending else 0)
            status = attachment_ocr.view(db, aid, project_id=pid, cfg=cfg)
            state = status['state']
            if state == 'rejected_by_policy':
                position = (aid, attachment_ocr.MAX_PAGES)
                if boundary is not None and (position <= boundary if ascending else position >= boundary):
                    result['scanned'] += 1
                    first_seen, last_seen = first_seen or position, position
                    continue
                if len(selected) + len(result['excluded_documents']) == limit:
                    more = True
                    break
                result['excluded_documents'].append(status)
                result['policy_rejected'] += 1
                displayed.append(position)
            result['scanned'] += 1
            result['excluded' if state in attachment_ocr.EXCLUDED_STATES else 'pending'] += 1
            first_seen, last_seen = first_seen or position, position
            continue
        pages = [page for page in document['pages'] if boundary is None
                 or ((aid, page['page']) > boundary if ascending else (aid, page['page']) < boundary)]
        if not pages:
            # Inclusive boundary rows still consume the SQL window and must advance past their attachment.
            result['scanned'] += 1
            position = (aid, attachment_ocr.MAX_PAGES if ascending else 0)
            first_seen, last_seen = first_seen or position, position
            continue
        if not ascending:
            pages.reverse()
        for page in pages:
            position = (aid, page['page'])
            if exhausted():
                result['limited'] = more = True
                break
            result['scanned'] += 1
            if page['text'].strip():
                key = f"attachment_ocr:{document['artifact_id']}:{page['page']}"
                source = attachment_extraction.source(db, pid, key, cfg=cfg)
                if source is None:
                    result['pending'] += 1
                elif len(selected) + len(result['excluded_documents']) == limit:
                    more = True
                    break  # The lookahead remains beyond the next token's displayed boundary.
                else:
                    selected.append(source)
                    displayed.append(position)
            first_seen, last_seen = first_seen or position, position
        if more:
            break
    sources = selected if ascending else list(reversed(selected))
    result['sources'] = sources
    if not ascending:
        result['excluded_documents'].reverse()
    first = min(displayed) if displayed else (first_seen if ascending else last_seen) or boundary
    last = max(displayed) if displayed else (last_seen if ascending else first_seen) or boundary
    if first and (boundary is not None if ascending else more):
        result['prev_cursor'] = _pin('before', first, policy)
    if last and (more if ascending else boundary is not None):
        result['next_cursor'] = _pin('after', last, policy)
    result['range_complete'] = not (more or result['pending'] or result['limited'])
    return result


def read_page(db, project_id, message_ids, *, cfg, cursor=None, limit=5):
    """Borrow one snapshot and inspect only pages beyond a server-pinned physical boundary."""
    if (not positive(project_id) or project_id >= 2**63 or not isinstance(message_ids, (list, tuple))
            or not 0 < len(message_ids) <= 64 or any(not positive(mid) or mid >= 2**63 for mid in message_ids)
            or len(set(message_ids)) != len(message_ids) or type(limit) is not int or not 1 <= limit <= 5):
        raise ValueError('attachment_report_scope_invalid')
    before = db.total_changes
    db.execute('SAVEPOINT attachment_report_read')
    try:
        result = _read(db, project_id, list(message_ids), cfg, cursor, limit)
        if db.total_changes != before:
            raise RuntimeError('attachment_report_read_changed')
    except BaseException:
        db.execute('ROLLBACK TO attachment_report_read')
        db.execute('RELEASE attachment_report_read')
        raise
    db.execute('RELEASE attachment_report_read')
    return result
