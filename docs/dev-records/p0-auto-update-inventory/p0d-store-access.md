=== store.read('...') calls ===
adapters/sdc/connector.py:17:        self.config = store.read('endpoint.json')
adapters/sdc/connector.py:46:        state = self.store.read('state.json', {})
adapters/sdc/connector.py:47:        credential = self.store.read('credential', {})
adapters/sdc/connector.py:90:        state = self.store.read('state.json')
adapters/sdc/connector.py:91:        key = self.secret(state, self.store.read('credential'))
adapters/sdc/connector.py:92:        receipt = self.store.read('enrollment-consent.json', {})
adapters/sdc/connector.py:123:        state = self.store.read('state.json')
adapters/sdc/connector.py:126:        key = self.secret(state, self.store.read('credential'))
adapters/sdc/connector.py:133:        state = self.store.read('state.json')
adapters/sdc/connector.py:134:        identity = self._enrollment_identity(result, state, self.secret(state, self.store.read('credential')))
adapters/sdc/connector.py:139:                   'key_sha256': hashlib.sha256(bytes.fromhex(self.secret(state, self.store.read('credential')))).hexdigest(),
adapters/sdc/connector.py:145:        if self.store.read('enrollment-consent.json') != receipt:
adapters/sdc/connector.py:150:        receipt = self.store.read('enrollment-consent.json', {})
adapters/sdc/connector.py:177:        state = self.store.read('state.json', {})
adapters/sdc/connector.py:184:        key = self.secret(state, self.store.read('credential'))
adapters/sdc/connector.py:246:        state = self.store.read('state.json')
adapters/sdc/connector.py:247:        credential = self.store.read('credential')
adapters/sdc/connector.py:307:        state = self.store.read('state.json')
adapters/sdc/connector.py:310:        credential = self.store.read('credential')
adapters/sdc/connector.py:316:        state = self.store.read('state.json', {})
adapters/sdc/connector.py:317:        credential = self.store.read('credential') if not self.store.customer else None
adapters/sdc/connector.py:323:            result = self.request('/v2/device/suspend', {}, state, self.secret(state, credential if credential is not None else self.store.read('credential')))
adapters/sdc/connector.py:345:        state = self.store.read('state.json', {})
adapters/sdc/connector.py:348:        credential = self.store.read('credential')
adapters/sdc/diagnostic_sender.py:42:        monitor = endpoint(store.read('endpoint.json'))
adapters/sdc/diagnostic_sender.py:47:        state = store.read('state.json')
adapters/sdc/diagnostic_sender.py:82:        credential = store.read('credential')
adapters/sdc/inventory_sender.py:101:        policy = self.store.read('inventory-policy.json')
adapters/sdc/inventory_sender.py:107:        state = self.store.read('state.json', {})
adapters/sdc/inventory_sender.py:115:        status = self.store.read('inventory-status.json') or initial_status(policy['revision'])
adapters/sdc/inventory_sender.py:128:        target_hash = endpoint_hash(self.store.read('endpoint.json'))
adapters/sdc/inventory_sender.py:131:        spool = self.store.read('inventory-spool.json')
adapters/sdc/inventory_sender.py:171:        current_policy = self.store.read('inventory-policy.json')
adapters/sdc/inventory_sender.py:172:        current_state = self.store.read('state.json', {})
adapters/sdc/inventory_sender.py:177:        if endpoint_hash(self.store.read('endpoint.json')) != policy['endpoint_hash']:
adapters/sdc/inventory_sender.py:181:            result = connector.request(PATH, raw, current_state, connector.secret(current_state, self.store.read('credential')))
adapters/sdc/provision.py:615:    guard_registration(store, monitor, store.read('credential'))
adapters/sdc/provision.py:622:    credential = store.read('credential')
adapters/sdc/restart_planning.py:237:    _check(not state['disabled'] and not restart['disabled'] and store.read('restart-execution.json') is None, 'CONFLICT')
adapters/sdc/restart_planning.py:243:    target = endpoint(store.read('endpoint.json'))
adapters/sdc/support_client.py:69:    value = store.read('support-bootstrap.json')
adapters/sdc/support_client.py:79:    config = store.read('endpoint.json')
adapters/sdc/support_client.py:102:        state = store.read('state.json')
adapters/sdc/support_client.py:107:        credential = store.read('credential')  # Existing customer Keychain read is noninteractive.
mcs/ops/sdc_cli.py:91:        state = store.read('state.json')
mcs/ops/sdc_cli.py:100:    state = store.read('state.json')
mcs/ops/sdc_cli.py:101:    receipt = connector._consent_receipt(state, connector.secret(state, store.read('credential')))
mcs/ops/sdc_cli.py:245:            state = store.read('state.json', {})
mcs/ops/sdc_cli.py:261:                Connector(store, Transport(store.read('endpoint.json')), admit=lambda: _enrollment_admitted(store.root)).poll_enrollment()
mcs/ops/sdc_cli.py:269:            transport = Transport(store.read('endpoint.json'))
mcs/ops/sdc_cli.py:279:                inventory_policy = store.read('inventory-policy.json')
mcs/ops/sdc_cli.py:399:    marker = store.read('credential-store.json', {})  # Metadata only: no Keychain access.
mcs/ops/sdc_cli.py:415:        marker = store.read('credential-store.json', {})  # Metadata only: no Keychain access.
mcs/ops/sdc_cli.py:431:        state = store.read('state.json', {})
mcs/ops/sdc_cli.py:432:        connector = Connector(store, Transport(store.read('endpoint.json')), admit=lambda: _enrollment_admitted(store.root))
mcs/ops/sdc_cli.py:462:        store.read('state.json', {})
mcs/ops/sdc_cli.py:464:            store.read('credential')
mcs/ops/sdc_cli.py:472:    if args.command == 'connect' and store.read('state.json', {}).get('state') in ('suspended', 'revoked'):
mcs/ops/sdc_cli.py:480:        current = store.read('state.json', {})
mcs/ops/sdc_cli.py:481:        marker = store.read('credential-store.json', {})
mcs/ops/sdc_cli.py:490:        state = store.read('state.json', {})
mcs/ops/sdc_cli.py:500:        connector = Connector(store, Transport(store.read('endpoint.json')), admit=lambda: _enrollment_admitted(store.root))
mcs/ops/sdc_cli.py:518:        receipt = store.read('enrollment-consent.json', {})
mcs/ops/sdc_inventory_cli.py:37:            existing = store.read('inventory-policy.json')
mcs/ops/sdc_inventory_cli.py:39:                status = store.read('inventory-status.json', {})
mcs/ops/sdc_inventory_cli.py:52:            state = store.read('state.json', {})
mcs/ops/sdc_inventory_cli.py:69:                      'device_id': state['device_id'], 'endpoint_hash': endpoint_hash(store.read('endpoint.json'))}
mcs/ops/setup/setup_wizard.py:506:            endpoint(store.read('endpoint.json'))
mcs/ops/setup/setup_wizard.py:508:            kind = 'customer' if store.customer else ('self' if store.read('credential', {}) or store.read('state.json', {}) else 'new')
=== store.write('...') calls ===
adapters/sdc/connector.py:33:            self.store.write('credential', credential, mutation={'purpose': purpose, 'after': {
adapters/sdc/connector.py:36:            self.store.write('credential', credential)
adapters/sdc/connector.py:37:            self.store.write('state.json', state)
adapters/sdc/connector.py:57:                self.store.write('credential', credential)  # Self-mode resume; a customer root rejects this.
adapters/sdc/connector.py:63:            self.store.write('state.json', state)
adapters/sdc/connector.py:103:        self.store.write('state.json', state)
adapters/sdc/connector.py:143:        self.store.write('enrollment-consent.json', receipt)
adapters/sdc/connector.py:208:            self.store.write('state.json', state)
adapters/sdc/connector.py:216:                    self.store.write('state.json', state)
adapters/sdc/connector.py:227:            self.store.write('state.json', state)
adapters/sdc/connector.py:235:                self.store.write('state.json', state)
adapters/sdc/connector.py:238:                self.store.write('state.json', state)
adapters/sdc/connector.py:242:            self.store.write('state.json', state)
adapters/sdc/connector.py:252:            self.store.write('state.json', state)
adapters/sdc/connector.py:264:            self.store.write('state.json', state)
adapters/sdc/connector.py:295:        self.store.write('state.json', state)
adapters/sdc/connector.py:313:        self.store.write('state.json', state)
adapters/sdc/connector.py:321:        self.store.write('state.json', state)
adapters/sdc/connector.py:328:        self.store.write('state.json', state)
adapters/sdc/connector.py:355:            self.store.write('state.json', state)
adapters/sdc/connector.py:365:        self.store.write('state.json', state)
adapters/sdc/connector.py:392:        self.store.write('state.json', state)
adapters/sdc/inventory_sender.py:96:        self.store.write('inventory-status.json', status)
adapters/sdc/inventory_sender.py:141:            self.store.write('inventory-status.json', status)
adapters/sdc/inventory_sender.py:155:            self.store.write('inventory-spool.json', spool)
adapters/sdc/inventory_sender.py:159:            self.store.write('inventory-status.json', status)
mcs/ops/sdc_cli.py:94:        store.write('state.json', state)
mcs/ops/sdc_cli.py:435:        store.write('state.json', state)
mcs/ops/sdc_inventory_cli.py:49:                    store.write('inventory-policy.json', existing)
mcs/ops/sdc_inventory_cli.py:83:            store.write('inventory-policy.json', policy)
mcs/ops/sdc_inventory_cli.py:84:            store.write('inventory-status.json', initial_status(policy['revision']))
=== store._file_lock / lock names ===
adapters/sdc/storage.py:602:        with self._file_lock('tick.lock') as fd:
adapters/sdc/storage.py:613:        with self._file_lock('diagnostic-state.lock'):
adapters/sdc/storage.py:639:        with self._file_lock('support-state.lock'):
adapters/sdc/storage.py:645:        with self._file_lock('support-session.lock'):
adapters/sdc/storage.py:651:        with self._file_lock('restart-state.lock'):
adapters/sdc/storage.py:659:        with self._file_lock('release-authority.lock') as fd:
adapters/sdc/storage.py:669:        with self._file_lock('release-owner.lock') as fd:
adapters/sdc/storage.py:677:        with self._file_lock('release-entry.lock', shared=shared) as fd:
=== Store( instantiations ===
mcs/ops/release_apply_checks.py:397:    with Store(root) as store, store.release_owner_lock():
mcs/ops/release_apply_checks.py:451:    with Store(root.parent, create=True):
mcs/ops/release_apply_checks.py:453:    with Store(root, create=True) as store:
mcs/ops/sdc_cli.py:240:        with Store(root, credential_deadline=end) as store, store.lock():
mcs/ops/sdc_cli.py:385:        with Store(root, create=args.command == 'connect') as store:
mcs/ops/sdc_diagnostic_cli.py:127:            store = Store(root)
mcs/ops/sdc_inventory_cli.py:36:        with Store(root) as store, store.lock():
mcs/ops/sdc_provision_cli.py:151:            with Store(args.state_root, create_new=True) as store:
mcs/ops/sdc_provision_cli.py:157:            with Store(args.state_root) as store:
mcs/ops/sdc_provision_cli.py:193:            with Store(args.state_root) as store:
mcs/ops/sdc_support_cli.py:186:        with Store(root) as store:
mcs/ops/setup/setup_wizard.py:497:        with Store(root) as store:
mcs_standalone/runtime.py:684:            with Store(settings["state_root"]) as store:
