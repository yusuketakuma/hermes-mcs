"""Structured display view over extraction artifacts.

Shared formatter for the extract_v1/extract_llm artifacts — used by the
text notify_flush (_fmt's 📋 要約 section) and the interactive card
renderer (notify_render._structured_block). Keeping one implementation
means the suppression rules (negated/resolved symptoms, other-person
meds, unverified-medication labelling) and the freshness gate can never
diverge between surfaces.

Freshness/safety gate lives in latest_artifact's SQL: the artifact must
be bound to the message's CURRENT content_hash and the message must not
be deleted — a tombstoned message never contributes extracted data.

Merge contract (v1 rules ∪ llm ∪ v4 canonical facts), per field:
  events   — llm authoritative when present; rules add media references only.
  vitals   — preserve one source; no cross-subject or cross-time key filling.
  symptoms — llm entries carry polarity/subject; v1 tokens merge in
             unless they overlap an llm symptom (typed negation must not
             resurface as a bare symptom token).
  meds     — llm ONLY when it emitted meds (even all-filtered): falling
             back to v1 would resurrect filtered negated/family items.
             v1 feeds "薬剤候補（未確認）" only when llm saw nothing.
  requests — llm preferred; v1 formulaic 確認/連絡 fill only when llm
             emitted none.
  canonical facts — appended last as the highest-authority layer.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from drug_map import KIND as REF_KIND, candidate_note, current_refs, generation_signature
from mcs_queries import (FACT_KINDS_SQL, current_extract_pred, current_fact_pred,
                         json_or_null, med_is_patient_current, item_unverified)
from mcs_util import loads_dict
from semantic_render import _LINE_ATTRS


def _content_dict(r) -> dict | None:
    """json.loads an artifact content blob — non-object or corrupt JSON
    is simply absent, never a view-killing error."""
    return loads_dict(r["content"]) if r else None


def latest_artifact(db, kind: str, mid: int) -> dict | None:
    r = db.execute(
        "SELECT a.content FROM artifacts a JOIN messages m "
        "ON m.message_id=a.message_id WHERE a.kind=? AND a.message_id=? "
        f"{current_extract_pred()} "
        "ORDER BY a.artifact_id DESC LIMIT 1", (kind, mid)).fetchone()
    return _content_dict(r)


def _drug_refs(db, mid: int) -> list:
    """Annotation failure never suppresses the post's raw medication facts."""
    if db is None:
        return []
    try:
        return current_refs(db, mid)
    except (sqlite3.DatabaseError, ValueError, TypeError, RecursionError):
        return []


def _drug_note(ref) -> str:
    """Only existing unconfirmed-candidate wording; never infer clinical equivalence."""
    try:
        note = candidate_note(ref)
        note.encode("utf-8")
        return note
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
        return ""


def fact_generations(db, mids: list) -> dict:
    """Current selected fact and rule generations, fetched in bounded batches."""
    ids = list(dict.fromkeys(int(m) for m in mids if isinstance(m, int) or
                            (isinstance(m, str) and m.isdigit())))
    result = {}
    dictionary_generation = None
    for offset in range(0, len(ids), 400):
        batch = ids[offset:offset + 400]
        marks = ",".join("?" * len(batch))
        for r in db.execute(
                "SELECT a.message_id,a.kind,MAX(a.artifact_id) generation "
                "FROM artifacts a JOIN messages m ON m.message_id=a.message_id "
                f"WHERE a.message_id IN ({marks}) AND ("
                f"(a.kind='extract_v1' {current_extract_pred()}) OR "
                f"(a.kind IN ({FACT_KINDS_SQL}) "
                f"{current_fact_pred()})) GROUP BY a.message_id,a.kind", batch):
            result.setdefault(r["message_id"], {})[r["kind"]] = r["generation"]
        ref_mids = db.execute(
            f"SELECT DISTINCT message_id FROM artifacts WHERE kind=? "
            f"AND message_id IN ({marks})", (REF_KIND, *batch)).fetchall()
        for row in ref_mids:
            mid = row[0]
            if mid not in result:
                continue  # an annotation alone is not a ready extraction
            refs = _drug_refs(db, mid)
            if not refs:
                continue  # disabled/corrupt/removed annotations share the same absent state
            if dictionary_generation is None:
                dictionary_generation = generation_signature(db)
            # Hash the usable annotations, not rewrite IDs/cursor movement.
            # Direct corruption changes this too, even without a new artifact ID.
            result[mid]["med_ref"] = hashlib.sha256(json.dumps(
                [dictionary_generation, refs], sort_keys=True
            ).encode()).hexdigest()
    return result


