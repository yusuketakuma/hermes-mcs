"""Retain compact negative-use dependencies before source or derivative destruction."""
import hashlib
import json
import sqlite3
import time
from datetime import date
import re

from inference_version import CURRENT_VERSION
from mcs_util import loads_dict

KIND = 'inference_dependency_loss'
SCHEMA = 'dependency-loss/v1'
_SOURCE_FIELDS = ('content_hash', 'body_text', 'body_state', 'parent_id', 'sender_id', 'sender_name',
                  'sender_type', 'profession', 'organization', 'posted_at', 'posted_at_ts')
SOURCE_BINDING_COLUMNS = (','.join('m.' + field for field in _SOURCE_FIELDS)
                          + ',(SELECT COALESCE(MAX(seq),1) FROM message_revisions '
                            'WHERE message_id=m.message_id) AS _source_revision')
_SOURCE_KINDS = ('extract_v1', 'extract_llm', 'canonical_projection', 'semantic_facts_v4',
                 'patient_context_scan_chunk_v1')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _active(db):
    if not db.in_transaction:
        raise ValueError('dependency_loss_transaction_required')



def _karte_id(source_key):
    if not isinstance(source_key, str) or re.fullmatch(r'karte:[1-9][0-9]{0,18}', source_key) is None:
        raise ValueError('dependency_loss_source_key_invalid')
    raw_id = int(source_key.split(':')[1])
    if raw_id >= 2**63:
        raise ValueError('dependency_loss_source_key_invalid')
    return raw_id

def source_binding_from_row(row):
    """Bind selected message fields and revision read in the same snapshot."""
    try:
        values = [row[field] for field in _SOURCE_FIELDS]
        revision = row['_source_revision']
    except (KeyError, IndexError, TypeError):
        return None
    if type(revision) is not int:
        return None
    return _digest([values, revision])


def source_binding(db, pid, mid, *, source_key=None):
    """Read the occurrence fence, distinct from body hash (including observed ABA)."""
    if source_key is not None:
        raw_id = _karte_id(source_key)
        raw = db.execute("SELECT content,meta FROM artifacts WHERE kind='karte_summary' AND project_id=? AND artifact_id=?",
                         (pid, raw_id)).fetchone()
        if raw is None:
            return None
        return _digest(['karte-source/v1', pid, raw_id, _digest(loads_dict(raw['content']) or {}),
                        (loads_dict(raw['meta']) or {}).get('karte_id')])
    row = db.execute('SELECT ' + ','.join(_SOURCE_FIELDS) + ' FROM messages WHERE project_id=? AND message_id=?',
                     (pid, mid)).fetchone()
    if row is None:
        return None
    revision = db.execute('SELECT COALESCE(MAX(seq),1) FROM message_revisions WHERE message_id=?', (mid,)).fetchone()[0]
    return _digest([list(row), revision])


def fatal_read_error(error):
    """Do not replay cancelled, locked or resource-failed SQLite reads."""
    if not isinstance(error, sqlite3.Error):
        return False
    code = getattr(error, 'sqlite_errorcode', None)
    if type(code) is not int:
        return True
    # Only a classified generic SQL error can enter the ordered fallback.
    return code & 0xff != sqlite3.SQLITE_ERROR



def source_bindings(db, pairs):
    """Read up to 64 ordered message occurrence fences on one borrowed snapshot.

    Inputs must be a materialized list or tuple of exact integer pairs. The
    returned list follows the input order, including duplicates and missing
    or out-of-scope messages. Karte sources remain a separate single read.
    """
    if type(pairs) not in (list, tuple):
        raise TypeError('dependency_loss_batch_pairs_invalid')
    if len(pairs) > 64:
        raise ValueError('dependency_loss_batch_limit')
    prepared = []
    for pair in pairs:
        if (type(pair) not in (list, tuple) or len(pair) != 2
                or any(type(value) is not int or not -(2**63) <= value < 2**63 for value in pair)):
            raise TypeError('dependency_loss_batch_pair_invalid')
        prepared.append((pair[0], pair[1]))
    if not prepared:
        return []
    db.execute('SAVEPOINT source_bindings_read')
    try:
        try:
            result = _source_bindings_page(db, prepared)
        except Exception as error:
            if fatal_read_error(error):
                raise
            # Replay singles in order if a grouped query fails on a later row.
            result = [source_binding(db, pid, mid) for pid, mid in prepared]
    except BaseException:
        try:
            db.execute('RELEASE source_bindings_read')
        except sqlite3.Error:
            pass  # SQLite may already have rolled back the savepoint.
        raise
    else:
        db.execute('RELEASE source_bindings_read')
        return result


