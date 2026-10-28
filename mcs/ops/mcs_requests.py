"""Human-confirmed local requests; no network or automatic task creation."""
import fcntl
import json
import mcs_util
import os
import re
import tempfile
import time
import uuid
from contextlib import ExitStack, contextmanager, suppress
from datetime import date
from pathlib import Path

# canonical/payload_hash/positive live in core; re-exported for callers
from mcs_util import canonical, payload_hash, positive, reject_constant, unique_pairs, valid_text  # noqa: F401
from safe_io import fsync_dir, read_regular_nofollow, fdopen_owned

MAX_COMMAND_BYTES = 16384
STATUSES = ("open", "in_progress", "done", "cancelled")
ALIAS_RETIRED_KIND = 'drug_alias_retired_command'
ALIAS_RETIRED_LIMIT = 2048


class AliasCommitUnknown(RuntimeError):
    pass


def alias_retired_commands(db):
    """Read the bounded, durable set of commands retired during recovery."""
    rows = db.execute(
        "SELECT typeof(content),length(CAST(content AS BLOB)),substr(content,1,37) "
        "FROM artifacts WHERE kind=? LIMIT ?",
        (ALIAS_RETIRED_KIND, ALIAS_RETIRED_LIMIT + 1)).fetchall()
    if len(rows) > ALIAS_RETIRED_LIMIT:
        raise ValueError('alias_retirement_capacity')
    values = set()
    for row in rows:
        if row[0] != 'text' or row[1] != 36 or not valid_uuid(row[2]) or row[2] in values:
            raise ValueError('alias_retirement_invalid')
        values.add(row[2])
    return values


@contextmanager
def _receipt_transaction(db):
    commit_sensitive = [False]
    try:
        with db:
            yield commit_sensitive
    except BaseException as error:
        if commit_sensitive[0]:
            raise AliasCommitUnknown("alias_commit_unknown: journal retained; inspect receipt and config before rollback") from error
        raise


def parse_command(raw):
    if len(raw) > MAX_COMMAND_BYTES:
        raise ValueError("command_too_large")
    try:
        command = json.loads(raw, object_pairs_hook=unique_pairs, parse_constant=reject_constant)
    except RecursionError:
        raise ValueError("command_too_deep") from None
    return command



def read_scheduled_receipt(raw, command_id):
    """Read a scheduled receipt without ambiguous JSON or contradictory identity."""
    try:
        receipt = parse_command(raw)
    except (ValueError, TypeError, RecursionError):
        return None
    if (not isinstance(command_id, str) or not command_id.strip()
            or not isinstance(receipt, dict) or receipt.get("scheduled") is not True
            or receipt.get("command_id", command_id) != command_id
            or receipt.get("outcome", "applied") != "applied"
            or receipt.get("error") is not None):
        return None
    return receipt



def read_command(path):
    return parse_command(read_regular_nofollow(
        path, MAX_COMMAND_BYTES,
        lambda: ValueError("regular_command_file_required"), check_size=False))


def valid_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _fields(fields):
    if not isinstance(fields, dict) or not fields or fields.keys() - {
            "title", "assignee", "due_date", "status"}:
        return "bad_patch"
    if "title" in fields and not valid_text(fields["title"], 1000):
        return "bad_title"
    if "assignee" in fields and not valid_text(fields["assignee"], 120, True):
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
    # every human-confirmed request command carries its reason — the
    # frontends already require one; the core enforces it so a command
    # written straight into data/cmd cannot skip the approval record
    if "reason" not in req:
        return ("loop_reason_required" if "loop_ref" in req
                else "reason_required")
    if not valid_text(req["reason"], 2000):
        return "bad_reason"
    if "loop_ref" not in req:
        return None
    from request_loops import validate_loop_ref
    return validate_loop_ref(req["loop_ref"])


