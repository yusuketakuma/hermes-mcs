"""Validate source-grounded extraction fields and merge document chunks without I/O."""
import clinical_scope
import clinical_events
import json
import math
import re
import unicodedata

import clinical_values
import extract
import patient_context
from patient_context import CATEGORIES, context_items
from mcs_util import locate_quote_span

_VITAL_KEYS = {"bt", "hr", "rr", "sbp", "dbp", "spo2", "bs"}


# Deterministic vital-sign thresholds, active only when the config
# policy enables them (vital_threshold_policy). Vitals carry no
# subject or tense context, so a crossing is gated by a context scan
# of the body window around the reading — family readings, past
# reports, conditional instructions ("90を切ったら連絡") and
# unmeasurable markers must not fire.
_VITAL_THRESHOLDS_DEFAULT = {"spo2_lte": 90.0, "sbp_lte": 90.0,
                             "sbp_gte": 180.0, "bs_lte": 70.0}

def vital_threshold_policy(cfg) -> dict | None:
    """cfg['vital_urgency'] -> {"mode", "thresholds"} or None.

    Default off. mode "flag" records vital_flags on the artifact for
    review; mode "high" additionally raises urgency to "high" with the
    flagged spans as urgency_evidence — a clinical-policy decision that
    must be enabled explicitly, never by accident."""
    raw = (cfg or {}).get("vital_urgency") if isinstance(cfg, dict) \
        else None
    if not isinstance(raw, dict) or raw.get("mode") not in ("flag", "high"):
        return None
    merged = dict(_VITAL_THRESHOLDS_DEFAULT)
    th = raw.get("thresholds")
    if isinstance(th, dict):
        for k in _VITAL_THRESHOLDS_DEFAULT:
            v = th.get(k)
            if type(v) in (int, float) and math.isfinite(v):
                merged[k] = float(v)
    return {"mode": raw["mode"], "thresholds": merged}


def apply_vital_policy(out: dict, body: str | None,
                        vital_policy: dict) -> None:
    """Deterministic vital thresholds on a finished extraction —
    applied post-merge/post-validate so single, chunked and batch
    paths agree. "flag" annotates only; "high" (explicit clinical
    policy) raises urgency with the flagged spans as its evidence."""
    flags = clinical_values.vital_threshold_flags(out.get("vitals") or {}, body,
                                   vital_policy["thresholds"])
    if not flags:
        return
    out["vital_flags"] = flags
    if vital_policy["mode"] == "high" and out.get("urgency") != "high":
        out["urgency"] = "high"
        out["urgency_evidence"] = [f["evidence"] for f in flags][:2]


# Free-text fields (summary/points): the model sometimes parrots JSON
# fragments or slips into another language's output — structural
# characters never belong in a Japanese one-liner.
_BAD_TEXT = re.compile(r'[{}\[\]"\n\r\t]')
SUMMARY_MAX_CHARS = 60
POINT_MAX_CHARS = 40


def _cap(v, maxlen: int):
    """Length-bound an optional string field; non-strings pass as-is
    (type checks happen at the call site)."""
    return v[:maxlen] if isinstance(v, str) else v


def _clean_text(v, maxlen: int) -> str | None:
    """Reject invalid or overlength free text without cutting off its qualifiers."""
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s or len(s) > maxlen or _BAD_TEXT.search(s):
        return None
    return s


_RX_ACTS = {"start", "stop", "change", "decrease", "increase", "none", None}

_EVENTS = {"visit", "exam", "admission", "discharge", "transfer", "fall",
           "eol", "care", "family_contact", "prescription_inquiry", "other"}

# Discrete clinical events must be grounded in the target body — the
# model sometimes lifts an event from thread context or invents one
# (Jev audit: events are the largest NO_MATCH class). Unlisted kinds
# (care/family_contact/other) stay ungrounded: their cues are too
# broad for a keyword check to falsify.
EVENT_CUES = {
    "eol": re.compile(
        r"看取り|逝去|死去|永眠|お亡くなり|亡くな(?:っ|り)|死亡|息を引き取|"
        r"心肺停止|心停止|呼吸停止|終末期|臨終|旅立|安らか|緩和ケア|"
        r"ACP|モルヒネ|オピオイド"),
    "fall": re.compile(r"転倒|転落|滑落|落ち(?:た|て|る)|倒れ"),
    "admission": re.compile(r"入院|搬送|救急|急性期"),
    "discharge": re.compile(r"退院|出院|退所|退館"),
    "transfer": re.compile(r"転院|転棟|転所|搬送|施設間|移動|移り"),
    "exam": re.compile(
        r"外来|" + extract.ENCOUNTER_CUE + r"|受診|検査|採血|レントゲン|エコー|心電図|"
        r"CT|MRI|血液検査|尿検査|処置"),
    "visit": re.compile(r"訪問|往診|来訪|訪れ|伺|参り|到着|向かい|家に来"),
    # A bare 照会/疑義 is mostly not about a prescription (T9: 3/3 false cues);
    # only 疑義照会, 処方提案 or an inquiry in the same clause as 処方/薬 counts.
    "prescription_inquiry": re.compile(
        # clause spans are bounded so one long clause cannot scan quadratically
        r"疑義照会|処方提案|(?:処方|薬(?!剤師|局|師))[^。、，,;；！？!?\n]{0,200}照会"
        r"|照会[^。、，,;；！？!?\n]{0,200}(?:処方|薬(?!剤師|局|師))"),
}

_MED_STATUSES = {"current", "past", "planned"}