def _source_bindings_page(db, pairs):
    unique = list(dict.fromkeys(pairs))
    where = ' OR '.join('(m.project_id=? AND m.message_id=?)' for _ in unique)
    params = [part for pair in unique for part in pair]
    columns = ','.join('m.' + field for field in _SOURCE_FIELDS)
    rows = {(row[0], row[1]): list(row[2:]) for row in db.execute(
        'SELECT m.project_id,m.message_id,' + columns + ' FROM messages m WHERE ' + where, params)}
    if not rows:
        return [None] * len(pairs)
    mids = sorted({mid for _, mid in rows})
    marks = ','.join('?' for _ in mids)
    revisions = {row[0]: row[1] for row in db.execute(
        'SELECT message_id,COALESCE(MAX(seq),1) FROM message_revisions '
        'WHERE message_id IN (' + marks + ') GROUP BY message_id', mids)}
    return [_digest([rows[pair], revisions.get(pair[1], 1)]) if pair in rows else None for pair in pairs]


def support_binding(db, pid, mid):
    """Identify clinical support independently of display and equivalent timestamp formatting."""
    source = db.execute('SELECT body_text,body_state,parent_id,posted_at_ts '
                        'FROM messages WHERE project_id=? AND message_id=?', (pid, mid)).fetchone()
    if source is None:
        return None
    body, state, parent, posted = source
    return _digest([body, state or ('full' if body else 'unknown'), parent, posted])


def equivalent_tx(db, pid, mid, origin):
    """Record verified unchanged support, without asserting an audit of absent historical text."""
    return record_tx(db, {'project_id': pid, 'message_id': mid, 'records': [], 'gaps': [],
        'observed': [{**origin, 'kind': 'equivalent_support', 'artifact_id': None}]})


def bind_facts(db, facts):
    """Attest a source set once on a borrowed snapshot; retain every unbound candidate unchanged."""
    from mcs_queries import karte_source_record
    import patient_context_state as state
    facts = list(facts)
    mids = {fact.get('message_id') for fact in facts if type(fact.get('message_id')) is int and fact['message_id'] > 0}
    karte_ids = {_karte_id(fact['source_key']) for fact in facts
                 if isinstance(fact.get('source_key'), str) and fact['source_key'].startswith('karte:')}
    sources = {}
    message_columns = {row[1] for row in db.execute('PRAGMA table_info(messages)')} if mids else set()
    revision_columns = {row[1] for row in db.execute('PRAGMA table_info(message_revisions)')} if mids else set()
    if (mids and {'message_id', 'project_id', 'content_hash', 'body_text', 'body_state', 'posted_at_ts',
            'parent_id', 'sender_id', 'sender_name', 'sender_type', 'profession', 'organization', 'posted_at'}.issubset(message_columns)
            and {'message_id', 'seq'}.issubset(revision_columns)):
        revisions = {row[0]: row[1] for row in db.execute('SELECT message_id,MAX(seq) FROM message_revisions '
            'WHERE message_id IN (SELECT value FROM json_each(?)) GROUP BY message_id', (json.dumps(sorted(mids)),))}
        for row in db.execute('SELECT * FROM messages WHERE message_id IN (SELECT value FROM json_each(?))', (json.dumps(sorted(mids)),)):
            source = dict(row)
            revision = revisions.get(source['message_id'], 1)
            binding = _digest([[source.get(field) for field in _SOURCE_FIELDS], revision])
            sources[(source['project_id'], source['message_id'])] = (source, revision, binding)
    if karte_ids:
        for row in db.execute("SELECT * FROM artifacts WHERE kind='karte_summary' AND artifact_id IN "
                              '(SELECT value FROM json_each(?))', (json.dumps(sorted(karte_ids)),)):
            source = karte_source_record(row, row['project_id'])
            binding = _digest(['karte-source/v1', row['project_id'], row['artifact_id'], source['content_hash'],
                              (loads_dict(row['meta']) or {}).get('karte_id')])
            sources[(row['project_id'], source['source_key'])] = (source, row['artifact_id'], binding)
    bound = []
    for original in facts:
        fact = {key: value for key, value in original.items() if key != 'dependency_origin'}
        key = fact.get('source_key', fact.get('message_id'))
        found = sources.get((fact.get('project_id'), key))
        if found is not None:
            source, revision, binding = found
            span, body = fact.get('evidence_span'), source.get('body_text') or ''
            if (source.get('body_state') in (None, 'full') and isinstance(fact.get('content_hash'), str)
                    and re.fullmatch('[0-9a-f]{64}', fact['content_hash']) is not None
                    and fact.get('content_hash') == source.get('content_hash')
                    and fact.get('posted_at_ts') == source.get('posted_at_ts')
                    and isinstance(span, list) and len(span) == 2 and all(type(n) is int for n in span)
                    and 0 <= span[0] < span[1] <= len(body) and body[span[0]:span[1]] == fact.get('evidence')
                    and all(isinstance(detail, dict) and isinstance(detail.get('value'), str)
                            and detail['value'] in fact['evidence'] for detail in fact.get('details', []))
                    and state.annotate(fact, body) == fact.get('temporal') and not fact['temporal'].get('unknown')):
                fact['dependency_origin'] = {'source_key': key, 'content_hash': source['content_hash'],
                    'revision': revision, 'binding': binding, 'grounded': True}
        bound.append(fact)
    return bound

