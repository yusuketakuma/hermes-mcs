"""Recheck semantic send eligibility and current audited summaries at each delivery boundary."""
import semantic_policy
import semantic_store
import semantic_transport
import json
import sqlite3
from mcs_util import loads_dict, payload_hash, strict_json





class DeferredSend(Exception):
    """Not deliverable under CURRENT policy (e.g. semantic mode left
    enforce) — the committed intent stays pending for a later flush;
    it is not destroyed (INV-23) and never sent past its gate."""


class StaleSend(Exception):
    """The source generation this intent was built for has moved on —
    suppress terminally instead of publishing stale results (§18.4)."""


class FreezeSend(Exception):
    """A semantic intent changed after a chunk was accepted — preserve its
    receipt/progress and quarantine the remaining chunks."""


def _stale(reason: str, in_progress: bool):
    if in_progress:
        raise FreezeSend(reason)
    raise StaleSend(reason)



def intake_root_held(db, project_id, root):
    """Hold semantic presentation if the root's intake cannot be verified."""
    try:
        import semantic_intake_hold
        semantic_intake_hold.validate_indexes(db)
        return semantic_intake_hold.has_held_root(db, project_id, root)
    except sqlite3.Error:
        return True


def skipped_summary_current(db, project_id, message_id, fingerprint, policy, revision, mode):
    """Validate a skipped summary against its current, separately stored receipt."""
    from inference_version import is_current
    from semantic_policy import KIND_AUDIT, KIND_SUMMARY
    rows = []
    for kind in (KIND_SUMMARY, KIND_AUDIT):
        row = db.execute("SELECT content,meta FROM artifacts WHERE kind=? AND project_id=? "
                         "AND message_id=? ORDER BY artifact_id DESC LIMIT 1",
                         (kind, project_id, message_id)).fetchone()
        if row is None:
            return False
        try:
            content, meta = strict_json(row["content"]), strict_json(row["meta"])
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return False
        if (not isinstance(content, dict) or not isinstance(meta, dict) or not is_current(meta)
                or meta.get("fingerprint") != fingerprint or meta.get("policy_fingerprint") != policy
                or meta.get("audit_status") != "SKIPPED" or meta.get("jev_evaluated") is not False
                or meta.get("code_checks") != "passed" or meta.get("invalidated") or meta.get("stale")
                or meta.get("publication_mode") != mode):
            return False
        rows.append((content, meta))
    (summary, smeta), (audit, ameta) = rows
    reasons = {"not_configured", "disabled", "daily_budget", "backlog_reserved", "service_unavailable", "circuit_open"}
    findings = audit.get("findings")
    return (smeta.get("target_revision") == revision and summary.get("audit_status") == "SKIPPED"
            and audit.get("status") == "SKIPPED" and audit.get("target_message_id") == message_id
            and isinstance(findings, list) and bool(findings)
            and all(isinstance(item, dict) and item.get("code") == "support_unevaluated" for item in findings)
            and smeta.get("jev_skip_reason") in reasons
            and smeta.get("jev_skip_reason") == ameta.get("jev_skip_reason")
            and smeta.get("summary_hash") == ameta.get("summary_hash") == payload_hash(summary))

