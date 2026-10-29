"""extract_qc: Jev quality control over extract_llm artifacts.

Annotate-only audit of the fully-local extraction. The drain's existing
guards (OFF mode, circuit, paused, project_ids, daily budget, usage
reservation, mid-run config recheck) apply verbatim — the job never
mutates the extraction artifact and never suppresses an item.
"""
from __future__ import annotations

from inference_version import CURRENT_VERSION, VERSION_KEY, is_current

import json
import math
import time
from collections import deque
from contextlib import suppress

from mcs_util import load_config, strict_json
from mcs_queries import current_qc_pred, qc_source_id
import semantic_jev as jev
import semantic_runtime as runtime
from semantic_policy import QC_ARTIFACT, QC_JOB_KIND, runtime_config

QC_MAX_ITEMS = 16
_QC_SECTIONS = ("vitals", "meds", "symptoms", "labs", "events")
# QC covers posts from the last 60 days. A pending job that ages past
# the window is reaped rather than evaluated late.
QC_REALTIME_MAX_AGE_S = 60 * 86400


def skipped_current(db, project_id, message_id, source_id, source_hash, version, scfg):
    """A skip is usable only for this extraction and without a known QC objection."""
    rows = db.execute("SELECT content,meta FROM artifacts WHERE kind='extract_qc_skipped' "
                      "AND project_id=? AND message_id=? ORDER BY artifact_id DESC LIMIT 1025",
                      (project_id, message_id)).fetchall()
    source = db.execute("SELECT content FROM artifacts WHERE artifact_id=? AND kind='extract_llm' "
                        "AND project_id=? AND message_id=?", (source_id, project_id, message_id)).fetchone()
    if not rows or len(rows) > 1024 or source is None:
        return False
    try:
        extraction = strict_json(source[0])
        questions, _, _ = _qc_questions(extraction)
        matched = False
        for row in rows:
            content, meta = strict_json(row[0]), strict_json(row[1])
            if not isinstance(meta, dict) or not is_current(meta):
                return False
            if meta.get("hash") != source_hash or meta.get("source_artifact_id") != source_id:
                continue
            matched = True
            if (not isinstance(content, dict) or content.get("status") != "SKIPPED"
                    or content.get("reason") not in {"not_configured", "disabled", "daily_budget", "backlog_reserved",
                                                     "circuit_open", "service_unavailable"}
                    or type(meta.get("extract_version")) is not int or meta["extract_version"] != version):
                return False
            partial = content.get("partial_answers", {})
            if not isinstance(partial, dict) or set(partial) - set(questions):
                return False
            for qid, answer in partial.items():
                if not isinstance(answer, dict) or answer.get("type") != questions[qid].get("type"):
                    return False
                if answer["type"] == "noul":
                    value = answer.get("noul")
                    if type(value) not in (int, float) or not math.isfinite(value) or not scfg["match_threshold"] <= value <= 1:
                        return False
                elif (qid != "urg" or answer.get("choice") != extraction.get("urgency")
                      or type(answer.get("confidence")) not in (int, float)
                      or not math.isfinite(answer["confidence"])
                      or not scfg["match_threshold"] <= answer["confidence"] <= 1):
                    return False
        return matched
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        return False


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