def latest_fact_artifact(db, mid: int) -> dict | None:
    """Newest usable fact artifact for a message — a hash-current
    ``canonical_projection`` shadows ``extract_llm`` (the same
    ``current_fact_pred`` shadow rule every consumer shares); when the
    projection is stale, invalidated, or absent the legacy extraction
    is read instead — never the other way around."""
    r = db.execute(
        "SELECT a.content FROM artifacts a JOIN messages m "
        "ON m.message_id=a.message_id WHERE a.message_id=? "
        f"AND a.kind IN ({FACT_KINDS_SQL}) "
        "AND m.body_state IS NOT 'deleted' "
        "AND CASE WHEN json_valid(a.content) THEN "
        "json_type(a.content)='object' ELSE 0 END "
        f"{current_fact_pred('a', 'm')} "
        "ORDER BY a.artifact_id DESC LIMIT 1", (mid,)).fetchone()
    return _content_dict(r)


RX_LABEL = {"start": "開始", "stop": "中止", "change": "変更",
            "none": "変更なし", "no_change": "変更なし",
            "increase": "増量", "decrease": "減量"}
REQ_LABEL = {"confirm": "確認", "contact": "連絡", "share": "共有",
             "ask": "質問", "request": "依頼", "report": "報告"}
EVT_LABEL = {"admission": "入院", "discharge": "退院", "exam": "受診/検査",
             "visit": "訪問", "medication": "投薬", "adherence": "服薬",
             "care": "介護", "eol": "看取り", "media_ref": "添付",
             "transfer": "転院/移動", "fall": "転倒",
             "family_contact": "家族連絡", "other": "その他"}
# schema v4 detail labels
_SEVERITY_JP = {"mild": "軽度", "moderate": "中等度", "severe": "重度"}
_ROUTE_JP = {"oral": "内服", "topical": "外用", "injection": "注射",
             "infusion": "点滴", "inhalation": "吸入", "tube": "経管",
             "other": "その他"}
_LAB_FLAG_JP = {"high": "高", "low": "低"}


# Canonical-only categories: these v2 kinds have no legacy slot, so
# they render straight from ``canonical_facts`` — fact_id and evidence
# stay attached and the statement is never squeezed into a wrong field.
_FINDING_LABEL = {"allergy_intolerance": "アレルギー・不耐",
                  "adverse_drug_event": "有害事象",
                  "vital_lab": "バイタル・検査",
                  "preference": "希望",
                  "other_observation": "所見"}


def _items(doc: dict, key: str) -> list:
    value = doc.get(key)
    return value if isinstance(value, list) else []


def _label(labels: dict, value):
    return labels.get(value) if isinstance(value, str) else None


def _empty_field(doc: dict, key: str) -> bool:
    return key not in doc or (isinstance(doc[key], list) and not doc[key])


def _source_context(db, mid):
    if db is None:
        return None, None, "patient"
    columns = {r[1] for r in db.execute("PRAGMA table_info(patients)")}
    name = "p.patient_name" if "patient_name" in columns else "NULL"
    kind = "p.project_type" if "project_type" in columns else "NULL"
    join = "LEFT JOIN patients p ON p.project_id=m.project_id" if columns else ""
    row = db.execute(f"SELECT m.body_text,m.body_state,{name},{kind} FROM messages m "
                     f"{join} WHERE m.message_id=?", (mid,)).fetchone()
    if row is None:
        return "", None, "patient"
    body = row[0] if row[1] in (None, "full") else ""
    return body, row[2], "unknown" if row[3] == "group" else "patient"


def _item_scope(item, context, surface=None):
    from clinical_values import patient_item_scope
    body, name, default = context
    if body is None:
        return default  # retained parsed-only callers have no original body
    return patient_item_scope(item, body, patient_name=name, surface=surface, default=default)


