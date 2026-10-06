"""Semantic notice rendering (spec §20, §19.3): the human-readable
summary text, the code-only degraded notice, outbox delivery dedupe,
and origin-event eligibility.

_emit_degraded resolves pipeline names through the semantic module so
facade monkeypatch points (thread_bundle/_current/policy_fingerprint)
keep working."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import time

from mcs_requests import payload_hash
from mcs_util import loads_dict
import semantic_facts as sf

_SECTION_LABEL = {"medication": "薬剤・処方に関する情報",
                  "status": "現在の状況（対象投稿時点）",
                  "pharmacy": "薬局への影響", "followup": "未決事項／フォローアップ",
                  "progress": "前回からの進展", "flow": "投稿の流れ",
                  "other": "その他"}

_CATEGORY_LABEL = {
    "medication": "薬剤", "allergy_intolerance": "アレルギー・不耐",
    "adverse_drug_event": "有害事象", "adherence_administration": "服薬・投与",
    "symptom_state": "症状・状態", "vital_lab": "バイタル・検査",
    "care_event": "診療・ケア", "request_pending": "依頼・未決",
    "preference": "希望", "other_observation": "その他の所見",
}


MANDATORY_PAGE_BUDGET = 1900    # chars/page — the send-path chunk bound

# Stored contract attributes a reader needs to interpret a line (dose,
# negation, certainty, workflow state). Rendered verbatim — codes, not
# paraphrase — and only when stated; "unknown" adds nothing.
_LINE_ATTRS = (("quantity", "量"), ("polarity", "極性"),
               ("epistemic", "確度"), ("workflow_status", "状態"),
               ("action", "行為"), ("valid_time", "有効時点"),
               ("actor", "記録者"))


def fact_binding(fid: str) -> str:
    """Page-line marker binding a fact ID to its evidence."""
    return f"、ID:{fid}、証拠:"


def _attr_desc(fact: dict) -> str:
    return "".join(f"、{label}:{fact[key]}" for key, label in _LINE_ATTRS
                   if isinstance(fact.get(key), str)
                   and fact[key] not in ("", "unknown"))


def _evidence_desc(fact: dict) -> str:
    ids = [e for e in (fact.get("evidence_ids") or []) if isinstance(e, str)]
    if not ids:
        return "なし"
    return ids[0] + (f"他{len(ids) - 1}件" if len(ids) > 1 else "")


def _fact_pages(entries: list, budget: int) -> list:
    """Pack (line, fact_id) pairs into immutable pages within the char
    budget. A line alone over budget still takes its own page — the
    completeness check then reports it oversized, never truncated."""
    pages, cur_lines, cur_ids, used = [], [], [], 0
    for line, fid in entries:
        need = len(line) + (1 if cur_lines else 0)
        if cur_lines and used + need > budget:
            pages.append({"index": len(pages), "fact_ids": cur_ids,
                          "text": "\n".join(cur_lines)})
            cur_lines, cur_ids, used = [], [], 0
            need = len(line)
        cur_lines.append(line)
        cur_ids.append(fid)
        used += need
    if cur_lines:
        pages.append({"index": len(pages), "fact_ids": cur_ids,
                      "text": "\n".join(cur_lines)})
    return pages


def verify_mandatory_pages(rendered: dict,
                           budget: int = MANDATORY_PAGE_BUDGET) -> dict:
    """Completeness oracle for a mandatory_render result — the gate
    between 'published' and 'still missing'. A missing/duplicated/
    unbound fact_id or an oversized page means publication is
    INCOMPLETE, never PASS."""
    fact_ids = list(rendered.get("fact_ids") or [])
    pages = list(rendered.get("pages") or [])
    declared = [fid for p in pages for fid in p.get("fact_ids") or []]
    declared_set = set(declared)
    fact_set = set(fact_ids)
    missing = [f for f in fact_ids if f not in declared_set]
    extra = [f for f in declared if f not in fact_set]
    duplicated = sorted(f for f, count in Counter(declared).items() if count > 1)
    oversized = [p.get("index") for p in pages
                 if len(p.get("text") or "") > budget]
    unbound = [{"page": p.get("index"), "fact_id": fid}
               for p in pages for fid in p.get("fact_ids") or []
               if fact_binding(fid) not in (p.get("text") or "")]
    return {"complete": not (missing or extra or duplicated
                             or oversized or unbound),
            "missing": missing, "extra": extra,
            "duplicated": duplicated, "oversized_pages": oversized,
            "unbound": unbound}


def mandatory_render(doc: dict,
                     page_budget: int = MANDATORY_PAGE_BUDGET) -> dict:
    """Deterministic mandatory layer for a semantic-facts/v2 document
    (T11): every verified fact and every non-terminal obligation must
    be visible in the rendered output even when the model summary
    omits them.  Lines are code-generated from the stored contract —
    never model text invented here. No count cap: each rendered line
    carries subject/time/ID/evidence identity plus every stated
    quantity/polarity/epistemic/workflow/action/valid-time/actor
    attribute, and the full list is
    split into source-bound pages within the platform char budget —
    'N omitted' is publication-incomplete, never a limitation."""
    facts = [f for f in doc.get("facts", [])
             if isinstance(f, dict)
             and f.get("validation_status") == "verified"]
    lines, fact_ids, seen = [], [], set()
    category_counts = Counter()
    entries = []
    for fact in facts:
        text = fact.get("statement")
        fid = fact.get("fact_id")
        if not isinstance(text, str) or not text \
                or not isinstance(fid, str) or not fid or fid in seen:
            continue
        seen.add(fid)
        label = _CATEGORY_LABEL.get(
            sf.KIND_CATEGORY.get(fact.get("kind"), ""), "その他の所見")
        category_counts[label] += 1
        line = (f"{label}｜{text}"
                f"（対象:{fact.get('subject') or '不明'}"
                f"、時点:{fact.get('event_time') or '不明'}"
                f"{_attr_desc(fact)}"
                f"{fact_binding(fid)}{_evidence_desc(fact)}）")
        lines.append(line)
        fact_ids.append(fid)
        entries.append((line, fid))
    lims, lim_seen = [], set()
    for ob in doc.get("obligations", []):
        if not isinstance(ob, dict) \
                or ob.get("status") not in ("open", "ambiguous",
                                            "failed"):
            continue
        label = _CATEGORY_LABEL.get(ob.get("category"), "その他の所見")
        line = f"{label}の判定が未確定のため要約対象外（原文確認）"
        if line not in lim_seen:
            lim_seen.add(line)
            lims.append(line)
    breakdown = "、".join(f"{label}{n}"
                          for label, n in category_counts.items())
    overview = (f"確認済み事実{len(lines)}件（{breakdown}）"
                if lines else "確認済み事実0件")
    src = doc.get("source") or {}
    out = {"facts": lines, "fact_ids": fact_ids, "overview": overview,
           "pages": _fact_pages(entries, page_budget),
           "limitations": lims,
           "source_fingerprint": src.get("source_fingerprint")}
    out["complete"] = verify_mandatory_pages(out, page_budget)["complete"]
    return out


def render_notice(ledger, project_id: int, root_id: int,
                  summary: dict, audit_status: str,
                  targets: list | None = None,
                  quality: str | None = None,
                  focus_mid: int | None = None) -> str:
    """The §20.1 block. Patient label + coverage/audit status lines are
    code-generated; claim text comes from the audited summary. The MCS
    link is the stored patient URL — never a guessed permalink.
    対象新着 counts only THIS generation's target posts — the rest of
    the thread is context, not arrivals. A multi-target generation
    emits one notice per target; focus_mid names WHICH covered post
    this notice's claims belong to."""
    def instant(value):
        try:
            parsed = datetime.fromisoformat(value or "")
            return parsed if parsed.tzinfo is not None else None
        except ValueError:
            return None

    def stamp(value):
        return value.astimezone(timezone(timedelta(hours=9))).strftime(
            "%Y/%m/%d %H:%M JST") if value else "時刻不明"

    pat = ledger.db.execute(
        "SELECT patient_name,url FROM patients WHERE project_id=?",
        (project_id,)).fetchone()
    name = (pat["patient_name"] if pat else None) or str(project_id)
    url = (pat["url"] if pat else None) or ""
    ids = [t for t in (targets or [root_id]) if type(t) is int] \
        or [root_id]
    marks = ",".join("?" * len(ids))
    members = ledger.db.execute(
        f"SELECT message_id,posted_at FROM messages "
        f"WHERE project_id=? AND message_id IN ({marks})",
        (project_id, *ids)).fetchall()
    times = [instant(row["posted_at"]) for row in members]
    latest = stamp(max((value for value in times if value is not None),
                       default=None))
    lines = [f"【{name}】",
             f"対象新着：{len(members)}投稿"
             f"｜対象投稿の最終時刻：{latest}",
             f"取得：{'完全' if quality == 'full' else '一部未取得'}",
             f"要約：{'自動検査完了' if audit_status == 'PASS' else '要確認'}"]
    if focus_mid is not None and len(ids) > 1:
        trow = ledger.db.execute(
            "SELECT posted_at FROM messages WHERE project_id=? "
            "AND message_id=?", (project_id, focus_mid)).fetchone()
        focus_stamp = stamp(instant(trow["posted_at"])) if trow else "時刻不明"
        lines.append(f"要約対象：{focus_stamp} の投稿#{focus_mid}")
    by_sec: dict[str, list] = {}
    for c in summary.get("claims", []):
        by_sec.setdefault(c["section"], []).append(c)
    if by_sec:
        lines.append("\n■ 今回の重要情報")
        for c in by_sec.get("medication", []) + by_sec.get("status", []):
            prefix = "【提案】" if c["claim_kind"] == "inference" else ""
            lines.append(f"・{prefix}{c['text']}")
    for sec in ("pharmacy", "followup", "progress", "flow", "other"):
        if by_sec.get(sec):
            lines.append(f"\n■ {_SECTION_LABEL[sec]}")
            for c in by_sec[sec]:
                prefix = "【提案】" if c["claim_kind"] == "inference" else ""
                lines.append(f"・{prefix}{c['text']}")
    mfacts = summary.get("mandatory_facts") or []
    if mfacts:
        lines.append("\n■ 抽出済み事実（監査済み）")
        m_overview = summary.get("mandatory_overview")
        if m_overview:
            lines.append(f"・{m_overview}")
        lines.extend(f"・{x}" for x in mfacts)
    lims = summary.get("limitations") or []
    if lims:
        lines.append("\n■ 原文・制約")
        lines.extend(f"・{x}" for x in lims)
    if url.startswith("https://"):
        lines.append("\n▶ MCSで確認\n" + url)
    return "\n".join(lines)


