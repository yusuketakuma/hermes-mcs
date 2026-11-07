"""Persist captured patient messages, attachments and observations on the owning ledger connection."""
import attachment_followups
import hashlib
import json
import math
import os
import sqlite3
import time
from contextlib import suppress
from time import monotonic

import initial_import_state
from mcs_util import RETRY_LATER_4XX, html_to_text, loads_dict, valid_hash
from mcs_queries import posted_epoch


def _patient_name_for_write(patient, previous):
    """Apply name presence without joining observed and stored components."""
    from mcs_contract import valid_id
    presence = getattr(patient, "patient_name_presence", None)
    if patient.project_type != "medical" or presence is None:
        return patient.patient_name
    observed_id = getattr(patient, "patient_name_karte_id", None)
    incoming_id = getattr(patient, "karte_id", None)
    prior_id = previous["karte_id"] if previous is not None else None
    if valid_id(incoming_id) and observed_id != incoming_id:
        return ""
    # An unverified or different observation must not attach a name to an
    # existing medical ID; archived observations do not update karte_id.
    if valid_id(prior_id) and not valid_id(incoming_id):
        if valid_id(observed_id) and observed_id != prior_id:
            return ""
        if not valid_id(observed_id):
            return ""
    if "missing" not in presence:
        return patient.patient_name
    if any(part in ("null", "empty") for part in presence):
        return ""
    same_scope = (previous is not None and previous["project_type"] == "medical"
                  and valid_id(prior_id) and valid_id(observed_id)
                  and prior_id == observed_id)
    if same_scope and previous["patient_name"]:
        return previous["patient_name"]
    return patient.patient_name


# body_state values meaning "nothing more to fetch" — shared by the
# ledger's job-reconciliation and the walk checkpoint logic
TERMINAL_BODY_STATES = ("full", "deleted")


# attachment download failures that retrying the SAME url can never fix
# (a fresh url from the server revives the row — see _save_attachments)
_ATTACH_PERMANENT = frozenset({
    "download_too_large", "url_not_allowed", "download_empty"})


def message_write_failure_diagnostic(value):
    """Allow only fixed SQLite writer-reservation diagnostics, never exception text."""
    if type(value) is not dict or value.get("phase") != "message_write_reserve":
        return None
    code = value.get("sqlite_errorcode")
    code = code if type(code) is int and 0 < code <= 0x7fffffff else None
    name = value.get("sqlite_errorname")
    primary_names = ("ERROR", "INTERNAL", "PERM", "ABORT", "BUSY", "LOCKED",
                     "NOMEM", "READONLY", "INTERRUPT", "IOERR", "CORRUPT",
                     "NOTFOUND", "FULL", "CANTOPEN", "PROTOCOL", "EMPTY",
                     "SCHEMA", "TOOBIG", "CONSTRAINT", "MISMATCH", "MISUSE",
                     "NOLFS", "AUTH", "FORMAT", "RANGE", "NOTADB", "NOTICE", "WARNING")
    if not (type(name) is str and code is not None
            and any(name == "SQLITE_" + base or name.startswith("SQLITE_" + base + "_")
                    for base in primary_names)
            and getattr(sqlite3, name, None) == code):
        name = None
    return {
        "phase": "message_write_reserve",
        "sqlite_errorcode": code, "sqlite_errorname": name,
        "transaction_open_before": (value.get("transaction_open_before")
                                    if type(value.get("transaction_open_before")) is bool else None),
        **{key: (value.get(key) if type(value.get(key)) in (int, float)
                 and 0 <= value[key] <= 1e20 and math.isfinite(value[key]) else None)
           for key in ("elapsed_s", "at")},
    }


