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
from datetime import date, datetime, timedelta
import json
import os
import re
import sqlite3
import sys
import time

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every first-level subdir as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401
from ledger import Ledger, karte_summary_block
from mcs_queries import (JST, current_extract_pred, current_fact_pred,
                         med_is_patient_current, item_unverified)
from mcs_util import HOME, acquire_run_lock
from mcs_signals import normalize_sender_id
from drug_map import (KIND as REF_KIND, PROGRESS_KIND, candidate_note, current_refs,
                      generation_signature)

DB = os.path.join(HOME, "data", "ledger.db")
KIND = "patient_rollup"
STALE_DAYS = 21          # message unseen this long while siblings refresh
# Bump when persisted rollup content changes shape: dirty_projects()
# rebuilds every row stamped with another version (5: empty fact-source vitals).
# Rebuilds rewrite artifacts; automatic card rendering does not read patient_rollup.
PERIOD_CHECK_VERSION = 5
# #20-C thread-level reply_state: strongest reply kind seen in the thread
# after the request, from another sender. View-only, never a transition.
_REPLY_STAGE = {k: i for i, k in enumerate(
    ("ack", "intent", "progress", "answer", "done", "cancel"))}


def _dicts(value) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] \
        if isinstance(value, list) else []


def _llm_reply_kind(blob):
    """reply.kind of one extract_llm artifact (blob or its already
    parsed dict), else None."""
    try:
        lm = blob if isinstance(blob, dict) \
            else json.loads(blob) if blob else None
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(lm, dict) or lm.get("_error"):
        return None
    reply = lm.get("reply")
    kind = reply.get("kind") if isinstance(reply, dict) else None
    return kind if isinstance(kind, str) else None


