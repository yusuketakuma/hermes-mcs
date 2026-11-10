=== acquire_run_lock ===
ci/gates.py:490:            return ('lock',) if module == 'writer_lock' and name == 'acquire_run_lock' else None
ci/gates.py:494:        return ('lock',) if module == 'writer_lock' and name == 'acquire_run_lock' and target else target
ci/gates.py:541:                if name == 'acquire_run_lock' and name not in table:
ci/gates.py:566:    return [f"{path.name}:{line} write-mode Ledger without acquire_run_lock on every caller path"
mcs/extract/attachment_inference.py:175:    fd = writer_lock.acquire_run_lock(str(root / ('attachment_inference_' + stage + '.lock')))
mcs/extract/attachment_ocr.py:530:        fd = writer_lock.acquire_run_lock(str(root / "run.lock"))
mcs/extract/attachment_ocr.py:586:    fd = writer_lock.acquire_run_lock(str(root / "attachment_ocr.lock"))
mcs/extract/llm/extract_llm.py:143:from writer_lock import acquire_run_lock as acquire_run_lock
mcs/extract/llm/extract_llm_errors.py:16:from writer_lock import acquire_run_lock
mcs/extract/llm/extract_llm_errors.py:302:            fd = acquire_run_lock()
mcs/extract/llm/extract_llm_errors.py:311:            fd = acquire_run_lock()
mcs/extract/llm/extract_llm_resident.py:25:from writer_lock import acquire_run_lock
mcs/extract/llm/extract_llm_resident.py:81:    lock_fd = acquire_run_lock()
mcs/extract/llm/extract_llm_resident.py:246:    fd = writer_lock.acquire_run_lock()
mcs/extract/llm/extract_llm_resident.py:663:            lock_fd = acquire_run_lock()
mcs/extract/rules/extract.py:45:from writer_lock import acquire_run_lock
mcs/extract/rules/extract.py:911:        lock_fd = acquire_run_lock()
mcs/ingest/cross_lists.py:308:    from writer_lock import acquire_run_lock
mcs/ingest/cross_lists.py:324:    lock_fd = acquire_run_lock(os.path.join(
mcs/ingest/health_recovery.py:323:        fd = writer_lock.acquire_run_lock(os.path.join(home, "data", "update.lock"))
mcs/ingest/init_data.py:36:from writer_lock import (RUN_LOCK, UPDATE_MARKER_NAME, acquire_run_lock,
mcs/ingest/init_data.py:68:    lock_fd = acquire_run_lock(LOCKFILE)
mcs/ingest/project_metadata.py:521:    from writer_lock import acquire_run_lock
mcs/ingest/project_metadata.py:555:            lock_fd = acquire_run_lock(os.path.join(
mcs/ingest/run_check.py:58:from writer_lock import RUN_LOCK, UPDATE_MARKER_NAME, RunLockLost, acquire_run_lock
mcs/ingest/run_check.py:1232:    lock_fd = acquire_run_lock(LOCKFILE)
mcs/llm/cloud_backlog.py:367:    from writer_lock import acquire_run_lock
mcs/llm/cloud_backlog.py:377:        startup_lock = acquire_run_lock()
mcs/llm/cloud_backlog.py:501:                        lock = writer_lock.acquire_run_lock()
mcs/ops/cloud_cli.py:801:            from writer_lock import acquire_run_lock, wait_run_lock, code_stamp
mcs/ops/cloud_cli.py:820:            lock_fd = acquire_run_lock(str(path.parent / "run.lock"))
mcs/ops/commands/job_cli.py:74:        from writer_lock import acquire_run_lock, wait_run_lock, code_stamp
mcs/ops/commands/job_cli.py:81:        lock_fd = acquire_run_lock(str(path.parent / "run.lock"))
mcs/ops/mcs_ocr.py:21:from writer_lock import acquire_run_lock  # noqa: E402
mcs/ops/mcs_ocr.py:117:    job = acquire_run_lock(os.path.join(DATA, 'attachment_ocr_job.lock'))
mcs/ops/mcs_ocr.py:127:        fd = acquire_run_lock(os.path.join(DATA, 'run.lock'))
mcs/ops/mcs_update.py:97:from writer_lock import acquire_run_lock as acquire_run_lock
mcs/ops/mcs_update.py:163:    _acquire_run_lock_wait as _acquire_run_lock_wait,
mcs/ops/notification_cancel.py:23:from writer_lock import acquire_run_lock, code_stamp, wait_run_lock
mcs/ops/notification_cancel.py:214:        fd = acquire_run_lock(str(path))
mcs/ops/notification_cancel.py:292:            fd = acquire_run_lock(str(data / name))
mcs/ops/ocr_failure_ack.py:22:from writer_lock import acquire_run_lock, code_stamp, wait_run_lock  # noqa: E402
mcs/ops/ocr_failure_ack.py:84:            fd = acquire_run_lock(str(data / name))
mcs/ops/release_apply_owner.py:902:                run_fd = mcs_update.acquire_run_lock(mcs_update.RUN_LOCK)
mcs/ops/update_apply.py:53:    _acquire_run_lock_wait,
mcs/ops/update_apply.py:468:                run_fd = _acquire_run_lock_wait()
mcs/ops/update_apply.py:655:        run_fd = _acquire_run_lock_wait()
mcs/ops/update_lifecycle_context.py:38:from writer_lock import RUN_LOCK_POLL_S, UPDATE_MARKER_NAME, acquire_run_lock
mcs/ops/update_lifecycle_context.py:314:    return acquire_run_lock(UPDATE_LOCK)
mcs/ops/update_lifecycle_context.py:317:def _acquire_run_lock_wait(tries: int = RUN_LOCK_TRIES) -> int | None:
mcs/ops/update_lifecycle_context.py:322:        fd = acquire_run_lock(RUN_LOCK)
mcs/ops/update_lifecycle_context.py:488:        fd = acquire_run_lock(RUN_LOCK)
mcs/ops/update_recovery.py:38:    _acquire_run_lock_wait,
mcs/ops/update_recovery.py:254:        run_fd = _acquire_run_lock_wait(tries=2)
mcs/patients/patient_context_quality_controller.py:170:        fd = patient_context_refresh_state.acquire_run_lock()
mcs/patients/patient_context_quality_controller.py:220:    fd = patient_context_refresh_state.acquire_run_lock()
mcs/patients/patient_context_quality_controller.py:379:        fd = patient_context_refresh_state.acquire_run_lock()
mcs/patients/patient_context_quality_controller.py:435:    while (fd := patient_context_refresh_state.acquire_run_lock()) is None:
mcs/patients/patient_context_quality_controller.py:511:    fd = patient_context_refresh_state.acquire_run_lock()
mcs/patients/patient_context_quality_controller.py:748:            fd = patient_context_refresh_state.acquire_run_lock()
mcs/patients/patient_context_quality_controller.py:767:            fd = patient_context_refresh_state.acquire_run_lock()
mcs/patients/patient_context_quality_receipts.py:60:    fd = patient_context_refresh_state.acquire_run_lock()
mcs/patients/patient_context_quality_receipts.py:132:    fd = patient_context_refresh_state.acquire_run_lock()
mcs/patients/patient_context_quality_receipts.py:243:    fd = patient_context_refresh_state.acquire_run_lock()
mcs/patients/patient_context_quality_receipts.py:305:    fd = patient_context_refresh_state.acquire_run_lock()
mcs/patients/patient_context_quality_receipts.py:445:    fd = patient_context_refresh_state.acquire_run_lock()
mcs/patients/patient_context_quality_receipts.py:537:    fd = patient_context_refresh_state.acquire_run_lock()
mcs/patients/patient_context_refresh.py:76:    acquire_run_lock as acquire_run_lock,
mcs/patients/patient_context_refresh.py:1156:    while (fd := patient_context_refresh_state.acquire_run_lock()) is None:
mcs/patients/patient_context_refresh.py:1490:            audit_fd = patient_context_refresh_state.acquire_run_lock()
mcs/patients/patient_context_refresh_state.py:9:from writer_lock import acquire_run_lock as acquire_run_lock
mcs/patients/rollup.py:50:from writer_lock import acquire_run_lock as acquire_run_lock
mcs/patients/rollup.py:1056:    lock_fd = acquire_run_lock()
mcs/semantic/semantic.py:58:from writer_lock import acquire_run_lock
mcs/semantic/semantic.py:281:    lock_fd = acquire_run_lock()
mcs/semantic/semantic_drain.py:271:    run_check; a standalone CLI takes acquire_run_lock itself.
mcs/semantic/semantic_drain.py:1111:    from writer_lock import acquire_run_lock
mcs/semantic/semantic_drain.py:1117:            lock_fd = acquire_run_lock()
mcs/semantic/semantic_due_selection.py:405:    from writer_lock import acquire_run_lock
mcs/semantic/semantic_due_selection.py:412:    fd = acquire_run_lock(path)
mcs/semantic/semantic_jev.py:118:    from writer_lock import acquire_run_lock, code_stamp
mcs/semantic/semantic_jev.py:119:    lock_fd = acquire_run_lock(str(home / "data" / "run.lock"))
=== unlocked_transport ===
mcs/extract/llm/extract_llm_runner.py:1019:                writer_lock.unlocked_transport, ledger.db, run_lock_fd, deadline=deadline)
mcs/ingest/run_check.py:974:            self._transport = writer_lock.unlocked_transport(
mcs/semantic/semantic_drain.py:389:    from writer_lock import unlocked_transport
mcs/semantic/semantic_drain.py:406:            with unlocked_transport(ledger.db, run_lock_fd, wait_s=reacquire_s,
mcs/semantic/semantic_drain.py:493:        with unlocked_transport(ledger.db, run_lock_fd, wait_s=reacquire_s, deadline=deadline):
=== wait_run_lock ===
mcs/extract/attachment_inference.py:78:    fd = writer_lock.wait_run_lock(str(root / 'run.lock'), wait_s=5,
mcs/extract/attachment_ocr.py:533:            fd = writer_lock.wait_run_lock(str(root / "run.lock"), wait_s=wait_s,
mcs/ingest/run_check.py:1235:            lock_fd = writer_lock.wait_run_lock(LOCKFILE, wait_s=(LOCK_WAIT_S if deadline is None else min(
mcs/ops/cloud_cli.py:801:            from writer_lock import acquire_run_lock, wait_run_lock, code_stamp
mcs/ops/cloud_cli.py:822:                lock_fd = wait_run_lock(str(path.parent / "run.lock"), wait_s=5.0,
mcs/ops/commands/job_cli.py:74:        from writer_lock import acquire_run_lock, wait_run_lock, code_stamp
mcs/ops/commands/job_cli.py:83:            lock_fd = wait_run_lock(str(path.parent / "run.lock"), wait_s=5.0,
mcs/ops/mcs_ocr.py:130:            fd = (writer_lock.wait_run_lock(os.path.join(DATA, 'run.lock'), wait_s=remaining,
mcs/ops/mcs_ocr.py:150:            fd = (writer_lock.wait_run_lock(os.path.join(DATA, 'run.lock'), wait_s=remaining,
mcs/ops/notification_cancel.py:23:from writer_lock import acquire_run_lock, code_stamp, wait_run_lock
mcs/ops/notification_cancel.py:294:                fd = wait_run_lock(str(data / name), wait_s=wait_seconds,
mcs/ops/ocr_failure_ack.py:22:from writer_lock import acquire_run_lock, code_stamp, wait_run_lock  # noqa: E402
mcs/ops/ocr_failure_ack.py:86:                fd = wait_run_lock(str(data / name), wait_s=wait_seconds,
=== writer_blocked ===
mcs/extract/attachment_ocr.py:592:            return {**counts, "reason": "writer_blocked"}
mcs/extract/llm/extract_llm_carry.py:32:    if not carry or writer_lock.writer_blocked():
mcs/extract/llm/extract_llm_carry.py:43:        if writer_lock.writer_blocked():
mcs/extract/llm/extract_llm_source_contract.py:252:            writer_lock.record_writer_refusal("initial_writer_blocked", "writer_fence")
mcs/extract/llm/extract_llm_source_contract.py:254:        if writer_lock.writer_blocked():
mcs/ingest/health_report.py:80:        known = {None, "disabled", "busy", "writer_busy", "writer_blocked", "config_invalid",
mcs/ingest/health_report.py:147:    writer_fence_wait = (reason == "writer_blocked" and writer_detail
mcs/ops/mcs_ocr.py:43:                result['reason'] = 'writer_blocked'
mcs/ops/mcs_ocr.py:116:        return {'reason': 'writer_blocked'}
mcs/ops/mcs_ocr.py:137:                return {'reason': 'writer_blocked'}
mcs/ops/mcs_ocr.py:160:                        result['reason'] = 'writer_blocked'
=== updater_state ===
mcs/extract/attachment_inference.py:64:    if (writer_lock.updater_state() != generation or generation[0]
mcs/extract/attachment_inference.py:83:        if writer_lock.updater_state() != generation or generation[0]:
mcs/extract/attachment_inference.py:162:    generation = writer_lock.updater_state()
mcs/extract/attachment_ocr.py:527:                or writer_lock.updater_state() != generation or generation[0]
mcs/extract/attachment_ocr.py:544:            if (writer_lock.updater_state() != generation or generation[0]
mcs/extract/attachment_ocr.py:590:        generation = writer_lock.updater_state()
mcs/extract/llm/extract_llm_carry.py:46:                (fence is not None and writer_lock.updater_state() != fence)):
mcs/extract/llm/extract_llm_carry.py:49:            write = write and writer_lock.updater_state() == fence
mcs/extract/llm/extract_llm_resident.py:164:        if (writer_lock.updater_state() != initial_state or initial_state[0]):
mcs/extract/llm/extract_llm_resident.py:192:    if writer_lock.updater_state() != initial_state:
mcs/extract/llm/extract_llm_resident.py:451:            context_state = writer_lock.updater_state()
mcs/extract/llm/extract_llm_resident.py:505:                        if writer_lock.updater_state() != context_state:
mcs/extract/llm/extract_llm_source_contract.py:232:_WRITER_STATE = writer_lock.updater_state()
mcs/extract/llm/extract_llm_source_contract.py:266:        state = writer_lock.updater_state()
mcs/extract/rules/extract.py:50:_WRITER_STATE = writer_lock.updater_state()
mcs/extract/rules/extract.py:539:        if (writer_lock.updater_state() != _WRITER_STATE or _WRITER_STATE[0]
mcs/extract/rules/extract.py:673:        if (writer_lock.updater_state() != _WRITER_STATE or _WRITER_STATE[0]
mcs/extract/rules/extract.py:765:                or writer_lock.updater_state() != _WRITER_STATE or _WRITER_STATE[0]
mcs/extract/rules/extract.py:776:    if (writer_lock.updater_state() != _WRITER_STATE or _WRITER_STATE[0]
mcs/extract/rules/extract.py:876:            or writer_lock.updater_state() != _WRITER_STATE or _WRITER_STATE[0]
mcs/extract/rules/extract.py:883:                or writer_lock.updater_state() != _WRITER_STATE or _WRITER_STATE[0]
mcs/llm/cloud_backlog.py:370:    initial = writer_lock.updater_state()
mcs/llm/cloud_backlog.py:374:    while os.getppid() == parent and writer_lock.updater_state() == initial and not initial[0]:
mcs/llm/cloud_backlog.py:384:        if writer_lock.updater_state() != initial or not owner_alive():
mcs/llm/cloud_backlog.py:415:            while os.getppid() == parent and writer_lock.updater_state() == initial and not initial[0]:
mcs/llm/cloud_backlog.py:429:                    if writer_lock.updater_state() != initial or not extract_llm._writer_current(full=True):
mcs/llm/cloud_backlog.py:524:                                    or writer_lock.updater_state() != initial
mcs/llm/cloud_backlog.py:596:                    and writer_lock.updater_state() == initial
mcs/llm/cloud_backlog.py:663:        while owner_alive() and writer_lock.updater_state() == initial and not initial[0]:
mcs/llm/cloud_inference.py:97:        updater = writer_lock.updater_state()
mcs/llm/cloud_inference.py:161:                          or writer_lock.updater_state() != updater
mcs/ops/mcs_ocr.py:42:            if writer_lock.updater_state() != generation:
mcs/ops/mcs_ocr.py:114:    generation = writer_lock.updater_state()
mcs/ops/mcs_ocr.py:136:            if writer_lock.updater_state() != generation:
mcs/ops/mcs_ocr.py:159:                    if writer_lock.updater_state() != generation:
=== RunLockLost ===
mcs/extract/llm/extract_llm_runner.py:1140:    except writer_lock.RunLockLost:
mcs/ingest/run_check.py:58:from writer_lock import RUN_LOCK, UPDATE_MARKER_NAME, RunLockLost, acquire_run_lock
mcs/ingest/run_check.py:830:        raise RunLockLost(held=lost == "held")
mcs/ingest/run_check.py:1383:    except RunLockLost as e:
mcs/llm/cloud_backlog.py:419:                    except writer_lock.RunLockLost:
mcs/llm/cloud_backlog.py:583:                except writer_lock.RunLockLost:
mcs/llm/cloud_inference.py:365:        except writer_lock.RunLockLost:
mcs/semantic/semantic_drain.py:412:        except writer_lock.RunLockLost:
mcs/semantic/semantic_drain.py:856:            except writer_lock.RunLockLost as e:
mcs/semantic/semantic_transport.py:210:        except writer_lock.RunLockLost as error:
=== sqlite3.connect ===
adapters/common/projects.py:33:        db = sqlite3.connect(
adapters/common/summary.py:67:        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
ci/gates.py:383:    sqlite3.connect elsewhere must carry mode=ro/immutable (snapshot
ci/gates.py:448:                bad.append(f"{path.name}:{node.lineno} sqlite3.connect without "
deployment/recovery/mcs_recovery_database.py:79:        con = sqlite3.connect(
deployment/recovery/mcs_recovery_database.py:258:        live = sqlite3.connect(
deployment/recovery/mcs_recovery_database.py:263:        back = sqlite3.connect(
deployment/recovery/mcs_recovery_database.py:345:        con = sqlite3.connect(
deployment/recovery/mcs_recovery_database.py:481:        candidate = sqlite3.connect(
deployment/recovery/mcs_recovery_database.py:490:        live = sqlite3.connect(Path(mcs_recovery_core.LEDGER).resolve().as_uri() + "?mode=rw",
integration/test_mcs_recovery_narrative.py:406:        con = sqlite3.connect(
integration/test_mcs_recovery_narrative.py:422:        con = sqlite3.connect(
integration/test_mcs_recovery_narrative.py:663:        sdb = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
mcs/core/ledger.py:113:        self.db = sqlite3.connect(path, timeout=30)
mcs/core/ledger.py:1214:        self.db = sqlite3.connect(uri, uri=True,
mcs/core/ledger.py:1235:        db = sqlite3.connect(uri, uri=True)
mcs/core/maintenance.py:132:        with closing(sqlite3.connect(
mcs/core/maintenance.py:135:            with closing(sqlite3.connect(tmp, **({} if deadline is None else {'timeout': min(5.0, max(0, deadline - monotonic()))}))) as dst:
mcs/core/maintenance.py:506:    con = sqlite3.connect(db_path, timeout=30)
mcs/core/maintenance.py:729:        with closing(sqlite3.connect(uri, uri=True, timeout=timeout)) as con:
mcs/core/maintenance.py:1334:    con = sqlite3.connect(db_path, timeout=5)
mcs/core/snapshots.py:175:    src = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro",
mcs/core/snapshots.py:178:        dst = sqlite3.connect(tmp, timeout=timeout)
mcs/extract/eval/pattern_cards.py:397:        with closing(sqlite3.connect(copy.as_uri() + "?mode=ro", uri=True)) as db:
mcs/extract/llm/extract_llm_selection.py:531:            reader = sqlite3.connect(Path(path).absolute().as_uri() + '?mode=ro', uri=True, timeout=0)
mcs/ingest/health_report.py:38:        with closing(sqlite3.connect((Path(data) / 'ledger.db').resolve().as_uri() + '?mode=ro',
mcs/llm/admission/llm_admission.py:211:    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
mcs/notify/notify_actions.py:57:        reader = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.1)
mcs/notify/notify_digest.py:702:    db = sqlite3.connect(Path(DB).resolve().as_uri() + "?mode=ro", uri=True)
mcs/ops/backup/backup_evidence.py:543:    with closing(sqlite3.connect(
mcs/ops/backup/restore_evidence.py:114:        con = sqlite3.connect(
mcs/ops/backup/restore_evidence.py:170:        live = sqlite3.connect(
mcs/ops/backup/restore_evidence.py:175:        back = sqlite3.connect(
mcs/ops/backup/restore_evidence.py:232:        con = sqlite3.connect(
mcs/ops/cloud_cli.py:49:    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.25)) as db:
mcs/ops/mcs_setup.py:759:        with closing(sqlite3.connect(
mcs/ops/mcs_setup.py:901:        with closing(sqlite3.connect(ledger.resolve().as_uri() + "?mode=ro", uri=True,
mcs/ops/notification_cancel.py:302:        with closing(sqlite3.connect((data / "ledger.db").resolve().as_uri() + "?mode=rw",
mcs/ops/notification_cancel.py:344:                with closing(sqlite3.connect(str(backup))) as dest:
mcs/ops/notification_cancel.py:427:            with closing(sqlite3.connect((args.root / "data/ledger.db").resolve().as_uri() + "?mode=ro",
mcs/ops/ocr_failure_ack.py:93:        with closing(sqlite3.connect((data / 'ledger.db').resolve().as_uri() + '?mode=rw',
mcs/ops/ocr_failure_ack.py:111:                with closing(sqlite3.connect(name)) as backup:
mcs/ops/ocr_failure_ack.py:187:            with closing(sqlite3.connect((args.root / 'data/ledger.db').resolve().as_uri() + '?mode=ro',
mcs/ops/semantic_observe.py:91:        c = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True,
mcs/ops/update_execution.py:109:        con = sqlite3.connect(
mcs/ops/update_restore.py:178:        con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro",
mcs/ops/update_restore.py:264:        con = sqlite3.connect(Path(LEDGER).resolve().as_uri() + "?mode=rw",
mcs/ops/update_restore.py:426:        candidate = sqlite3.connect(
mcs/ops/update_restore.py:435:        live = sqlite3.connect(Path(LEDGER).resolve().as_uri() + "?mode=rw",
mcs/patients/patient_context_refresh_sources.py:1055:    with closing(sqlite3.connect(Path(path).absolute().as_uri() + '?mode=ro', uri=True,
mcs/semantic/eval/semantic_blind.py:221:    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
mcs/semantic/eval/semantic_lifecycle.py:194:    ``db`` must be a sqlite3 connection with ``Row`` factory."""
mcs/semantic/eval/semantic_lifecycle.py:244:    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro",
mcs/semantic/eval/semantic_snapshot.py:46:    with closing(sqlite3.connect(':memory:')) as db:
mcs/semantic/eval/semantic_snapshot_observations.py:121:    with closing(sqlite3.connect(':memory:')) as db:
mcs/semantic/semantic_due_selection.py:608:            reader = sqlite3.connect(Path(path).absolute().as_uri() + '?mode=ro',
mcs/views/card_worker_status.py:107:            with closing(sqlite3.connect(ledger.resolve().as_uri() + '?mode=ro', uri=True,
mcs/views/measurement_local_view.py:72:        db = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True,
scripts/development/generate_slack_gallery.py:411:    with closing(sqlite3.connect(":memory:")) as db:
scripts/evaluation/canonical_request_eval.py:412:            db = sqlite3.connect(f"{args.shadow_db.resolve().as_uri()}?mode=ro", uri=True)
=== store.read/write filename calls ===
adapters/sdc/connector.py:17:        self.config = store.read('endpoint.json')
adapters/sdc/connector.py:33:            self.store.write('credential', credential, mutation={'purpose': purpose, 'after': {
adapters/sdc/connector.py:36:            self.store.write('credential', credential)
adapters/sdc/connector.py:37:            self.store.write('state.json', state)
adapters/sdc/connector.py:46:        state = self.store.read('state.json', {})
adapters/sdc/connector.py:47:        credential = self.store.read('credential', {})
adapters/sdc/connector.py:63:            self.store.write('state.json', state)
adapters/sdc/connector.py:90:        state = self.store.read('state.json')
adapters/sdc/connector.py:91:        key = self.secret(state, self.store.read('credential'))
adapters/sdc/connector.py:92:        receipt = self.store.read('enrollment-consent.json', {})
adapters/sdc/connector.py:103:        self.store.write('state.json', state)
adapters/sdc/connector.py:123:        state = self.store.read('state.json')
adapters/sdc/connector.py:126:        key = self.secret(state, self.store.read('credential'))
adapters/sdc/connector.py:133:        state = self.store.read('state.json')
adapters/sdc/connector.py:134:        identity = self._enrollment_identity(result, state, self.secret(state, self.store.read('credential')))
adapters/sdc/connector.py:139:                   'key_sha256': hashlib.sha256(bytes.fromhex(self.secret(state, self.store.read('credential')))).hexdigest(),
adapters/sdc/connector.py:143:        self.store.write('enrollment-consent.json', receipt)
adapters/sdc/connector.py:145:        if self.store.read('enrollment-consent.json') != receipt:
adapters/sdc/connector.py:150:        receipt = self.store.read('enrollment-consent.json', {})
adapters/sdc/connector.py:177:        state = self.store.read('state.json', {})
adapters/sdc/connector.py:184:        key = self.secret(state, self.store.read('credential'))
adapters/sdc/connector.py:208:            self.store.write('state.json', state)
adapters/sdc/connector.py:216:                    self.store.write('state.json', state)
adapters/sdc/connector.py:227:            self.store.write('state.json', state)
adapters/sdc/connector.py:235:                self.store.write('state.json', state)
adapters/sdc/connector.py:238:                self.store.write('state.json', state)
adapters/sdc/connector.py:242:            self.store.write('state.json', state)
adapters/sdc/connector.py:246:        state = self.store.read('state.json')
adapters/sdc/connector.py:247:        credential = self.store.read('credential')
adapters/sdc/connector.py:252:            self.store.write('state.json', state)
adapters/sdc/connector.py:264:            self.store.write('state.json', state)
adapters/sdc/connector.py:295:        self.store.write('state.json', state)
adapters/sdc/connector.py:307:        state = self.store.read('state.json')
adapters/sdc/connector.py:310:        credential = self.store.read('credential')
adapters/sdc/connector.py:313:        self.store.write('state.json', state)
adapters/sdc/connector.py:316:        state = self.store.read('state.json', {})
adapters/sdc/connector.py:317:        credential = self.store.read('credential') if not self.store.customer else None
adapters/sdc/connector.py:321:        self.store.write('state.json', state)
adapters/sdc/connector.py:323:            result = self.request('/v2/device/suspend', {}, state, self.secret(state, credential if credential is not None else self.store.read('credential')))
adapters/sdc/connector.py:328:        self.store.write('state.json', state)
adapters/sdc/connector.py:345:        state = self.store.read('state.json', {})
adapters/sdc/connector.py:348:        credential = self.store.read('credential')
adapters/sdc/connector.py:355:            self.store.write('state.json', state)
adapters/sdc/connector.py:365:        self.store.write('state.json', state)
adapters/sdc/connector.py:392:        self.store.write('state.json', state)
adapters/sdc/credential_mutation.py:216:            raw = stream.read(MAX_MARKER + 1)
adapters/sdc/credential_mutation.py:262:            stream.write(raw)
adapters/sdc/diagnostic_sender.py:42:        monitor = endpoint(store.read('endpoint.json'))
adapters/sdc/diagnostic_sender.py:47:        state = store.read('state.json')
adapters/sdc/diagnostic_sender.py:82:        credential = store.read('credential')
adapters/sdc/diagnostic_state.py:217:        anchor, value = store.read(ANCHOR), store.read(NAME)
adapters/sdc/diagnostic_state.py:241:        store.write(NAME, value)
adapters/sdc/diagnostic_state.py:243:               and store.read(NAME) == value, 'STATE_CONFLICT')
adapters/sdc/inventory_sender.py:96:        self.store.write('inventory-status.json', status)
adapters/sdc/inventory_sender.py:101:        policy = self.store.read('inventory-policy.json')
adapters/sdc/inventory_sender.py:107:        state = self.store.read('state.json', {})
adapters/sdc/inventory_sender.py:115:        status = self.store.read('inventory-status.json') or initial_status(policy['revision'])
adapters/sdc/inventory_sender.py:128:        target_hash = endpoint_hash(self.store.read('endpoint.json'))
adapters/sdc/inventory_sender.py:131:        spool = self.store.read('inventory-spool.json')
adapters/sdc/inventory_sender.py:141:            self.store.write('inventory-status.json', status)
adapters/sdc/inventory_sender.py:155:            self.store.write('inventory-spool.json', spool)
adapters/sdc/inventory_sender.py:159:            self.store.write('inventory-status.json', status)
adapters/sdc/inventory_sender.py:171:        current_policy = self.store.read('inventory-policy.json')
adapters/sdc/inventory_sender.py:172:        current_state = self.store.read('state.json', {})
adapters/sdc/inventory_sender.py:177:        if endpoint_hash(self.store.read('endpoint.json')) != policy['endpoint_hash']:
adapters/sdc/inventory_sender.py:181:            result = connector.request(PATH, raw, current_state, connector.secret(current_state, self.store.read('credential')))
adapters/sdc/keychain_owner.py:277:                    count = os.write(self._request.fileno(), packet[offset:])
adapters/sdc/keychain_owner.py:285:                    chunk = os.read(self._response.fileno(), MAX_FRAME + 5 - len(output))
adapters/sdc/provision.py:615:    guard_registration(store, monitor, store.read('credential'))
adapters/sdc/provision.py:622:    credential = store.read('credential')
adapters/sdc/release_artifact.py:45:    data = stream.read(size)
adapters/sdc/release_artifact.py:73:                pending = stream.read(CHUNK)
adapters/sdc/release_artifact.py:79:            destination.write(out)
adapters/sdc/release_artifact.py:95:        while chunk := stream.read(CHUNK):
adapters/sdc/release_artifact.py:112:            while opcode := instructions.read(1):
adapters/sdc/release_artifact.py:131:                        output.write(part)
adapters/sdc/release_artifact.py:136:                    output.write(_exact(instructions, size))
adapters/sdc/release_artifact.py:205:    _check(checksum == digest.digest() and stream.read(1) == b'', 'PACK_CHECKSUM_OR_TRAILING_BYTES')
adapters/sdc/release_artifact.py:276:            while chunk := stream.read(CHUNK):
adapters/sdc/release_artifact.py:281:                output.write(chunk)
adapters/sdc/release_artifact.py:306:            raw = stream.read(MAX_METADATA_BYTES + 1)
adapters/sdc/release_artifact.py:380:                while chunk := stream.read(CHUNK):
adapters/sdc/release_artifact.py:382:                    output.write(chunk)
adapters/sdc/release_auto_migration.py:41:    raw = store.read(NAME)
adapters/sdc/release_auto_migration.py:51:    store.write(NAME, _parse(value))
adapters/sdc/release_auto_migration.py:61:        _check(store.read(NAME) is None, 'INTENT_EXISTS')
adapters/sdc/release_auto_state.py:64:    raw = store.read(OWNER_NAME)
adapters/sdc/release_auto_state.py:80:        store.write(OWNER_NAME, value)
adapters/sdc/release_auto_state.py:399:        _check(store.read(INTENT_NAME) is None, 'INTENT_EXISTS')
adapters/sdc/release_auto_state.py:400:        store.write(INTENT_NAME, value)
adapters/sdc/release_auto_state.py:405:    raw = store.read(INTENT_NAME)
adapters/sdc/release_auto_state.py:440:        store.write(INTENT_NAME, intent)
adapters/sdc/release_state.py:140:    value = store.read(NAME)
adapters/sdc/release_state.py:167:    marker = store.read(ACTIVATION)
adapters/sdc/release_state.py:189:        store.write(NAME, value)
adapters/sdc/release_state.py:212:        _check(store.read(NAME) is None and store.read(ACTIVATION) is None, 'ALREADY_INITIALIZED')
adapters/sdc/release_state.py:231:            store.write(ACTIVATION, marker)
adapters/sdc/release_state.py:240:    return store.read(NAME) is not None or store.read(ACTIVATION) is not None
adapters/sdc/release_state.py:245:    _check(store.read(NAME) is not None, 'UNKNOWN_BASELINE')
adapters/sdc/restart_client.py:335:        if store.read(name) is None:
adapters/sdc/restart_client.py:350:            if store.read(name) is not None:
adapters/sdc/restart_execution.py:126:        state = store.read(NAME)
adapters/sdc/restart_execution.py:175:        store.write(NAME, state)
adapters/sdc/restart_planning.py:185:        state = store.read(NAME)
adapters/sdc/restart_planning.py:206:        store.write(NAME, state)
adapters/sdc/restart_planning.py:237:    _check(not state['disabled'] and not restart['disabled'] and store.read('restart-execution.json') is None, 'CONFLICT')
adapters/sdc/restart_planning.py:243:    target = endpoint(store.read('endpoint.json'))
adapters/sdc/restart_planning.py:292:        _check(store.read(NAME) is None, 'CONFLICT')
adapters/sdc/restart_state.py:140:        state = store.read(NAME)
adapters/sdc/restart_state.py:160:        store.write(NAME, state)
adapters/sdc/restart_state.py:192:        _check(store.read(NAME) is None, 'CONFLICT')
adapters/sdc/storage.py:158:            raw = stream.read(LIMITS[name] + 1)
adapters/sdc/storage.py:305:            raw = self._keychain().read() if deadline is None else self._keychain().read(deadline=deadline)
adapters/sdc/storage.py:466:            result = native.delete(**lease) if raw is None else native.write(raw, **lease)
adapters/sdc/storage.py:513:                self.write('state.json', state)
adapters/sdc/storage.py:587:                stream.write(raw)
adapters/sdc/storage.py:627:                stream.write(raw)
adapters/sdc/support_client.py:69:    value = store.read('support-bootstrap.json')
adapters/sdc/support_client.py:79:    config = store.read('endpoint.json')
adapters/sdc/support_client.py:102:        state = store.read('state.json')
adapters/sdc/support_state.py:142:        state = store.read(NAME)
adapters/sdc/support_state.py:184:            store.write(NAME, _validated(state))
adapters/sdc/support_state.py:243:        store.write(NAME, state)
adapters/sdc/support_state.py:255:        store.write(NAME, _validated(state))
adapters/sdc/support_state.py:317:    store.write(NAME, state)
adapters/sdc/transport.py:135:                raw_response = response.read(16385)
