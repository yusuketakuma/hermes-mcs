"""Source-bound completeness, repair acceptance and protected-assertion decisions."""
import clinical_scope
import json
import re

import clinical_values
import extract
import extract_schema
import extract_validation
import inference_version as inference
from mcs_queries import med_is_patient_current
from mcs_util import locate_quote_span

# a clause containing any of these is not a current, still-open statement
_NEGATED_CLAUSE = r"ない|なし|なく|ません|不要|未使用|未測定|中止|済み|ありがとう"


def drop_total(v: dict | None) -> float:
    """Validation-loss score for comparing an output with its repair —
    None (total failure) outranks any drop count so a salvageable
    repair always wins."""
    if v is None:
        return float("inf")
    return (v.get("_evidence_dropped", 0) + v.get("_items_dropped", 0)
            + sum(bool(lab.get("unverified")) for lab in v.get("labs") or []))


_FACT_LIST_FIELDS = ("meds", "symptoms", "labs", "events", "event_records", "requests", "patient_context")


def facts(v: dict | None, split_conditions: bool = False) -> set:
    """Clinical assertions retained independently of prose fields.

    With split_conditions a request's located condition is its own
    fact, so a repair that grounds an unverified request's condition
    still covers the prior request, and losing a located one regresses.
    """
    facts = set()
    for key in _FACT_LIST_FIELDS:
        if key == "events" and (v or {}).get("event_records"):
            continue  # The enum is the compatibility projection of these records.
        for item in (v or {}).get(key) or []:
            if isinstance(item, dict):
                item = {k: x for k, x in item.items()
                        if not k.startswith("_")
                        and k not in ("unverified", "evidence", "normalized")}
                if key == "patient_context":
                    details = item.pop("details", [])
                    for detail in details:
                        facts.add(("patient_context_detail", json.dumps(
                            {"parent": item, "key": detail["key"], "value": detail["value"]},
                            sort_keys=True, ensure_ascii=False)))
                if key == "labs":
                    details = {k: item.pop(k) for k in ("subject", "status", "measured_on", "condition")
                               if k in item}
                    for field, value in details.items():
                        facts.add(("lab_detail", json.dumps(
                            {"parent": item, "key": field, "value": value},
                            sort_keys=True, ensure_ascii=False)))
                if split_conditions and key == "requests" \
                        and "condition" in item:
                    facts.add(("request_condition", json.dumps(
                        item, sort_keys=True, ensure_ascii=False)))
                    item.pop("condition")
            facts.add((key, json.dumps(item, sort_keys=True,
                                       ensure_ascii=False)))
    for key, value in ((v or {}).get("vitals") or {}).items():
        facts.add(("vitals", key, value))
    reply = (v or {}).get("reply")
    if isinstance(reply, dict) and reply.get("kind"):
        facts.add(("reply", reply["kind"]))
    return facts


def _retains_clinical_items(prior, candidate):
    """Retain distinct clinical items with iterative matching."""
    if not isinstance(prior, list):
        return False
    if not prior:
        return True
    if not isinstance(candidate, list):
        return False
    def distinct(items):
        unique = {}
        for item in items:
            identity = {key: value for key, value in item.items()
                        if key not in ('unverified', 'normalized') and not key.startswith('_')} if isinstance(item, dict) else item
            unique.setdefault(json.dumps(identity, sort_keys=True, ensure_ascii=False), item)
        return list(unique.values())
    prior, candidate = distinct(prior), distinct(candidate)
    if len(prior) > len(candidate):
        return False
    assigned, owners = {}, {}
    for start in range(len(prior)):
        queue, seen, via = [start], {start}, {}
        found = False
        for old_index in queue:
            for new_index, item in enumerate(candidate):
                if new_index in via or not retains_assertion(prior[old_index], item):
                    continue
                via[new_index] = old_index
                owner = owners.get(new_index)
                if owner is None:
                    # Move the previous assignments along the augmenting path.
                    while new_index is not None:
                        old_index = via[new_index]
                        previous = assigned.get(old_index)
                        assigned[old_index] = new_index
                        owners[new_index] = old_index
                        new_index = previous
                    found = True
                    break
                if owner not in seen:
                    seen.add(owner)
                    queue.append(owner)
            if found:
                break
        if not found:
            return False
    return True


def improves(candidate: dict | None, prior: dict | None) -> bool:
    """Accept repairs only when retained assertions do not regress."""
    if candidate is None:
        return False
    if prior is None:
        return True
    prior_facts = facts(prior, split_conditions=True)
    candidate_facts = facts(candidate, split_conditions=True)
    # A repair may fill an unknown attribute without changing any known assertion.
    if not retains_assertion({key: value for key, value in prior.items()
                              if key in ("vitals", "reply")}, candidate):
        return False
    for key in _FACT_LIST_FIELDS:
        if prior.get(key) is not None and not _retains_clinical_items(prior[key], candidate.get(key)):
            return False
    if prior.get("urgency") == "high" and candidate.get("urgency") != "high":
        return False
    return (drop_total(candidate), -len(candidate_facts)) < (
        drop_total(prior), -len(prior_facts)) or (
            drop_total(candidate) == drop_total(prior) and candidate_facts != prior_facts)


# One bounded completeness pass for sparse clinical assertions. Prose
# fields never count as recovered facts; multiple items in one field do.
_THIN_BODY_MIN = 300

_THIN_FACTS_MIN = 2


def is_thin(v: dict | None, body: str | None) -> bool:
    # Repeated spans of the same event do not make a sparse result richer.
    if isinstance(v, dict) and v.get("event_records"):
        v = {**v, "event_records": [{k: value for k, value in item.items() if k != "span"}
                                   if isinstance(item, dict) else item for item in v["event_records"]]}
    return (v is not None and body is not None
            and len(body) >= _THIN_BODY_MIN
            and len(facts(v)) < _THIN_FACTS_MIN)


# closed set of repair-trigger codes for the meta histogram: the drops
# buckets (incl. the legacy vitals key) plus the three non-drop triggers
_ISSUE_KEYS = ("ev", "items", "reasons", "labs", "events", "vitals",
               "structure", "completeness", "thin")


def repair_issues(drops: dict, v: dict | None) -> list[str]:
    """Concrete failure list for the repair prompt — evidence quotes
    that failed to locate plus fields whose items violated the schema.
    Empty means nothing actionable to fix (e.g. transport failure)."""
    issues = [f"evidence「{q}」は対象本文に一致しません(完全一致引用に修正)"
              for q in drops.get("ev") or []]
    issues += [f"{s} (vitalsのキーは本文の測定名に合わせてください)"
               for s in drops.get("vitals") or []]
    issues += list(drops.get("labs") or [])
    issues += list(drops.get("reasons") or [])
    issues += [f"event「{e}」の根拠表現を確認できませんでした"
               "（対象本文の言い換え・活用も再確認し、根拠がなければ省いてください）"
               for e in drops.get("events") or []]
    issues += [f"「{f}」の項目がスキーマ違反でした"
               for f in drops.get("items") or []]
    if v is None and not issues:
        issues.append("出力が構造を満たしませんでした")
    return issues


_QUALITY_CHECKPOINT = "completeness-v1"