def current_summary(db, project_id: int, message_id: int,
                     bundle, policy: str):
    """The newest semantic_summary for this message when it is publishable
    on the CURRENT generation (PASS, not stale, enforce, same policy,
    whole-thread fingerprint and target revision), else None. The single
    predicate shared by the send gate, render state and summary block.
    Returns ``{artifact_id, content, meta}``. ``bundle`` may be a dict or
    a zero-arg callable resolved only after the cheap row/meta checks pass
    (thread_bundle reads the whole thread); a raising callable -> None."""
    row = db.execute(
        "SELECT artifact_id,content,meta FROM artifacts "
        "WHERE kind='semantic_summary' AND project_id=? AND message_id=? "
        "ORDER BY artifact_id DESC LIMIT 1",
        (project_id, message_id)).fetchone()
    if row is None:
        return None
    content, meta = loads_dict(row["content"]), loads_dict(row["meta"])
    if not isinstance(content, dict) or not isinstance(meta, dict):
        return None
    if (meta.get("audit_status") not in ("PASS", "SKIPPED")
            or meta.get("stale")
            or meta.get("publication_mode") != "enforce"
            or meta.get("policy_fingerprint") != policy):
        return None
    if callable(bundle):
        try:
            bundle = bundle()
        except Exception:
            return None
    if not isinstance(bundle, dict):
        return None
    target = next((m for m in bundle.get("members", [])
                   if m["message_id"] == message_id), None)
    if (meta.get("fingerprint") == bundle.get("source_fingerprint")
            and target is not None
            and meta.get("target_revision") == target["revision"]
            and (meta.get("audit_status") == "PASS" or skipped_summary_current(
                db, project_id, message_id, bundle["source_fingerprint"], policy,
                target["revision"], "enforce"))):
        return {"artifact_id": row["artifact_id"], "content": content,
                "meta": meta}
    return None


def enforce_block_reason(db, scfg: dict, project_id) -> str | None:
    """The reason semantic content must not ride this send, or None.

    Order is mode, project_id type, pause, scope: a malformed id never
    reaches ``paused``. Each caller maps the reason to its own outcome."""
    if scfg["mode"] != "enforce" or scfg["summary_mode"] != "enforce":
        return "semantic_not_enforce"
    if type(project_id) is not int or project_id <= 0:
        return "payload_invalid"
    from mcs_queries import paused
    if paused(db, project_id):
        return "semantic_paused"
    if scfg["project_ids"] is not None and project_id not in scfg["project_ids"]:
        return "project_out_of_scope"
    return None


class _SourceUnverified(Exception):
    """The source cannot be read within the safe admission bounds."""


def _source_event(db, project_id: int, event_id: int, *, read_progress=True):
    """Load a bounded origin event only inside the semantic notice's project."""
    import semantic_intake_hold
    try:
        if not semantic_intake_hold._rowid_primary_key(db, 'notify_outbox', 'event_id'):
            raise _SourceUnverified()
        row = db.execute(
            "SELECT state,attempts,typeof(payload) AS payload_type,typeof(progress) AS progress_type, "
            "CASE WHEN typeof(progress) IN ('integer','real') THEN progress END AS progress_scalar "
            "FROM main.notify_outbox WHERE event_id=? AND kind='new_messages' AND project_id=?",
            (event_id, project_id)).fetchone()
        if row is None:
            return None, None
        def bounded(column, storage, limit):
            if storage not in ('text', 'blob'):
                return None
            with db.blobopen('notify_outbox', column, event_id, readonly=True) as blob:
                if len(blob) > limit:
                    raise _SourceUnverified()
                raw = blob.read()
            return raw.decode(semantic_intake_hold._codec(db)) if storage == 'text' else raw
        raw = bounded('payload', row['payload_type'], 1024 * 1024)
        progress = (bounded('progress', row['progress_type'], 65536)
                    if read_progress and row['progress_type'] in ('text', 'blob')
                    else row['progress_scalar'] if read_progress else None)
        source = loads_dict(raw)
        return {'state': row['state'], 'attempts': row['attempts'], 'progress': progress}, source
    except (sqlite3.Error, UnicodeError):
        raise _SourceUnverified() from None



