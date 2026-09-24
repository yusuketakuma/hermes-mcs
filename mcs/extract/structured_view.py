"""Structured display view over extraction artifacts.

Shared formatter for the extract_v1/extract_llm artifacts — used by the
text notifier (_fmt's 📋 構造化 section) and the interactive card
renderer (notify_render._structured_block). Keeping one implementation
means the suppression rules (negated/resolved symptoms, other-person
meds, unverified-medication labelling) and the freshness gate can never
diverge between surfaces.

Freshness/safety gate lives in latest_artifact's SQL: the artifact must
be bound to the message's CURRENT content_hash and the message must not
be deleted — a tombstoned message never contributes extracted data.
"""
from __future__ import annotations

import json

from mcs_queries import med_is_patient_current


def latest_artifact(db, kind: str, mid: int) -> dict | None:
    r = db.execute(
        "SELECT a.content FROM artifacts a JOIN messages m "
        "ON m.message_id=a.message_id WHERE a.kind=? AND a.message_id=? "
        "AND m.body_state IS NOT 'deleted' "
        "AND CASE WHEN json_valid(a.meta) THEN "
        "json_extract(a.meta,'$.error') IS NOT 1 AND "
        "json_extract(a.meta,'$.hash')=m.content_hash ELSE 0 END "
        "AND CASE WHEN json_valid(a.content) THEN "
        "json_type(a.content)='object' ELSE 0 END "
        "ORDER BY a.artifact_id DESC LIMIT 1", (kind, mid)).fetchone()
    if not r:
        return None
    try:
        d = json.loads(r["content"])
    except (json.JSONDecodeError, TypeError):
        return None
    return d if isinstance(d, dict) else None


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


def structured_lines(db, mid: int) -> list[str]:
    """Compact structured summary from extract_v1/extract_llm artifacts.
    Returns [] when nothing usable exists (caller falls back to raw only)."""
    v1 = latest_artifact(db, "extract_v1", mid) or {}
    llm = latest_artifact(db, "extract_llm", mid) or {}
    if not v1 and not llm:
        return []
    lines: list[str] = []
    if (llm.get("summary") or "").strip():
        lines.append(f"要約: {llm['summary'].strip()[:80]}")
    pts = [str(p).strip() for p in (llm.get("points") or [])
           if isinstance(p, str) and p.strip()]
    if pts:
        lines.append("要点: " + " / ".join(p[:40] for p in pts[:3]))
    evs = [e for e in (llm.get("events") or v1.get("events") or [])
           if e in EVT_LABEL]
    if evs:
        lines.append("区分: " + "・".join(EVT_LABEL[e] for e in evs[:5]))
    vit = llm.get("vitals") if isinstance(llm.get("vitals"), dict) else None
    if not vit and isinstance(v1.get("vitals"), dict):
        vit = v1["vitals"]
    if vit:
        parts = []
        if vit.get("sbp") is not None:
            parts.append(f"BP {vit['sbp']}/{vit.get('dbp')}")
        for k, lab in (("bt", "BT"), ("hr", "HR"), ("rr", "RR"),
                       ("spo2", "SpO2"), ("bs", "BS")):
            if vit.get(k) is not None:
                parts.append(f"{lab} {vit[k]}")
        if parts:
            lines.append("バイタル: " + "  ".join(parts))
    syms, neg, seen, neg_seen = [], [], set(), set()
    llm_symptoms = [s for s in llm.get("symptoms") or []
                    if isinstance(s, dict) and isinstance(s.get("text"), str)
                    and s["text"]]
    for s in llm_symptoms:
        if isinstance(s, dict) and s.get("text"):
            if s.get("subject") in ("family", "other") or s.get("unverified"):
                continue
            # resolved/past/negated all cancel an earlier positive —
            # they enter neg_seen so a v1 positive below is contradicted
            # (same resolver semantics as rollup.py)
            if s.get("negated") or s.get("status") in ("resolved", "past"):
                if s["text"] not in neg_seen:
                    neg_seen.add(s["text"])
                    if s.get("negated"):
                        neg.append(s["text"])
            elif s["text"] not in seen:
                seen.add(s["text"])
                syms.append(s["text"])
    for s in v1.get("symptoms") or []:
        if not isinstance(s, str):
            continue
        if any(x["text"] in s or s in x["text"] for x in llm_symptoms):
            continue  # Typed polarity/subject must not reappear through rules.
        contradicted = any(n in s or s in n for n in neg_seen)
        if s and s not in seen and not contradicted:
            syms.append(s)
    if syms or neg:
        line = "症状: " + "、".join(syms[:6])
        if neg:
            line += ("　" if syms else "") + "、".join(
                f"{n}なし" for n in neg[:4])
        lines.append(line)
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
        meds.append(d)
    unverified_meds = []
    if not llm.get("meds"):
        # v1 fallback only when the LLM saw NO meds — if it saw meds
        # but all were filtered (negated/family/past), falling back to
        # v1 would re-display the very mentions that were filtered out
        for m in v1.get("medications") or []:
            if isinstance(m, dict) and m.get("name"):
                unverified_meds.append(str(m["name"]) +
                                       (f" {m['dose']}" if m.get("dose") else ""))
        unverified_meds.extend(
            f"{RX_LABEL[a['action']]}:{a['ctx'][:18]}"
            for a in v1.get("rx_actions") or []
            if isinstance(a, dict) and a.get("action") in RX_LABEL
            and a.get("ctx"))
    if meds:
        lines.append("薬剤: " + "、".join(meds[:6]))
    if unverified_meds:
        lines.append("薬剤候補（未確認）: " + "、".join(unverified_meds[:6]))
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
        for r in v1.get("requests") or []:
            if isinstance(r, dict) and r.get("ctx"):
                reqs.append(f"{REQ_LABEL.get(r.get('kind'), '依頼')}:"
                            f"{r['ctx'][:24]}")
    if reqs:
        lines.append("依頼: " + " / ".join(reqs[:3]))
    if v1.get("med_periods"):
        mp = v1["med_periods"][0]
        if isinstance(mp, dict) and mp.get("start"):
            lines.append(f"服薬期間: {mp['start']}〜{mp.get('end') or '?'}")
    if v1.get("next_planned"):
        lines.append(f"次回予定: {v1['next_planned']}")
    return lines
