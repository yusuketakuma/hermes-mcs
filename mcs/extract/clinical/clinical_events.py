"""Preserve source-bound event axes and safely project the legacy event labels."""
import itertools
import json
import re

from clinical_scope import REPORTED_ABSENCE, source_axes, source_polarity
from clinical_values import written_day
from mcs_util import locate_quote_span

EVENT_KINDS = ('visit', 'exam', 'admission', 'discharge', 'transfer', 'fall',
               'eol', 'care', 'family_contact', 'prescription_inquiry', 'other')
SUBJECTS = ('patient', 'family', 'other', 'unknown')
STATUSES = ('current', 'past', 'planned', 'conditional', 'unknown')

# Optional care_detail.event_type values on event_records. The enumeration
# contract lives in docs/specs/care-intervention-events.md; legacy EVENT_KINDS
# stay unchanged and a record may carry at most one care_detail.
CARE_DETAIL_TYPES = (
    'intervention_start', 'intervention_pause', 'intervention_resume',
    'intervention_end', 'service_contract_start', 'pre_discharge_conference',
    'discharge_joint_guidance', 'care_team_conference', 'care_plan_change',
    'monitoring', 'care_need_certification_change', 'respite',
    'facility_entry', 'facility_exit', 'death', 'acp_discussion',
    'care_policy_change')

# Per-type source cues aggregated in the shared verification path: a
# care_detail claim is kept only when its own vocabulary appears inside the
# evidence quote (docs/specs/care-intervention-events.md §2). These are
# recall cues for meaning support; subject/polarity/status axes are still
# verified by the shared record path, and generic verbs without the type's
# distinctive noun (e.g. a bare 開始 for a meeting) do not qualify.
_SERVICE_HEAD = (r'(?:サービス|介護|支援|訪問看護|訪問介護|訪問|看護|'
                 r'リハビリ|ケア|介入)')
_SERVICE_CUE = _SERVICE_HEAD + r'(?:サービス|ステーション|事業所)?'
# Event nouns shared by the recall gap, the bind spine and instance counts.
_DETAIL_EVENT_NOUN = (
    r'開催|実施|話し合い|話合い|指導|面談|確認|表明|意向'
    r'|決定|変更|見直し|撤回|聴取')
# Words that can complete a care event. The gap between a service word and
# its transition verb may hold a date or adverb but never crosses a
# sentence end, an event-completing word or another action's object —
# 「訪問看護について資料作成を開始」 binds 開始 to the material work, not
# to the service, so the match stops before it.
_DETAIL_EVENT_WORD = (
    _DETAIL_EVENT_NOUN
    + r'|開始|スタート|始め|中断|休止|一時停止|おやすみ|お休み|再開'
    + r'|終了|終結|卒業|打ち切り|締結|結ぶ|結んだ|成立|契約開始'
    + r'|区分変更|更新|結果|修正|改訂|利用|入所|入居|退所|退去'
    + r'|説明|受け')
_DETAIL_OBJECT = r'[^\sをがはのにへとで、。，,;；！？!?\n]{2,}[をが]'
# A verb termination followed by a comma ends the act: 「…を訪問し、開始」
# makes the start a separate act, so the gap never borrows the predicate
# across the boundary. A comma after a date or particle (「9月1日、開始」)
# stays inside the same act.
_ACT_END = re.compile(r'(?:て|で|し|た|ました|する|します)(?=、)')
_GAP = (r'(?:(?!' + _DETAIL_EVENT_WORD + r'|' + _DETAIL_OBJECT
        + r'|(?:て|で|し|た|ました|する|します)(?=、)'
        + r')[^。！？!?\n]){0,20}')