def completeness_resolved(meta: dict) -> bool:
    """Require explicit per-leaf checks for current inference; only legacy may omit them."""
    version = inference.read_version(meta)
    if version not in (inference.LEGACY_VERSION, inference.CURRENT_VERSION):
        return False
    integrity = meta.get("integrity", {})
    quality = integrity.get("completeness", {}) if isinstance(integrity, dict) else None
    if version == inference.CURRENT_VERSION:
        chunks = integrity.get("completeness_chunks") if isinstance(integrity, dict) else None
        if (type(chunks) is not int or chunks < 1 or not isinstance(quality, dict)
                or len(quality) != chunks):
            return False
    return isinstance(quality, dict) and all(
        isinstance(entry, dict) and entry.get("state") == "hint_gaps_resolved"
        # bool/float zero and missing counts cannot establish resolution.
        and type(entry.get("remaining_hint_candidates")) is int
        and entry["remaining_hint_candidates"] == 0 for entry in quality.values())


def _current_cue(body, surface):
    """A literal patient-current occurrence can invite review without confirming a fact."""
    if not isinstance(surface, str) or not surface:
        return False
    for match in re.finditer(re.escape(surface), body):
        if clinical_scope.patient_source_scope(body, *match.span()) != "patient":
            continue
        left = re.split(r"[。！？!?;；\r\n]", body[:match.start()])[-1]
        right = re.split(r"[。！？!?;；\r\n]", body[match.end():])[0]
        if not re.search(_NEGATED_CLAUSE, left + surface + right):
            return True
    return False


def completeness_issues(v, hints, body):
    """Source-bound missing hints request a reread, never establish clinical facts."""
    if not isinstance(v, dict):
        return []
    issues = _pharmacy_gaps(v, body)
    if not isinstance(hints, dict):
        return issues

    for key, value in clinical_values.patient_current_vitals(hints.get("vitals") or {}, body).items():
        if key not in (v.get("vitals") or {}) and any(
                float(match[0].replace("．", ".")) == value and _current_cue(body, match[0])
                for match in re.finditer(clinical_values.VITAL_NUMBER_TOKEN, body)):
            issues.append(f"本文の測定候補 {key}={value} が未収載です。原文で再確認してください")
    names = {med.get("name") for med in v.get("meds") or []
             if med_is_patient_current(med) and clinical_values.patient_item_scope(
                 med, body, surface=med.get("name")) == "patient"
             and _current_cue(body, med.get("evidence"))}
    for med in hints.get("medications") or []:
        name = med.get("name") if isinstance(med, dict) else None
        if (isinstance(name, str) and not extract_validation.nonspecific_med_name(name)
                and name not in names and _current_cue(body, name)):
            issues.append(f"本文の薬剤候補「{name}」が未収載です。原文で再確認してください")
    issues += _rx_action_gaps(v, hints, body)
    # The rule hints' confirm/contact patterns also fire on reports (確認を終えた);
    # the per-sentence cues apply the same request-ending test to every kind.
    for request in _request_cues(body):
        if (isinstance(request, dict) and request.get("kind") in _REQUEST_STEMS
                and isinstance(request.get("ctx"), str)
                and _request_current(body, request["ctx"], request["clause"])
                and not any(_request_covers(item, request) for item in v.get("requests") or [])):
            issues.append("本文の確認・連絡・質問候補が未収載です。原文で再確認してください")
            break
    return issues


_PHARMACY_DETAIL_CUES = {
    "support_method": re.compile(r"一包化|分包"),
    "storage_status": re.compile(r"(?:冷蔵|室温|冷凍)(?:で)?保管|薬(?:剤)?の保管状況"),
    "storage_place": re.compile(r"冷蔵庫|(?:服薬|薬)カレンダー|配薬ボックス|お薬箱|薬(?:剤)?の(?:保管場所|置き場)"),
    "medication_manager": re.compile(r"(?:薬(?:剤)?の管理|服薬管理)[^。！？!?;；\n]{0,60}(?:が|担当|行)"),
    "inquiry_result": extract_validation.EVENT_CUES["prescription_inquiry"],
    "interaction_check": re.compile(r"相互作用|重複(?:投薬|処方)"),
}


def _pharmacy_gaps(v, body):
    """Ask once about missing source-local pharmacy details, without creating any facts."""
    issues = []
    for sentence in re.finditer(r"[^。！？!?;；\n]+", body):
        text = sentence[0]
        if not re.search(r"薬(?!剤師|局)|処方|内服|服薬|インスリン|一包化|分包", text):
            continue
        if (re.search(r"(?:薬局|病院|事業所)の(?:冷蔵庫|棚|保管庫)", text)
                and not re.search(r"本人|患者|利用者", text)):
            continue
        for key, pattern in _PHARMACY_DETAIL_CUES.items():
            for cue in pattern.finditer(text):
                if clinical_scope.patient_source_scope(body, sentence.start() + cue.start(),
                                                sentence.start() + cue.end()) in ("family", "other", "unknown"):
                    continue
                if key == "storage_place" and not re.search(r"保管|置き|置い", text):
                    continue
                if key == "support_method" and re.match(
                        r"(?:を|は|が|も)?\s*(?:検討|希望|予定|不要|なし|され?ていません|していません)",
                        text[cue.end():]):
                    continue
                if key == "interaction_check" and not re.search(r"確認|チェック|照合|点検|調べ", text):
                    continue
                if key == "inquiry_result":
                    if "prescription_inquiry" not in (v.get("events") or []):
                        issues.append("本文の薬剤の疑義照会・処方提案がeventsに未収載です。原文で再確認してください")
                    if not re.search(r"回答|返答|了承|了解|承認|見送|変更なし|結果|未確認", text):
                        continue
                covered = False
                for item in v.get("patient_context") or []:
                    if (not isinstance(item, dict) or item.get("category") != "medication_management"
                            or item.get("subject") != "patient"):
                        continue
                    for detail in item.get("details") or []:
                        if not isinstance(detail, dict) or detail.get("key") != key:
                            continue
                        span = locate_quote_span(body, detail.get("evidence"))
                        if span is None or not (span[0] < sentence.end() and sentence.start() < span[1]):
                            continue
                        # A calendar is another support method; it does not cover omitted packaging.
                        if key == "support_method" and cue[0] not in str(detail.get("value", "")):
                            continue
                        covered = True
                        break
                    if covered:
                        break
                if not covered:
                    issues.append(f"本文の服薬管理候補「{cue[0]}」の詳細 {key} が未収載です。原文で再確認してください")
                break
    return issues


# v1 rx_actions' own change words. Each is met by its own action only: a generic
# 変更 states no direction, and an increase or decrease keeps its direction.
_RX_WORDS = {"start": "開始|追加|再開|導入", "stop": "中止|停止|終了|中断|休薬", "decrease": "減量",
             "increase": "増量", "change": "変更(?!なし)"}

_RX_ANY = "|".join(_RX_WORDS.values())

# 変更なし states no change: never a change to cover, but kept as a position so an
# extracted change of that drug has nothing to rest on (Aは変更なし beside A/change)
_RX_NO_CHANGE = r"変更(?:なし|無し)"