def _facts(source, artifact):
    import patient_context_state as state
    meta, doc = loads_dict(artifact['meta']), loads_dict(artifact['content'])
    if (meta is None or doc is None or meta.get('error') or doc.get('_error')
            or meta.get('invalidated')
            or meta.get('source_binding') is not None and meta['source_binding'] != source.get('_binding')
            or artifact['kind'] != 'patient_context_scan_chunk_v1'
            and meta.get('hash', meta.get('content_hash')) != source['content_hash']):
        return None
    if artifact['kind'] == 'patient_context_scan_chunk_v1':
        candidates = []
        for fact in doc.get('facts') or []:
            if (not isinstance(fact, dict) or any(fact.get(k) != source.get(k)
                    for k in ('project_id', 'message_id', 'content_hash'))
                    or source.get('source_key') is not None and fact.get('source_key') != source['source_key']):
                continue
            span = fact.get('evidence_span')
            if (not isinstance(span, list) or len(span) != 2 or any(type(n) is not int for n in span)
                    or not 0 <= span[0] < span[1] <= len(source['body_text'])
                    or source['body_text'][span[0]:span[1]] != fact.get('evidence')):
                continue
            if any(not isinstance(detail, dict) or not isinstance(detail.get('value'), str)
                    or detail['value'] not in fact['evidence'] for detail in fact.get('details', [])):
                continue
            candidate = {**fact, 'temporal': state.annotate(fact, source['body_text'])}
            if candidate['temporal'] == fact.get('temporal'):
                candidates.append(candidate)
        return candidates
    candidates = []
    for category, key in (('medications', 'medications' if artifact['kind'] == 'extract_v1' else 'meds'),
                          ('symptoms', 'symptoms')):
        for item in doc.get(key) if isinstance(doc.get(key), list) else []:
            if isinstance(item, dict):
                fact = state.clinical_state_fact(source, item, category, artifact['kind'])
                if fact is not None:
                    candidates.append(fact)
    return candidates