def _qc_seed(ledger, now: float, limit: int = 32, *, skip_mode: bool = False) -> int:
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
                        '{VERSION_KEY}', '{CURRENT_VERSION}',
                        'ver', json_extract(a.meta,'$.extract_version'),
                        'source_artifact_id', a.artifact_id),
            'pending', ?, ?, ?
          FROM artifacts a JOIN messages m ON m.message_id=a.message_id
          WHERE a.artifact_id={qc_source_id(version=version)}
            AND CASE WHEN json_valid(a.meta) AND json_type(a.meta)='object'
                THEN json_extract(a.meta,'$.prefilter') IS NULL END
            AND {scope}
            AND NOT EXISTS(SELECT 1 FROM artifacts q
                           WHERE q.kind=? AND q.message_id=a.message_id
                             {current_qc_pred('q', version=version)})
            AND (NOT ? OR NOT EXISTS(SELECT 1 FROM artifacts s
                           WHERE s.kind='extract_qc_skipped' AND s.project_id=m.project_id
                             AND s.message_id=a.message_id AND json_valid(s.meta)
                             AND json_extract(s.meta,'$.hash')=m.content_hash
                             AND json_extract(s.meta,'$.extract_version')={version}
                             AND json_extract(s.meta,'$.source_artifact_id')=a.artifact_id
                             AND json_extract(s.meta,'$.{VERSION_KEY}')='{CURRENT_VERSION}'))
            AND NOT EXISTS(SELECT 1 FROM fetch_jobs j
                           WHERE j.kind=? AND j.message_id=a.message_id
                             AND j.state IN ('pending','failed')
                             AND CASE WHEN json_valid(j.payload) THEN
                               json_extract(j.payload,'$.source_artifact_id')
                                   =a.artifact_id
                               AND json_extract(j.payload,'$.hash')=m.content_hash
                               AND json_extract(j.payload,'$.ver')={version}
                               AND json_extract(j.payload,'$.{VERSION_KEY}')='{CURRENT_VERSION}'
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
              OR json_extract(fetch_jobs.payload,'$.{VERSION_KEY}') IS NOT '{CURRENT_VERSION}'
            ELSE 1 END
        """, (QC_JOB_KIND, now, now, now, *scope_params,
              QC_ARTIFACT, int(skip_mode), QC_JOB_KIND, limit))
    return cur.rowcount


_VITAL_JP = {"bt": "体温", "hr": "脈拍・心拍数", "rr": "呼吸数",
             "sbp": "収縮期血圧", "dbp": "拡張期血圧",
             "spo2": "SpO2(酸素飽和度)", "bs": "血糖値"}


def _qc_candidates(ex: dict):
    """Select a bounded round-robin audit while retaining original item indices."""
    pools = {}
    for section in _QC_SECTIONS:
        value = ex.get(section) or ([] if section != "vitals" else {})
        entries = value.items() if isinstance(value, dict) else enumerate(value)
        seen = set()
        pools[section] = deque()
        for index, item in entries:
            identity = json.dumps(item, sort_keys=True, ensure_ascii=False)
            if section == "events" and identity in seen:
                continue
            seen.add(identity)
            pools[section].append((index, item))
    n = QC_MAX_ITEMS
    while n > 0 and any(pools.values()):
        for section, pool in pools.items():
            if not pool or n <= 0:
                continue
            index, item = pool.popleft()
            yield section, index, item
            n -= 1


def _qc_questions(ex: dict) -> tuple[dict, list, dict]:
    """Per-item support questions (noul) plus classification audits
    (choice). Returns (questions, layout, context_items): layout maps
    qid -> (section, index); context_items carries the extracted items
    as quoted DATA for state.context — never in the instruction channel
    (an evidence span is a verbatim quote of the message body, i.e.
    attacker-controlled text). Vitals join the audit with the vital KEY
    as index — a key mislabel ('脈は48' -> bs:48) reads as NO_MATCH."""
    questions, layout, ctx_items = {}, [], {}
    sequence = {section: 0 for section in _QC_SECTIONS}
    for section, index, item in _qc_candidates(ex):
        # Numeric IDs keep untrusted vital names out of instructions.
        qid = f"{section[0]}{sequence[section]}"
        sequence[section] += 1
        label = {"vitals": {index: item}} if section == "vitals" else item
        ctx_items[qid] = json.dumps(label, ensure_ascii=False)[:400]
        measurement = (_VITAL_JP.get(index, "測定")
                       if section == "vitals" else "項目")
        questions[qid] = jev.noul_question(
            f"state.context の id={qid} の抽出{measurement}は、対象の投稿本文に裏付けられているか",
            f"本文にこの{measurement}と値を裏付ける記述がある",
            f"本文にこの{measurement}と値を裏付ける記述がない")
        layout.append((qid, section, index))
    urg = ex.get("urgency")
    if urg in ("high", "routine", "unclear"):
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
                    config_generation: str | None = None, *,
                    jev_transport=None, jev_timing=None, skip_reason=None, lane=None,
                    diagnostics=None) -> str:
    """One extract_qc job -> extract_qc artifact (annotate only).
    The job row transitions to 'done' inside the artifact commit tx —
    a claimed row must never be left re-claimable after its result
    landed. Returns 'done'|'deferred'|'retry'|'stale'|'failed'."""
    import extract_llm

    if (jev_client is None and skip_reason is None) or scfg.get("extract_qc") != "annotate":
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
    if not is_current(payload) or payload.get("source_artifact_id") != art[2] \
            or payload.get("hash") != art[1] \
            or payload.get("ver") != extract_llm.EXTRACT_VERSION:
        return done()  # the seed pass will queue the new extraction
    try:
        ex = json.loads(art[0] or "{}")
    except (ValueError, TypeError, RecursionError):
        return "failed"
    if not isinstance(ex, dict):
        return done()
    try:
        questions, layout, ctx_items = _qc_questions(ex)
    except (TypeError, ValueError, RecursionError):
        return "failed"
    state = {"target": {"id": f"m{mid}", "role": "target",
                        "text": msg["body_text"]},
             "context": [{"id": qid, "role": "extracted_item",
                          "text": text}
                         for qid, text in ctx_items.items()]}

    def guard(stage):
        if skip_reason is not None:
            from semantic_store import jev_usage_today
            try:
                runtime._jev_window_check(config_path=cfg_path)
                used = jev_usage_today(ledger)
                cap = scfg["daily_request_budget"]
                if lane == "backlog":
                    from semantic_policy import BACKLOG_BUDGET_RESERVE
                    cap -= math.ceil(cap * BACKLOG_BUDGET_RESERVE)
                if ((skip_reason in ("daily_budget", "backlog_reserved") and used < cap)
                        or (skip_reason == "disabled" and scfg["daily_request_budget"] > 0)
                        or (skip_reason == "not_configured" and jev.api_key())
                        or (skip_reason == "circuit_open" and (not runtime.circuit_open(ledger)
                            or runtime._circuit_state(ledger)["failure_class"]
                               not in runtime._CIRCUIT_FAILURE_CLASSES))):
                    raise ValueError("semantic_skip_reason_stale")
            except (jev.JevError, ValueError):
                raise runtime.RuntimeOff(stage + ':jev_unverified') from None
        def current_source():
            row = ledger.db.execute(
                f"SELECT content_hash,{qc_source_id(version=extract_llm.EXTRACT_VERSION)} "
                "FROM messages m WHERE message_id=?", (mid,)).fetchone()
            return f"{row[0]}:{row[1]}" if row else None
        runtime.guard(
            ledger, token, deadline=deadline,
            expected_config_generation=config_generation,
            expected_mode=scfg["mode"], cfg_path=cfg_path,
            load_cfg=load_config, parse_cfg=runtime_config,
            source_fingerprint=f"{art[1]}:{art[2]}", current_source=current_source,
            stage=stage)

    def write(content, qc, extra=None):
        # stale QC rows for a superseded hash are replaced in the same tx;
        # the job transition lands here too so the row is never left
        # claimable after its result
        with ledger.db:
            guard('qc:write')
            _drop_stale_qc(ledger, mid, art[1])
            ledger.artifact_add_tx(
                QC_ARTIFACT, json.dumps(content, ensure_ascii=False),
                project_id=pid, message_id=mid, model=jev.JEV_MODEL,
                meta={VERSION_KEY: CURRENT_VERSION, "hash": art[1],
                      "extract_version": extract_llm.EXTRACT_VERSION,
                      "source_artifact_id": art[2],
                      "qc": qc, **(extra or {})})
            if not runtime.transition_tx(ledger, token, "done"):
                raise runtime.RuntimeStale("qc:write")

    def write_skip(reason, partial):
        nonlocal skip_reason
        skip_reason = reason
        if skip_reason not in {"not_configured", "disabled", "daily_budget", "backlog_reserved", "circuit_open", "service_unavailable"}:
            return "deferred"
        if not isinstance(partial, dict) or set(partial) - set(questions):
            return "failed"
        reduced = {}
        for qid, answer in partial.items():
            if not isinstance(answer, dict):
                return "failed"
            question = questions[qid]
            if question.get("type") == "noul":
                value = answer.get("noul")
                if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                    return "failed"
                reduced[qid] = {"type": "noul", "noul": value}
            else:
                choice, confidence = answer.get("choice"), answer.get("confidence")
                if (not isinstance(choice, str) or choice not in question.get("criteria", {})
                        or type(confidence) not in (int, float)
                        or not math.isfinite(confidence) or not 0 <= confidence <= 1):
                    return "failed"
                reduced[qid] = {"type": "choice", "choice": choice, "confidence": confidence}
        try:
            with ledger.db:
                guard('qc:skip')
                ledger.artifact_add_tx(
                    "extract_qc_skipped", json.dumps({"status": "SKIPPED", "reason": skip_reason,
                                                      **({"partial_answers": reduced} if reduced else {})}),
                    project_id=pid, message_id=mid,
                    meta={VERSION_KEY: CURRENT_VERSION, "hash": art[1],
                          "extract_version": extract_llm.EXTRACT_VERSION, "source_artifact_id": art[2]})
                if not runtime.transition_tx(ledger, token, "done"):
                    raise runtime.RuntimeStale('qc:skip')
        except runtime.RuntimeStale:
            return "stale"
        except (runtime.RuntimeOff, runtime.RuntimeBudget):
            return "deferred"
        if diagnostics is not None:
            diagnostics["jev_skip_reason"] = skip_reason
        return "done"

    if skip_reason is not None:
        return write_skip(skip_reason, {})

    unevaluated = None
    req0 = jev_client.requests_made
    client = runtime.bind_jev(jev_client, guard, reserve_fn, transport=jev_transport, timing=jev_timing,
                              config_path=cfg_path)
    try:
        out = (jev.evaluate_chunked(client, state, questions, deadline,
                                    scfg["max_questions_per_request"])
               if questions else {"answers": {}})
    except jev.JevError as error:
        with suppress(Exception):
            jev_client.last_error = error
        from semantic_audit import jev_skip_reason
        reason = jev_skip_reason(error)
        if reason is not None and hasattr(error, "partial_answers"):
            return write_skip(reason, error.partial_answers)
        cls = jev.failure_class(error)
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
        unevaluated = error.kind
    except runtime.RuntimeStale:
        return "stale"
    except (runtime.RuntimeOff, runtime.RuntimeBudget):
        return "deferred"
    if unevaluated is not None:
        # a failed CAS here propagates, as it always has for this path
        write({"qc": "unevaluated", "reason": unevaluated}, "unevaluated")
        return "done"
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
        write(content, "done", {"jev_requests": jev_client.requests_made - req0})
    except runtime.RuntimeStale:
        return "stale"
    except (runtime.RuntimeOff, runtime.RuntimeBudget):
        return "deferred"
    return "done"
