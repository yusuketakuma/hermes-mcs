#!/usr/bin/env python3
"""MCS patient rollup — one consolidated artifact per patient.

Aggregates extract_v1 + extract_llm artifacts + raw message metadata into
kind='patient_rollup' (one current row per patient — old rollups are
replaced, not stacked):

  last_activity, msg counts, latest vitals (with date), current med period,
  meds seen recently, recent symptoms, open requests (unanswered-looking),
  next planned visit, top senders, possibly_deleted message ids.

Deterministic — no LLM needed for the aggregation itself. A 'summary' line
is taken from the newest extract_llm artifact when present.

Usage: python3 rollup.py [--project <id>] [--all]
Called by run_check for patients that received new messages this tick.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ledger import Ledger
from mcs_util import acquire_run_lock

HOME = os.path.expanduser("~/.mcs")
DB = os.path.join(HOME, "data", "ledger.db")
KIND = "patient_rollup"
STALE_DAYS = 21          # message unseen this long while siblings refresh


def _dicts(value) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] \
        if isinstance(value, list) else []


def build_rollup(ledger, project_id: int) -> dict:
    db = ledger.db
    msgs = db.execute("""
      SELECT message_id, posted_at, posted_at_ts, sender_name, sender_type,
             parent_id, updated_seen, body_state
      FROM messages WHERE project_id=? ORDER BY posted_at_ts DESC
    """, (project_id,)).fetchall()
    out: dict = {"project_id": project_id, "generated_at": time.time(),
                 "msg_count": len(msgs),
                 "reply_count": sum(1 for m in msgs if m["parent_id"])}
    if not msgs:
        return out
    newest = msgs[0]
    out["last_activity"] = newest["posted_at"]
    out["last_activity_ts"] = newest["posted_at_ts"]

    arts = {}
    for a in db.execute("""
      SELECT a.message_id, a.kind, a.content FROM artifacts a
      JOIN messages m ON m.message_id=a.message_id
      WHERE m.project_id=? AND a.kind IN ('extract_v1','extract_llm')
        AND CASE WHEN json_valid(a.meta) THEN
          json_extract(a.meta,'$.error') IS NOT 1
          AND json_extract(a.meta,'$.hash')=m.content_hash
        ELSE 0 END
        AND CASE WHEN json_valid(a.content)
                 THEN json_type(a.content)='object' ELSE 0 END
      ORDER BY a.artifact_id
    """, (project_id,)):
        arts.setdefault(a["message_id"], {})[a["kind"]] = a["content"]

    latest_vitals = None
    med_period = None
    meds = {}
    sym_pos = {}   # term -> latest positive ts (msgs iterated newest-first)
    sym_neg = {}   # term -> latest negated ts
    requests = []
    next_planned = None
    senders = {}
    summary = None
    for m in msgs:
        ts = m["posted_at_ts"] or 0
        senders[m["sender_name"] or "?"] = \
            senders.get(m["sender_name"] or "?", 0) + 1
        blobs = arts.get(m["message_id"], {})
        try:
            v1 = json.loads(blobs["extract_v1"]) \
                if "extract_v1" in blobs else {}
            lm = json.loads(blobs["extract_llm"]) \
                if "extract_llm" in blobs else {}
        except (json.JSONDecodeError, TypeError):
            v1 = lm = {}
        if not isinstance(v1, dict):
            v1 = {}
        if not isinstance(lm, dict):
            lm = {}
        if lm.get("_error"):
            lm = {}
        if summary is None and isinstance(lm.get("summary"), str) \
                and lm["summary"]:
            summary = {"text": lm["summary"], "at": m["posted_at"]}
        if latest_vitals is None:
            vit = lm.get("vitals") or v1.get("vitals")
            if isinstance(vit, dict) and vit:
                latest_vitals = {"at": m["posted_at"], **vit}
        periods = _dicts(v1.get("med_periods"))
        if med_period is None and periods:
            med_period = periods[-1]
        for x in _dicts(lm.get("meds")):
            name = x.get("name")
            if isinstance(name, str) and name not in ("", "処方薬", "薬"):
                meds.setdefault(name, {"dose": x.get("dose"),
                                       "last": m["posted_at"]})
        for x in _dicts(v1.get("medications")):
            if isinstance(x.get("name"), str) and x["name"]:
                meds.setdefault(x["name"], {"dose": x.get("dose"),
                                            "last": m["posted_at"]})
        for s in v1.get("symptoms") \
                if isinstance(v1.get("symptoms"), list) else []:
            if not isinstance(s, str) or not s:
                continue
            sym_pos.setdefault(s, ts)
        for s in _dicts(lm.get("symptoms")):
            t = s.get("text")
            if not isinstance(t, str) or not t:
                continue
            # LLM polarity: a negation newer than a positive mention
            # RESOLVES the symptom — it must cancel v1/rule positives,
            # not just be skipped (Oracle B24)
            if s.get("negated"):
                sym_neg.setdefault(t, ts)
            else:
                sym_pos.setdefault(t, ts)
        for rq in _dicts(v1.get("requests")):
            requests.append({"kind": rq.get("kind"), "ctx": rq.get("ctx"),
                             "at": m["posted_at"], "mid": m["message_id"]})
        for rq in _dicts(lm.get("requests")):
            requests.append({"kind": rq.get("to"), "ctx": rq.get("action"),
                             "at": m["posted_at"], "mid": m["message_id"]})
        if next_planned is None and isinstance(v1.get("next_planned"), str) \
                and v1["next_planned"]:
            next_planned = v1["next_planned"]

    # resolve: a positive symptom stands only if no matching negation is
    # newer (substring match covers 「浮腫」 vs 「浮腫あり」等)
    symptoms = {}
    for t, pts in sym_pos.items():
        overridden = any(nts >= pts and (nt in t or t in nt)
                         for nt, nts in sym_neg.items())
        if not overridden:
            symptoms[t] = msgs_by_ts(msgs, pts)
    if latest_vitals:
        out["latest_vitals"] = latest_vitals
    if med_period:
        out["current_med_period"] = med_period
    if meds:
        out["medications"] = [{"name": k, **v}
                              for k, v in list(meds.items())[:20]]
    if symptoms:
        out["recent_symptoms"] = [{"symptom": k, "last": v}
                                  for k, v in list(symptoms.items())[:20]]
    if requests:
        out["recent_requests"] = requests[:15]
    if next_planned:
        out["next_planned"] = next_planned
    if summary:
        out["summary"] = summary
    out["top_senders"] = sorted(senders.items(), key=lambda x: -x[1])[:8]

    # staleness heuristic: message unseen while siblings refreshed ->
    # possibly deleted/edited-out on the server (archive keeps it anyway)
    cutoff = (newest["updated_seen"] or time.time()) - STALE_DAYS * 86400
    out["possibly_deleted"] = [
        m["message_id"] for m in msgs
        if (m["updated_seen"] or 0) < cutoff][:20]
    return out


def msgs_by_ts(msgs, ts: float) -> str:
    for m in msgs:
        if m["posted_at_ts"] == ts:
            return m["posted_at"]
    return str(ts)


def rebuild(ledger, project_id: int) -> int:
    """Atomic replace — a crash between delete and insert must not leave
    a patient with NO rollup (Oracle B22)."""
    d = build_rollup(ledger, project_id)
    with ledger.db:
        ledger.db.execute(
            "DELETE FROM artifacts WHERE kind=? AND project_id=?",
            (KIND, project_id))
        cur = ledger.db.execute(
            "INSERT INTO artifacts(kind,project_id,message_id,content,"
            "model,meta,created_at) VALUES(?,?,?,?,?,?,?)",
            (KIND, project_id, None,
             json.dumps(d, ensure_ascii=False), "rules-v1",
             json.dumps({"generated_at": d["generated_at"]}), time.time()))
    return cur.lastrowid


def dirty_projects(ledger) -> list:
    """Patients whose rollup is missing or older than its newest source
    (message/artifact). Rolls forward artifact-only changes too — an LLM
    pass landing after the last message must still refresh (Oracle B23)."""
    rows = ledger.db.execute("""
      SELECT p.project_id, r.g AS gen,
        (SELECT MAX(a.created_at) FROM artifacts a
          WHERE a.project_id=p.project_id
            AND a.kind IN ('extract_v1','extract_llm')) AS art_ts,
        (SELECT MAX(m.updated_seen) FROM messages m
          WHERE m.project_id=p.project_id) AS msg_ts
      FROM patients p
        LEFT JOIN (SELECT project_id,
                   MAX(CASE WHEN json_valid(meta)
                            THEN json_extract(meta,'$.generated_at') END) g
                 FROM artifacts WHERE kind=?
                 GROUP BY project_id) r ON r.project_id=p.project_id
      WHERE EXISTS (SELECT 1 FROM messages m3
                    WHERE m3.project_id=p.project_id)
    """, (KIND,)).fetchall()
    return [x["project_id"] for x in rows
            if x["gen"] is None
            or (x["art_ts"] or 0) > x["gen"]
            or (x["msg_ts"] or 0) > x["gen"]]


def rebuild_many(ledger, project_ids, dirty_only: bool = False) -> int:
    if dirty_only:
        dirty = set(dirty_projects(ledger))
        project_ids = [p for p in project_ids if p in dirty]
    n = 0
    for pid in project_ids:
        rebuild(ledger, pid)
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", type=int, action="append", default=[])
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    # same single-writer lock as the scheduled tick
    lock_fd = acquire_run_lock()
    if lock_fd is None:
        print(json.dumps({"ok": False, "error": "lock_held"}))
        return 3
    try:
        l = Ledger(DB)
    except Exception:
        os.close(lock_fd)
        raise
    ids = args.project or [r["project_id"] for r in
                           l.db.execute("SELECT project_id FROM patients")]
    n = rebuild_many(l, ids)
    print(json.dumps({"rollups": n}, ensure_ascii=False))
    l.close()
    os.close(lock_fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