def capture_tx(db, pid, mid, *, artifact_ids=None, source_key=None):
    """Capture validated old support in memory under the caller's writer reservation."""
    _active(db)
    if source_key is not None:
        from mcs_queries import karte_source_record
        raw_id = _karte_id(source_key)
        raw = db.execute("SELECT * FROM artifacts WHERE kind='karte_summary' AND project_id=? AND artifact_id=?",
                         (pid, raw_id)).fetchone()
        if raw is None:
            raise ValueError('dependency_loss_karte_source_missing')
        source = karte_source_record(raw, pid)
        binding = source_binding(db, pid, 0, source_key=source_key)
        source['_binding'] = binding
        revision = raw_id
        condition = ("artifacts.project_id=? AND artifacts.kind='patient_context_scan_chunk_v1' "
            "AND CASE WHEN json_valid(artifacts.meta) THEN json_extract(artifacts.meta,'$.source_key')=? END")
        params = (pid, source_key)
    else:
        source_row = db.execute('SELECT * FROM messages WHERE project_id=? AND message_id=?', (pid, mid)).fetchone()
        if source_row is None:
            return {'project_id': pid, 'message_id': mid, 'records': [], 'gaps': []}
        source = dict(source_row)
        binding = source_binding(db, pid, mid)
        source['_binding'] = binding
        revision = db.execute('SELECT COALESCE(MAX(seq),1) FROM message_revisions WHERE message_id=?', (mid,)).fetchone()[0]
        condition = ('(artifacts.project_id=? OR artifacts.project_id IS NULL) AND artifacts.message_id=? '
                     'AND artifacts.kind IN (' + ','.join('?' for _ in _SOURCE_KINDS) + ')')
        params = (pid, mid, *_SOURCE_KINDS)
    if artifact_ids is None:
        rows = db.execute('SELECT artifacts.* FROM artifacts WHERE ' + condition
                          + ' ORDER BY artifacts.artifact_id', params).fetchall()
    else:
        # Artifact IDs are integers; 1.0 matches 1, while '1' does not.
        wanted = set(artifact_ids)
        selected = set()
        for value in wanted:
            try:
                number = int(value.real if isinstance(value, complex) and value.imag == 0 else value)
            except (TypeError, ValueError, OverflowError):
                continue
            if -(2**63) <= number < 2**63 and number in wanted:
                selected.add(number)
        if selected:
            # CROSS JOIN fixes selected IDs as the outer loop, so only those
            # primary-key rows are materialized, regardless of other kinds.
            rows = db.execute('SELECT artifacts.* FROM json_each(?) selected CROSS JOIN artifacts '
                              'WHERE artifacts.artifact_id=selected.value AND ' + condition
                              + ' ORDER BY artifacts.artifact_id',
                              (json.dumps(sorted(selected)), *params)).fetchall()
        else:
            rows = []
    records, gaps, bound_facts, observed = [], [], {}, []
    analysed = False
    valid_chunk_sigs = set()
    import patient_context_state as state
    for artifact in rows:
        meta = loads_dict(artifact['meta']) or {}
        if meta.get('invalidated'):
            continue
        origin = {'message_id': mid, 'content_hash': meta.get('hash', meta.get('content_hash', source['content_hash'])),
                  'revision': revision, 'source_key': source_key if source_key is not None else mid,
                  'binding': binding, 'kind': artifact['kind'],
                  'artifact_id': artifact['artifact_id']}
        facts = _facts(source, artifact) if source.get('body_state') in (None, 'full') else None
        if source_key is None and revision > 1 and not meta.get('source_binding'):
            facts = None
        if facts is None:
            # A prior destruction may already have captured exactly this row.
            known = db.execute("SELECT 1 FROM artifacts WHERE kind=? AND project_id=? "
                "AND CASE WHEN json_valid(content) THEN json_extract(content,'$.origin.artifact_id')=? END LIMIT 1",
                (KIND, pid, artifact['artifact_id'])).fetchone()
            if not known and not meta.get('error'):
                gaps.append(origin)
            continue
        if artifact['kind'] == 'patient_context_scan_chunk_v1' and isinstance(meta.get('source_sig'), str):
            valid_chunk_sigs.add(meta['source_sig'])
        if artifact['kind'] != 'extract_v1':
            observed.append(origin)
        if artifact['kind'] not in ('extract_v1', 'patient_context_scan_chunk_v1'):
            analysed = True
        for fact in facts:
            bound_facts[fact['id']] = (fact, origin)
            temporal = fact.get('temporal') or {}
            entity = state.dependency_entity(fact)
            axes = state.dependency_axes(fact)
            if (entity is None or not axes or temporal.get('unknown')
                    or temporal.get('period') in ('past', 'planned', 'conditional')):
                continue
            day = temporal.get('as_of_day') or temporal.get('event_day')
            for axis in axes:
                record = {'entity': entity, 'axis': axis, 'origin': origin, 'day': day,
                          'support': _digest([state.dependency_values(fact), temporal])}
                records.append(record)
    audited = False
    if artifact_ids is None and valid_chunk_sigs:
        for scan in db.execute("SELECT content,meta FROM artifacts WHERE kind='patient_context_scan_v1' AND project_id=? "
                "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.source_key') IS ? END", (pid, source_key if source_key is not None else mid)):
            scan_doc, scan_meta = loads_dict(scan[0]) or {}, loads_dict(scan[1]) or {}
            if (scan_meta.get('source_sig') in valid_chunk_sigs and not scan_meta.get('invalidated')
                    and scan_doc.get('complete') is True and scan_doc.get('audited') is True
                    and isinstance(scan_doc.get('audited_chunks_sig'), str)
                    and re.fullmatch('[0-9a-f]{64}', scan_doc['audited_chunks_sig'])):
                analysed = audited = True
                break
    # Resolve only explicit, affirmed correction targets using the same exact
    # quote/person/category relation contract as the clinical projection.
    for fact, origin in list(bound_facts.values()):
        refs = [ref for ref in (fact.get('temporal') or {}).get('corrections', [])
                if ref.get('affirmed') is True and type(ref.get('message_id')) is int]
        for target_mid in {ref['message_id'] for ref in refs}:
            target_row = db.execute('SELECT * FROM messages WHERE project_id=? AND message_id=?',
                                    (pid, target_mid)).fetchone()
            if target_row is None:
                continue
            target_source = dict(target_row)
            target_source['_binding'] = source_binding(db, pid, target_mid)
            candidates = {}
            for target_artifact in db.execute('SELECT * FROM artifacts WHERE (project_id=? OR project_id IS NULL) AND message_id=? '
                    'AND kind IN (' + ','.join('?' for _ in _SOURCE_KINDS) + ')', (pid, target_mid, *_SOURCE_KINDS)):
                for candidate in _facts(target_source, target_artifact) or []:
                    candidates[candidate['id']] = candidate
            projection = state.project([fact, *candidates.values()])
            for relation in projection['relations']:
                if (relation.get('source_fact_id') != fact['id']
                        or relation.get('type') not in ('corrects', 'retracts')):
                    continue
                target = candidates.get(relation.get('target_fact_id'))
                entity = state.dependency_entity(target) if target else None
                if entity is None:
                    continue
                temporal = fact['temporal']
                for axis in state.dependency_axes(target):
                    records.append({'entity': entity, 'axis': axis, 'origin': origin,
                        'target_origin': {'message_id': target_mid, 'content_hash': target['content_hash']},
                        'day': temporal.get('as_of_day') or temporal.get('event_day') or temporal.get('posted_day'),
                        'support': _digest(['resolved_relation', relation['type'], state.dependency_values(target)])})
    if any((fact.get('temporal') or {}).get('corrections') for fact, _ in bound_facts.values()) and not records:
        gaps.append({'message_id': mid, 'content_hash': source['content_hash'], 'revision': revision,
                     'binding': binding, 'source_key': source_key if source_key is not None else mid, 'kind': 'unresolved_dependency', 'artifact_id': None})
    if artifact_ids is None and not analysed and source.get('body_state') in (None, 'full') and source.get('body_text'):
        gaps.append({'message_id': mid, 'content_hash': source['content_hash'], 'revision': revision,
                     'binding': binding, 'source_key': source_key if source_key is not None else mid, 'kind': 'unanalysed', 'artifact_id': None})
    return {'project_id': pid, 'message_id': mid, 'records': records, 'gaps': gaps, 'observed': observed, 'source_key': source_key, 'audited': audited}