CARE_DETAIL_CUES = {
    'intervention_start': re.compile(
        _SERVICE_CUE + r'(?:を|の|は)?' + _GAP + r'(?:開始|スタート|始め)'),
    'intervention_pause': re.compile(
        _SERVICE_CUE + r'(?:を|の|は)?' + _GAP
        + r'(?:中断|休止|一時停止|おやすみ|お休み)'),
    'intervention_resume': re.compile(
        _SERVICE_CUE + r'(?:を|の|は)?' + _GAP + r'再開'),
    'intervention_end': re.compile(
        _SERVICE_CUE + r'(?:を|の|は)?' + _GAP
        + r'(?:終了|終結|卒業|打ち切り)'),
    'service_contract_start': re.compile(
        r'契約(?:を|の|は)?' + _GAP
        + r'(?:開始|締結|結ぶ|結んだ|成立)'
        r'|契約(?:を|の|は)?し(?:ました|ている|する)|契約開始'),
    'pre_discharge_conference': re.compile(
        r'(?:退院前|退院に向けた)(?:の|に)?'
        r'(?:カンファレンス|会議|検討会|話し合い)|退院前カンファレンス'),
    'discharge_joint_guidance': re.compile(
        r'退院時共同指導|退院時の共同指導|退院に際しての共同指導'
        r'|退院前の共同指導'),
    'care_team_conference': re.compile(
        r'サービス担当者会議|担当者会議|ケア会議|検討会|担当者会'),
    'care_plan_change': re.compile(
        r'(?:ケアプラン|介護計画|計画|プラン)(?:の|を|は)?' + _GAP
        + r'(?:変更|見直し|修正|改訂)|計画変更'),
    'monitoring': re.compile(
        r'モニタリング|経過観察|フォローアップ|目標達成'
        r'|(?:計画|プラン)(?:の|を|は)?' + _GAP + r'見直し'),
    'care_need_certification_change': re.compile(
        r'(?:要介護|要支援|介護度|認定|区分)(?:の|が|を|は)?' + _GAP
        + r'(?:変更|区分変更|更新|結果|決定)'
        r'|認定(?:の|が|は)?' + _GAP + r'(?:変更|結果|更新|決定)'),
    'respite': re.compile(r'レスパイト|短期入所|ショートステイ|短期利用'),
    'facility_entry': re.compile(
        r'(?:施設|ホーム|サ高住|グループホーム|特養|老健)(?:へ|に|への)?' + _GAP
        + r'(?:入所|入居)'
        r'|(?<!短期)(?<!ショートステイ)入所(?:しました|する|した|している)'
        r'|(?<!短期)(?<!ショートステイ)入居(?:しました|する|した|している)'),
    'facility_exit': re.compile(
        r'(?:施設|ホーム|サ高住|グループホーム|特養|老健)(?:を|から|へ)?' + _GAP
        + r'(?:退所|退去)'
        r'|退所(?:しました|する|した|している)'
        r'|退去(?:しました|する|した|している)'),
    'death': re.compile(r'死亡|亡くな|お亡くなり|他界|永眠|死去|急逝'),
    'acp_discussion': re.compile(
        r'ACP|人生会議|アドバンス・ケア|終末期(?:の|に関する)?話し合い'
        r'|意向(?:の|を)?(?:確認|表明|変化)'),
    'care_policy_change': re.compile(
        r'(?:療養|治療|ケア|看取り|延命)(?:の|に関する)?方針'
        r'(?:を|の|は)?' + _GAP + r'(?:変更|決定|見直し|撤回)'
        r'|方針(?:を|の|は)?' + _GAP + r'(?:変更|決定|撤回)'),
}

# For cue patterns that continue past the event noun through a gap into
# the predicate, the instance head is the leading noun only — the anchor
# must sit inside it to share the instance (「訪問看護を開始」 anchors a
# visit record; 「サービスを訪問後、開始」 does not). Types absent here
# keep the whole cue match as the head (noun-only cues like 会議).
_DETAIL_HEAD = {
    'intervention_start': _SERVICE_CUE,
    'intervention_pause': _SERVICE_CUE,
    'intervention_resume': _SERVICE_CUE,
    'intervention_end': _SERVICE_CUE,
    'service_contract_start': r'契約',
    'care_plan_change': r'(?:ケアプラン|介護計画|計画|プラン)',
    'monitoring': r'(?:モニタリング|経過観察|フォローアップ|目標達成'
                  r'|計画|プラン)',
    'care_need_certification_change': r'(?:要介護|要支援|介護度|認定|区分)',
    'facility_entry': r'(?:グループホーム|サ高住|特養|老健|施設|ホーム'
                      r'|入所|入居)',
    'facility_exit': r'(?:グループホーム|サ高住|特養|老健|施設|ホーム'
                     r'|退所|退去)',
    'care_policy_change': r'(?:(?:療養|治療|ケア|看取り|延命)'
                          r'(?:の|に関する)?方針|方針)',
}
_DETAIL_HEAD = {kind: re.compile(pattern)
                for kind, pattern in _DETAIL_HEAD.items()}

# Vocabulary co-occurrence alone does not establish the event: a contract
# start is not a service-provision start, a short stay is not facility
# entry, and DNAR/a bare "joint guidance" mention is not a held conference
# or guidance session. Matches whose own span carries the veto are dropped
# before uniqueness/anchor checks, so the rejected reading cannot anchor.
_DETAIL_VETOES = {
    'intervention_start': re.compile(r'契約'),
    'intervention_pause': re.compile(r'契約'),
    'intervention_resume': re.compile(r'契約'),
    'intervention_end': re.compile(r'契約'),
    'facility_entry': re.compile(r'短期|ショートステイ|レスパイト'),
}

