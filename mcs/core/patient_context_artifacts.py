"""Read sealed patient-context artifact membership without interpreting facts or performing I/O beyond the borrowed DB."""
from contextlib import closing
import re

from cloud_policy import _reservation_evidence_batch

from inference_version import CURRENT_VERSION, current_meta_pred, is_current, read_version
from mcs_util import loads_dict, payload_hash, strict_json, valid_hash

SCAN_KIND = 'patient_context_scan_v1'
ORIGINAL_KIND = 'patient_context_scan_chunk_v1'
REPAIR_KIND = 'patient_context_source_quality_repair_v1'
CANDIDATE_KIND = 'patient_context_source_quality_repair_candidate_v1'


def _invalid():
    return ValueError('patient_context_artifact_invalid')


def _key(pid, source_key):
    if type(pid) is not int or not 0 < pid < 2**63:
        raise _invalid()
    if type(source_key) is int and 0 < source_key < 2**63:
        return source_key
    if (isinstance(source_key, str) and re.fullmatch(r'karte:[1-9][0-9]{0,18}', source_key)
            and int(source_key[6:]) < 2**63):
        return source_key
    raise _invalid()


def decode_document(raw, *, metadata=False):
    if not isinstance(raw, str) or metadata and len(raw) > 32768:
        raise _invalid()
    try:
        value = strict_json(raw)
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise _invalid() from None
    if not isinstance(value, dict):
        raise _invalid()
    return value


def _occurrence(pid, value, source_binding):
    if (not isinstance(value, dict) or set(value) != {'source_binding', 'first_seen', 'scope'}
            or value['source_binding'] != source_binding or not valid_hash(source_binding)
            or value['first_seen'] is not None and type(value['first_seen']) not in (int, float)
            or not isinstance(value['scope'], dict)
            or set(value['scope']) != {'project_id', 'project_type', 'karte_id', 'is_archived'}):
        raise _invalid()
    scope = value['scope']
    if (type(scope['project_id']) is not int or scope['project_id'] != pid
            or scope['project_type'] is not None and not isinstance(scope['project_type'], str)
            or scope['karte_id'] is not None and (type(scope['karte_id']) is not int or not 0 < scope['karte_id'] < 2**63)
            or scope['is_archived'] is not None and (type(scope['is_archived']) is not int or scope['is_archived'] not in (0, 1))):
        raise _invalid()
    return value


def _rows(db, query, params):
    with closing(db.execute(query, params)) as cursor:
        names = [item[0] for item in cursor.description]
        return [dict(zip(names, row)) for row in cursor]


def _fence(db):
    return db.execute('PRAGMA data_version').fetchone()[0], db.total_changes


def _unchanged(db, before):
    if _fence(db) != before:
        raise ValueError('patient_context_artifact_changed')


def _check_metadata(db, kind, pid, *, repair=False):
    repair_check = (" OR json_type(meta,'$.repair_input_key') IS NOT 'text' "
                    "OR length(json_extract(meta,'$.repair_input_key'))!=64 "
                    "OR json_extract(meta,'$.repair_input_key') GLOB '*[^0-9a-f]*'") if repair else ''
    if db.execute("SELECT 1 FROM artifacts WHERE kind=? AND project_id=? AND CASE "
                  "WHEN typeof(meta)!='text' OR length(meta)>32768 OR NOT json_valid(meta) THEN 1 "
                  "WHEN json_type(meta)!='object' THEN 1 "
                  "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE key IS NOT NULL "
                  "GROUP BY parent,key HAVING COUNT(*)>1) THEN 1 "
                  "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE type='real' AND abs(value)>1.7976931348623157e308) THEN 1 "
                  'ELSE 0' + repair_check + ' END LIMIT 1', (kind, pid)).fetchone():
        raise _invalid()


