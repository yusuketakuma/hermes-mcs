"""extract_qc: Jev quality control over extract_llm artifacts.

Annotate-only audit of the fully-local extraction. The drain's existing
guards (OFF mode, circuit, paused, project_ids, daily budget, usage
reservation, mid-run config recheck) apply verbatim — the job never
mutates the extraction artifact and never suppresses an item.
"""
from __future__ import annotations

import json
import time
from contextlib import suppress

from mcs_util import load_config
from mcs_queries import current_qc_pred, qc_source_id
import semantic_jev as jev
import semantic_runtime as runtime
from semantic_policy import QC_ARTIFACT, QC_JOB_KIND, semantic_config

QC_MAX_ITEMS = 16
# QC covers posts from the last 60 days. A pending job that ages past
# the window is reaped rather than evaluated late.
QC_REALTIME_MAX_AGE_S = 60 * 86400


def qc_scope_sql(now: float, msg: str = "m") -> tuple[str, tuple]:
    """Shared post-age eligibility for QC seeding, processing, and display."""
    return (f"({msg}.posted_at_ts IS NOT NULL AND {msg}.posted_at_ts >= ?)",
            (now - QC_REALTIME_MAX_AGE_S,))


def _drop_stale_qc(ledger, mid: int, current_hash: str) -> None:
    """Replace QC rows left over from a superseded message hash — the
    replacement lands in the caller's tx so the row is never left
    claimable without its result."""
    ledger.db.execute(
        "DELETE FROM artifacts WHERE kind=? AND message_id=?"
        " AND json_valid(meta) AND json_extract(meta,'$.hash') != ?",
        (QC_ARTIFACT, mid, current_hash))


def _qc_seed(ledger, now: float, limit: int = 32) -> int:
    """Queue extract_qc jobs for current extract_llm artifacts lacking a
    QC artifact for the same content hash and extractor generation.
    Re-extraction (hash change) or an extractor version bump re-pends
    the row; a pending/failed row for the SAME hash+version is left
    alone (failed inputs stay failed until an explicit retry).
    Pending and exhausted jobs for the same extraction cannot consume
    the seed window. A changed extraction replaces their generation."""
    import extract_llm
    version = extract_llm.EXTRACT_VERSION
    scope, scope_params = qc_scope_sql(now)
    with ledger.db:
        ledger.db.execute(
            "DELETE FROM fetch_jobs WHERE kind=? AND state='pending'"
            " AND message_id IN (SELECT message_id FROM messages m"
            f"  WHERE NOT {scope})",
            (QC_JOB_KIND, *scope_params))
        cur = ledger.db.execute(f"""
          INSERT INTO fetch_jobs(kind,project_id,message_id,parent_id,
            payload,state,next_try,created_at,updated_at)
          SELECT ?, m.project_id, a.message_id, NULL,
            json_object('hash', json_extract(a.meta,'$.hash'),
                        'ver', json_extract(a.meta,'$.extract_version'),
                        'source_artifact_id', a.artifact_id),
            'pending', ?, ?, ?
          FROM artifacts a JOIN messages m ON m.message_id=a.message_id
          WHERE a.artifact_id={qc_source_id(version=version)}
            AND json_extract(a.meta,'$.prefilter') IS NULL
            AND {scope}
            AND NOT EXISTS(SELECT 1 FROM artifacts q
                           WHERE q.kind=? AND q.message_id=a.message_id
                             {current_qc_pred('q', version=version)})
            AND NOT EXISTS(SELECT 1 FROM fetch_jobs j
                           WHERE j.kind=? AND j.message_id=a.message_id
                             AND j.state IN ('pending','failed')
                             AND CASE WHEN json_valid(j.payload) THEN
                               json_extract(j.payload,'$.source_artifact_id')
                                   =a.artifact_id
                               AND json_extract(j.payload,'$.hash')=m.content_hash
                               AND json_extract(j.payload,'$.ver')={version}
                             ELSE 0 END)
          ORDER BY a.artifact_id DESC LIMIT ?
          ON CONFLICT(kind,project_id,message_id) DO UPDATE SET
            payload=excluded.payload,state='pending',attempts=0,
            next_try=excluded.next_try,updated_at=excluded.updated_at
          WHERE fetch_jobs.state='done' OR CASE
            WHEN json_valid(fetch_jobs.payload) THEN
              coalesce(json_extract(fetch_jobs.payload,'$.source_artifact_id'),0)
                !=json_extract(excluded.payload,'$.source_artifact_id')
              OR coalesce(json_extract(fetch_jobs.payload,'$.hash'),'')
                !=json_extract(excluded.payload,'$.hash')
              OR coalesce(json_extract(fetch_jobs.payload,'$.ver'),0)
                !=json_extract(excluded.payload,'$.ver')
            ELSE 1 END
        """, (QC_JOB_KIND, now, now, now, *scope_params,
              QC_ARTIFACT, QC_JOB_KIND, limit))
    return cur.rowcount


