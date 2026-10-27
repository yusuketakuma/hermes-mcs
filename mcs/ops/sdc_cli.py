"""自社Macの状態監視を登録・確認・停止・鍵交換します。"""
from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
import platform
import signal
import sys
import time
import secrets

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


def service(enabled, root, *, note=print):
    """Only install or retire the Connector's existing-manager-owned agent."""
    if sys.platform != 'darwin':
        raise ValueError('SDC_MAC_REQUIRED')
    cfg = lifecycle_io.read_owner_config(mcs_util.CONF_PATH)
    cfg['sdc'] = {'enabled': enabled, 'state_root': str(root)}
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


def tick(root=None):
    old_handler = signal.signal(signal.SIGALRM, _deadline)
    old_timer = signal.setitimer(signal.ITIMER_REAL, 60)
    try:
        with Store(root) as store, store.lock():
            state = store.read('state.json', {})
            if not state.get('enabled'):
                return 0
            time.sleep(secrets.randbelow(31))
            connector = Connector(store, Transport(store.read('endpoint.json')))
            connector.tick(facts())
        return 0
    except BlockingIOError:
        return 0
    except (OSError, ValueError, TypeError, KeyError, TimeoutError):
        print('SDC_TICK_UNAVAILABLE', file=sys.stderr)
        return 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, *old_timer)
        signal.signal(signal.SIGALRM, old_handler)


def main(argv=None):
    ap = JapaneseArgumentParser(description=__doc__)
    ap.add_argument('command', choices=('connect', 'connection', 'disconnect', 'rotate-key'))
    ap.add_argument('--state-root', type=Path, help='導入時に承認した専用私有ディレクトリ')
    args = ap.parse_args(argv)
    try:
        with Store(args.state_root, create=args.command == 'connect') as store:
            if args.command == 'disconnect':
                service(False, store.root)
            with store.lock():
                state = store.read('state.json', {})
                if args.command == 'connection':
                    print(json.dumps({key: state.get(key) for key in ('state', 'enabled', 'last_result', 'clock_status', 'central_suspended')}, ensure_ascii=False))
                    return 0
                connector = Connector(store, Transport(store.read('endpoint.json')))
                if args.command == 'disconnect':
                    # The owned job has stopped; persist disable before any network request.
                    state['enabled'] = False
                    store.write('state.json', state)
                    confirmed = connector.disconnect()
                    print('ローカル送信を停止しました。中央停止: ' + ('確認済み' if confirmed else '未確認（オフライン等）'))
                    if input('資格情報だけを削除しますか？ 業務データは残ります。1: 削除 / 2: 保持 [2]: ').strip() == '1':
                        store.remove_credential()
                    return 0
                if args.command == 'rotate-key':
                    complete = connector.rotate()
                    print('鍵を交換しました。接続の再確認は次の送信時に行います。' if complete else '交換候補の期限が切れたため、新しい候補を安全保存しました。mcs rotate-key を再実行してください。')
                    return 0
                if args.command == 'connect' and state.get('state') in ('suspended', 'revoked'):
                    print('停止済みです。中央で旧端末の停止を確認し、新しい登録コードを発行してください。')
                    if input('新しい端末IDと鍵で登録し直しますか？ 古い資格情報は置き換えます。1: 新登録 / 2: 中止 [2]: ').strip() != '1':
                        return 0
                    store.write('credential', {'key': secrets.token_hex(32)})
                    state = {'state': 'pending', 'enabled': False, 'sequence': 0}
                    store.write('state.json', state)
                if state.get('state') == 'active' and not state.get('enabled'):
                    print('送信を停止しています。時刻や設定の原因を直してから、同じ端末IDで再開できます。順序巻戻しの場合は管理者へ新規登録を依頼してください。')
                    if input('同じ端末で送信を再開しますか？ 1: 再開 / 2: 中止 [2]: ').strip() != '1':
                        return 0
                    connector.resume()
                    service(True, store.root)
                    print('端末の現在の許可を確認して、送信を再開しました。')
                    return 0
                token = None if state.get('device_id') else getpass.getpass('登録コード（表示されません）: ')
                status = connector.connect(token)
                if status['state'] == 'active':
                    if not state.get('enabled'):
                        connector.confirm()
                    service(True, store.root)
                    print('登録済みです。状態監視だけが有効です。')
                    return 0
                print('所属: {tenant_name} / 環境: {environment} / 別名: {alias}\n送信範囲: {data_scope}\nfingerprint: {fingerprint}'.format(**status))
                if status['state'] != 'admin_approved':
                    print('管理画面で同じ表示を確認して承認し、その後 mcs connect を再実行してください。')
                    return 0
                if input('双方の表示が一致し、状態監視への送信に同意しますか？ 1: 同意 / 2: 中止 [2]: ').strip() != '1':
                    print('登録を完了していません。')
                    return 0
                connector.confirm()
                service(True, store.root)
                print('接続しました。状態監視は5分周期です。')
                return 0
    except BlockingIOError:
        print('送信処理が終了待ちです。mcs ' + args.command + ' を再実行してください。停止記録と中央確認の完了は、再実行後の結果で確認してください。', file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError, TransportError):
        print('接続操作を完了できませんでした。私有ディレクトリ・承認済み送信先・管理画面の状態を確認してください。秘密値を共有しないでください。', file=sys.stderr)
        return 1
