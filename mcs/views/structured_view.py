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
                         med_is_patient_current, item_unverified)
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


def _canonical_finding_lines(llm: dict) -> list[str]:
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
        attrs = [f"{name}:{f[key]}" for key, name in
                 (("subject", "対象"), *_LINE_ATTRS, ("event_time", "時点"))
                 if isinstance(f.get(key), str) and f[key].strip()]
        qualifier = f"（{'、'.join(attrs)}）" if attrs else ""
        line = f"{label}｜{qualifier}{statement.strip()}"
        quote = f.get("evidence_quote")
        if isinstance(quote, str) and quote.strip():
            line += f"（根拠:{quote.strip()}）"
        out.append(line)
    return out


# Where high urgency came from: explicit exclusions still leave a lexical
# rule match, not a clinical assessment, so the source is always shown.
URGENCY_LABEL = {"llm": "緊急度: 高（AI抽出）",
                 "rule": "緊急語を含む（機械照合）"}


def message_urgency(db, mid: int) -> str | None:
    """'llm' when the message's current fact artifact (the same
    v4 > canonical > extract_llm pick the 📋 body reads —
    latest_fact_artifact) says urgency high, 'rule' when only the rule
    extractor (extract_v1) flags it, else None — the one urgency reading
    for cards, text notices and signal escalation."""
    if (latest_fact_artifact(db, mid) or {}).get("urgency") == "high":
        return "llm"
    if (latest_artifact(db, "extract_v1", mid) or {}).get("urgency") \
            == "high":
        return "rule"
    return None


def _head_lines(llm: dict | None, v1: dict, urgency: str | None = None) -> list[str]:
    selected = llm is not None
    llm = llm or {}
    lines: list[str] = []
    if urgency in URGENCY_LABEL:
        lines.append(URGENCY_LABEL[urgency])
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


def _vital_line(llm: dict | None, v1: dict):
    selected = llm is not None
    llm = llm or {}
    lv = llm.get("vitals") if isinstance(llm.get("vitals"), dict) else {}
    vv = v1.get("vitals") if isinstance(v1.get("vitals"), dict) else {}
    # A missing LLM key may be an intentional subject/time exclusion.
    # Keep a reading intact; never construct a BP pair across sources.
    vit = lv if selected else vv
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


def _lab_lines(llm: dict, body: str | None = None) -> list[str]:
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
        normalized = lab_candidate(
            lb["name"], lb["value"],
            lb.get("unit") if isinstance(lb.get("unit"), str) else None,
            evidence if located else None,
            unverified=item_unverified(lb),
            flag=lb.get("flag") if lb.get("flag") in ("high", "low") else None)
        d = f"{lb['name']} {lb.get('value')}"
        if isinstance(lb.get("unit"), str) and lb["unit"]:
            d += lb["unit"]
        if (flag := _label(_LAB_FLAG_JP, lb.get("flag"))):
            d += f"({flag})"
        if normalized["measured_on"]:
            d += f"(測定日:{normalized['measured_on']})"
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


def _symptom_line(llm: dict, v1: dict):
    syms, neg, seen, neg_seen = [], [], set(), set()
    llm_symptoms = [s for s in _items(llm, "symptoms")
                    if isinstance(s, dict) and isinstance(s.get("text"), str)
                    and s["text"]]
    for s in llm_symptoms:
        if s.get("subject") in ("family", "other") \
                or item_unverified(s):
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


def _med_entries(llm: dict, v1: dict, refs=()) -> tuple[list, list]:
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
            if isinstance(m, dict) and m.get("name"))
        unverified.extend(
            (f"{RX_LABEL[a['action']]}:{a['ctx']}", None)
            for a in _items(v1, "rx_actions")
            if isinstance(a, dict) and _label(RX_LABEL, a.get("action"))
            and isinstance(a.get("ctx"), str) and a["ctx"])
    return meds, unverified


def _med_lines(llm: dict, v1: dict, refs=()) -> list[str]:
    meds, unverified = _med_entries(llm, v1, refs)

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
    return _med_entries(latest_fact_artifact(db, mid) or {}, v1, _drug_refs(db, mid))


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
        reqs.extend(f"{_label(REQ_LABEL, r.get('kind')) or '依頼'}:"
                    f"{r['ctx']}"
                    for r in _items(v1, "requests")
                    if isinstance(r, dict) and isinstance(r.get("ctx"), str)
                    and r["ctx"])
    lines = ["依頼: " + " / ".join(reqs)] if reqs else []
    if cands:
        lines.append("依頼候補（未確認）: " + " / ".join(cands))
    return lines


def structured_lines(db, mid: int) -> list[str]:
    """Compact structured summary from extract_v1 + the current fact
    artifact (canonical_projection shadows extract_llm).  Returns []
    when nothing usable exists (caller falls back to raw only)."""
    v1 = latest_artifact(db, "extract_v1", mid) or {}
    selected = latest_fact_artifact(db, mid)
    llm = selected or {}
    if not v1 and not llm:
        return []
    lines: list[str] = _head_lines(selected, v1, message_urgency(db, mid))
    if (line := _vital_line(selected, v1)) is not None:
        lines.append(line)
    if _items(llm, "labs"):
        lab_source = db.execute(
            "SELECT body_text FROM messages WHERE message_id=?", (mid,)).fetchone()
        lines.extend(_lab_lines(llm, lab_source["body_text"] if lab_source else None))
    if (line := _symptom_line(llm, v1)) is not None:
        lines.append(line)
    lines.extend(_med_lines(llm, v1, _drug_refs(db, mid)))
    lines.extend(_request_lines(llm, v1))
    periods = _items(v1, "med_periods")
    if periods:
        mp = periods[0]
        if isinstance(mp, dict) and mp.get("start"):
            lines.append(f"服薬期間: {mp['start']}〜{mp.get('end') or '?'}")
    if v1.get("next_planned"):
        lines.append(f"次回予定: {v1['next_planned']}")
    lines.extend(_canonical_finding_lines(llm))
    return lines