def _scoped_vitals(values, context):
    from extract import patient_vitals
    body, name, default = context
    if body is None:
        return values
    values = patient_vitals(values, body, patient_name=name)
    if default != "patient":
        values = {key: value for key, value in values.items()
                  if _item_scope({}, context, str(value)) == "patient"}
    return values


def _canonical_finding_lines(llm: dict, *, context=(None, None, "patient")) -> list[str]:
    """Structured lines for verified facts that no legacy slot can
    carry. Interpretation qualifiers precede the shortened statement."""
    out = []
    facts = llm.get("canonical_facts")
    if not isinstance(facts, list):
        return out
    seen = set()
    for f in facts:
        if not isinstance(f, dict):
            continue
        label = _label(_FINDING_LABEL, f.get("kind"))
        statement = f.get("statement")
        fid = f.get("fact_id")
        if label is None or not isinstance(statement, str) \
                or not statement.strip() or not isinstance(fid, str) \
                or not fid.strip() or fid in seen:
            continue
        seen.add(fid)
        scope = _item_scope({"evidence": f.get("evidence_quote")}, context, statement)
        if scope in ("family", "other", "unknown"):
            f = {**f, "subject": {"family": "家族", "other": "本人以外", "unknown": "未確認"}[scope]}
        attrs = [f"{name}:{f[key]}" for key, name in
                 (("subject", "対象"), *_LINE_ATTRS, ("event_time", "時点"))
                 if isinstance(f.get(key), str) and f[key].strip()]
        if scope in ("past", "planned", "conditional"):
            attrs.append("原文:" + {"past": "過去の報告", "planned": "予定", "conditional": "条件・可能性の記載"}[scope])
        qualifier = f"（{'、'.join(attrs)}）" if attrs else ""
        line = f"{label}｜{qualifier}{statement.strip()}"
        quote = f.get("evidence_quote")
        if isinstance(quote, str) and quote.strip():
            line += f"（根拠:{quote.strip()}）"
        out.append(line)
    return out


# Where high urgency came from: a lexical rule match is only the 🚨 icon —
# not a clinical assessment — while the AI verdict adds its wording.
# owner 2026-10-08: no "(AI抽出)" style qualifiers on anything new; the
# legacy wording stays only for layout-1 cards so their faces never drift
URGENCY_LABEL_PLAIN = {"llm": "🚨 緊急度: 高", "rule": "🚨"}
URGENCY_LABEL = {"llm": "🚨 緊急度: 高（AI抽出）",
                 "rule": "🚨"}


def message_urgency(db, mid: int) -> str | None:
    """'llm' when the current fact artifact says urgency high, None when it
    says routine (the content-level verdict supersedes the lexical rule),
    'rule' when only extract_v1 flags it and no LLM verdict exists — the one
    urgency reading for cards, text notices and signal escalation.
    canonical_projection / semantic_facts_v4 rows carry no urgency, so the
    newest hash-current extract_llm row supplies the verdict behind them."""
    return message_urgency_details(db, mid)["source"]