def selected_scan(db, pid, source_key):
    """Return the newest current live SCAN for one physical source, rejecting malformed metadata."""
    source_key = _key(pid, source_key)
    before = _fence(db)
    _check_metadata(db, SCAN_KIND, pid)
    selected = None
    with closing(db.execute("SELECT artifact_id,message_id,meta FROM artifacts WHERE kind=? AND project_id=? "
            "AND CASE WHEN json_valid(meta) THEN COALESCE(json_extract(meta,'$.source_key'),message_id)=? END "
            'ORDER BY artifact_id DESC', (SCAN_KIND, pid, source_key))) as rows:
        for artifact_id, message_id, raw in rows:
            meta = decode_document(raw, metadata=True)
            if read_version(meta) is None:
                raise _invalid()
            if not is_current(meta) or meta.get('invalidated') not in (None, False, 0):
                continue
            if 'source_key' in meta and (type(meta['source_key']) is not type(source_key) or meta['source_key'] != source_key):
                raise _invalid()
            if message_id not in ((source_key,) if type(source_key) is int else (None, 0)):
                raise _invalid()
            selected = _rows(db, 'SELECT * FROM artifacts WHERE artifact_id=?', (artifact_id,))[0]
            break
    _unchanged(db, before)
    return selected


def candidate_rows(db, pid, source_key, candidate_ref, *, source_sig=None, source_binding=None,
                   source_occurrence=None, contract=None, body_size=None):
    """Return exact sealed stage rows, including empty split slots, after complete storage-membership checks."""
    source_key = _key(pid, source_key)
    if (not isinstance(candidate_ref, dict) or set(candidate_ref) != {
            'repair_artifact_id', 'repair_input_key', 'candidate_key', 'chunks_sig'}
            or type(candidate_ref['repair_artifact_id']) is not int or not 0 < candidate_ref['repair_artifact_id'] < 2**63
            or any(not valid_hash(candidate_ref[key]) for key in ('repair_input_key', 'candidate_key', 'chunks_sig'))
            or source_sig is not None and not valid_hash(source_sig)
            or source_binding is not None and not valid_hash(source_binding)
            or contract is not None and not isinstance(contract, dict)):
        raise _invalid()
    before = _fence(db)
    if body_size is None and type(source_key) is int:
        row = db.execute('SELECT length(body_text) FROM messages WHERE project_id=? AND message_id=?', (pid, source_key)).fetchone()
        body_size = row[0] if row else None
    if type(body_size) is not int or body_size <= 0:
        raise _invalid()
    _check_metadata(db, REPAIR_KIND, pid, repair=True)
    headers = _rows(db, "SELECT * FROM artifacts WHERE kind=? AND project_id=? AND json_extract(meta,'$.repair_input_key')=? "
                    'ORDER BY artifact_id LIMIT 2', (REPAIR_KIND, pid, candidate_ref['repair_input_key']))
    if len(headers) != 1 or headers[0]['artifact_id'] != candidate_ref['repair_artifact_id']:
        raise _invalid()
    header = headers[0]
    origin, seal = decode_document(header['meta'], metadata=True), decode_document(header['content'], metadata=True)
    if (not is_current(origin) or origin.get('invalidated') not in (None, False, 0)
            or type(origin.get('source_occurrence_version')) is not int or origin['source_occurrence_version'] != 1
            or type(origin.get('source_key')) is not type(source_key) or origin.get('source_key') != source_key
            or not valid_hash(origin.get('source_sig'))
            or not valid_hash(origin.get('source_binding'))
            or not isinstance(origin.get('contract'), dict)
            or source_sig is not None and origin['source_sig'] != source_sig
            or source_binding is not None and origin['source_binding'] != source_binding
            or contract is not None and origin['contract'] != contract
            or seal.get('phase') != 'ready' or any(seal.get(key) != candidate_ref[key] for key in ('candidate_key', 'chunks_sig'))):
        raise _invalid()
    saved_occurrence = _occurrence(pid, origin.get('source_occurrence'), origin['source_binding'])
    if source_occurrence is not None:
        _occurrence(pid, source_occurrence, origin['source_binding'])
    if (type(origin.get('body_length')) is not int or origin['body_length'] != body_size
            or source_occurrence is not None and payload_hash(saved_occurrence) != payload_hash(source_occurrence)):
        raise _invalid()
    rows = staged_rows(db, pid, source_key, candidate_ref['repair_input_key'],
        source_sig=origin['source_sig'], contract=origin['contract'], source_binding=origin['source_binding'],
        source_occurrence=saved_occurrence, body_size=body_size)
    if (origin.get('provider') != origin['contract'].get('provider')
            or origin.get('model') != ((origin.get('provider') or {}).get('model')
                                      if origin.get('provider') is not None else origin['contract'].get('model'))
            or payload_hash([[row['artifact_id'], row['content'], row['meta']] for row in rows]) != candidate_ref['chunks_sig']):
        raise _invalid()
    _unchanged(db, before)
    return rows