def build_rollup(ledger, project_id: int) -> dict:
    db = ledger.db
    msgs = db.execute("""
      SELECT message_id, posted_at, posted_at_ts, body_text, sender_id, sender_name, sender_type,
             parent_id, updated_seen, body_state
      FROM messages WHERE project_id=? ORDER BY posted_at_ts DESC, message_id DESC
    """, (project_id,)).fetchall()
    out: dict = {"project_id": project_id, "generated_at": time.time(),
                 "msg_count": len(msgs),
                 "reply_count": sum(1 for m in msgs if m["parent_id"])}
    # 連携サマリー (#21): the newest karte_summary artifact, or None when
    # never fetched — 空 (fetched, nothing registered) stays distinct.
    ks = ledger.karte_summary_current(project_id)
    out["karte_summary"] = None if ks is None else {
        **karte_summary_block(ks), "fetched_at": ks.get("fetched_at")}
    if not msgs:
        return out
    newest = msgs[0]
    out["last_activity"] = newest["posted_at"]
    out["last_activity_ts"] = newest["posted_at_ts"]

    arts = {}
    for a in db.execute(f"""
      SELECT a.message_id, a.kind, a.content FROM artifacts a
      JOIN messages m ON m.message_id=a.message_id
      WHERE m.project_id=? AND a.kind IN
          ('extract_v1','extract_llm','canonical_projection',
           'semantic_facts_v4')
        {current_extract_pred()}
        AND (a.kind IN ('extract_v1','extract_llm')
             OR (1=1 {current_fact_pred()}))
      ORDER BY a.artifact_id
    """, (project_id,)):
        arts.setdefault(a["message_id"], {})[a["kind"]] = a["content"]
    # current_refs needs a med_ref row on the exact message: look it up
    # only for those instead of two queries per message (superset filter;
    # current_refs keeps its own project/binding checks).
    ref_mids = {r[0] for r in db.execute(
        "SELECT DISTINCT a.message_id FROM artifacts a JOIN messages m "
        "ON m.message_id=a.message_id WHERE a.kind=? AND m.project_id=?",
        (REF_KIND, project_id))}

    latest_vitals = None
    latest_labs = {}
    med_period = None
    as_of = datetime.fromtimestamp(out["generated_at"], JST).date()
    next_period_check = None
    # name -> (bucket, item, posted_at): every med name resolves ONCE,
    # on its newest mention — a stop/negation/past report newer than a
    # 'current' mention suppresses it; an item missing status/subject
    # and a rule-extracted name are candidates, never silently current
    # (F06/F08)
    med_state = {}
    sym_pos = {}   # term -> latest positive ts (msgs iterated newest-first)
    sym_neg = {}   # term -> latest negated ts
    # fact_id -> carried canonical fact: newest generation wins per id
    # (msgs iterate newest-first), evidence/statement/kind ride along so
    # slot-less canonical categories stay enumerable in the read model.
    canonical = {}
    requests = []
    # thread root -> [(ts, sender ID, reply kind)] / LLM request rows with
    # their (root, ts, sender ID) for the reply_state pass below
    replies = {}
    req_thread = []
    next_planned = None
    senders = {}
    summary = None
    posted_by_ts = {}
    for m in msgs:
        ts = m["posted_at_ts"] or 0
        posted_by_ts.setdefault(ts, m["posted_at"] or f"msg:{m['message_id']}")
        sender_id = normalize_sender_id(m["sender_id"])
        senders[m["sender_name"] or "?"] = \
            senders.get(m["sender_name"] or "?", 0) + 1
        blobs = arts.get(m["message_id"], {})
        llm_blob = blobs.get("extract_llm")
        llm = llm_blob   # parsed below when it is the row lm came from
        has_fact_source = False
        try:
            v1 = json.loads(blobs["extract_v1"]) \
                if "extract_v1" in blobs else {}
            # semantic_facts_v4 (T18) shadows canonical_projection,
            # which shadows extract_llm for the same message (a
            # hash-current extract_llm row is kept in blobs only so
            # its reply can be read below).
            lm_blob = blobs.get("semantic_facts_v4") \
                or blobs.get("canonical_projection") \
                or llm_blob
            lm = json.loads(lm_blob) if lm_blob else {}
            has_fact_source = (lm_blob is not None and isinstance(lm, dict)
                               and not lm.get("_error"))
            if lm_blob is not None and lm_blob is llm_blob:
                llm = lm    # one decode per unshadowed extract_llm row
        except (ValueError, TypeError, RecursionError):
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
            # Same rule as structured_view._vital_line: once an LLM
            # row exists, a missing vitals key may be an intentional
            # exclusion — never resurrect the v1 rule reading over it.
            vit = lm.get("vitals") if has_fact_source else v1.get("vitals")
            if isinstance(vit, dict) and vit:
                latest_vitals = {"at": m["posted_at"], **vit}
        # v4 labs: newest report per analyte wins (msgs walk newest-first)
        for lb in _dicts(lm.get("labs")):
            if isinstance(lb, dict) and isinstance(lb.get("name"), str) \
                    and lb["name"].strip() and lb["name"] not in latest_labs:
                latest_labs[lb["name"]] = {"at": m["posted_at"], **lb}
        per, chk = _period_candidates(m, v1, as_of)
        if med_period is None:
            med_period = per
        if chk is not None \
                and (next_period_check is None or chk < next_period_check):
            next_period_check = chk
        # Annotate the exact source index without changing surface-name state.
        for ref in (current_refs(db, m["message_id"])
                    if m["message_id"] in ref_mids else ()):
            container, key = ((v1, "medications") if ref["source_kind"] == "extract_v1"
                              else (lm, "meds"))
            meds = container.get(key)
            i = ref["i"]
            if (isinstance(meds, list) and i < len(meds)
                    and isinstance(meds[i], dict)
                    and meds[i].get("name") == ref["name"]):
                meds[i] = {**meds[i], "ref": ref}
        _med_states(m, v1, lm, med_state)
        _symptom_ts(m, v1, lm, ts, sym_pos, sym_neg)
        requests.extend({"kind": rq.get("kind"), "ctx": rq.get("ctx"),
                         "at": m["posted_at"], "mid": m["message_id"]}
                        for rq in _dicts(v1.get("requests")))
        # negated/speculative/ungrounded requests stay flagged for the
        # readers; any flag other than a literal False fails closed
        root = m["parent_id"] or m["message_id"]
        for rq in _dicts(lm.get("requests")):
            row = {"kind": rq.get("to"), "ctx": rq.get("action"),
                   "at": m["posted_at"], "mid": m["message_id"],
                   "unverified": item_unverified(rq)}
            # #20 request kind/condition/due_text under NEW keys — "kind"
            # already means the addressee for mcs-read-model/1 readers
            for src, dst in (("kind", "req_kind"), ("condition", "condition"),
                             ("due_text", "due_text")):
                if isinstance(rq.get(src), str) and rq[src]:
                    row[dst] = rq[src]
            requests.append(row)
            # Only the first 15 requests reach the read model; discarded
            # rows need no reply scan, but all replies remain evidence.
            if len(requests) <= 15:
                req_thread.append((row, root, ts, sender_id))
        # reply lives only in extract_llm; read it there even when a
        # canonical_projection / semantic_facts_v4 blob shadows lm
        kind = _llm_reply_kind(llm)
        if kind in _REPLY_STAGE:
            replies.setdefault(root, []).append((ts, sender_id, kind))
        for f in _dicts(lm.get("canonical_facts")):
            fid = f.get("fact_id")
            if isinstance(fid, str) and fid and fid not in canonical:
                canonical[fid] = {
                    "fact_id": fid, "kind": f.get("kind"),
                    "statement": f.get("statement"),
                    "subject": f.get("subject"),
                    "importance": f.get("importance"),
                    "evidence": f.get("evidence_quote"),
                    "last": m["posted_at"], "mid": m["message_id"]}
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
            symptoms[t] = posted_by_ts.get(pts, str(pts))
    if latest_vitals:
        out["latest_vitals"] = latest_vitals
    if latest_labs:
        out["recent_labs"] = list(latest_labs.values())[:15]
    if med_period:
        out["current_med_period"] = med_period
    if next_period_check is not None:
        out["_next_med_period_check"] = next_period_check
    for bucket, key, cap in (("current", "medications", 20),
                             ("unverified", "unverified_medications", 20),
                             ("planned", "planned_medications", 10)):
        rows = [{"name": k, "dose": v[1].get("dose"), "last": v[2],
                 **{f: v[1][f] for f in ("route", "freq", "prn", "ref")
                    if f in v[1]}}
                for k, v in med_state.items() if v[0] == bucket][:cap]
        if rows:
            out[key] = rows
    if symptoms:
        out["recent_symptoms"] = [{"symptom": k, "last": v}
                                  for k, v in list(symptoms.items())[:20]]
    # ponytail: thread-level reply_state — any later reply by another
    # sender in the same thread counts, no per-request action matching
    # (that is the Loop path). Unknown sender IDs cannot prove different
    # authors. NULL posted_at_ts is 0 and never 'later'.
    for row, root, ts, sender in req_thread:
        seen = [(rts, kind) for rts, rsender, kind in replies.get(root, ())
                if rts > ts and sender is not None and rsender is not None
                and rsender != sender]
        if seen:
            row["reply_state"] = max(
                (kind for _, kind in seen), key=_REPLY_STAGE.get)
            # conflict = a cancel later than the EARLIEST done (seen is
            # newest-first, so done_at[0] would be the newest)
            done_at = [rts for rts, kind in seen if kind == "done"]
            if done_at and any(kind == "cancel" and rts > min(done_at)
                               for rts, kind in seen):
                row["reply_conflict"] = True
    if requests:
        out["recent_requests"] = requests[:15]
    if canonical:
        out["canonical_facts"] = list(canonical.values())
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