def capture_current_karte_tx(db, pid):
    """Capture only the current Karte original before its scope or head is replaced."""
    _active(db)
    row = db.execute("SELECT a.artifact_id FROM artifacts a JOIN patients p ON p.project_id=a.project_id "
        "WHERE a.kind='karte_summary' AND a.project_id=? AND CASE WHEN json_valid(a.meta) "
        "THEN json_extract(a.meta,'$.karte_id')=p.karte_id END ORDER BY a.artifact_id DESC LIMIT 1", (pid,)).fetchone()
    return capture_tx(db, pid, 0, source_key='karte:' + str(row[0])) if row is not None else None

def record_tx(db, snapshot, *, replacement_id=None, replacement_ids=(), audited_source=False):
    """Persist lost dependencies atomically; never persist quote, clinical value or support hash."""
    _active(db)
    pid, mid = snapshot['project_id'], snapshot['message_id']
    if audited_source and not (isinstance(snapshot.get('source_key'), str) and snapshot['source_key'].startswith('karte:')):
        raise ValueError('dependency_loss_chat_audit_unsupported')
    equivalent = set()
    replacement_ids = list(replacement_ids) + ([replacement_id] if replacement_id is not None else [])
    if replacement_ids:
        replacement = capture_tx(db, pid, mid, artifact_ids=replacement_ids, source_key=snapshot.get('source_key'))
        equivalent = {(r['entity'], r['axis'], r['support']) for r in replacement['records']}
    entries = [(r, False) for r in snapshot['records']
               if (r['entity'], r['axis'], r['support']) not in equivalent]
    entries += [({'origin': origin}, True) for origin in snapshot['gaps']]
    audit_proof = audited_source or snapshot.get('audited') is True
    if not replacement_ids and (not entries or audit_proof) and not snapshot['gaps']:
        entries += [({'origin': origin, 'coverage': True, 'audited_source': audit_proof}, False) for origin in snapshot.get('observed', [])]
    count = 0
    for item, gap in entries:
        origin = item['origin']
        identity_origin = {k: v for k, v in origin.items() if k != 'artifact_id'}
        content = {'schema': SCHEMA, 'project_id': pid, 'origin': origin, 'gap': gap}
        if item.get('coverage'):
            content['coverage'] = True
            content['audited_source'] = item.get('audited_source') is True
        elif not gap:
            content.update(entity=item['entity'], axis=item['axis'], day=item['day'])
            if item.get('target_origin'):
                content['target_origin'] = item['target_origin']
        identity = _digest([SCHEMA, pid, identity_origin, content.get('entity'), content.get('axis'), gap, content.get('coverage', False)])
        if db.execute('SELECT 1 FROM artifacts WHERE kind=? AND project_id=? AND '
                "CASE WHEN json_valid(meta) THEN json_extract(meta,'$.identity')=? END LIMIT 1",
                (KIND, pid, identity)).fetchone():
            continue
        db.execute('INSERT INTO artifacts(kind,project_id,message_id,content,meta,created_at) VALUES(?,?,?,?,?,?)',
                   (KIND, pid, mid if mid else None, json.dumps(content, ensure_ascii=False),
                    json.dumps({'schema': SCHEMA, 'inference_engine_version': CURRENT_VERSION, 'identity': identity}), time.time()))
        count += 1
    return count