def message_urgency_details(db, mid: int) -> dict:
    """Report guarded urgency separately from the model's raw verdict and preserve review holds."""
    from extract import (clinical_urgency_quote, patient_clinical_quotes, patient_rule_urgency_quotes,
                         patient_urgency_quotes, urgent_request_quote)

    document = latest_fact_artifact(db, mid) or {}
    verdict = document.get("urgency")
    if verdict not in ("high", "routine", "unclear"):
        document = latest_artifact(db, "extract_llm", mid) or {}
        verdict = document.get("urgency")
    result = {"source": None, "subject": "unknown", "reasons": [], "raw_verdict": verdict,
              "verdict": verdict, "held": False, "kind": "clinical"}
    rule = latest_artifact(db, "extract_v1", mid) or {}
    if verdict not in ("high", "routine", "unclear") and rule.get("urgency") != "high":
        return result
    if db is None:
        if verdict == "high":
            result.update(verdict="unclear", held=True, scopes=["unknown"])
        return result
    columns = {column[1] for column in db.execute("PRAGMA table_info(patients)")}
    name = "p.patient_name" if "patient_name" in columns else "NULL"
    kind = "p.project_type" if "project_type" in columns else "NULL"
    row = db.execute(f"SELECT m.body_text,m.body_state,{name} patient_name,{kind} project_type FROM messages m "
                     "LEFT JOIN patients p ON p.project_id=m.project_id WHERE m.message_id=?", (mid,)).fetchone()
    if row is None or row["body_state"] not in (None, "full") or not isinstance(row["body_text"], str):
        return result
    body = row["body_text"]
    default = "unknown" if row["project_type"] == "group" else "patient"
    if default == "unknown":
        if verdict == "high":
            result.update(verdict="unclear", held=True, scopes=["unknown"])
        return result
    if verdict == "high":
        quotes = document.get("urgency_evidence")
        reasons, scopes = patient_urgency_quotes(
            body, quotes if isinstance(quotes, list) else [], patient_name=row["patient_name"], default=default)
        clinical = [quote for quote in reasons if clinical_urgency_quote(quote)]
        if clinical:
            return {**result, "source": "llm", "subject": "patient", "reasons": clinical[:2]}
        if reasons and any(urgent_request_quote(quote) for quote in reasons):
            result.update(kind="request", reasons=reasons[:2])
        result.update(verdict="unclear", held=True, scopes=scopes or ["unknown"])
    if verdict == "routine":
        return result
    # "unclear" is an abstention, not a clearance — it falls through to
    # the lexical net exactly like a missing verdict does.
    if rule.get("urgency") == "high":
        reasons = patient_rule_urgency_quotes(body)
        scoped, _ = patient_urgency_quotes(body, reasons, patient_name=row["patient_name"], default=default)
        clinical = [quote for quote in scoped if clinical_urgency_quote(quote)]
        if not clinical and any(urgent_request_quote(quote) for quote in scoped):
            clinical = patient_clinical_quotes(body, patient_name=row["patient_name"])
        if clinical and default == "patient":
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
    row = db.execute(
        "SELECT q.content FROM artifacts q"
        " JOIN messages m ON m.message_id=q.message_id"
        " WHERE q.kind='extract_qc' AND q.message_id=?"
        f" {current_extract_pred('q', 'm')}"
        f" AND json_extract({json_or_null('q.meta')},"
        "'$.source_artifact_id')="
        "  (SELECT MAX(a.artifact_id) FROM artifacts a"
        "   JOIN messages m2 ON m2.message_id=a.message_id"
        "   WHERE a.kind='extract_llm' AND a.message_id=q.message_id"
        f"   {current_extract_pred('a', 'm2')})"
        " ORDER BY q.artifact_id DESC LIMIT 1", (mid,)).fetchone()
    urg = (_content_dict(row) or {}).get("urgency")
    if not isinstance(urg, dict) or urg.get("jev") is None \
            or urg.get("jev") == urg.get("extracted"):
        return None
    return urg


_QC_URGENCY_SUFFIX = {"routine": "（監査では通常判定）",
                      "unclear": "（監査では判断保留）"}


def urgency_qc_suffix(db, mid: int) -> str:
    """Display suffix marking a displayed 'llm' high badge whose newest
    current QC disagreed — empty string when QC agrees or never ran."""
    qc = urgency_qc_disagreement(db, mid)
    if qc and qc.get("jev") in _QC_URGENCY_SUFFIX:
        return _QC_URGENCY_SUFFIX[qc["jev"]]
    return ""


_VFLAG_LABEL = {"spo2": "SpO2", "sbp": "収縮期BP", "bs": "BS"}


def _head_lines(llm: dict | None, v1: dict, urgency: str | None = None,
                urgency_suffix: str = "", plain: bool = False) -> list[str]:
    selected = llm is not None
    llm = llm or {}
    lines: list[str] = []
    labels = URGENCY_LABEL_PLAIN if plain else URGENCY_LABEL
    if urgency in labels:
        lines.append(labels[urgency] + urgency_suffix)
    summary = llm.get("summary")
    if isinstance(summary, str) and summary.strip():
        lines.append(summary.strip())
    pts = [p.strip() for p in _items(llm, "points")
           if isinstance(p, str) and p.strip()]
    if pts:
        lines.append("要点: " + " / ".join(pts))
    # LLM omissions can encode negation, subject or temporal exclusions.
    rule_events = _items(v1, "events")
    if selected:
        rule_events = [event for event in rule_events if event == "media_ref"]
    events = _items(llm, "events") + rule_events
    evs = list(dict.fromkeys(event for event in events
                             if _label(EVT_LABEL, event)))
    if evs:
        lines.append("区分: " + "・".join(EVT_LABEL[e] for e in evs))
    return lines


