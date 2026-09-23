"""Human-confirmed local requests; no network or automatic task creation."""
import hashlib
import json
import os
import re
import stat
import tempfile
import time
import uuid
from datetime import date
from pathlib import Path

MAX_COMMAND_BYTES = 16384
STATUSES = ("open", "in_progress", "done", "cancelled")
SCHEMA = """
CREATE TABLE IF NOT EXISTS requests(
  request_id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL, source_message_id INTEGER NOT NULL,
  source_hash TEXT NOT NULL, title TEXT NOT NULL,
  assignee TEXT, due_date TEXT,
  status TEXT NOT NULL CHECK(status IN ('open','in_progress','done','cancelled')),
  revision INTEGER NOT NULL CHECK(revision > 0),
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_requests_project ON requests(project_id,request_id);
CREATE TABLE IF NOT EXISTS command_receipts(
  command_id TEXT PRIMARY KEY NOT NULL, payload_hash TEXT NOT NULL,
  project_id INTEGER, request_id INTEGER,
  outcome TEXT NOT NULL CHECK(outcome IN ('applied','rejected')),
  receipt_json TEXT NOT NULL, processed_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_receipts_request ON command_receipts(request_id);
CREATE TABLE IF NOT EXISTS snapshot_meta(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  generation_id TEXT NOT NULL, generated_at REAL NOT NULL);
"""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def payload_hash(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def parse_command(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("invalid_number")

    if len(raw) > MAX_COMMAND_BYTES:
        raise ValueError("command_too_large")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def read_command(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("regular_command_file_required")
        return parse_command(stream.read(MAX_COMMAND_BYTES + 1))


def positive(value):
    return type(value) is int and 0 < value < 2**63


def valid_hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def valid_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _text(value, size, nullable=False):
    return (nullable and value is None) or (
        isinstance(value, str) and 0 < len(value) <= size
        and bool(value.strip()) and "\x00" not in value)


def _fields(fields):
    if not isinstance(fields, dict) or not fields or fields.keys() - {
            "title", "assignee", "due_date", "status"}:
        return "bad_patch"
    if "title" in fields and not _text(fields["title"], 1000):
        return "bad_title"
    if "assignee" in fields and not _text(fields["assignee"], 120, True):
        return "bad_assignee"
    if "status" in fields and fields["status"] not in STATUSES:
        return "bad_status"
    due = fields.get("due_date")
    if due is not None:
        try:
            if not isinstance(due, str) or date.fromisoformat(due).isoformat() != due:
                return "bad_due_date"
        except ValueError:
            return "bad_due_date"
    return None


def _request_extras(req):
    if "reason" in req and not _text(req["reason"], 2000):
        return "bad_reason"
    if "loop_ref" not in req:
        return None
    from request_loops import validate_loop_ref
    error = validate_loop_ref(req["loop_ref"])
    if error:
        return error
    if "reason" not in req:
        return "loop_reason_required"
    return None


def validate(req):
    if not isinstance(req, dict):
        return "bad_command"
    common = {"version", "cmd", "command_id", "actor", "human_confirmed", "project_id"}
    if type(req.get("version")) is not int or req["version"] != 1:
        return "bad_version"
    if not valid_uuid(req.get("command_id")):
        return "bad_command_id"
    if req.get("human_confirmed") is not True:
        return "human_confirmation_required"
    if not _text(req.get("actor"), 120):
        return "bad_actor"
    if not positive(req.get("project_id")):
        return "bad_project_id"
    if isinstance(req.get("cmd"), str) and req["cmd"].startswith("ops."):
        # Keep the identity boundary in this module; operation payload rules
        # must not be able to bypass the shared human-confirmed envelope.
        from mcs_operations import validate_ops
        return validate_ops(req, common)
    if req.get("cmd") == "request.create":
        allowed = common | {"source_message_id", "source_hash", "title", "assignee", "due_date", "reason", "loop_ref"}
        if req.keys() - allowed:
            return "unknown_field"
        if not positive(req.get("source_message_id")):
            return "bad_source_message_id"
        if not valid_hash(req.get("source_hash")):
            return "bad_source_hash"
        extra_error = _request_extras(req)
        if extra_error:
            return extra_error
        return _fields({k: req[k] for k in ("title", "assignee", "due_date") if k in req}) \
            if "title" in req else "bad_title"
    if req.get("cmd") == "request.update":
        if req.keys() - (common | {"request_id", "expected_revision", "expected_source_hash", "patch", "reason", "loop_ref"}):
            return "unknown_field"
        if not positive(req.get("request_id")) or not positive(req.get("expected_revision")):
            return "bad_revision_or_id"
        if not valid_hash(req.get("expected_source_hash")):
            return "bad_source_hash"
        extra_error = _request_extras(req)
        if extra_error:
            return extra_error
        return _fields(req.get("patch"))
    return "unknown_cmd"


def apply_command(ledger, req):
    """Receipt-first transaction; exceptions leave the queue input retryable."""
    if not isinstance(req, dict) or not valid_uuid(req.get("command_id")):
        raise ValueError("bad_command_id")
    digest = payload_hash(req)
    db = ledger.db
    # requests/command_receipts landed in schema v5; newer versions still
    # carry them, and Ledger.__init__ already refuses schemas NEWER than
    # the code — a floor check is the right contract here
    if db.execute("PRAGMA user_version").fetchone()[0] < 5:
        raise RuntimeError("request_schema_not_ready")
    with db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT payload_hash,receipt_json FROM command_receipts WHERE command_id=?",
                         (req["command_id"],)).fetchone()
        if old:
            if old["payload_hash"] != digest:
                return {"outcome": "rejected", "error": "command_id_conflict"}
            return json.loads(old["receipt_json"])
        error = validate(req)
        before = after = None
        pid = req.get("project_id") if positive(req.get("project_id")) else None
        rid = None
        now = time.time()
        extra = {}
        loop_candidate = None
        receipt_loop_ref = None
        if req.get("cmd") in ("request.create", "request.update") \
                and isinstance(req.get("loop_ref"), dict):
            from request_loops import validate_loop_ref
            if validate_loop_ref(req["loop_ref"]) is None:
                receipt_loop_ref = req["loop_ref"]
        if not error and isinstance(req.get("cmd"), str) \
                and req["cmd"].startswith("ops."):
            from mcs_operations import apply_tx
            error, extra = apply_tx(db, req, now=now)
        elif not error:
            if req["cmd"] == "request.update":
                row = db.execute("SELECT * FROM requests WHERE request_id=? AND project_id=?",
                                 (req["request_id"], pid)).fetchone()
                before = dict(row) if row else None
                if before is None:
                    error = "request_not_found"
                elif before["revision"] != req["expected_revision"]:
                    error = "revision_conflict"
            if not error:
                mid = req["source_message_id"] if req["cmd"] == "request.create" \
                    else before["source_message_id"]
                source = db.execute("SELECT content_hash,body_state FROM messages WHERE message_id=? AND project_id=?",
                                    (mid, pid)).fetchone()
                expected = req.get("source_hash", req.get("expected_source_hash"))
                if source is None:
                    error = "source_missing"
                elif source["body_state"] != "full":
                    error = "source_incomplete"
                elif source["content_hash"] != expected:
                    error = "source_changed"
                if not error and "loop_ref" in req:
                    from request_loops import current_candidate
                    try:
                        loop_candidate = current_candidate(
                            db, pid, req["loop_ref"]["artifact_id"], mid)
                    except ValueError as exc:
                        error = str(exc)
                    else:
                        ref = req["loop_ref"]
                        if (ref["artifact_id"] != loop_candidate["artifact_id"]
                                or ref["source_fingerprint"]
                                != loop_candidate["source_fingerprint"]
                                or ref["policy_fingerprint"]
                                != loop_candidate["policy_fingerprint"]):
                            error = "loop_ref_stale"
                if not error:
                    if req["cmd"] == "request.create":
                        rid = db.execute("""
                          INSERT INTO requests(project_id,source_message_id,source_hash,title,
                            assignee,due_date,status,revision,created_at,updated_at)
                          VALUES(?,?,?,?,?,?,'open',1,?,?)
                        """, (pid, mid, expected, req["title"], req.get("assignee"),
                              req.get("due_date"), now, now)).lastrowid
                    else:
                        rid = before["request_id"]
                        fields = req["patch"]
                        changed = db.execute(
                            "UPDATE requests SET " + ",".join(f"{k}=?" for k in fields)
                            + ",revision=revision+1,updated_at=? WHERE request_id=? AND revision=?",
                            (*fields.values(), now, rid, req["expected_revision"]))
                        if changed.rowcount != 1:
                            raise RuntimeError("request_revision_changed")
                    after = dict(db.execute("SELECT * FROM requests WHERE request_id=?", (rid,)).fetchone())
                    if loop_candidate is not None:
                        link = {
                            "request_id": rid,
                            "loop_artifact_id": loop_candidate["artifact_id"],
                            "source_fingerprint": loop_candidate["source_fingerprint"],
                            "policy_fingerprint": loop_candidate["policy_fingerprint"],
                            "command_id": req["command_id"],
                            "actor": req["actor"],
                            "reason": req["reason"],
                        }
                        db.execute(
                            "INSERT INTO artifacts(kind,project_id,message_id,"
                            "content,model,meta,created_at) VALUES(?,?,?,?,?,?,?)",
                            ("request_loop_link", pid, mid,
                             json.dumps(link, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":"), allow_nan=False),
                             "human", json.dumps({
                                 "command_id": req["command_id"],
                                 "actor": req["actor"]}, ensure_ascii=False,
                                 sort_keys=True, separators=(",", ":")), now),
                        )
        receipt = {"command_id": req["command_id"], "payload_hash": digest,
                   "project_id": pid, "request_id": rid,
                   "outcome": "rejected" if error else "applied", "error": error,
                   "actor": req.get("actor") if _text(req.get("actor"), 120) else None,
                   "reason": (req.get("reason")
                              if (req.get("cmd") in ("request.create", "request.update")
                                  and _text(req.get("reason"), 2000))
                              else None),
                   "loop_ref": receipt_loop_ref,
                   "revision": after["revision"] if after else None,
                   "before": before, "after": after, "processed_at": now}
        if extra:
            receipt.update(extra)
        db.execute("INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)",
                   (req["command_id"], digest, pid, rid, receipt["outcome"],
                    canonical(receipt).decode(), now))
    return receipt


def enqueue(req, cmd_dir):
    error = validate(req)
    if error:
        raise ValueError(error)
    raw = canonical(req)
    if len(raw) > MAX_COMMAND_BYTES:
        raise ValueError("command_too_large")
    # The inbox must already exist; never accidentally create a host-like path in a container.
    fd, temp = tempfile.mkstemp(prefix="request-", suffix=".tmp", dir=cmd_dir)
    receipt = {"command_id": req["command_id"], "payload_hash": payload_hash(req), "outcome": "queued"}
    published = False
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp, temp[:-4] + ".json")  # Atomic publication without clobbering pending input.
        published = True
        os.unlink(temp)
        directory = os.open(cmd_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        if not published:
            raise
        return {**receipt, "outcome": "unknown", "error": "queue_durability_unknown"}
    finally:
        try:
            Path(temp).unlink(missing_ok=True)
        except OSError:
            pass  # An ignored .tmp is safer than masking the primary result or deleting published input.
    return receipt


def candidates(db, message):
    """Select newest extraction FIRST; malformed/newer errors never resurrect
    old suggestions.  A hash-current canonical_projection shadows
    extract_llm for the same message (T12) so canonical request_pending
    facts drive suggestions when canonical mode is the fact source."""
    if (message["body_state"] != "full" or not isinstance(message["body_text"], str)
            or not message["body_text"].strip() or not valid_hash(message["content_hash"])):
        return []
    has_projection = db.execute("""
      SELECT 1 FROM artifacts WHERE message_id=? AND kind='canonical_projection'
        AND json_valid(meta) AND json_extract(meta,'$.hash')=? LIMIT 1
    """, (message["message_id"], message["content_hash"])).fetchone() is not None
    seen, result = set(), []
    for row in db.execute("""
      SELECT * FROM artifacts WHERE message_id=?
        AND kind IN ('extract_v1','extract_llm','canonical_projection')
      ORDER BY artifact_id DESC
    """, (message["message_id"],)):
        if row["kind"] in seen:
            continue
        if has_projection and row["kind"] == "extract_llm":
            continue
        seen.add(row["kind"])
        try:
            meta, content = json.loads(row["meta"]), json.loads(row["content"])
        except (ValueError, TypeError):
            continue
        if (row["project_id"] != message["project_id"] or not isinstance(meta, dict)
                or meta.get("hash") != message["content_hash"] or meta.get("error")
                or not isinstance(content, dict) or content.get("_error")
                or not isinstance(content.get("requests"), list)):
            continue
        for item in content["requests"]:
            if not isinstance(item, dict):
                continue
            text_key, to_key = ("ctx", "kind") if row["kind"] == "extract_v1" else ("action", "to")
            if not _text(item.get(text_key), 1000) or not _text(
                    item.get(to_key), 120,
                    nullable=row["kind"] in ("extract_llm",
                                             "canonical_projection")):
                continue
            if row["kind"] == "extract_v1" and item[to_key] not in (
                    "confirm", "contact", "share", "request", "ask", "report"):
                continue
            result.append({"extraction_kind": row["kind"], "artifact_id": row["artifact_id"],
                           "extracted_at": row["created_at"], "suggestion": item[text_key],
                           "suggested_kind_or_recipient": item.get(to_key), "confirmed": False})
    return result