def staged_rows(db, pid, source_key, repair_input_key, *, source_sig, contract, source_binding,
                source_occurrence=None, body_size, require_complete=True):
    """Validate stored unit parts and origin; incomplete ranges are allowed only for explicit staging reads."""
    source_key = _key(pid, source_key)
    if (not valid_hash(repair_input_key) or not valid_hash(source_sig) or not valid_hash(source_binding)
            or not isinstance(contract, dict) or type(body_size) is not int or body_size <= 0
            or type(require_complete) is not bool):
        raise _invalid()
    before = _fence(db)
    provider = contract.get('provider')
    if provider is not None and not isinstance(provider, dict):
        raise _invalid()
    model = provider.get('model') if provider is not None else contract.get('model')
    expected = {'repair_input_key': repair_input_key, 'source_key': source_key, 'source_sig': source_sig,
                'source_binding': source_binding, 'contract': contract, 'provider': provider, 'model': model,
                'body_length': body_size}
    if source_occurrence is not None:
        expected['source_occurrence'] = _occurrence(pid, source_occurrence, source_binding)
    _check_metadata(db, CANDIDATE_KIND, pid, repair=True)
    rows = _rows(db, "SELECT * FROM artifacts WHERE kind=? AND project_id=? AND json_extract(meta,'$.repair_input_key')=? "
                 'ORDER BY artifact_id', (CANDIDATE_KIND, pid, repair_input_key))
    receipt_ids = [decode_document(row['meta'], metadata=True).get('reservation_artifact_id') for row in rows]
    evidence_by_id = _reservation_evidence_batch(db, receipt_ids)
    units, spans = {}, []
    for row in rows:
        meta, doc = decode_document(row['meta'], metadata=True), decode_document(row['content'])
        saved_occurrence = _occurrence(pid, meta.get('source_occurrence'), source_binding)
        if 'source_occurrence' not in expected:
            expected['source_occurrence'] = saved_occurrence
        if (row['project_id'] != pid or row['message_id'] != (source_key if type(source_key) is int else None)
                or not is_current(meta) or meta.get('invalidated') not in (None, False, 0)
                or type(meta.get('source_occurrence_version')) is not int or meta['source_occurrence_version'] != 1
                or any(payload_hash(meta.get(key)) != payload_hash(value) for key, value in expected.items())
                or row['model'] != meta.get('model') or not valid_hash(meta.get('unit_key'))
                or type(meta.get('reservation_artifact_id')) is not int or not 0 < meta['reservation_artifact_id'] < 2**63
                or not isinstance(meta.get('reservation_id'), str) or re.fullmatch('[0-9a-f]{32}', meta['reservation_id']) is None
                or not valid_hash(meta.get('request_fp')) or doc.get('phase') not in ('complete', 'split')
                or not valid_hash(doc.get('receipt_sig'))):
            raise _invalid()
        span, part, parts, facts = (doc.get(key) for key in ('chunk_span', 'part', 'parts', 'facts'))
        if (not isinstance(span, list) or len(span) != 2 or any(type(n) is not int for n in span)
                or not 0 <= span[0] < span[1] <= body_size or type(part) is not int or type(parts) is not int
                or not 0 <= part < parts <= body_size or not isinstance(facts, list)
                or doc['phase'] == 'split' and (part != 0 or parts != 1 or facts)):
            raise _invalid()
        binding = (doc['phase'], span, parts, meta['reservation_artifact_id'], meta['reservation_id'], meta['request_fp'], doc['receipt_sig'])
        unit = units.get(meta['unit_key'])
        if unit is None:
            evidence = evidence_by_id[meta['reservation_artifact_id']]
            reservation = evidence['reservation']
            if (reservation['reservation_id'] != meta['reservation_id'] or reservation['request_fp'] != meta['request_fp']
                    or reservation['route'] != 'patient_context' or reservation['project_id'] != pid
                    or reservation['message_id'] != (source_key if type(source_key) is int else None)
                    or reservation['unit_key'] != meta['unit_key'] or reservation['model'] != model
                    or reservation.get('owner') != {'repair_input_key': repair_input_key, 'unit_key': meta['unit_key']}
                    or evidence['witness'] != doc['receipt_sig']):
                raise _invalid()
            unit = units[meta['unit_key']] = {'binding': binding, 'seen': set()}
        if unit['binding'] != binding or part in unit['seen']:
            raise _invalid()
        unit['seen'].add(part)
        if doc['phase'] == 'complete' and part == 0:
            spans.append(span)
    if any(len(unit['seen']) != unit['binding'][2] for unit in units.values()):
        raise _invalid()
    stop = 0
    for start, end in sorted(spans):
        if require_complete and start > stop:
            raise _invalid()
        stop = max(stop, end)
    if require_complete and stop != body_size:
        raise _invalid()
    _unchanged(db, before)
    return rows