def _vital_line(llm: dict | None, v1: dict, body=None, *, patient_name=None, default="patient"):
    selected = llm is not None
    llm = llm or {}
    lv = llm.get("vitals") if isinstance(llm.get("vitals"), dict) else {}
    vv = v1.get("vitals") if isinstance(v1.get("vitals"), dict) else {}
    # A missing LLM key may be an intentional subject/time exclusion.
    # Keep a reading intact; never construct a BP pair across sources.
    vit = lv if selected else vv
    vit = _scoped_vitals(vit, (body, patient_name, default))
    if not vit:
        return None
    parts = []
    if vit.get("sbp") is not None or vit.get("dbp") is not None:
        # a half-extracted pair renders its known side only — never
        # the literal "None" in a clinical line
        sbp, dbp = vit.get("sbp"), vit.get("dbp")
        parts.append(f"BP {'?' if sbp is None else sbp}"
                     f"/{'?' if dbp is None else dbp}")
    for k, lab in (("bt", "BT"), ("hr", "HR"), ("rr", "RR"),
                   ("spo2", "SpO2"), ("bs", "BS")):
        if vit.get(k) is not None:
            parts.append(f"{lab} {vit[k]}")
    return "バイタル: " + "  ".join(parts) if parts else None


def _lab_lines(llm: dict, body: str | None = None, *, patient_name=None, default="patient") -> list[str]:
    """Reported lab values (v4) — name+value+unit plus the body's own
    out-of-range marker; the view never invents reference ranges."""
    from clinical_values import lab_candidate
    from mcs_util import locate_quote_span

    confirmed, candidates = [], []
    for lb in _items(llm, "labs"):
        if not isinstance(lb, dict) or not isinstance(lb.get("name"), str) \
                or not lb["name"]:
            continue
        if type(lb.get("value")) not in (int, float, str):
            continue
        evidence = lb.get("evidence")
        located = (isinstance(evidence, str) and body is not None
                   and locate_quote_span(body, evidence) is not None)
        contextual = (lb.get("subject") in ("family", "other")
                      or lb.get("status") == "planned" or bool(lb.get("condition")))
        scope = _item_scope(lb, (body, patient_name, default), lb["name"])
        contextual = contextual or scope not in ("patient", "past")
        normalized = lab_candidate(
            lb["name"], lb["value"],
            lb.get("unit") if isinstance(lb.get("unit"), str) else None,
            evidence if located else None,
            unverified=item_unverified(lb) or contextual,
            flag=lb.get("flag") if lb.get("flag") in ("high", "low") else None,
            patient_name=patient_name)
        d = f"{lb['name']} {lb.get('value')}"
        if isinstance(lb.get("unit"), str) and lb["unit"]:
            d += lb["unit"]
        if (flag := _label(_LAB_FLAG_JP, lb.get("flag"))):
            d += f"({flag})"
        if normalized["measured_on"]:
            d += f"(測定日:{normalized['measured_on']})"
        elif (located and isinstance(lb.get("measured_on"), str)
              and lb["measured_on"] and lb["measured_on"] in evidence):
            d += f"(測定時期:{lb['measured_on']})"
        if lb.get("subject") in ("family", "other"):
            d += "(対象:" + ("家族" if lb["subject"] == "family" else "本人以外") + ")"
        elif scope in ("family", "other", "unknown"):
            d += "(対象:" + {"family": "家族", "other": "本人以外", "unknown": "未確認"}[scope] + ")"
        if lb.get("status") in ("past", "planned"):
            d += "(過去の報告)" if lb["status"] == "past" else "(予定)"
        elif scope in ("past", "planned", "conditional"):
            d += "(" + {"past": "過去の報告", "planned": "予定", "conditional": "条件・可能性の記載"}[scope] + ")"
        if isinstance(lb.get("condition"), str) and lb["condition"]:
            d += f"(条件:{lb['condition']})"
        (candidates if normalized["confirmation"] == "unverified"
         else confirmed).append(d)
        if len(confirmed) + len(candidates) == 6:
            break
    lines = []
    if confirmed:
        lines.append("検査: " + "・".join(confirmed))
    if candidates:
        lines.append("検査候補（未確認）: " + "・".join(candidates))
    return lines