# For event types whose cue is only a noun, the cue must bind to the
# event's own predicate — vocabulary presence alone is not a held
# conference, a conducted guidance session, a held discussion, an
# implemented respite stay, an updated certification, or a decided policy
# change. care_policy_change needs no entry: its cue already ends in the
# change word itself.
_DETAIL_PREDICATES = {
    'pre_discharge_conference': re.compile(
        r'開催|実施|行(?:った|う|います|われ)|開(?:いた|かれ|きた)|決ま'),
    'discharge_joint_guidance': re.compile(
        r'実施|行(?:った|う|います|われ)|指導(?:した|します|している)'
        r'|説明(?:した|します)|受け|決ま'),
    'care_team_conference': re.compile(
        r'開催|実施|行(?:った|う|います|われ)|開(?:いた|かれ|きた)|決ま'),
    'acp_discussion': re.compile(
        r'話し合い|話合い|面談|確認|表明|意向|実施|聴取|決ま'),
    'care_need_certification_change': re.compile(
        r'変更|区分変更|更新|結果|決定'),
    'respite': re.compile(r'利用|入所|泊'),
}

# A noun cue heads the event only through an object/topic particle or a
# nominalised event noun (「会議を開催」「会議の開催」「指導について実施」).
# 「会議の資料」「指導の説明」 bind の+non-event noun and never qualify.
_DETAIL_BIND = re.compile(
    r'(?:を|が|は|も|について|に関して)'
    r'|の(?=' + _DETAIL_EVENT_NOUN + r')')

_WORKFLOW_PLANNED = frozenset(
    ('planned', 'ordered', 'pending', 'considering', 'on_hold'))
_WORKFLOW_DONE = frozenset(('performed', 'done', 'in_progress'))


def _workflow_family(value):
    if value in _WORKFLOW_PLANNED:
        return 'planned'
    if value in _WORKFLOW_DONE:
        return 'performed'
    return None

ENCOUNTER_CUE = (r"(?:診察|往診|診療)(?!医|所|科|機関|方針|報酬|体制|契約|情報|録|券|室|"
                 r"の(?:説明|方針|契約|案内|資料)|について(?:の)?(?:説明|案内|資料)|"
                 r"に関する(?:説明|案内|資料))|診て(?:くれ|いただ|もら)")
OUTPATIENT_CUE = r'外来(?:に|へ)行(?:ってきました|きました)'

# Broad recall cues supplied by the caller may nominate a candidate. These
# direct event cues alone can establish the kind; medication is never EOL proof.
_DIRECT = {
    'admission': r'入院', 'discharge': r'退院|出院|退所|退館',
    'transfer': r'転院|転棟|転所', 'fall': r'転倒|転落|滑落',
    'eol': r'看取り|逝去|死去|永眠|お亡くなり|亡くな(?:っ|り)|死亡|終末期|臨終',
    'visit': r'訪問|往診|来訪',
    'exam': ENCOUNTER_CUE + r'|受診|検査|採血|レントゲン|エコー|心電図|CT|MRI|' + OUTPATIENT_CUE,
    'prescription_inquiry': r'疑義照会|処方提案',
    'care': r'介助|ケア|処置', 'family_contact': r'家族(?:に|へ|と)(?:連絡|相談)|家族連絡',
}
_DIRECT = {kind: re.compile(pattern) for kind, pattern in _DIRECT.items()}
_EVENT_IDENTITY = {'退院': '退院', '出院': '退院'}
_EVENT_CANDIDATE_LIMIT = 64
_BREAK = re.compile(r'[。！？!?;；\n、,]')
_UNCERTAIN = re.compile(r'否定でき|かもしれ|疑い|不明|未確認|未確定|未定|可能性|ない(?:こと|わけ|訳)はない')
_NEGATED = re.compile(r'^(?:では(?:ありません|ない|なく)|じゃない|(?:の)?(?:予定|計画)(?:は|が|も)?(?:なし|ない|ありません))')
_QUESTION = re.compile(r'(?:か|でしょうか)[。！？!?\s]*$|[?？][。！？!?\s]*$|かどうか|'
                       r'とは[^。！？!?\n]{0,80}(?:記載なし|書かれていません|報告されていません)|' + REPORTED_ABSENCE)
_AFFIRMED = re.compile(r'^(?:は|が|を)?(?:現在|今は)?(?:しました|した|しています|している|し(?=[、,]|$)|中|済み|です|となり|となった|となりました|されました|された|を受けました|を実施しました|ました|りました|なりました|なった|になりました|になった)')
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
    cue = body[start:target_end]
    if value is None and cue and cue in tail:
        return None
    if _NEGATED.search(tail):
        return True
    if value is not None:
        return value
    if ((cue in ('お亡くなり', '亡くなり') and re.fullmatch(r'(?:になりました|になった|ました)[。;；\n\s]*', tail))
            or cue == '亡くなっ' and re.fullmatch(r'た[。;；\n\s]*', tail)):
        return False
    if _AFFIRMED.search(tail) or status in ('planned', 'conditional'):
        return False
    return None