def render_degraded(ledger, project_id: int) -> str:
    """Minimal code-generated notice for enforce-mode overruns — carries
    no unverified clinical claims (§19.3)."""
    pat = ledger.db.execute(
        "SELECT patient_name,url FROM patients WHERE project_id=?",
        (project_id,)).fetchone()
    name = (pat["patient_name"] if pat else None) or str(project_id)
    url = (pat["url"] if pat else None) or ""
    text = (f"【{name}】新着を取り込みました\n"
            "取得：保存済み\n"
            "要約：意味検査が完了していないため保留\n"
            "確認方法：MCS原文を確認してください")
    if url.startswith("https://"):
        text += "\n\n▶ MCSで確認\n" + url
    return text


def _outbox_has_delivery(ledger, delivery_key: str) -> bool:
    for r in ledger.db.execute(
            "SELECT payload FROM notify_outbox WHERE kind=? AND state IN "
            "('pending','failed','accepted','suppressed')",
            ("semantic_notice",)):
        payload = loads_dict(r["payload"])
        if payload is not None and payload.get("delivery_key") == delivery_key:
            return True
    return False


def _arrival_ids(raw) -> list[int]:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    ids = payload.get("message_ids")
    if not isinstance(ids, list) or any(type(mid) is not int or mid <= 0 for mid in ids):
        return []
    return ids