# A non-current change is denied, done, planned or weighed right at its own word:
# 中止しません, 中止しなかった, 中止はしない, 変更無し, 中止済み, 開始予定, 中止は見送り.
# A later predicate (中止し経過観察予定) or earlier text (副作用なしで) is something else.
# いたし/致し (開始いたします) is a polite form of し: it keeps the meaning that follows
_RX_VERB = r"(?:を|は|も|が|の)?\s*(?:いた|致)?(?:し|する|して|され)?"

_RX_NOT_CURRENT = (_RX_VERB +
                   r"(?:ない|なく|なかっ|ません|せず|無し|なし|不要|予定|検討|見送|拒否|ありません"
                   # 中止を撤回, 開始を取り消し, 中止を取りやめ: the change is withdrawn
                   r"|撤回|取り?消|取消|取り?やめ|とりやめ|白紙"
                   # 中止していません, 中止されていない (not 開始しています: ongoing, current)
                   r"|て?い(?:ない|ません|なかっ)"
                   # できる/出来る states no done change (中止できません, できるか確認中, できます,
                   # できましたか, できたら) except its plain affirmative past (中止できました, できた)
                   r"|(?:でき|出来)(?!ました(?!か)|た(?!か|ら|ろう))"
                   # a question asks nothing either: 開始しますか, 中止しましたか
                   r"|ますか|ましたか|たか|でしょうか|か[？?])")

# 開始予定, 開始を検討: not done, but no denial either (a planned item may state it)
_RX_UNDECIDED = _RX_VERB + r"(?:予定|検討)"

# A requested, consulted or not yet done change is checked as planned, never as done:
# 開始してください, 追加を相談, 開始します
_RX_PLANNED = (_RX_VERB + r"(?:(?:主治医|処方医|医師|薬剤師|看護師)(?:へ|に)\s*)?"
               r"(?:ください|下さい|ほしい|欲しい|お願い|相談|依頼|(?:処方)?提案|ます(?!た|か))")

# 明日から架空薬Aを開始: a future time in the change's own clause
_RX_FUTURE = r"明日|明後日|来週|来月|今後|次回|後日|翌日|今度|週明け"

# 明日の退院に備えて本日開始しました: a nearer current time decides
_RX_NOW = r"本日|今日|昨日|昨夜|今朝|先ほど|先程|現在|先日|今回"

# 中止薬, 開始薬, 追加薬: the word is part of a noun, not a change
_RX_NOUN_AFTER = r"薬|剤"

# a name inside a longer one (架空薬A in 架空薬AA, 薬ZA in 架空薬ZA) is no occurrence of it;
# a form after it (アムロジピン錠) is
_RX_NAME_CHAR = r"[A-Za-zＡ-Ｚａ-ｚ0-9０-９ァ-ヶー一-龠々]"


# a class label before the product (降圧薬アムロジピン, 抗菌薬セフトリアキソン)
_RX_CLASS = (r"(?:降圧|抗菌|抗生|睡眠|鎮痛|解熱鎮痛|解熱|抗凝固|抗血小板|利尿|緩下|抗精神病|抗うつ"
             r"|抗不安|抗てんかん|抗アレルギー|糖尿病|整腸|制吐)(?:薬|剤)")


def _rx_bounded(name, text):
    """``name`` occurs in ``text`` as itself, not only inside a longer word."""
    return any(not re.match(_RX_NAME_GOES_ON, text[m.end():]) and not _rx_glued(text[:m.start()])
               for m in re.finditer(re.escape(name), text))


def _rx_glued(before):
    """The text before a name runs into it (架空 before 薬ZA); a time word written
    straight before a drug (本日架空薬A) does not."""
    return bool(re.search(_RX_NAME_CHAR + "$", before) and not re.search(
        rf"(?:{_RX_NOW}|{_RX_FUTURE}|当日|毎日|同日|{_RX_CLASS})$", before))

# AとBを開始, A5mgと架空薬Bを開始: a listing particle beside a known drug (and its dose)
_RX_LIST = r"(?:と|や|・|及び|および)"

_RX_NAME_GOES_ON = (rf"(?!錠|散|液|軟膏|テープ|カプセル|注|点眼|貼付|{extract_schema.RX_DOSE_ONE}|{_RX_ANY}|内服|服用|投与"
                    r"|及び)" + _RX_NAME_CHAR)

_RX_ADMIN = r"(?:の(?:投与|内服|服用|使用|投薬|処方)\s*(?:を|は|も|が)?\s*)?"

_RX_ACTOR = (r"(?:(?:主治医|医師|先生|看護師|薬剤師|訪問医|母|父|妻|夫|娘|息子|家族|本人)"
             r"(?:が|より|の判断で)\s*)?")

_RX_ADVERB = (r"(?:(?:(?:本日|今日|昨日|今朝|明日|明後日|来週|来月|今後|次回|後日|翌日|今度|週明け)"
              r"(?:から|より|に)?|先ほど|先程|いったん|一旦|一時|再度|再び|すぐに?|直ちに"
              r"|既に|すでに|新たに|新規に)\s*(?:[、，,]\s*)?)?")

# Aは副作用のため中止, Aは発疹が出たので中止: the reason sits between the drug and its change
_RX_CAUSE = r"(?:[^\s。！？、を]{1,12}?(?:のため|ため|ので)\s*、?\s*)?"

_RX_BETWEEN = _RX_ADMIN + _RX_CAUSE + _RX_ACTOR + _RX_ADVERB

# 2.5mgから5mgへ増量, 1日2回から1回へ減量: a regimen range between the drug and its word
_RX_AMOUNT = r"(?:1日|一日)?\s*[0-9０-９.．]+\s*(?:mg|μg|mcg|g|mL|ml|錠|%|単位|回|包|枚|滴|カプセル)"

_RX_RANGE = rf"(?:(?:{_RX_AMOUNT})\s*から\s*)?(?:{_RX_AMOUNT})\s*(?:へ|に|まで)?\s*"

# AとB, A、B: a listing separator between two named drugs
_RX_SEP = r"(?:と|や|・|及び|および|、|，|,)"

# 服薬指導, 投薬管理: medication services, not drugs
_RX_SERVICE = r"(?:服薬|投薬|薬剤|与薬|薬)(?:指導|管理|支援|確認|相談|説明|調整|カレンダー)"

_RX_WORD_CODE = {w.split("(")[0]: c for c, rx in _RX_WORDS.items() for w in rx.split("|")}

_RX_CONFLICT = {"start", "stop", "decrease", "increase", "change"}

# Aと共に運動療法を開始: A accompanies; the change's object is what follows
_RX_ALONG = r"と(?:共に|ともに|同時に|一緒に|いっしょに|併せて|あわせて)"

# an unnamed object reads as a medication only with v1's own medication words
_RX_MED_WORD = r"薬|処方|内服|外用|点眼|貼付|錠|剤|坐薬|座薬|注射"


def _rx_med_like(text):
    """An unnamed object reads as a medication: v1's medication words or a dose."""
    text = re.sub(_RX_SERVICE, "", text)
    return bool(re.search(_RX_MED_WORD, text) or re.search(extract_schema.RX_DOSE_ONE, text))