def _symptom_line(llm: dict, v1: dict, *, context=(None, None, "patient")):
    syms, neg, seen, neg_seen = [], [], set(), set()
    llm_symptoms = [s for s in _items(llm, "symptoms")
                    if isinstance(s, dict) and isinstance(s.get("text"), str)
                    and s["text"]]
    for s in llm_symptoms:
        if s.get("subject") in ("family", "other") \
                or item_unverified(s):
            continue
        if _item_scope(s, context, s["text"]) != "patient":
            continue
        if s.get("negated") or s.get("status") in ("resolved", "past"):
            if s["text"] not in neg_seen:
                neg_seen.add(s["text"])
                if s.get("negated"):
                    neg.append(s["text"])
        elif s["text"] not in seen:
            seen.add(s["text"])
            parts = []
            sev = _label(_SEVERITY_JP, s.get("severity"))
            if sev:
                parts.append(sev)
            parts += [x.strip() for x in (s.get("onset"), s.get("duration"))
                      if isinstance(x, str) and x.strip()]
            syms.append(s["text"]
                        + (f"({'・'.join(parts)})" if parts else ""))
    # An unreadable selected field can carry exclusions; do not revive rules.
    raw_symptoms = llm.get("symptoms", [])
    rule_symptoms = _items(v1, "symptoms") if isinstance(raw_symptoms, list) \
        and len(llm_symptoms) == len(raw_symptoms) else []
    for s in rule_symptoms:
        if not isinstance(s, str):
            continue
        if _item_scope({}, context, s) != "patient":
            continue
        if any(x["text"] in s or s in x["text"] for x in llm_symptoms):
            continue  # Typed polarity/subject must not reappear through rules.
        if s and s not in seen:
            syms.append(s)
    if not syms and not neg:
        return None
    line = "症状: " + "、".join(syms)
    if neg:
        line += ("　" if syms else "") + "、".join(
            f"{n}なし" for n in neg)
    return line


def _med_entries(llm: dict, v1: dict, refs=(), *, context=(None, None, "patient")) -> tuple[list, list]:
    """(confirmed, unverified) medication entries as ``(text, ref)`` —
    ``ref`` is the current dictionary annotation of exactly that mention
    (same source list index and surface name), else None. The card's
    薬剤 lines and the 💊 薬剤を確認 view share this one selection."""
    annotations = {(r["source_kind"] == "extract_v1", r["i"]): r for r in refs}

    def ref_of(rule, i, item):
        ref = annotations.get((rule, i))
        return ref if ref and ref.get("name") == item.get("name") else None

    meds = []
    for i, m in enumerate(_items(llm, "meds")):
        if not isinstance(m, dict) or not m.get("name"):
            continue
        # negated / other-person / historical meds must not read as the
        # patient's own medication (planned survives — shown as [予定])
        if not med_is_patient_current(m):
            continue
        scope = _item_scope(m, context, str(m["name"]))
        if scope != "patient" and not (scope == "planned" and m.get("status") == "planned"):
            continue
        d = str(m["name"]) + (f" {m['dose']}" if m.get("dose") else "")
        if (action := _label(RX_LABEL, m.get("action"))):
            d += f"[{action}]"
        if m.get("status") == "planned":
            d += "[予定]"
        tail = []
        if (route := _label(_ROUTE_JP, m.get("route"))):
            tail.append(route)
        if isinstance(m.get("freq"), str) and m["freq"]:
            tail.append(m["freq"])
        if m.get("prn") is True:
            tail.append("頓服")
        if tail:
            d += f"({'・'.join(tail)})"
        meds.append((d, ref_of(False, i, m)))
    unverified = []
    if _empty_field(llm, "meds"):
        # v1 fallback only when the LLM saw NO meds — if it saw meds
        # but all were filtered (negated/family/past), falling back to
        # v1 would re-display the very mentions that were filtered out
        unverified.extend(
            (str(m["name"]) + (f" {m['dose']}" if m.get("dose") else ""),
             ref_of(True, i, m))
            for i, m in enumerate(_items(v1, "medications"))
            if isinstance(m, dict) and m.get("name")
            and _item_scope(m, context, str(m["name"])) == "patient")
        unverified.extend(
            (f"{RX_LABEL[a['action']]}:{a['ctx']}", None)
            for a in _items(v1, "rx_actions")
            if isinstance(a, dict) and _label(RX_LABEL, a.get("action"))
            and isinstance(a.get("ctx"), str) and a["ctx"]
            and _item_scope({}, context, a["ctx"]) == "patient")
    return meds, unverified


