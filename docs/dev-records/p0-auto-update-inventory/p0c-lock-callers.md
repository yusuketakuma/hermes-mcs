=== Store lock/method callers ===
adapters/sdc/connector.py:42:        self.store.guard_credential()  # Metadata only: no send past an unfinished credential change.
adapters/sdc/connector.py:70:            self.store.guard_credential()
adapters/sdc/connector.py:138:        receipt = {'schema_version': 'sdc.enrollment-consent.v1', 'accepted': True, **self.store.enrollment_binding(),
adapters/sdc/connector.py:162:        if any(receipt.get(name) != value for name, value in self.store.enrollment_binding().items()):
adapters/sdc/diagnostic_sender.py:36:        snapshot = provision.profile_snapshot(store, profile='support', now_ms=now_ms)
adapters/sdc/diagnostic_sender.py:43:        enrollment = store.enrollment_binding()
adapters/sdc/diagnostic_sender.py:77:        snapshot = provision.profile_snapshot(store, profile='support', now_ms=sender._now())
adapters/sdc/diagnostic_sender.py:101:        store.guard_credential()  # Cached auth never sends past an unfinished credential change.
adapters/sdc/diagnostic_state.py:376:    with store.diagnostic_state_lock():
adapters/sdc/diagnostic_state.py:413:    with store.diagnostic_state_lock():
adapters/sdc/diagnostic_state.py:438:    with store.diagnostic_state_lock():
adapters/sdc/diagnostic_state.py:449:    with store.diagnostic_state_lock():
adapters/sdc/diagnostic_state.py:525:    with store.diagnostic_state_lock():
adapters/sdc/diagnostic_state.py:577:    with store.diagnostic_state_lock():
adapters/sdc/diagnostic_state.py:600:    with store.diagnostic_state_lock():
adapters/sdc/diagnostic_state.py:620:    with store.diagnostic_state_lock():
adapters/sdc/diagnostic_state.py:713:    with store.diagnostic_state_lock():
adapters/sdc/diagnostic_state.py:761:    with store.diagnostic_state_lock():
adapters/sdc/provision.py:302:    with store.lock():
adapters/sdc/provision.py:311:    with store.lock():
adapters/sdc/provision.py:349:    with store.lock():
adapters/sdc/provision.py:373:    with store.lock():
adapters/sdc/provision.py:468:    result = _profile_snapshot(state, profile)
adapters/sdc/provision.py:478:    """Project saved support bytes; consumers must separately recheck current profile_snapshot."""
adapters/sdc/provision.py:482:    authority = _profile_snapshot(state, 'support')
adapters/sdc/provision.py:514:    with store.lock():
adapters/sdc/provision.py:528:        with store.lock():
adapters/sdc/provision.py:597:    with store.lock():
adapters/sdc/provision.py:658:    with store.lock():
adapters/sdc/provision.py:693:        with store.lock():
adapters/sdc/provision.py:714:        store.guard_credential()  # Before dispatch; a held change never rolls back the reservation.
adapters/sdc/provision.py:728:            with store.lock():
adapters/sdc/provision.py:748:    with store.lock():
adapters/sdc/release_auto_migration.py:7:規約: prepare/advanceはstore.release_owner_lock()を取得するleaf。load/require_v3_ready
adapters/sdc/release_auto_migration.py:60:    with store.release_owner_lock():
adapters/sdc/release_auto_migration.py:69:    with store.release_owner_lock():
adapters/sdc/release_auto_state.py:7:規約: 変更系の公開関数は各々 store.release_owner_lock() を取得するleaf。
adapters/sdc/release_auto_state.py:163:    with store.release_owner_lock():
adapters/sdc/release_auto_state.py:188:    with store.release_owner_lock():
adapters/sdc/release_auto_state.py:215:    with store.release_owner_lock():
adapters/sdc/release_auto_state.py:227:    with store.release_owner_lock():
adapters/sdc/release_auto_state.py:247:    with store.release_owner_lock():
adapters/sdc/release_auto_state.py:299:    with store.release_owner_lock():
adapters/sdc/release_auto_state.py:398:    with store.release_owner_lock():
adapters/sdc/release_auto_state.py:425:    with store.release_owner_lock():
adapters/sdc/release_state.py:210:    with store.release_owner_lock():
adapters/sdc/release_state.py:246:    with store.release_owner_lock():
adapters/sdc/release_state.py:254:    with store.release_owner_lock():
adapters/sdc/release_state.py:258:    with store.release_owner_lock():
adapters/sdc/release_state.py:318:    with store.release_owner_lock():
adapters/sdc/release_state.py:385:    with store.release_owner_lock():
adapters/sdc/release_state.py:411:    with store.release_owner_lock():
adapters/sdc/restart_client.py:46:                _require(value == managed_provision.profile_snapshot(store, profile='restart', now_ms=_now(clock)))
adapters/sdc/restart_client.py:53:            monitor = store.enrollment_binding()
adapters/sdc/restart_client.py:67:                        guard=store.guard_credential)
adapters/sdc/restart_client.py:298:                with store.support_state_lock():
adapters/sdc/restart_client.py:310:            with store.support_state_lock():
adapters/sdc/restart_execution.py:221:    with store.restart_state_lock(), store.lock():
adapters/sdc/restart_execution.py:264:    with store.restart_state_lock():
adapters/sdc/restart_execution.py:275:    with store.restart_state_lock(), store.lock():
adapters/sdc/restart_execution.py:298:    with store.restart_state_lock(), store.lock():
adapters/sdc/restart_execution.py:350:    with store.restart_state_lock():
adapters/sdc/restart_execution.py:357:    with store.restart_state_lock(), store.lock():
adapters/sdc/restart_execution.py:373:    with store.restart_state_lock():
adapters/sdc/restart_planning.py:217:    with store.support_state_lock(), store.restart_state_lock(), store.lock():
adapters/sdc/restart_planning.py:223:    snapshot = provision.profile_snapshot(store, profile=profile, now_ms=now_ms)
adapters/sdc/restart_planning.py:290:    with store.restart_state_lock():
adapters/sdc/restart_planning.py:337:    with store.restart_state_lock():
adapters/sdc/restart_planning.py:375:    with store.restart_state_lock():
adapters/sdc/restart_planning.py:473:    with store.restart_state_lock():
adapters/sdc/restart_planning.py:482:    with store.restart_state_lock():
adapters/sdc/restart_state.py:188:    with store.restart_state_lock():
adapters/sdc/restart_state.py:208:    with store.restart_state_lock(), store.lock():
adapters/sdc/restart_state.py:234:    with store.restart_state_lock(), store.lock():
adapters/sdc/restart_state.py:268:    with store.restart_state_lock(), store.lock():
adapters/sdc/restart_state.py:294:    with store.restart_state_lock():
adapters/sdc/restart_state.py:310:    with store.restart_state_lock():
adapters/sdc/restart_state.py:322:    with store.restart_state_lock():
adapters/sdc/restart_state.py:328:    with store.restart_state_lock():
adapters/sdc/support_client.py:74:    monitor = store.enrollment_binding()
adapters/sdc/support_client.py:81:        current = provision.profile_snapshot(store, profile='support', now_ms=now_ms)
adapters/sdc/support_client.py:120:                    guard=store.guard_credential)
adapters/sdc/support_state.py:171:    with store.support_state_lock(), store.lock():
adapters/sdc/support_state.py:196:    root fingerprint (via Store.enrollment_binding), monitor/support endpoint hashes,
adapters/sdc/support_state.py:206:    with store.support_state_lock(), store.lock():
adapters/sdc/support_state.py:250:    with store.support_state_lock():
adapters/sdc/support_state.py:323:    with store.support_state_lock():
adapters/sdc/support_state.py:333:    with store.support_state_lock(), store.lock():
adapters/sdc/support_state.py:359:    with store.support_state_lock():
adapters/sdc/support_state.py:374:    with store.support_state_lock(), store.lock():
adapters/sdc/support_state.py:404:    with store.support_state_lock():
adapters/sdc/support_state.py:535:    with store.support_state_lock(), store.lock():
adapters/sdc/support_state.py:554:    with store.support_state_lock(), store.lock():
adapters/sdc/support_state.py:565:    with store.support_state_lock(), store.lock():
adapters/sdc/support_state.py:594:    with store.support_state_lock(), store.lock():
adapters/sdc/support_state.py:607:    with store.support_state_lock(), store.lock():
adapters/sdc/support_state.py:620:    with store.support_state_lock():
adapters/sdc/support_state.py:631:    with store.support_state_lock(), store.lock():
adapters/sdc/support_state.py:642:    with store.support_state_lock(), store.lock():
adapters/sdc/support_state.py:659:    with store.support_state_lock():
adapters/sdc/support_state.py:677:    with store.support_state_lock(), store.lock():
adapters/sdc/support_state.py:686:    with store.support_state_lock(), store.lock():
mcs/export/c1_contract.py:126:        raise C1ContractError("profile_snapshot_age_invalid")
mcs/ops/release_apply_checks.py:246:        with control.release_owner_lock():
mcs/ops/release_apply_checks.py:256:        with control.release_owner_lock():
mcs/ops/release_apply_checks.py:361:        with control.release_owner_lock():
mcs/ops/release_apply_checks.py:397:    with Store(root) as store, store.release_owner_lock():
mcs/ops/release_apply_owner.py:100:    with control.release_owner_lock():
mcs/ops/release_apply_owner.py:147:    with control.release_authority_lock(), monitor.lock():
mcs/ops/release_apply_owner.py:153:    with control.release_authority_lock(), monitor.lock():
mcs/ops/release_apply_owner.py:162:        with control.release_owner_lock():
mcs/ops/release_apply_owner.py:264:        enrollment = monitor.enrollment_binding()
mcs/ops/release_apply_owner.py:265:        current_monitor = provision.profile_snapshot(monitor, profile='monitor', now_ms=now_ms)
mcs/ops/release_apply_owner.py:266:        support = provision.profile_snapshot(monitor, profile='support', now_ms=now_ms)
mcs/ops/release_apply_owner.py:279:        monitor.guard_credential()
mcs/ops/release_apply_owner.py:295:        with control.release_owner_lock():
mcs/ops/release_apply_owner.py:301:        monitor.guard_credential()
mcs/ops/release_apply_owner.py:303:                or monitor.enrollment_binding() != enrollment
mcs/ops/release_apply_owner.py:304:                or provision.profile_snapshot(monitor, profile='monitor', now_ms=now_ms) != current_monitor
mcs/ops/release_apply_owner.py:305:                or provision.profile_snapshot(monitor, profile='support', now_ms=now_ms) != support
mcs/ops/release_apply_owner.py:479:        with control.release_authority_lock(), monitor.lock():
mcs/ops/release_apply_owner.py:497:        with control.release_owner_lock():
mcs/ops/release_apply_owner.py:522:        with control.release_authority_lock(), monitor.lock():
mcs/ops/release_apply_owner.py:535:            with control.release_owner_lock():
mcs/ops/release_apply_owner.py:555:    with control.release_authority_lock():
mcs/ops/release_apply_owner.py:570:            profile = provision.profile_snapshot(monitor, profile='support', now_ms=now)
mcs/ops/release_apply_owner.py:597:        with control.release_owner_lock():
mcs/ops/release_apply_owner.py:664:        with control.release_authority_lock():
mcs/ops/release_apply_owner.py:717:        with control.release_authority_lock():
mcs/ops/release_apply_owner.py:747:        with control.release_authority_lock():
mcs/ops/release_apply_owner.py:781:        with control.release_authority_lock():
mcs/ops/release_apply_owner.py:789:        with control.release_authority_lock():
mcs/ops/release_apply_owner.py:816:    with control.release_authority_lock():
mcs/ops/release_apply_owner.py:847:        with control.release_authority_lock():
mcs/ops/release_apply_owner.py:879:        with control.release_owner_lock():
mcs/ops/release_apply_owner.py:888:            gate_fd = lifetime.enter_context(control.release_entry_lock())
mcs/ops/release_apply_owner.py:916:            with control.release_authority_lock():
mcs/ops/release_apply_owner.py:926:            with control.release_authority_lock():
mcs/ops/release_apply_owner.py:929:                with control.release_owner_lock():
mcs/ops/release_apply_owner.py:943:            with control.release_authority_lock():
mcs/ops/release_apply_owner.py:975:            with control.release_authority_lock():
mcs/ops/release_apply_owner.py:1009:                with control.release_owner_lock():
mcs/ops/release_apply_owner.py:1018:            with control.release_authority_lock():
mcs/ops/release_apply_owner.py:1023:                with control.release_owner_lock():
mcs/ops/release_apply_owner.py:1107:    with control.release_owner_lock():
mcs/ops/release_apply_owner.py:1281:        with entry.store.release_owner_lock():
mcs/ops/release_apply_owner.py:1299:        with entry.store.release_authority_lock():
mcs/ops/release_apply_owner.py:1374:    with entry.store.release_owner_lock():
mcs/ops/sdc_cli.py:240:        with Store(root, credential_deadline=end) as store, store.lock():
mcs/ops/sdc_cli.py:247:                store.guard_credential()
mcs/ops/sdc_cli.py:414:    with store.lock():
mcs/ops/sdc_cli.py:430:    with store.lock():
mcs/ops/sdc_cli.py:446:        with store.lock():
mcs/ops/sdc_cli.py:487:    with store.lock():
mcs/ops/sdc_diagnostic_cli.py:74:    snapshot = provision.profile_snapshot(store, profile='support', now_ms=now_ms)
mcs/ops/sdc_diagnostic_cli.py:153:                with store.lock():
mcs/ops/sdc_inventory_cli.py:36:        with Store(root) as store, store.lock():
mcs/ops/sdc_provision_cli.py:139:    provision.profile_snapshot(store, profile=profile, now_ms=now_ms)
mcs/ops/sdc_support_cli.py:189:                    return provision.profile_snapshot(store, profile='restart', now_ms=int(time.time() * 1000))
mcs_standalone/restart_owner.py:180:            snapshot = provision.profile_snapshot(self.store, profile='restart', now_ms=now)
mcs_standalone/runtime.py:686:                    provision.profile_snapshot(store, profile='restart', now_ms=int(time.time() * 1000))
=== control.read/write calls ===
mcs/ops/release_apply_owner.py:167:            previous = control.read('release-planning.json')
mcs/ops/release_apply_owner.py:176:                control.write('release-planning.json', record)
mcs/ops/release_apply_owner.py:177:                if control.read('release-planning.json') != record or time.monotonic() >= deadline:
mcs/ops/release_apply_owner.py:247:        raw_bootstrap = json.dumps(control.read('release-bootstrap.json'), ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('ascii')
mcs/ops/release_apply_owner.py:297:            activation = control.read(release_state.ACTIVATION)
mcs/ops/release_apply_owner.py:302:        if (control.read('release-bootstrap.json') != value
mcs/ops/release_apply_owner.py:444:        value = control.read('release-plan.json')
mcs/ops/release_apply_owner.py:482:            consent = _validate_planning_consent(control.read('release-planning.json'))
mcs/ops/release_apply_owner.py:525:            if (fresh.binding != current.binding or control.read('release-planning.json') != consent
mcs/ops/release_apply_owner.py:539:                    control.write('release-plan.json', record)
mcs/ops/release_apply_owner.py:624:        value = control.read('release-change.json')
mcs/ops/release_apply_owner.py:649:        control.write('release-change.json', value)
mcs/ops/release_apply_owner.py:1122:            control.write('release-successor.json', proof)
mcs/ops/release_apply_owner.py:1123:            if control.read('release-successor.json') != proof:
=== release_state _read/_write callers ===
mcs/ops/release_apply_checks.py:247:            saved = release_state._read(control)
mcs/ops/release_apply_checks.py:257:            if release_state._read(control) != saved:
mcs/ops/release_apply_checks.py:362:            owner = release_state._read(control)
mcs/ops/release_apply_checks.py:398:        owner = release_state._read(store)
mcs/ops/release_apply_checks.py:426:        if release_state._read(store) != owner:
mcs/ops/release_apply_checks.py:432:        if release_state._read(store) != owner:
mcs/ops/release_apply_owner.py:101:        owner = release_state._read(control)
mcs/ops/release_apply_owner.py:163:            owner = release_state._read(control)
mcs/ops/release_apply_owner.py:296:            owner = release_state._read(control)
mcs/ops/release_apply_owner.py:498:            owner = release_state._read(control)
mcs/ops/release_apply_owner.py:536:                if release_state._read(control) != owner or _read_frozen_plan(control) != prior:
mcs/ops/release_apply_owner.py:598:            owner = release_state._read(control)
mcs/ops/release_apply_owner.py:880:            baseline = release_state._read(control)
mcs/ops/release_apply_owner.py:930:                    witness = release_state._read(control)
mcs/ops/release_apply_owner.py:1010:                    published = release_state._read(control)
mcs/ops/release_apply_owner.py:1024:                    published = release_state._read(control)
mcs/ops/release_apply_owner.py:1108:        value = release_state._read(control)
mcs/ops/release_apply_owner.py:1282:            saved = release_state._read(entry.store)
mcs/ops/release_apply_owner.py:1375:        owner = release_state._read(entry.store)

=== legacy G-gate chain (_call_legacy/_legacy_entry/_release_legacy_scope/_legacy_scope/_open_release_gate) ===
mcs/ops/update_recovery.py:46:    _legacy_entry,
mcs/ops/update_recovery.py:94:@_legacy_entry
mcs/ops/update_recovery.py:232:@_legacy_entry
mcs/ops/update_lifecycle_context.py:44:    from release_apply_owner import _legacy_scope as _release_legacy_scope  # noqa: E402
mcs/ops/update_lifecycle_context.py:46:    _release_legacy_scope = None  # Preserve diagnostic startup; never reload from a switched tree.
mcs/ops/update_lifecycle_context.py:224:        if _release_legacy_scope is None:
mcs/ops/update_lifecycle_context.py:226:        with _release_legacy_scope(HOME) as current:
mcs/ops/update_lifecycle_context.py:257:            if _release_legacy_scope is None:
mcs/ops/update_lifecycle_context.py:259:            fd = stack.enter_context(_release_legacy_scope(HOME))
mcs/ops/update_lifecycle_context.py:308:        return _call_legacy(function, *args, **kwargs)
mcs/ops/update_cli.py:41:    _call_legacy,
mcs/ops/update_cli.py:43:    _legacy_entry,
mcs/ops/update_cli.py:88:@_legacy_entry
mcs/ops/update_cli.py:259:    return _call_legacy(_legacy_post_merge, state)
mcs/ops/mcs_update.py:107:    _release_legacy_scope as _release_legacy_scope,
mcs/ops/mcs_update.py:157:    _call_legacy as _call_legacy,
mcs/ops/mcs_update.py:161:    _legacy_entry as _legacy_entry,
mcs/ops/update_apply.py:55:    _call_legacy,
mcs/ops/update_apply.py:58:    _legacy_entry,
mcs/ops/update_apply.py:125:    return _call_legacy(_apply_pipeline, tag, sha, command_id, base_sha, reinstall, install_args,
mcs/ops/update_apply.py:615:@_legacy_entry
--- _legacy_scope/_open_release_gate definitions & callers ---
mcs/ops/settings_migration_reconcile.py:143:    with release_apply_owner._legacy_scope(str(root)):
mcs/ops/update_lifecycle_context.py:44:    from release_apply_owner import _legacy_scope as _release_legacy_scope  # noqa: E402
mcs/ops/update_lifecycle_context.py:46:    _release_legacy_scope = None  # Preserve diagnostic startup; never reload from a switched tree.
mcs/ops/update_lifecycle_context.py:224:        if _release_legacy_scope is None:
mcs/ops/update_lifecycle_context.py:226:        with _release_legacy_scope(HOME) as current:
mcs/ops/update_lifecycle_context.py:257:            if _release_legacy_scope is None:
mcs/ops/update_lifecycle_context.py:259:            fd = stack.enter_context(_release_legacy_scope(HOME))
mcs/ops/release_apply_owner.py:32:    _open_release_gate as _open_release_gate,
mcs/ops/release_apply_owner.py:33:    _legacy_scope as _legacy_scope,
mcs/ops/mcs_update.py:107:    _release_legacy_scope as _release_legacy_scope,
mcs/ops/release_apply_checks.py:457:def _open_release_gate(runtime_home):
mcs/ops/release_apply_checks.py:497:def _legacy_scope(runtime_home):
mcs/ops/release_apply_checks.py:499:    fd = _open_release_gate(runtime_home)
mcs/ops/sdc_support_signatures.py:526:def _release_scope(v, expected):
mcs/ops/sdc_support_signatures.py:641:    _release_scope(trust, bindings)
mcs/ops/sdc_support_signatures.py:653:    _release_scope(current, bindings)
mcs/ops/sdc_support_signatures.py:659:        _release_scope(candidate, bindings)
mcs/ops/sdc_support_signatures.py:686:    _release_scope(authority, bindings)
mcs/ops/sdc_support_signatures.py:687:    _release_scope(entry['payload'], bindings)
scripts/mcs_upgrade.py:61:def _open_release_gate(runtime_home):
scripts/mcs_upgrade.py:101:def _release_scope(runtime_home):
scripts/mcs_upgrade.py:103:    fd = _open_release_gate(runtime_home)
scripts/mcs_upgrade.py:567:                scope.enter_context(_release_scope(runtime_home))
--- lifetime_fds/_inherited_fds/_POST_MERGE_UPDATE_FD ---
mcs/ops/update_execution.py:46:    _POST_MERGE_UPDATE_FD,
mcs/ops/update_execution.py:217:    update_fd = _POST_MERGE_UPDATE_FD.get()
mcs/ops/update_execution.py:220:    with owned_process._inherited_fds(tuple(fd for fd in (run_fd, update_fd) if fd is not None)) as inherited:
mcs/ops/update_lifecycle_context.py:164:_POST_MERGE_UPDATE_FD = ContextVar('mcs_post_merge_update_fd', default=None)
mcs/ops/update_lifecycle_context.py:260:            stack.enter_context(owned_process.lifetime_fds((fd,)))
mcs/ops/update_lifecycle_context.py:294:    with owned_process._inherited_fds(kwargs.pop('pass_fds', ())) as inherited:
mcs/ops/update_cli.py:248:            from release_apply_owner import CHILD_FDS_ENV, _adopt_post_merge
mcs/ops/update_cli.py:249:            locators = os.environ.get(CHILD_FDS_ENV, '').split(':')
mcs/ops/update_cli.py:257:        with owned_process.lifetime_fds(tuple(locks)), _release_git_scope():
mcs/ops/update_runtime_migration.py:145:        with owned_process._inherited_fds(fds) as inherited:
mcs/ops/update_runtime_migration.py:355:        with owned_process.lifetime_fds((update_fd, run_fd)), _release_git_scope(), \
mcs/ops/release_apply_owner.py:42:CHILD_FDS_ENV = 'MCS_RELEASE_CHILD_FDS'
mcs/ops/release_apply_owner.py:911:            lifetime.enter_context(owned_process.lifetime_fds(locks))
mcs/ops/release_apply_owner.py:1212:    with os.fdopen(os.dup(frame.descriptor), 'rb') as bundle, owned_process.lifetime_fds(entry.locks):
mcs/ops/release_apply_owner.py:1361:    with owned_process.lifetime_fds(entry.locks):
mcs/ops/release_apply_owner.py:1393:        with owned_process._inherited_fds((reader, *entry.locks)) as inherited:
mcs/ops/release_apply_owner.py:1394:            env[CHILD_FDS_ENV] = ':'.join(str(inherited[fd]) for fd in (reader, *entry.locks))
mcs/ops/mcs_update.py:142:    _POST_MERGE_UPDATE_FD as _POST_MERGE_UPDATE_FD,
mcs/ops/owned_process.py:12:_LIFETIME_FDS = ContextVar('mcs_owned_process_lifetime_fds', default=())
mcs/ops/owned_process.py:75:    with _inherited_fds() as inherited:
mcs/ops/update_apply.py:50:    _POST_MERGE_UPDATE_FD,
mcs/ops/update_apply.py:156:            lifetime.enter_context(owned_process.lifetime_fds(borrowed.locks))
mcs/ops/update_apply.py:574:                token = _POST_MERGE_UPDATE_FD.set(upd_fd)
mcs/ops/update_apply.py:578:                    _POST_MERGE_UPDATE_FD.reset(token)
--- acquire_update_lock / acquire_run_lock callers (update entries) ---
mcs/ops/update_apply.py:65:    acquire_update_lock,
mcs/ops/update_apply.py:380:                upd_fd = acquire_update_lock()
mcs/ops/update_apply.py:630:    upd_fd = acquire_update_lock()
mcs/ops/update_cli.py:47:    acquire_update_lock,
mcs/ops/update_cli.py:117:    upd_fd = acquire_update_lock()
mcs/ops/update_lifecycle_context.py:38:from writer_lock import RUN_LOCK_POLL_S, UPDATE_MARKER_NAME, acquire_run_lock
mcs/ops/update_lifecycle_context.py:314:    return acquire_run_lock(UPDATE_LOCK)
mcs/ops/update_lifecycle_context.py:322:        fd = acquire_run_lock(RUN_LOCK)
mcs/ops/update_lifecycle_context.py:488:        fd = acquire_run_lock(RUN_LOCK)
mcs/ops/update_recovery.py:54:    acquire_update_lock,
mcs/ops/update_recovery.py:100:    fd = acquire_update_lock()
mcs/ops/update_recovery.py:246:    upd_fd = acquire_update_lock()
mcs/ops/release_apply_owner.py:889:            update_fd = mcs_update.acquire_update_lock()
mcs/ops/release_apply_owner.py:902:                run_fd = mcs_update.acquire_run_lock(mcs_update.RUN_LOCK)