_MED_SUBJECTS = {"patient", "family", "other"}

_SYM_STATUSES = {"new", "ongoing", "resolved", "past"}

# schema v4 detail fields — present-but-invalid enum drops the ITEM
# (never normalize a guess); absent stays absent.
_RX_ROUTES = {"oral", "topical", "injection", "infusion",
              "inhalation", "tube", "other"}

_SYM_SEVERITY = {"mild", "moderate", "severe"}

# descriptive, not safety-bearing: an invalid kind omits the KEY, the
# request item survives (unlike status/negated, which drop the item)
_REQ_KINDS = {"request", "question", "self_plan"}

_REPLY_KINDS = {"ack", "intent", "progress", "answer", "done", "cancel"}

_LAB_FLAGS = {"high", "low"}

_DUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _valid_date(text: str) -> bool:
    if not _DUE_RE.match(text):
        return False
    from datetime import date
    try:
        date.fromisoformat(text)
        return True
    except ValueError:
        return False


def flat_text(text: str) -> str:
    """NFKC-normalized text with all whitespace removed."""
    return "".join(unicodedata.normalize("NFKC", text).split())


def nonspecific_med_name(name: str) -> bool:
    """Bare medication words and person-owned variants identify no particular drug."""
    generic = re.fullmatch(r"(?:(.+)の)?(?:薬|お薬|内服薬|処方薬|服用薬)", flat_text(name))
    return bool(generic and (not generic[1]
                            or clinical_scope.person_scope(generic[1], None) != "unknown"
                            or re.fullmatch(clinical_scope.NAMED_PERSON, generic[1])))


def validate(d: dict, body: str | None = None,
              drops: dict | None = None, ctx: bool = False, *, references=None) -> dict | None:
    """Schema check — malformed LLM output must not become a success
    artifact (it poisons downstream rollups, Oracle B20). Returns the
    cleaned dict or None when nothing salvageable remains.

    Per-item strictness, per-key tolerance: a bad item inside a list is
    dropped without losing its valid siblings (a deterministic model
    emitting the same malformed item would otherwise burn all retries
    into a permanent error and lose the whole extraction). A present-
    but-invalid enum value drops the ITEM — silently normalizing
    "廃止" to "current" would mislabel a past med as active.

    `body` (the target message's full body_text) enables evidence
    verification: a quote is stored only when it locates to a UNIQUE
    verbatim span of the target body — never a context-only or ambiguous
    match — and the stored value is the body's own span, not the
    model's rendering of it. Dropped evidence/items are counted under
    "_evidence_dropped"/"_items_dropped" so the loss stays observable.

    A confirmed medication quote must contain its name and agree with
    its explicit person. Unresolved drug references and missing/unlocated
    evidence remain unverified candidates under the compatibility contract.

    `drops`, when given, collects bounded failure detail for the repair
    pass: {"ev": [rejected quotes], "items": [fields with dropped
    items]} — capped so a pathological output can't blow memory.

    `ctx` (the prompt carried thread/summary context) enables the lift
    guard: a med dose/freq or symptom onset/duration absent from the
    body is dropped at the key — it was lifted from the context."""
    out: dict = {}
    v = Validator(body, drops, ctx, references)
    try:
        v.meds(d, out)
        v.symptoms(d, out)
        v.labs(d, out)
        v.events(d, out)
        v.requests(d, out)
        v.patient_context(d, out)
        v.reply(d, out)
        v.vitals(d, out)
        v.scalars(d, out)
        if v.ev_dropped:
            out["_evidence_dropped"] = v.ev_dropped
        if v.items_dropped:
            out["_items_dropped"] = v.items_dropped
        # when every recognized key was corrupt (or emptied by drops)
        # the output is a failure, not an empty extraction
        if v.items_dropped and not any(
                (isinstance(x, list) and x)
                or (isinstance(x, dict) and x)
                or (isinstance(x, str) and x.strip())
                for k, x in out.items() if not k.startswith("_")):
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    return out