def _period_candidates(m, v1: dict, as_of):
    """(med_period, next_boundary_ts) contributed by one message —
    undated/invalid periods are evidence, not current use."""
    best = None
    earliest = None
    for period in reversed(_dicts(v1.get("med_periods"))):
        try:
            start = date.fromisoformat(period["start"])
            end = date.fromisoformat(period["end"])
        except (KeyError, TypeError, ValueError):
            continue  # undated or invalid is evidence, not current
        raw = period.get("raw")
        context = ""
        if isinstance(raw, str) and m["body_text"]:
            pos = m["body_text"].find(raw)
            if pos >= 0:
                context = m["body_text"][
                    max(0, pos - 16):pos + len(raw) + 16]
        if re.search(r"予定|検討", context):
            continue  # a dated plan is not evidence of current use
        if start > as_of:
            boundary = start
        elif start <= as_of <= end:
            if best is None:
                best = period
            boundary = end + timedelta(days=1) if end < date.max else None
        else:
            boundary = None
        if boundary is not None:
            at = datetime.combine(boundary, datetime.min.time(), JST).timestamp()
            if earliest is None or at < earliest:
                earliest = at
    return best, earliest


def _med_states(m, v1: dict, lm: dict, med_state: dict):
    """name -> (bucket, item, posted_at): every med name resolves ONCE,
    on its newest mention — a stop/negation/past report newer than a
    'current' mention suppresses it; an item missing status/subject
    and a rule-extracted name are candidates, never silently current
    (F06/F08)."""
    mentioned_meds = {x.get("name") for x in _dicts(lm.get("meds"))
                      if isinstance(x.get("name"), str)}
    # Chunk merging retains source order. The last mention within a
    # post wins; another person's mention never changes this patient's state.
    for x in reversed(_dicts(lm.get("meds"))):
        name = x.get("name")
        if not isinstance(name, str) or name in ("", "処方薬", "薬"):
            continue
        if x.get("subject") in ("family", "other"):
            continue
        if name in med_state:
            continue  # newest mention already decided this name
        if item_unverified(x):
            med_state[name] = ("unverified", x, m["posted_at"])
        elif x.get("action") == "stop" or x.get("negated") \
                or x.get("status") == "past":
            med_state[name] = ("suppressed", x, m["posted_at"])
        elif med_is_patient_current(x) \
                and x.get("status", "current") == "current":
            med_state[name] = ("current", x, m["posted_at"])
        elif not x.get("negated") \
                and x.get("subject", "patient") == "patient" \
                and x.get("status") == "planned":
            med_state[name] = ("planned", x, m["posted_at"])
        else:
            # stop/past/negated/other-person — suppresses any older
            # 'current' mention of the same name
            med_state[name] = ("suppressed", x, m["posted_at"])
    for x in _dicts(v1.get("medications")):
        name = x.get("name")
        if isinstance(name, str) and name and name not in med_state \
                and name not in mentioned_meds:
            med_state[name] = ("unverified", x, m["posted_at"])