def _rx_objects(region, spans, code):
    """The change word's objects among named drug spans, right to left, with each
    one's role: None, "from" (AからB / AをBに変更) or "to". A drug under another
    predicate (Aを継続しBを中止) is no object. Also returns the region spans of
    listed neighbours that are no known drug but read as a medication (架空薬Aと架空薬Bを)."""
    objects, missed = [], []
    for s, e in reversed(spans):
        rest = region[e:]
        if not objects:
            to = code == "change" and re.fullmatch(extract_schema.RX_DOSE + r"\s*[へに]\s*", rest)
            listed = re.match(extract_schema.RX_DOSE + r"\s*" + _RX_LIST + r"\s*", rest)
            if to or re.fullmatch(extract_schema.RX_DOSE + extract_schema.RX_PARTICLE + _RX_BETWEEN + f"(?:{_RX_RANGE})?", rest):
                objects.append((s, e, "to" if to else None))
            elif listed:
                # Aと<unknown>(と<unknown>…)を開始: A is listed; each unknown member is
                # a drug only if it reads as one
                tail = e + listed.end()
                missed += [(tail + m.start(), tail + m.end())
                           for m in re.finditer(r"(?:(?!" + _RX_LIST + r").)+", region[tail:])
                           if _rx_med_like(m.group(0))]
                objects.append((s, e, None))
            continue
        ns, _, role = objects[-1]
        between = region[e:ns]
        # AとBを, A、Bを, 本人のAと母のBを: the next drug's own owner may follow the separator
        if re.fullmatch(extract_schema.RX_DOSE + r"\s*" + _RX_SEP + r"\s*(?:[^\s、，,をはもがのと]{1,10}の)?\s*", between):
            objects.append((s, e, role))
        elif role == "to" and re.fullmatch(extract_schema.RX_DOSE + r"\s*(?:から|を)\s*", between):
            objects.append((s, e, "from"))
    if objects:
        first = objects[-1][0]
        before = re.search(_RX_LIST + r"\s*$", region[:first])
        if before:
            # <unknown>とAを開始
            word = re.search(r"[^、をはもがの\s]*$", region[:before.start()])
            if _rx_med_like(word.group(0)):
                missed.append(word.span())
    return objects, missed


def _named_in(text, names):
    """Names written in ``text``, longest first, so 架空薬A inside 架空薬AA is not a second drug."""
    found, taken = [], []
    for name in sorted(names, key=len, reverse=True):
        for m in re.finditer(re.escape(name), text):
            if not any(m.start() < e and s < m.end() for s, e in taken):
                taken.append(m.span())
                if name not in found:
                    found.append(name)
    return found


def _rx_positions(hints, names, body):
    """Change words to check, as (start, end, code, unknown): every v1 rx_action
    located in the body, plus a known drug written directly before a change word that
    v1 did not hint (本人のカロナールを中止: no medication keyword beside the brand).
    ``unknown`` marks a fallback whose source word is longer than the known name
    (ロナール in カロナール, カロナールA in カロナールAA): a drug never extracted."""
    seen = set()
    for act in hints.get("rx_actions") or []:
        code = act.get("action") if isinstance(act, dict) else None
        ctx = act.get("ctx") if isinstance(act, dict) else None
        word = _RX_NO_CHANGE if code == "no_change" else _RX_WORDS.get(code)
        if not word or not isinstance(ctx, str) or not ctx:
            continue
        # v1 cuts ctx RX_CTX_BACK characters before its change word
        words = [m for m in re.finditer(word, ctx) if m.start() <= extract.RX_CTX_BACK]
        for found in re.finditer(re.escape(ctx), body) if words else ():
            start = found.start() + words[-1].start()
            if re.match(_RX_NOUN_AFTER, body[found.start() + words[-1].end():]):
                continue
            if start not in seen:
                seen.add(start)
                yield start, found.start() + words[-1].end(), code, False
    for name in sorted(names, key=len, reverse=True):
        rx = (re.escape(name) + f"((?:{_RX_NAME_GOES_ON})*)" + extract_schema.RX_DOSE + extract_schema.RX_PARTICLE + _RX_BETWEEN
              + f"(?:{_RX_RANGE})?" + r"(" + _RX_NO_CHANGE + "|" + _RX_ANY + r")")
        for m in re.finditer(rx, body):
            code = "no_change" if re.fullmatch(_RX_NO_CHANGE, m.group(2)) else _RX_WORD_CODE.get(m.group(2))
            if code and m.start(2) not in seen and not re.match(_RX_NOUN_AFTER, body[m.end(2):]):
                seen.add(m.start(2))
                yield m.start(2), m.end(2), code, bool(m.group(1)) or _rx_glued(body[:m.start()])


