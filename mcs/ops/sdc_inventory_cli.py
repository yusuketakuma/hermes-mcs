"""公開依存の構成情報を別の同意で有効化・確認・停止します。"""
import json
import os
from pathlib import Path
import sys
import time
import uuid

import mcs_runtime
import mcs_util
from cli_args import JapaneseArgumentParser

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from adapters.sdc.inventory import _name, _version  # noqa: E402
from adapters.sdc.inventory_sender import CONSENT, MAX_PUBLIC_PACKAGES, POLICY_SCHEMA, RESULTS, endpoint_hash, initial_status, validate_policy  # noqa: E402
from adapters.sdc.storage import Store  # noqa: E402


def main(argv=None):
    ap = JapaneseArgumentParser(prog='mcs inventory', description=__doc__)
    ap.add_argument('command', choices=('enable', 'status', 'disable'))
    ap.add_argument('--public-package', action='append', default=[], help='公開であることを確認したPyPIパッケージ名（複数指定可）')
    ap.add_argument('--lock-package', action='append', default=None, help='許可済みパッケージの固定版 name==version（複数指定可）')
    args = ap.parse_args(argv)
    if args.command != 'enable' and (args.public_package or args.lock_package is not None):
        ap.error('パッケージの指定はenableでのみ使えます')
    try:
        cfg = mcs_util.load_config()
        selected_mode = mcs_runtime.mode(cfg) if args.command == 'enable' else None
        # Different venvs may symlink to the same base binary; retain their bin-directory identity.
        if args.command == 'enable' and os.path.realpath(os.path.dirname(mcs_runtime.python_executable(cfg))) != os.path.realpath(os.path.dirname(sys.executable)):
            raise ValueError('SDC_INVENTORY_RUNTIME_UNKNOWN')
        root = cfg.get('sdc', {}).get('state_root')
        with Store(root) as store, store.lock():
            existing = store.read('inventory-policy.json')
            if args.command == 'status':
                status = store.read('inventory-status.json', {})
                enabled = bool(existing and validate_policy(existing)['enabled'])
                print(json.dumps({'enabled': enabled, 'public_package_count': len(existing['public_packages']) if existing else 0,
                                  'lock_status': 'configured' if existing and existing['lock_entries'] is not None else 'unknown',
                                  'last_result': status.get('last_result') if status.get('last_result') in RESULTS else 'SDC_INVENTORY_DISABLED'}, ensure_ascii=False))
                return 0
            if args.command == 'disable':
                if existing is not None:
                    validate_policy(existing)
                    existing['enabled'] = False
                    store.write('inventory-policy.json', existing)
                print('構成情報の新規採取と送信を停止しました。通常の状態監視は継続します。')
                return 0
            state = store.read('state.json', {})
            if state.get('state') != 'active' or not state.get('enabled') or state.get('disconnected') or state.get('central_suspended'):
                raise ValueError('SDC_INVENTORY_CONNECTION_REQUIRED')
            if not args.public_package or len(args.public_package) > MAX_PUBLIC_PACKAGES:
                raise ValueError('SDC_INVENTORY_ALLOWLIST_REQUIRED')
            names = sorted({_name(name) for name in args.public_package})
            locks = None if args.lock_package is None else {}
            for value in args.lock_package or ():
                if value.count('==') != 1:
                    raise ValueError('SDC_INVENTORY_LOCK_INVALID')
                name, version = value.split('==')
                name, version = _name(name), _version(version)
                if name not in names or name in locks and locks[name] != version:
                    raise ValueError('SDC_INVENTORY_LOCK_INVALID')
                locks[name] = version
            policy = {'schema_version': POLICY_SCHEMA, 'enabled': True, 'consent_revision': CONSENT, 'public_packages': names,
                      'lock_entries': locks, 'revision': str(uuid.uuid4()), 'approved_at': int(time.time() * 1000),
                      'device_id': state['device_id'], 'endpoint_hash': endpoint_hash(store.read('endpoint.json'))}
            validate_policy(policy)
            print(f'このMacの選択済み実行環境（{selected_mode}）にある、許可した公開パッケージ{len(names)}件の構成情報を送信します。')
            print('送信項目: 公開パッケージ名と導入版、実行環境、コード版、取得時刻、hash、除外件数、lockとの差異。')
            print('患者情報・秘密値・私有パッケージ名・取得元URL・ローカルパスは含めません。通常の状態監視とは別の許可です。')
            print('公開パッケージの一覧: ' + ', '.join(names))
            print('1: 許可する / 2: 中止する（既定）')
            try:
                answer = input('番号を入力してください [2]: ').strip()
            except (EOFError, KeyboardInterrupt):
                answer = '2'
            if answer != '1':
                print('変更せず終了しました。')
                return 0
            store.write('inventory-policy.json', policy)
            store.write('inventory-status.json', initial_status(policy['revision']))
            print('構成情報の採取と送信を許可しました。中央側でも同じ公開名の許可が必要です。停止: mcs inventory disable')
            return 0
    except FileNotFoundError:
        if args.command in ('status', 'disable'):
            print('構成情報の送信は有効になっていません。')
            return 0
        print('状態監視の接続を先に完了してください。', file=sys.stderr)
        return 1
    except BlockingIOError:
        print('状態監視の処理中です。少し待って再実行してください。', file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        print('SDC_INVENTORY_UNAVAILABLE: 許可設定・接続・実行環境を確認してください。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