class Validator:
    """Per-field schema cleaning shared state: the evidence verifier +
    drop accounting used by every item validator in _validate."""

    def __init__(self, body, drops, ctx=False, references=None):
        self.body = body
        self.drops = drops
        self.ctx = ctx
        self.references = references if isinstance(references, dict) else {}
        self.flat = flat_text(body) if body is not None else None
        self.ev_dropped = 0
        self.items_dropped = 0

    def grounded(self, text) -> bool:
        """True when text occurs in the body after NFKC + whitespace
        removal; with no body there is nothing to check against."""
        if self.flat is None:
            return True
        t = flat_text(text) if isinstance(text, str) else ""
        return bool(t) and t in self.flat

    def unlift(self, item: dict, keys: tuple):
        """Lift guard: with context in the prompt, drop detail keys
        whose value the target body does not contain."""
        if self.ctx and self.body is not None:
            for k in keys:
                if item.get(k) and not self.grounded(item[k]):
                    del item[k]

    def uncertain(self, item, key, *, nullable=False):
        value = item.get(key)
        if value is not None:
            self.miss(value)
        if nullable:
            item[key] = None
        else:
            item.pop(key, None)
        item["unverified"] = True

    def assertion(self, item, surfaces=(), *, request=False, sender=None, medication=False):
        """Locate one target assertion inside its exact evidence, never another item's clause."""
        quote = item.get("evidence")
        span = locate_quote_span(self.body, quote) if isinstance(quote, str) and self.body is not None else None
        if span is None:
            return None
        normalize = clinical_values.request_action_key if request else flat_text
        segments = [(m.start(), m.end(), m[0]) for m in re.finditer(r"[^。！？!?;；\n]+", quote) if m[0].strip()]
        if not request:
            segments = [(left + m.start(), left + m.end(), m[0])
                        for left, _, text in segments for m in re.finditer(r"[^、,]+", text) if m[0].strip()]
        elif sum(bool(re.search(r"確認|連絡|報告|中止|開始|教え|送付|調べ", text))
                 for text in re.split(r"[、,]", quote)) > 1:
            segments = [(left + m.start(), left + m.end(), m[0])
                        for left, _, text in segments for m in re.finditer(r"[^、,]+", text) if m[0].strip()]
        def supports(term, text):
            wanted, source = normalize(term), normalize(text)
            if not request:
                return wanted in source
            # A suffix inside a different object name is not the same request.
            for match in re.finditer(re.escape(wanted), source):
                prefix = source[:match.start()]
                if not prefix or re.search(r"へ$|に$|を$|から$|より$|[:：]$|なら$|たら$|場合は$|ときは$|れば$", prefix):
                    return True
                if sender and re.search(re.escape(normalize(sender)) + r"(?:は|が)$", prefix):
                    return True
            return False
        hits = [(left, right, text, term) for left, right, text in segments
                for term in surfaces if isinstance(term, str) and normalize(term) and supports(term, text)]
        selected = {(left, right): (text, term) for left, right, text, term in hits}
        if len(selected) != 1:
            if selected or len(segments) != 1 or request:
                return None
            left, right, text = segments[0]
            term = None
            item["unverified"] = True  # Keep an unresolved paraphrase; do not invent a semantic match.
        else:
            (left, right), (text, term) = next(iter(selected.items()))
        inner = locate_quote_span(text, term) if term is not None else None
        if medication and term is not None and (inner is None or len(list(re.finditer(
                r"(?=" + re.escape(flat_text(term)) + r")", flat_text(quote)))) != 1):
            return None
        start = span[0] + left + (inner[0] if inner else 0)
        end = span[0] + left + (inner[1] if inner else len(text))
        return text, start, end, span[0] + right

    def medication_continuation(self, item, assertion):
        """Prove one positive current medication continuation inside a complete quoted sentence."""
        quote = item.get("evidence")
        span = locate_quote_span(self.body, quote)
        start, end = assertion[1:3]
        left = max(self.body.rfind(mark, 0, start) for mark in "。！？!?;；\n") + 1
        stop = re.search(r"[。！？!?;；\n]", self.body[end:])
        right = end + stop.start() if stop else len(self.body)
        if (self.body[right:right + 1] in ("？", "?", "！", "!")
                or self.body[left:span[0]].strip() or self.body[span[1]:right].strip()):
            return None
        prefix = flat_text(self.body[left:start])
        owner = re.fullmatch(r"(.+?)(?:は|が)", prefix)
        subject = clinical_scope.person_scope(owner[1], None) if owner else "unknown"
        if subject == "unknown":
            return None
        tail = flat_text(self.body[end:right]).casefold()
        match = re.fullmatch(r"を(?P<dose>[0-9]+(?:\.[0-9]+)?(?:mg|μg|mcg|g|ml|錠|包|カプセル))"
                             r"(?P<prn>頓(?:服|用)で)?[、,]"
                             r"(?P<freq>1日[1-6]回(?:まで)?|就寝前)(?:に)?"
                             r"(?:内服|服用)(?:しています|している)", tail)
        if match is None or clinical_scope.source_axes(self.body, start, end, default="unknown") != (subject, "current"):
            return None
        return {"dose": match["dose"], "freq": match["freq"], "route": "oral",
                "prn": bool(match["prn"]), "subject": subject, "status": "current", "negated": False}

    def identity_attributes(self, item, assertion, *, symptom=False):
        text, start, end, assertion_end = assertion
        subject, period = clinical_scope.source_axes(self.body, start, end)
        if (item.get("subject") is not None and subject in _MED_SUBJECTS
                and item["subject"] != subject):
            self.uncertain(item, "subject")
        elif subject == "unknown":
            self.uncertain(item, "subject")
        status = item.get("status")
        incompatible = ((period == "past" and status not in (None, "past", "resolved"))
                        or period in ("planned", "conditional") and status is not None
                        and (symptom or status != "planned"))
        if incompatible:
            self.uncertain(item, "status")
        tail = self.body[end:assertion_end]
        # The same source-bound polarity helper is also used by context views.
        polarity = clinical_scope.source_polarity(self.body, start, assertion_end,
                                                  target_end=end, medication=not symptom)
        if polarity is not None and item.get("negated") is not None and item["negated"] != polarity:
            self.uncertain(item, "negated")
        elif re.search(r"かもしれ|不明|疑い|ではない|ない(?:こと|わけ|訳)はない", tail):
            self.uncertain(item, "negated")
        if symptom:
            resolved = clinical_scope.source_resolution(self.body, start, assertion_end, target_end=end)
            continuing = re.search(r"持続|続いて|残って", tail)
            if status in ("new", "ongoing") and resolved is True:
                self.uncertain(item, "status")
            elif status == "resolved" and (continuing or resolved is not True):
                self.uncertain(item, "status")

    def miss(self, q):
        """Count an unlocated quote so the repair pass re-asks for it."""
        self.ev_dropped += 1
        if self.drops is not None:
            lst = self.drops.setdefault("ev", [])
            if len(lst) < 5:
                lst.append(str(q)[:60])

    def drop_item(self, field: str, reason: str | None = None):
        self.items_dropped += 1
        if self.drops is not None:
            lst = self.drops.setdefault("items", [])
            if field not in lst and len(lst) < 5:
                lst.append(field)
            if reason:
                reasons = self.drops.setdefault("reasons", [])
                if len(reasons) < 5:
                    reasons.append(reason)

    def ev(self, item: dict, source: dict):
        if source.get("unverified") is True:
            item["unverified"] = True
        q = source.get("evidence")
        if q is None:
            # no evidence key at all — the claim is real input but
            # cannot be quote-verified: keep it, mark it (F08)
            item["unverified"] = True
            return
        span = locate_quote_span(self.body, q) if isinstance(q, str) \
            and q.strip() and self.body is not None else None
        if span is None:
            self.miss(q)
            item["unverified"] = True
        else:
            item["evidence"] = self.body[span[0]:span[1]]

    @staticmethod
    def enum(source: dict, key: str, allowed: set):
        """Normalized enum value, or None when absent. An invalid value
        returns False so the caller can drop the item."""
        v = source.get(key)
        if v is None:
            return None
        v = str(v).strip().lower()
        return v if v in allowed else False

    def patient_context(self, d: dict, out: dict):
        if "patient_context" not in d:
            return
        if not isinstance(d["patient_context"], list):
            self.drop_item("patient_context")
            return
        items = []
        for item in d["patient_context"]:
            if (not isinstance(item, dict)
                    or not isinstance(item.get("category"), str)
                    or item["category"] not in CATEGORIES
                    or item.get("subject") not in ("patient", "family", "other")):
                self.drop_item("patient_context")
                continue
            grounded = context_items({"patient_context": [item]}, self.body) \
                if isinstance(self.body, str) else []
            if not grounded:
                self.miss(item.get("evidence"))
                self.drop_item("patient_context")
                continue
            clean = grounded[0]
            if "details" in item:
                raw = item["details"]
                missing = max(0, len(raw) - len(clean.get("details", []))) if isinstance(raw, list) else 1
                for _ in range(missing):
                    self.drop_item("patient_context.details")
                names = sorted({detail["value"] for detail in clean.get("details", [])
                                if detail["key"] == "drug_name"})
                for detail in clean.get("details", []):
                    # Only the supplied S-map may turn model output into a source identity.
                    detail.pop("refers_to", None)
                    matches = [entry for entry in raw if isinstance(entry, dict)
                        and entry.get("key") == detail["key"] and entry.get("value") == detail["value"]
                        and locate_quote_span(self.body, entry.get("evidence"))
                        == locate_quote_span(self.body, detail["evidence"]) and "refers_to" in entry] \
                        if isinstance(raw, list) else []
                    if not matches:
                        continue
                    candidate = matches[0]["refers_to"]
                    token = candidate.get("token") if isinstance(candidate, dict) else candidate
                    reference = self.references.get(token) if isinstance(token, str) else None
                    resolved = patient_context.reference_item({**reference, "token": token}) \
                        if isinstance(reference, dict) else None
                    if (len(matches) != 1 or resolved is None or clean["subject"] != "patient"
                            or resolved["key"] != detail["key"] or len(names) > 1
                            or clinical_values.patient_item_scope(detail, self.body, default="patient",
                                surface=(names or resolved["drug_names"] or [None])[0])
                            not in ("patient", "past", "planned", "conditional")
                            or (names and names != resolved["drug_names"])
                            or (isinstance(candidate, dict) and candidate != resolved)):
                        self.drop_item("patient_context.refers_to")
                    else:
                        detail["refers_to"] = resolved
            items.append({key: clean[key] for key in
                          ("category", "text", "evidence", "subject", "details") if key in clean})
        out["patient_context"] = items

    def meds(self, d: dict, out: dict):
        if "meds" not in d:
            return
        if not isinstance(d["meds"], list):
            self.drop_item("meds")
            return
        meds = []
        for index, m in enumerate(d["meds"]):
            st = self.enum(m, "status", _MED_STATUSES) \
                if isinstance(m, dict) else False
            sj = self.enum(m, "subject", _MED_SUBJECTS) \
                if isinstance(m, dict) else False
            rt = self.enum(m, "route", _RX_ROUTES) \
                if isinstance(m, dict) else False
            if not (isinstance(m, dict)
                    and isinstance(m.get("name"), str)
                    and m["name"].strip()
                    and (m.get("action") is None
                         or (isinstance(m.get("action"), str)
                             and m["action"] in _RX_ACTS))
                    and ("negated" not in m
                         or type(m.get("negated")) is bool)
                    and ("prn" not in m
                         or type(m.get("prn")) is bool)
                    and st is not False and sj is not False
                    and rt is not False):
                self.drop_item("meds")
                continue
            if nonspecific_med_name(m["name"]):
                self.drop_item("meds", f"meds[{index}].nameが薬剤を特定しない一般表現です。"
                               "本文に薬剤名がなければmedsに入れず、服薬管理情報はpatient_contextに残してください。")
                continue
            dose = m.get("dose")
            if type(dose) in (int, float) \
                    and -1e308 <= dose <= 1e308:
                dose = str(dose)
            elif not isinstance(dose, str | type(None)):
                dose = None
            item = {"name": m["name"], "dose": dose,
                    "action": m.get("action"),
                    "negated": m.get("negated", False)}
            if rt:
                item["route"] = rt
            freq = _clean_text(m.get("freq"), 30)
            if freq:
                item["freq"] = freq
            if m.get("prn") is True:
                item["prn"] = True
            # absent status/subject is preserved, never filled
            # with a fabricated 'current'/'patient' (F08) —
            # downstream patient-state consumers must treat the
            # item as a candidate, not confirmed current
            if st:
                item["status"] = st
            if sj:
                item["subject"] = sj
            if st is None or sj is None or "negated" not in m:
                item["unverified"] = True
            self.ev(item, m)
            quote = item.get("evidence")
            if quote is not None:
                flat_quote = flat_text(quote)
                mentions = list(re.finditer(re.escape(flat_text(item["name"])), flat_quote))
                if mentions and all(re.match(r"(?:さん|氏)(?:の|は|が|に|を|へ|から|と|[、。])",
                                             flat_quote[mention.end():]) for mention in mentions):
                    self.drop_item("meds", f"meds[{index}].nameは引用中で敬称付きの人物です。"
                                   "人物名と薬剤名を区別して再抽出してください。")
                    continue
                scope = clinical_values.patient_item_scope(item, self.body, surface=item["name"], default="unknown")
                wrong_person = sj in _MED_SUBJECTS and scope in _MED_SUBJECTS and scope != sj
                missing_name = flat_text(item["name"]) not in flat_text(quote)
                if missing_name and sj in _MED_SUBJECTS:
                    span = locate_quote_span(self.body, quote)
                    # A reference at the quote's start must not mask a later explicit drug owner.
                    scopes = [clinical_scope.patient_source_scope(self.body, span[0] + word.start(),
                                                           span[0] + word.end(), default="unknown")
                              for word in re.finditer(r"薬(?!剤師|局)|剤(?!師)|処方", quote)]
                    if scopes:
                        wrong_person = any(s in _MED_SUBJECTS and s != sj for s in scopes)
                if wrong_person:
                    self.uncertain(item, "subject")
                    if self.drops is not None and len(self.drops.setdefault("reasons", [])) < 5:
                        self.drops["reasons"].append(f"meds[{index}].subjectと引用の対象者が一致しません。原文の服用者を確認してください。")
                if missing_name and re.search(r"同薬|同剤|当該薬|その薬|あの薬|上記", quote):
                    # Preserve a possible stop/reference, without confirming an unproved identity.
                    self.miss(quote)
                    item["unverified"] = True
                elif missing_name:
                    self.drop_item("meds", f"meds[{index}].evidenceにnameが含まれていません。"
                                   "別の文を流用せず、薬剤名を含む本文を引用し、服用者のsubjectを確認してください。")
                    continue
                assertion = None if missing_name else self.assertion(item, (item["name"],), medication=True)
                if assertion is None:
                    for key in ("dose", "freq", "route", "prn"):
                        if item.get(key) is not None:
                            self.uncertain(item, key, nullable=key == "dose")
                    item["unverified"] = True
                else:
                    self.identity_attributes(item, assertion)
                    continuation = self.medication_continuation(item, assertion)
                    if continuation is not None:
                        for key, supported in continuation.items():
                            value = m.get(key) if key == "prn" else item.get(key)
                            matches = type(value) is type(supported) and (
                                flat_text(value).casefold() == supported if isinstance(value, str) else value == supported)
                            if value is not None and not matches:
                                self.uncertain(item, key, nullable=key == "dose")
                        meds.append({**item, "normalized": clinical_values.medication_surface(item["name"])})
                        continue
                    for key in ("dose", "freq", "route", "prn"):
                        if item.get(key) is not None and not clinical_values.medication_attribute_supported(
                                key, item[key], assertion[0]):
                            self.uncertain(item, key, nullable=key == "dose")
                    if m.get("prn") is False and not clinical_values.medication_attribute_supported(
                            "prn", False, assertion[0]):
                        item["unverified"] = True
                    supported_plan = item.get("status") == "planned" and re.search(
                        r"予定|計画|検討|提案|(?:処方医|主治医|医師)(?:に|へ)相談(?:しました|した)(?=$|[が。．\s])", assertion[0])
                    if (not supported_plan and self.body[assertion[3]:].lstrip().startswith(("、", ","))
                            and clinical_scope.source_polarity(self.body, assertion[1], assertion[3],
                                target_end=assertion[2], medication=True) is None):
                        self.uncertain(item, "negated")
            item["normalized"] = clinical_values.medication_surface(item["name"])
            meds.append(item)
        out["meds"] = meds

    def symptoms(self, d: dict, out: dict):
        if "symptoms" not in d:
            return
        if not isinstance(d["symptoms"], list):
            self.drop_item("symptoms")
            return
        syms = []
        for s in d["symptoms"]:
            st = self.enum(s, "status", _SYM_STATUSES) \
                if isinstance(s, dict) else False
            sj = self.enum(s, "subject", _MED_SUBJECTS) \
                if isinstance(s, dict) else False
            sv = self.enum(s, "severity", _SYM_SEVERITY) \
                if isinstance(s, dict) else False
            if not (isinstance(s, dict)
                    and isinstance(s.get("text"), str)
                    and s["text"].strip()
                    and ("negated" not in s
                         or type(s.get("negated")) is bool)
                    and st is not False and sj is not False
                    and sv is not False):
                self.drop_item("symptoms")
                continue
            item = {"text": s["text"],
                    "negated": s.get("negated", False)}
            if st:
                item["status"] = st
            if sj:
                item["subject"] = sj
            if sv:
                item["severity"] = sv
            onset = _clean_text(s.get("onset"), 30)
            if onset:
                item["onset"] = onset
            duration = _clean_text(s.get("duration"), 30)
            if duration:
                item["duration"] = duration
            self.unlift(item, ("onset", "duration"))
            if st is None or sj is None or "negated" not in s:
                item["unverified"] = True
            self.ev(item, s)
            terms = [term for term in (*clinical_scope.SYMPTOMS, "頭痛", "腹痛", "胸痛", "腰痛")
                     if term in item["text"]]
            for aliases in (("吐き気", "嘔気"), ("むくみ", "浮腫")):
                if any(term in item["text"] for term in aliases):
                    terms.extend(aliases)
            assertion = self.assertion(item, terms or (item["text"],))
            if assertion is not None:
                self.identity_attributes(item, assertion, symptom=True)
            elif item.get("evidence"):
                item["unverified"] = True
            syms.append(item)
        out["symptoms"] = syms

    def labs(self, d: dict, out: dict):
        """Reported lab results — only what the body states verbatim.
        `flag` is kept only when the body itself calls the value out of
        range; the extractor never invents reference ranges."""
        if "labs" not in d:
            return
        if not isinstance(d["labs"], list):
            self.drop_item("labs")
            return
        labs = []
        for lb in d["labs"]:
            fl = self.enum(lb, "flag", _LAB_FLAGS) \
                if isinstance(lb, dict) else False
            subject = self.enum(lb, "subject", _MED_SUBJECTS) if isinstance(lb, dict) else False
            status = self.enum(lb, "status", _MED_STATUSES) if isinstance(lb, dict) else False
            if not (isinstance(lb, dict)
                    and isinstance(lb.get("name"), str)
                    and lb["name"].strip()
                    and type(lb.get("value")) in (int, float, str)
                    and fl is not False and subject is not False and status is not False):
                self.drop_item("labs")
                continue
            item = {"name": lb["name"].strip()[:40]}
            val = lb["value"]
            if type(val) in (int, float) and -1e308 <= val <= 1e308:
                item["value"] = val
            else:
                sv = _clean_text(val, 30)
                if not sv:
                    self.drop_item("labs")
                    continue
                item["value"] = sv
            unit = _clean_text(lb.get("unit"), 15)
            if unit:
                item["unit"] = unit
            if fl:
                item["flag"] = fl
            self.ev(item, lb)
            for key, value in (("subject", subject), ("status", status)):
                if value:
                    item[key] = value
            for key in ("measured_on", "condition"):
                if key not in lb:
                    continue
                value = lb[key]
                if isinstance(value, str) and value.strip() and value in (item.get("evidence") or ""):
                    item[key] = value
                else:
                    self.drop_item("labs." + key)
                    item["unverified"] = True
            item["normalized"] = clinical_values.lab_candidate(
                item["name"], item["value"], item.get("unit"),
                item.get("evidence"), unverified=(
                    item.get("unverified", False)
                    or lb.get("unverified", False) is not False),
                flag=item.get("flag"), subject=subject, status=status,
                measured_on=item.get("measured_on"), condition=item.get("condition"))
            if item["normalized"]["confirmation"] == "unverified":
                item["unverified"] = True
                if self.drops is not None:
                    notes = self.drops.setdefault("labs", [])
                    if len(notes) < 5:
                        notes.append(f"labs.{item['name']} の値・単位・根拠を確認できません")
            labs.append(item)
        out["labs"] = labs

    def events(self, d: dict, out: dict):
        if 'events' not in d and 'event_records' not in d:
            return
        events, records, dropped = clinical_events.normalize_events(
            d.get('events'), d.get('event_records'), self.body, cues=EVENT_CUES)
        for _ in range(dropped):
            self.drop_item('event_records')
        out['events'] = events
        out['event_records'] = records

    def requests(self, d: dict, out: dict):
        if "requests" not in d:
            return
        if not isinstance(d["requests"], list):
            self.drop_item("requests")
            return
        reqs = []
        for r in d["requests"]:
            if not (isinstance(r, dict)
                    and (r.get("to") is None
                         or isinstance(r.get("to"), str))
                    and (r.get("action") is None
                         or isinstance(r.get("action"), str))
                    and (r.get("from") is None
                         or isinstance(r.get("from"), str))):
                self.drop_item("requests")
                continue
            # Ungrounded free text reaches notifications — bound its
            # length (the prompt asks for 30 chars; a runaway or
            # injected string must not ride through unbounded).
            item = {"to": _cap(r.get("to"), 30),
                    "action": _cap(r.get("action"), 60)}
            kind = self.enum(r, "kind", _REQ_KINDS)
            if kind:
                item["kind"] = kind
            self.ev(item, r)
            assertion = self.assertion(item, (item.get("action"),), request=True, sender=r.get("from"))
            if assertion is None:
                item["unverified"] = True
                if item.get("evidence"):
                    self.uncertain(item, "action", nullable=True)
                    self.uncertain(item, "to", nullable=True)
            else:
                quote = assertion[0]
                recipient = item.get("to")
                if recipient and not re.search(re.escape(recipient) + r"(?:さん|様)?(?:へ|に|宛|まで|[、,])", quote):
                    self.uncertain(item, "to", nullable=True)
                sender = _clean_text(r.get("from"), 30)
                if sender:
                    if re.search(re.escape(sender) + r"(?:さん|様)?(?:から|より|が|は)", quote):
                        item["from"] = sender
                    else:
                        item["unverified"] = True
                cond = _clean_text(r.get("condition"), 60)
                inner = locate_quote_span(quote, cond) if cond else None
                if inner is not None:
                    item["condition"] = quote[inner[0]:inner[1]]
                elif cond:
                    self.miss(cond)
                    item["unverified"] = True
                elif clinical_scope.source_axes(self.body, assertion[1], assertion[2])[1] == "conditional":
                    item["unverified"] = True
                due_text = _clean_text(r.get("due_text"), 60)
                if due_text and locate_quote_span(quote, due_text) is not None:
                    item["due_text"] = due_text
                elif due_text:
                    item["unverified"] = True
                due = _clean_text(r.get("due"), 10)
                if due and _valid_date(due):
                    normalized = unicodedata.normalize("NFKC", quote)
                    dates = {clinical_values.absolute_date(m[0]) for m in re.finditer(
                        r"[0-9]{4}[-/年][0-9]{1,2}[-/月][0-9]{1,2}(?![0-9])", normalized)
                        if re.match(r"日?(?:まで|を期限|が期限|を締切|に(?:は)?[^、,。]{0,60}(?:確認|連絡|送付|提出|教え))",
                                    normalized[m.end():])
                        or re.search(r"(?:期限|締切|期日)(?:は|が|[:：])?\s*$", normalized[:m.start()])}
                    if due in dates:
                        item["due"] = due
                    else:
                        self.miss(due)
                        item["unverified"] = True
            reqs.append(item)
        out["requests"] = reqs

    def reply(self, d: dict, out: dict):
        r = d.get("reply")
        if r is None:
            return          # null = no reply (the object/plain rungs)
        kind = self.enum(r, "kind", _REPLY_KINDS) \
            if isinstance(r, dict) else False
        if not kind:
            self.drop_item("reply")
            return
        item = {"kind": kind}
        self.ev(item, r)
        # a typed reply never surfaces without its located quote — and
        # a quote-less one is a counted drop so the repair asks for it
        if "evidence" in item:
            out["reply"] = item
        else:
            self.drop_item("reply")

    def vitals(self, d: dict, out: dict):
        if "vitals" not in d:
            return
        v = d["vitals"]
        if not isinstance(v, dict):
            self.drop_item("vitals")
            return
        vit = {}
        for k in _VITAL_KEYS:
            val = v.get(k)
            if val is None:
                continue
            if type(val) not in (int, float) \
                    or not -1e308 <= val <= 1e308:
                self.drop_item("vitals")
                continue
            vit[k] = float(val)
        if vit and self.body is not None:
            vit = clinical_values.grounded_vitals(self.body, vit, self.drops)
        if vit:
            out["vitals"] = vit

    def current_prose(self, text: str, field: str) -> bool:
        """Account for prose rejected by the canonical source/current measurement guard."""
        if not clinical_values.prose_preserves_current_readings(text, self.body):
            self.drop_item(field, f"{field}の測定値は本文では本人の現在の値ではありません。"
                           "要約・要点でも対象者・時点・条件を保持し、未測定の値を補わないでください。")
            return False
        return True

    def scalars(self, d: dict, out: dict):
        if "summary" in d:
            s = _clean_text(d["summary"], SUMMARY_MAX_CHARS)
            if s:
                if self.current_prose(s, "summary"):
                    out["summary"] = s
            else:
                self.drop_item("summary", f"summaryは{SUMMARY_MAX_CHARS}字以内の全文で返してください。"
                               "否定・条件・主体を途中で切らず、根拠となる事実は各明細に保持してください。")
        if "urgency" in d:
            if d["urgency"] in ("high", "routine", "unclear"):
                out["urgency"] = d["urgency"]
            else:
                self.drop_item("urgency")
        # urgency_evidence: verbatim quotes grounding the verdict, same
        # locate-in-body rule as item evidence. A "high" whose quotes do
        # not locate degrades to "unclear" — never "routine": an
        # unproven alert stays unresolved (the v1 lexical net still
        # applies downstream) rather than being silently cleared.
        if isinstance(d.get("urgency_evidence"), list):
            spans = []
            for q in d["urgency_evidence"]:
                if len(spans) >= 2:
                    break
                span = locate_quote_span(self.body, q) \
                    if isinstance(q, str) and q.strip() \
                    and self.body is not None else None
                if span is None:
                    self.miss(q)
                else:
                    spans.append(self.body[span[0]:span[1]])
            if spans:
                out["urgency_evidence"] = spans
        if out.get("urgency") == "high" and not out.get("urgency_evidence") \
                and self.body is not None:
            out["urgency"] = "unclear"
        if "points" in d:
            if isinstance(d["points"], list):
                points = []
                for raw in d["points"]:
                    point = _clean_text(raw, POINT_MAX_CHARS)
                    if point is None:
                        self.drop_item("points", f"pointsは各{POINT_MAX_CHARS}字以内の全文で返してください。"
                                       "否定・条件・主体を途中で切らず、根拠となる事実は各明細に保持してください。")
                    elif self.current_prose(point, "points") and point not in points:
                        points.append(point)
                out["points"] = points
            else:
                self.drop_item("points")

