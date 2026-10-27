"""Preserve source-bound event axes and safely project the legacy event labels."""
import json
import re

from clinical_scope import REPORTED_ABSENCE, source_axes, source_polarity
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
_QUESTION = re.compile(r'(?:か|でしょうか)[。！？!?\s]*$|[?？][。！？!?\s]*$|かどうか|'
                       r'とは[^。！？!?\n]{0,80}(?:記載なし|書かれていません|報告されていません)|' + REPORTED_ABSENCE)
_AFFIRMED = re.compile(r'^(?:は|が|を)?(?:現在|今は)?(?:しました|した|しています|している|し(?=[、,]|$)|中|済み|です|となり|となった|となりました|されました|された|を受けました|を実施しました)')
_SENTENCE_ENDS = '。！？!?;；\n'
_CORRECTION_TAIL = (r'[^。！？!?;；\n]{0,24}(?:'
                    r'(?:誤り|誤記)(?!では(?:ない|ありません|なく)|じゃない)|'
                    r'間違(?!いでは(?:ない|ありません|なく)|って(?:いない|いません|ない|おらず))|'
                    r'(?:訂正|撤回|取り消|取消)(?!\s*(?:し|す|され)?\s*(?:は|が|を)?\s*'
                    r'(?:不要|必要(?:は|が)?(?:ない|ありません)|ない|ません))|'
                    r'事実(?:ではない|ではありません|と異なる)|正しく(?:ない|ありません)|'
                    r'違(?:います|いました|う|った)|異な(?:ります|りました|る|った)|'
                    r'なしで(?!は(?:ない|ありません)))')
_CORRECTED_FORWARD = re.compile(r'(?:次|下記|以下)(?:の)?(?:記載|内容|報告|文章|投稿|情報|件)' + _CORRECTION_TAIL)
_CORRECTED_BACKWARD = re.compile(
    r'(?:(?:上記|前記|前述|先ほど|その|この)(?:の)?|という|との)'
    r'(?:記載|内容|報告|文章|投稿|情報|件)' + _CORRECTION_TAIL)
_CONTRADICTED_EVENT = re.compile(r'(?:実際(?:には|は)?|事実(?:は|としては)?)\s*'
                                 r'(?:違(?:います|いました|う|った)|異な(?:ります|りました|る|った))')
_OTHER_TOPIC = re.compile(
    r'(?:家族|ご家族|母|父|娘|息子|夫|妻|他者|他人|別の患者|別患者)'
    r'(?:本人|自身)?(?:の(?:話|件|こと|記録|状態|情報)|について)')
_EXPLICIT_PATIENT = re.compile(r'(?:ご本人|患者本人|本人|患者(?:さん|様)?)')


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
    if (_UNCERTAIN.search(tail) or _QUESTION.search(tail)
            or tail.rstrip().endswith(('、', ',')) and re.match(r'\s*' + REPORTED_ABSENCE, body[end:])):
        return None
    if _NEGATED.search(tail):
        return True
    if value is not None:
        return value
    cue = body[start:target_end]
    if ((cue in ('お亡くなり', '亡くなり') and re.fullmatch(r'(?:になりました|になった|ました)[。;；\n\s]*', tail))
            or cue == '亡くなっ' and re.fullmatch(r'た[。;；\n\s]*', tail)):
        return False
    if _AFFIRMED.search(tail) or status in ('planned', 'conditional'):
        return False
    return None


def _ambiguous_context(body, anchor, target_end, subject):
    """Find adjacent references that make an event or its subject uncertain."""
    left = max(body.rfind(mark, 0, anchor) for mark in _SENTENCE_ENDS) + 1
    right = min((position for mark in _SENTENCE_ENDS
                 if (position := body.find(mark, target_end)) >= 0), default=len(body))
    previous_end = left - 1 if left else 0
    previous_left = max(body.rfind(mark, 0, previous_end) for mark in _SENTENCE_ENDS) + 1
    following_start = right + 1 if right < len(body) else len(body)
    following_end = min((position for mark in _SENTENCE_ENDS
                         if (position := body.find(mark, following_start)) >= 0), default=len(body))
    previous, sentence, following = body[previous_left:previous_end], body[left:right], body[following_start:following_end]
    correction = (_CORRECTED_FORWARD.search(previous) or _CORRECTED_FORWARD.search(sentence)
                  or _CORRECTED_BACKWARD.search(sentence) or _CORRECTED_BACKWARD.search(following)
                  or _CONTRADICTED_EVENT.search(body[target_end:right]))
    # A preceding topic such as "family's story" can make a subsequent bare
    # "the person" refer to that person; a family member merely reporting
    # about the patient does not establish such a conflicting topic.
    ambiguous_subject = (subject == 'patient' and _OTHER_TOPIC.search(previous)
                         and _EXPLICIT_PATIENT.search(body[left:anchor]))
    return bool(correction), bool(ambiguous_subject)


def candidate_span(item, body):
    """Return a nomination's source-bound cue span without confirming its unknown axes."""
    if not isinstance(item, dict) or not isinstance(item.get('kind'), str):
        return None
    span = _span(body, item)
    cue = _DIRECT.get(item.get('kind'))
    return span if span is not None and cue is not None and len(list(cue.finditer(body[span[0]:span[1]]))) == 1 else None


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
    correction, ambiguous_subject = _ambiguous_context(body, anchor, target_end, actual_subject)
    if correction:
        actual_status, actual_negated, unresolved = 'unknown', None, True
    if ambiguous_subject:
        actual_subject, unresolved = 'unknown', True
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