def _valid_loss(doc):
    origin = doc.get('origin') or {}
    if origin.get('message_id') == 0:
        try:
            if _karte_id(origin.get('source_key')) != origin.get('revision'):
                return False
        except ValueError:
            return False
    if (type(origin.get('message_id')) is not int or origin['message_id'] < 0
            or origin['message_id'] == 0 and (not isinstance(origin.get('source_key'), str)
                or re.fullmatch(r'karte:[1-9][0-9]{0,18}', origin['source_key']) is None)
            or type(origin.get('revision')) is not int or not 0 < origin['revision'] < 2**63
            or not isinstance(origin.get('binding'), str) or re.fullmatch('[0-9a-f]{64}', origin['binding']) is None):
        return False
    if doc.get('gap') or doc.get('coverage') is True:
        return True
    if (not isinstance(doc.get('entity'), str) or re.fullmatch('[0-9a-f]{64}', doc['entity']) is None
            or doc.get('axis') not in ('actual_dose', 'actual_frequency', 'residual_quantity', 'medication_manager',
                'supporter', 'storage_place', 'value', 'self_management', 'swallowing', 'food_texture',
                'use_state', 'action', 'symptom_presence')):
        return False
    if doc.get('day') is not None:
        try:
            if not isinstance(doc['day'], str) or len(doc['day']) != 10:
                return False
            date.fromisoformat(doc['day'])
        except ValueError:
            return False
    return True

