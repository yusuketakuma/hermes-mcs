"""extract_qc: Jev quality control over extract_llm artifacts.

Annotate-only audit of the fully-local extraction. The drain's existing
guards (OFF mode, circuit, paused, project_ids, daily budget, usage
reservation, mid-run config recheck) apply verbatim — the job never
mutates the extraction artifact and never suppresses an item.
"""
from __future__ import annotations

import json

from mcs_util import load_config
import semantic_jev as jev
import semantic_runtime as runtime
from semantic_policy import QC_ARTIFACT, QC_JOB_KIND, semantic_config

QC_MAX_ITEMS = 16


def _qc_seed(ledger, now: float, limit: int = 32) -> int:
    """Queue extract_qc jobs for current extract_llm artifacts lacking a
    QC artifact for the same content hash. Re-extraction (hash change)
    re-pends the row; a pending/failed row for the SAME hash is left
    alone (failed inputs stay failed until an explicit retry).
    json_valid guards keep poisoned meta rows from aborting the scan."""
    import extract_llm
    with ledger.db:
        cur = ledger.db.execute("""
          INSERT INTO fetch_jobs(kind,project_id,message_id,parent_id,
            payload,state,next_try,created_at,updated_at)
          SELECT ?, m.project_id, a.message_id, NULL,
            json_object('hash', json_extract(a.meta,'$.hash')),
            'pending', ?, ?, ?
          FROM artifacts a JOIN messages m ON m.message_id=a.message_id
          WHERE a.kind='extract_llm' AND json_valid(a.meta)
            AND json_extract(a.meta,'$.hash')=m.content_hash
            AND json_extract(a.meta,'$.extract_version')=?
            AND json_extract(a.meta,'$.error') IS NULL
            AND NOT EXISTS(SELECT 1 FROM artifacts q
                           WHERE q.kind=? AND q.message_id=a.message_id
                             AND json_valid(q.meta)
                             AND json_extract(q.meta,'$.hash')
                                 =json_extract(a.meta,'$.hash'))
          ORDER BY a.artifact_id DESC LIMIT ?
          ON CONFLICT(kind,project_id,message_id) DO UPDATE SET
            payload=excluded.payload,state='pending',attempts=0,
            next_try=excluded.next_try,updated_at=excluded.updated_at
          WHERE fetch_jobs.state IN ('done','failed')
            AND coalesce(json_extract(fetch_jobs.payload,'$.hash'),'')
                !=json_extract(excluded.payload,'$.hash')
        """, (QC_JOB_KIND, now, now, now,
              extract_llm.EXTRACT_VERSION, QC_ARTIFACT, limit))
    return cur.rowcount


def _qc_questions(ex: dict) -> tuple[dict, list, dict]:
    """Per-item support questions (noul) plus classification audits
    (choice). Returns (questions, layout, context_items): layout maps
    qid -> (section, index); context_items carries the extracted items
    as quoted DATA for state.context — never in the instruction channel
    (an evidence span is a verbatim quote of the message body, i.e.
    attacker-controlled text)."""
    questions, layout, ctx_items = {}, [], {}
    n = QC_MAX_ITEMS
    for section in ("meds", "symptoms", "events"):
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

    msg = ledger.db.execute(
        "SELECT content_hash,body_text FROM messages WHERE message_id=?",
        (mid,)).fetchone()
    if msg is None:
        return done()
    art = None
    for r in ledger.db.execute(
            "SELECT content,meta FROM artifacts WHERE kind='extract_llm'"
            " AND message_id=? ORDER BY artifact_id DESC", (mid,)):
        try:
            m = json.loads(r["meta"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if m.get("hash") == msg["content_hash"] \
                and m.get("extract_version") == extract_llm.EXTRACT_VERSION \
                and not m.get("error"):
            art = (r["content"], m["hash"])
            break
    if art is None:
        return done()
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
        runtime.guard(
            ledger, token, deadline=deadline,
            expected_config_generation=config_generation,
            expected_mode=scfg["mode"], cfg_path=cfg_path,
            load_cfg=load_config, parse_cfg=semantic_config,
            stage=stage)

    req0 = jev_client.requests_made
    client, _cleanup = runtime.bind_jev(jev_client, guard, reserve_fn)
    try:
        out = (_eval_chunked(client, state, questions, deadline,
                             scfg["max_questions_per_request"])
               if questions else {"answers": {}})
    except jev.JevError as error:
        try:
            jev_client.last_error = error
        except Exception:
            pass
        cls = _jev_failure_class(error)
        if cls == "resource":
            return "deferred"
        if cls == "retry":
            return "retry"
        # permanent failure: record why QC could not run instead of
        # retrying forever — the extraction artifact itself is untouched
        with ledger.db:
            ledger.db.execute(
                "DELETE FROM artifacts WHERE kind=? AND message_id=?"
                " AND json_valid(meta)"
                " AND json_extract(meta,'$.hash') != ?",
                (QC_ARTIFACT, mid, art[1]))
            ledger.artifact_add_tx(
                QC_ARTIFACT, json.dumps({"qc": "unevaluated",
                                         "reason": error.kind},
                                        ensure_ascii=False),
                project_id=pid, message_id=mid, model=jev.JEV_MODEL,
                meta={"hash": art[1],
                      "extract_version": extract_llm.EXTRACT_VERSION,
                      "qc": "unevaluated"})
            if not runtime.transition_tx(ledger, token, "done"):
                raise runtime.RuntimeStale("qc:write")
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
            items.append({"section": section, "index": i,
                          "item": (ex.get(section) or [])[i],
                          # NO_MATCH means 'not supported by this
                          # text', never 'the fact does not exist'
                          "verdict": jev.verdict_for(
                              ans.get("noul", 0.0),
                              scfg["match_threshold"],
                              scfg["nomatch_threshold"]),
                          "noul": ans.get("noul")})
    content = {"qc": "done", "items": items}
    if urgency is not None:
        content["urgency"] = urgency
    try:
        with ledger.db:
            # stale QC rows for a superseded hash are replaced in the
            # same tx; the job transition lands here too so the row is
            # never left claimable after its result
            ledger.db.execute(
                "DELETE FROM artifacts WHERE kind=? AND message_id=?"
                " AND json_valid(meta)"
                " AND json_extract(meta,'$.hash') != ?",
                (QC_ARTIFACT, mid, art[1]))
            ledger.artifact_add_tx(
                QC_ARTIFACT, json.dumps(content, ensure_ascii=False),
                project_id=pid, message_id=mid, model=jev.JEV_MODEL,
                meta={"hash": art[1],
                      "extract_version": extract_llm.EXTRACT_VERSION,
                      "qc": "done",
                      "jev_requests": jev_client.requests_made - req0})
            if not runtime.transition_tx(ledger, token, "done"):
                raise runtime.RuntimeStale("qc:write")
    except runtime.RuntimeStale:
        return "stale"
    return "done"