def _med_lines(llm: dict, v1: dict, refs=(), *, context=(None, None, "patient")) -> list[str]:
    meds, unverified = _med_entries(llm, v1, refs, context=context)

    def shown(entries):
        return "、".join(text + (f"（{note}）" if (note := _drug_note(ref)) else "")
                        for text, ref in entries)

    lines = []
    if meds:
        lines.append("薬剤: " + shown(meds))
    if unverified:
        lines.append("薬剤候補（未確認）: " + shown(unverified))
    return lines


def medication_entries(db, mid: int) -> tuple[list, list]:
    """The post's current (confirmed, unverified) medication entries with
    their dictionary annotations — the 💊 薬剤を確認 view's input."""
    v1 = latest_artifact(db, "extract_v1", mid) or {}
    return _med_entries(latest_fact_artifact(db, mid) or {}, v1, _drug_refs(db, mid),
                        context=_source_context(db, mid))


# per-item prefix inside the 依頼: line — a plan of the poster or a
# question is not an order to somebody (#20 order 3)
_REQ_KIND_PREFIX = {"self_plan": "予定:", "question": "確認依頼:"}


def _request_lines(llm: dict, v1: dict) -> list[str]:
    reqs, cands = [], []
    for r in _items(llm, "requests"):
        if isinstance(r, dict) and (r.get("to") or r.get("action")):
            to = str(r.get("to") or "")
            to = "" if to in ("", "不明", "unknown", "-") else f"{to}へ"
            frm = str(r.get("from") or "")
            prefix = "" if frm in ("", "不明", "unknown", "-") \
                else f"{frm}→"
            kind = r.get("kind")
            if isinstance(kind, str):
                prefix = _REQ_KIND_PREFIX.get(kind, "") + prefix
            # a relative deadline (due null, due_text kept verbatim) is
            # still a deadline to the reader
            due = r.get("due")
            due = due if isinstance(due, str) and due else r.get("due_text")
            suffix = f"(期限:{due})" if isinstance(due, str) and due \
                else ""
            # owner rule 2026-10-05: the 要約 shows every word — no caps
            cond = r.get("condition")
            cond = cond if isinstance(cond, str) else ""
            if cond:
                suffix += f"(条件:{cond})"
            action = str(r.get("action") or "")
            # negated/speculative/ungrounded requests must not read as
            # confirmed; any flag other than a literal False fails closed
            (cands if item_unverified(r) else reqs).append(
                prefix + to + action + suffix)
    # rule fallback only when the selected facts carry no request at all
    if _empty_field(llm, "requests"):
        reqs.extend(("" if (_label(REQ_LABEL, r.get("kind")) or "依頼") == "依頼"
                     else f"{_label(REQ_LABEL, r.get('kind'))}:") + r["ctx"]
                    for r in _items(v1, "requests")
                    if isinstance(r, dict) and isinstance(r.get("ctx"), str)
                    and r["ctx"])
    lines = ["依頼: " + " / ".join(reqs)] if reqs else []
    if cands:
        lines.append("依頼候補（未確認）: " + " / ".join(cands))
    return lines


