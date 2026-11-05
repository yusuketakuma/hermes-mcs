"""Maintain source generations, invalidation and durable semantic intake on a borrowed ledger transaction."""
import hashlib
import json
import sqlite3
import time
import uuid

from mcs_util import loads_dict


def _bump_inference_epoch_tx(db, field):
    if field not in ('source_epoch', 'analysis_epoch', 'job_state_epoch'):
        raise ValueError('inference_epoch_field_invalid')
    if not db.in_transaction:
        raise ValueError(f'inference_{field}_transaction_required')
    row = db.execute("SELECT payload FROM fetch_jobs WHERE kind='semantic_sched' "
                     'AND project_id=0 AND message_id=0').fetchone()
    if row is None:
        return
    state = loads_dict(row[0]) or {}
    if field == 'job_state_epoch':
        from inference_version import is_current
        progress = state.get('inference_reprocess')
        if (not is_current(progress) or progress.get('phase') not in
                ('seed', 'verify', 'verify_patients', 'waiting', 'complete')):
            return
    value = state.get(field, 0)
    if type(value) is not int or not 0 <= value < 2**63 - 1:
        db.execute(f"UPDATE fetch_jobs SET payload=json_set(payload,'$.{field}_invalid',json('true')) "
                   "WHERE kind='semantic_sched' AND project_id=0 AND message_id=0 AND json_valid(payload)")
        return
    db.execute(f"UPDATE fetch_jobs SET payload=json_set(payload,'$.{field}',?) "
               "WHERE kind='semantic_sched' AND project_id=0 AND message_id=0 AND json_valid(payload)", (value + 1,))


def bump_inference_analysis_epoch_tx(db, project_id=None):
    """Update the adopted-input witness on a borrowed active transaction, without committing or closing it."""
    from inference_version import current_meta_pred
    _bump_inference_epoch_tx(db, 'analysis_epoch')
    if project_id is not None:
        db.execute("DELETE FROM artifacts WHERE kind='patient_rollup' AND project_id=? AND "
                   + current_meta_pred('meta'), (project_id,))


def bump_semantic_job_state_epoch_tx(db):
    """Bookmark real job state transitions for an existing replay, without changing its input budget."""
    _bump_inference_epoch_tx(db, 'job_state_epoch')


