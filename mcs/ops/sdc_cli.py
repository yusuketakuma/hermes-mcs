"""自社・顧客Macの状態監視を登録・確認・停止・鍵交換します。"""
from __future__ import annotations

import getpass
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import sys
import time
import secrets
import stat
import uuid

import lifecycle_io
import mcs_runtime
import mcs_util
import service_contract
import service_management
from cli_args import JapaneseArgumentParser
from safe_io import atomic_write

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from adapters.sdc.connector import Connector  # noqa: E402
from adapters.sdc.storage import Store  # noqa: E402
from adapters.sdc.transport import Transport, TransportError  # noqa: E402
from adapters.sdc.inventory_sender import InventorySender  # noqa: E402


@contextmanager
def _sdc_owner_lock():
    # SDC config writers only. Never hold this lock while waiting for Store.lock.
    path = str(mcs_util.CONF_PATH) + '.sdc-owner.lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size:
            raise ValueError('SDC_OWNER_LOCK_INVALID')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _owner_snapshot():
    def identity():
        try:
            info = os.stat(mcs_util.CONF_PATH, follow_symlinks=False)
        except FileNotFoundError:
            return None
        return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
    before = identity()
    cfg = lifecycle_io.read_owner_config(mcs_util.CONF_PATH)
    after = identity()
    if before != after:
        raise ValueError('SDC_OWNER_CHANGED')
    digest = hashlib.sha256(json.dumps(cfg, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return {'path': str(Path(mcs_util.CONF_PATH).absolute()), 'file_identity': after, 'config_sha256': digest}


def _save_enrollment_consent(store, connector, status):
    def prepare(receipt):
        if receipt['pending_expires_at'] is None:
            return  # Legacy explicit manual enrollment does not enable pending polling.
        with _sdc_owner_lock():
            owner = _owner_snapshot()
        state = store.read('state.json')
        state['enrollment_install'] = {'schema_version': 'sdc.enrollment-install.v1',
                                       'receipt_hash': connector._receipt_hash(receipt), 'owner': owner}
        store.write('state.json', state)
    # Snapshot/intent failures leave no new receipt; receipt failures cannot confirm.
    return connector.save_enrollment_consent(status, before_save=prepare)


def _validated_install_intent(store, connector):
    state = store.read('state.json')
    receipt = connector._consent_receipt(state, connector.secret(state, store.read('credential')))
    intent = state.get('enrollment_install')
    if (not isinstance(intent, dict) or set(intent) != {'schema_version', 'receipt_hash', 'owner'}
            or intent.get('schema_version') != 'sdc.enrollment-install.v1'
            or intent.get('receipt_hash') != connector._receipt_hash(receipt)
            or int(connector.clock() * 1000) >= receipt['pending_expires_at']):
        raise ValueError('SDC_LOCAL_START_NOT_PERMITTED')
    return receipt, intent


def _install_enrollment_job(store, connector):
    _, intent = _validated_install_intent(store, connector)
    service(True, store.root, expected_owner=intent['owner'])


def _retry_enrollment_install(store, connector):
    receipt, intent = _validated_install_intent(store, connector)
    with _sdc_owner_lock():
        if intent['owner'] != _owner_snapshot():
            raise ValueError('SDC_OWNER_CHANGED')
    identity = receipt['identity']
    print('保存済み同意: 所属: {tenant_name} / 環境: {environment} / 別名: {alias}\n送信範囲: {data_scope}\nfingerprint: {fingerprint}'.format(**identity))
    if input('本体設定への保存が未完了です。同じ内容で専用ジョブの設定保存を再試行しますか？ 1: 再試行 / 2: 中止 [2]: ').strip() != '1':
        return False
    _install_enrollment_job(store, connector)
    return True


def service(enabled, root, *, note=print, reconcile_only=False, expected_owner=None):
    """Only install or retire the Connector's existing-manager-owned agent."""
    if sys.platform != 'darwin':
        raise ValueError('SDC_MAC_REQUIRED')
    with _sdc_owner_lock():
        cfg = lifecycle_io.read_owner_config(mcs_util.CONF_PATH)
        if reconcile_only:
            if enabled is not True or not _enrollment_config_admits(cfg, Path(root)):
                raise ValueError('SDC_LOCAL_START_NOT_PERMITTED')
        else:
            if expected_owner is not None and expected_owner != _owner_snapshot():
                raise ValueError('SDC_OWNER_CHANGED')
            cfg['sdc'] = {**cfg.get('sdc', {}), 'enabled': enabled, 'state_root': str(root)}
            atomic_write(mcs_util.CONF_PATH, lambda stream: json.dump(cfg, stream, ensure_ascii=False, indent=2), mode=0o600, makedirs=False)
    paths = service_management.default_paths(root=mcs_util.HOME, checkout=REPO)
    if not enabled:
        if service_management.retire_agents({service_contract.SDC_LABEL}, note, False, paths=paths):
            raise ValueError('SDC_LOCAL_STOP_UNCONFIRMED')
        return
    body = service_management.desired_agents(service_management.service_substitutions(cfg, paths=paths), cfg, paths=paths)[service_contract.SDC_LABEL]
    dst = str(Path(paths.agents_dir) / (service_contract.SDC_LABEL + '.plist'))
    Path(paths.agents_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
    service_management.write_service_file(dst, body)
    if not service_management.agent_reconcile(service_contract.SDC_LABEL, dst, note, False, paths=paths):
        raise ValueError('SDC_LOCAL_START_UNCONFIRMED')


def facts():
    """Read only fixed version/runtime sources; unknown is not evidence of health."""
    try:
        mode = mcs_runtime.mode(mcs_util.load_config())
    except ValueError:
        mode = 'unknown'
    commit = None
    revision = lifecycle_io.run_bounded(['/usr/bin/git', '--no-optional-locks', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null', '-C', str(REPO), 'rev-parse', 'HEAD'], timeout=2)
    dirty = lifecycle_io.run_bounded(['/usr/bin/git', '--no-optional-locks', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null', '-C', str(REPO), 'status', '--porcelain', '--untracked-files=normal'], timeout=2)
    if revision.returncode == 0 and dirty.returncode == 0 and not dirty.stdout.strip():
        commit = revision.stdout.strip()
    process = 'unknown'
    if sys.platform == 'darwin' and mode in ('standalone', 'hermes'):
        label = service_contract.STANDALONE_LABEL if mode == 'standalone' else 'ai.hermes.gateway'
        observed = lifecycle_io.run_bounded(['/bin/launchctl', 'print', f'gui/{os.getuid()}/{label}'], timeout=2)
        if observed.returncode == 0:
            lines = {line.strip() for line in observed.stdout.splitlines()}
            if 'state = running' in lines:
                process = 'running'
            elif 'state = not running' in lines or 'state = waiting' in lines:
                process = 'stopped'
    return {'mode': mode, 'python_version': platform.python_version(), 'macos_version': platform.mac_ver()[0] or None, 'mcs_commit': commit, 'mcs_process': process, 'loaded_commit': None}


def _deadline(_signum, _frame):
    raise TimeoutError('SDC_TICK_DEADLINE')


def _enrollment_admitted(root):
    cfg = lifecycle_io.read_owner_config(mcs_util.CONF_PATH)
    return _enrollment_config_admits(cfg, root)


def _enrollment_config_admits(cfg, root):
    settings = cfg.get('sdc', {})
    return isinstance(settings, dict) and settings.get('enabled') is True and isinstance(settings.get('state_root'), str) and Path(settings['state_root']).absolute() == root


def tick(root=None):
    started = time.monotonic()
    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)
    parent = started + old_timer[0] if old_timer[0] else None
    end = min(started + 60, parent) if parent is not None else started + 60
    parent_fired, tick_alarm_fired, installed = False, False, False

    def expire(signum, frame):
        nonlocal parent_fired, tick_alarm_fired
        tick_alarm_fired = True
        if parent is not None and time.monotonic() >= parent:
            parent_fired = True
            old_handler(signum, frame)
        _deadline(signum, frame)

    def check_budget():
        if time.monotonic() >= end:
            expire(signal.SIGALRM, None)

    try:
        if parent is not None and not callable(old_handler):
            raise ValueError('SDC_TICK_DEADLINE_UNAVAILABLE')
        signal.signal(signal.SIGALRM, expire)
        installed = True
        check_budget()
        signal.setitimer(signal.ITIMER_REAL, max(1e-6, end - time.monotonic()))
        with Store(root, credential_deadline=end) as store, store.lock():
            check_budget()
            # A leftover job must follow the current owner config, not a stale active state.
            if not _enrollment_admitted(store.root):
                return 0
            state = store.read('state.json', {})
            try:
                store.guard_credential()
            except ValueError as error:
                if str(error) != 'SDC_CREDENTIAL_MUTATION_UNRESOLVED':
                    raise
                # Held for an explicit reconcile; no sender (R1, enrollment, inventory, diagnostics) runs.
                print('SDC_CREDENTIAL_MUTATION_UNRESOLVED', file=sys.stderr)
                return 0
            if state.get('state') == 'pending':
                # The same single job polls enrollment; pending never reaches either sender.
                cfg = mcs_util.load_config()
                if not _enrollment_admitted(store.root):
                    return 0
                if os.path.realpath(os.path.dirname(mcs_runtime.python_executable(cfg))) != os.path.realpath(os.path.dirname(sys.executable)):
                    return 0
                Connector(store, Transport(store.read('endpoint.json')), admit=lambda: _enrollment_admitted(store.root)).poll_enrollment()
                return 0
            if not state.get('enabled'):
                return 0
            time.sleep(secrets.randbelow(31))
            check_budget()
            if not _enrollment_admitted(store.root):
                return 0
            transport = Transport(store.read('endpoint.json'))
            connector = Connector(store, transport)
            observed = facts()
            if not _enrollment_admitted(store.root):
                return 0
            connector.tick(observed)
            check_budget()
            if not _enrollment_admitted(store.root):
                return 0
            try:
                inventory_policy = store.read('inventory-policy.json')
                if inventory_policy and inventory_policy.get('enabled'):
                    cfg = mcs_util.load_config()
                    if observed.get('mode') != mcs_runtime.mode(cfg) or os.path.realpath(os.path.dirname(mcs_runtime.python_executable(cfg))) != os.path.realpath(os.path.dirname(sys.executable)):
                        print('SDC_INVENTORY_RUNTIME_UNKNOWN', file=sys.stderr)
                        return 0
                InventorySender(store, transport).tick(observed)
            except TimeoutError:
                if tick_alarm_fired or time.monotonic() >= end:
                    raise
                print('SDC_INVENTORY_UNAVAILABLE', file=sys.stderr)
            except Exception:
                print('SDC_INVENTORY_UNAVAILABLE', file=sys.stderr)
            check_budget()
            if not _enrollment_admitted(store.root):
                return 0
            try:
                from adapters.sdc import diagnostic_sender, diagnostic_state
                if diagnostic_state.status(store, now_ms=int(time.time() * 1000))['enabled']:
                    cfg = mcs_util.load_config()
                    if observed.get('mode') != mcs_runtime.mode(cfg) or os.path.realpath(os.path.dirname(mcs_runtime.python_executable(cfg))) != os.path.realpath(os.path.dirname(sys.executable)):
                        print('SDC_DIAGNOSTIC_RUNTIME_UNKNOWN', file=sys.stderr)
                        return 0
                    from sdc_diagnostic_events import collect_events

                    def provider(**kwargs):
                        return collect_events(trusted_root=mcs_util.HOME, mode=observed['mode'], **kwargs)

                    diagnostic_sender.run_managed_once(store, provider=provider, deadline=end)
            except TimeoutError:
                if tick_alarm_fired or time.monotonic() >= end:
                    raise
                print('SDC_DIAGNOSTIC_UNAVAILABLE', file=sys.stderr)
            except (OSError, ValueError, TypeError, KeyError):
                print('SDC_DIAGNOSTIC_UNAVAILABLE', file=sys.stderr)
            check_budget()
        return 0
    except BlockingIOError:
        return 0
    except (OSError, ValueError, TypeError, KeyError, TimeoutError):
        print('SDC_TICK_UNAVAILABLE', file=sys.stderr)
        return 1
    finally:
        if installed:
            try:
                signal.setitimer(signal.ITIMER_REAL, 0)
            finally:
                try:
                    signal.signal(signal.SIGALRM, old_handler)
                finally:
                    if parent is not None:
                        rest = parent - time.monotonic()
                        if parent_fired:
                            if old_timer[1]:
                                signal.setitimer(signal.ITIMER_REAL, max(1e-6, rest % old_timer[1]), old_timer[1])
                        else:
                            signal.setitimer(signal.ITIMER_REAL, max(1e-6, rest), old_timer[1])


def main(argv=None):
    ap = JapaneseArgumentParser(description=__doc__)
    ap.add_argument('command', choices=('connect', 'connection', 'disconnect', 'rotate-key'))
    ap.add_argument('--state-root', type=Path, help='導入時に承認した専用私有ディレクトリ')
    ap.add_argument('--customer', action='store_true', help='新しい私有領域とKeychainで顧客の状態監視を登録する')
    ap.add_argument('--reconcile-credential', action='store_true',
                    help='記録された資格情報の操作を現在の値と一度だけ照合して完了し、他の操作はしない')
    import sdc_provision_cli
    sdc_provision_cli.add_arguments(ap)
    args = ap.parse_args(argv)
    sdc_provision_cli.validate_arguments(ap, args)
    if args.reconcile_credential and (args.command not in ('connect', 'rotate-key', 'disconnect') or not args.confirm_human
                                      or args.customer or sdc_provision_cli.requested(args)):
        ap.error('--reconcile-credential は connect / rotate-key / disconnect と --confirm-human・--reason だけに指定してください')
    if sdc_provision_cli.requested(args):
        return sdc_provision_cli.run(args, connect=lambda store: _connection(store, args))
    if args.customer and args.command != 'connect':
        ap.error('--customer は connect だけに指定できます')
    try:
        root = args.state_root
        if args.customer:
            current = mcs_util.load_config().get('sdc', {})
            selected = root or Path.home() / '.mcs-security-customer'
            if current.get('enabled') and current.get('state_root') and Path(current['state_root']).resolve() != selected.resolve():
                print('接続を完了すると監視ジョブが顧客用へ切り替わり、現在の監視先への heartbeat 送信は停止します。資格情報とファイルは保持します。')
        if root is None:
            root = Path.home() / '.mcs-security-customer' if args.customer else mcs_util.load_config().get('sdc', {}).get('state_root')
        with Store(root, create=args.command == 'connect') as store:
            return _connection(store, args)
    except BlockingIOError:
        print('送信処理が終了待ちです。同じ指定で mcs ' + args.command + ' を再実行してください。停止記録と中央確認の完了は、再実行後の結果で確認してください。', file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError, TransportError):
        print('接続操作を完了できませんでした。私有ディレクトリ・承認済み送信先・管理画面の状態を確認してください。秘密値を共有しないでください。', file=sys.stderr)
        return 1


def _unfinished_credential_change(store, args):
    """Hold or explicitly reconcile a recorded change; never continue into a new change, HTTP or re-enable."""
    marker = store.read('credential-store.json', {})  # Metadata only: no Keychain access.
    operation = marker.get('mutation') if isinstance(marker, dict) else None
    if operation is None or operation['phase'] == 'complete':
        return None
    if operation['phase'] == 'pending':
        print('結果を確認できない資格情報の操作が記録されているため停止しています。この私有領域では自動で再試行・修復しません。'
              '状態表示と送信停止は使えます。新しい私有領域での登録し直しを管理者と確認してください。', file=sys.stderr)
        return 1
    print('完了前の資格情報の操作が記録されています。同じコマンドに --reconcile-credential、--confirm-human と'
          '秘密を含まない半角の --reason を付けて実行すると、現在の値を一度だけ照合して記録を完了します。', file=sys.stderr)
    return 1


def _reconcile_recorded_change(store, args):
    """The explicit recovery choice: one actual check of the recorded change, then stop."""
    with store.lock():
        marker = store.read('credential-store.json', {})  # Metadata only: no Keychain access.
        operation = marker.get('mutation') if isinstance(marker, dict) else None
        if operation is None:
            print('照合する資格情報の操作記録はありません。', file=sys.stderr)
            return 1
        if operation['phase'] == 'pending':
            return _unfinished_credential_change(store, args)
        store.reconcile_credential(confirm_human=True, reason=args.reason, now_ms=int(time.time() * 1000))
    print('記録された資格情報の操作を現在の値と照合し、完了として保存しました。送信は自動では再開しません。'
          '続きの操作は通常の同じコマンドで実行してください。')
    return 0


def _disconnect(store, args):
    """Persist the local stop first; ask about deletion only after the lock is released."""
    with store.lock():
        state = store.read('state.json', {})
        connector = Connector(store, Transport(store.read('endpoint.json')), admit=lambda: _enrollment_admitted(store.root))
        # The owned job has stopped; persist disable before any network request.
        state['enabled'] = False
        store.write('state.json', state)
        confirmed = connector.disconnect()
    print('ローカル送信を停止しました。中央停止: ' + ('確認済み' if confirmed else '未確認（オフライン等）'))
    if input('資格情報だけを削除しますか？ 業務データは残ります。1: 削除 / 2: 保持 [2]: ').strip() == '1':
        if store.customer and not args.confirm_human:
            print('顧客のKeychainの資格情報を削除するには、同じコマンドに --confirm-human と秘密を含まない半角の'
                  ' --reason を付けて実行してください。ローカル送信の停止は完了しています。', file=sys.stderr)
            return 1
        if args.confirm_human:
            store.authorize_credential_mutation(operation='disconnect', confirm_human=True, reason=args.reason,
                                                now_ms=int(time.time() * 1000))
        with store.lock():
            unfinished = _unfinished_credential_change(store, args)
            if unfinished is not None:
                return unfinished
            store.remove_credential(mutation={'purpose': 'delete'})
    return 0


def _connection(store, args):
    """Use the caller-owned Store for the existing R1 connection operation."""
    if args.reconcile_credential:
        return _reconcile_recorded_change(store, args)
    if args.command == 'connect':
        from adapters.sdc import provision
        provision.begin_registration(store, now_ms=int(time.time() * 1000))
    if args.command == 'disconnect':
        store.read('state.json', {})
        if not store.customer:
            store.read('credential')
        service(False, store.root)
        return _disconnect(store, args)
    if args.confirm_human and args.command in ('connect', 'rotate-key'):
        # The flag is the human confirmation; it is taken before the lock and re-bound inside it.
        store.authorize_credential_mutation(operation=args.command, confirm_human=True, reason=args.reason,
                                            now_ms=int(time.time() * 1000))
    reenroll = False
    if args.command == 'connect' and store.read('state.json', {}).get('state') in ('suspended', 'revoked'):
        print('停止済みです。中央で旧端末の停止を確認し、新しい登録コードを発行してください。')
        if input('新しい端末IDと鍵で登録し直しますか？ 古い資格情報は置き換えます。1: 新登録 / 2: 中止 [2]: ').strip() != '1':
            return 0
        reenroll = True
    token = None
    if args.command == 'connect':
        # Ask for the one-time code before the lock, from metadata only; it stays in RAM and is never saved.
        current = store.read('state.json', {})
        marker = store.read('credential-store.json', {})
        operation = marker.get('mutation') if isinstance(marker, dict) else None
        held = (operation is not None and operation['phase'] != 'complete'
                or current.get('enrollment_hold') and current.get('state') not in ('suspended', 'revoked'))
        if reenroll or not (current.get('device_id') or held):
            token = getpass.getpass('登録コード（表示されません）: ')
    with store.lock():
        if args.customer:
            store.enable_customer()
        state = store.read('state.json', {})
        if args.command == 'connection':
            print(json.dumps({key: state.get(key) for key in ('state', 'enabled', 'last_result', 'clock_status', 'central_suspended')}, ensure_ascii=False))
            return 0
        unfinished = _unfinished_credential_change(store, args)
        if unfinished is not None:
            return unfinished
        if args.command == 'connect' and state.get('enrollment_hold') and state.get('state') not in ('suspended', 'revoked'):
            print('登録の自動確認を保留しています。管理者に登録状態を確認してください。停止理由は自動解除しません。')
            return 1
        connector = Connector(store, Transport(store.read('endpoint.json')), admit=lambda: _enrollment_admitted(store.root))
        if args.command == 'rotate-key':
            complete = connector.rotate()
            print('鍵を交換しました。接続の再確認は次の送信時に行います。' if complete else '交換候補の期限が切れたため、新しい候補を安全保存しました。mcs rotate-key を再実行してください。')
            return 0
        if args.command == 'connect' and state.get('state') in ('suspended', 'revoked'):
            if not reenroll:
                raise ValueError('SDC_STATE_CHANGED')  # Stopped after the unlocked confirmation.
            state = {'claim_id': str(uuid.uuid4()), 'state': 'pending', 'sequence': 0, 'enabled': False}
            connector.save_credential({'key': secrets.token_hex(32)}, 'reenroll-key', state)
        if state.get('state') == 'active' and not state.get('enabled'):
            print('送信を停止しています。時刻や設定の原因を直してから、同じ端末IDで再開できます。順序巻戻しの場合は管理者へ新規登録を依頼してください。')
            if input('同じ端末で送信を再開しますか？ 1: 再開 / 2: 中止 [2]: ').strip() != '1':
                return 0
            connector.resume()
            service(True, store.root)
            print('端末の現在の許可を確認して、送信を再開しました。')
            return 0
        receipt = store.read('enrollment-consent.json', {})
        same_claim = isinstance(receipt.get('identity'), dict) and receipt['identity'].get('claim_id') == state.get('claim_id')
        if state.get('state') == 'pending' and state.get('device_id') and same_claim and (receipt.get('pending_expires_at') is not None or state.get('enrollment_confirmation')):
            installed = False
            if not _enrollment_admitted(store.root):
                installed = _retry_enrollment_install(store, connector)
                if not installed:
                    return 0
            result = connector.poll_enrollment()
            if result in ('SDC_ENROLLMENT_PENDING', 'SDC_ENROLLMENT_ACTIVE', 'SDC_ENROLLMENT_NOT_DUE'):
                # Explicit CLI repair only: never restore a disabled owner config.
                if not installed:
                    service(True, store.root, reconcile_only=True)
                print('保存済みの同意で登録状態を確認しました。管理者の承認前は監視情報を送りません。')
                return 0
            print('保存済みの同意では自動接続できません。期限・管理画面の登録状態を確認してください。')
            return 1
        if not state.get('device_id') and token is None:
            raise ValueError('SDC_STATE_CHANGED')  # Changed after the unlocked input; never prompt under the lock.
        status = connector.connect(None if state.get('device_id') else token)
        if status['state'] == 'active' and state.get('state') == 'active':
            service(True, store.root)
            print('登録済みです。状態監視だけが有効です。')
            return 0
        print('所属: {tenant_name} / 環境: {environment} / 別名: {alias}\n送信範囲: {data_scope}\nfingerprint: {fingerprint}'.format(**status))
        if status['state'] not in ('admin_approved', 'active'):
            if type(status.get('pending_expires_at')) is int:
                if input('この表示内容と状態監視だけの送信に同意し、管理者の承認後に自動接続しますか？ 1: 同意 / 2: 中止 [2]: ').strip() == '1':
                    _save_enrollment_consent(store, connector, status)
                    _install_enrollment_job(store, connector)
                    print('同意を保存しました。管理者の承認を5分周期で確認します。承認前は監視情報を送りません。')
                return 0
            command = 'mcs connect --customer' if store.customer else 'mcs connect'
            print('中央の登録期限を確認できないため自動接続は未設定です。管理画面で同じ表示を確認して承認し、その後 ' + command + ' を同じ私有領域の指定で再実行してください。')
            return 0
        if input('双方の表示が一致し、状態監視への送信に同意しますか？ 1: 同意 / 2: 中止 [2]: ').strip() != '1':
            print('登録を完了していません。')
            return 0
        automatic = _save_enrollment_consent(store, connector, status)
        if automatic:
            # Install only after the receipt is durable. Lost ACKs use status next tick.
            _install_enrollment_job(store, connector)
            result = connector.poll_enrollment()
            if result != 'SDC_ENROLLMENT_ACTIVE':
                print('同意は保存済みですが接続は未完了です。期限・専用ジョブ・管理画面の登録状態を確認してください。')
                return 0 if result in ('SDC_ENROLLMENT_PENDING', 'SDC_ENROLLMENT_NOT_DUE') else 1
        else:
            connector.confirm()
            service(True, store.root)
        print('接続しました。状態監視は5分周期です。')
        return 0