def _rx_bindings(hints, names, body, *, every=False, ground=False):
    """Each patient-current rule change word's objects, as (name, allowed actions,
    (clause start, clause end, planned)); a planned change (requested, consulted,
    future) is met by a planned med, a done one by a current or past med. A name of None is an unnamed object; a listed neighbour that reads
    as a medication but is no known drug yields ("?", ...), a drug never extracted.
    With ``every`` the denied, past and other-person changes are kept too, so a
    caller can count every change the source states for a drug. With ``ground`` the
    patient's past and undecided (予定, 検討) changes are kept, as planned for the
    latter, to support an extracted action; denials, conditions and other people's
    changes never do. The span also carries the change word's own position."""
    # The shared scope reads 時に in と同時に as a condition; for the change's own
    # scope it accompanies like と一緒に (same length, so positions hold)
    scoped = body.replace("と同時に", "と一緒に")
    # 入院時に開始しました: an event time of a done change reads as a condition (時に) to the
    # shared scope; it is the day of that event (same length, so positions hold)
    scoped = re.sub(r"(?<=入院|退院|転院|来院|受診|往診|訪問|手術|診察)時に(?=[^。！？]*?(?:しました|した[。、]|しています))",
                    "当日", scoped)
    carry = {}      # sentence head -> (end of its last change word, that change's drugs)
    for start, end, code, unknown in sorted(_rx_positions(hints, names, body)):
        if code == "no_change" and not every:
            continue        # no change asks nothing and supports no extracted change
        head = max(body.rfind(c, 0, start) for c in "。！？!?;；\r\n") + 1
        tail = re.search(r"[。！？!?;；\r\n]", body[end:])
        tail = end + tail.start() if tail else len(body)
        mark = re.search(_CLAUSE_MARK, body[end:tail])
        lead = head + _clause_start(body[head:start])
        close = end + mark.start() if mark else tail
        while True:
            # から and 、 are no clause break inside AからBへ変更, 2.5mgから5mgへ増量,
            # 明日から開始 or A5mg、Bを開始: the drug before them belongs to this change
            joined = (re.search(extract_schema.RX_DOSE + "から$", body[head:lead]) if code == "change" else None)
            joined = (joined if joined and body[head:head + joined.start()].endswith(tuple(names))
                      else re.search(f"(?:{_RX_AMOUNT}|{_RX_FUTURE}|{_RX_NOW})\\s*から$", body[head:lead])
                      or re.search(extract_schema.RX_DOSE + (
                          extract_schema.RX_PARTICLE + _RX_BETWEEN if re.fullmatch(_RX_BETWEEN, body[lead:start]) else "")
                          + r"\s*[、，,]$", body[head:lead])
                      # 副作用のため中止: a reason followed directly by the change (an actor or
                      # adverb at most) keeps the drug before it; an object after the reason
                      # (架空薬Bを, 運動療法を) is that change's own
                      or (re.search(r"(?:のため|ため|ので)\s*、?$", body[head:lead])
                          if re.fullmatch(r"\s*" + _RX_ACTOR + _RX_ADVERB, body[lead:start]) else None))
            if not joined or not (re.search(r"(?:から|ため|ので)\s*、?$", joined.group(0))
                                  or body[head:head + joined.start()].endswith(tuple(names))):
                break
            previous = lead
            lead = head + _clause_start(body[head:head + joined.start()])
            if lead >= previous:
                break
        # a condition, history or plan qualifies the whole clause (もし…場合, 以前に…);
        # whose drug it is, is decided per drug below (本人のAと母のBを開始)
        clause_scope = clinical_scope.patient_source_scope(scoped, start, end)
        not_current = bool(re.match(_RX_NOT_CURRENT, body[end:close]))
        # 中止する予定はありません, 中止を検討していません: a denied plan is no plan
        weighed = re.match(_RX_UNDECIDED, body[end:close])
        undecided = bool(weighed) and not re.match(
            r"\s*(?:は|も|が)?\s*(?:あり|し|して|され)?\s*(?:ない|ません|なし|無し|て?い(?:ない|ません|なかっ))",
            body[end + weighed.end():close])
        denied = not_current and not undecided
        if ground and (denied or clause_scope in ("conditional", "unknown")):
            continue            # history and 予定 may support an item; a denial or condition not
        if not (every or ground) and (not_current or clause_scope in ("conditional", "past", "unknown")):
            continue
        # から is no break for a time (明日から), but 通院予定ですが、 is another clause
        breaks = list(re.finditer(r"ので|ため|けど|けれど|が、|、", body[head:start]))
        asked = re.match(_RX_PLANNED, body[end:close])
        # "consult" (追加を相談) is no instruction: it is checked but never repaired
        when = body[head + (breaks[-1].end() if breaks else 0):start]
        future = [m.start() for m in re.finditer(_RX_FUTURE, when)]
        now = [m.start() for m in re.finditer(_RX_NOW, when)]
        planned = ("consult" if asked and re.search(r"相談|提案", asked.group(0)) else bool(
            asked or undecided or clause_scope == "planned" or (future and (not now or future[-1] > now[-1]))))
        # this change word's objects: AとBを開始 names two, Aを開始しBも開始 one each
        prior = list(re.finditer(_RX_ANY, body[lead:start]))
        base = lead + (prior[-1].end() if prior else 0)
        region = body[base:start]
        if not every and re.search(r"の\s*" + _RX_SERVICE + extract_schema.RX_PARTICLE + "$", region):
            continue            # A5mgの服薬指導を開始: the service starts, not the drug (still counted,
                                # so an extracted start of the drug finds no ground)
        along = list(re.finditer(_RX_ALONG, region))
        if along:
            base, region = base + along[-1].end(), region[along[-1].end():]
        spans = []
        for name in _named_in(region, names):
            spans += [(m.start(), m.end(), name) for m in re.finditer(re.escape(name), region)
                      if not _rx_glued(body[:base + m.start()])]
        spans = sorted({(s, e) for s, e, _ in spans if not any(
            s2 <= s and e <= e2 and (s2, e2) != (s, e) for s2, e2, _ in spans)})
        objects, missed = _rx_objects(region, spans, code)
        # a missed neighbour counts only as the patient's (本人のAと母のBを開始: B is the mother's)
        if any(every or clinical_scope.patient_source_scope(scoped, base + max(
                [ms] + [ms + m.end() for m in re.finditer(r"の[ \t　]*", region[ms:me])]), base + me) == "patient"
               for ms, me in missed):
            yield "?", set(), (lead, close, planned, denied, start)
        # Aを開始後、中止 / Aを開始し、その後中止: the same sentence's last drugs carry over
        before = carry.get(head)
        carried = (not objects and not along and before and before[0] <= start and re.fullmatch(
            r"(?:後|し|して|した後)?\s*[、,]?\s*(?:その後|そののち|後に|のち|続けて|続いて)?\s*[、,]?\s*",
            body[before[0]:start]) and before[1])
        if carried:
            for name in carried:
                yield name, {code}, (lead, close, planned, denied, start)
            carry[head] = (end, carried)
            continue
        if not objects and unknown:
            # only a fragment of a longer source word is known: that drug was never extracted
            if every or clinical_scope.patient_source_scope(scoped, start, end) == "patient":
                yield "?", set(), (lead, close, planned, denied, start)
            continue
        if not objects:
            if along:
                # 本人Aと共に架空薬Bを開始 asks for B; Aと共に運動療法を開始 asks nothing
                if _rx_med_like(region) and (every or clinical_scope.patient_source_scope(
                        scoped, base, start) == "patient"):
                    yield "?", set(), (lead, close, planned, denied, start)
                continue
            if not every and not _rx_med_like(body[lead:close]):
                continue    # リハビリを開始, or a neighbouring clause's drug (…内服中。心停止)
            objects = [(None, None, None)]
        kept = []
        for s, e, role in objects:
            name = region[s:e] if s is not None else None
            # each drug's own owner decides it: the nearest の before it, read up to the
            # drug itself (本人の架空薬Aと母の架空薬Bを開始 keeps A, 母の付き添いで本人の架空薬A keeps A)
            at, until = (base + s, base + e) if name else (start, end)
            last = list(re.finditer(r"の[ \t　]*", body[lead:at]))
            # the patient's own past (以前にAを開始) may support an item, never another person's
            if not every and any(clinical_scope.patient_source_scope(scoped, a, until) not in (
                    ("patient", "past", "planned") if ground else ("patient",))
                                 for a in ([lead + last[-1].end()] if last else []) + [at]):
                continue
            if name:
                kept.append(name)
            # AからBへ / AをBに変更: the source drug stops and the destination starts;
            # a generic change on either would leave the replaced drug active
            allowed = {"from": {"stop"}, "to": {"start"}}.get(role, {code})
            yield name, allowed, (lead, close, planned, denied, start)
        carry[head] = (end, kept)


def _rx_names(v, hints):
    """Candidate names from rules and the model, without confirming their drug category.

    A rule's class label (降圧薬アムロジピン) never outranks the product name."""
    def named(items):
        return [m["name"] for m in items or [] if isinstance(m, dict) and isinstance(m.get("name"), str) and m["name"]]
    return {re.sub("^" + _RX_CLASS + "(?=.)", "", n) for n in named(hints.get("medications"))} | set(named(v.get("meds")))


def _rx_action_names(names, hints, body):
    """Names with independent medication cues for adding or replacing an action."""
    rule_names = _rx_names({}, hints if isinstance(hints, dict) else {})
    # A model-only name may invite review, but cannot justify a new medication action.
    return {name for name in names if _rx_bounded(name, body) and (
        name in rule_names or _rx_med_like(name) or any(
            not _rx_glued(body[:m.start()]) and (
                re.match(extract_schema.RX_DOSE_ONE, body[m.end():]) or re.search(_RX_CLASS + r"\s*$", body[:m.start()]))
            for m in re.finditer(re.escape(name), body)))}


