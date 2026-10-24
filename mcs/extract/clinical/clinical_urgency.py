"""Resolve source-grounded clinical urgency independently of display and transport."""
from __future__ import annotations

import re

import clinical_scope
import clinical_values
import mcs_queries
from mcs_queries import item_unverified

_URGENT = re.compile(
    r"至急|緊急(?!時|連絡(?:先|網|票|カード)|用|体制)(?:搬送|受診|対応)?|早急|すぐに|急ぎ|"
    r"救急(?:搬送|受診)?|搬送(?!先|元|方法|手段|経路|用|体制|依頼書)")
_URGENT_CLAUSES = re.compile(
    r"[。！？!?;\n]|しかし|ただし|けれど(?:も)?|"
    r"(?<=ない)が|(?<=ません)が|(?<=した)が|(?<=ました)が|(?<=です)が")
# A past/future word negates only when it directly modifies the urgent
# word (「来週、緊急受診」); 「明日の訪問前に至急」 or 「昨日の採血で…、至急」 stay current,
# and 「昨日から/より」「明日までに」 bound a current need, so they never match here.
_URGENT_NONCURRENT = re.compile(
    r"(?:昨日|一昨日|先日|先週|先月|昨年|以前|過去|"
    r"明日|明後日|来週|来月|来年|後日|今度|\d+日前|\d+日後)"
    r"(?:には|にも|は|に|も)?[、,\s　]*$")
# A contingency head directly before the urgent word (「悪化した場合は救急搬送」
# 「続くようなら至急連絡」) describes a plan, not a current need; a clock time
# (「10時に至急」) is not a contingency.
_URGENT_CONDITIONAL = re.compile(
    r"(?:(?<![0-9０-９])(?:場合|際|とき|時|折)(?:に|には|は|も|の)?|"
    r"(?:なら|ようなら|たら|ましたら|れば|ければ|あれば|ようであれば|ようでしたら))"
    r"[、,\s　]*$")
_URGENT_HYPOTHETICAL = re.compile(r"もし|万一|万が一")
# 「すぐに」 alone is a plain adverb (「すぐに眠れる」); it is urgent only with an
# action or request in its scope.
_URGENT_SOON_ACTION = re.compile(
    r"ください|下さい|お願い|願います|ほしい|欲しい|必要|搬送|受診|往診|連絡|中止|"
    r"対応|確認|報告|相談|救急|呼(?:ん|び)|来て|向か|駆けつけ")
_URGENT_INACTIVE = re.compile(
    r"^(?:性|度|(?:の|な|に)?(?:対応|連絡|確認|受診|処置|搬送|要請))?"
    r"の?(?:は|も|が|を)?(?:で(?:は)?|じゃ|(?:し|され)(?:て(?:い|おり?)?)?|する)?"
    r"(?:ありません|ません|なかった|ない|なく|なし|"
    r"不要(?!では(?:ありません|ない))|必要(?:は|が)?(?:ありません|ない|なし)|"
    r"低い|低く|低め|高くない|高くありません|"
    r"済み|完了|(?:た|ました|された|されました)"
    # A past ending closes the phrase; 「ただちに」「たすけて」 are not past tense.
    r"(?=$|[、,]|ので|ため|から|けど|けれど|が|と))")

_CLINICAL_REASON_CUE = re.compile(r"救急(?:搬送|受診)|搬送|急変|意識|反応|呼吸|SpO2|血圧|体温|発熱|高熱|疼痛|痛み|[胸腹頭背腰]痛|苦し|出血|転倒|死亡|逝去|心肺|酸素|けいれん|痙攣|脱水|嘔吐|吐血|血尿|冷汗|(?:Cr|eGFR|K|Na|血糖|BP|BS|Glu)\s*\d", re.I)



def clinical_urgency_quote(quote, *, symptom=None):
    """A medical report/event is separate from an urgent reply request; this is not a severity threshold."""
    if not isinstance(quote, str):
        return False
    pattern = (_CLINICAL_REASON_CUE if not isinstance(symptom, str) or not symptom.strip() else
               re.compile(_CLINICAL_REASON_CUE.pattern + "|" + re.escape(symptom), re.I))
    for match in pattern.finditer(quote):
        tail = quote[match.end():]
        prefix = r"(?:障害|消失|低下)" if match[0] == "意識" else r"困難" if match[0] == "呼吸" else ""
        if re.match(prefix + r"(?:は|も|が|を)?(?:して|認めて|認め|はして)?(?:では|で|じゃ)?"
                    r"(?:いません|ありません|ません|ない|なかった|なし|なく)", tail):
            continue
        return True
    return False