def _repair_trace(db, pid, source_key, source_binding, source_sig, source_occurrence):
    for kind in (REPAIR_KIND, CANDIDATE_KIND):
        _check_metadata(db, kind, pid, repair=True)
        with closing(db.execute("SELECT meta FROM artifacts WHERE kind=? AND project_id=? "
                "AND json_extract(meta,'$.source_key')=? ORDER BY artifact_id", (kind, pid, source_key))) as rows:
            for (raw,) in rows:
                meta = decode_document(raw, metadata=True)
                if not valid_hash(meta.get('source_binding')) or not valid_hash(meta.get('source_sig')):
                    raise _invalid()
                if type(meta.get('source_occurrence_version')) is not int or meta['source_occurrence_version'] != 1:
                    raise _invalid()
                if (is_current(meta) and meta.get('invalidated') in (None, False, 0)
                        and (source_binding is None or meta['source_binding'] == source_binding)
                        and (source_sig is None or meta['source_sig'] == source_sig)
                        and (source_occurrence is None or payload_hash(
                            _occurrence(pid, meta.get('source_occurrence'), meta['source_binding'])) == payload_hash(source_occurrence))):
                    return True
    return False


def original_rows(db, pid, source_key, scan_meta, scan_content, *, artifact_ids=None, body_size=None,
                  source_occurrence=None, current_only=False):
    """Read only original chunks belonging to one checkpoint occurrence, with complete new-format unit proofs."""
    source_key = _key(pid, source_key)
    if not isinstance(scan_meta, dict) or not isinstance(scan_content, dict):
        raise _invalid()
    if source_occurrence is not None:
        _occurrence(pid, source_occurrence, source_occurrence.get('source_binding') if isinstance(source_occurrence, dict) else None)
    before = _fence(db)
    marked = 'source_occurrence_version' in scan_content or 'source_occurrence' in scan_content
    if marked:
        if type(scan_content.get('source_occurrence_version')) is not int or scan_content['source_occurrence_version'] != 1:
            raise _invalid()
        occurrence = _occurrence(pid, scan_content.get('source_occurrence'),
                                 (scan_content.get('source_occurrence') or {}).get('source_binding')
                                 if isinstance(scan_content.get('source_occurrence'), dict) else None)
        if (source_occurrence is not None and payload_hash(source_occurrence) != payload_hash(occurrence)
                or 'complete' in scan_content and type(scan_content['complete']) is not bool
                or not valid_hash(scan_meta.get('source_sig')) or not isinstance(scan_meta.get('contract'), dict)):
            raise _invalid()
    else:
        occurrence = None
    if type(source_key) is int:
        condition = ("(project_id=? OR project_id IS NULL) AND (message_id=? OR (project_id=? "
                     "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.source_key')=? END))")
        params = (pid, source_key, pid, source_key)
    else:
        condition = "project_id=? AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.source_key')=? END"
        params = (pid, source_key)
    if current_only:
        condition = "project_id=? AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.source_key')=? END AND " + current_meta_pred("meta", target=CURRENT_VERSION)
        params = (pid, source_key)
    ids = []
    with closing(db.execute('SELECT artifact_id,project_id,message_id,meta FROM artifacts WHERE kind=? AND '
                            + condition + ' ORDER BY artifact_id', (ORIGINAL_KIND, *params))) as cursor:
        for artifact_id, project_id, message_id, raw in cursor:
            meta = loads_dict(raw)
            if meta is None:
                raise _invalid()
            new = 'source_occurrence_version' in meta or 'source_occurrence' in meta
            if new:
                meta = decode_document(raw, metadata=True)
                if type(meta.get('source_occurrence_version')) is not int or meta['source_occurrence_version'] != 1:
                    raise _invalid()
                member_occurrence = _occurrence(pid, meta.get('source_occurrence'), meta.get('source_binding'))
                if not is_current(meta) or meta.get('invalidated') not in (None, False, 0):
                    continue
                if not marked:
                    if source_occurrence is None or payload_hash(member_occurrence) == payload_hash(source_occurrence):
                        raise _invalid()
                    continue
                if (payload_hash(member_occurrence) != payload_hash(occurrence)
                        or meta.get('source_sig') != scan_meta['source_sig']
                        or payload_hash(meta.get('contract')) != payload_hash(scan_meta['contract'])
                        or payload_hash(meta.get('provider')) != payload_hash(scan_meta.get('provider'))):
                    continue
                if (project_id != pid or message_id != (source_key if type(source_key) is int else 0)
                        and not (type(source_key) is str and message_id is None)
                        or type(meta.get('source_key')) is not type(source_key) or meta['source_key'] != source_key):
                    raise _invalid()
            elif marked:
                continue
            ids.append(artifact_id)
    selected = None if artifact_ids is None else set(artifact_ids)
    if selected is not None and any(type(artifact_id) is not int or not 0 < artifact_id < 2**63 for artifact_id in selected):
        raise _invalid()
    if not marked and selected is not None:
        ids = [artifact_id for artifact_id in ids if artifact_id in selected]
    import json
    rows = _rows(db, 'SELECT * FROM artifacts WHERE artifact_id IN (SELECT value FROM json_each(?)) ORDER BY artifact_id',
                 (json.dumps(ids),))
    if marked:
        if body_size is None and type(source_key) is int:
            found = db.execute('SELECT length(body_text) FROM messages WHERE project_id=? AND message_id=?', (pid, source_key)).fetchone()
            body_size = found[0] if found else None
        if type(body_size) is not int or body_size <= 0:
            raise _invalid()
        units, spans = {}, []
        for row in rows:
            meta, doc = decode_document(row['meta'], metadata=True), decode_document(row['content'])
            span, part, count, facts = (doc.get(key) for key in ('chunk_span', 'part', 'parts', 'facts'))
            if (not valid_hash(meta.get('unit_key')) or not valid_hash(meta.get('chunk_key'))
                    or not isinstance(span, list) or len(span) != 2 or any(type(n) is not int for n in span)
                    or not 0 <= span[0] < span[1] <= body_size or type(part) is not int or type(count) is not int
                    or not 0 <= part < count <= body_size or not isinstance(facts, list)):
                raise _invalid()
            unit = units.setdefault(meta['unit_key'], {'span': span, 'count': count, 'seen': set()})
            if unit['span'] != span or unit['count'] != count or part in unit['seen']:
                raise _invalid()
            unit['seen'].add(part)
            if part == 0:
                spans.append(span)
        if any(len(unit['seen']) != unit['count'] for unit in units.values()):
            raise _invalid()
        stop = 0
        for start, end in sorted(spans):
            if scan_content.get('complete') is True and start > stop:
                raise _invalid()
            stop = max(stop, end)
        if scan_content.get('complete') is True and stop != body_size:
            raise _invalid()
        if selected is not None:
            rows = [row for row in rows if row['artifact_id'] in selected]
    _unchanged(db, before)
    return rows