def _rx_covering(v, planned=False):
    """Meds that may state a change: verified, explicitly the patient's, not denied,
    and done (current or past) or, for a planned change, planned."""
    return [med for med in v.get("meds") or [] if isinstance(med, dict)
            and med.get("subject") == "patient" and not med.get("negated")
            and not med.get("unverified")
            and med.get("status") in (("planned",) if planned else ("current", "past"))
            and isinstance(med.get("name"), str) and med["name"]
            and isinstance(med.get("evidence"), str) and med["evidence"]]


def _rx_grounds(hints, names, body):
    """Named patient changes an extracted action may rest on (history and 予定 count;
    denials, withdrawals, conditions and other people's changes do not)."""
    return [(t, ok, lead, close, pl) for t, ok, (lead, close, pl, *_) in _rx_bindings(
        hints, names, body, ground=True) if t not in (None, "?")]


def _rx_grounded(med, grounds, body):
    """The med's action rests on a named change of that drug in a fitting state: planned
    for a planned one, and a done (even historical) stop is past (以前に中止: stop/past)."""
    return any(
        med["action"] in ok and t == med["name"]
        and (med.get("status") == "planned") == bool(pl)
        and (pl or med["action"] != "stop" or med.get("status") == "past")
        and any(m.start() < close and m.end() > lead for m in re.finditer(re.escape(med["evidence"]), body))
        for t, ok, lead, close, pl in grounds)


def _rx_action_gaps(v, hints, body):
    """Patient-current rule change words whose clause no extracted patient med states.

    A name alone never covers a change: every drug named since the clause's
    previous change word needs its own verified patient med with the matching
    action, quoted over the clause, and no action its sentence never states.
    A listed drug the extraction never named keeps the gap open. Other people,
    past, planned and denied changes ask nothing."""
    names = _rx_names(v, hints)
    bindings = list(_rx_bindings(hints, names, body))
    # every extracted action needs a change the source states for that drug and does
    # not deny (Bは内服中 beside Aを中止 grounds no stop of B); history still counts,
    # but a denial (中止しません) or no change word at all grounds nothing
    # only a named patient object proves its action: an unnamed one (心停止 beside the
    # drug) or another person's or a conditional change proves nothing
    stated_any = list(_rx_bindings(hints, names, body, every=True))
    grounds = _rx_grounds(hints, names, body)
    if stated_any and any(
            med.get("action") in _RX_CONFLICT and not _rx_grounded(med, grounds, body)
            for planned in (False, True) for med in _rx_covering(v, planned)):
        return ["本文の薬剤の開始・中止・変更候補が未収載か異なる操作です。原文で再確認してください"]
    # a repair keeping a fragment (ロナール beside カロナール) beside the right name
    # still names a drug the source never writes on its own
    if bindings and any(med.get("action") in _RX_CONFLICT and med["name"] in body
                        and not _rx_bounded(med["name"], body)
                        for planned in (False, True) for med in _rx_covering(v, planned)):
        return ["本文の薬剤の開始・中止・変更候補が未収載か異なる操作です。原文で再確認してください"]
    # 架空薬Aを開始し、架空薬Aを中止: every action the source binds to that same drug,
    # done and planned apart, so a requested stop never excuses a recorded done stop
    stated = {}
    for target, allowed, (_, _, planned, _, _) in bindings:
        stated.setdefault((target, bool(planned)), set()).update(allowed)
    for target, allowed, (lead, close, planned, _, at) in bindings:
        # an unnamed object (Aを内服中に心停止) is matched to no drug: fail closed
        mine = [med for med in _rx_covering(v, planned) if target is not None
                and med["name"] == target
                and any(m.start() < close and m.end() > lead
                        for m in re.finditer(re.escape(med["evidence"]), body))]
        # an item of the other state (done beside a planned change, or planned beside a
        # done one) contradicts unless the source states that action in that state too
        others = [med for med in _rx_covering(v, not planned)
                  if target is not None and med["name"] == target
                  and any(m.start() < close and m.end() > lead
                          for m in re.finditer(re.escape(med["evidence"]), body))]
        if any(med.get("action") in _RX_CONFLICT - stated.get((med["name"], not planned), set())
               for med in others):
            return ["本文の薬剤の開始・中止・変更候補が未収載か異なる操作です。原文で再確認してください"]
        if planned == "consult":
            # 追加を相談: a planned item is enough, with no action; a planned action the
            # consultation does not name (相談した追加に中止) still contradicts it
            if not mine or any(med.get("action") in _RX_CONFLICT - allowed
                               - stated.get((med["name"], True), set()) for med in mine):
                return ["本文の薬剤の開始・中止・変更候補が未収載か異なる操作です。原文で再確認してください"]
            continue
        # a wrong action kept beside the right one is a contradiction, not a repair;
        # a done stop is past (中止しました), so a stop still current contradicts it.
        # An item wrong here may be right for another change of the same drug
        # (開始し、中止し、その後開始: one start past, one current), so it contradicts
        # only when no change of that drug and action takes it
        def right_somewhere(med):
            # the other change must be one the item's own quote covers: a narrow quote of
            # 再開 never borrows the earlier occurrence's past start
            return any(t == target and med.get("action") in ok and pl == planned
                       and _rx_right(med, ok, pl, _rx_later(bindings, t, pl, st))
                       and any(m.start() < cl and m.end() > ld
                               for m in re.finditer(re.escape(med["evidence"]), body))
                       for t, ok, (ld, cl, pl, _, st) in bindings)
        right = [_rx_right(med, allowed, planned, _rx_later(bindings, target, planned, at)) for med in mine]
        if (not any(right)
                or any(med.get("action") in allowed and not ok and not right_somewhere(med)
                       for med, ok in zip(mine, right))
                # 中止しました beside a neutral current item: readers take the last item of a
                # drug, so the neutral one would keep the stopped drug current
                or (not planned and "stop" in allowed and any(
                    med.get("action") in (None, "none") and med.get("status") == "current" for med in mine))
                or any(med.get("action") in _RX_CONFLICT - allowed - stated.get((med["name"], bool(planned)), set())
                       for med in mine)):
            return ["本文の薬剤の開始・中止・変更候補が未収載か異なる操作です。原文で再確認してください"]
    return []


def _rx_later(bindings, target, planned, at):
    """{"stop"} when the source stops ``target`` after the change at ``at``
    (開始し、その後中止: the start may be past); 中止し、その後開始 restarts it."""
    return {"stop"} if any(t == target and "stop" in ok and pl == planned and st > at
                           for t, ok, (_, _, pl, _, st) in bindings) else set()


def _rx_right(med, allowed, planned, later=()):
    """The med states the bound change: its action, and for a done change its status:
    a stop is past; a start or a dose change current unless ``later`` holds a stop the
    source states after it (架空薬Aを開始し、その後中止: both past)."""
    action, status = med.get("action"), med.get("status")
    return action in allowed and (planned or (
        status == "past" if action == "stop"
        else status == "current" or (status == "past" and "stop" in later)))