def read(db, pid):
    """Read persistent negative-use barriers separately from clinical facts and model inputs."""
    losses, gaps, identities, captured, covered_sources = [], 0, [], set(), set()
    pending_gaps, resolved = {}, set()
    coverage = {'terminal': 0, 'repairable': 0, 'reasons': {}}
    def mark(reason, *, terminal=True):
        nonlocal gaps
        gaps += 1
        coverage['terminal' if terminal else 'repairable'] += 1
        coverage['reasons'][reason] = coverage['reasons'].get(reason, 0) + 1
    columns = {table: {row[1] for row in db.execute('PRAGMA table_info(' + table + ')')}
               for table in ('artifacts', 'messages', 'message_revisions', 'patients')}
    if not {'artifact_id', 'kind', 'project_id', 'content', 'meta'}.issubset(columns['artifacts']):
        return {'losses': [], 'complete': False, 'gaps': 1, 'signature': _digest(['missing_artifact_schema']),
                'coverage': {'terminal': 1, 'repairable': 0, 'reasons': {'dependency_schema_unavailable': 1}}}
    retained_karte = {'karte:' + str(row[0]) for row in db.execute(
        "SELECT artifact_id FROM artifacts WHERE kind='karte_summary' AND project_id=?", (pid,))}
    for row in db.execute('SELECT content,meta FROM artifacts WHERE kind=? AND project_id=? ORDER BY artifact_id', (KIND, pid)):
        doc, meta = loads_dict(row[0]), loads_dict(row[1])
        if (doc is None or meta is None or doc.get('schema') != SCHEMA or doc.get('project_id') != pid
                or not isinstance(doc.get('origin'), dict) or type(doc.get('gap')) is not bool or not _valid_loss(doc)):
            mark('invalid_dependency_proof')
            identities.append(_digest(list(row)))
            continue
        identity_origin = {k: v for k, v in doc['origin'].items() if k != 'artifact_id'}
        expected = _digest([SCHEMA, pid, identity_origin, doc.get('entity'), doc.get('axis'), doc['gap'], doc.get('coverage', False)])
        if meta.get('schema') != SCHEMA or meta.get('identity') != expected:
            mark('invalid_dependency_proof')
            identities.append(_digest(list(row)))
            continue
        identities.append(meta.get('identity'))
        origin = doc['origin']
        if type(origin.get('message_id')) is int and type(origin.get('revision')) is int:
            captured.add((origin['message_id'], origin['revision']))
            covered_sources.add(origin.get('source_key', origin['message_id']))
        occurrence = _digest([pid, origin.get('source_key', origin.get('message_id')),
            origin.get('content_hash'), origin.get('revision'), origin.get('binding')])
        if doc['gap']:
            pending_gaps[occurrence] = origin
        elif doc.get('coverage') is True:
            if doc.get('audited_source') is True and origin.get('source_key') in retained_karte:
                resolved.add(occurrence)
            continue
        elif (not isinstance(doc.get('entity'), str) or len(doc['entity']) != 64
              or not isinstance(doc.get('axis'), str) or doc.get('day') is not None and not isinstance(doc['day'], str)):
            mark('invalid_dependency_proof')
        else:
            losses.append(doc)
    for occurrence, origin in pending_gaps.items():
        if occurrence in resolved:
            continue
        if origin.get('source_key') in retained_karte:
            mark('retained_karte_requires_audit', terminal=False)
        else:
            mark('historical_chat_body_unavailable')
    if ({'message_id', 'project_id'}.issubset(columns['messages'])
            and {'message_id', 'seq', 'content_hash'}.issubset(columns['message_revisions'])):
        for row in db.execute('SELECT r.message_id,r.seq,r.content_hash FROM message_revisions r '
                'JOIN messages m ON m.message_id=r.message_id WHERE m.project_id=? '
                'AND r.seq<(SELECT MAX(n.seq) FROM message_revisions n WHERE n.message_id=r.message_id)', (pid,)):
            covered = (row[0], row[1]) in captured
            if not covered:
                mark('uncaptured_chat_revision')
                identities.append(_digest(['uncaptured_revision', *row]))
    else:
        mark('dependency_schema_unavailable')
        identities.append('missing_revision_schema')
    if {'project_id', 'karte_id'}.issubset(columns['patients']):
        rows = db.execute("SELECT a.artifact_id FROM artifacts a JOIN patients p ON p.project_id=a.project_id "
            "WHERE a.kind='karte_summary' AND a.project_id=? AND CASE WHEN json_valid(a.meta) "
            "AND json_valid(a.content) THEN json_extract(a.meta,'$.karte_id')=p.karte_id "
            "AND length(json_extract(a.content,'$.comment'))>0 END "
            "AND a.artifact_id<(SELECT MAX(n.artifact_id) FROM artifacts n WHERE n.kind='karte_summary' "
            "AND n.project_id=a.project_id AND CASE WHEN json_valid(n.meta) THEN "
            "json_extract(n.meta,'$.karte_id')=p.karte_id END)", (pid,))
        for row in rows:
            if 'karte:' + str(row[0]) not in covered_sources:
                mark('retained_karte_requires_audit', terminal=False)
                identities.append(['uncaptured_karte', row[0]])
    else:
        mark('dependency_schema_unavailable')
        identities.append('missing_karte_scope_schema')
    return {'losses': losses, 'complete': gaps == 0, 'gaps': gaps, 'signature': _digest(identities), 'coverage': coverage}