def _symptom_ts(m, v1: dict, lm: dict, ts,
                sym_pos: dict, sym_neg: dict):
    """term -> latest positive/negated ts (msgs iterated newest-first)."""
    for s in v1.get("symptoms") \
            if isinstance(v1.get("symptoms"), list) else []:
        if not isinstance(s, str) or not s:
            continue
        if any(x.get("text") == s for x in _dicts(lm.get("symptoms"))):
            continue
        sym_pos.setdefault(s, ts)
    for s in _dicts(lm.get("symptoms")):
        t = s.get("text")
        if not isinstance(t, str) or not t:
            continue
        if s.get("subject") in ("family", "other") \
                or item_unverified(s):
            continue
        # LLM polarity: a negation newer than a positive mention
        # RESOLVES the symptom — it must cancel v1/rule positives,
        # not just be skipped (Oracle B24). resolved/past statuses
        # resolve the same way — they are not ongoing symptoms.
        if s.get("negated") or s.get("status") in ("resolved", "past"):
            sym_neg.setdefault(t, ts)
        else:
            sym_pos.setdefault(t, ts)


def msgs_by_ts(msgs, ts: float) -> str:
    # callers map NULL posted_at_ts to 0 (unparseable posted_at), so match
    # on the same coercion — otherwise ts=0 finds no row and the symptom's
    # 'last' renders as the literal string "0" (FIX-RU1)
    for m in msgs:
        if (m["posted_at_ts"] or 0) == ts:
            return m["posted_at"] or f"msg:{m['message_id']}"
    return str(ts)


# derive replaces med_ref by DELETE+INSERT, so any rewrite raises MAX(id)
# and any removal lowers COUNT: together they change on every edit (M5).
_REF_SIG_SQL = ("SELECT COUNT(*) || ':' || IFNULL(MAX(artifact_id), '') "
                "FROM artifacts WHERE kind=? AND project_id={pid}")