def marker_source_gate(db, project_id, payload, *, in_progress=False):
    """Recheck the exact arrival and target inside the send-marker transaction."""
    import semantic_intake_hold
    src = payload.get('src_event_id')
    if type(src) is not int or src <= 0:
        raise ValueError('payload_invalid')
    row = db.execute(
        "SELECT state,attempts,typeof(progress) AS progress_type FROM main.notify_outbox "
        "WHERE event_id=? AND kind='new_messages' AND project_id=?", (src, project_id)).fetchone()
    if row is None or row['state'] == 'suppressed':
        _stale('src_event_ineligible', in_progress)
    try:
        ids = semantic_intake_hold._arrival_targets(db, project_id, src, strict=True)
    except (sqlite3.Error, ValueError, TypeError):
        raise FreezeSend('src_event_unverified') if in_progress else DeferredSend('src_event_unverified') from None
    degraded = payload.get('degraded', False)
    targets = payload.get('target_message_ids') if degraded else [payload.get('target_message_id')]
    if (not isinstance(targets, list) or not targets
            or any(type(mid) is not int or mid <= 0 for mid in targets)
            or not set(targets).issubset(ids)):
        _stale('src_event_ineligible', in_progress)
    if degraded:
        if row['state'] == 'accepted' or row['attempts'] > 0:
            _stale('base_delivered', in_progress)
        if row['progress_type'] == 'null':
            progress = {}
        elif row['progress_type'] in ('text', 'blob'):
            try:
                with db.blobopen('notify_outbox', 'progress', src, readonly=True) as blob:
                    if len(blob) > 65536:
                        raise ValueError('progress_too_large')
                    raw = blob.read()
                    if row['progress_type'] == 'text':
                        raw = raw.decode(semantic_intake_hold._codec(db))
                    progress = loads_dict(raw)
            except (sqlite3.Error, ValueError, UnicodeError):
                progress = None
        else:
            progress = None
        if not isinstance(progress, dict):
            raise FreezeSend('src_event_unverified') if in_progress else DeferredSend('src_event_unverified')
        if progress.get('sent'):
            _stale('base_delivered', in_progress)

def initial_history_ids(db, project_id, ids):
    history = set()
    for offset in range(0, len(ids), 900):
        group = ids[offset:offset + 900]
        history.update(row[0] for row in db.execute(
                "SELECT m.message_id FROM messages m JOIN message_metadata d ON d.message_id=m.message_id "
                "AND d.source='capture' WHERE m.project_id=? AND m.message_id IN ("
                + ','.join('?' * len(group)) + ") AND CASE WHEN json_valid(d.content) "
                "THEN json_extract(d.content,'$._initial_import.role')='history' ELSE 0 END",
                (project_id, *group)))
    return history


def _has_initial_history(db, project_id, ids):
    return bool(initial_history_ids(db, project_id, ids))


def semantic_gate(db, ev, payload: dict, cfg: dict,
                  in_progress: bool = False):
    """Recheck a semantic intent immediately before every external post."""
    if not isinstance(payload, dict):
        raise ValueError("payload_invalid")
    scfg, _ = semantic_policy.runtime_config(cfg)
    project_id = ev["project_id"]
    reason = enforce_block_reason(db, scfg, project_id)
    if reason == "payload_invalid":
        raise ValueError(reason)
    if reason is not None:
        raise FreezeSend(reason) if in_progress else DeferredSend(reason)
    policy_version = payload.get("policy_version")
    if not isinstance(policy_version, str) \
            or policy_version != semantic_policy.POLICY_VERSION:
        _stale("policy_changed", in_progress)
    degraded = payload.get("degraded", False)
    if type(degraded) is not bool:
        raise ValueError("payload_invalid")
    policy = payload.get("policy_fingerprint")
    if not isinstance(policy, str) \
            or policy != semantic_policy.policy_fingerprint(scfg):
        _stale("policy_changed", in_progress)
    fp = payload.get("fingerprint")
    root = payload.get("root_id")
    src = payload.get("src_event_id")
    if not isinstance(fp, str) or not fp or type(root) is not int \
            or root <= 0 or type(src) is not int or src <= 0:
        raise ValueError("payload_invalid")
    if intake_root_held(db, project_id, root):
        raise FreezeSend('semantic_intake_held') if in_progress else DeferredSend('semantic_intake_held')
    try:
        source_row, source = _source_event(db, project_id, src, read_progress=degraded)
    except _SourceUnverified:
        raise FreezeSend('src_event_unverified') if in_progress else DeferredSend('src_event_unverified') from None
    if source_row is None or source is None:
        _stale("src_event_ineligible", in_progress)
    try:
        source_ids = source.get("message_ids")
        if (not isinstance(source_ids, list)
                or any(type(mid) is not int or mid <= 0 for mid in source_ids)):
            raise ValueError
        source_ids = set(source_ids)
    except (AttributeError, ValueError):
        _stale("src_event_ineligible", in_progress)
    if source_row["state"] == "suppressed":
        _stale("src_event_ineligible", in_progress)
    if degraded:
        target_ids = payload.get("target_message_ids")
        if (not isinstance(target_ids, list) or not target_ids
                or any(type(mid) is not int or mid <= 0 for mid in target_ids)
                or len(set(target_ids)) != len(target_ids)
                or not set(target_ids).issubset(source_ids)):
            raise ValueError("payload_invalid")
        if _has_initial_history(db, project_id, target_ids):
            _stale("initial_history", in_progress)
        progress = loads_dict(source_row["progress"] or "{}")
        if (source_row["state"] == "accepted"
                or source_row["attempts"] > 0
                or not isinstance(progress, dict)
                or bool(progress.get("sent"))):
            _stale("base_delivered", in_progress)
        bundle = semantic_store.thread_bundle(db, project_id, root)
        if bundle is None or bundle["source_fingerprint"] != fp:
            _stale("stale_generation", in_progress)
        member_ids = {m["message_id"] for m in bundle["members"]}
        if not set(target_ids).issubset(member_ids):
            _stale("stale_generation", in_progress)
    else:
        target = payload.get("target_message_id")
        target_revision = payload.get("target_revision")
        if (type(target) is not int or target <= 0
                or not isinstance(target_revision, str) or not target_revision
                or target not in source_ids):
            raise ValueError("payload_invalid")
        if _has_initial_history(db, project_id, [target]):
            _stale("initial_history", in_progress)
        bundle = semantic_store.thread_bundle(db, project_id, root)
        if bundle is None or bundle["source_fingerprint"] != fp:
            _stale("stale_generation", in_progress)
        member = next((m for m in bundle["members"]
                       if m["message_id"] == target), None)
        if member is None or member["revision"] != target_revision:
            _stale("stale_generation", in_progress)
        if current_summary(db, project_id, target, bundle,
                            policy) is None:
            _stale("summary_stale", in_progress)


