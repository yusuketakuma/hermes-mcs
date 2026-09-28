"""Structured display view over extraction artifacts.

Shared formatter for the extract_v1/extract_llm artifacts — used by the
text notify_flush (_fmt's 📋 構造化 section) and the interactive card
renderer (notify_render._structured_block). Keeping one implementation
means the suppression rules (negated/resolved symptoms, other-person
meds, unverified-medication labelling) and the freshness gate can never
diverge between surfaces.

Freshness/safety gate lives in latest_artifact's SQL: the artifact must
be bound to the message's CURRENT content_hash and the message must not
be deleted — a tombstoned message never contributes extracted data.

Merge contract (v1 rules ∪ llm ∪ v4 canonical facts), per field:
  events   — UNION of llm+v1; llm's enum lacks v1-only kinds
             (medication/adherence/media_ref) and a partial llm list
             must not shadow v1 detections.
  vitals   — per-key merge: llm wins keys it emitted (already
             guard-verified against body numerals); v1 regex fills keys
             llm omitted.
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

import json

from mcs_queries import (current_extract_pred, current_fact_pred,
                         med_is_patient_current)


def _content_dict(r) -> dict | None:
    """json.loads an artifact content blob — non-object or corrupt JSON
    is simply absent, never a view-killing error."""
    if not r:
        return None
    try:
        d = json.loads(r["content"])
    except (json.JSONDecodeError, TypeError):
        return None
    return d if isinstance(d, dict) else None


def latest_artifact(db, kind: str, mid: int) -> dict | None:
    r = db.execute(
        "SELECT a.content FROM artifacts a JOIN messages m "
        "ON m.message_id=a.message_id WHERE a.kind=? AND a.message_id=? "
        f"{current_extract_pred()} "
        "ORDER BY a.artifact_id DESC LIMIT 1", (kind, mid)).fetchone()
    return _content_dict(r)


def fact_ready_ids(db, mids: list) -> set:
    """mids that carry a CURRENT fact artifact (extract_llm /
    canonical_projection / semantic_facts_v4) under the same
    freshness/shadow predicate latest_fact_artifact applies — batched
    so callers can fingerprint "structured block exists" without
    one query per message."""
    ids = [int(m) for m in mids if isinstance(m, int) or
           (isinstance(m, str) and m.isdigit())]
    if not ids:
        return set()
    marks = ",".join("?" * len(ids))
    return {r["message_id"] for r in db.execute(
        "SELECT DISTINCT a.message_id FROM artifacts a "
        "JOIN messages m ON m.message_id=a.message_id "
        f"WHERE a.message_id IN ({marks}) "
        "AND a.kind IN ('extract_llm','canonical_projection',"
        "'semantic_facts_v4') "
        "AND m.body_state IS NOT 'deleted' "
        "AND CASE WHEN json_valid(a.content) THEN "
        "json_type(a.content)='object' ELSE 0 END "
        f"{current_fact_pred('a', 'm')}",
        tuple(ids)).fetchall()}


def latest_fact_artifact(db, mid: int) -> dict | None:
    """Newest usable fact artifact for a message — a hash-current
    ``canonical_projection`` shadows ``extract_llm`` (the same
    ``current_fact_pred`` shadow rule every consumer shares); when the
    projection is stale, invalidated, or absent the legacy extraction
    is read instead — never the other way around."""
    r = db.execute(
        "SELECT a.content FROM artifacts a JOIN messages m "
        "ON m.message_id=a.message_id WHERE a.message_id=? "
        "AND a.kind IN ('extract_llm','canonical_projection','semantic_facts_v4') "
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


def _canonical_finding_lines(llm: dict) -> list[str]:
    """Structured lines for verified facts that no legacy slot can
    carry. Kind is labelled so a vital never reads as a symptom."""
    out = []
    facts = llm.get("canonical_facts")
    if not isinstance(facts, list):
        return out
    seen = set()
    for f in facts:
        if not isinstance(f, dict):
            continue
        label = _FINDING_LABEL.get(f.get("kind"))
        statement = f.get("statement")
        fid = f.get("fact_id")
        if label is None or not isinstance(statement, str) \
                or not statement.strip() or not isinstance(fid, str) \
                or not fid.strip() or fid in seen:
            continue
        seen.add(fid)
        line = f"{label}｜{statement.strip()[:60]}"
        quote = f.get("evidence_quote")
        if isinstance(quote, str) and quote.strip():
            line += f"（根拠:{quote.strip()[:40]}）"
        out.append(line)
    return out


def _head_lines(llm: dict, v1: dict) -> list[str]:
    lines: list[str] = []
    if (llm.get("summary") or "").strip():
        lines.append(f"要約: {llm['summary'].strip()[:80]}")
    pts = [str(p).strip() for p in (llm.get("points") or [])
           if isinstance(p, str) and p.strip()]
    if pts:
        lines.append("要点: " + " / ".join(p[:40] for p in pts[:3]))
    # events merge as a UNION — llm's enum excludes v1-only kinds
    # (medication/adherence/media_ref) and a partial llm list must not
    # shadow v1 detections (production: 区分 lost eol on ~400 posts).
    evs = [e for e in dict.fromkeys(
        list(llm.get("events") or []) + list(v1.get("events") or []))
        if e in EVT_LABEL]
    if evs:
        lines.append("区分: " + "・".join(EVT_LABEL[e] for e in evs[:5]))
    return lines


def _vital_line(llm: dict, v1: dict):
    # Per-key merge: llm wins on keys it extracted (vitals_guard has
    # already relabelled/dropped mislabelled values), v1's regex
    # catches keys llm omitted entirely (production: ~520 posts lost
    # v1-only hr/sbp/dbp readings under a partial llm vitals dict).
    lv = llm.get("vitals") if isinstance(llm.get("vitals"), dict) else {}
    vv = v1.get("vitals") if isinstance(v1.get("vitals"), dict) else {}
    vit = {k: v for k in ("sbp", "dbp", "bt", "hr", "rr", "spo2", "bs")
           if (v := lv.get(k) if lv.get(k) is not None
               else vv.get(k)) is not None}
    if not vit:
        return None
    parts = []
    if vit.get("sbp") is not None:
        parts.append(f"BP {vit['sbp']}/{vit.get('dbp')}")
    for k, lab in (("bt", "BT"), ("hr", "HR"), ("rr", "RR"),
                   ("spo2", "SpO2"), ("bs", "BS")):
        if vit.get(k) is not None:
            parts.append(f"{lab} {vit[k]}")
    return "バイタル: " + "  ".join(parts) if parts else None


def _lab_line(llm: dict):
    """Reported lab values (v4) — name+value+unit plus the body's own
    out-of-range marker; the view never invents reference ranges."""
    out = []
    for lb in llm.get("labs") or []:
        if not isinstance(lb, dict) or not lb.get("name"):
            continue
        d = f"{lb['name']} {lb.get('value')}"
        if isinstance(lb.get("unit"), str) and lb["unit"]:
            d += lb["unit"]
        if lb.get("flag") in _LAB_FLAG_JP:
            d += f"({_LAB_FLAG_JP[lb['flag']]})"
        out.append(d)
    return "検査: " + "・".join(out[:6]) if out else None


def _symptom_line(llm: dict, v1: dict):
    syms, neg, seen, neg_seen = [], [], set(), set()
    llm_symptoms = [s for s in llm.get("symptoms") or []
                    if isinstance(s, dict) and isinstance(s.get("text"), str)
                    and s["text"]]
    for s in llm_symptoms:
        if s.get("subject") in ("family", "other") or s.get("unverified"):
            continue
        if s.get("negated") or s.get("status") in ("resolved", "past"):
            if s["text"] not in neg_seen:
                neg_seen.add(s["text"])
                if s.get("negated"):
                    neg.append(s["text"])
        elif s["text"] not in seen:
            seen.add(s["text"])
            parts = []
            sev = _SEVERITY_JP.get(s.get("severity"))
            if sev:
                parts.append(sev)
            parts += [x.strip() for x in (s.get("onset"), s.get("duration"))
                      if isinstance(x, str) and x.strip()]
            syms.append(s["text"]
                        + (f"({'・'.join(parts)})" if parts else ""))
    for s in v1.get("symptoms") or []:
        if not isinstance(s, str):
            continue
        if any(x["text"] in s or s in x["text"] for x in llm_symptoms):
            continue  # Typed polarity/subject must not reappear through rules.
        if s and s not in seen:
            syms.append(s)
    if not syms and not neg:
        return None
    line = "症状: " + "、".join(syms[:6])
    if neg:
        line += ("　" if syms else "") + "、".join(
            f"{n}なし" for n in neg[:4])
    return line


def _med_lines(llm: dict, v1: dict) -> list[str]:
    meds = []
    for m in llm.get("meds") or []:
        if not isinstance(m, dict) or not m.get("name"):
            continue
        # negated / other-person / historical meds must not read as the
        # patient's own medication (planned survives — shown as [予定])
        if not med_is_patient_current(m):
            continue
        d = str(m["name"]) + (f" {m['dose']}" if m.get("dose") else "")
        if m.get("action") in RX_LABEL:
            d += f"[{RX_LABEL[m['action']]}]"
        if m.get("status") == "planned":
            d += "[予定]"
        tail = []
        if m.get("route") in _ROUTE_JP:
            tail.append(_ROUTE_JP[m["route"]])
        if isinstance(m.get("freq"), str) and m["freq"]:
            tail.append(m["freq"])
        if m.get("prn") is True:
            tail.append("頓服")
        if tail:
            d += f"({'・'.join(tail)})"
        meds.append(d)
    unverified_meds = []
    if not llm.get("meds"):
        # v1 fallback only when the LLM saw NO meds — if it saw meds
        # but all were filtered (negated/family/past), falling back to
        # v1 would re-display the very mentions that were filtered out
        unverified_meds.extend(
            str(m["name"]) + (f" {m['dose']}" if m.get("dose") else "")
            for m in v1.get("medications") or []
            if isinstance(m, dict) and m.get("name"))
        unverified_meds.extend(
            f"{RX_LABEL[a['action']]}:{a['ctx'][:18]}"
            for a in v1.get("rx_actions") or []
            if isinstance(a, dict) and a.get("action") in RX_LABEL
            and a.get("ctx"))
    lines = []
    if meds:
        lines.append("薬剤: " + "、".join(meds[:6]))
    if unverified_meds:
        lines.append("薬剤候補（未確認）: " + "、".join(unverified_meds[:6]))
    return lines


def _request_lines(llm: dict, v1: dict) -> list[str]:
    reqs = []
    for r in llm.get("requests") or []:
        if isinstance(r, dict) and (r.get("to") or r.get("action")):
            to = str(r.get("to") or "")
            to = "" if to in ("", "不明", "unknown", "-") else f"{to}へ"
            frm = str(r.get("from") or "")
            prefix = "" if frm in ("", "不明", "unknown", "-") \
                else f"{frm}→"
            due = r.get("due")
            suffix = f"(期限:{due})" if isinstance(due, str) and due \
                else ""
            reqs.append(prefix + to + str(r.get("action") or "")[:30]
                        + suffix)
    if not reqs:
        reqs.extend(f"{REQ_LABEL.get(r.get('kind'), '依頼')}:"
                    f"{r['ctx'][:24]}"
                    for r in v1.get("requests") or []
                    if isinstance(r, dict) and r.get("ctx"))
    return ["依頼: " + " / ".join(reqs[:3])] if reqs else []


def structured_lines(db, mid: int) -> list[str]:
    """Compact structured summary from extract_v1 + the current fact
    artifact (canonical_projection shadows extract_llm).  Returns []
    when nothing usable exists (caller falls back to raw only)."""
    v1 = latest_artifact(db, "extract_v1", mid) or {}
    llm = latest_fact_artifact(db, mid) or {}
    if not v1 and not llm:
        return []
    lines: list[str] = _head_lines(llm, v1)
    if (line := _vital_line(llm, v1)) is not None:
        lines.append(line)
    if (line := _lab_line(llm)) is not None:
        lines.append(line)
    if (line := _symptom_line(llm, v1)) is not None:
        lines.append(line)
    lines.extend(_med_lines(llm, v1))
    lines.extend(_request_lines(llm, v1))
    if v1.get("med_periods"):
        mp = v1["med_periods"][0]
        if isinstance(mp, dict) and mp.get("start"):
            lines.append(f"服薬期間: {mp['start']}〜{mp.get('end') or '?'}")
    if v1.get("next_planned"):
        lines.append(f"次回予定: {v1['next_planned']}")
    lines.extend(_canonical_finding_lines(llm))
    return lines