def _ambiguous_context(body, anchor, target_end, subject):
    """Find references that make an event or its subject uncertain.

    Correction references are directional and local: a forward announcement
    ("次の記載は誤り") covers the previous and own sentence, a backward
    reference ("その記載は誤り") covers the own and following sentence, and
    an explicit contradiction only tails the own sentence. Corrections about
    unrelated records elsewhere in the body belong to the projection-level
    ``context_clean`` gate, not to this record's evidence check. The
    topic-shift check stays adjacent: only a neighbouring other-person
    frame can rebind a bare patient mention.
    """
    left = max(body.rfind(mark, 0, anchor) for mark in _SENTENCE_ENDS) + 1
    right = min((position for mark in _SENTENCE_ENDS
                 if (position := body.find(mark, target_end)) >= 0),
                default=len(body))
    previous_end = left - 1 if left else 0
    previous_left = max(body.rfind(mark, 0, previous_end)
                        for mark in _SENTENCE_ENDS) + 1
    following_start = right + 1 if right < len(body) else len(body)
    following_end = min((position for mark in _SENTENCE_ENDS
                         if (position := body.find(mark, following_start)) >= 0),
                        default=len(body))
    previous, sentence = body[previous_left:previous_end], body[left:right]
    following = body[following_start:following_end]
    correction = (_CORRECTED_FORWARD.search(previous)
                  or _CORRECTED_FORWARD.search(sentence)
                  or _CORRECTED_BACKWARD.search(sentence)
                  or _CORRECTED_BACKWARD.search(following)
                  or _CONTRADICTED_EVENT.search(body[target_end:right]))
    ambiguous_subject = (subject == 'patient' and _OTHER_TOPIC.search(previous)
                         and _EXPLICIT_PATIENT.search(body[left:anchor]))
    return bool(correction), bool(ambiguous_subject)


def _contradictory_event(body, anchor, target_end, subject, kind):
    """Hold opposing assertions in one sentence or an explicit adjacent contrast."""
    if subject == 'unknown' or kind not in _DIRECT:
        return False
    left = max(body.rfind(mark, 0, anchor) for mark in _SENTENCE_ENDS) + 1
    right = min((pos for mark in _SENTENCE_ENDS
                 if (pos := body.find(mark, target_end)) >= 0), default=len(body))
    contrast = r'\s*(?:しかし|ところが|だが|ただし|実際(?:には|は)?)'
    if left and re.match(contrast, body[left:anchor]):
        left = max(body.rfind(mark, 0, left - 1) for mark in _SENTENCE_ENDS) + 1
    if right < len(body) and re.match(contrast, body[right + 1:]):
        right = min((pos for mark in _SENTENCE_ENDS
                     if (pos := body.find(mark, right + 1)) >= 0), default=len(body))
    cue = body[anchor:target_end]
    candidates = list(itertools.islice(_DIRECT[kind].finditer(body, left, right), _EVENT_CANDIDATE_LIMIT + 1))
    if len(candidates) > _EVENT_CANDIDATE_LIMIT:
        return True  # A bounded scan cannot establish absence of a contradiction.
    identity = _EVENT_IDENTITY.get(cue, cue)
    matches = [m for m in candidates if _EVENT_IDENTITY.get(m.group(), m.group()) == identity]
    phases = {}
    for index, match in enumerate(matches):
        owner, phase = source_axes(body, match.start(), match.end(), default='unknown')
        if owner != subject or phase not in ('current', 'past'):
            continue
        _, stop = _clause(body, match.start(), match.end())
        if index + 1 < len(matches):
            stop = min(stop, matches[index + 1].start())
        polarity = _polarity(body, match.start(), stop, match.end(), phase)
        if polarity is not None:
            phases.setdefault(phase, set()).add(polarity)
            if len(phases[phase]) > 1:
                return True
    return False


def candidate_span(item, body):
    """Return a nomination's source-bound cue span without confirming its unknown axes."""
    if not isinstance(item, dict) or not isinstance(item.get('kind'), str):
        return None
    span = _span(body, item)
    cue = _DIRECT.get(item.get('kind'))
    return span if span is not None and cue is not None and len(list(cue.finditer(body[span[0]:span[1]]))) == 1 else None


def _detail_cue_matches(quote, event_type, left=0, right=None):
    cue = CARE_DETAIL_CUES.get(event_type)
    if cue is None or not isinstance(quote, str):
        return []
    veto = _DETAIL_VETOES.get(event_type)
    end = len(quote) if right is None else right
    return [m for m in cue.finditer(quote, left, end)
            if veto is None or not veto.search(m.group())]


def _sentence_bounds(text, pos):
    left = max(text.rfind(mark, 0, pos) for mark in _SENTENCE_ENDS) + 1
    right = min((p for mark in _SENTENCE_ENDS
                 if (p := text.find(mark, pos)) >= 0), default=len(text))
    return left, right