def _rx_superseded(prior, candidate, hints, body):
    """Prior medication facts a repair may replace: the source states exactly one
    change for the drug (denied, past and other-person ones counted too), that change
    is patient-current, the prior item quotes its clause and states no action or one
    that change excludes, and the candidate carries the same item with an allowed
    action instead. Every other field (dose, freq, route, subject, negation) must stay equal."""
    if (not isinstance(hints, dict) or not isinstance(prior, dict) or not isinstance(candidate, dict)
            # v1 keeps 10 rx_actions: a full list cannot prove a drug has only one change
            or len(hints.get("rx_actions") or []) >= extract.RX_ACTIONS_CAP):
        return set()
    names = _rx_names(candidate, hints) | _rx_names(prior, hints)
    action_names = _rx_action_names(names, hints, body)
    stated = {}
    for target, _, _ in _rx_bindings(hints, names, body, every=True):
        stated[target] = stated.get(target, 0) + 1
    current = {}
    for target, allowed, (lead, close, planned, *_) in _rx_bindings(hints, names, body):
        current.setdefault(target, []).append((allowed, (lead, close), planned))

    def rest(med):
        return {k: x for k, x in med.items() if not k.startswith("_")
                and k not in ("action", "status", "evidence", "unverified", "normalized")}

    kept = facts({"meds": candidate.get("meds") or []}, split_conditions=True)
    gone = set()
    for med in prior.get("meds") or []:
        name = med.get("name") if isinstance(med, dict) else None
        if name not in action_names or stated.get(name) != 1 or len(current.get(name, [])) != 1:
            continue                    # several changes or none for this drug: no replacement
        (allowed, (lead, close), planned), = current[name]
        if planned == "consult":
            continue                    # a consultation instructs nothing to repair toward
        quote = med.get("evidence")
        fact = next(iter(facts({"meds": [med]}, split_conditions=True)))
        # a neutral mention (none) may gain the source's action the same way
        if (fact not in kept and (med.get("action") in (_RX_CONFLICT - allowed) | {None, "none"}
                                  or not _rx_right(med, allowed, planned))
                and isinstance(quote, str) and quote
                and any(m.start() < close and m.end() > lead for m in re.finditer(re.escape(quote), body))
                # a planned change stays planned: never turned into a done one
                and (med.get("status") == "planned") == bool(planned)
                and any(_rx_right(other, allowed, planned) and rest(other) == rest(med)
                        for other in _rx_covering(candidate, bool(planned)) if other.get("name") == name)):
            gone.add(fact)
    return gone


# The rule hint's own action word. A quote spanning several requests covers
# a hint only when the extracted action names it, not by overlap alone.
_REQUEST_STEMS = {"confirm": "確認", "contact": "連絡", "ask": "教え"}


_REQUEST_END = (r"(?:お願い|ください|下さい|いただけ|いただきたい|いただきますよう|頂け|頂きたい"
                r"|頂きますよう|ほしい|欲しい|願います)")

# A stem is requested only when the request ending follows it directly
# (確認をお願い, 確認してください) or when it is listed (と/や/、) through the
# following confirm/contact stems up to one that is directly requested. Free text never
# reaches a later ending past a report (連絡をいただきましたが…教えてください,
# 確認と連絡を終えたので…連絡してください).
_REQUEST_DIRECT = rf"(?:を)?(?:して)?{_REQUEST_END}"

# one listing step: a particle, then text up to the NEXT confirm/contact stem
_CLAUSE_MARK = r"ので|から|ため|けど|けれど|が、|、"

_REQUEST_LINK = (r"(?:(?:と|や|・|及び|および)、?|、)"
                 rf"(?:(?!確認|連絡|{_CLAUSE_MARK})[^。！？!?;；\r\n])*(?:確認|連絡)")

_REQUEST_CUES = {
    # any number of listed stems, the last directly requested (確認と…確認と…確認をお願い)
    kind: re.compile(rf"{stem}(?=(?:{_REQUEST_LINK})*{_REQUEST_DIRECT})")
    for kind, stem in (("confirm", "確認"), ("contact", "連絡"))}

_REQUEST_CUES["ask"] = re.compile(r"教えて(?:ください|いただけ|欲しい)")

def _request_cues(body):
    """Every sentence's confirm/contact/ask request, for the quality re-check only.

    The rule hints keep one entry per pattern, so a second kind in the same
    sentence (確認と…連絡をお願い) or a second sentence of one kind is not
    hinted. A stem counts only as the object of a request ending, never
    inside a noun (連絡記録) or a finished report (連絡をいただきました)."""
    cues = []
    for sentence in re.finditer(r"[^。！？!?;；\r\n]+", body):
        ctx = sentence[0].strip()
        for kind, rx in _REQUEST_CUES.items():
            found, start = [], 0
            for m in rx.finditer(ctx) if ctx else ():
                # the target lies between the previous stem and this one; the
                # clause (after ので/から/、…) is what a negation must sit in
                found.append({"kind": kind, "ctx": ctx,
                              "chunks": _request_target(ctx[start:m.start()]),
                              "clause": ctx[_clause_start(ctx[:m.start()]):_clause_end(ctx, m.end())]})
                start = m.end()
            _distinct_targets(found)
            if len(found) > 1:
                # 記録の確認と検査値の確認: one quote and one 確認 must not cover both
                for cue in found:
                    cue["siblings"] = [o["target"] for o in found if o is not cue]
            cues += found
    return cues


def _clause_start(before):
    """Start of the clause holding a request stem (after ので, から, 、 …)."""
    marks = list(re.finditer(_CLAUSE_MARK, before))
    return marks[-1].end() if marks else 0


def _clause_end(ctx, stem_end):
    """End of the clause holding a request: past its listed stems and request
    ending, up to the next clause mark (…してください、転倒はありません)."""
    chain = re.match(rf"(?:{_REQUEST_LINK})*{_REQUEST_DIRECT}", ctx[stem_end:])
    tail = stem_end + (chain.end() if chain else 0)
    mark = re.search(_CLAUSE_MARK, ctx[tail:])
    return tail + mark.start() if mark else len(ctx)


def _distinct_targets(found):
    """Each cue's target: its last word, extended (血圧の記録 vs 血糖の記録) only
    for cues whose targets would otherwise coincide; a unique one stays short."""
    sizes = [1] * len(found)
    while True:
        names = ["".join(c["chunks"][-n:]) or None for c, n in zip(found, sizes)]
        clashing = [i for i, name in enumerate(names)
                    if name and names.count(name) > 1 and sizes[i] < len(found[i]["chunks"])]
        if not clashing:
            break
        for i in clashing:
            sizes[i] += 1
    for cue, name in zip(found, names):
        cue["target"] = name if name and len(name) <= 20 else None


def _request_current(body, ctx, clause):
    """The request sentence is patient-scoped and its own clause is not negated."""
    for match in re.finditer(re.escape(ctx), body):
        if (clinical_scope.patient_source_scope(body, *match.span()) == "patient"
                and not re.search(_NEGATED_CLAUSE, clause)):
            return True
    return False


def _request_target(before):
    """The の-separated words before a request stem (本人の記録の確認 -> [本人, 記録]). ``before``
    starts at the previous stem, so only its listing particle is dropped and
    a と inside a word (かかと) is never a boundary."""
    # a listing particle counts only right after a request stem of any kind
    clause = re.split(r"(?:確認|連絡)(?:と|や|、|・|及び|および)", before)[-1]
    clause = re.sub(r"^(?:と|や|、|・|及び|および)", "", clause)
    clause = clause[_clause_start(clause):]      # 転倒がないので記録 -> 記録
    clause = re.sub(r"(?:の|を|へ|に|は|も|ご)+$", "", clause)
    return [c.removeprefix("ご") for c in clause.split("の") if c]


