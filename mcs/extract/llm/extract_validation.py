"""Validate source-grounded extraction fields and merge document chunks without I/O."""
import clinical_scope
import math
import re
import unicodedata

import clinical_values
import extract
import extract_schema
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

_VITAL_CTX_WIN = 40

_VITAL_CTX_EXCLUDE = re.compile(
    r"父|母|義父|義母|(?<![丈工])夫|妻|主人|息子|娘|兄|姉|弟|妹|祖父|祖母|家族|親族"
    r"|なら|たら|れば|の場合|の際|切ったら|超えたら|下回ったら"
    r"|を切ると|を超えると|を下回ると"
    r"|だった|でした|ありました|していた|になった|なりました"
    r"|先月|先週|昨日|以前|過去に"
    r"|測定不能|測れ(?:な|ませ|ず)|測定でき(?:な|ませ|ず)|未測定|不明")


def vital_threshold_flags(vit: dict, body: str | None,
                           thresholds: dict) -> list[dict]:
    """Deterministic threshold crossings on validated vitals.

    Returns [{key, value, rule, evidence}] — the reading's own body
    span (±20 chars) as the verbatim quote. A crossing whose reading
    cannot be anchored to its label, or whose context window shows a
    family/conditional/past/unmeasurable marker, does not flag."""
    if not body or not vit:
        return []
    toks = [(m.start(), m.end(), float(m.group().replace("．", ".")))
            for m in re.finditer(r"\d+(?:[.．]\d+)?", body)]
    rules = (("spo2", "lte", thresholds.get("spo2_lte")),
             ("sbp", "lte", thresholds.get("sbp_lte")),
             ("sbp", "gte", thresholds.get("sbp_gte")),
             ("bs", "lte", thresholds.get("bs_lte")))
    out = []
    for key, op, limit in rules:
        if limit is None or key not in vit:
            continue
        val = vit[key]
        if not ((op == "lte" and val <= limit)
                or (op == "gte" and val >= limit)):
            continue
        span = None
        for s, e, num in toks:
            if num != val:
                continue
            cls = clinical_values._nearest_vital_label(body, s, e)
            if cls == "bp":
                cls = clinical_values._bp_side(body, s, e) or cls
            if cls == key:
                span = (s, e)
                break
        if span is None:
            continue
        s, e = span
        if _VITAL_CTX_EXCLUDE.search(
                body, max(0, s - _VITAL_CTX_WIN), e + _VITAL_CTX_WIN):
            continue
        out.append({"key": key, "value": val,
                    "rule": f"{key}_{op}_{limit:g}",
                    "evidence": body[max(0, s - 20):min(len(body), e + 20)]
                    .strip()})
    return out


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
    flags = vital_threshold_flags(out.get("vitals") or {}, body,
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


def _cap(v, maxlen: int):
    """Length-bound an optional string field; non-strings pass as-is
    (type checks happen at the call site)."""
    return v[:maxlen] if isinstance(v, str) else v


def _clean_text(v, maxlen: int) -> str | None:
    """Strip a free-text field; reject JSON-structural fragments and
    cap length. None means the value is not a usable string."""
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s or _BAD_TEXT.search(s):
        return None
    return s[:maxlen]


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
            self.unlift(item, ("dose", "freq"))
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
                    self.drop_item("meds", f"meds[{index}].subjectと引用の対象者が一致しません。"
                                   "薬剤名を含む本文の引用と服用者を対応させて再抽出してください。")
                    continue
                if missing_name and re.search(r"同薬|同剤|当該薬|その薬|あの薬|上記", quote):
                    # Preserve a possible stop/reference, without confirming an unproved identity.
                    self.miss(item.pop("evidence"))
                    item["unverified"] = True
                elif missing_name:
                    self.drop_item("meds", f"meds[{index}].evidenceにnameが含まれていません。"
                                   "別の文を流用せず、薬剤名を含む本文を引用し、服用者のsubjectを確認してください。")
                    continue
                # Only the named drug's direct non-use contradicts its current affirmation.
                if (st == "current" and m.get("negated") is False
                        and scope not in ("past", "planned", "conditional")
                        and flat_text(quote).count(flat_text(item["name"])) == 1 and re.search(
                        re.escape(flat_text(item["name"])) + extract_schema.RX_DOSE + extract_schema.RX_PARTICLE
                        + r"(?:(?:服用|内服|使用|投与)(?:は|を|も)?"
                          r"(?:して(?:いない|いません)|せず|しない|しません)"
                          r"|飲(?:んで(?:いない|いません)|まず|まない|みません)"
                          r"|使(?:わず|わない|って(?:いない|いません)))"
                          r"(?!こと|事|わけ|訳|場合|とき|時|なら|と|か|が|けど|けれど|ものの"
                          r"|で|だ|ほう|方|よう|予定|つもり|日|回)", flat_text(quote))):
                    self.drop_item("meds", f"meds[{index}]のcurrent・negated:falseが引用の不使用と矛盾します。"
                                   "本文の服用状況を再確認し、否定と時点を保持してください。")
                    continue
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
        if "events" not in d:
            return
        if not isinstance(d["events"], list):
            self.drop_item("events")
            return
        evs = []
        for e in d["events"]:
            if not isinstance(e, str) or e not in _EVENTS:
                continue
            cue = EVENT_CUES.get(e)
            if cue is not None and self.body is not None \
                    and not cue.search(self.body):
                self.items_dropped += 1
                if self.drops is not None:
                    lst = self.drops.setdefault("events", [])
                    if len(lst) < 5 and e not in lst:
                        lst.append(e)
                continue
            evs.append(e)
        out["events"] = evs

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
            # from/due_text are kept only when the body states them
            if isinstance(r.get("from"), str) \
                    and self.grounded(r["from"].strip()[:30]):
                item["from"] = r["from"].strip()[:30]
            kind = self.enum(r, "kind", _REQ_KINDS)
            if kind:
                item["kind"] = kind
            # a condition is a quote: stored only as the body's own
            # span, never as the model's rendering (no fabricated 〜なら).
            # An unlocated one is a counted miss (repair re-asks) and the
            # request stays only as an unverified candidate — never an
            # unconditional instruction.
            cond = _clean_text(r.get("condition"), 60)
            span = locate_quote_span(self.body, cond) \
                if cond and self.body is not None else None
            if span is not None:
                item["condition"] = self.body[span[0]:span[1]]
            elif cond:
                self.miss(cond)
                item["unverified"] = True
            elif r.get("unverified") is True:
                item["unverified"] = True   # re-validated checkpoint
            if isinstance(r.get("due"), str) \
                    and _valid_date(r["due"].strip()):
                item["due"] = r["due"].strip()
            # F09: relative phrasing ('明日まで') is preserved
            # verbatim — never coerced into a guessed ISO date
            if isinstance(r.get("due_text"), str) \
                    and r["due_text"].strip():
                if self.grounded(r["due_text"].strip()[:60]):
                    item["due_text"] = r["due_text"].strip()[:60]
                else:
                    item.pop("due", None)   # derived from an unlocated text
            self.ev(item, r)
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
            vit = clinical_values._vitals_guard(self.body, vit, self.drops)
        if vit:
            out["vitals"] = vit

    def current_prose(self, text: str, field: str) -> bool:
        """Do not present a source's explicitly noncurrent reading without its qualifier."""
        if not isinstance(self.body, str):
            return True
        # Only explicit measurement syntax is checked, never a nearby drug dose or body weight.
        readings = [(m.start(group), m.end(group))
                    for pattern in (*clinical_values.VITAL_PATTERNS.values(),
                                    r"(?:収縮期|拡張期)(?:血圧)?[:：はが=\s]*(\d+(?:[.．]\d+)?)")
                    for m in re.finditer(pattern, text) for group in range(1, (m.lastindex or 0) + 1)]
        for start, end in readings:
            cls = clinical_values._nearest_vital_label(text, start, end)
            if cls is None or clinical_scope.patient_source_scope(text, start, end) != "patient":
                continue
            left = max(text.rfind(mark, 0, start) for mark in "。！？!?\n") + 1
            if (re.search(clinical_values._OTHER_TIME, text[left:start])
                    and not clinical_values._vital_current(text, start)):
                continue
            hits = [m.span() for m in re.finditer(r"\d+(?:[.．]\d+)?", self.body)
                    if clinical_values._bp_number(m[0]) == clinical_values._bp_number(text[start:end])
                    and clinical_values._nearest_vital_label(self.body, *m.span()) == cls]
            if hits and (all(clinical_scope.patient_source_scope(self.body, s, e)
                             in clinical_values._NOT_PATIENT_CURRENT for s, e in hits)
                         or clinical_values._vital_superseded(self.body, cls, hits)):
                self.drop_item(field, f"{field}の測定値は本文では本人の現在の値ではありません。"
                               "要約・要点でも対象者・時点・条件を保持し、未測定の値を補わないでください。")
                return False
        return True

    def scalars(self, d: dict, out: dict):
        if "summary" in d:
            s = _clean_text(d["summary"], 60)
            if s:
                if self.current_prose(s, "summary"):
                    out["summary"] = s
            else:
                self.drop_item("summary")
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
                out["points"] = [p for p in
                                 (_clean_text(x, 40) for x in d["points"])
                                 if p and self.current_prose(p, "points")][:3]
            else:
                self.drop_item("points")

def merge(outs: list[dict]) -> dict:
    """Deterministic merge of per-chunk validated outputs, in document
    order.

    Identity keys preserve in-message progression rather than collapsing
    it: meds dedupe on (name, subject, action) so a past stop and a
    planned restart coexist; symptoms keep the LAST status per text —
    a later "resolved" must overwrite an earlier "new" or downstream
    resolvers never fire; vitals keep the latest reading per key,
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
    med_order: dict = {}
    sym_idx: dict = {}
    lab_idx: dict = {}
    for d in outs:
        for m in d.get("meds") or []:
            k = (m["name"], m.get("subject"), m.get("action"))
            # Retain the latest occurrence in source order: start -> stop
            # -> start must end at the restart, including its new dose.
            out.setdefault("meds", [])
            med_order.pop(k, None)
            med_order[k] = m
        for s in d.get("symptoms") or []:
            key = (s["text"], s.get("subject"))
            if key in sym_idx:
                out["symptoms"][sym_idx[key]] = s
            else:
                sym_idx[key] = len(out.setdefault("symptoms", []))
                out["symptoms"].append(s)
        for lab in d.get("labs") or []:
            normalized = lab.get("normalized") or {}
            key = (lab["name"], lab.get("subject"), lab.get("status"),
                   lab.get("measured_on", normalized.get("measured_on")),
                   lab.get("condition"), lab.get("unverified", False))
            if key in lab_idx:
                out["labs"][lab_idx[key]] = lab
            else:
                lab_idx[key] = len(out.setdefault("labs", []))
                out["labs"].append(lab)
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
        for p in d.get("points") or []:
            if len(out.setdefault("points", [])) < 3 \
                    and p not in out["points"]:
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
            for q in d.get("urgency_evidence") or []:
                if len(out.setdefault("urgency_evidence", [])) < 2 \
                        and q not in out["urgency_evidence"]:
                    out["urgency_evidence"].append(q)
        if "reply" not in out and d.get("reply"):
            out["reply"] = d["reply"]
        for k in ("_items_dropped", "_evidence_dropped"):
            if d.get(k):
                out[k] = out.get(k, 0) + d[k]
    if med_order:
        out["meds"] = list(med_order.values())
    # Unclear beats routine in the merge: a chunk that could not ground
    # an urgency call leaves residual uncertainty — folding it to
    # routine would silence the v1 lexical net downstream for no gain.
    if "urgency" not in out:
        if any(d.get("urgency") == "unclear" for d in outs):
            out["urgency"] = "unclear"
            for d in outs:
                for q in d.get("urgency_evidence") or []:
                    if len(out.setdefault("urgency_evidence", [])) < 2 \
                            and q not in out["urgency_evidence"]:
                        out["urgency_evidence"].append(q)
        elif outs and all(d.get("urgency") == "routine" for d in outs):
            out["urgency"] = "routine"
    return out

def validate_piece(document, piece, body, drops=None, ctx=False, references=None):
    """Evidence must first belong to the core, and remain unique in the whole source."""
    scoped = validate(document, piece, drops, ctx, references=references)
    if scoped is None or piece == body:
        return scoped
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