_VITAL_JP = {"bt": "体温", "hr": "脈拍・心拍数", "rr": "呼吸数",
             "sbp": "収縮期血圧", "dbp": "拡張期血圧",
             "spo2": "SpO2(酸素飽和度)", "bs": "血糖値"}


def _qc_questions(ex: dict) -> tuple[dict, list, dict]:
    """Per-item support questions (noul) plus classification audits
    (choice). Returns (questions, layout, context_items): layout maps
    qid -> (section, index); context_items carries the extracted items
    as quoted DATA for state.context — never in the instruction channel
    (an evidence span is a verbatim quote of the message body, i.e.
    attacker-controlled text). Vitals join the audit with the vital KEY
    as index — a key mislabel ('脈は48' -> bs:48) reads as NO_MATCH."""
    questions, layout, ctx_items = {}, [], {}
    n = QC_MAX_ITEMS
    for section in ("meds", "symptoms", "events", "labs"):
        for i, item in enumerate((ex.get(section) or [])[:n]):
            label = (item if section == "events"
                     else json.dumps(item, ensure_ascii=False))
            qid = f"{section[0]}{i}"
            ctx_items[qid] = str(label)[:400]
            questions[qid] = jev.noul_question(
                f"state.context の id={qid} の抽出項目は、"
                "対象の投稿本文に裏付けられているか",
                "本文にこの項目を裏付ける記述がある",
                "本文にこの項目を裏付ける記述がない")
            layout.append((qid, section, i))
            n -= 1
            if n <= 0:
                break
        if n <= 0:
            break
    vits = ex.get("vitals")
    if isinstance(vits, dict) and n > 0:
        for i, (k, val) in enumerate(list(vits.items())[:n]):
            qid = f"v{i}"
            ctx_items[qid] = json.dumps({"vitals": {k: val}},
                                        ensure_ascii=False)
            jp = _VITAL_JP.get(k, "未分類のバイタル")
            questions[qid] = jev.noul_question(
                f"state.context の id={qid} のバイタル項目は、"
                "対象の投稿本文に裏付けられているか",
                f"本文に{jp}の項目としてstate.contextに示した値を裏付ける記述がある",
                f"本文に{jp}の項目としてstate.contextに示した値を裏付ける記述がない")
            layout.append((qid, "vitals", k))
            n -= 1
    urg = ex.get("urgency")
    if urg in ("high", "routine"):
        questions["urg"] = jev.choice_question(
            "対象の投稿本文の緊急度として最も妥当なものを選ぶ"
            f"（抽出側の分類: {urg}）",
            {"high": "早急な対応が必要な内容",
             "routine": "通常対応で足りる内容",
             "unclear": "本文だけでは判断できない"})
        layout.append(("urg", "urgency", -1))
    return questions, layout, ctx_items