def urgent_request_quote(quote):
    """Recognize urgency of a requested action without asserting a patient's clinical state."""
    return (isinstance(quote, str) and _URGENT.search(quote) is not None
            and (clinical_scope.URGENT_REQUEST.search(quote) is not None or _URGENT_SOON_ACTION.search(quote) is not None))



def patient_clinical_quotes(body, *, patient_name=None):
    """Source-grounded acute-event clauses, for an already established urgent request."""
    quotes = []
    offset = 0
    for clause in re.split(r"[。！？!?;；\n]", body):
        start = body.find(clause, offset)
        offset = start + len(clause)
        # An unrelated routine measurement must not turn a paperwork request
        # into a clinical emergency. Cross-sentence linkage requires an
        # explicit acute event, not a new numeric severity heuristic.
        if not re.search(r"急変|救急(?:搬送|受診)|意識(?:を失|がない|消失|障害|低下)|呼吸(?:がない|困難|停止)|心肺停止", clause):
            continue
        if not clinical_urgency_quote(clause):
            continue
        kept, _ = clinical_scope.patient_urgency_quotes(body, [clause], patient_name=patient_name)
        quotes.extend(quote for quote in kept if clinical_urgency_quote(quote))
    return list(dict.fromkeys(quotes))



def patient_rule_urgency_quotes(body):
    """Current patient-only lexical emergency scopes, shared by writers and cached readers."""
    clause_start = 0
    for clause in _URGENT_CLAUSES.split(body):
        offset = body.find(clause, clause_start)
        clause_start = offset + len(clause)
        # Adjacent urgent words (「すぐに搬送」「緊急で搬送」) share one scope.
        spans: list[list[int]] = []
        for match in _URGENT.finditer(clause):
            if spans and clause[spans[-1][1]:match.start()].strip() in ("", "で"):
                spans[-1][1] = match.end()
            else:
                spans.append([match.start(), match.end()])
        cursor = 0
        for i, (start, end) in enumerate(spans):
            before = clause[cursor:start]
            cursor = end
            stop = spans[i + 1][0] if i + 1 < len(spans) else len(clause)
            if clinical_scope.patient_source_scope(body, offset + start, offset + end) != "patient":
                continue
            if ((_URGENT_NONCURRENT.search(before)
                 and not clinical_scope.URGENT_REQUEST.search(clause[end:]))
                    or _URGENT_CONDITIONAL.search(before)
                    or _URGENT_HYPOTHETICAL.search(before)
                    or _URGENT_INACTIVE.match(clause[end:stop].strip())
                    or (clause[start:end] == "すぐに"
                        and not _URGENT_SOON_ACTION.search(clause[end:stop]))):
                continue
            return [clause.strip()]
    return []



def _typed_urgency_reasons(document, reasons, context):
    from mcs_util import locate_quote_span

    body, patient_name, default = context
    kept = []
    items = document.get("symptoms")
    for item in items if isinstance(items, list) else []:
        if (not isinstance(item, dict) or item_unverified(item) or item.get("subject") != "patient"
                or item.get("status") not in ("new", "ongoing") or item.get("negated") is not False):
            continue
        text, quote = item.get("text"), item.get("evidence")
        if not isinstance(text, str) or not text.strip() or not isinstance(quote, str):
            continue
        span = locate_quote_span(body, quote)
        inner = locate_quote_span(body[span[0]:span[1]], text) if span is not None else None
        if span is None or inner is None or clinical_values.patient_item_scope(item, body, patient_name=patient_name, surface=text, default=default) != "patient":
            continue
        for reason in reasons:
            reason_span = locate_quote_span(body, reason)
            if (reason_span is not None and reason_span[0] <= span[0] + inner[0]
                    and span[0] + inner[1] <= reason_span[1]
                    and clinical_urgency_quote(reason, symptom=text) and reason not in kept):
                kept.append(reason)
    return kept



