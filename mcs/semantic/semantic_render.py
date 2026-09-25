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


def mandatory_render(doc: dict, max_facts: int = 40) -> dict:
    """Deterministic mandatory layer for a semantic-facts/v2 document
    (T11): every verified fact and every non-terminal obligation must
    be visible in the rendered output even when the model summary
    omits them.  Lines are code-generated from the stored contract —
    never model text invented here."""
    facts = [f for f in doc.get("facts", [])
             if isinstance(f, dict)
             and f.get("validation_status") == "verified"]
    statement_counts = Counter(
        fact.get("statement") for fact in
        {fact["fact_id"]: fact for fact in facts}.values())
    lines, seen = [], set()
    omitted = 0
    for fact in facts:
        text = fact.get("statement")
        if not isinstance(text, str) or not text or fact["fact_id"] in seen:
            continue
        seen.add(fact["fact_id"])
        # beyond the cap a verified fact must not vanish silently —
        # count it and disclose the omission as a limitation (FIX-SR1)
        if len(lines) >= max_facts:
            omitted += 1
            continue
        label = _CATEGORY_LABEL.get(
            sf.KIND_CATEGORY.get(fact.get("kind"), ""), "その他の所見")
        if statement_counts[text] > 1:
            text += (f"（対象: {fact['subject']}、時点: {fact['event_time']}、"
                     f"ID: {fact['fact_id']}）")
        lines.append(f"{label}｜{text}")
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
    if omitted:
        lims.append(f"確認済み事実{omitted}件は表示上限のため省略"
                    "（台帳を参照）")
    return {"facts": lines, "limitations": lims}


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
        try:
            if json.loads(r["payload"]).get("delivery_key") \
                    == delivery_key:
                return True
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
    return False


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
            attempted.add((payload.get("src_event_id"), payload.get("target_message_id")))
    for r in ledger.db.execute(
            "SELECT event_id,payload FROM notify_outbox "
            "WHERE kind='new_messages' AND project_id=? "
            "AND state != 'suppressed' "
            "ORDER BY event_id DESC", (project_id,)):
        try:
            ids = json.loads(r["payload"]).get("message_ids") or []
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(ids, list):
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
        try:
            ids = json.loads(ev["payload"]).get("message_ids") or []
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(ids, list):
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