def semantic_render_state(db, ev, cfg: dict) -> tuple:
    """Current identities of audited summaries attached to a raw notice."""
    scfg, _ = semantic_policy.runtime_config(cfg)
    project_id = ev["project_id"]
    if enforce_block_reason(db, scfg, project_id) is not None:
        return ()
    try:
        payload = json.loads(ev["payload"])
        ids = payload.get("message_ids") or []
    except Exception:
        return ()
    state = []
    local_model = semantic_transport.llm_model() if isinstance(ids, list) and ids else None
    for mid in ids if isinstance(ids, list) else []:
        if type(mid) is not int:
            continue
        row = db.execute(
            "SELECT project_id,message_id,parent_id,content_hash "
            "FROM messages WHERE project_id=? AND message_id=?",
            (project_id, mid)).fetchone()
        if row is None:
            continue
        root = row["parent_id"] or row["message_id"]
        if intake_root_held(db, project_id, root):
            continue
        cur = current_summary(
            db, project_id, mid,
            lambda r=root, m=mid: semantic_store.thread_bundle(
                db, project_id, r, [m], local_model=local_model),
            semantic_policy.policy_fingerprint(scfg))
        if cur is None:
            continue
        state.append((mid, cur["artifact_id"], cur["meta"]["fingerprint"]))
    return tuple(state)


def semantic_render_gate(db, ev, initial: tuple, cfg: dict,
                         in_progress: bool = False):
    """Freeze/defer a raw notice whose attached summaries left enforce
    or changed since ``initial`` was snapshotted."""
    if not initial:
        return
    scfg, _ = semantic_policy.runtime_config(cfg)
    # the cheap policy check runs before the thread_bundle-heavy snapshot
    reason = enforce_block_reason(db, scfg, ev["project_id"])
    if reason is not None:
        raise FreezeSend(reason) if in_progress else DeferredSend(reason)
    if semantic_render_state(db, ev, cfg) != initial:
        raise FreezeSend("semantic_generation_changed") if in_progress \
            else StaleSend("semantic_generation_changed")








   # never let a summary render break delivery