def current_cached_refs(db, project_id: int, roll: dict, meta) -> dict:
    """Keep raw rollup rows while omitting dictionary annotations without current proof."""
    signature = generation_signature(db)
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError, RecursionError):
            meta = None
    stamped = meta.get("med_ref_generation") if isinstance(meta, dict) else None
    marker = db.execute("SELECT 1 FROM artifacts WHERE kind=? LIMIT 1",
                        (PROGRESS_KIND,)).fetchone() is not None
    # True absence is the old-version compatibility path, not a disabled
    # or damaged active-generation marker. New stamped rows still fence drift.
    mismatch = stamped is not None and stamped != signature
    out = dict(roll)
    sources = {}
    for key in ("medications", "unverified_medications", "planned_medications"):
        if not isinstance(roll.get(key), list):
            continue
        items = []
        for item in roll[key]:
            if not isinstance(item, dict) or "ref" not in item:
                items.append(item)
                continue
            item = dict(item)
            ref, last = item["ref"], item.get("last")
            valid = (not mismatch and isinstance(ref, dict)
                     and ref.get("name") == item.get("name")
                     and isinstance(ref.get("dict_id"), str) and bool(ref["dict_id"])
                     and isinstance(ref.get("dict_sha256"), str)
                     and re.fullmatch(r"[0-9a-f]{64}", ref["dict_sha256"]) is not None
                     and isinstance(ref.get("cands"), list))
            if valid:
                try:
                    valid = bool(candidate_note(ref))
                except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
                    valid = False
            if valid and marker:
                valid = False
                if not isinstance(last, str):
                    item.pop("ref")
                    items.append(item)
                    continue
                if last not in sources:
                    sources[last] = []
                    try:
                        mids = db.execute(
                            "SELECT DISTINCT m.message_id FROM messages m JOIN artifacts a "
                            "ON a.message_id=m.message_id WHERE m.project_id=? "
                            "AND m.posted_at=? AND a.kind=?",
                            (project_id, last, REF_KIND))
                        for row in mids:
                            sources[last].extend(current_refs(db, row[0]))
                    except sqlite3.DatabaseError:
                        pass  # old/incomplete schema cannot prove a dictionary annotation
                valid = ref.get("name") == item.get("name") and ref in sources[last]
            if not valid:
                item.pop("ref")
            items.append(item)
        out[key] = items
    # An active generation changed while the reader checked source bindings.
    # Strip only annotations; vitals/raw medication rows remain available.
    if generation_signature(db) != signature:
        for key in ("medications", "unverified_medications", "planned_medications"):
            if isinstance(out.get(key), list):
                out[key] = [{k: v for k, v in item.items() if k != "ref"}
                            if isinstance(item, dict) else item for item in out[key]]
    return out


def _newest_source_ts(ledger, project_id: int) -> float:
    """Newest source stamp dirty_projects compares to generated_at."""
    return ledger.db.execute(
        "SELECT MAX(IFNULL((SELECT MAX(created_at) FROM artifacts"
        " WHERE project_id=:p AND kind IN ('extract_v1','extract_llm',"
        "'canonical_projection','semantic_facts_v4','karte_summary')), 0),"
        " IFNULL((SELECT MAX(updated_seen) FROM messages"
        " WHERE project_id=:p), 0))", {"p": project_id}).fetchone()[0]


def rebuild(ledger, project_id: int) -> int:
    """Atomic replace — a crash between delete and insert must not leave
    a patient with NO rollup (Oracle B22). An unchanged rebuild (all
    but generated_at equal) keeps the content and id; it only advances
    meta.generated_at when a newer source would otherwise keep it dirty."""
    # sign before building: a rewrite racing the build stays dirty (M5)
    ref_generation = generation_signature(ledger.db)
    ref_sig = ledger.db.execute(_REF_SIG_SQL.format(pid="?"),
                                (REF_KIND, project_id)).fetchone()[0]
    d = build_rollup(ledger, project_id)
    # compare in JSON space (tuples→lists, int keys→str) like the stored row
    new = json.loads(json.dumps(d, ensure_ascii=False))
    new.pop("generated_at", None)
    with ledger.db:
        # read + decide inside the write txn: a concurrent DELETE
        # (semantic_store / ledger / semantic_v4) just means no match → INSERT
        old = ledger.db.execute(
            "SELECT artifact_id, content, meta FROM artifacts"
            " WHERE kind=? AND project_id=?", (KIND, project_id)).fetchall()
        if len(old) == 1:
            try:
                oc, om = json.loads(old[0]["content"]), json.loads(
                    old[0]["meta"])
            except (TypeError, ValueError, RecursionError):
                oc = om = None
            if isinstance(oc, dict) and isinstance(om, dict):
                oc.pop("generated_at", None)
                if (oc == new and om.get("period_check_version")
                        == PERIOD_CHECK_VERSION
                        and om.get("next_med_period_check")
                        == d.get("_next_med_period_check")
                        and om.get("med_ref_sig") == ref_sig
                        and om.get("med_ref_generation") == ref_generation):
                    # a no-op source write (LLM error row, unchanged
                    # re-save) must still clear dirty_projects, else the
                    # patient is rebuilt every tick; the stamp was taken
                    # before the sources were read, so a racing write
                    # stays dirty
                    g = om.get("generated_at")
                    if type(g) not in (int, float) or not 0 <= g < 1e12 \
                            or _newest_source_ts(ledger, project_id) > g:
                        om["generated_at"] = d["generated_at"]
                        ledger.db.execute(
                            "UPDATE artifacts SET meta=? WHERE artifact_id=?",
                            (json.dumps(om), old[0]["artifact_id"]))
                    return old[0]["artifact_id"]
        ledger.db.execute(
            "DELETE FROM artifacts WHERE kind=? AND project_id=?",
            (KIND, project_id))
        cur = ledger.db.execute(
            "INSERT INTO artifacts(kind,project_id,message_id,content,"
            "model,meta,created_at) VALUES(?,?,?,?,?,?,?)",
            (KIND, project_id, None,
             json.dumps(d, ensure_ascii=False), "rules-v1",
             json.dumps({"generated_at": d["generated_at"],
                         "period_check_version": PERIOD_CHECK_VERSION,
                         "next_med_period_check":
                         d.get("_next_med_period_check"),
                         "med_ref_sig": ref_sig,
                         "med_ref_generation": ref_generation}), time.time()))
    return cur.lastrowid


