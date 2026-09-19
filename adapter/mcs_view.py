#!/usr/bin/env python3
"""Local snapshot queries and explicit human-approved inbox commands (JSON CLI)."""
import argparse
import base64
import json
import sqlite3
import sys
import time
from pathlib import Path

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
            if self.db.execute("PRAGMA user_version").fetchone()[0] != 5:
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
        return self._page(sql, params, ["r.request_id"], ["requests", pid, request_id, status], limit, cursor)

    def _attachments(self, project, message_id, limit, cursor):
        self._message(project, message_id)
        result = self._page("""SELECT attachment_id AS _key,attachment_id,message_id,name,state,
              downloaded_at,created_at,bytes,sha256,error FROM attachments WHERE message_id=?""",
                            [message_id], ["attachment_id"], ["attachments", project, message_id], limit, cursor)
        for item in result["items"]:
            item["error"] = _reason(item["error"])
        return result

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
            "receipt": lambda: self._receipt(project, command_id, payload_hash),
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
    for kind in ("status", "search", "timeline", "evidence", "thread", "attachments", "candidates", "receipt", "requests"):
        sub = subs.add_parser(kind)
        if kind == "requests":
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
            child.add_argument("--project", type=int, required=kind != "status")
            if kind not in ("evidence", "receipt") and (kind != "requests" or index == 0):
                child.add_argument("--limit", type=int, default=50)
                child.add_argument("--cursor")
        if kind == "search":
            sub.add_argument("--query", required=True)
        if kind in ("search", "timeline", "thread", "candidates"):
            sub.add_argument("--since", type=int, help="Inclusive epoch seconds; excludes unknown posting times")
            sub.add_argument("--until", type=int, help="Inclusive epoch seconds")
        if kind in ("evidence", "thread", "attachments"):
            sub.add_argument("--message-id", type=int, required=True)
        if kind == "receipt":
            sub.add_argument("--command-id", required=True)
            sub.add_argument("--payload-hash", required=True)
    return parser


def main(argv=None):
    args = vars(_parser().parse_args(argv))
    snapshot, cmd_dir = args.pop("snapshot"), args.pop("cmd_dir")
    action = args.pop("action", None)
    confirmed = args.pop("confirm_human", False)
    view = None
    try:
        view = View(snapshot)  # Gate commands on an upgraded, published snapshot; never open the live writer DB.
        if action in ("create", "update"):
            req = requests.parse_command(sys.stdin.buffer.read(requests.MAX_COMMAND_BYTES + 1))
            if not isinstance(req, dict) or req.keys() & {"cmd", "version", "project_id", "human_confirmed"}:
                raise ValueError("bad_command_envelope")
            req = {**req, "cmd": "request." + action, "version": 1,
                   "project_id": args["project"], "human_confirmed": confirmed}
            result = requests.enqueue(req, cmd_dir)
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
