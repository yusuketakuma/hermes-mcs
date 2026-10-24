"""固定診断イベントの独立したローカル同意・再同意・状態確認・取消を行います。"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

import mcs_util
from cli_args import JapaneseArgumentParser

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from adapters.sdc import diagnostic_sender, diagnostic_state as local, provision  # noqa: E402
from adapters.sdc.diagnostic_contract import COMPONENTS  # noqa: E402
from adapters.sdc.protocol import endpoint  # noqa: E402
from adapters.sdc.storage import Store  # noqa: E402
from adapters.sdc.support_contract import canonical  # noqa: E402


class _Parser(JapaneseArgumentParser):
    def error(self, message):
        # Invalid arguments may contain secrets; never echo argparse's details.
        self.print_usage(sys.stderr)
        self.exit(2, '引数が正しくありません。mcs diagnostics --helpで確認してください。\n')


def _arguments(argv):
    parser = _Parser(prog='mcs diagnostics', description=__doc__, epilog=(
        'status はローカルの閉じた要約だけを表示し、既存の保持期限に従い期限切れ本文を整理します。'
        '中央への照会や診断の実行は行いません。'))
    parser.add_argument('command', choices=('enable', 'renew', 'status', 'disable'))
    parser.add_argument('--state-root', help='設定済みの私有監視領域（指定時は設定ファイルを読みません）')
    parser.add_argument('--valid-for-hours', type=int, help='enable / renew の有効時間（必須、1〜2160時間）')
    parser.add_argument('--confirm-human', action='store_true', help='独立した診断同意を本人が確認する')
    parser.add_argument('--reason', help='同意記録の理由（秘密を含まない半角文字、1〜128文字）')
    args = parser.parse_args(argv)
    if args.command in ('enable', 'renew'):
        if (args.valid_for_hours is None or not 1 <= args.valid_for_hours <= 2160
                or not args.confirm_human or type(args.reason) is not str
                or not 1 <= len(args.reason) <= 128 or not args.reason.strip()
                or any(not 32 <= ord(c) <= 126 for c in args.reason)):
            parser.error('confirmation required')
    elif args.valid_for_hours is not None or args.confirm_human or args.reason is not None:
        parser.error('confirmation is only for enable and renew')
    if args.state_root is not None and not args.state_root.strip():
        parser.error('root required')
    return args


def _root(explicit):
    if explicit is not None:
        return explicit
    cfg = mcs_util.load_config()
    if type(cfg) is not dict:
        raise ValueError('SDC_DIAGNOSTIC_ROOT_REQUIRED')
    sdc = cfg.get('sdc', {})
    if type(sdc) is not dict:
        raise ValueError('SDC_DIAGNOSTIC_ROOT_REQUIRED')
    root = sdc.get('state_root')
    if root is None:
        return None
    if type(root) is not str or not root.strip():
        raise ValueError('SDC_DIAGNOSTIC_ROOT_REQUIRED')
    return root


def _hash(value):
    return hashlib.sha256(canonical(value, max_bytes=16384).encode('ascii')).hexdigest()


def _display_target(store, binding, now_ms):
    snapshot = provision.profile_snapshot(store, profile='support', now_ms=now_ms)
    bundle = snapshot['bundle']
    target = endpoint({k: bundle[k] for k in ('origin', 'audience', 'environment')})
    if (bundle['profile_id'] != 'support'
            or snapshot['bundle_hash'] != binding['support_bundle_hash']
            or snapshot['bundle_hash'] != _hash(bundle)
            or _hash(target) != binding['support_endpoint_hash']
            or snapshot['root_fingerprint'] != binding['root_fingerprint']
            or snapshot['monitor_endpoint_hash'] != binding['monitor_endpoint_hash']
            or any(target[k] != binding[k] for k in ('audience', 'environment'))):
        raise ValueError('SDC_DIAGNOSTIC_TARGET_CHANGED')
    return target


def _confirm(binding, target, expires_at):
    print('診断イベントだけのローカル同意です。通常の監視や遠隔支援（R4a）の同意とは別です。')
    print('中央の診断同意も別途必要です。この操作だけで中央の現在の許可を確認したことにはなりません。')
    print('対象component: ' + ', '.join(COMPONENTS))
    print('固定コード・段階・重大度と10分bucketごとの件数、採取不足（partial）だけを扱います。')
    print('既存healthの固定分類、standaloneの受動witness、既存Connectorの固定結果を利用します。'
          '更新（update）とHermesのRuntimeは未観測です。ほかのsourceも未観測ならpartialとして扱い、全項目の採取完了とはしません。')
    print('患者情報・秘密・原文・ログ・自由な例外文・URL・pathは採取しません。')
    print('本文の保持は最大7日、中央receiptは180日です。ローカル許可は最大90日です。')
    print('support送信先: ' + target['origin'] + ' / audience=' + target['audience'])
    print('対象: tenant=' + binding['tenant'] + ' / installation=' + binding['installation']
          + ' / device=' + binding['device_id'] + ' / environment=' + binding['environment'])
    print('root ID: ' + binding['root_id'] + ' / root fingerprint: ' + binding['root_fingerprint'])
    print('有効期限: ' + datetime.fromtimestamp(expires_at / 1000, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'))
    print('信頼しているConsoleの対象・送信先と独立に照合してください。')
    print('採取・送信は既存の監視tickでのみ進みます。新しいサービスの導入・有効化や、この場での診断は行いません。')
    print('1: この内容に同意する / 2: 中止する（既定）')
    try:
        return input('番号を入力してください [2]: ').strip() == '1'
    except (EOFError, KeyboardInterrupt):
        return False


def _show(value):
    # Only the state API's closed metadata projection reaches this output.
    print(json.dumps(value, ensure_ascii=True, sort_keys=True))


def main(argv=None):
    args = _arguments(argv)
    local_only = args.command in ('status', 'disable')
    try:
        root = _root(args.state_root)
        if root is None:
            if local_only:
                print('診断のローカル同意は未設定です。')
                return 0
            raise ValueError('SDC_DIAGNOSTIC_ROOT_REQUIRED')
        try:
            store = Store(root)
        except FileNotFoundError:
            if local_only:
                print('診断のローカル同意は未設定です。')
                return 0
            raise
        with store:
            if args.command == 'status':
                _show(local.status(store, now_ms=int(time.time() * 1000)))
            elif args.command == 'disable':
                before = local.status(store, now_ms=int(time.time() * 1000))
                if before['local_receipt_id'] is None:
                    print('診断のローカル同意は未設定です。')
                    return 0
                _show(local.disable(store, now_ms=int(time.time() * 1000)))
                print('診断の新規採取・送信をローカルで停止しました。開始済み処理の終了は未確認です。通常の状態監視（R1）は継続します。')
            else:
                started = int(time.time() * 1000)
                expires_at = started + args.valid_for_hours * 3600000
                before = local.validate_binding(diagnostic_sender.managed_binding(store, now_ms=started))
                target = _display_target(store, before, started)
                if not _confirm(before, target, expires_at):
                    print('中止しました。ローカル同意は変更していません。')
                    return 0
                # Human input never owns either lock. The state API owns its local
                # lock after this tick lock; metadata alone never sends HTTP.
                with store.lock():
                    fresh = diagnostic_sender.managed_binding(store, now_ms=int(time.time() * 1000))
                    now_ms = int(time.time() * 1000)
                    if fresh != before or not started <= now_ms < expires_at:
                        raise ValueError('SDC_DIAGNOSTIC_TARGET_CHANGED')
                    action = local.enable if args.command == 'enable' else local.renew
                    result = action(store, binding=fresh, now_ms=now_ms, expires_at=expires_at,
                                    confirm_human=args.confirm_human, reason=args.reason)
                _show(result)
                print('ローカル同意を保存しました。中央の別同意・現在性の照合後、既存の監視tickで進みます。')
        return 0
    except (Exception, KeyboardInterrupt):
        print('診断の操作を完了できませんでした。保存状態を mcs diagnostics status で確認してください。'
              '停止完了や新しい許可の成立は確認していません。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