def selected_rows(db, pid, source_key, *, artifact_ids=None, source_binding=None,
                  source_occurrence=None, body_size=None):
    """Select the accepted candidate or original chunks; an invalid accepted reference never falls back."""
    source_key = _key(pid, source_key)
    if source_occurrence is not None:
        _occurrence(pid, source_occurrence, source_occurrence.get('source_binding') if isinstance(source_occurrence, dict) else None)
        if source_binding is not None and source_occurrence['source_binding'] != source_binding:
            raise _invalid()
    before = _fence(db)
    selected = None if artifact_ids is None else set(artifact_ids)
    if selected is not None and any(type(artifact_id) is not int or not 0 < artifact_id < 2**63 for artifact_id in selected):
        raise _invalid()
    scan = selected_scan(db, pid, source_key)
    if scan is not None and decode_document(scan['content']).get('accepted_candidate_ref') is not None:
        meta, doc = decode_document(scan['meta'], metadata=True), decode_document(scan['content'])
        if not valid_hash(meta.get('source_sig')) or not isinstance(meta.get('contract'), dict):
            raise _invalid()
        if type(meta.get('source_key')) is not type(source_key) or meta['source_key'] != source_key:
            raise _invalid()
        occurrence = doc.get('source_occurrence')
        if (type(doc.get('source_occurrence_version')) is not int or doc['source_occurrence_version'] != 1
                or occurrence is None or not isinstance(occurrence, dict)):
            raise _invalid()
        _occurrence(pid, occurrence, occurrence.get('source_binding'))
        rows = candidate_rows(db, pid, source_key, doc['accepted_candidate_ref'], source_sig=meta.get('source_sig'),
                              source_binding=occurrence['source_binding'], source_occurrence=occurrence,
                              contract=meta.get('contract'), body_size=body_size)
        if (source_binding is not None and occurrence['source_binding'] != source_binding
                or source_occurrence is not None and payload_hash(occurrence) != payload_hash(source_occurrence)):
            raise _invalid()
        rows = rows if selected is None else [row for row in rows if row['artifact_id'] in selected]
    else:
        source_sig = decode_document(scan['meta'], metadata=True).get('source_sig') if scan is not None else None
        if _repair_trace(db, pid, source_key, source_binding, source_sig if valid_hash(source_sig) else None, source_occurrence):
            raise _invalid()
        rows = original_rows(db, pid, source_key, decode_document(scan['meta'], metadata=True) if scan is not None else {},
                             decode_document(scan['content']) if scan is not None else {}, artifact_ids=selected,
                             body_size=body_size, source_occurrence=source_occurrence)
    _unchanged(db, before)
    return rows