# After the cue's own bind particle, only event nouns, their particles and
# topic markers may stand between the cue and the event predicate —
# 「会議の開催を実施」 qualifies while 「会議について資料作成を実施」 does
# not, because 資料作成 is not part of the event spine.
_DETAIL_SPINE = re.compile(
    r'について|に関して|として|における|を|が|は|も|に|へ|と|で'
    r'|の(?=' + _DETAIL_EVENT_NOUN + r')'
    r'|(?:\d{4}年\d{1,2}月\d{1,2}日|\d{1,2}月\d{1,2}日|\d{1,2}/\d{1,2})'
    r'(?:\([^)]*\)|（[^）]*）)?(?:に|の|から|より|、)?'
    r'|(?:' + _DETAIL_EVENT_NOUN + r')')


def _detail_event_target(quote, match, event_type):
    """Quote offset where this detail event's own predicate ends.

    A noun-only cue qualifies only when it heads the event predicate inside
    its own event spine (「会議を開催」「会議の開催を実施」「指導を受け」),
    or when the cue match itself already ends in the predicate
    (「意向を確認」「短期入所」). 「会議の資料を作成」「会議について資料
    作成を実施」 describe related material, not the event, and return
    None — the predicate search never crosses a clause boundary or another
    action's object. Non-predicate types keep the cue end as the target.
    """
    if event_type not in _DETAIL_PREDICATES:
        return match.end()
    predicate = _DETAIL_PREDICATES[event_type]
    if re.search(r'(?:' + predicate.pattern + r')$', match.group()):
        return match.end()
    _, sent_right = _sentence_bounds(quote, match.start())
    rest = quote[match.end():sent_right]
    bind = _DETAIL_BIND.match(rest)
    pos = bind.end() if bind is not None else 0
    while True:
        found = predicate.match(rest, pos)
        if found is not None:
            return match.end() + found.end()
        token = _DETAIL_SPINE.match(rest, pos)
        if token is None:
            return None
        pos = token.end()


def _detail_clause_left(quote, pos, floor):
    """Start of the act clause holding ``pos``.

    The clause begins after the last verb+、 boundary before ``pos``, or
    at ``floor`` when none exists — attributes inside one act never bind
    to an event in another.
    """
    left = floor
    for marker in _ACT_END.finditer(quote, floor, pos):
        left = marker.end() + 1
    return left


def _detail_event_instances(quote, event_type, left, right):
    """Predicate-bound same-type instances in ``quote[left:right]``.

    Each entry is ``(cue_match, head_end, target_end)`` — the head is the
    leading event noun region the anchor must share. A compound cue
    absorbed into a wider same-type mention is the same instance, not a
    second one.
    """
    head = _DETAIL_HEAD.get(event_type)
    found = []
    for match in _detail_cue_matches(quote, event_type, left, right):
        target = _detail_event_target(quote, match, event_type)
        if target is None:
            continue
        if any(match.start() < host[2] for host in found):
            continue
        head_end = match.end()
        if head is not None:
            leading = head.match(quote, match.start())
            if leading is not None:
                head_end = leading.end()
        found.append((match, head_end, target))
    if event_type == 'acp_discussion' and len(found) > 1:
        # A compound cue (意向を確認, 終末期の話し合い…) is the same mention
        # as a bare ACP/人生会議 anchor only when it sits inside the bare
        # instance's span or shares its predicate end — a compound with its
        # own predicate is an independent instance and stays counted.
        predicate = _DETAIL_PREDICATES[event_type]
        bare = [entry for entry in found
                if not re.search(r'(?:' + predicate.pattern + r')$',
                                 entry[0].group())]
        if bare:
            found = [entry for entry in found if entry in bare or not any(
                host[0].start() <= entry[0].start() < host[2]
                or entry[2] == host[2] for host in bare)]
    return found


def _care_detail_workflow(detail, *, status=None, negated=None,
                          fact_workflow=None):
    """Demote detail.workflow_status to 'unknown' when axes conflict.

    A performed-family claim cannot ride a planned/negated record, a
    planned-family claim cannot ride an affirmed current/past record, and a
    fact-level workflow_status that disagrees is also a conflict. The detail
    itself stays — only the disputed axis becomes unknown.
    """
    workflow = detail.get('workflow_status')
    family = _workflow_family(workflow)
    if family is None:
        return detail
    conflict = negated is True
    other = _workflow_family(fact_workflow)
    if other is not None and other != family:
        conflict = True
    if status is not None:
        if family == 'performed' and status in ('planned', 'conditional'):
            conflict = True
        if family == 'planned' and status in ('current', 'past') \
                and negated is False:
            conflict = True
    if conflict:
        detail = dict(detail, workflow_status='unknown')
    return detail