def _add_urgency_evidence(out: dict, d: dict) -> None:
    """Add up to two distinct quotes; no key is created when d has none."""
    for q in d.get("urgency_evidence") or []:
        if len(out.setdefault("urgency_evidence", [])) < 2 and q not in out["urgency_evidence"]:
            out["urgency_evidence"].append(q)


def merge(outs: list[dict]) -> dict:
    """Deterministic merge of per-chunk validated outputs, in document
    order.

    Exact duplicate medication/symptom records coalesce; distinct evidence,
    attributes and times survive in source order. Current-state reducers
    decide their relationship without erasing the reported history.
    Vitals keep the latest reading per key,
    with blood-pressure sides kept together from one chunk;
    labs retain distinct explicit sampling dates and confirmation groups;
    requests dedupe on (to, from, action, due, condition, due_text).
    urgency is high if any chunk said high (grounded by then — _validate
    already degraded an unquoted high to unclear); else unclear beats
    routine, since residual uncertainty must not masquerade as a
    confident routine. `summary` is dropped — a first-chunk summary is a
    PARTIAL viewpoint and must not be displayed as the whole message's
    gist (points survive: they are additive facts, each still true).
    Drop counters are summed."""
    out: dict = {}
    seen_requests: set = set()
    seen_context: dict = {}
    seen_meds: set = set()
    seen_symptoms: set = set()
    seen_labs: set = set()
    seen_events: set = set()
    for d in outs:
        for m in d.get("meds") or []:
            key = json.dumps(m, sort_keys=True, ensure_ascii=False)
            if key not in seen_meds:
                seen_meds.add(key)
                out.setdefault("meds", []).append(m)
        for s in d.get("symptoms") or []:
            key = json.dumps(s, sort_keys=True, ensure_ascii=False)
            if key not in seen_symptoms:
                seen_symptoms.add(key)
                out.setdefault("symptoms", []).append(s)
        for lab in d.get("labs") or []:
            # Different readings or quotations are not a last-wins update.
            key = json.dumps(lab, sort_keys=True, ensure_ascii=False)
            if key not in seen_labs:
                seen_labs.add(key)
                out.setdefault("labs", []).append(lab)
        for rq in d.get("requests") or []:
            # condition/due_text change what is asked: two chunks'
            # requests differing only there are distinct, never merged
            # (kind stays out — an overlap sentence may be labelled
            # request in one chunk and question in the next)
            k = (rq.get("to"), rq.get("from"), rq.get("action"),
                 rq.get("due"), rq.get("condition"), rq.get("due_text"))
            if k not in seen_requests:
                seen_requests.add(k)
                out.setdefault("requests", []).append(rq)
        for item in d.get("patient_context") or []:
            key = tuple(item[key] for key in ("category", "text", "evidence", "subject"))
            if key not in seen_context:
                clean = dict(item)
                if "details" in item:
                    clean["details"] = list(item["details"])
                seen_context[key] = clean
                out.setdefault("patient_context", []).append(clean)
            elif item.get("details"):
                prior = seen_context[key]
                for detail in item["details"]:
                    if detail not in prior.setdefault("details", []):
                        prior["details"].append(detail)
        for e in d.get("events") or []:
            if e not in out.setdefault("events", []):
                out["events"].append(e)
        for record in d.get('event_records') or []:
            key = json.dumps(record, sort_keys=True, ensure_ascii=False)
            if key not in seen_events:
                seen_events.add(key)
                out.setdefault('event_records', []).append(record)
        for p in d.get("points") or []:
            if p not in out.setdefault("points", []):
                out["points"].append(p)
        vitals = d.get("vitals") or {}
        if "sbp" in vitals or "dbp" in vitals:
            # A later single-side reading never borrows the earlier side.
            prior = out.setdefault("vitals", {})
            prior.pop("sbp", None)
            prior.pop("dbp", None)
        for k, v in vitals.items():
            out.setdefault("vitals", {})[k] = v   # latest reading wins
        if d.get("urgency") == "high":
            out["urgency"] = "high"
            _add_urgency_evidence(out, d)
        if "reply" not in out and d.get("reply"):
            out["reply"] = d["reply"]
        for k in ("_items_dropped", "_evidence_dropped"):
            if d.get(k):
                out[k] = out.get(k, 0) + d[k]
    # Unclear beats routine in the merge: a chunk that could not ground
    # an urgency call leaves residual uncertainty — folding it to
    # routine would silence the v1 lexical net downstream for no gain.
    if "urgency" not in out:
        if any(d.get("urgency") == "unclear" for d in outs):
            out["urgency"] = "unclear"
            for d in outs:
                _add_urgency_evidence(out, d)
        elif outs and all(d.get("urgency") == "routine" for d in outs):
            out["urgency"] = "routine"
    return out

