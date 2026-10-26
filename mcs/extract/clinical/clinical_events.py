"""Preserve source-bound event axes and safely project the legacy event labels."""
import json
import re

from clinical_scope import source_axes, source_polarity
from mcs_util import locate_quote_span

EVENT_KINDS = ('visit', 'exam', 'admission', 'discharge', 'transfer', 'fall',
               'eol', 'care', 'family_contact', 'prescription_inquiry', 'other')
SUBJECTS = ('patient', 'family', 'other', 'unknown')
STATUSES = ('current', 'past', 'planned', 'conditional', 'unknown')

ENCOUNTER_CUE = (r"(?:診察|往診|診療)(?!医|所|科|機関|方針|報酬|体制|契約|情報|録|券|室|"
                 r"の(?:説明|方針|契約|案内|資料)|について(?:の)?(?:説明|案内|資料)|"
                 r"に関する(?:説明|案内|資料))|診て(?:くれ|いただ|もら)")

# Broad recall cues supplied by the caller may nominate a candidate. These
# direct event cues alone can establish the kind; medication is never EOL proof.
_DIRECT = {
    'admission': r'入院', 'discharge': r'退院|出院|退所|退館',
    'transfer': r'転院|転棟|転所', 'fall': r'転倒|転落|滑落',
    'eol': r'看取り|逝去|死去|永眠|お亡くなり|亡くな(?:っ|り)|死亡|終末期|臨終',
    'visit': r'訪問|往診|来訪',
    'exam': ENCOUNTER_CUE + r'|受診|検査|採血|レントゲン|エコー|心電図|CT|MRI',
    'prescription_inquiry': r'疑義照会|処方提案',
    'care': r'介助|ケア|処置', 'family_contact': r'家族(?:に|へ|と)(?:連絡|相談)|家族連絡',
}
_DIRECT = {kind: re.compile(pattern) for kind, pattern in _DIRECT.items()}
_BREAK = re.compile(r'[。！？!?;；\n、,]')
_UNCERTAIN = re.compile(r'否定でき|かもしれ|疑い|不明|未確認|未確定|未定|可能性|ない(?:こと|わけ|訳)はない')
_NEGATED = re.compile(r'^(?:では(?:ありません|ない|なく)|じゃない|(?:の)?(?:予定|計画)(?:は|が|も)?(?:なし|ない|ありません))')
_QUESTION = re.compile(r'(?:か|でしょうか)[。！？!?\s]*$|かどうか|とは[^。！？!?\n]{0,80}(?:記載なし|書かれていません|報告されていません)')
_AFFIRMED = re.compile(r'^(?:は|が|を)?(?:現在|今は)?(?:しました|した|しています|している|し(?=[、,]|$)|中|済み|です|となり|となった|となりました|されました|された|を受けました|を実施しました)')


def _span(body, item):
    quote = item.get('evidence')
    if not isinstance(body, str) or not isinstance(quote, str) or not quote.strip():
        return None
    span = item.get('span')
    if isinstance(span, dict):
        start, end = span.get('start'), span.get('end')
        if (type(start) is int and type(end) is int and 0 <= start < end <= len(body)
                and body[start:end] == quote):
            return start, end
    return locate_quote_span(body, quote)


def _clause(body, start, end):
    left = max((m.end() for m in _BREAK.finditer(body, 0, start)), default=0)
    right = _BREAK.search(body, end)
    return left, right.end() if right else len(body)


def _polarity(body, start, end, target_end, status):
    value = source_polarity(body, start, end, target_end=target_end)
    tail = body[target_end:end]
    if _UNCERTAIN.search(tail) or _QUESTION.search(tail):
        return None
    if _NEGATED.search(tail):
        return True
    if value is not None:
        return value
    if _AFFIRMED.search(tail) or status in ('planned', 'conditional'):
        return False
    return None


def validate_record(item, body):
    """Keep a typed candidate even when its semantic or quotation support is unresolved."""
    if not isinstance(item, dict) or item.get('kind') not in EVENT_KINDS:
        return None
    subject = item.get('subject', 'unknown')
    status = item.get('status', 'unknown')
    negated = item.get('negated')
    if (subject not in SUBJECTS or status not in STATUSES
            or negated is not None and type(negated) is not bool
            or 'unverified' in item and type(item['unverified']) is not bool):
        return None
    result = {'kind': item['kind'], 'subject': subject, 'status': status,
              'negated': negated, 'evidence': '', 'span': None}
    located = _span(body, item)
    unresolved = item.get('unverified') is True
    if located is None:
        result['unverified'] = True
        return result
    start, end = located
    quote = body[start:end]
    result.update(evidence=quote, span={'start': start, 'end': end})
    cue = _DIRECT.get(item['kind'])
    matches = list(cue.finditer(quote)) if cue is not None else []
    if len(matches) != 1:
        result['unverified'] = True
        return result
    match = matches[0]
    anchor, target_end = start + match.start(), start + match.end()
    _, clause_end = _clause(body, anchor, target_end)
    actual_subject, actual_status = source_axes(body, anchor, target_end, default='unknown')
    actual_negated = _polarity(body, anchor, clause_end, target_end, actual_status)
    # Explicit wrong attributes do not become verified facts on revalidation.
    if subject not in ('unknown', actual_subject):
        unresolved = True
    if status not in ('unknown', actual_status):
        unresolved = True
    if negated is not None and negated != actual_negated:
        unresolved = True
    result.update(subject=actual_subject, status=actual_status, negated=actual_negated)
    if unresolved or actual_subject == 'unknown' or actual_status == 'unknown' or actual_negated is None:
        result['unverified'] = True
    return result