def validate(req):
    if not isinstance(req, dict):
        return "bad_command"
    if req.get("cmd") == "ops.card_part_resolve":
        from notify_recovery import validate_card_part_resolve
        return validate_card_part_resolve(req)
    if req.get("cmd") == "ops.card_resolve":
        # Scope is derived from the stored render/coverage, not the
        # envelope — the positive project_id rule does not apply and a
        # supplied one is rejected as an unknown field by the dedicated
        # validator.
        from notify_recovery import validate_card_resolve
        return validate_card_resolve(req)
    common = {"version", "cmd", "command_id", "actor", "human_confirmed", "project_id"}
    if type(req.get("version")) is not int or req["version"] != 1:
        return "bad_version"
    if not valid_uuid(req.get("command_id")):
        return "bad_command_id"
    if req.get("human_confirmed") is not True:
        return "human_confirmation_required"
    if not valid_text(req.get("actor"), 120):
        return "bad_actor"
    if req.get("cmd") in ("ops.cloud_stop", "ops.cloud_resume", "ops.update_apply", "ops.update_rollback",
                          "ops.restore_approve", "ops.drug_alias_apply", "ops.drug_alias_rollback"):
        # System-wide lifecycle ops are projectless — they govern the
        # whole install, not one patient. The shared identity fields
        # (human_confirmed/actor/command_id) were already checked above;
        # operation rules run in operation_contract like every other ops.*.
        from operation_contract import validate_ops
        return validate_ops(req, common)
    if not positive(req.get("project_id")):
        return "bad_project_id"
    if isinstance(req.get("cmd"), str) and req["cmd"].startswith("ops."):
        # Keep the identity boundary in this module; operation payload rules
        # must not be able to bypass the shared human-confirmed envelope.
        from operation_contract import validate_ops
        return validate_ops(req, common)
    if req.get("cmd") == "request.create":
        allowed = common | {"source_message_id", "source_hash", "title", "assignee", "due_date", "reason", "loop_ref"}
        if req.keys() - allowed:
            return "unknown_field"
        if not positive(req.get("source_message_id")):
            return "bad_source_message_id"
        if not mcs_util.valid_hash(req.get("source_hash")):
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
        if not mcs_util.valid_hash(req.get("expected_source_hash")):
            return "bad_source_hash"
        extra_error = _request_extras(req)
        if extra_error:
            return extra_error
        return _fields(req.get("patch"))
    return "unknown_cmd"


def replay_receipt(db, command_id, digest) -> tuple:
    """(found, result) for ``command_id``: a conflict for another payload, else the stored original result.

    Call it inside the caller's BEGIN IMMEDIATE so the replay check and the
    receipt insert stay one transaction."""
    old = db.execute("SELECT payload_hash,receipt_json FROM command_receipts WHERE command_id=?",
                     (command_id,)).fetchone()
    if old is None:
        return False, None
    if old["payload_hash"] != digest:
        return True, {"outcome": "rejected", "error": "command_id_conflict"}
    return True, json.loads(old["receipt_json"])


def apply_command(ledger, req, *, cfg=None, data_dir=None):
    """Receipt-first transaction; exceptions leave the queue input retryable."""
    if not isinstance(req, dict) or not valid_uuid(req.get("command_id")):
        raise ValueError("bad_command_id")
    digest = payload_hash(req)
    db = ledger.db
    # requests/command_receipts landed in schema v5; Ledger already rejects
    # schemas newer than this code, so a floor is the compatibility check.
    if db.execute("PRAGMA user_version").fetchone()[0] < 5:
        raise RuntimeError("request_schema_not_ready")
    if req.get("cmd") == "ops.update_apply" and validate(req) is None:
        old = db.execute("SELECT payload_hash,receipt_json FROM command_receipts WHERE command_id=?",
                         (req["command_id"],)).fetchone()
        if old:
            if old["payload_hash"] != digest:
                return {"outcome": "rejected", "error": "command_id_conflict"}
            return json.loads(old["receipt_json"])
        # Network-bound sha resolution happens OUTSIDE the write
        # transaction — an ls-remote inside BEGIN IMMEDIATE would hold
        # the DB writer lock for the network round-trip (F-tx). The
        # digest above covers the DELIVERED command so a retry still
        # replays its original receipt even if the tag re-pointed.
        from mcs_operations import prepare_update_pins
        req = prepare_update_pins(req)
    # SQLite exits first, so a failed COMMIT still unwinds file promotions.
    with ExitStack() as filesystem_changes, _receipt_transaction(db) as commit_sensitive:
        db.execute("BEGIN IMMEDIATE")
        found, old = replay_receipt(db, req["command_id"], digest)
        if found:
            return old
        if req.get('cmd') == 'ops.drug_alias_apply':
            try:
                retired = alias_retired_commands(db)
            except ValueError:
                raise RuntimeError('alias_retirement_unavailable') from None
            if req['command_id'] in retired:
                return {'command_id': req['command_id'], 'outcome': 'rejected',
                        'error': 'alias_recovery_retired'}
        error = validate(req)
        before = after = None
        pid = req.get("project_id") if positive(req.get("project_id")) else None
        rid = None
        now = time.time()
        extra = {}
        receipt_loop_ref = None
        if req.get("cmd") in ("request.create", "request.update") \
                and isinstance(req.get("loop_ref"), dict):
            from request_loops import validate_loop_ref
            if validate_loop_ref(req["loop_ref"]) is None:
                receipt_loop_ref = req["loop_ref"]
        if not error and isinstance(req.get("cmd"), str) \
                and req["cmd"].startswith("ops."):
            from mcs_operations import apply_tx
            error, extra = apply_tx(db, req, now=now,
                                   filesystem_changes=filesystem_changes,
                                   **({"cfg": cfg, "data_dir": data_dir}
                                      if req["cmd"] == "ops.mcs_reaction" else {}))
        elif not error:
            error, rid, before, after = _apply_request_tx(
                db, req, pid, now)
        receipt = {"command_id": req["command_id"], "payload_hash": digest,
                   "project_id": pid, "request_id": rid,
                   "outcome": "rejected" if error else "applied", "error": error,
                   "actor": req.get("actor") if valid_text(req.get("actor"), 120) else None,
                   "reason": (req.get("reason")
                              if (req.get("cmd") in ("request.create", "request.update")
                                  and valid_text(req.get("reason"), 2000))
                              else None),
                   "loop_ref": receipt_loop_ref,
                   "revision": after["revision"] if after else None,
                   "before": before, "after": after, "processed_at": now}
        if extra:
            receipt.update(extra)
        db.execute("INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)",
                   (req["command_id"], digest, pid, rid, receipt["outcome"],
                    canonical(receipt).decode(), now))
        if not error and req.get("cmd") in ("ops.drug_alias_apply", "ops.drug_alias_rollback"):
            commit_sensitive[0] = True
    return receipt