def structured_lines(db, mid: int, *, drug_candidates: bool = True,
                     plain: bool = False) -> list[str]:
    """Compact structured summary from extract_v1 + the current fact
    artifact (canonical_projection shadows extract_llm).  Returns []
    when nothing usable exists (caller falls back to raw only)."""
    v1 = latest_artifact(db, "extract_v1", mid) or {}
    selected = latest_fact_artifact(db, mid)
    llm = selected or {}
    if not v1 and not llm:
        return []
    details = message_urgency_details(db, mid)
    urgency = details["source"]
    # Fact artifacts (canonical projections) carry no urgency fields —
    # the extract_llm row behind them holds evidence and vital_flags.
    ext = (latest_artifact(db, "extract_llm", mid) or {}) \
        if urgency or llm else {}
    suffix = ""
    if urgency == "llm":
        ev = (llm.get("urgency_evidence") or ext.get("urgency_evidence")
              or [])
        if ev:
            suffix = f" — 根拠:「{str(ev[0])[:40]}」"
        suffix += urgency_qc_suffix(db, mid)
    lines: list[str] = _head_lines(selected, v1, urgency, suffix, plain)
    if details["held"]:
        lines.append("急ぎの確認依頼（本人の緊急状態とは別）" if details["kind"] == "request" else
                     "緊急度: 要確認（対象人物・時点の根拠を確認）" if plain else
                     "緊急度: 要確認（対象人物・時点の根拠を確認。元のAI判定は高）")
    elif details["kind"] == "request":
        lines.append("急ぎの確認依頼（本人の緊急状態とは別）")
    context = _source_context(db, mid)
    body, patient_name, default = context
    # Public source-less views cannot prove a measurement. Direct helpers and
    # nullable-body legacy rows retain their parsed-only compatibility.
    vital_context = ("", patient_name, default) if db is None else context
    if (line := _vital_line(selected, v1, vital_context[0], patient_name=patient_name, default=default)) is not None:
        lines.append(line)
    flags = [_VFLAG_LABEL.get(f["key"], f["key"]) + f" {f['value']:g}"
             for f in (llm.get("vital_flags") or ext.get("vital_flags")
                       or [])
             if isinstance(f, dict) and f.get("key") in _VFLAG_LABEL
             and type(f.get("value")) in (int, float)
             and _scoped_vitals({f["key"]: f["value"]}, vital_context).get(f["key"]) == f["value"]]
    if flags and body:
        lines.append("閾値超過の測定値: " + "、".join(flags))
    if _items(llm, "labs"):
        lines.extend(_lab_lines(llm, body, patient_name=patient_name, default=default))
    if (line := _symptom_line(llm, v1, context=context)) is not None:
        lines.append(line)
    lines.extend(_med_lines(llm, v1, _drug_refs(db, mid) if drug_candidates else (), context=context))
    lines.extend(_request_lines(llm, v1))
    for mp in _items(v1, "med_periods"):
        if isinstance(mp, dict) and mp.get("start") and _item_scope(mp, context, mp.get("raw")) == "patient":
            lines.append(f"服薬期間: {mp['start']}〜{mp.get('end') or '?'}")
            break
    if v1.get("next_planned"):
        lines.append(f"次回予定: {v1['next_planned']}")
    lines.extend(_canonical_finding_lines(llm, context=context))
    if plain:
        # owner 2026-10-08: one fixed reading order on new cards and notices
        # (missing items are simply absent); layout-1 cards keep the old order
        lines.sort(key=_summary_rank)
    return lines


# 緊急度 → 概要・要点 → 依頼 → 薬剤 → 臨床所見（バイタル・検査・症状） → 予定 → 区分
_SUMMARY_ORDER = (
    (0, ("🚨", "緊急度", "急ぎの確認依頼")),
    (2, ("依頼",)),
    (3, ("薬剤", "服薬期間")),
    (4, ("バイタル", "閾値超過", "検査", "症状")),
    (5, ("次回予定",)),
    (7, ("区分",)),
)


def _summary_rank(line: str) -> int:
    for rank, prefixes in _SUMMARY_ORDER:
        if line.startswith(prefixes):
            return rank
    # canonical findings read 「ラベル｜…」 next to the other findings
    return 4 if "｜" in line.split(" ", 1)[0] else 1