def _notify_src_event(ledger, project_id: int,
                      message_ids: list) -> int | None:
    """The newest new_messages outbox intent covering any of these
    message ids — the stored origin event this evaluation descends
    from. Notification eligibility is decided by the recorded origin
    event, never by the semantic pipeline (INV-20, §20.3): a history
    import, archive-suppressed path, replay, or after-the-fact seed
    whose targets were never in a real arrival intent returns None and
    can produce artifacts only — no notice is generated (AT-055).
    A suppressed origin (archived/retracted arrival) counts as no
    origin — the send-time gate in semantic_send_gate re-checks the same
    condition in case suppression lands after enqueue. Replay of a
    genuinely notified thread re-derives the same eligibility, which
    is what lets a crash-lost intent heal."""
    if not message_ids:
        return None
    want = set(message_ids)
    # A delivery attempt consumes its arrival event, not every future arrival
    # for the same message. A correction can have a separately recorded origin.
    attempted = set()
    for row in ledger.db.execute(
            "SELECT payload,state,attempts,progress FROM notify_outbox "
            "WHERE kind='semantic_notice' AND project_id=?", (project_id,)):
        try:
            payload = json.loads(row["payload"])
            progress = json.loads(row["progress"] or "{}")
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict) or not isinstance(progress, dict):
            return None
        if payload.get("degraded"):
            continue
        if (row["state"] in ("accepted", "in_flight")
                or row["attempts"] > 0 or progress.get("sent")):
            identity = (payload.get("src_event_id"), payload.get("target_message_id"))
            if any(type(value) is not int or value <= 0 for value in identity):
                return None
            attempted.add(identity)
    for r in ledger.db.execute(
            "SELECT event_id,payload FROM notify_outbox "
            "WHERE kind='new_messages' AND project_id=? "
            "AND state != 'suppressed' "
            "ORDER BY event_id DESC", (project_id,)):
        ids = _arrival_ids(r["payload"])
        if ids:
            covered = want.intersection(ids)
            if any((r["event_id"], mid) not in attempted for mid in covered):
                return r["event_id"]
            want.difference_update(covered)
            if not want:
                return None
    return None