def message_urgency(db, mid: int) -> str | None:
    """'llm' when the current fact artifact says urgency high, None when it
    says routine (the content-level verdict supersedes the lexical rule),
    'rule' when only extract_v1 flags it and no LLM verdict exists — the one
    urgency reading for cards, text notices and signal escalation.
    canonical_projection / semantic_facts_v4 rows carry no urgency, so the
    newest hash-current extract_llm row supplies the verdict behind them."""
    return message_urgency_details(db, mid)["source"]



class _Unset:
    """Marks an omitted preloaded read; a class because the fingerprint accepts no object() constant."""


def message_urgency_details(db, mid: int, *, facts=_Unset, rule=_Unset, source=_Unset) -> dict:
    """Report guarded urgency separately from the model's raw verdict and preserve review holds."""
    # facts/rule/source: preloaded reads (None included); omitted ones are read here
    document = (mcs_queries.current_message_facts(db, mid) if facts is _Unset else facts) or {}
    verdict = document.get("urgency")
    if verdict not in ("high", "routine", "unclear"):
        document = mcs_queries.current_message_extract(db, "extract_llm", mid) or {}
        verdict = document.get("urgency")
    result = {"source": None, "subject": "unknown", "reasons": [], "raw_verdict": verdict,
              "verdict": verdict, "held": False, "kind": "clinical"}
    if verdict == "routine":
        return result
    if rule is _Unset:
        rule = mcs_queries.current_message_extract(db, "extract_v1", mid)
    rule = rule or {}
    if verdict not in ("high", "unclear") and rule.get("urgency") != "high":
        return result
    if db is None:
        if verdict == "high":
            result.update(verdict="unclear", held=True, scopes=["unknown"])
        return result
    row = mcs_queries.message_urgency_source(db, mid) if source is _Unset else source
    if row is None or row["body_state"] not in (None, "full") or not isinstance(row["body_text"], str):
        # unreadable source: a raw high stays held for review, never promoted
        if verdict == "high":
            result.update(verdict="unclear", held=True, scopes=["unknown"])
        return result
    body = row["body_text"]
    default = "unknown" if row["project_type"] == "group" else "patient"
    if default == "unknown":
        if verdict == "high":
            result.update(verdict="unclear", held=True, scopes=["unknown"])
        return result
    if verdict == "high":
        quotes = document.get("urgency_evidence")
        reasons, scopes = clinical_scope.patient_urgency_quotes(
            body, quotes if isinstance(quotes, list) else [], patient_name=row["patient_name"], default=default)
        clinical = [quote for quote in reasons if clinical_urgency_quote(quote)]
        clinical += [quote for quote in _typed_urgency_reasons(document, reasons, (body, row["patient_name"], default))
                     if quote not in clinical]
        if clinical:
            return {**result, "source": "llm", "subject": "patient", "reasons": clinical[:2]}
        if reasons and any(urgent_request_quote(quote) for quote in reasons):
            result.update(kind="request", reasons=reasons[:2])
        result.update(verdict="unclear", held=True, scopes=scopes or ["unknown"])
    # "unclear" is an abstention, not a clearance — it falls through to
    # the lexical net exactly like a missing verdict does.
    if rule.get("urgency") == "high":
        reasons = patient_rule_urgency_quotes(body)
        scoped, _ = clinical_scope.patient_urgency_quotes(body, reasons, patient_name=row["patient_name"], default=default)
        clinical = [quote for quote in scoped if clinical_urgency_quote(quote)]
        if not clinical and any(urgent_request_quote(quote) for quote in scoped):
            clinical = patient_clinical_quotes(body, patient_name=row["patient_name"])
        if clinical:
            result.update(source="rule", subject="patient", reasons=clinical[:2])
        elif reasons:
            result.update(kind="request", reasons=reasons[:2])
    return result



def urgency_qc_disagreement(db, mid: int) -> dict | None:
    """Newest current QC's urgency verdict when it disagrees with the
    extraction it audited — {"extracted", "jev", "confidence"} or None.

    The verdict must pin to the CURRENT extract_llm row (a verdict on a
    superseded artifact is invisible here), and a missing QC row simply
    returns None — absence can never suppress or annotate an alert."""
    document = mcs_queries.current_message_urgency_qc(db, mid)
    urg = (document or {}).get("urgency")
    if not isinstance(urg, dict) \
            or urg.get("jev") not in ("high", "routine", "unclear") \
            or urg.get("extracted") not in ("high", "routine", "unclear") \
            or urg.get("jev") == urg.get("extracted"):
        return None
    return urg
