#!/usr/bin/env python3
"""Phase J notification side — audited-summary render (§20.1),
degraded minimal notice (§19.3), and the outbox dedup/eligibility
helpers the drain commit relies on. Everything here produces or gates
notification text; nothing here decides what is clinically true.
"""
import json
import time

from mcs_requests import payload_hash
from semantic_model import (KIND_AUDIT, KIND_PLAN, POLICY_VERSION,
                            thread_bundle)

_SECTION_LABEL = {"medication": "薬剤・処方に関する情報",
                  "status": "現在の状況（対象投稿時点）",
                  "pharmacy": "薬局への影響", "followup": "未決事項／フォローアップ",
                  "progress": "前回からの進展", "flow": "投稿の流れ",
                  "other": "その他"}


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
    pat = ledger.db.execute(
        "SELECT patient_name,url FROM patients WHERE project_id=?",
        (project_id,)).fetchone()
    name = (pat["patient_name"] if pat else None) or str(project_id)
    url = (pat["url"] if pat else None) or ""
    ids = [t for t in (targets or [root_id]) if type(t) is int] \
        or [root_id]
    marks = ",".join("?" * len(ids))
    members = ledger.db.execute(
        f"SELECT COUNT(*) c, MAX(posted_at) latest FROM messages "
        f"WHERE project_id=? AND message_id IN ({marks})",
        (project_id, *ids)).fetchone()
    latest = (members["latest"] or "")[:16].replace("T", " ") \
        .replace("-", "/")
    lines = [f"【{name}】",
             f"対象新着：{members['c']}投稿"
             f"｜対象投稿の最終時刻：{latest} JST",
             f"取得：{'完全' if quality == 'full' else '一部未取得'}",
             f"要約：{'自動検査完了' if audit_status == 'PASS' else '要確認'}"]
    if focus_mid is not None and len(ids) > 1:
        trow = ledger.db.execute(
            "SELECT posted_at FROM messages WHERE project_id=? "
            "AND message_id=?", (project_id, focus_mid)).fetchone()
        stamp = ((trow["posted_at"] or "")[:16].replace("T", " ")
                 .replace("-", "/")) if trow else ""
        lines.append(f"要約対象：{stamp} の投稿" if stamp
                     else f"要約対象：投稿#{focus_mid}")
    by_sec: dict[str, list] = {}
    for c in summary.get("claims", []):
        by_sec.setdefault(c["section"], []).append(c)
    if by_sec:
        lines.append("\n■ 今回の重要情報")
        for c in by_sec.get("medication", []) + by_sec.get("status", []):
            lines.append(f"・{c['text']}")
    for sec in ("pharmacy", "followup", "progress", "other"):
        if by_sec.get(sec):
            lines.append(f"\n■ {_SECTION_LABEL[sec]}")
            for c in by_sec[sec]:
                prefix = "【提案】" if c["claim_kind"] == "inference" else ""
                lines.append(f"・{prefix}{c['text']}")
    lims = summary.get("limitations") or []
    if lims:
        lines.append("\n■ 原文・制約")
        lines.extend(f"・{x}" for x in lims[:5])
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
    origin — the send-time gate in notifier re-checks the same
    condition in case suppression lands after enqueue. Replay of a
    genuinely notified thread re-derives the same eligibility, which
    is what lets a crash-lost intent heal."""
    if not message_ids:
        return None
    want = set(message_ids)
    for r in ledger.db.execute(
            "SELECT event_id,payload FROM notify_outbox "
            "WHERE kind='new_messages' AND project_id=? "
            "AND state != 'suppressed' "
            "ORDER BY event_id DESC", (project_id,)):
        try:
            ids = json.loads(r["payload"]).get("message_ids") or []
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(ids, list) and want.intersection(ids):
            return r["event_id"]
    return None


def _emit_degraded(ledger, scfg: dict) -> int:
    """Enforce-only fallback: a new-messages intent whose thread still
    lacks a PASS-audited summary past delayed_notice_seconds gets ONE
    code-generated degraded notice through the existing outbox — no
    clinical claims, original-check instruction only (spec §19.3).
    Each (root, fingerprint) pair dedupes via delivery_key."""
    cutoff = time.time() - scfg["delayed_notice_seconds"]
    sent = 0
    # Only events still undelivered qualify — a base notification that
    # already reached Discord (accepted) or was dropped (suppressed)
    # must never get an extra "新着取得" degraded notice (§19.3). A
    # pending/failed event this old means delivery is genuinely stuck,
    # so the minimal code-generated notice covers the silence.
    events = ledger.db.execute(
        "SELECT event_id,project_id,payload FROM notify_outbox "
        "WHERE kind='new_messages' AND state IN ('pending','failed') "
        "AND created_at<?", (cutoff,))
    for ev in events:
        try:
            ids = json.loads(ev["payload"]).get("message_ids") or []
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(ids, list):
            continue
        ids = ids[:500]      # a malformed fat payload must not wedge the scan
        pid = ev["project_id"]
        if scfg["project_ids"] is not None and pid not in \
                scfg["project_ids"]:
            continue
        marks = ",".join("?" * len(ids)) or "NULL"
        roots = {r["r"] for r in ledger.db.execute(
            f"SELECT COALESCE(parent_id,message_id) r FROM messages "
            f"WHERE message_id IN ({marks})", ids)}
        for root in roots:
            bundle = thread_bundle(ledger, pid, root)
            if bundle is None:
                continue
            fp = bundle["source_fingerprint"]
            passed = False
            for a in ledger.db.execute(
                    "SELECT meta FROM artifacts WHERE kind=? AND "
                    "project_id=?", (KIND_AUDIT, pid)):
                try:
                    m = json.loads(a["meta"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    continue
                if m.get("fingerprint") == fp \
                        and m.get("audit_status") == "PASS":
                    passed = True
                    break
            if passed:
                continue
            dkey = payload_hash({"kind": "semantic_notice", "root": root,
                                 "fp": fp, "degraded": 1})
            if _outbox_has_delivery(ledger, dkey):
                continue
            ledger.outbox_add("semantic_notice", pid, {
                "delivery_key": dkey, "root_id": root,
                "degraded": True, "src_event_id": ev["event_id"],
                "text": render_degraded(ledger, pid),
                "policy_version": POLICY_VERSION})
            sent += 1
    return sent


def _plan_exists(ledger, message_id: int, fp: str,
                 status: str) -> bool:
    """A notify_plan for THIS (generation, audit outcome) already
    recorded — replay and crash-retry must not stack duplicate plan
    rows. Keyed on status too: a plan left by a PENDING run does not
    satisfy a later PASS on the same fingerprint."""
    for r in ledger.db.execute(
            "SELECT meta FROM artifacts WHERE kind=? AND message_id=?",
            (KIND_PLAN, message_id)):
        try:
            m = json.loads(r["meta"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if m.get("fingerprint") == fp \
                and m.get("audit_status") == status:
            return True
    return False