def _emit_degraded(ledger, scfg: dict) -> int:
    """Enforce-only fallback: a new-messages intent whose thread still
    lacks a PASS-audited summary past delayed_notice_seconds gets ONE
    code-generated degraded notice through the existing outbox — no
    clinical claims, original-check instruction only (spec §19.3).
    Each (root, fingerprint) pair dedupes via delivery_key."""
    import semantic
    cutoff = time.time() - scfg["delayed_notice_seconds"]
    sent = 0
    # Only events still undelivered qualify — a base notification that
    # already reached Discord (accepted) or was dropped (suppressed)
    # must never get an extra "新着取得" degraded notice (§19.3). A
    # pending/failed event this old means delivery is genuinely stuck,
    # so the minimal code-generated notice covers the silence.
    events = ledger.db.execute(
        "SELECT event_id,project_id,payload,progress FROM notify_outbox "
        "WHERE kind='new_messages' AND state IN ('pending','failed') AND attempts=0 "
        "AND created_at<?", (cutoff,))
    for ev in events:
        try:
            progress = json.loads(ev["progress"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(progress, dict) or progress.get("sent"):
            continue
        ids = _arrival_ids(ev["payload"])
        if not ids:
            continue
        ids = ids[:500]      # a malformed fat payload must not wedge the scan
        pid = ev["project_id"]
        from mcs_operations import paused
        if paused(ledger.db, pid):
            continue
        if scfg["project_ids"] is not None and pid not in \
                scfg["project_ids"]:
            continue
        marks = ",".join("?" * len(ids)) or "NULL"
        roots = {r["r"] for r in ledger.db.execute(
            f"SELECT COALESCE(parent_id,message_id) r FROM messages "
            f"WHERE project_id=? AND message_id IN ({marks})", (pid, *ids))}
        for root in roots:
            bundle = semantic.thread_bundle(ledger, pid, root)
            if bundle is None:
                continue
            fp = bundle["source_fingerprint"]
            arrivals = [m["message_id"] for m in bundle["members"]
                        if m["message_id"] in ids]
            audits = [semantic._current(ledger, semantic.KIND_AUDIT, mid,
                                        fp, semantic.policy_fingerprint(scfg))
                      for mid in arrivals]
            if arrivals and all(a and a["meta"].get("audit_status") == "PASS"
                                for a in audits):
                continue
            dkey = payload_hash({"kind": "semantic_notice", "root": root,
                                 "fp": fp, "degraded": 1})
            if _outbox_has_delivery(ledger, dkey):
                continue
            ledger.outbox_add("semantic_notice", pid, {
                "delivery_key": dkey, "root_id": root,
                "degraded": True, "src_event_id": ev["event_id"],
                "fingerprint": fp,
                "policy_fingerprint": semantic.policy_fingerprint(scfg),
                "target_message_ids": arrivals,
                "text": render_degraded(ledger, pid),
                "policy_version": semantic.POLICY_VERSION})
            sent += 1
    return sent