class LedgerCaptureMixin:
    """Store original capture data while Ledger owns connection and intake transaction lifetime."""

    def _save_attachments(self, m, now: float):
        import attachment_ocr
        deleted = m.body_state == "deleted"
        if m.body_state not in TERMINAL_BODY_STATES:
            # Partial observations cannot undo a stored deletion.
            deleted = self.db.execute("SELECT 1 FROM messages WHERE message_id=? AND body_state='deleted'",
                                      (m.message_id,)).fetchone() is not None
        dependencies = attachment_ocr.capture_dependencies_tx(self.db, message_id=m.message_id)
        for a in m.attachments:
            saved = self.db.execute(
                "SELECT local_path FROM attachments WHERE message_id=? AND file_id=? "
                "AND state='withdrawn'", (m.message_id, a.file_id)).fetchone()
            has_payload = bool(saved and isinstance(saved[0], str)
                               and os.path.isfile(saved[0]))
            self.db.execute("""
              INSERT INTO attachments
                (message_id,file_id,name,url,created_at)
              VALUES(?,?,?,?,?)
              ON CONFLICT(message_id,file_id) DO UPDATE SET
                name=COALESCE(NULLIF(excluded.name,''),attachments.name),
                url=COALESCE(NULLIF(excluded.url,''),attachments.url),
                -- a file listed again after being withdrawn is current
                -- server-side: reuse only a payload that still exists.
                -- A FRESH url (signed links rotate) revives a row that
                -- failed on the stale one — back to pending, attempts
                -- cleared (F11)
                state=CASE WHEN attachments.state='withdrawn'
                           THEN CASE WHEN ?
                                     THEN 'downloaded' ELSE 'pending' END
                           WHEN attachments.state IN ('pending','failed')
                                AND NULLIF(excluded.url,'') IS NOT NULL
                                AND excluded.url <> attachments.url
                           THEN 'pending'
                           ELSE attachments.state END,
                attempts=CASE WHEN attachments.state='withdrawn' THEN 0
                              WHEN attachments.state IN ('pending','failed')
                                AND NULLIF(excluded.url,'') IS NOT NULL
                                AND excluded.url <> attachments.url
                              THEN 0 ELSE attachments.attempts END,
                next_try=CASE WHEN attachments.state='withdrawn' THEN NULL
                              WHEN attachments.state IN ('pending','failed')
                                AND NULLIF(excluded.url,'') IS NOT NULL
                                AND excluded.url <> attachments.url
                              THEN NULL ELSE attachments.next_try END,
                error=CASE WHEN attachments.state='withdrawn' THEN NULL
                           WHEN attachments.state IN ('pending','failed')
                                AND NULLIF(excluded.url,'') IS NOT NULL
                                AND excluded.url <> attachments.url
                           THEN NULL ELSE attachments.error END
            """, (m.message_id, a.file_id, a.name, a.url, now, has_payload))
        # reconcile the set only when the response enumerated `files`
        # (complete list, possibly empty) or the post is deleted —
        # a response that simply omitted the key must not withdraw rows
        if not getattr(m, "files_present", False) \
                and not deleted:
            attachment_ocr.record_dependencies_tx(self.db, dependencies)
            return
        keep = [a.file_id for a in m.attachments if not deleted]
        if keep:
            self.db.execute(f"""
              UPDATE attachments SET state='withdrawn'
              WHERE message_id=? AND state NOT IN ('withdrawn')
                AND file_id NOT IN ({','.join('?' * len(keep))})
            """, (m.message_id, *keep))
        else:
            self.db.execute("""
              UPDATE attachments SET state='withdrawn'
              WHERE message_id=? AND state != 'withdrawn'
            """, (m.message_id,))
        attachment_ocr.record_dependencies_tx(self.db, dependencies)

    def _save_tree(self, m, new_ids: list, now: float,
                   project_id: int | None = None):
        """One message + its attachments + replies (with their attachments).
        Replies carry files too — a single shared path keeps them from
        silently dropping."""
        expected_project = m.project_id if project_id is None else project_id
        for message in [m, *m.replies]:
            if message.project_id != expected_project:
                raise ValueError("message_project_mismatch")
            if message.parent_id and not message.is_unread \
                    and self._reply_job_unread(message):
                # acknowledged by thread-read before it was stored: the
                # job carries the unread evidence the fetch cannot
                message.is_unread = True
            if self._upsert_message(message):
                new_ids.append(message.message_id)
            self._save_attachments(message, now)
            self._retire_reply_job(message, now)

    def save_patient(self, p, notify: dict | None = None,
                     semantic: bool = False,
                     notify_max_age_s: float | None = None,
                     observation_ts: int | None = None) -> list:
        """Whole patient block + optional notify intent in ONE transaction —
        a crash between message save and outbox insert must not be able to
        lose the notification (Oracle B11). notify is a payload template;
        message_ids is filled with the new ids. semantic=True also seeds
        durable semantic-eval jobs for the same messages in the SAME
        commit (INV-06).

        notify_max_age_s drops messages posted more than this many
        seconds ago from the notification (bulk-added patients surface
        as 'unread' with months-old history — those are imported and
        consumed, never announced). A NULL/unparseable posted_at cannot
        be proven old, so it still notifies. Stale ids are marked
        notified so they never resurface as candidates."""
        now = time.time()
        new_ids = []
        before_semantic = (self._semantic_generation_snapshot(p.messages)
                           if semantic or self._needs_semantic_source_tracking() else {})
        invalidated_roots = set()
        with self._intake_transaction():  # commit on success, rollback on exception
            self.db.execute('UPDATE patients SET project_id=project_id WHERE 0')
            previous = self.db.execute("SELECT fetch_reason,project_type,karte_id,is_archived,fetch_state,patient_name FROM patients WHERE project_id=?", (p.project_id,)).fetchone()
            first = previous is None
            import semantic_intake_hold
            unstored = semantic_intake_hold.has_unstored(self.db, p.project_id)
            fetch_state = ('forbidden' if unstored and (p.fetch_state == 'forbidden'
                           or previous is not None and previous[4] == 'forbidden')
                           else 'incomplete' if unstored else p.fetch_state)
            restricted = ('forbidden', 'permission_denied', 'access_revoked')
            if unstored and p.fetch_reason in restricted:
                fetch_reason = p.fetch_reason
            elif unstored and previous is not None and previous[0] in restricted:
                fetch_reason = previous[0]
            else:
                fetch_reason = 'semantic_intake_unstored' if unstored else p.fetch_reason
            import inference_dependency_loss as dependency_loss
            old_karte = (dependency_loss.capture_current_karte_tx(self.db, p.project_id)
                if previous is not None and getattr(p, 'karte_id', None) is not None
                and previous['karte_id'] != p.karte_id else None)
            self.db.execute("""
              INSERT INTO patients(project_id,project_type,patient_name,disease,
                station_name,url,karte_id,fetch_state,fetch_reason,
                last_complete_fetch,last_seen,created_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(project_id) DO UPDATE SET
                group_id=CASE WHEN patients.project_type IS NOT excluded.project_type
                              THEN NULL ELSE patients.group_id END,
                group_name=CASE WHEN patients.project_type IS NOT excluded.project_type
                                THEN NULL ELSE patients.group_name END,
                project_type=excluded.project_type,
                patient_name=excluded.patient_name,
                disease=excluded.disease,
                station_name=excluded.station_name,
                url=excluded.url,
                karte_id=COALESCE(excluded.karte_id, patients.karte_id),
                fetch_state=excluded.fetch_state,
                fetch_reason=excluded.fetch_reason,
                last_complete_fetch=COALESCE(excluded.last_complete_fetch,
                                             patients.last_complete_fetch),
                last_seen=excluded.last_seen,
                -- the unread path only sees live projects: presence here
                -- is positive proof of reactivation (Oracle F4)
                is_archived=0
            """, (p.project_id, p.project_type, _patient_name_for_write(p, previous), p.disease,
                  p.station_name, p.url, getattr(p, "karte_id", None),
                  fetch_state, fetch_reason,
                  now if fetch_state == "complete" else None,
                  now, now))
            if getattr(p, "project_type", None) == "group":
                self._update_group_metadata_tx(p)
            if old_karte is not None:
                dependency_loss.record_tx(self.db, old_karte)
            if (previous is None or (previous[0] == 'forbidden') != (fetch_reason == 'forbidden')
                    or previous[1] != p.project_type or previous[3]
                    or getattr(p, 'karte_id', None) is not None and previous[2] != p.karte_id):
                self.bump_inference_source_epoch_tx()
            if first and notify and observation_ts is not None:
                self._start_initial_import_tx(p.project_id, observation_ts, now)
            for m in p.messages:
                self._save_tree(m, new_ids, now, p.project_id)
            changed_semantic = (self._semantic_changed_ids(
                before_semantic, invalidated_roots=invalidated_roots)
                                if before_semantic else {})
            # The unread endpoint makes all newly stored posts eligible;
            # previously stored posts are eligible only when THIS fetch
            # still reports them unread (the stored flag is sticky).
            fresh_unread = [m.message_id for m in p.messages
                            if m.is_unread] + [
                t.message_id for m in p.messages for t in m.replies
                if t.is_unread]
            notify_ids = (list(dict.fromkeys(
                new_ids + self._unnotified(fresh_unread))) if notify else [])
            self._queue_saved_messages_tx(
                p.project_id, new_ids, changed_semantic.get(p.project_id, []),
                notify_ids, notify=notify, semantic=semantic, now=now,
                notify_max_age_s=notify_max_age_s,
                invalidated_roots=invalidated_roots)
        return new_ids

    def _queue_saved_messages_tx(self, project_id, new_ids, changed_ids,
                                  notify_ids, *, notify, semantic, now,
                                  notify_max_age_s, invalidated_roots: set | None = None):
        """Keep notification eligibility and semantic coverage in the save
        transaction for unread, backfill and reply arrivals alike."""
        history = initial_import_state.classify_tx(
            self, project_id, [*new_ids, *changed_ids, *notify_ids], now)
        history_updates = sorted(history & (set(new_ids) | set(changed_ids)
                                            | set(self._unnotified(notify_ids))))
        if history_updates:
            initial_import_state.emit_tx(
                self, project_id, initial_import_state.load(self.db, project_id), history_updates)
            self._mark_notified(history_updates, now)
        initial_import_state.refresh_capture_tx(self, project_id, now)
        notify_ids = [mid for mid in notify_ids if mid not in history]
        if not (notify or semantic):
            return
        if self.is_archived(project_id):
            seed_ids = sorted(set(new_ids) | set(changed_ids))
            if semantic and seed_ids:
                self.semantic_seed_tx(project_id, seed_ids, {
                    "source": "history_import", "notification_free": True},
                    invalidated_roots=invalidated_roots)
            return
        origin = {"source": "history_import"}
        if notify:
            origin = {"source": notify.get("source")}
            # Recent arrivals also notify when the server already marks
            # them read. A bounded age and known timestamp keep historical
            # imports from announcing old or undated read context.
            if new_ids and notify_max_age_s is not None:
                recent = self.db.execute(
                    "SELECT message_id FROM messages WHERE message_id IN ("
                    + ",".join("?" * len(new_ids))
                    + ") AND posted_at_ts >= ?",
                    [*new_ids, now - notify_max_age_s]).fetchall()
                notify_ids = list(dict.fromkeys(
                    notify_ids + self._unnotified([r[0] for r in recent if r[0] not in history])))
            # Opted-in realtime notification paths cover every new
            # reply. Discovery still age-filters old replies on the first import.
            reply_ids = []
            if (self.notify_all_replies and new_ids
                    and notify.get("source") != "discovery"):
                replies = self.db.execute(
                    "SELECT message_id FROM messages WHERE message_id IN ("
                    + ",".join("?" * len(new_ids))
                    + ") AND parent_id IS NOT NULL AND parent_id != 0",
                    new_ids).fetchall()
                reply_ids = self._unnotified([r[0] for r in replies if r[0] not in history])
            unbounded = set(reply_ids)
            notify_ids = self._filter_notify_age(
                [mid for mid in notify_ids if mid not in unbounded],
                now, notify_max_age_s)
            notify_ids = list(dict.fromkeys(notify_ids + reply_ids))
            if notify_ids:
                payload = dict(notify, message_ids=notify_ids)
                origin["event_id"] = self._outbox_insert(
                    "new_messages", project_id, payload)
                self._mark_notified(notify_ids, now)
        if semantic:
            # Every new/changed post is evaluation input, including
            # already-read and age-filtered arrivals. Notification
            # eligibility is separately derived from the origin event.
            seed_ids = sorted(set(new_ids) | set(notify_ids) | set(changed_ids))
            history_seed = [mid for mid in seed_ids if mid in history]
            live_seed = [mid for mid in seed_ids if mid not in history]
            if history_seed:
                history_origin = {"source": "initial_history", "notification_free": True}
                self.semantic_seed_tx(project_id, history_seed, history_origin,
                                      invalidated_roots=invalidated_roots)
            if live_seed:
                self.semantic_seed_tx(project_id, live_seed, origin,
                                      invalidated_roots=invalidated_roots,
                                      _eligible_ids=set(notify_ids))

    def _filter_notify_age(self, ids: list, now: float,
                           max_age_s: float | None) -> list:
        """Drop ids posted more than max_age_s ago from the candidate
        set — bulk-added patients surface as 'unread' carrying
        months-old history; those are imported and consumed, never
        announced. NULL/unparseable posted_at cannot be proven old, so
        it is kept. Stale ids are marked notified in the SAME commit so
        a later fetch reporting them unread cannot resurrect them.
        Shared by every notify path (unread, backfill, reply_job) —
        an old post is equally stale no matter which fetch saw it.
        Caller holds `with self.db`."""
        if not ids or max_age_s is None:
            return ids
        q = ("SELECT message_id FROM messages WHERE message_id IN ("
             + ",".join("?" * len(ids)) + ") AND "
             "(posted_at_ts IS NULL OR posted_at_ts=0 "
             "OR posted_at_ts >= ?)")
        keep = {r["message_id"] for r in self.db.execute(
            q, (*ids, now - max_age_s))}
        stale = [i for i in ids if i not in keep]
        self._mark_notified(stale, now)
        return [i for i in ids if i in keep]

    def _unnotified(self, ids: list) -> list:
        """Of caller-selected candidates, return rows never notified.
        Caller holds `with self.db`."""
        if not ids:
            return []
        q = ("SELECT message_id FROM messages WHERE notified_at IS NULL "
             "AND message_id IN (" + ",".join("?" * len(ids)) + ")")
        return [r["message_id"] for r in self.db.execute(q, ids)]

    def _mark_notified(self, ids: list, now: float):
        """Caller holds `with self.db` — same commit as the outbox intent,
        so a crash cannot produce a notified-but-unqueued message."""
        if not ids:
            return
        self.db.execute(
            "UPDATE messages SET notified_at=? WHERE message_id IN ("
            + ",".join("?" * len(ids)) + ")", [now, *ids])

    def save_messages(self, msgs, project_id: int | None = None,
                      notify: dict | None = None,
                      semantic: bool = False,
                      notify_max_age_s: float | None = None,
                      notify_all_new: bool = False,
                      notify_new_after_ts: float | None = None) -> list:
        """Backfill path: upsert messages (+reply attachments) without
        touching patient fetch_state. Optional notify intent lands in the
        same transaction. Returns ids of newly-inserted messages.
        notify_max_age_s drops stale posts from the intent (bulk-added
        patients carry months-old unread history).
        notify_all_new widens notification eligibility from this fetch's
        unread posts to every newly-stored row — used by the self-post
        probe, whose whole point is catching posts the unread set can
        never contain (own posts are is_unread=0 by definition).
        With notify_max_age_s, newly stored read posts with a known
        recent timestamp also qualify through the shared save gate.
        notify_all_replies on the ledger exempts new replies from the age
        and unread gates only when this call supplies a notify template.
        notify_new_after_ts widens eligibility like notify_all_new, but
        only to new rows posted strictly after it (a page-limited
        self-post probe's cutoff; older history stays quiet)."""
        new_ids = []
        now = time.time()
        before_semantic = (self._semantic_generation_snapshot(msgs)
                           if semantic or self._needs_semantic_source_tracking() else {})
        invalidated_roots = set()
        with self._intake_transaction():
            for m in msgs:
                self._save_tree(m, new_ids, now, project_id)
            changed_semantic = (self._semantic_changed_ids(
                before_semantic, invalidated_roots=invalidated_roots)
                                if before_semantic else {})
            if project_id:
                # Previously stored rows qualify when freshly unread;
                # the shared gate also admits new recent read arrivals.
                fresh_unread = [m.message_id for m in msgs
                                if m.is_unread] + [
                    t.message_id for m in msgs for t in m.replies
                    if t.is_unread]
                if notify and notify.get("source") == "discovery":
                    # A quiet import may have stored these before the first notify walk.
                    fresh_unread = [m.message_id for m in msgs] + [
                        t.message_id for m in msgs for t in m.replies]
                if notify_all_new:
                    fresh_unread = list(dict.fromkeys(
                        fresh_unread + new_ids))
                elif notify_new_after_ts is not None and new_ids:
                    fresh_unread = list(dict.fromkeys(fresh_unread + [
                        r[0] for r in self.db.execute(
                            "SELECT message_id FROM messages WHERE message_id IN ("
                            + ",".join("?" * len(new_ids))
                            + ") AND posted_at_ts > ?",
                            [*new_ids, notify_new_after_ts])]))
                notify_ids = self._unnotified(fresh_unread) if notify else []
                self._queue_saved_messages_tx(
                    project_id, new_ids, changed_semantic.get(project_id, []),
                    notify_ids, notify=notify, semantic=semantic, now=now,
                    notify_max_age_s=notify_max_age_s,
                    invalidated_roots=invalidated_roots)
            else:
                # init_data saves mixed projects without requesting individual notifications.
                grouped = {}
                for message in msgs:
                    for row in [message, *message.replies]:
                        grouped.setdefault(row.project_id, []).append(row.message_id)
                for pid, ids in grouped.items():
                    touched = set(ids)
                    self._queue_saved_messages_tx(
                        pid, [mid for mid in new_ids if mid in touched], changed_semantic.get(pid, []),
                        ids, notify=None, semantic=semantic, now=now,
                        notify_max_age_s=notify_max_age_s,
                        invalidated_roots=invalidated_roots)
        return new_ids

    def save_thread_replies(self, replies: list, project_id: int,
                            notify: dict | None = None,
                            semantic: bool = False,
                            notify_max_age_s: float | None = None) -> list:
        """Reply-job drain path: persist a fetched thread's replies AND
        reconcile their reply-job states in the SAME commit (Oracle F05).
        A reply whose body is now terminal retires its queued job (a
        burnt-out one would otherwise block floor certification forever);
        a still-incomplete reply reserves a durable retry.
        notify_max_age_s drops stale replies from the intent — a thread
        drained for a bulk-added patient can carry months-old unread
        replies that must not announce. New read replies with a known
        timestamp inside the bound also qualify for notification;
        notify_all_replies exempts new replies from that bound."""
        new_ids = []
        now = time.time()
        before_semantic = (self._semantic_generation_snapshot(replies)
                           if semantic or self._needs_semantic_source_tracking() else {})
        invalidated_roots = set()
        with self._intake_transaction():
            for m in replies:
                self._save_tree(m, new_ids, now, project_id)
                # reconcile against the STORED body, not the fetched one:
                # upsert never downgrades 'full', so a refetch returning
                # a snippet for an already-full reply must not spawn an
                # endless retry job — and any stale job for it must be
                # retired here (_save_tree's retire only fires on a
                # terminal FETCH state)
                st = self.db.execute(
                    "SELECT body_state s FROM messages WHERE message_id=?",
                    (m.message_id,)).fetchone()
                if st and st["s"] in TERMINAL_BODY_STATES:
                    self._retire_reply_job_id(project_id, m.message_id, now)
                else:
                    # never revive a failed sibling: two replies the
                    # thread API keeps returning as snippets would
                    # otherwise reset each other's attempts forever and
                    # pending_reply_jobs could never reach 0
                    self.job_add_tx("reply", project_id, m.message_id,
                                     parent_id=m.parent_id,
                                     revive_failed=False)
            changed_semantic = (self._semantic_changed_ids(
                before_semantic, invalidated_roots=invalidated_roots)
                                if before_semantic else {})
            notify_ids = (self._unnotified(
                [m.message_id for m in replies if m.is_unread]) if notify else [])
            self._queue_saved_messages_tx(
                project_id, new_ids, changed_semantic.get(project_id, []),
                notify_ids, notify=notify, semantic=semantic, now=now,
                notify_max_age_s=notify_max_age_s,
                invalidated_roots=invalidated_roots)
        return new_ids

    def _upsert_message(self, m) -> int:
        """Returns 1 if newly inserted. Never downgrades a stored 'full' body
        to a later 'snippet'."""
        now = time.time()
        chash = hashlib.sha256((m.body_html or "").encode()).hexdigest()
        # A zero-row write reserves the writer without committing caller work.
        started = transaction_open = None
        with suppress(Exception):
            transaction_open = self.db.in_transaction
            started = monotonic()
        try:
            self.db.execute("UPDATE messages SET message_id=message_id WHERE 0")
        except sqlite3.Error as exc:
            with suppress(Exception):  # Diagnostic failure must preserve the original exception.
                exc._mcs_message_write_failure = message_write_failure_diagnostic({
                    "phase": "message_write_reserve",
                    "sqlite_errorcode": getattr(exc, "sqlite_errorcode", None),
                    "sqlite_errorname": getattr(exc, "sqlite_errorname", None),
                    "transaction_open_before": transaction_open,
                    "elapsed_s": None if started is None else monotonic() - started,
                    "at": time.time(),
                })
            raise
        existing = self.db.execute(
            "SELECT * FROM messages WHERE message_id=?",
            (m.message_id,)).fetchone()
        if existing is not None and existing["project_id"] != m.project_id:
            raise ValueError("message_project_mismatch")
        import inference_dependency_loss as dependency_loss
        body_text = html_to_text(m.body_html)
        possible_change = existing is not None and (
            m.body_state in TERMINAL_BODY_STATES and (existing['content_hash'] != chash or existing['body_state'] != m.body_state
                or m.body_state == 'full' and existing['body_text'] != body_text)
            or m.body_state not in TERMINAL_BODY_STATES and existing['body_state'] not in TERMINAL_BODY_STATES
                and (existing['content_hash'] != chash or existing['body_state'] != m.body_state or existing['body_text'] != body_text)
            or m.parent_id is not None and m.parent_id != existing['parent_id']
            or m.sender_id is not None and m.sender_id != existing['sender_id']
            or any(getattr(m, field) and getattr(m, field) != existing[field]
                   for field in ('sender_name', 'sender_type', 'profession', 'organization'))
            or posted_epoch(m.posted_at) is not None and (m.posted_at != existing['posted_at']
                or posted_epoch(m.posted_at) != existing['posted_at_ts']))
        old_binding = dependency_loss.source_binding(self.db, m.project_id, m.message_id) if possible_change else None
        identity_changed = existing is not None and (
            existing['sender_id'] is not None and m.sender_id is not None and m.sender_id != existing['sender_id']
            or existing['sender_type'] and m.sender_type and m.sender_type != existing['sender_type'])
        support_possible = existing is not None and (
            m.body_state in TERMINAL_BODY_STATES and (existing['body_text'] != ('' if m.body_state == 'deleted' else body_text)
                or m.body_state == 'deleted' and existing['body_state'] != 'deleted')
            or m.parent_id is not None and m.parent_id != existing['parent_id']
            or identity_changed
            or posted_epoch(m.posted_at) is not None and posted_epoch(m.posted_at) != existing['posted_at_ts'])
        old_support = dependency_loss.support_binding(self.db, m.project_id, m.message_id) if support_possible else None
        old_origin = ({'message_id': m.message_id, 'source_key': m.message_id,
            'content_hash': existing['content_hash'], 'binding': old_binding,
            'revision': self.db.execute('SELECT COALESCE(MAX(seq),1) FROM message_revisions WHERE message_id=?',
                                       (m.message_id,)).fetchone()[0]} if possible_change and valid_hash(existing['content_hash']) else None)
        old_dependencies = (dependency_loss.capture_tx(self.db, m.project_id, m.message_id)
                            if support_possible and old_origin is not None else None)
        import attachment_ocr
        ocr_dependencies = (attachment_ocr.capture_dependencies_tx(self.db, message_id=m.message_id)
                            if possible_change else [])
        edited = (existing is not None and existing["content_hash"] is not None
                  and existing["body_state"] in TERMINAL_BODY_STATES
                  and m.body_state in TERMINAL_BODY_STATES
                  and existing["content_hash"] != chash)
        # unparseable posted_at -> NULL so COALESCE keeps the stored epoch
        # instead of overwriting a valid value with 0 (Oracle T31)
        posted_ts = posted_epoch(m.posted_at)
        self.db.execute("""
          INSERT INTO messages(message_id,project_id,parent_id,sender_id,
            sender_name,sender_type,profession,organization,posted_at,
            posted_at_ts,is_unread,body_html,body_text,body_state,
            content_hash,reply_count,first_seen,updated_seen)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(message_id) DO UPDATE SET
            -- history/snippet responses may omit metadata; keep the
            -- authoritative values already hydrated in the ledger.
            parent_id=COALESCE(excluded.parent_id, messages.parent_id),
            sender_id=COALESCE(excluded.sender_id, messages.sender_id),
            sender_name=CASE
              WHEN excluded.sender_name != '' THEN excluded.sender_name
              ELSE messages.sender_name END,
            sender_type=CASE
              WHEN excluded.sender_type != '' THEN excluded.sender_type
              ELSE messages.sender_type END,
            profession=CASE
              WHEN excluded.profession != '' THEN excluded.profession
              ELSE messages.profession END,
            organization=CASE
              WHEN excluded.organization != '' THEN excluded.organization
              ELSE messages.organization END,
            posted_at=CASE
              WHEN excluded.posted_at_ts IS NOT NULL THEN excluded.posted_at
              ELSE messages.posted_at END,
            body_html=CASE
              WHEN excluded.body_state='deleted' THEN ''
              WHEN excluded.body_state='full' THEN excluded.body_html
              WHEN messages.body_state IN ('full','deleted') THEN messages.body_html
              ELSE excluded.body_html END,
            body_text=CASE
              WHEN excluded.body_state='deleted' THEN ''
              WHEN excluded.body_state='full' THEN excluded.body_text
              WHEN messages.body_state IN ('full','deleted') THEN messages.body_text
              ELSE excluded.body_text END,
            body_state=CASE
              WHEN excluded.body_state='deleted' THEN 'deleted'
              WHEN excluded.body_state='full' THEN 'full'
              WHEN messages.body_state IN ('full','deleted') THEN messages.body_state
              ELSE excluded.body_state END,
            is_unread=MAX(COALESCE(messages.is_unread,0),
                          COALESCE(excluded.is_unread,0)),
            posted_at_ts=COALESCE(excluded.posted_at_ts,
                                  messages.posted_at_ts),
            content_hash=CASE
              WHEN excluded.body_state='deleted' THEN excluded.content_hash
              WHEN excluded.body_state='full' THEN excluded.content_hash
              WHEN messages.body_state IN ('full','deleted') THEN messages.content_hash
              ELSE excluded.content_hash END,
            reply_count=excluded.reply_count,
            updated_seen=excluded.updated_seen
        """, (m.message_id, m.project_id, m.parent_id, m.sender_id,
              m.sender_name, m.sender_type, m.profession, m.organization,
              m.posted_at, posted_ts, int(bool(m.is_unread)),
              m.body_html, body_text, m.body_state, chash, m.reply_count,
              now, now))
        self._save_message_metadata(m, source="capture", now=now,
                                    body_changed=edited)
        source_changed = possible_change and old_binding != dependency_loss.source_binding(self.db, m.project_id, m.message_id)
        if source_changed:
            # Only adopted source inputs advance the occurrence; a retained snippet,
            # unread flag or unchanged capture is not a new source generation.
            # Seed real prior content only: legacy NULL hashes never identify a body.
            current = self.db.execute("SELECT content_hash,body_state,body_text FROM messages WHERE message_id=?",
                                      (m.message_id,)).fetchone()
            # A retained legacy full body may still lack its hash after a snippet.
            # It cannot support a valid reference; wait for real full hydration.
            if valid_hash(current['content_hash']):
                if valid_hash(existing['content_hash']):
                    self.db.execute("INSERT OR IGNORE INTO message_revisions "
                        "(message_id,seq,content_hash,prev_content_hash,body_state,observed_at) VALUES(?,1,?,NULL,?,?)",
                        (m.message_id, existing['content_hash'], existing['body_state'] or ('full' if existing['body_text'] else 'unknown'),
                         existing['first_seen'] or now))
                previous = self.db.execute("SELECT seq,content_hash FROM message_revisions WHERE message_id=? "
                                           "ORDER BY seq DESC LIMIT 1", (m.message_id,)).fetchone()
                self.db.execute("INSERT INTO message_revisions "
                    "(message_id,seq,content_hash,prev_content_hash,body_state,observed_at) VALUES(?,?,?,?,?,?)",
                    (m.message_id, previous['seq'] + 1 if previous else 1, current['content_hash'],
                     previous['content_hash'] if previous else None, current['body_state'] or ('full' if current['body_text'] else 'unknown'), now))
            if old_dependencies is not None and (identity_changed or old_support != dependency_loss.support_binding(self.db, m.project_id, m.message_id)):
                if not any(old_dependencies.get(field) for field in ('records', 'gaps', 'observed')):
                    # No earlier clinical body exists to lose (empty/deleted source).
                    old_dependencies['observed'] = [{**old_origin, 'kind': 'empty_source', 'artifact_id': None}]
                dependency_loss.record_tx(self.db, old_dependencies)
            elif old_origin is not None:
                dependency_loss.equivalent_tx(self.db, m.project_id, m.message_id, old_origin)
            invalidation_scope = ("WHERE message_id=? AND (project_id=? OR project_id IS NULL) AND kind IN "
                "('extract_v1','extract_llm','extract_llm_chunk','semantic_source_manifest',"
                "'semantic_extraction_chunk','patient_context_scan_v1','patient_context_scan_chunk_v1') "
                "AND CASE WHEN json_valid(meta) THEN json_type(meta)='object' "
                "AND json_extract(meta,'$.invalidated') IS NOT 1 END")
            # Intake often has only thread projections. Avoid a no-op UPDATE
            # of the artifact store for every member of those same threads.
            if self.db.execute("SELECT 1 FROM artifacts " + invalidation_scope + " LIMIT 1",
                               (m.message_id, m.project_id)).fetchone():
                self.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.invalidated',json('true')) "
                                + invalidation_scope, (m.message_id, m.project_id))
        if existing is not None and (edited or m.body_state in TERMINAL_BODY_STATES and existing["body_state"] != m.body_state):
            initial_import_state.dirty_source_tx(self.db, m.project_id, m.message_id, now)
        attachment_ocr.record_dependencies_tx(self.db, ocr_dependencies)
        return 0 if existing is not None else 1

    def _save_message_metadata(self, m, *, source, now, body_changed=False):
        metadata = {k: v for k, v in getattr(m, "metadata", {}).items()
                    if k != initial_import_state.META_KEY}
        errors = getattr(m, "metadata_errors", [])
        if (not metadata and not errors and source == "capture"
                and not body_changed):
            return
        row = self.db.execute(
            "SELECT content FROM message_metadata WHERE message_id=? AND source=?",
            (m.message_id, source)).fetchone()
        content = (loads_dict(row["content"]) or {}) if row else {}
        if row and source == "capture" and loads_dict(row["content"]) is None:
            active = self.db.execute(
                "SELECT 1 FROM patients p JOIN messages m ON m.project_id=p.project_id "
                "WHERE m.message_id=? AND p.initial_import IS NOT NULL", (m.message_id,)).fetchone()
            if active:
                raise ValueError("initial_import_membership_invalid")
        if body_changed:
            # a field this capture did not re-observe belongs to the earlier
            # body; keeping it under the new checked_at would re-bind it
            content = {k: v for k, v in content.items()
                       if k in metadata or k == initial_import_state.META_KEY}
        # observed_at marks when this value was first seen: unread posts
        # are re-captured every tick, and refreshing an unchanged value
        # would re-render cards and recount digests each time.
        content.update({key: {"value": value, "observed_at": now}
                        for key, value in metadata.items()
                        if not (isinstance(content.get(key), dict)
                                and content[key].get("value") == value)})
        self.db.execute("""
          INSERT INTO message_metadata(message_id,source,content,checked_at,last_error)
          VALUES(?,?,?,?,?) ON CONFLICT(message_id,source) DO UPDATE SET
            content=excluded.content,checked_at=excluded.checked_at,
            last_error=excluded.last_error
        """, (m.message_id, source, json.dumps(content, ensure_ascii=False),
              now, ",".join(errors) or None))

    def save_metadata_shadow(self, m, *, error=None, publish=False):
        """Save refresh observations without changing chat, coverage or delivery.

        publish=True also writes a successful refresh to capture in the
        same transaction; a failure never touches capture."""
        row = self.db.execute(
            "SELECT project_id FROM messages WHERE message_id=?", (m.message_id,)).fetchone()
        if row is None or row["project_id"] != m.project_id:
            raise ValueError("message_project_mismatch")
        if error is not None:
            from types import SimpleNamespace
            m = SimpleNamespace(message_id=m.message_id, metadata={}, metadata_errors=[error])
        now = time.time()
        with self.db:
            self._save_message_metadata(m, source="shadow", now=now)
            if publish is True and error is None and not getattr(m, "metadata_errors", []):
                self._save_message_metadata(m, source="capture", now=now)

    def save_reaction_actors(self, message_id, rows, complete, *, error=None, now=None):
        """Merge a complete walk into the kept actor history.

        Retention is unlimited (#22-D3, 2026-10-03): a present actor keeps
        its first observed_at and gets its current name, an actor missing
        from a complete walk is marked removed_at (never deleted), and a
        re-press clears it. An incomplete walk records checked_at /
        last_error only — a partial list never proves a cancellation."""
        now = time.time() if now is None else now
        with self.db:
            if complete is True:
                seen = [(r["actor_id"], r["reaction_type"]) for r in rows]
                self.db.executemany("""
                  INSERT INTO message_reaction_actors(
                    message_id,actor_id,reaction_type,profession,observed_at,
                    actor_name,organization,removed_at)
                  VALUES(?,?,?,?,?,?,?,NULL)
                  ON CONFLICT(message_id,actor_id,reaction_type) DO UPDATE SET
                    profession=excluded.profession,
                    actor_name=COALESCE(excluded.actor_name,actor_name),
                    organization=COALESCE(excluded.organization,organization),
                    observed_at=CASE WHEN removed_at IS NULL THEN observed_at
                                     ELSE excluded.observed_at END,
                    removed_at=NULL""", [
                    (message_id, r["actor_id"], r["reaction_type"],
                     r.get("profession") or None, now, r.get("name") or None,
                     r.get("organization") or None) for r in rows])
                # Keep SQLite's original error for invalid row types.
                seen = set(seen)
                for actor, kind in self.db.execute(
                        "SELECT actor_id,reaction_type FROM message_reaction_actors "
                        "WHERE message_id=? AND removed_at IS NULL",
                        (message_id,)).fetchall():
                    if (actor, kind) not in seen:
                        self.db.execute(
                            "UPDATE message_reaction_actors SET removed_at=? WHERE "
                            "message_id=? AND actor_id=? AND reaction_type=?",
                            (now, message_id, actor, kind))
            self.db.execute("""
              INSERT INTO message_reaction_actor_fetch(
                message_id,complete_at,checked_at,last_error) VALUES(?,?,?,?)
              ON CONFLICT(message_id) DO UPDATE SET
                complete_at=COALESCE(excluded.complete_at,complete_at),
                checked_at=excluded.checked_at,last_error=excluded.last_error
            """, (message_id, now if complete is True else None, now,
                  None if complete is True else (error or "incomplete")))

    def attachment_saved(self, attachment_id: int, path: str,
                         nbytes: int, sha256: str, semantic: bool = False):
        with self.db:
            import attachment_ocr
            self.db.execute("UPDATE attachments SET attachment_id=attachment_id WHERE 0")
            dependencies = attachment_ocr.capture_dependencies_tx(self.db, attachment_id=attachment_id)
            row = self.db.execute(
                "SELECT m.project_id,m.message_id,COALESCE(m.parent_id,m.message_id) root "
                "FROM attachments a "
                "JOIN messages m ON m.message_id=a.message_id WHERE attachment_id=?",
                (attachment_id,)).fetchone()
            before = (self._semantic_message_fingerprint(
                row["project_id"], row["message_id"])
                if row and (semantic or self._needs_semantic_source_tracking()) else None)
            now = time.time()
            self.db.execute("""
              UPDATE attachments SET local_path=?,bytes=?,sha256=?,
                state='downloaded',downloaded_at=?,error=NULL
              WHERE attachment_id=?
            """, (path, nbytes, sha256, now, attachment_id))
            attachment_ocr.record_dependencies_tx(self.db, dependencies)
            if row and before is not None and before != self._semantic_message_fingerprint(
                    row["project_id"], row["message_id"]):
                self._invalidate_thread_projections(row["project_id"], row["root"])
                if semantic:
                    self.semantic_seed_tx(row["project_id"], [row["message_id"]],
                                           {"source": "attachment"})
            if row:
                attachment_followups.enqueue_downloaded_attachment_followup_tx(
                    self.db, attachment_id, row["project_id"], row["message_id"], now)

    def attachment_failed(self, attachment_id: int, kind: str,
                          retry_in: float = 900, max_attempts: int = 6,
                          semantic: bool = False):
        """Classified failure recording (F11). PERMANENT kinds (4xx other
        than RETRY_LATER_4XX, oversize, policy blocks) can never succeed on the
        same url -> 'failed' now; a fresh signed url on re-save revives
        the row (see _save_attachments). Transient kinds back off to the
        attempt cap as before."""
        permanent = (
            kind in _ATTACH_PERMANENT
            or (kind.startswith("http_")
                and kind[5:].isdigit()
                and 400 <= int(kind[5:]) < 500
                and int(kind[5:]) not in RETRY_LATER_4XX))
        with self.db:
            import attachment_ocr
            self.db.execute("UPDATE attachments SET attachment_id=attachment_id WHERE 0")
            dependencies = attachment_ocr.capture_dependencies_tx(self.db, attachment_id=attachment_id)
            source = self.db.execute(
                "SELECT m.project_id,m.message_id,a.state,"
                "COALESCE(m.parent_id,m.message_id) root FROM attachments a "
                "JOIN messages m ON m.message_id=a.message_id WHERE attachment_id=?",
                (attachment_id,)).fetchone() if (
                    semantic or self._needs_semantic_source_tracking()) else None
            self.db.execute("""
              UPDATE attachments SET attempts=attempts+1,next_try=?,error=?,
                state=CASE WHEN ? OR attempts+1>=? THEN 'failed'
                           ELSE 'pending' END
              WHERE attachment_id=?
            """, (time.time() + retry_in, kind[:80], permanent,
                  max_attempts, attachment_id))
            attachment_ocr.record_dependencies_tx(self.db, dependencies)
            if source:
                state = self.db.execute("SELECT state FROM attachments WHERE attachment_id=?",
                                        (attachment_id,)).fetchone()[0]
                if state != source["state"]:
                    self._invalidate_thread_projections(source["project_id"], source["root"])
                    if semantic:
                        self.semantic_seed_tx(source["project_id"], [source["message_id"]],
                                               {"source": "attachment"})

    def attachments_due(self, limit: int = 50,
                        priority_mids: list[int] | None = None) -> list:
        """priority_mids: message_ids whose attachments jump the queue —
        unsent notify events need their files before flush() posts."""
        prio = [m for m in (priority_mids or []) if type(m) is int][:500]
        if not prio:
            return self.db.execute("""
              SELECT attachment_id,message_id,file_id,name,url FROM attachments
              WHERE state='pending' AND url != ''
                AND COALESCE(next_try,0) <= ?
              ORDER BY attachment_id LIMIT ?
            """, (time.time(), limit)).fetchall()
        ph = ",".join("?" * len(prio))
        return self.db.execute(f"""
          SELECT attachment_id,message_id,file_id,name,url FROM attachments
          WHERE state='pending' AND url != ''
            AND COALESCE(next_try,0) <= ?
          ORDER BY CASE WHEN message_id IN ({ph}) THEN 0 ELSE 1 END,
            attachment_id LIMIT ?
        """, (time.time(), *prio, limit)).fetchall()