def _apply_request_tx(db, req, pid, now):
    """request.create/update write path inside the receipt-first tx —
    returns (error, rid, before, after)."""
    error = None
    before = after = None
    rid = None
    loop_candidate = None
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
                from mcs_queries import resolve_staff
                assignee = req.get("assignee")
                if assignee:
                    # 台帳 link — materialize name（facility） when
                    # the directory resolves it unambiguously
                    assignee = resolve_staff(db, assignee, pid)
                rid = db.execute("""
                  INSERT INTO requests(project_id,source_message_id,source_hash,title,
                    assignee,due_date,status,revision,created_at,updated_at)
                  VALUES(?,?,?,?,?,?,'open',1,?,?)
                """, (pid, mid, expected, req["title"], assignee,
                      req.get("due_date"), now, now)).lastrowid
            else:
                rid = before["request_id"]
                fields = req["patch"]
                if fields.get("assignee"):
                    from mcs_queries import resolve_staff
                    fields = {**fields, "assignee": resolve_staff(
                        db, fields["assignee"], pid)}
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
                     canonical(link).decode(),
                     "human", canonical({
                         "command_id": req["command_id"],
                         "actor": req["actor"]}).decode(), now),
                )
    return error, rid, before, after


QUEUED_REQUEST_RE = re.compile(r"request-([0-9]{20})-")


