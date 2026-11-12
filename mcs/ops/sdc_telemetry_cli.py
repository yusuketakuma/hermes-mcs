"""固定テレメトリ項目を別の同意で有効化・確認・停止します。"""
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
from adapters.sdc.inventory_sender import endpoint_hash  # noqa: E402
from adapters.sdc.storage import Store  # noqa: E402
from adapters.sdc.telemetry import SECTIONS  # noqa: E402
from adapters.sdc.telemetry_sender import CONSENT, INTERVAL, POLICY_SCHEMA, RESULTS, initial_status, validate_policy  # noqa: E402

_SECTION_TEXT = {
    'hardware': 'CPU・機種・メモリ・OS・ディスク容量',
    'operations': '稼働秒数・起動ID・固定サービス名と状態',
    'logs': '実行ログのイベント名・時刻・重大度（本文は送りません）',
    'mcs': 'ヘルス状態・実行状態・状態理由・バックアップ状態',
}


def main(argv=None):
    ap = JapaneseArgumentParser(prog='mcs telemetry', description=__doc__)
    ap.add_argument('command', choices=('enable', 'status', 'disable'))
    ap.add_argument('--section', action='append', default=[], choices=SECTIONS, help='送信を許可する項目（複数指定可。省略時は全項目）')
    args = ap.parse_args(argv)
    if args.command != 'enable' and args.section:
        ap.error('--sectionはenableでのみ使えます')
    try:
        cfg = mcs_util.load_config()
        selected_mode = mcs_runtime.mode(cfg) if args.command == 'enable' else None
        if args.command == 'enable' and os.path.realpath(os.path.dirname(mcs_runtime.python_executable(cfg))) != os.path.realpath(os.path.dirname(sys.executable)):
            raise ValueError('SDC_TELEMETRY_RUNTIME_UNKNOWN')
        root = cfg.get('sdc', {}).get('state_root')
        with Store(root) as store, store.lock():
            existing = store.read('telemetry-policy.json')
            if args.command == 'status':
                status = store.read('telemetry-status.json', {})
                enabled = bool(existing and validate_policy(existing)['enabled'])
                print(json.dumps({'enabled': enabled, 'sections': existing['sections'] if existing else [],
                                  'interval_seconds': INTERVAL,
                                  'last_result': status.get('last_result') if status.get('last_result') in RESULTS else 'SDC_TELEMETRY_DISABLED'}, ensure_ascii=False))
                return 0
            if args.command == 'disable':
                if existing is not None:
                    validate_policy(existing)
                    existing['enabled'] = False
                    store.write('telemetry-policy.json', existing)
                print('テレメトリの新規採取と送信を停止しました。通常の状態監視は継続します。')
                return 0
            state = store.read('state.json', {})
            if state.get('state') != 'active' or not state.get('enabled') or state.get('disconnected') or state.get('central_suspended'):
                raise ValueError('SDC_TELEMETRY_CONNECTION_REQUIRED')
            sections = sorted(set(args.section) or set(SECTIONS))
            policy = {'schema_version': POLICY_SCHEMA, 'enabled': True, 'consent_revision': CONSENT, 'sections': sections,
                      'revision': str(uuid.uuid4()), 'approved_at': int(time.time() * 1000),
                      'device_id': state['device_id'], 'endpoint_hash': endpoint_hash(store.read('endpoint.json'))}
            validate_policy(policy)
            print(f'このMacの選択済み実行環境（{selected_mode}）から、許可した固定項目を定期的（{INTERVAL}秒間隔）に送信します。')
            for name in sections:
                print(f'  {name}: {_SECTION_TEXT[name]}')
            print('患者情報・秘密値・ホスト名・パス・シリアル・MACアドレス・ログ本文は含めません。通常の状態監視・構成情報・固定診断とは別の許可です。')
            print('1: 許可する / 2: 中止する（既定）')
            try:
                answer = input('番号を入力してください [2]: ').strip()
            except (EOFError, KeyboardInterrupt):
                answer = '2'
            if answer != '1':
                print('変更せず終了しました。')
                return 0
            store.write('telemetry-policy.json', policy)
            store.write('telemetry-status.json', initial_status(policy['revision']))
            print('テレメトリの採取と送信を許可しました。中央側の受信設定も有効である必要があります。停止: mcs telemetry disable')
            return 0
    except FileNotFoundError:
        if args.command in ('status', 'disable'):
            print('テレメトリの送信は有効になっていません。')
            return 0
        print('状態監視の接続を先に完了してください。', file=sys.stderr)
        return 1
    except BlockingIOError:
        print('状態監視の処理中です。少し待って再実行してください。', file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        print('SDC_TELEMETRY_UNAVAILABLE: 許可設定・接続・実行環境を確認してください。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