def validate_piece(document, piece, body, drops=None, ctx=False, references=None, *, source_offset=None):
    """Evidence must first belong to the core, and remain unique in the whole source."""
    scoped = validate(document, piece, drops, ctx, references=references)
    if scoped is None or piece == body:
        return scoped
    if scoped.get('event_records'):
        scoped['event_records'] = clinical_events.project_piece_records(
            document, scoped['event_records'], piece, body, source_offset)
    repeated = drops is not None and bool(piece) and body.count(piece) > 1
    global_drops = {} if repeated else drops
    validated = validate(scoped, body, global_drops, ctx, references=references)
    if repeated:
        for reason, values in global_drops.items():
            # No substring of an exactly repeated core can become globally unique.
            # Keep the rejection/counters, but do not ask for an impossible quote.
            if reason == "ev":
                continue
            if reason == "items":
                values = [field for field in values if field not in ("patient_context", "reply")]
            if values:
                target = drops.setdefault(reason, [])
                if reason == "items":
                    values = [field for field in values if field not in target]
                limit = 6 if reason == "vitals" else 5
                target.extend(values[:max(0, limit - len(target))])
    if validated is not None:
        for counter in ("_items_dropped", "_evidence_dropped"):
            validated[counter] = validated.get(counter, 0) + scoped.get(counter, 0)
    return validated