@contextmanager
def queue_lock(cmd_dir, *, wait=True):
    """The order in which this lock is taken is the order commands apply: enqueue names and
    publishes a command while holding it, and drain lists the inbox only while holding it,
    so a drain never overtakes a writer however long that writer stalls."""
    # beside the inbox (like _circuit_lock), so the inbox itself holds only commands
    fd = os.open(os.path.normpath(cmd_dir) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError:
            locked = False
        yield locked
    finally:
        os.close(fd)  # closing releases the flock


@contextmanager
def _enqueue_lock(cmd_dir, authorized):
    if authorized is None:
        with queue_lock(cmd_dir):
            yield
        return
    # A revoked native listener must be able to finish even when another
    # process holds the inbox lock. CLI callers retain their blocking wait.
    while True:
        if authorized() is not True:
            raise ValueError("command_scope_changed")
        with queue_lock(cmd_dir, wait=False) as acquired:
            if acquired:
                yield
                return
        time.sleep(0.05)


def enqueue(req, cmd_dir, *, authorized=None):
    error = validate(req)
    if error:
        raise ValueError(error)
    raw = canonical(req)
    if len(raw) > MAX_COMMAND_BYTES:
        raise ValueError("command_too_large")
    receipt = {"command_id": req["command_id"], "payload_hash": payload_hash(req), "outcome": "queued"}
    # The inbox must already exist; never accidentally create a host-like path in a container.
    with _enqueue_lock(cmd_dir, authorized):
        if authorized is not None and authorized() is not True:
            raise ValueError("command_scope_changed")
        # drain_commands applies files in name order: number after every queued command,
        # even if the clock stepped back, so the lock order is the apply order.
        queued = [int(m[1]) for name in os.listdir(cmd_dir) if (m := QUEUED_REQUEST_RE.match(name))]
        seq = max([time.time_ns(), *(n + 1 for n in queued)])
        fd, temp = tempfile.mkstemp(prefix=f"request-{seq:020d}-", suffix=".tmp", dir=cmd_dir)
        published = False
        try:
            with fdopen_owned(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if authorized is not None and authorized() is not True:
                raise ValueError("command_scope_changed")
            os.link(temp, temp[:-4] + ".json")  # Atomic publication without clobbering pending input.
            published = True
            os.unlink(temp)
            fsync_dir(cmd_dir)
        except OSError:
            if not published:
                raise
            return {**receipt, "outcome": "unknown", "error": "queue_durability_unknown"}
        finally:
            # An ignored .tmp is safer than masking the primary result or
            # deleting published input.
            with suppress(OSError):
                Path(temp).unlink(missing_ok=True)
    return receipt


def candidates(db, message):
    """Select the newest legacy extraction before validating its suggestions.
    The newest usable canonical_projection shadows
    extract_llm for the same message (T12) so canonical request_pending
    facts drive suggestions when canonical mode is the fact source."""
    if (message["body_state"] != "full" or not isinstance(message["body_text"], str)
            or not message["body_text"].strip() or not mcs_util.valid_hash(message["content_hash"])):
        return []
    # Select the same artifact as stats/signals, including when a newer
    # malformed or expired projection follows a usable one.
    from mcs_queries import EXTRACTION_KINDS_SQL, current_projection_id, current_v4_id
    current = db.execute(f"""
      SELECT {current_projection_id('m')}, {current_v4_id('m')}
      FROM messages m
      WHERE m.message_id=? AND m.project_id=? AND m.content_hash=?
    """, (message["message_id"], message["project_id"],
          message["content_hash"])).fetchone()
    projection_id = current[0] if current else None
    v4_id = current[1] if current else None
    seen, result = set(), []
    for row in db.execute(f"""
      SELECT * FROM artifacts WHERE message_id=?
        AND kind IN ({EXTRACTION_KINDS_SQL})
      ORDER BY artifact_id DESC
    """, (message["message_id"],)):
        if row["kind"] == "semantic_facts_v4" \
                and row["artifact_id"] != v4_id:
            continue
        if row["kind"] == "canonical_projection" and (
                row["artifact_id"] != projection_id
                or v4_id is not None):
            continue
        if row["kind"] in seen:
            continue
        if (projection_id is not None or v4_id is not None) \
                and row["kind"] == "extract_llm":
            continue
        seen.add(row["kind"])
        try:
            meta, content = json.loads(row["meta"]), json.loads(row["content"])
        except (ValueError, TypeError, RecursionError):
            continue
        if (row["project_id"] != message["project_id"] or not isinstance(meta, dict)
                or meta.get("hash") != message["content_hash"] or meta.get("error")
                or (row["kind"] == "canonical_projection"
                    and meta.get("invalidated"))
                or not isinstance(content, dict) or content.get("_error")
                or not isinstance(content.get("requests"), list)):
            continue
        for item in content["requests"]:
            if not isinstance(item, dict):
                continue
            text_key, to_key = ("ctx", "kind") if row["kind"] == "extract_v1" else ("action", "to")
            if not valid_text(item.get(text_key), 1000) or not valid_text(
                    item.get(to_key), 120,
                    nullable=row["kind"] in ("extract_llm",
                                             "canonical_projection",
                                             "semantic_facts_v4")):
                continue
            if row["kind"] == "extract_v1" and item[to_key] not in (
                    "confirm", "contact", "share", "request", "ask", "report"):
                continue
            result.append({"extraction_kind": row["kind"], "artifact_id": row["artifact_id"],
                           "extracted_at": row["created_at"], "suggestion": item[text_key],
                           "suggested_kind_or_recipient": item.get(to_key), "confirmed": False})
    return result