class LedgerSemanticSourcesMixin:
    """Maintain derivation input witnesses and jobs using the caller's existing connection."""

    def _semantic_generation_snapshot(self, messages) -> dict:
        """Capture touched rows and their thread generation before a batch."""
        keys = set()
        for message in messages:
            rows = [message, *(getattr(message, "replies", None) or [])]
            for row in rows:
                project_id = getattr(row, "project_id", None)
                message_id = getattr(row, "message_id", None)
                parent_id = getattr(row, "parent_id", None)
                root = message_id if parent_id is None else parent_id
                if project_id is not None and root is not None:
                    keys.add((project_id, message_id, root))
        snapshot = {}
        generations = {}
        for project_id, message_id, root in keys:
            thread_key = (project_id, root)
            if thread_key not in generations:
                generations[thread_key] = self._semantic_source_generation(
                    project_id, root)
            snapshot[(project_id, message_id)] = (
                root,
                generations[thread_key],
                self._semantic_message_fingerprint(project_id, message_id),
            )
        return snapshot

    def _needs_semantic_source_tracking(self) -> bool:
        return self.db.execute(
            "SELECT 1 FROM artifacts WHERE kind IN "
            "('canonical_projection','semantic_facts_v4','extract_v1','extract_llm',"
            "'patient_context_scan_chunk_v1','inference_dependency_loss') LIMIT 1"
        ).fetchone() is not None or self.db.execute(
            "SELECT 1 FROM fetch_jobs WHERE kind='semantic_sched' AND project_id=0 "
            "AND message_id=0 AND CASE WHEN json_valid(payload) THEN "
            "json_type(payload,'$.inference_reprocess')='object' END LIMIT 1"
        ).fetchone() is not None

    def bump_inference_source_epoch_tx(self):
        """Record a real original/scope input change in the caller's active transaction."""
        _bump_inference_epoch_tx(self.db, 'source_epoch')

    def bump_inference_analysis_epoch_tx(self, project_id=None):
        """Record an adopted extraction change and expire only the current rollup in the same transaction."""
        bump_inference_analysis_epoch_tx(self.db, project_id)

    def _invalidate_thread_projections(self, project_id: int, root: int,
                                       *, invalidated_roots: set | None = None, source_changed=True):
        from inference_version import current_meta_pred
        key = (project_id, root)
        source_key = ('source', project_id, root)
        source_dirty = source_changed and (invalidated_roots is None or source_key not in invalidated_roots)
        changed = 0
        if invalidated_roots is None or key not in invalidated_roots:
            changed = self.db.execute("""
              UPDATE artifacts SET meta=json_set(meta,'$.invalidated',json('true'))
              WHERE kind IN ('canonical_projection','semantic_facts_v4')
                AND project_id=?
                AND json_valid(meta)
                AND json_extract(meta,'$.invalidated') IS NOT 1
                AND message_id IN (SELECT message_id FROM messages
                  WHERE project_id=? AND (message_id=? OR parent_id=?))
            """, (project_id, project_id, root, root)).rowcount
        if source_dirty:
            self.bump_inference_source_epoch_tx()
            if invalidated_roots is not None:
                invalidated_roots.add(source_key)
        if changed or source_dirty:
            self.db.execute(
                "DELETE FROM artifacts WHERE kind='patient_rollup' AND project_id=? AND "
                + current_meta_pred('meta'),
                (project_id,))
        if invalidated_roots is not None:
            invalidated_roots.add(key)

    def _semantic_changed_ids(self, before: dict, *,
                              invalidated_roots: set | None = None) -> dict[int, list[int]]:
        """Return touched message IDs whose thread input actually changed."""
        changed: dict[int, list[int]] = {}
        invalidated_roots = set() if invalidated_roots is None else invalidated_roots
        generations = {}
        for (project_id, message_id), (root, previous, row_before) \
                in before.items():
            thread_key = (project_id, root)
            if thread_key not in generations:
                generations[thread_key] = self._semantic_source_generation(
                    project_id, root)
            current = generations[thread_key]
            row_after = self._semantic_message_fingerprint(project_id, message_id)
            if current != previous and row_after != row_before:
                changed.setdefault(project_id, []).append(message_id)
                self._invalidate_thread_projections(
                    project_id, root, invalidated_roots=invalidated_roots)
        for ids in changed.values():
            ids.sort()
        return changed

    def _semantic_attachments(self, message_id: int) -> list:
        return [dict(row) for row in self.db.execute(
            "SELECT attachment_id,file_id,name,bytes,sha256,state FROM attachments "
            "WHERE message_id=? AND state != 'withdrawn' "
            "ORDER BY attachment_id", (message_id,))]

    def _semantic_member(self, row, attachments=None) -> dict:
        """Canonical member dict of the thread-input digest contract —
        one shape shared by the per-thread and per-message fingerprints."""
        body = row["body_text"] or ""
        return {
            "message_id": row["message_id"],
            "parent_id": row["parent_id"],
            "revision": row["content_hash"]
                        or hashlib.sha256(body.encode()).hexdigest(),
            "body_digest": hashlib.sha256(body.encode()).hexdigest(),
            "source_occurrence": row["source_occurrence"] if "source_occurrence" in row.keys() else 1,
            "body_state": row["body_state"] or "unknown",
            "reply_count": row["reply_count"] or 0,
            "sender_id": row["sender_id"],
            "sender_name": row["sender_name"] or "",
            "sender_type": row["sender_type"] or "",
            "profession": row["profession"] or "",
            "organization": row["organization"] or "",
            "posted_at": row["posted_at"] or "",
            "attachments": (self._semantic_attachments(row["message_id"])
                            if attachments is None else attachments),
        }

    @staticmethod
    def _semantic_digest(payload) -> str:
        return hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()

    def _semantic_source_generation(self, project_id: int, root: int) -> str:
        """Digest the stored thread input used by a semantic seed.

        This is deliberately semantic-specific.  Generic fetch jobs retain
        their existing revive/reset contract; semantic retries need a stable
        input generation so replaying the same input cannot silently erase
        its accumulated attempts.
        """
        rows = self.db.execute(
            "SELECT message_id,parent_id,content_hash,body_state,body_text,"
            "reply_count,sender_id,sender_name,sender_type,profession,"
            "organization,posted_at FROM messages WHERE project_id=? "
            "AND (message_id=? OR parent_id=?) "
            "ORDER BY posted_at_ts,message_id", (project_id, root, root))
        first = rows.fetchone()
        if first is None:
            return self._semantic_digest([])
        attachments = {}
        for row in self.db.execute(
                "SELECT a.message_id,a.attachment_id,a.file_id,a.name,a.bytes,a.sha256,a.state "
                "FROM attachments a JOIN messages m ON m.message_id=a.message_id "
                "WHERE m.project_id=? AND (m.message_id=? OR m.parent_id=?) "
                "AND a.state != 'withdrawn' ORDER BY a.attachment_id", (project_id, root, root)):
            item = dict(row)
            message_id = item.pop("message_id")
            attachments.setdefault(message_id, []).append(item)
        occurrences = {row[0]: row[1] for row in self.db.execute(
            'SELECT message_id,MAX(seq) FROM message_revisions WHERE message_id IN '
            '(SELECT message_id FROM messages WHERE project_id=? AND (message_id=? OR parent_id=?)) '
            'GROUP BY message_id', (project_id, root, root))}
        members = [self._semantic_member({**dict(row), 'source_occurrence': occurrences.get(row['message_id'], 1)},
                                        attachments.get(row['message_id'], [])) for row in [first, *rows]]
        return self._semantic_digest(members)

    def _semantic_message_fingerprint(self, project_id: int,
                                      message_id: int) -> str | None:
        """Digest one touched row using the thread input fields."""
        row = self.db.execute(
            "SELECT message_id,parent_id,content_hash,body_state,body_text,"
            "reply_count,sender_id,sender_name,sender_type,profession,"
            "organization,posted_at,(SELECT COALESCE(MAX(r.seq),1) FROM message_revisions r "
            "WHERE r.message_id=messages.message_id) AS source_occurrence FROM messages "
            "WHERE project_id=? AND message_id=?",
            (project_id, message_id)).fetchone()
        if row is None:
            return None
        return self._semantic_digest(self._semantic_member(row))

    def semantic_seed_tx(self, project_id: int, message_ids: list,
                          origin: dict, *, invalidated_roots: set | None = None,
                          _source_generation: str | None = None,
                          _eligible_ids: set | None = None):
        """Durable semantic-eval seeding — caller holds `with self.db` so
        job creation commits (or rolls back) with the message and notify
        intent that triggered it (INV-06, spec §18.1). Keyed by thread
        ROOT so one job covers a post plus its replies; a new arrival on
        an evaluated thread revives the job as a new input generation."""
        from inference_version import CURRENT_VERSION, VERSION_KEY, is_current
        import semantic_intake_hold
        # Pause holds execution, not durable intake; OFF callers do not seed.
        if not message_ids:
            return
        semantic_intake_hold.validate_indexes(self.db)
        if self.db.execute("SELECT 1 FROM temp.sqlite_schema WHERE type IN ('table','view') "
                           "AND lower(name) IN ('fetch_jobs','artifacts','notify_outbox') LIMIT 1").fetchone():
            raise sqlite3.DatabaseError('semantic_seed_temp_shadow')
        q = ("SELECT message_id,COALESCE(parent_id,message_id) r "
             "FROM messages WHERE project_id=? AND message_id IN ("
             + ",".join("?" * len(message_ids)) + ")")
        roots: dict[int, list] = {}
        for r in self.db.execute(q, [project_id, *message_ids]):
            roots.setdefault(r["r"], []).append(r["message_id"])
        now = time.time()
        for root, ids in roots.items():
            existing = self.db.execute("""
              SELECT job_id,state,attempts,typeof(payload) AS payload_type FROM fetch_jobs
              WHERE kind='semantic' AND project_id=? AND message_id=?
            """, (project_id, root)).fetchone()
            if ((existing is not None and not semantic_intake_hold.supported_job(
                    self.db, existing, project_id, root))
                    or semantic_intake_hold.has_held_root(self.db, project_id, root)):
                self._intake_hold_touched = True
                try:
                    semantic_intake_hold.hold_tx(
                        self, project_id, root, ids, origin, eligible_ids=_eligible_ids,
                        source_generation=(_source_generation if _source_generation is not None
                                           else self._semantic_source_generation(project_id, root)))
                except Exception as exc:
                    # Keep the original exception type for callers. The enclosing
                    # message transaction must roll back before recording failure.
                    exc.semantic_intake_hold_write_failed = True
                    raise
                continue
            eligible = isinstance(origin, dict) \
                and origin.get("event_id") is not None
            notification_free = isinstance(origin, dict) \
                and origin.get("notification_free") is True
            execution = (origin.get('inference_generation')
                         if isinstance(origin, dict)
                         and origin.get('source') == 'inference_reprocess' else None)
            source_generation = (_source_generation if _source_generation is not None
                                 else self._semantic_source_generation(project_id, root))
            if existing is not None:
                existing = self.db.execute(
                    'SELECT job_id,state,attempts,payload FROM fetch_jobs WHERE job_id=?',
                    (existing['job_id'],)).fetchone()
                old_pl = loads_dict(existing["payload"] or "{}") or {}
                stored_targets = old_pl.get("targets")
                old_targets = ({x for x in stored_targets if type(x) is int}
                               if isinstance(stored_targets, list) else set())
                old_origin = old_pl.get("origin")
                old_eligible = bool(old_pl.get("eligible"))
                old_notification_free = bool(old_pl.get(
                    "notification_free"))
                arrival = origin.get('arrival_event_id') if isinstance(origin, dict) else None
                preserve_live = (execution is not None
                                 and existing['state'] == 'pending'
                                 and old_eligible and not old_notification_free
                                 and type(arrival) is int and self.db.execute(
                                     "SELECT 1 FROM notify_outbox WHERE event_id=? AND project_id=? "
                                     "AND kind='new_messages' AND state!='suppressed'",
                                     (arrival, project_id)).fetchone() is not None)
                if isinstance(old_pl.get('intake_notification_targets'), dict) and not old_notification_free:
                    preserve_live |= any(semantic_intake_hold.notification_allowed(self, old_pl, project_id, mid)
                                         for mid in old_targets)
                pl = dict(old_pl)
                pl["targets"] = sorted(old_targets | set(ids))
                # once a job descends from a notify intent it keeps that
                # provenance — a later history/replay merge must not
                # demote its drain priority nor erase which arrival
                # event seeded it (INV-20 keeps eligibility derived
                # from the stored event; 'eligible' is ordering
                # metadata only)
                if eligible or not (
                        isinstance(pl.get("origin"), dict)
                        and pl["origin"].get("event_id") is not None):
                    pl["origin"] = origin
                if eligible:
                    pl["eligible"] = True
                    if isinstance(pl.get('intake_notification_targets'), dict):
                        arrival_ids = semantic_intake_hold._arrival_targets(
                            self.db, project_id, origin['event_id'], strict=True)
                        pl['intake_notification_targets'].update({
                            str(mid): self._semantic_message_fingerprint(project_id, mid)
                            for mid in ids if mid in arrival_ids})
                    # An actual arrival event is the explicit opt-in for a
                    # notification.  It supersedes a replay's suppression
                    # marker, even when the old origin is being replaced.
                    pl.pop("notification_free", None)
                    pl.pop('reprocess_only', None)
                    pl.pop('execution_generation', None)
                    pl.pop('execution_contract', None)
                elif notification_free and not preserve_live:
                    # Keep this marker at the payload level as well as in
                    # the incoming origin: a prior eligible origin may be
                    # retained for provenance, but this seed must still be
                    # ineligible for notification generation.
                    pl["notification_free"] = True
                source_changed = (old_targets != set(pl['targets'])
                                  or pl.get('source_generation') != source_generation)
                input_changed = (not is_current(old_pl)
                                 or (execution is not None
                                     and old_pl.get('execution_contract') == 1
                                     and old_pl.get('execution_generation') != execution)
                                 or source_changed)
                if execution is not None and existing['state'] == 'done' and not input_changed:
                    # Enumeration is not a request to obtain a different
                    # answer from this already evaluated input generation.
                    continue
                pl[VERSION_KEY] = CURRENT_VERSION
                if execution is not None:
                    pl['execution_generation'] = execution
                    pl['execution_contract'] = 1
                    if not preserve_live:
                        pl['reprocess_only'] = True
                payload_changed = (input_changed
                                   or old_origin != pl.get("origin")
                                   or old_eligible != bool(pl.get("eligible"))
                                   or old_notification_free
                                   != bool(pl.get("notification_free")))
                # A missing generation is a legacy row.  Add one before any
                # worker can observe it, so old and new payloads are never
                # confused by an ID-only check.
                if payload_changed or existing["state"] != "pending" \
                        or not isinstance(pl.get("generation"), str):
                    pl["generation"] = uuid.uuid4().hex
                pl["source_generation"] = source_generation
                attempts = (0 if input_changed
                             else int(existing["attempts"] or 0))
                if input_changed:
                    self._invalidate_thread_projections(
                        project_id, root, invalidated_roots=invalidated_roots,
                        source_changed=source_changed and execution is None)
                    # A source edit is a new budget and may not inherit a
                    # human retry extension or the command token that
                    # invalidated the prior worker.
                    pl.pop("manual_attempt_limit", None)
                    pl.pop("retry_command_id", None)
                    state = "pending"
                else:
                    from semantic_runtime import attempt_limit
                    state = "pending"
                    if (existing["state"] in {"failed", "pending"}
                            and attempts >= attempt_limit(pl)):
                        # Re-seeding the same exhausted input must not revive
                        # a job that only an explicit human retry may extend.
                        state = "failed"
                self.db.execute(
                    "UPDATE fetch_jobs SET state=?,payload=?,"
                    "attempts=?,next_try=?,updated_at=? WHERE job_id=?",
                    (state, json.dumps(pl, ensure_ascii=False, sort_keys=True),
                     attempts, now, now, existing["job_id"]))
            else:
                payload = {"targets": sorted(set(ids)),
                           VERSION_KEY: CURRENT_VERSION,
                           "origin": origin,
                           "generation": uuid.uuid4().hex,
                           "source_generation": source_generation}
                if execution is not None:
                    payload['execution_generation'] = execution
                    payload['execution_contract'] = 1
                    payload['reprocess_only'] = True
                if eligible:
                    payload["eligible"] = True
                elif notification_free:
                    payload["notification_free"] = True
                self.db.execute("""
                  INSERT INTO fetch_jobs(kind,project_id,message_id,parent_id,
                    payload,state,next_try,created_at,updated_at)
                  VALUES(?,?,?,?,?,'pending',?,?,?)
                """, ("semantic", project_id, root, None,
                      json.dumps(payload, ensure_ascii=False, sort_keys=True),
                      now, now, now))

    def semantic_seed(self, project_id: int, message_ids: list,
                      origin: dict):
        """Standalone seed for explicit replay — same job shape as the
        ingest path, committed on its own (spec §18.5)."""
        with self.db:
            self.semantic_seed_tx(project_id, message_ids, origin)
