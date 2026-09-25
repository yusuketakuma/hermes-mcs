#!/usr/bin/env python3
"""Local snapshot queries and explicit human-approved inbox commands (JSON CLI)."""
import argparse
import base64
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every first-level subdir as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401

from ledger import LedgerReader
import mcs_requests as requests

UNKNOWN_TIME = -(2**63)
WARNINGS = ["history_completeness_unverified", "snapshot_may_lag_live_state"]
REASONS = frozenset({
    "schema_error", "network_error", "http_error", "session_expired", "no_token",
    "pages_exceeded", "thread_incomplete", "replies_missing", "body_incomplete",
    "download_failed", "download_too_large", "url_not_allowed", "db_write_failed",
    "deadline_exceeded", "fs_OSError", "fs_PermissionError", "fs_FileNotFoundError",
    "OperationalError", "IntegrityError", "DatabaseError",
})


def _reason(value):
    return value if value in REASONS else "not_recorded" if not value else "other_error"


class View:
    def __init__(self, path):
        self.reader = LedgerReader(str(path))
        self.db = self.reader.db
        try:
            self.db.execute("BEGIN")
            # reader declares the schema generations it understands —
            # v6 added patients.is_archived, v7 messages.notified_at
            # (both additive, read-compatible)
            if self.db.execute("PRAGMA user_version").fetchone()[0] \
                    not in (5, 6, 7):
                raise ValueError("snapshot_upgrade_required")
            if self.db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise ValueError("published_snapshot_required")
            row = self.db.execute("SELECT generation_id,generated_at FROM snapshot_meta WHERE singleton=1").fetchone()
            if row is None:
                raise ValueError("published_snapshot_required")
            self.meta = dict(row)
        except Exception:
            self.close()
            raise

    def close(self):
        self.reader.close()

    def _page(self, sql, params, columns, scope, limit, cursor):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("bad_limit")
        binding = requests.payload_hash([self.meta["generation_id"], scope])
        if cursor:
            try:
                if not isinstance(cursor, str) or len(cursor) > 2048:
                    raise ValueError
                decoded = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                key = decoded["key"]
                if decoded["binding"] != binding or not isinstance(key, list) \
                        or len(key) != len(columns) \
                        or any(type(k) is not int or not -(2**63) <= k < 2**63 for k in key):
                    raise ValueError
            except (ValueError, TypeError, KeyError, RecursionError):
                raise ValueError("cursor_scope_or_generation_changed") from None
            left = "(" + ",".join(columns) + ")" if len(columns) > 1 else columns[0]
            right = "(" + ",".join("?" for _ in columns) + ")" if len(columns) > 1 else "?"
            sql += f" AND {left} < {right}"
            params += key
        sql += " ORDER BY " + ",".join(c + " DESC" for c in columns) + " LIMIT ?"
        rows = [dict(r) for r in self.db.execute(sql, (*params, limit + 1))]
        more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = None
        if more:
            last = rows[-1]
            key = ([last["posted_at_ts"] if last["posted_at_ts"] is not None else UNKNOWN_TIME,
                    last["message_id"]] if len(columns) == 2 else [last["_key"]])
            next_cursor = base64.urlsafe_b64encode(requests.canonical({"binding": binding, "key": key})).decode()
        for row in rows:
            row.pop("_key", None)
        return {"items": rows, "next_cursor": next_cursor}

    def _message(self, pid, mid):
        if not requests.positive(mid):
            raise ValueError("bad_message_id")
        row = self.db.execute("""
          SELECT m.message_id,m.project_id,m.parent_id,m.sender_name,m.posted_at,
            m.posted_at_ts,m.body_text,m.body_state,m.content_hash,m.reply_count,
            m.first_seen,m.updated_seen,p.url AS source_url
          FROM messages m LEFT JOIN patients p ON p.project_id=m.project_id
          WHERE m.project_id=? AND m.message_id=?
        """, (pid, mid)).fetchone()
        if row is None:
            raise ValueError("message_not_found")
        return self._evidence(dict(row))

    def _evidence(self, row):
        pid, mid = row["project_id"], row["message_id"]
        # Only return the known project URL, never an upstream signed URL or guessed deep link.
        if row.get("source_url") != f"https://www.medical-care.net/projects/medical/{pid}":
            row["source_url"] = None
        row["stored_replies"] = self.db.execute(
            "SELECT count(*) FROM messages WHERE project_id=? AND parent_id=?", (pid, mid)).fetchone()[0]
        row["attachments"] = [dict(r) for r in self.db.execute("""
          SELECT state,count(*) AS count FROM attachments WHERE message_id=? GROUP BY state
        """, (mid,))]
        row["body_complete"] = row["body_state"] == "full"
        return row

    def _messages(self, kind, pid, limit, cursor, query, message_id, since, until):
        params = [pid]
        sql = """SELECT m.message_id,m.project_id,m.parent_id,m.sender_name,m.posted_at,
          m.posted_at_ts,m.body_text,m.body_state,m.content_hash,m.reply_count,
          m.first_seen,m.updated_seen,p.url AS source_url
          FROM messages m LEFT JOIN patients p ON p.project_id=m.project_id WHERE m.project_id=?"""
        if kind == "timeline":
            sql += " AND m.parent_id IS NULL"
        if kind == "thread":
            self._message(pid, message_id)
            sql += " AND m.parent_id=?"
            params.append(message_id)
        if kind == "search":
            if not isinstance(query, str) or not query.strip() or len(query) > 500:
                raise ValueError("bad_query")
            for term in query.split():
                sql += " AND (instr(lower(replace(replace(m.body_text,' ',''),'　','')),lower(?))>0" \
                       " OR instr(lower(replace(replace(m.sender_name,' ',''),'　','')),lower(?))>0)"
                params += [term, term]
        for value, operator in ((since, ">="), (until, "<=")):
            if value is not None:
                if type(value) is not int or not 0 <= value < 2**63:
                    raise ValueError("bad_time_range")
                sql += f" AND m.posted_at_ts {operator} ?"
                params.append(value)
        if since is not None and until is not None and since > until:
            raise ValueError("bad_time_range")
        page = self._page(sql, params, [f"COALESCE(m.posted_at_ts,{UNKNOWN_TIME})", "m.message_id"],
                          [kind, pid, query, message_id, since, until], limit, cursor)
        page["scope"] = "roots_only" if kind == "timeline" else "stored_messages"
        for row in page["items"]:
            self._evidence(row)
            if kind == "candidates":
                row["candidates"] = requests.candidates(self.db, row)
        return page

    def _status(self, pid, limit, cursor):
        sql = """SELECT project_id AS _key,project_id,fetch_state,fetch_reason,
          last_complete_fetch AS last_successful_unread_fetch,last_seen,
          coverage_ts,history_floor,history_target,history_page FROM patients WHERE 1=1"""
        params = []
        if pid is not None:
            sql += " AND project_id=?"
            params.append(pid)
        page = self._page(sql, params, ["project_id"], ["status", pid], limit, cursor)
        for row in page["items"]:
            pid = row["project_id"]
            row["fetch_reason"] = _reason(row["fetch_reason"])
            from mcs_operations import paused
            row["semantic_paused"] = paused(self.db, pid)
            floor = row["history_floor"]
            row["history_record"] = "natural_end_recorded" if floor == -1 else \
                "cutoff_recorded" if floor and floor > 0 else "no_completion_record"
            row["gapless_verified"] = False
            row["exact_missing_ranges"] = None
            row["messages"] = dict(self.db.execute("""
              SELECT count(*) AS stored,min(posted_at_ts) AS earliest,max(posted_at_ts) AS latest,
                count(CASE WHEN body_state IS NOT 'full' THEN 1 END) AS incomplete_bodies,
                min(CASE WHEN body_state IS NOT 'full' THEN posted_at_ts END) AS incomplete_body_first,
                max(CASE WHEN body_state IS NOT 'full' THEN posted_at_ts END) AS incomplete_body_last
              FROM messages WHERE project_id=?
            """, (pid,)).fetchone())
            row["incomplete_reply_roots"] = self.db.execute("""
              SELECT count(*) FROM messages m WHERE m.project_id=? AND m.parent_id IS NULL
                AND m.reply_count > (SELECT count(*) FROM messages r
                  WHERE r.project_id=m.project_id AND r.parent_id=m.message_id AND r.body_state='full')
            """, (pid,)).fetchone()[0]
            row["jobs"] = [dict(r) for r in self.db.execute("""
              SELECT kind,state,count(*) AS count,max(attempts) AS max_attempts,min(next_try) AS next_try
              FROM fetch_jobs WHERE project_id=? GROUP BY kind,state
            """, (pid,))]
            for job in row["jobs"]:
                job["reason"] = "not_recorded"
            row["attachment_states"] = [dict(r) for r in self.db.execute("""
              SELECT a.state,count(*) AS count FROM attachments a JOIN messages m USING(message_id)
              WHERE m.project_id=? GROUP BY a.state
            """, (pid,))]
            reasons = {}
            for reason, count in self.db.execute("""
              SELECT a.error,count(*) FROM attachments a JOIN messages m USING(message_id)
              WHERE m.project_id=? AND a.state!='downloaded' GROUP BY a.error
            """, (pid,)):
                code = _reason(reason)
                reasons[code] = reasons.get(code, 0) + count
            row["attachment_failure_reasons"] = reasons
            row["history_jobs"] = []
            for job in self.db.execute("SELECT payload,state FROM fetch_jobs WHERE project_id=? AND kind='history'", (pid,)):
                try:
                    payload = json.loads(job["payload"])
                except (ValueError, TypeError):
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                row["history_jobs"].append({"state": job["state"], **{
                    key: payload.get(key) if type(payload.get(key)) is int else None
                    for key in ("since", "page", "pages")}})
        return page

    def _operations(self, pid, limit, cursor):
        from mcs_operations import paused
        from semantic_runtime import attempt_limit
        page = self._page(
            "SELECT j.job_id AS _key,j.job_id,j.kind,j.state,j.attempts,j.next_try,j.payload "
            "FROM fetch_jobs j WHERE j.project_id=? AND j.kind IN ('semantic','history')",
            [pid], ["j.job_id"], ["operations", pid], limit, cursor)
        for row in page["items"]:
            raw = row.pop("payload")
            try:
                payload = json.loads(raw or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("bad_payload")
                row["payload_hash"] = requests.payload_hash(payload)
                if row["kind"] == "semantic":
                    row["attempt_limit"] = attempt_limit(payload)
            except (ValueError, TypeError, UnicodeError, RecursionError):
                row["payload_hash"] = None
            row["payload_valid"] = row["payload_hash"] is not None
        return {**page, "semantic_paused": paused(self.db, pid)}

    def _qc(self, pid, mid, limit, cursor):
        """extract_qc annotation view — Jev quality control over v2
        extractions. Verdicts are annotations only: they never modified
        the extraction they describe. Without --message-id: aggregate
        counts plus a paginated list of messages whose QC flagged at
        least one item or could not run. With --message-id: the full
        per-item verdicts for that message."""
        import extract_llm
        from mcs_queries import current_qc_pred, qc_source_id
        from semantic_qc import QC_REALTIME_MAX_AGE_S, qc_scope_sql
        version = extract_llm.EXTRACT_VERSION
        if mid is not None:
            msg = self._message(pid, mid)
            source = self.db.execute(
                f"SELECT {qc_source_id(version=version)} FROM messages m "
                "WHERE m.project_id=? AND m.message_id=?", (pid, mid)).fetchone()
            source_id = source[0] if source else None
            rows = []
            for r in self.db.execute(
                    "SELECT artifact_id,content,meta,model,created_at "
                    "FROM artifacts WHERE kind='extract_qc' AND "
                    "message_id=? AND project_id=? "
                    "ORDER BY artifact_id DESC LIMIT 10", (mid, pid)):
                try:
                    meta = json.loads(r["meta"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                if not isinstance(meta, dict):
                    meta = {}
                try:
                    content = json.loads(r["content"])
                except (json.JSONDecodeError, TypeError):
                    content = None
                rows.append({"artifact_id": r["artifact_id"],
                             "model": r["model"], "meta": meta,
                             "content": content,
                             "current": source_id is not None
                                 and meta.get("hash") == msg["content_hash"]
                                 and meta.get("extract_version") == version
                                 and meta.get("source_artifact_id") == source_id,
                             "created_at": r["created_at"]})
            return {"message": msg, "qc": rows}
        cur = ("a.kind='extract_qc' AND m.project_id=? "
               + current_qc_pred(version=version))
        base = ("FROM artifacts a JOIN messages m "
                "ON m.message_id=a.message_id WHERE " + cur)
        summary = dict(self.db.execute(
            "SELECT COUNT(*) total,"
            " COALESCE(SUM(json_extract(a.content,'$.qc')='done'),0)"
            "   evaluated,"
            " COALESCE(SUM(json_extract(a.content,'$.qc')='unevaluated'),0)"
            "   unevaluated,"
            " COALESCE(SUM(json_extract(a.content,'$.urgency.jev')"
            "    IS NOT NULL"
            "    AND json_extract(a.content,'$.urgency.jev') IS NOT"
            "        json_extract(a.content,'$.urgency.extracted')),0)"
            "   urgency_mismatch,"
            " COALESCE(SUM(json_extract(a.content,'$.coverage.unchecked')),0)"
            "   unchecked_items " + base, (pid,)).fetchone())
        for r in self.db.execute(
                "SELECT COALESCE(json_extract(je.value,'$.verdict'),'?') v,"
                " COUNT(*) c FROM artifacts a"
                " JOIN messages m ON m.message_id=a.message_id,"
                " json_each(a.content,'$.items') je WHERE " + cur +
                " GROUP BY v", (pid,)):
            summary.setdefault("verdicts", {})[r["v"]] = r["c"]
        summary.setdefault("verdicts", {})
        # The historical annotations stay visible after their post ages out;
        # pending counts only current extractions eligible for another audit.
        scope, scope_params = qc_scope_sql(time.time())
        scope_counts = self.db.execute(f"""
          WITH sources AS (
            SELECT m.message_id, {scope} AS eligible,
              EXISTS(SELECT 1 FROM artifacts q
                WHERE q.kind='extract_qc' AND q.message_id=a.message_id
                  {current_qc_pred('q', version=version)}) AS has_qc
            FROM artifacts a JOIN messages m ON m.message_id=a.message_id
            WHERE m.project_id=?
              AND a.artifact_id={qc_source_id(version=version)}
          )
          SELECT COALESCE(SUM(eligible),0) AS eligible,
            COALESCE(SUM(eligible AND NOT has_qc),0) AS pending,
            COALESCE(SUM(NOT eligible),0) AS out_of_scope
          FROM sources
        """, (*scope_params, pid)).fetchone()
        summary.update(dict(scope_counts))
        page = self._page(
            "SELECT a.artifact_id AS _key,a.artifact_id,a.message_id,"
            "m.posted_at_ts,m.sender_name,a.content,a.created_at " + base +
            " AND (json_extract(a.content,'$.qc')='unevaluated'"
            "  OR json_extract(a.content,'$.coverage.unchecked')>0"
            "  OR EXISTS(SELECT 1 FROM json_each(a.content,'$.items') je"
            "            WHERE json_extract(je.value,'$.verdict')"
            "                  IS NOT 'MATCH')"
            "  OR (json_extract(a.content,'$.urgency.jev') IS NOT NULL"
            "      AND json_extract(a.content,'$.urgency.jev')"
            "          IS NOT json_extract(a.content,'$.urgency.extracted')))",
            [pid], ["a.artifact_id"], ["qc", pid], limit, cursor)
        for row in page["items"]:
            try:
                content = json.loads(row.pop("content") or "{}")
            except (json.JSONDecodeError, TypeError):
                content = {}
            if not isinstance(content, dict):
                content = {}
            row["qc_state"] = content.get("qc")
            row["coverage"] = content.get("coverage")
            row["flagged_items"] = [
                it for it in (content.get("items") or [])
                if isinstance(it, dict) and it.get("verdict") != "MATCH"]
            urg = content.get("urgency")
            if isinstance(urg, dict) and urg.get("jev") is not None \
                    and urg.get("jev") != urg.get("extracted"):
                row["urgency_mismatch"] = urg
            if content.get("qc") == "unevaluated":
                row["unevaluated_reason"] = content.get("reason")
        window_days = QC_REALTIME_MAX_AGE_S // 86400
        legend = {
            "qc": "抽出チェック — 機械が拾い上げた各項目が本文に裏付け"
                  "られるかを外部の確認用AI（Jev）が判定した注記。"
                  "抽出結果自体は変更されない",
            "verdicts": {
                "MATCH": "本文に裏付けあり",
                "NO_MATCH": "本文に裏付けが見つからない"
                            "（その事実が存在しない、という意味ではない）",
                "UNDETERMINED": "本文だけでは判断できない"},
            "qc_state": {"done": "判定済み",
                         "unevaluated": "判定を実行できなかった"},
            "eligible": f"投稿時刻が直近{window_days}日以内の現行抽出件数（QC実施済みを含む）",
            "pending": "対象内の現行抽出に対するQC未実施件数（キュー済みを含む）",
            "out_of_scope": f"投稿時刻が{window_days}日より前または不明のため追加QC対象外の現行抽出件数。過去のQC注記は表示を維持する",
            "coverage": "検査済み・未検査の項目数。判定済みは全項目の確認を意味しない",
        }
        return {**page, "summary": summary, "legend": legend}

    def _requests(self, pid, request_id, status, limit, cursor):
        if status is not None and status not in requests.STATUSES:
            raise ValueError("bad_status")
        sql = """SELECT r.request_id AS _key,r.*,m.content_hash AS current_source_hash,
          CASE WHEN m.message_id IS NULL THEN 'source_missing'
            WHEN m.content_hash IS NOT r.source_hash THEN 'stale' ELSE 'unchanged' END AS source_state
          FROM requests r LEFT JOIN messages m ON m.message_id=r.source_message_id AND m.project_id=r.project_id
          WHERE r.project_id=?"""
        params = [pid]
        if request_id is not None:
            if not requests.positive(request_id):
                raise ValueError("bad_request_id")
            sql += " AND r.request_id=?"
            params.append(request_id)
        if status is not None:
            sql += " AND r.status=?"
            params.append(status)
        result = self._page(sql, params, ["r.request_id"], ["requests", pid, request_id, status], limit, cursor)
        for row in result["items"]:
            row["loop_links"] = self._loop_links(pid, request_id=row["request_id"])
        return result

    def _loop_links(self, pid, *, request_id=None, loop_id=None):
        # The request row is the authority; link artifacts contain no task state.
        rows = self.db.execute("""
          SELECT a.artifact_id,a.created_at,a.content,r.request_id,r.status,r.revision,r.title
          FROM artifacts a JOIN requests r ON r.project_id=a.project_id
            AND r.request_id=CASE WHEN json_valid(a.content)
              THEN json_extract(a.content,'$.request_id') END
          WHERE a.kind='request_loop_link' AND a.project_id=?
            AND (? IS NULL OR r.request_id=?)
            AND (? IS NULL OR CASE WHEN json_valid(a.content)
              THEN json_extract(a.content,'$.loop_artifact_id') END=?)
          ORDER BY a.artifact_id DESC
        """, (pid, request_id, request_id, loop_id, loop_id))
        links = []
        for row in rows:
            item = dict(row)
            item["link"] = json.loads(item.pop("content"))
            links.append(item)
        return links

    def _attachments(self, project, message_id, limit, cursor):
        self._message(project, message_id)
        result = self._page("""SELECT attachment_id AS _key,attachment_id,message_id,name,state,
              downloaded_at,created_at,bytes,sha256,error FROM attachments WHERE message_id=?""",
                            [message_id], ["attachment_id"], ["attachments", project, message_id], limit, cursor)
        for item in result["items"]:
            item["error"] = _reason(item["error"])
        return result

    def _semantic(self, pid, mid):
        """Phase-J artifacts for one message: bundle, proposition
        verdicts, verified fact candidates, audited summary, notify
        plan. Preserve history but label its validity against the source
        generation in this snapshot, including same-thread context."""
        from semantic import thread_bundle

        self._message(pid, mid)
        row = self.db.execute(
            "SELECT parent_id FROM messages WHERE message_id=?",
            (mid,)).fetchone()
        keys = [mid] + ([row["parent_id"]] if row and row["parent_id"]
                        else [])
        bundle = thread_bundle(self, pid, row["parent_id"] or mid)
        fingerprint = bundle["source_fingerprint"] if bundle else None
        policy_row = self.db.execute(
            "SELECT content FROM artifacts WHERE kind='semantic_policy' "
            "ORDER BY artifact_id DESC LIMIT 1").fetchone()
        policy = policy_row["content"] if policy_row else None
        out = {}
        for kind in ("semantic_bundle", "semantic_assess",
                     "semantic_facts", "semantic_summary",
                     "semantic_audit", "semantic_coverage", "notify_plan"):
            marks = ",".join("?" * len(keys))
            items = []
            for r in self.db.execute(
                    f"SELECT artifact_id,message_id,content,model,meta,"
                    f"created_at FROM artifacts WHERE kind=? AND "
                    f"message_id IN ({marks}) AND project_id=? "
                    f"ORDER BY artifact_id DESC LIMIT 5",
                    (kind, *keys, pid)):
                try:
                    meta = json.loads(r["meta"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                if not isinstance(meta, dict):
                    meta = {}
                try:
                    content = json.loads(r["content"])
                except (json.JSONDecodeError, TypeError):
                    content = None
                current = bool(fingerprint and meta.get("fingerprint") == fingerprint)
                if kind in ("semantic_assess", "semantic_summary", "semantic_audit", "semantic_coverage"):
                    current = current and bool(policy and meta.get("policy_fingerprint") == policy)
                effective = (meta.get("audit_status") or meta.get("technical_status")) \
                    if current else "STALE"
                items.append({"artifact_id": r["artifact_id"],
                              "message_id": r["message_id"],
                              "model": r["model"], "meta": meta,
                              "content": content,
                              "current": current, "effective_status": effective,
                              "created_at": r["created_at"]})
            out[kind] = items
        return {"semantic": out}

    def _comparison(self, pid, mid):
        from summary_review import comparison
        self._message(pid, mid)
        return comparison(self.db, pid, mid)

    def _staff(self, pid, limit):
        """Name -> facility directory for this room — pharmacy staff
        link by their pharmacy name in ``organization``."""
        from mcs_queries import staff_directory
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("bad_limit")
        rows = staff_directory(self.db, pid)
        return {"items": [dict(r) for r in rows[:limit]]}

    def _loops(self, pid, limit):
        """Open-Loop candidates + their relation events. Candidates are
        advisory only — promotion to a formal request goes through the
        human-confirmed request path, never automatic (INV-11)."""
        from semantic import thread_bundle
        from request_loops import valid_origin_evidence

        items = []
        policy_row = self.db.execute(
            "SELECT content FROM artifacts WHERE kind='semantic_policy' "
            "ORDER BY artifact_id DESC LIMIT 1").fetchone()
        policy = policy_row["content"] if policy_row else None
        bundles = {}
        for r in self.db.execute(
                "SELECT artifact_id,message_id,content,meta,created_at "
                "FROM artifacts WHERE kind='loop_candidate' "
                "AND project_id=? ORDER BY artifact_id DESC LIMIT ?",
                (pid, max(1, min(limit, 200)))):
            try:
                cand = json.loads(r["content"])
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(cand, dict):
                continue
            origin = cand.get("origin") or {}
            try:
                candidate_meta = json.loads(r["meta"] or "{}")
            except (json.JSONDecodeError, TypeError):
                candidate_meta = {}
            if not isinstance(candidate_meta, dict):
                candidate_meta = {}
            source = self.db.execute(
                "SELECT content_hash,COALESCE(parent_id,message_id) root "
                "FROM messages WHERE project_id=? AND message_id=?",
                (pid, r["message_id"])).fetchone()
            root = source["root"] if source else None
            if root not in bundles:
                bundles[root] = thread_bundle(self, pid, root) if root else None
            bundle = bundles[root]
            candidate_fp = candidate_meta.get(
                "source_fingerprint", candidate_meta.get("fingerprint"))
            current = bool(
                source and bundle
                and origin.get("revision") == source["content_hash"]
                and candidate_fp == bundle["source_fingerprint"]
                and policy
                and candidate_meta.get("policy_fingerprint") == policy)
            members = {m["message_id"]: m for m in bundle["members"]} if bundle else {}
            cand["adoption_eligible"] = current and valid_origin_evidence(
                origin, members.get(r["message_id"]))
            events = []                       # (created_at, event)
            for e in self.db.execute(
                    "SELECT content,meta,created_at FROM artifacts "
                    "WHERE kind='loop_event' AND project_id=? "
                    "AND CASE WHEN json_valid(content) THEN "
                    "json_extract(content,'$.loop_artifact_id')=? OR "
                    "(json_extract(content,'$.loop_artifact_id') IS NULL AND "
                    "json_extract(content,'$.loop_origin_id')=?) ELSE 0 END "
                    "ORDER BY artifact_id DESC LIMIT 50",
                    (pid, r["artifact_id"], r["message_id"])):
                try:
                    ev = json.loads(e["content"])
                    meta = json.loads(e["meta"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(ev, dict):
                    continue
                # 'unrelated' evaluations are recorded for dedup but are
                # not user-facing relations — hide them
                if ev.get("relation") == "unrelated":
                    continue
                trigger = members.get(ev.get("trigger_message_id"))
                ev["stale"] = not (
                    current and bundle and trigger and isinstance(meta, dict)
                    and meta.get("source_fingerprint", meta.get("fingerprint"))
                    == bundle["source_fingerprint"]
                    and policy
                    and meta.get("policy_fingerprint") == policy
                    and ev.get("origin_revision") == origin.get("revision")
                    and ev.get("trigger_revision") == trigger["revision"])
                if ev.get("loop_artifact_id") == r["artifact_id"] or (
                        ev.get("loop_artifact_id") is None
                        and ev.get("loop_origin_id") == r["message_id"]):
                    events.append((e["created_at"], ev))
            cand["relation_events"] = [ev for _, ev in events[:5]]
            # a completion/cancellation report relation promotes the
            # candidate to RESOLUTION_CANDIDATE for display — the stored
            # artifact stays immutable (§17.3). History is derived the
            # same way: the stored record only logs its creation entry,
            # later transitions live in relation events.
            history = list(cand.get("history") or [])
            for ts, e in reversed(events):
                if not e["stale"] and e.get("relation") in ("completion_report",
                                         "cancellation_report"):
                    history.append({
                        "state": "RESOLUTION_CANDIDATE", "at": ts,
                        "trigger_message_id":
                            e.get("trigger_message_id"),
                        "relation": e.get("relation")})
            cand["history"] = history
            cand["current"] = current
            cand["effective_state"] = "STALE" if not current else (
                "RESOLUTION_CANDIDATE"
                if any(not e["stale"] and e.get("relation") in ("completion_report",
                                             "cancellation_report")
                       for _, e in events)
                else cand.get("state"))
            cand["artifact_id"] = r["artifact_id"]
            cand["linked_requests"] = self._loop_links(pid, loop_id=r["artifact_id"])
            items.append(cand)
        return {"items": items, "scope": "loop_candidates"}

    def _receipt(self, project, command_id, payload_hash):
        if not requests.valid_uuid(command_id) or not requests.valid_hash(payload_hash):
            raise ValueError("bad_receipt_identity")
        identity = self.db.execute("SELECT payload_hash FROM command_receipts WHERE command_id=?",
                                   (command_id,)).fetchone()
        if identity is not None and identity["payload_hash"] != payload_hash:
            return {"outcome": "rejected", "error": "command_id_conflict"}
        row = self.db.execute("SELECT * FROM command_receipts WHERE command_id=? AND project_id=?",
                              (command_id, project)).fetchone()
        if row is None:
            return {"outcome": "not_processed_or_not_in_snapshot"}
        return json.loads(row["receipt_json"])

    def notification_receipt(self, command_id, payload_hash=None,
                             context=None):
        """Receipts for the interactive-notification channel — separate
        from _receipt because notification command_ids are composite
        ('<token>:<actor_hash>') and digest/notice rows legitimately
        carry project_id NULL. Snapshot lag is reported as
        not_processed, never as failure. context=None is the local
        operator CLI; a plugin context {actor, application_id,
        channel_id, projects, operator} narrows what may be seen."""
        if not isinstance(command_id, str) \
                or not (0 < len(command_id) <= 200):
            raise ValueError("bad_receipt_identity")
        if payload_hash is not None \
                and not requests.valid_hash(payload_hash):
            raise ValueError("bad_receipt_identity")
        row = self.db.execute(
            "SELECT payload_hash,receipt_json FROM command_receipts "
            "WHERE command_id=?", (command_id,)).fetchone()
        if row is None:
            return {"outcome": "not_processed_or_not_in_snapshot",
                    "snapshot_generated_at": self.meta["generated_at"]}
        if payload_hash is not None \
                and row["payload_hash"] != payload_hash:
            return {"outcome": "rejected", "error": "command_id_conflict"}
        try:
            receipt = json.loads(row["receipt_json"])
        except (json.JSONDecodeError, TypeError):
            receipt = None
        if not isinstance(receipt, dict):
            return {"outcome": "rejected", "error": "receipt_corrupt"}
        if context is None:
            return receipt
        return self._scope_notification_receipt(receipt, context)

    @staticmethod
    def _scope_notification_receipt(receipt: dict, context: dict) -> dict:
        """Plugin-side scope enforcement: a viewer sees only receipts
        for their own actor inside their own channel/application, and
        only projects they are authorized for; operator-only results
        (card_resolve) are withheld entirely."""
        if receipt.get("kind") not in ("notification", "refresh",
                                       "ops.card_resolve"):
            # Ordinary request receipts carry full before/after records
            # and use project_id, not the card channel's projects list.
            # They must stay behind the project-scoped receipt reader.
            return {"outcome": "rejected", "error": "receipt_kind_mismatch"}
        if receipt.get("kind") == "ops.card_resolve" \
                and not context.get("operator"):
            return {"outcome": "rejected", "error": "operator_only"}
        actor = receipt.get("actor")
        if actor is not None and actor != context.get("actor"):
            return {"outcome": "rejected", "error": "actor_mismatch"}
        scope = receipt.get("origin") or receipt.get("scope") or {}
        for key in ("application_id", "channel_id", "guild_id",
                    "profile"):
            if scope.get(key) and scope[key] != context.get(key):
                return {"outcome": "rejected", "error": "scope_mismatch"}
        allowed = set(context.get("projects") or [])
        want = set(receipt.get("projects") or [])
        if receipt.get("project_id") is not None:
            want.add(receipt["project_id"])
        if want and not want <= allowed:
            return {"outcome": "rejected",
                    "error": "project_scope_mismatch"}
        return receipt

    def stats(self, args: dict) -> dict:
        """Cross-project statistics — same snapshot generation, read-only
        (MCS-STAT-PROSPECTIVE §A-3: entry lives in mcs_view, the math in
        mcs_stats; no own connections, no writes)."""
        import mcs_stats
        result = mcs_stats.run_stats(self.db, self.meta["generated_at"], args)
        return {"snapshot": self.meta,
                "snapshot_age_s": max(0, time.time() - self.meta["generated_at"]),
                "warnings": WARNINGS,
                "query": {k: args.get(k) for k in
                          ("stat", "preset", "list", "since", "until",
                           "as_of", "project", "limit")},
                **result}

    def signals(self, args: dict) -> dict:
        """Open review-candidate signals (signal_v1 artifacts) — ids and
        extracted evidence only; candidates are prompts for human review
        of the source records, never proof of missed work."""
        import mcs_signals
        try:
            limit = min(max(int(args.get("limit") or 50), 1), 200)
        except (TypeError, ValueError):
            raise ValueError("bad_limit")
        result = mcs_signals.current_open(
            self.db, project_id=args.get("project"), limit=limit)
        return {"snapshot": self.meta,
                "snapshot_age_s": max(0, time.time() - self.meta["generated_at"]),
                "warnings": WARNINGS,
                "candidates": result,
                "note": "候補は原記録の人による確認を求める提示です。"
                        "記録の欠如は対応の欠如を意味しません。"
                        "検出対象は本文取得済みかつ抽出済みの記録に限り"
                        "ます — 未抽出・未取得の記録は候補に現れません。"
                        "pipeline_last_run_at が古い場合は未評価です"}

    def read(self, kind, project=None, limit=50, cursor=None, query=None,
             message_id=None, request_id=None, status=None, command_id=None,
             payload_hash=None, since=None, until=None):
        if (project is not None and not requests.positive(project)) or (kind != "status" and project is None):
            raise ValueError("project_required")
        handlers = {
            **dict.fromkeys(("search", "timeline", "thread", "candidates"),
                            lambda: self._messages(kind, project, limit, cursor, query, message_id, since, until)),
            "status": lambda: self._status(project, limit, cursor),
            "evidence": lambda: {"message": self._message(project, message_id)},
            "attachments": lambda: self._attachments(project, message_id, limit, cursor),
            "requests": lambda: self._requests(project, request_id, status, limit, cursor),
            "staff": lambda: self._staff(project, limit),
            "qc": lambda: self._qc(project, message_id, limit, cursor),
            "receipt": lambda: self._receipt(project, command_id, payload_hash),
            "semantic": lambda: self._semantic(project, message_id),
            "comparison": lambda: self._comparison(project, message_id),
            "loops": lambda: self._loops(project, limit),
            "operations": lambda: self._operations(project, limit, cursor),
        }
        if kind not in handlers:
            raise ValueError("unknown_view")
        result = handlers[kind]()
        return {"snapshot": self.meta, "snapshot_age_s": max(0, time.time() - self.meta["generated_at"]),
                "warnings": WARNINGS, "project_id": project,
                "query": {"kind": kind, "text": query, "since": since, "until": until,
                          "message_id": message_id, "request_id": request_id, "status": status}, **result}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, '{"ok":false,"error":"bad_arguments"}\n')


def _parser():
    parser = _Parser(description=__doc__)
    parser.add_argument("--snapshot", type=Path,
                        default=Path.home() / ".mcs/data/snapshots/ledger-snapshot.db")
    parser.add_argument("--cmd-dir", type=Path, default=Path.home() / ".mcs/data/cmd")
    subs = parser.add_subparsers(dest="kind", required=True)
    for kind in ("status", "search", "timeline", "evidence", "thread", "attachments", "candidates", "receipt", "notification_receipt", "requests", "staff", "qc", "semantic", "comparison", "loops", "operations", "control", "stats", "signals"):
        sub = subs.add_parser(kind)
        if kind == "control":
            actions = sub.add_subparsers(dest="action", required=True)
            parsers = [actions.add_parser(action) for action in ("scan", "retry", "pause", "resume", "adopt_summary", "signal_dismiss", "signal_policy", "refstat_approve", "card_resolve")]
            for write in parsers:
                write.add_argument("--confirm-human", action="store_true", required=True,
                                   help="Queue an exact human-approved operation from JSON stdin.")
        elif kind == "requests":
            actions = sub.add_subparsers(dest="action", required=True)
            parsers = [actions.add_parser(action) for action in ("list", "show", "create", "update")]
            parsers[0].add_argument("--status", choices=requests.STATUSES)
            parsers[1].add_argument("--request-id", type=int, required=True)
            for write in parsers[2:]:
                write.add_argument("--confirm-human", action="store_true", required=True,
                                   help="Only after a human explicitly approved this exact change. Read JSON from stdin.")
        else:
            parsers = [sub]
        for index, child in enumerate(parsers):
            if kind == "stats":
                break  # stats carries its own arg set below
            # card_resolve derives its project set from the stored
            # render/coverage — a --project input would be meaningless
            # and wrong; notification_receipt keys on command_id only
            projectless = (kind in ("status", "signals",
                                    "notification_receipt")
                           or (kind == "control" and index == len(parsers) - 1))
            child.add_argument("--project", type=int,
                               required=not projectless)
            if kind not in ("evidence", "receipt", "notification_receipt",
                            "control") and (kind != "requests" or index == 0):
                child.add_argument("--limit", type=int, default=50)
                if kind != "signals":
                    child.add_argument("--cursor")
        if kind == "search":
            sub.add_argument("--query", required=True)
        if kind == "stats":
            group = sub.add_mutually_exclusive_group(required=True)
            group.add_argument("--stat")
            group.add_argument("--preset")
            group.add_argument("--list", action="store_true")
            # dates: YYYY-MM-DD = JST midnight of that day; the scope is
            # half-open [since, until) — pass the next day for a whole day;
            # datetimes must carry an explicit offset (A-6)
            sub.add_argument("--since")
            sub.add_argument("--until")
            sub.add_argument("--as-of", dest="as_of")
            sub.add_argument("--project", type=int)
            sub.add_argument("--limit", type=int, default=20)
        if kind in ("search", "timeline", "thread", "candidates"):
            sub.add_argument("--since", type=int, help="Inclusive epoch seconds; excludes unknown posting times")
            sub.add_argument("--until", type=int, help="Inclusive epoch seconds")
        if kind in ("evidence", "thread", "attachments", "semantic", "comparison"):
            sub.add_argument("--message-id", type=int, required=True)
        if kind == "qc":
            sub.add_argument("--message-id", type=int)
        if kind == "receipt":
            sub.add_argument("--command-id", required=True)
            sub.add_argument("--payload-hash", required=True)
        if kind == "notification_receipt":
            sub.add_argument("--command-id", required=True)
            sub.add_argument("--payload-hash")
    return parser


def main(argv=None):
    args = vars(_parser().parse_args(argv))
    snapshot, cmd_dir = args.pop("snapshot"), args.pop("cmd_dir")
    action = args.pop("action", None)
    confirmed = args.pop("confirm_human", False)
    view = None
    try:
        view = View(snapshot)  # Gate commands on an upgraded, published snapshot; never open the live writer DB.
        if action in ("create", "update") or args["kind"] == "control":
            req = requests.parse_command(sys.stdin.buffer.read(requests.MAX_COMMAND_BYTES + 1))
            if not isinstance(req, dict) or req.keys() & {"cmd", "version", "project_id", "human_confirmed"}:
                raise ValueError("bad_command_envelope")
            command = ("ops." if args["kind"] == "control" else "request.") + action
            if command.startswith("request.") and "reason" not in req:
                raise ValueError("reason_required")
            req = {**req, "cmd": command, "version": 1,
                   "human_confirmed": confirmed}
            if action != "card_resolve":
                req["project_id"] = args["project"]
            result = requests.enqueue(req, cmd_dir)
        elif args["kind"] == "notification_receipt":
            result = {"snapshot": view.meta,
                      "snapshot_age_s": max(
                          0, time.time() - view.meta["generated_at"]),
                      **view.notification_receipt(
                          args["command_id"], args["payload_hash"])}
        elif args["kind"] == "stats":
            result = view.stats(args)
        elif args["kind"] == "signals":
            result = view.signals(args)
        else:
            result = view.read(**args)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 1 if result.get("outcome") == "unknown" else 0
    except (ValueError, OSError, sqlite3.Error, RecursionError) as error:
        # User text/SQL/paths must never be interpolated into error logs.
        code = str(error) if type(error) is ValueError and str(error).replace("_", "").isalnum() else type(error).__name__
        print(json.dumps({"ok": False, "error": code}))
        return 1
    finally:
        if view is not None:
            view.close()


if __name__ == "__main__":
    sys.exit(main())