def _process_qc_job(ledger, scfg: dict, job, jev_client,
                    deadline: float, reserve_fn=None,
                    cfg_path: str | None = None,
                    config_generation: str | None = None) -> str:
    """One extract_qc job -> extract_qc artifact (annotate only).
    The job row transitions to 'done' inside the artifact commit tx —
    a claimed row must never be left re-claimable after its result
    landed. Returns 'done'|'deferred'|'retry'|'stale'."""
    import extract_llm
    from semantic_drain import _eval_chunked, _jev_failure_class
    if jev_client is None or scfg.get("extract_qc") != "annotate":
        return "deferred"
    token = runtime.JobToken.from_row(job)
    deadline = runtime.job_deadline(scfg, deadline)
    pid, mid = job["project_id"], job["message_id"]

    def done():
        return "done" if runtime.transition(ledger, token, "done") \
            else "stale"

    scope, scope_params = qc_scope_sql(time.time())
    msg = ledger.db.execute(
        f"SELECT content_hash,body_text,{scope} AS qc_eligible FROM messages m"
        " WHERE message_id=?", (*scope_params, mid)).fetchone()
    if msg is None:
        return done()
    # A queued job whose post aged past the window is
    # completed without evaluation rather than audited late.
    if not msg["qc_eligible"]:
        return done()
    source = ledger.db.execute(
        "SELECT a.artifact_id,a.content,json_extract(a.meta,'$.hash') AS hash "
        "FROM messages m JOIN artifacts a ON a.artifact_id="
        f"{qc_source_id(version=extract_llm.EXTRACT_VERSION)} "
        "WHERE m.message_id=?", (mid,)).fetchone()
    if source is None:
        return done()
    art = (source["content"], source["hash"], source["artifact_id"])
    payload = runtime.parse_payload(job)
    if payload.get("source_artifact_id") != art[2] \
            or payload.get("hash") != art[1] \
            or payload.get("ver") != extract_llm.EXTRACT_VERSION:
        return done()  # the seed pass will queue the new extraction
    try:
        ex = json.loads(art[0] or "{}")
    except (json.JSONDecodeError, TypeError):
        return done()
    if not isinstance(ex, dict):
        return done()
    questions, layout, ctx_items = _qc_questions(ex)
    state = {"target": {"id": f"m{mid}", "role": "target",
                        "text": msg["body_text"]},
             "context": [{"id": qid, "role": "extracted_item",
                          "text": text}
                         for qid, text in ctx_items.items()]}

    def guard(stage):
        def current_source():
            row = ledger.db.execute(
                f"SELECT content_hash,{qc_source_id(version=extract_llm.EXTRACT_VERSION)} "
                "FROM messages m WHERE message_id=?", (mid,)).fetchone()
            return f"{row[0]}:{row[1]}" if row else None
        runtime.guard(
            ledger, token, deadline=deadline,
            expected_config_generation=config_generation,
            expected_mode=scfg["mode"], cfg_path=cfg_path,
            load_cfg=load_config, parse_cfg=semantic_config,
            source_fingerprint=f"{art[1]}:{art[2]}", current_source=current_source,
            stage=stage)

    req0 = jev_client.requests_made
    client, _cleanup = runtime.bind_jev(jev_client, guard, reserve_fn)
    try:
        out = (_eval_chunked(client, state, questions, deadline,
                             scfg["max_questions_per_request"])
               if questions else {"answers": {}})
    except jev.JevError as error:
        with suppress(Exception):
            jev_client.last_error = error
        cls = _jev_failure_class(error)
        if cls == "resource":
            return "deferred"
        if cls == "retry":
            return "retry"
        # permanent failure: record why QC could not run instead of
        # retrying forever — the extraction artifact itself is untouched
        try:
            guard("qc:write")
        except runtime.RuntimeStale:
            return "stale"
        except (runtime.RuntimeOff, runtime.RuntimeBudget):
            return "deferred"
        with ledger.db:
            _drop_stale_qc(ledger, mid, art[1])
            ledger.artifact_add_tx(
                QC_ARTIFACT, json.dumps({"qc": "unevaluated",
                                         "reason": error.kind},
                                        ensure_ascii=False),
                project_id=pid, message_id=mid, model=jev.JEV_MODEL,
                meta={"hash": art[1],
                      "extract_version": extract_llm.EXTRACT_VERSION,
                      "source_artifact_id": art[2],
                      "qc": "unevaluated"})
            if not runtime.transition_tx(ledger, token, "done"):
                raise runtime.RuntimeStale("qc:write") from None
        return "done"
    except runtime.RuntimeStale:
        return "stale"
    except (runtime.RuntimeOff, runtime.RuntimeBudget):
        return "deferred"
    answers = out["answers"]
    items, urgency = [], None
    for qid, section, i in layout:
        ans = answers.get(qid)
        if ans is None:
            continue
        if section == "urgency":
            urgency = {"extracted": ex.get("urgency"),
                       "jev": ans.get("choice"),
                       "confidence": ans.get("confidence")}
        else:
            src = ex.get(section)
            item = (src.get(i) if isinstance(src, dict)
                    else (src or [])[i])
            items.append({"section": section, "index": i,
                          "item": ({section: {i: item}}
                                   if section == "vitals" else item),
                          # NO_MATCH means 'not supported by this
                          # text', never 'the fact does not exist'
                          "verdict": jev.verdict_for(
                              ans.get("noul", 0.0),
                              scfg["match_threshold"],
                              scfg["nomatch_threshold"]),
                          "noul": ans.get("noul")})
    by_field = {}
    for field in ("meds", "symptoms", "events", "labs", "requests",
                  "vitals", "summary", "points", "urgency"):
        value = ex.get(field)
        total = len(value) if isinstance(value, list | dict) \
            else int(isinstance(value, str) and bool(value))
        checked = sum(item["section"] == field for item in items)
        if field == "urgency":
            checked = int(urgency is not None)
        by_field[field] = {"checked": checked, "total": total,
                           "unchecked": total - checked}
    total_items = sum(v["total"] for v in by_field.values())
    checked_items = sum(v["checked"] for v in by_field.values())
    content = {"qc": "done", "items": items,
               "coverage": {"checked": checked_items, "total": total_items,
                            "unchecked": total_items - checked_items,
                            "capped": sum(by_field[s]["total"] for s in
                                ("meds", "symptoms", "events", "labs", "vitals"))
                                > QC_MAX_ITEMS,
                            "by_field": by_field}}
    if urgency is not None:
        content["urgency"] = urgency
    try:
        guard("qc:write")
        with ledger.db:
            # stale QC rows for a superseded hash are replaced in the
            # same tx; the job transition lands here too so the row is
            # never left claimable after its result
            _drop_stale_qc(ledger, mid, art[1])
            ledger.artifact_add_tx(
                QC_ARTIFACT, json.dumps(content, ensure_ascii=False),
                project_id=pid, message_id=mid, model=jev.JEV_MODEL,
                meta={"hash": art[1],
                      "extract_version": extract_llm.EXTRACT_VERSION,
                      "source_artifact_id": art[2],
                      "qc": "done",
                      "jev_requests": jev_client.requests_made - req0})
            if not runtime.transition_tx(ledger, token, "done"):
                raise runtime.RuntimeStale("qc:write")
    except runtime.RuntimeStale:
        return "stale"
    except (runtime.RuntimeOff, runtime.RuntimeBudget):
        return "deferred"
    return "done"