# Quote-level context markers shared by timeline projection: corrections,
# retractions, quotations, suppositions, hearsay and contrastive endings
# disqualify the surrounding assertion context (moved from
# care_event_timeline so detail and plain records share one gate).
_UNCERTAIN_CONTEXT = re.compile(
    r"訂正|撤回|誤り|誤記|間違|取り消|取消|引用|仮定|仮に|"
    r"違(?:う|い|った|わ)|事実では|実際には|"
    r"(?:ました|した|です|予定)(?:が|けれど|けど|ものの|、|,)|"
    r"(?:という|との)(?:記載|報告|話)|と(?:聞き|聞いた|書かれ)")
_QUOTE_MARKS = re.compile(r'[「」『』"]')

# Markers that can retract or reframe an assertion at any distance, so the
# whole bounded body is scanned for them: corrections, retractions,
# quotations and third-party attribution. Local-only markers (contrastive
# endings, uncertainty wording, questions, quotation marks) stay scoped to
# the quote's own sentences — distant occurrences cannot be proven
# unrelated only for the classes listed here.
_GLOBAL_CONTEXT = re.compile(
    r"訂正|撤回|誤り|誤記|間違|取り消|取消|引用|仮定|仮に|"
    r"違(?:う|い|った|わ)|事実では|実際には|"
    r"(?:という|との|の|からの)(?:記載|報告|話|伝聞)|"
    r"と(?:聞き|聞いた|書かれ)|によると|いわく")


def context_clean(body, start, end, *, kind=None, subject=None,
                  status=None, negated=None, detail=None):
    """True when the quote's whole context is a clean assertion.

    The sentences the quote touches — plus one neighbour sentence on each
    side — must carry no correction/uncertainty/hearsay marker, quotation
    marks, uncertainty wording or questions, so a correction, denial or
    third-party framing in the immediate context still fails closed. The
    entire bounded body is then scanned for markers that retract or
    reframe an assertion at a distance (corrections, quotations,
    attribution), and for another mention of the same cue with an
    opposing polarity for the same subject and phase — relevance cannot
    be proven otherwise, so the scan is bounded and an exhausted bound
    fails closed. ``kind='other'`` detail anchors compare only mentions
    sharing the same service head, so a different provider's denial stays
    independent.
    """
    if not isinstance(body, str) or not (0 <= start < end <= len(body)):
        return False
    sent_left, _ = _sentence_bounds(body, start)
    _, sent_right = _sentence_bounds(body, end - 1)
    left = sent_left
    right = sent_right
    if left > 0:
        left = _sentence_bounds(body, left - 1)[0]
    if right < len(body) and right + 1 < len(body):
        right = _sentence_bounds(body, right + 1)[1]
    region = body[left:right]
    if (_UNCERTAIN_CONTEXT.search(region) or _QUOTE_MARKS.search(region)
            or _UNCERTAIN.search(region) or _QUESTION.search(region)):
        return False
    if _GLOBAL_CONTEXT.search(body):
        return False
    if subject is None or negated is None or status not in ('current', 'past'):
        return True
    head = None
    if kind == 'other':
        event_type = (detail or {}).get('event_type')
        cue = CARE_DETAIL_CUES.get(event_type)
        anchor = next((m for m in cue.finditer(body, start, end)), None) \
            if cue is not None else None
        if anchor is None:
            return True
        service = re.search(_SERVICE_HEAD, anchor.group())
        head = service.group() if service else anchor.group()
    else:
        cue = _DIRECT.get(kind)
        if cue is None:
            return True
    scanned = 0
    for match in cue.finditer(body):
        if start <= match.start() < end:
            continue
        if head is not None:
            service = re.search(_SERVICE_HEAD, match.group())
            other_head = service.group() if service else match.group()
            if other_head != head:
                continue
        scanned += 1
        if scanned > _EVENT_CANDIDATE_LIMIT:
            return False
        owner, phase = source_axes(body, match.start(), match.end(),
                                   default='unknown')
        if owner != subject or phase != status:
            continue
        _, clause_end = _clause(body, match.start(), match.end())
        polarity = _polarity(body, match.start(), clause_end, match.end(),
                             phase)
        if polarity is not None and polarity != negated:
            return False
    return True