def dirty_projects(ledger) -> list:
    """Patients whose rollup is missing or older than its newest source
    (message/artifact). Rolls forward artifact-only changes too — an LLM
    pass landing after the last message must still refresh (Oracle B23).
    med_ref rewrites/removals show as a changed signature, not via art_ts:
    derive rewrites every row on a dictionary change, and a rollup whose
    content is unchanged never advances generated_at (M5)."""
    rows = ledger.db.execute("""
      SELECT p.project_id, r.g AS gen, r.period_version, r.next_check,
        r.ref_sig, r.ref_generation, ({sig}) AS cur_ref_sig,
        (SELECT MAX(a.created_at) FROM artifacts a
          WHERE a.project_id=p.project_id
            AND a.kind IN ('extract_v1','extract_llm',
                           'canonical_projection',
                           'semantic_facts_v4','karte_summary')) AS art_ts,
        (SELECT MAX(m.updated_seen) FROM messages m
          WHERE m.project_id=p.project_id) AS msg_ts
      FROM patients p
        LEFT JOIN (SELECT project_id,
                   MAX(CASE WHEN json_valid(meta)
                            THEN json_extract(meta,'$.generated_at') END) g,
                   MAX(CASE WHEN json_valid(meta)
                            THEN json_extract(meta,'$.period_check_version')
                            END) period_version,
                   MAX(CASE WHEN json_valid(meta)
                            THEN json_extract(meta,'$.next_med_period_check')
                            END) next_check,
                   MAX(CASE WHEN json_valid(meta)
                            THEN json_extract(meta,'$.med_ref_sig')
                            END) ref_sig,
                   MAX(CASE WHEN json_valid(meta)
                            THEN json_extract(meta,'$.med_ref_generation')
                            END) ref_generation
                 FROM artifacts WHERE kind=?
                 GROUP BY project_id) r ON r.project_id=p.project_id
      WHERE EXISTS (SELECT 1 FROM messages m3
                    WHERE m3.project_id=p.project_id)
    """.replace("{sig}", _REF_SIG_SQL.format(pid="p.project_id")),
        (REF_KIND, KIND)).fetchall()
    now = time.time()
    ref_generation = generation_signature(ledger.db)
    return [x["project_id"] for x in rows
            if type(x["gen"]) not in (int, float)
            or not 0 <= x["gen"] < 1e12
            or x["period_version"] != PERIOD_CHECK_VERSION
            or x["ref_sig"] != x["cur_ref_sig"]
            or x["ref_generation"] != ref_generation
            or (x["next_check"] is not None and (
                type(x["next_check"]) not in (int, float)
                or not 0 <= x["next_check"] < 1e12
                or x["next_check"] <= now))
            or (x["art_ts"] or 0) > x["gen"]
            or (x["msg_ts"] or 0) > x["gen"]]


def rebuild_many(ledger, project_ids, dirty_only: bool = False,
                 *, deadline: float | None = None) -> int:
    if deadline is not None and time.monotonic() >= deadline:
        return 0
    if dirty_only:
        dirty = set(dirty_projects(ledger))
        project_ids = [p for p in project_ids if p in dirty]
    n = 0
    for pid in project_ids:
        if deadline is not None and time.monotonic() >= deadline:
            break
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
    led = None
    try:
        led = Ledger(DB)
        ids = args.project or [r["project_id"] for r in
                               led.db.execute("SELECT project_id FROM patients")]
        n = rebuild_many(led, ids)
        print(json.dumps({"rollups": n}, ensure_ascii=False))
        return 0
    finally:
        try:
            if led is not None:
                led.close()
        finally:
            os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