def original_quality_rows(db, pid, source_key, scan, source_sig, contract, occurrence, body_size):
    """Select a complete original scan proof; malformed membership is never silently accepted."""
    from patient_context_plan import MAX_SOURCE_FACTS, ranges_complete, source_digest
    if (not db.in_transaction or not isinstance(scan, dict) or not valid_hash(source_sig)
            or not isinstance(contract, dict) or type(body_size) is not int or body_size <= 0):
        raise _invalid()
    meta = decode_document(scan['meta'], metadata=True)
    content = decode_document(scan['content'])
    rows = original_rows(db, pid, source_key, meta, content, body_size=body_size,
                         source_occurrence=occurrence, current_only=True)
    proofs, units, selected = [], {}, []
    for row in rows:
        origin, doc = decode_document(row['meta'], metadata=True), decode_document(row['content'])
        actual = origin.get('contract')
        route = origin.get('route_contract')
        if route is not None:
            if (not isinstance(route, dict) or not isinstance(actual, dict) or 'provider' not in route
                    or actual != {k:v for k,v in route.items() if k != 'provider'}
                    or origin.get('provider') or origin.get('model') != actual.get('model')):
                continue
            actual = route
        elif not isinstance(actual, dict) or origin.get('provider') != actual.get('provider'):
            continue
        if (origin.get('invalidated') not in (None,False,0) or origin.get('source_key') != source_key
                or origin.get('source_sig') != source_sig or actual != contract
                or {k:origin.get(k) for k in ('contract','provider')} != {k:meta.get(k) for k in ('contract','provider')}):
            continue
        marked = 'source_occurrence_version' in content or 'source_occurrence' in content
        candidate = 'source_occurrence_version' in origin or 'source_occurrence' in origin
        if (marked != candidate or marked and (origin.get('source_occurrence_version') != 1
                or payload_hash(origin.get('source_occurrence')) != payload_hash(content.get('source_occurrence')))):
            continue
        span, part, count, facts = (doc.get(k) for k in ('chunk_span','part','parts','facts'))
        unit_key = origin.get('unit_key')
        if (not isinstance(unit_key, str) or not isinstance(facts, list) or len(facts) > MAX_SOURCE_FACTS
                or type(part) is not int or type(count) is not int or not 0 <= part < count <= max(1,body_size)):
            raise _invalid()
        unit = units.setdefault(unit_key, {'span':span, 'count':count, 'seen':set()})
        if unit['span'] != span or unit['count'] != count or part in unit['seen']:
            raise _invalid()
        unit['seen'].add(part)
        proofs.append(source_digest([origin.get('chunk_key'), row['content']]))
        selected.append(row)
    if (any(len(unit['seen']) != unit['count'] for unit in units.values())
            or not ranges_complete([unit['span'] for unit in units.values()], body_size)
            or source_digest(sorted(proofs)) != content.get('audited_chunks_sig')):
        raise _invalid()
    return selected