def _names_target(text, target, siblings):
    """``text`` mentions ``target`` outside any longer sibling that contains it
    (記録 is not named by 看護記録); の is ignored (血糖記録 names 血糖の記録)."""
    text = text.replace("の", "")
    for longer in sorted((s for s in siblings if target in s and s != target), key=len, reverse=True):
        text = text.replace(longer, "")
    return target in text


def _request_covers(item, hint):
    """An extracted request whose quote overlaps the hint sentence and names its action."""
    quote = item.get("evidence") if isinstance(item, dict) else None
    ctx = hint["ctx"]
    if not isinstance(quote, str) or not quote or not (quote in ctx or ctx in quote):
        return False
    action = item.get("action") if isinstance(item.get("action"), str) else ""
    if "siblings" in hint:
        # several requests of this kind in one sentence: the item must name this one
        target, siblings = hint.get("target"), [s for s in hint["siblings"] if s]
        if not target:
            return False        # unidentified target: left to the one bounded re-read
        if not _names_target(action, target, siblings) and (
                not _names_target(quote, target, siblings)
                or any(_names_target(quote, s, [target, *siblings]) for s in siblings)):
            return False
    target = hint.get("target")
    if target and _names_target(action, target, [s for s in hint.get("siblings") or [] if s]):
        return True             # 記録をチェックして主治医に伝達 names both targets
    stem = _REQUEST_STEMS[hint["kind"]]
    if stem in action or (hint["kind"] == "ask" and item.get("kind") == "question"):
        return True
    # a paraphrased action still covers a quote that names only this request
    return stem in quote and sum(s in quote for s in _REQUEST_STEMS.values()) == 1


def _neutral_med_grounded(med, body):
    """A new current neutral medication needs its own name, dose and patient quote."""
    quote = med.get("evidence")
    if (not isinstance(quote, str) or locate_quote_span(body, quote) is None
            or clinical_values.patient_item_scope(med, body, surface=med.get("name")) != "patient"
            or not _current_cue(body, quote)
            or not _rx_bounded(extract_validation.flat_text(med["name"]), extract_validation.flat_text(quote))):
        return False
    if med.get("dose") is not None and (
            not isinstance(med["dose"], str) or not any(
                match[1] == med["name"] and match[2] + match[3] == med["dose"].replace(" ", "")
                and not re.match(r"[A-Za-z0-9Ａ-Ｚａ-ｚ０-９/／]", quote[match.end():])
                for match in extract_schema.MEDICATION_TOKEN.finditer(quote))):
        return False
    return not (isinstance(med.get("freq"), str) and med["freq"] and not re.search(
        r"(?<![0-9])" + re.escape(med["freq"]) + r"(?![0-9])", quote))


def quality_improves(candidate, prior, body, hints=None):
    """Optional gains must retain prior assertions and have their own source evidence.

    The one exception: a medication action the source contradicts (_rx_superseded)
    may be replaced by the same item with the source's action."""
    gone = _rx_superseded(prior, candidate, hints, body)
    if gone:
        prior = {**prior, "meds": [med for med in prior.get("meds") or [] if not (
            isinstance(med, dict) and next(iter(facts({"meds": [med]}, split_conditions=True))) in gone)]}
    # dropping a contradicted item (a neutral duplicate beside the stop) is itself the gain
    dropped = bool(gone) and candidate is not None and (
        facts(prior, split_conditions=True) <= facts(candidate, split_conditions=True)
        and drop_total(candidate) <= drop_total(prior)
        and (prior.get("urgency") != "high" or candidate.get("urgency") == "high"))
    if not dropped and not improves(candidate, prior):
        # Evidence is excluded from _facts. A neutral medication mention
        # may acquire its missing patient-owned quote without adding a
        # predicate, provided every assertion and existing owned quote stays.
        if (candidate is None or prior is None
                or facts(candidate, split_conditions=True) != facts(prior, split_conditions=True)
                or drop_total(candidate) > drop_total(prior)
                or candidate.get("urgency") != prior.get("urgency")):
            return False

        def owned_med_quotes(document):
            quotes = set()
            for med in document.get("meds") or []:
                if not med_is_patient_current(med) or med.get("action") not in (None, "none", "no_change"):
                    continue
                quote = med.get("evidence")
                if not _neutral_med_grounded(med, body):
                    continue
                quotes.add((next(iter(facts({"meds": [med]}, split_conditions=True))), quote))
            return quotes

        return owned_med_quotes(prior) < owned_med_quotes(candidate)
    protected = facts(prior, split_conditions=True)
    action_names = _rx_action_names(_rx_names(candidate, hints if isinstance(hints, dict) else {}), hints, body)
    # with the rule hints, an added action must rest on a change the source states,
    # as the completeness check requires (only where the source has change words)
    grounds = None
    if isinstance(hints, dict):
        names = _rx_names(candidate, hints)
        if list(_rx_bindings(hints, names, body, every=True)):
            grounds = _rx_grounds(hints, names, body)
    for key in _FACT_LIST_FIELDS:
        for item in candidate.get(key) or []:
            if isinstance(item, dict) and not facts({key: [item]}, split_conditions=True) <= protected:
                if item.get("unverified") or locate_quote_span(body, item.get("evidence")) is None:
                    return False
                if (key == "meds" and med_is_patient_current(item)
                        and item.get("action") in (None, "none", "no_change")
                        and not _neutral_med_grounded(item, body)):
                    return False
                if key == "meds" and item.get("action") in _RX_CONFLICT and (
                        item.get("name") not in action_names
                        # 中止を撤回しました: a withdrawn or denied change grounds no added action
                        or (grounds is not None and not _rx_grounded(item, grounds, body))):
                    return False
    return True

def retains_assertion(prior, candidate):
    if isinstance(prior, dict):
        return isinstance(candidate, dict) and all(
            key in ("evidence", "normalized", "unverified") or (isinstance(key, str) and key.startswith("_"))
            or value is None or retains_assertion(value, candidate.get(key))
            for key, value in prior.items())
    if isinstance(prior, list):
        return isinstance(candidate, list) and all(
            any(retains_assertion(item, other) for other in candidate) for item in prior)
    return type(prior) is type(candidate) and prior == candidate


def protected_lost(prior, candidate, body):
    """Count grounded negation/nonpatient assertions lost by an automatic replacement."""
    # The stored assertion is the protection baseline, not a new candidate:
    # stricter validation must not erase an old protected assertion before
    # comparison. The caller binds that artifact to this exact source.
    old = prior if isinstance(prior, dict) else {}
    lost = {}
    if not isinstance(body, str):
        return lost
    new = extract_validation.validate(candidate if isinstance(candidate, dict) else {}, body) or {}
    for field in _FACT_LIST_FIELDS:
        items = old.get(field)
        if not isinstance(items, list):
            continue
        for item in items:
            if (not isinstance(item, dict)
                    or not (item.get("negated") is True or item.get("subject") in ("family", "other"))
                    or not isinstance(item.get("evidence"), str)
                    or locate_quote_span(body, item.get("evidence")) is None):
                continue
            if not any(retains_assertion(item, other) for other in new.get(field) or []):
                lost[field] = lost.get(field, 0) + 1
    return lost
