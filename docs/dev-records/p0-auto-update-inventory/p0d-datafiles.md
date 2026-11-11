=== notify spool files ===
   1 .json
   2 .jsonl
   1 flags/notify.json
   1 mcs/config.json
   2 notify.json
   2 notify_gc_cursor.json
=== notify_spool filename defs ===
mcs/notify/notify_flush.py:4:comes from ~/.mcs/config.json {notify_target} in `hermes send --to`
mcs/notify/notify_flush.py:47:_MAX_FILES = 10
mcs/notify/notify_flush.py:48:_MAX_FILES_BYTES = 23 * 1024 * 1024
mcs/notify/notify_flush.py:72:    message order, max _MAX_FILES, each <= notify_sender.MAX_FILE_BYTES. A downloaded
mcs/notify/notify_flush.py:87:            if len(files) >= _MAX_FILES:
mcs/notify/notify_flush.py:98:            if size > notify_sender.MAX_FILE_BYTES:
mcs/notify/notify_flush.py:101:            if total + size > _MAX_FILES_BYTES:
mcs/notify/notify_spool.py:21:RESTORE_MARKER = "restore_pending.json"
mcs/notify/notify_spool.py:24:RESTORE_RECEIPT = "restore_reconcile.json"
mcs/notify/notify_spool.py:51:        "restore.json", "backup_restore_approval.json",
mcs/notify/notify_spool.py:52:        "backup_restore_resume.json", "backup_restore_blocked.json"))
mcs/notify/notify_spool.py:111:    state = update_journal.load_state(os.path.join(root, "update_state.json"))
mcs/notify/notify_spool.py:128:            or os.path.lexists(os.path.join(root, "restore.json"))):
mcs/notify/notify_spool.py:144:    """flags/notify.json — the plugin's DB-free view of the effective
mcs/notify/notify_spool.py:159:    path = os.path.join(notify_dirs(root)["flags"], "notify.json")
mcs/notify/notify_spool.py:176:    publish_file(os.path.dirname(path), "notify.json", raw)
mcs/notify/notify_spool.py:206:                                spec["delivery_id"] + ".json", canonical(spec))
mcs/notify/notify_spool.py:252:                            r["delivery_id"] + ".json")
mcs/notify/notify_spool.py:257:                         r["delivery_id"] + ".json",
mcs/notify/notify_spool.py:281:                     if n.endswith(".json.claimed")]
=== recovery .prev / filenames ===
deployment/recovery/mcs_recover.py:47:# The entrypoint and each retained .prev select only their own complete generation.
deployment/recovery/mcs_recover.py:543:                target, prev = applying.get("sha"), applying.get("prev_sha")
deployment/recovery/mcs_recover.py:586:        prev = (applying or {}).get("prev_sha")
deployment/recovery/mcs_recover.py:679:                return escalate("MERGE_HEAD without known prev_sha")
deployment/recovery/mcs_recover.py:755:                identity_keys = ("sha", "prev_sha", "backup_path", "rollback", "command_id",
deployment/recovery/mcs_recover.py:835:                if applied and applied[-1].get("sha") == applying.get("prev_sha"):
deployment/recovery/prepare_recovery.py:151:            os.replace(str(previous_tmp), str(target) + ".prev")
mcs/ops/update_recovery.py:182:        if applied and applied[-1].get("sha") == applying.get("prev_sha"):
mcs/ops/update_recovery.py:272:                target, previous = applying.get("sha"), applying.get("prev_sha")
mcs/ops/update_recovery.py:342:        prev = (applying or {}).get("prev_sha")
mcs/ops/update_recovery.py:378:            tree is on prev_sha while the DB may still be the newer
mcs/ops/update_recovery.py:447:                return escalate("MERGE_HEAD without known prev_sha")
mcs/ops/update_recovery.py:644:        record, entry = applying, {**applying, "sha": applying["prev_sha"],
mcs/ops/update_recovery.py:645:                                   "prev_sha": applying["sha"]}
mcs/ops/update_restore.py:92:def _rollback_applying(entry, prev_sha, command_id) -> dict:
mcs/ops/update_restore.py:94:    mcs_recover's rollback_shaped check): target is entry's prev_sha."""
mcs/ops/update_restore.py:97:        "sha": entry["prev_sha"],
mcs/ops/update_restore.py:98:        "prev_sha": prev_sha,
mcs/ops/update_restore.py:116:    record — the tree is already reset to prev_sha, so the next recover
mcs/ops/update_restore.py:205:        target = _target_card_layout(entry["prev_sha"])
mcs/ops/update_restore.py:211:            f"target layout {target} ({entry['prev_sha'][:12]})")
mcs/ops/update_restore.py:215:    """Restore tracked files to prev_sha — shared by rollback() and the
mcs/ops/update_restore.py:219:    prev = entry["prev_sha"]
=== data dir filenames in ops ===
  38 ledger.db
  17 update_state.json
  10 update_in_progress.marker
   4 restore_report.json
   4 recovery_report.json
   3 mcs.log
   2 restore_pending.json
   1 args.log
   1 .mcs-release-dependencies.json
   1 .mcs-log-state.json