def normalize_events(events, records, body, *, cues):
    """Return safe enum projection, retained records and malformed-item count."""
    clean, dropped = [], 0
    if records is not None and not isinstance(records, list):
        dropped += 1
        records = []
    for item in records or []:
        record = validate_record(item, body)
        if record is None:
            dropped += 1
        else:
            clean.append(record)
    if events is not None and not isinstance(events, list):
        dropped += 1
        events = []
    for kind in dict.fromkeys(e for e in events or [] if isinstance(e, str) and e in EVENT_KINDS):
        # A supplied record is the authoritative interpretation of this kind;
        # a parallel old enum must not bypass its unresolved attributes.
        if any(record['kind'] == kind for record in clean):
            continue
        cue = _DIRECT.get(kind) or cues.get(kind)
        matches = list(cue.finditer(body)) if cue is not None and isinstance(body, str) else []
        if not matches:
            if isinstance(body, str) and kind in cues:
                matches = list(cues[kind].finditer(body))
        if not matches:
            clean.append({'kind': kind, 'subject': 'unknown', 'status': 'unknown',
                          'negated': None, 'evidence': '', 'span': None, 'unverified': True})
        for match in matches:
            start, end = _clause(body, match.start(), match.end())
            record = validate_record({'kind': kind, 'evidence': body[start:end],
                                      'span': {'start': start, 'end': end}}, body)
            if record is not None:
                clean.append(record)
    unique, seen = [], set()
    for record in clean:
        key = json.dumps(record, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            unique.append(record)
            seen.add(key)
    legacy = list(dict.fromkeys(record['kind'] for record in unique
        if record['subject'] == 'patient' and record['status'] == 'current'
        and record['negated'] is False and not record.get('unverified')))
    return legacy, unique, dropped


def project_piece_records(document, records, piece, body, source_offset=None):
    """Bind core-located event quotes to the actual source occurrence before global validation."""
    if source_offset is None:
        offset = body.find(piece) if piece and body.count(piece) == 1 else None
    else:
        offset = source_offset if (type(source_offset) is int and source_offset >= 0
            and body[source_offset:source_offset + len(piece)] == piece) else None
    raw = document.get('event_records')
    raw = raw if isinstance(raw, list) else []
    projected = []
    for record in records:
        local = record.get('span')
        located = (local['start'], local['end']) if isinstance(local, dict) else None
        # Re-evaluate the original proposed axes in the full source context.
        # A core beginning after its owner must not permanently erase that owner.
        candidates = [item for item in raw if isinstance(item, dict)
                      and item.get('kind') == record['kind'] and _span(piece, item) == located]
        item = dict(candidates[0]) if len(candidates) == 1 else {'kind': record['kind']}
        item.update(evidence=record['evidence'], span=None)
        if offset is not None and located is not None:
            item['span'] = {'start': offset + located[0], 'end': offset + located[1]}
        else:
            item['unverified'] = True
        projected.append(item)
    return projected


def canonical_record(fact, evidence, *, disputed=False):
    """Project canonical event axes using existing evidence coordinates, without a source body."""
    statement = fact.get('statement') or ''
    kinds = [kind for kind, cue in _DIRECT.items() if cue.search(statement)]
    kind = kinds[0] if len(kinds) == 1 else 'other'
    quote = evidence.get('quote') if isinstance(evidence, dict) else ''
    quote = quote if isinstance(quote, str) else ''
    identity = fact.get('subject')
    subject = ('patient' if isinstance(identity, str) and identity.startswith('patient:')
               else 'family' if identity == 'role:family'
               else 'unknown' if not identity or 'unknown' in identity else 'other')
    polarity = fact.get('polarity')
    negated = True if polarity == 'negated' else False if polarity == 'affirmed' else None
    workflow = fact.get('workflow_status')
    status = 'planned' if workflow in ('planned', 'ordered', 'pending', 'considering', 'on_hold') else 'unknown'
    cue = _DIRECT.get(kind)
    match = cue.search(quote) if cue is not None else None
    mismatch = False
    if match is not None:
        source_subject, source_status = source_axes(quote, match.start(), match.end(), default=subject)
        if source_subject != subject:
            subject = 'unknown'
        source_negated = _polarity(quote, match.start(), len(quote), match.end(), source_status)
        mismatch = source_negated is None or source_negated != negated
        if status == 'planned' and source_status != 'planned':
            mismatch = True
        elif source_status in ('past', 'planned', 'conditional'):
            status = source_status
        elif re.search(r'本日|今日|現在|今は|今も', quote):
            status = 'current'
        valid_time = fact.get('valid_time')
        if valid_time in STATUSES and valid_time != 'unknown' and valid_time != status:
            mismatch = True
    start = evidence.get('start') if isinstance(evidence, dict) else None
    end = evidence.get('end') if isinstance(evidence, dict) else None
    span = ({'start': start, 'end': end}
            if type(start) is int and type(end) is int and 0 <= start < end
            and end - start == len(quote) else None)
    record = {'kind': kind, 'subject': subject, 'status': status,
              'negated': negated, 'evidence': quote, 'span': span}
    if (disputed or mismatch or match is None or span is None or subject == 'unknown'
            or negated is None or fact.get('epistemic') not in ('asserted', 'reported')
            or workflow not in ('performed', 'done', 'reported', 'in_progress',
                                'planned', 'ordered', 'pending', 'considering', 'on_hold')):
        record['unverified'] = True
    return record