def _attribute_bound(quote, value, event_type, *, actor=False, bound=None):
    """True when ``value`` binds to this event type's own instance.

    Without ``bound`` the occurrence's own act clause — bounded by
    sentence ends and verb+、 boundaries — must hold exactly one
    predicate-bound instance of this event type: a date or provider in a
    different act (「開始し、9月1日に記録」), a neighbouring event, or a
    clause with two same-type instances is never adopted. With ``bound``
    the occurrence must sit inside the selected instance's
    ``(clause_left, target_end)`` span, so every attribute is proven
    against the one instance the record anchors. ``actor`` occurrences
    marked as a transfer/referral source (から/より) describe the
    predecessor, not the provider, and never bind.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    pos = 0
    while True:
        i = quote.find(value, pos)
        if i < 0:
            return False
        end = i + len(value)
        if actor and quote[end:end + 2] in ('から', 'より'):
            pos = i + 1
            continue
        sent_left, sent_right = _sentence_bounds(quote, i)
        if bound is not None:
            left, right = bound
        else:
            left = _detail_clause_left(quote, i, sent_left)
            marker = _ACT_END.search(quote, i)
            right = min(marker.end() + 1, sent_right) if marker is not None \
                else sent_right
            if len(_detail_event_instances(
                    quote, event_type, left, right)) != 1:
                pos = i + 1
                continue
        if not (left <= i and end <= right):
            pos = i + 1
            continue
        candidates = []
        for other_type, cue in CARE_DETAIL_CUES.items():
            candidates.extend((other_type, m) for m in cue.finditer(
                quote, left, right))
        if candidates:
            distance = min(abs(end - m.end()) for _, m in candidates)
            nearest = [(t, m) for t, m in candidates
                       if abs(end - m.end()) == distance]
            if len(nearest) == 1:
                other_type, match = nearest[0]
                veto = _DETAIL_VETOES.get(other_type)
                if (other_type == event_type
                        and (veto is None or not veto.search(match.group()))):
                    return True
        pos = i + 1


def _detail_shared_instance(detail, quote, anchor):
    """Return the shared cue/target span, or None when binding is unproved.

    The anchor's sentence must hold exactly one predicate-bound instance
    of the detail's event type, and the anchor must be that instance's
    own cue (kind=other records) or sit inside its head noun — a visit
    record anchored on 訪問 shares 「訪問看護を開始」 but never
    「サービスを訪問後、開始」 or a neighbouring act's mention. Every
    explicit attribute must bind inside the selected instance's own act
    clause — a date or provider belonging to a different act is a crossed
    pair and does not attach.
    """
    sent_left, sent_right = _sentence_bounds(quote, anchor.start())
    instances = _detail_event_instances(
        quote, detail['event_type'], sent_left, sent_right)
    if len(instances) != 1:
        return None
    cue_match, head_end, target_end = instances[0]
    own = (anchor.start() == cue_match.start()
           and anchor.end() == cue_match.end())
    inside_head = (cue_match.start() <= anchor.start()
                   and anchor.end() <= head_end)
    if not (own or inside_head):
        return None
    clause_left = _detail_clause_left(quote, cue_match.start(), sent_left)
    for field, actor in (('date_text', False), ('service_actor', True)):
        value = detail.get(field)
        if not isinstance(value, str) or value in ('', 'unknown'):
            continue
        if not _attribute_bound(quote, value, detail['event_type'],
                                actor=actor, bound=(clause_left, target_end)):
            return None
    return cue_match.start(), target_end


def verify_care_detail(detail, quote):
    """Return a normalized care_detail bound to the evidence quote, or None.

    event_type must be one of CARE_DETAIL_TYPES, its own cue must appear
    inside the quote, and for noun-only types the cue must head the
    event's own predicate — a held conference, a conducted guidance, a
    respite stay actually used, an updated certification — not merely a
    related noun like meeting materials or a plan sheet. date_text and
    service_actor must occur verbatim and bind to this event type's
    nearest predicate instance — attributes from other sentences,
    neighbouring events or ambiguous same-type pairs are not adopted.
    Axes that are not explicit stay 'unknown' — nothing is inferred or
    normalized.
    """
    if not isinstance(detail, dict) or not isinstance(quote, str):
        return None
    event_type = detail.get('event_type')
    if event_type not in CARE_DETAIL_TYPES:
        return None
    matches = _detail_cue_matches(quote, event_type)
    if not matches:
        return None
    if event_type in _DETAIL_PREDICATES and not any(
            _detail_event_target(quote, m, event_type) is not None
            for m in matches):
        return None
    from semantic_facts import WORKFLOW_STATUSES
    workflow = detail.get('workflow_status', 'unknown')
    if workflow not in WORKFLOW_STATUSES:
        workflow = 'unknown'
    date_text = detail.get('date_text')
    # 'unknown' is this contract's own absent sentinel: a normalized detail
    # re-verified downstream must stay idempotent, not fail the body check.
    if date_text == 'unknown':
        date_text = None
    if date_text is not None and not _attribute_bound(
            quote, date_text, event_type):
        return None
    service_actor = detail.get('service_actor')
    if service_actor == 'unknown':
        service_actor = None
    if service_actor is not None and not _attribute_bound(
            quote, service_actor, event_type, actor=True):
        return None
    return {'event_type': event_type, 'workflow_status': workflow,
            'date_text': date_text if isinstance(date_text, str) and date_text.strip() else 'unknown',
            'service_actor': service_actor if isinstance(service_actor, str) and service_actor.strip() else 'unknown'}


def care_detail_event_day(detail, quote):
    """Written event day for a detail's bound ``date_text``, or None.

    The verbatim ``date_text`` must bind to this event type's own instance
    under the same rule as ``_attribute_bound``, and ``written_day`` must
    resolve it to one full date — a month/day without a year, a relative
    expression or the absent sentinel never becomes an event day.
    """
    if not isinstance(detail, dict) or not isinstance(quote, str):
        return None
    date_text = detail.get('date_text')
    if (not isinstance(date_text, str) or not date_text.strip()
            or date_text == 'unknown'):
        return None
    if re.search(r'本日|今日|昨日|明日|翌日', quote):
        return None
    if not _attribute_bound(quote, date_text, detail.get('event_type')):
        return None
    day = written_day(date_text)
    return day[1].isoformat() if day is not None and day[0] == 'ymd' else None


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
    detail = verify_care_detail(item['care_detail'], quote) if 'care_detail' in item else None
    if item['kind'] == 'other':
        # A detail that does not fit a legacy kind carries its own cue as the
        # anchor instead of depending on _DIRECT (spec §1).
        matches = _detail_cue_matches(quote, detail['event_type']) \
            if detail is not None else []
        matches = matches if len(matches) == 1 else []
    else:
        cue = _DIRECT.get(item['kind'])
        matches = list(cue.finditer(quote)) if cue is not None else []
    if len(matches) != 1:
        result['unverified'] = True
        return result
    match = matches[0]
    shared = _detail_shared_instance(detail, quote, match) if detail is not None else None
    anchor = start + match.start()
    target_end = start + match.end()
    if item['kind'] == 'other' and detail is not None:
        # Noun-only detail cues anchor on the event's own predicate, not
        # the bare noun — polarity and status bind to what was done.
        extended = _detail_event_target(quote, match, detail['event_type'])
        if extended is None:
            result['unverified'] = True
            return result
        target_end = start + extended
    if shared is not None:
        target_end = start + shared[1]
    _, clause_end = _clause(body, anchor, target_end)
    actual_subject, actual_status = source_axes(body, anchor, target_end, default='unknown')
    actual_negated = _polarity(body, anchor, clause_end, target_end, actual_status)
    if item['kind'] == 'exam' and re.fullmatch(OUTPATIENT_CUE, match.group()):
        # Arrival alone does not prove that an examination took place.
        unresolved = True
    correction, ambiguous_subject = _ambiguous_context(body, anchor, target_end, actual_subject)
    if correction or _contradictory_event(body, anchor, target_end, actual_subject, item['kind']):
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
    elif shared is not None:
        # The detail claim must share the verified event's own instance:
        # one predicate-bound same-type instance in the anchor's sentence,
        # with every explicit attribute bound inside that same sentence.
        result['care_detail'] = _care_detail_workflow(
            detail, status=actual_status, negated=actual_negated)
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
    detail = verify_care_detail(fact.get('care_detail'), quote) \
        if isinstance(fact.get('care_detail'), dict) else None
    if kind == 'other' and detail is not None:
        # kind=other carries the detail's own cue as the anchor (spec §1),
        # extended through the event's own predicate for noun-only cues.
        detail_matches = _detail_cue_matches(quote, detail['event_type'])
        match = detail_matches[0] if len(detail_matches) == 1 else None
    else:
        match = cue.search(quote) if cue is not None else None
    target_end = match.end() if match is not None else None
    shared = (_detail_shared_instance(detail, quote, match)
              if detail is not None and match is not None else None)
    if kind == 'other' and detail is not None and match is not None:
        extended = _detail_event_target(quote, match, detail['event_type'])
        if extended is not None:
            target_end = extended
    if shared is not None:
        target_end = shared[1]
    # A different cue in the quote cannot establish examination for an
    # arrival-only statement.
    mismatch = kind == 'exam' and bool(re.search(OUTPATIENT_CUE, statement))
    if match is not None:
        if kind == 'exam' and re.fullmatch(OUTPATIENT_CUE, match.group()):
            mismatch = True
        source_subject, source_status = source_axes(quote, match.start(), target_end, default='unknown')
        if source_subject != subject:
            subject = 'unknown'
        source_negated = _polarity(quote, match.start(), len(quote), target_end, source_status)
        mismatch = mismatch or source_negated is None or source_negated != negated
        if _contradictory_event(quote, match.start(), target_end, source_subject, kind):
            status, negated, mismatch = 'unknown', None, True
        elif source_status == 'unknown':
            status, mismatch = 'unknown', True
        elif status == 'planned' and source_status != 'planned':
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
    if shared is not None and 'unverified' not in record:
        # The detail claim must share the verified event's own instance,
        # and its workflow axis must not contradict the fact-level workflow.
        record['care_detail'] = _care_detail_workflow(
            detail, status=status, negated=negated,
            fact_workflow=workflow)
    return record
