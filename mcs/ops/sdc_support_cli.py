"""遠隔支援の内容確認・本人同意登録・ローカル停止を前景で行います。"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import sys
import time

import mcs_util
from cli_args import JapaneseArgumentParser
import sdc_support_signatures

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from adapters.sdc import provision, restart_client, support_client  # noqa: E402
from adapters.sdc.storage import Store  # noqa: E402
from adapters.sdc.transport import RestartTransport, SupportTransport  # noqa: E402

FOREGROUND_SECONDS = 300


class _Expired(BaseException):
    pass


class _ForegroundBudget:
    """Callable deadline check with an explicit shorten-only alarm capability."""
    def __init__(self, check, shorten, expires_at):
        self._check, self.shorten, self.expires_at = check, shorten, expires_at

    def __call__(self):
        self._check()


@contextmanager
def _deadline():
    """Bound prompts/I/O together without extending an earlier caller alarm."""
    started = time.monotonic()
    previous_handler = signal.getsignal(signal.SIGALRM)
    delay, interval = signal.getitimer(signal.ITIMER_REAL)
    if delay and not callable(previous_handler):
        raise ValueError('SDC_SUPPORT_DEADLINE_UNAVAILABLE')
    parent = started + delay if delay else None
    end = min(started + FOREGROUND_SECONDS, parent) if parent else started + FOREGROUND_SECONDS
    parent_fired = False

    def expire(signum, frame):
        nonlocal parent_fired
        if parent is not None and time.monotonic() >= parent:
            parent_fired = True
            previous_handler(signum, frame)
        raise _Expired()

    def remaining():
        if time.monotonic() >= end:
            expire(signal.SIGALRM, None)

    def shorten(expires_at_ms):
        nonlocal end
        if type(expires_at_ms) is not int or not 0 <= expires_at_ms <= 2**53 - 1:
            raise ValueError('SDC_SUPPORT_DEADLINE_UNAVAILABLE')
        end = min(end, time.monotonic() + max(0, expires_at_ms / 1000 - time.time()))
        remaining()
        signal.setitimer(signal.ITIMER_REAL, max(1e-6, end - time.monotonic()))

    installed = False
    try:
        signal.signal(signal.SIGALRM, expire)
        installed = True
        remaining()
        signal.setitimer(signal.ITIMER_REAL, max(1e-6, end - time.monotonic()))
        yield _ForegroundBudget(remaining, shorten, lambda: int((time.time() + max(0, end - time.monotonic())) * 1000))
    finally:
        if installed:
            try:
                signal.setitimer(signal.ITIMER_REAL, 0)
            finally:
                try:
                    signal.signal(signal.SIGALRM, previous_handler)
                finally:
                    if parent is not None:
                        rest = parent - time.monotonic()
                        if parent_fired:
                            if interval:
                                signal.setitimer(signal.ITIMER_REAL, max(1e-6, rest % interval), interval)
                        else:
                            signal.setitimer(signal.ITIMER_REAL, max(1e-6, rest), interval)


def _stamp(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def _confirm(preview, endpoint):
    purposes = {'HEALTH_CHECK': '稼働状況の確認', 'NOTIFICATION_FAILURE': '通知障害の確認', 'PROCESS_STALL': '処理停止の確認', 'CONFIG_MISMATCH': '設定不整合の確認', 'UPDATE_FAILURE': '更新失敗の確認'}
    diagnostics = {'runtime.status.v1': '実行環境の状態', 'connectivity.status.v1': '接続状況',
                   'notification.health.v1': '通知の健全性'}
    print('遠隔支援の同意内容（この画面だけでは中央の対象確認を代替しません）')
    print('目的: ' + purposes.get(preview['purpose_code'], '承認された支援内容の確認'))
    print('診断項目: ' + ', '.join(diagnostics.get(name, name) for name in preview['diagnostic_ids']))
    print('対象case: ' + preview['case_id'])
    print('対象scope: tenant=' + preview['tenant'] + ' / installation=' + preview['installation'] + ' / device=' + preview['device_id'] + ' / environment=' + preview['environment'])
    print('担当ref: ' + ', '.join(preview['operator_refs']))
    print('Console上のcase・対象scope・担当refと照合してください。一致しない場合は中止してください。')
    print('同意回答期限: ' + _stamp(preview['expires_at']) + ' / 支援同意期限: ' + _stamp(preview['consent_expires_at']))
    print('送信先: ' + endpoint['origin'])
    print('今回は同意登録だけを行います。診断の実行には、個別承認とmcs support runまたはwatchの本人起動が別途必要です。サービス再起動は行いません。')
    print('停止: mcs support stop（開始済みの要求は終了未確認となる場合があります）')
    print('1: 照合し、同意して登録する / 2: 中止する（既定）')
    try:
        return input('番号を入力してください [2]: ').strip() == '1'
    except (EOFError, KeyboardInterrupt):
        return False


def _confirm_restart(stage, value, endpoint):
    """Display separately scoped read, inspection and individual change choices."""
    print('送信先: ' + endpoint['origin'])
    if stage == 'read':
        scope = value['expected']
        print('再起動準備: 登録metadataを取得し、稼働中Runtimeの世代・開始時刻・受付や更新の状態を採取します。')
        print('同じ本文の再取得も別の読取りです。不明時は初回を含め最大4回、中央の成功枠は端末ごと1日60回です。')
        print('次に採取内容を表示し、inspection送信への同意を確認します。')
        print('準備同意期限: ' + _stamp(value['expires_at']))
    elif stage == 'inspection':
        scope = value['preflight']
        print('再起動準備: 次のRuntime metadataをinspectionとして送信します。')
        print('登録revision: ' + str(value['registration']['service_revision']))
        print('Runtime世代: ' + scope['runtime_generation'])
        print('開始時刻: ' + _stamp(scope['started_at']) + ' / 観測時刻: ' + _stamp(scope['observed_at']))
        for key, label in (('owner_alive', 'owner生存'), ('update_in_progress', '更新中'),
                           ('command_intake_in_progress', 'コマンド受付処理中'), ('restarting', '再起動中')):
            print(label + ': ' + ('はい' if scope[key] else 'いいえ'))
        print('準備同意期限: ' + _stamp(value['expires_at']))
    else:
        scope = value
        print('再起動の個別変更同意: MCS standaloneのサービス再起動を許可します。')
        print('対象operation: ' + value['operation_id'])
        purposes = {'HEALTH_CHECK': '稼働状況の確認', 'NOTIFICATION_FAILURE': '通知障害への対応', 'PROCESS_STALL': '処理停止への対応',
                    'CONFIG_MISMATCH': '設定不整合への対応', 'UPDATE_FAILURE': '更新失敗への対応'}
        print('目的: ' + purposes[value['intent']['purpose_code']])
        print('提案者ref: ' + value['intent']['requested_by'] + ' / 個別承認者ref: ' + value['approval']['approved_by'])
        print('登録revision: ' + str(value['service_revision']) + ' / Runtime世代: ' + value['params']['expected_runtime_generation'])
        print('回答期限: ' + _stamp(value['expires_at']) + ' / 変更同意期限: ' + _stamp(min(value['intent']['expires_at'], value['approval']['expires_at'])))
        print('このコマンドは変更同意の登録までです。実行時の現在権限と同じ生存Runtimeの最終確認は別途必要です。')
    print('対象case: ' + scope['case_id'])
    print('対象scope: tenant=' + scope['tenant'] + ' / installation=' + scope['installation'] + ' / device=' + scope['device_id'] + ' / environment=' + scope['environment'])
    print('Consoleの案件・対象・内容と照合してください。停止: mcs support stop')
    print('1: この内容に同意して進む / 2: 中止する（既定）')
    try:
        return input('番号を入力してください [2]: ').strip() == '1'
    except (EOFError, KeyboardInterrupt):
        return False


class _SupportParser(JapaneseArgumentParser):
    def error(self, message):
        # Untrusted positional values/flags can contain secrets; discard parser detail.
        self.print_usage(sys.stderr)
        self.exit(2, '引数が正しくありません。mcs support --helpで確認してください。\n')


def main(argv=None, *, provider=None, restart_provision=None):
    ap = _SupportParser(prog='mcs support', description=__doc__)
    ap.add_argument('command', choices=('connect', 'status', 'stop', 'run', 'watch', 'plan-restart', 'consent-restart'))
    ap.add_argument('case_id', nargs='?', help='本人確認済みcase UUID（connect/plan-restart）またはoperation UUID（consent-restart）')
    ap.add_argument('--interval', type=int, choices=(30, 60), default=None, help='watchの待機中poll間隔（秒、既定60。完了時間の保証ではありません）')
    ap.add_argument('--state-root', help='承認済みのローカル監視領域')
    args = ap.parse_args(argv)
    if (args.command in ('connect', 'plan-restart', 'consent-restart')) != (args.case_id is not None):
        ap.error('connect/plan-restart/consent-restartにはUUIDを1つ指定します')
    if args.interval is not None and args.command != 'watch':
        ap.error('intervalはwatchでのみ指定できます')
    try:
        cfg = None
        if args.state_root:
            root = args.state_root
        else:
            cfg = mcs_util.load_config()
            sdc = cfg.get('sdc') if type(cfg) is dict else None
            if type(sdc) is not dict or type(sdc.get('state_root')) is not str or not sdc['state_root']:
                raise ValueError('SDC_SUPPORT_ROOT_REQUIRED')
            root = sdc['state_root']
        with Store(root) as store:
            if args.command in ('plan-restart', 'consent-restart') and restart_provision is None:
                def restart_provision():
                    return provision.profile_snapshot(store, profile='restart', now_ms=int(time.time() * 1000))
                restart_provision()
            if args.command == 'status':
                print('ローカル保存情報のみです。中央の現在の認可・接続・鍵の利用可否は確認していません。')
                print(json.dumps(support_client.status(store, now_ms=int(time.time() * 1000)), ensure_ascii=True))
                print(json.dumps(restart_client.status(store, now_ms=int(time.time() * 1000)), ensure_ascii=True))
            elif args.command == 'stop':
                result = support_client.stop(store, now_ms=int(time.time() * 1000))
                restart_client.stop_children(store)
                print('新規受付停止済み・前景処理の終了確認済み。' if result['foreground_ended'] else '新規受付停止済み。開始済み要求の終了は未確認です。')
                print('通常の状態監視は継続します。中央caseの閉鎖はConsoleで別途確認してください。')
            elif args.command in ('plan-restart', 'consent-restart'):
                with _deadline() as budget:
                    options = dict(restart_provision=restart_provision, check_pins=sdc_support_signatures.validate_pins,
                                   check_restart_pins=sdc_support_signatures.validate_restart_pins,
                                   transport_factory=RestartTransport, confirm=_confirm_restart,
                                   budget=budget, limit_deadline=budget.shorten)
                    if args.command == 'plan-restart':
                        result = restart_client.plan(store, args.case_id, **options)
                    else:
                        result = restart_client.consent(store, args.case_id, verifier=sdc_support_signatures.verify_restart_envelope, **options)
                messages = {'planned': 'inspectionの受付証拠を保存しました。Consoleの個別提案・承認後、consent-restartで変更内容を確認してください。',
                            'accepted': '再起動の個別変更同意ACKを保存しました。このコマンドで再起動は実行していません。',
                            'cancelled': '再起動準備または変更同意を中止しました。',
                            'inspection_unknown': 'inspectionの結果は未確認です。同じIDの保存記録を保持して保留しました。',
                            'preview_unknown': 'previewの結果は未確認です。新しいIDへ置き換えず保留しました。',
                            'ack_recorded_inactive': 'ACKは保存しましたが、現在の同意・権限は確認できません。新規要求は開始しません。'}
                print(messages[result['state']])
            elif args.command in ('run', 'watch'):
                if provider is None:
                    from sdc_support_diagnostics import collect_diagnostic
                    if cfg is None:
                        cfg = mcs_util.load_config()
                    selected_mode = cfg.get('runtime_mode') if type(cfg) is dict else None
                    selected_mode = selected_mode if type(selected_mode) is str and selected_mode in ('hermes', 'standalone') else None
                    def provider(diagnostic_id, *, generated_at_ms):
                        return collect_diagnostic(diagnostic_id, generated_at_ms=generated_at_ms, root=mcs_util.HOME, mode=selected_mode)
                with _deadline() as budget:
                    if args.command == 'watch':
                        print('同意の元期限・親の期限・全体300秒の最も早い期限まで、承認済み診断を照会します。')
                        print('待機中の照会間隔は' + str(args.interval or 60) + '秒です。診断完了時間の保証ではありません。停止: mcs support stop / Ctrl+C')
                        result = support_client.watch(store, verifier=sdc_support_signatures.verify_envelope,
                                                      check_pins=sdc_support_signatures.validate_pins, transport_factory=SupportTransport,
                                                      provider=provider, budget=budget, limit_deadline=budget.shorten,
                                                      interval=args.interval or 60)
                    else:
                        result = support_client.run_once(store, verifier=sdc_support_signatures.verify_envelope,
                                                         check_pins=sdc_support_signatures.validate_pins, transport_factory=SupportTransport,
                                                         provider=provider, budget=budget)
                messages = {'watch_finished': '前景の継続照会を終了しました。同意やgrantの期限は延長していません。',
                            'empty': '今回のpollで承認済み診断要求は観測されませんでした。',
                            'orphan_hold': '未解決のadmit記録があり保留しました。新しいIDで再実行しません。',
                            'pregrant_terminal': 'grant未要求のローカル証拠と中央の取消・期限切れを照合し、未開始の記録を終端化しました。診断は実行していません。',
                            'grant_hold': '実行grantを確認できず保留しました。診断は開始していません。',
                            'result_unknown': '結果は未確認です。保存済み記録を保持し、診断を再実行しません。',
                            'report_reconciled': '同じreport本文hashの受付証拠を照合しました。診断の再実行はしていません。',
                            'report_accepted': '診断コマンドのreport受付ACKを保存しました。機器の健全性を示す表示ではありません。'}
                if result['state'] == 'watch_finished':
                    print('期限に達したため終了しました。' if result['reason'] == 'deadline' else '停止または同意owner変更のため終了しました。')
                print(messages[result['state']])
            else:
                with _deadline() as budget:
                    result = support_client.connect(store, args.case_id, verifier=sdc_support_signatures.verify_envelope,
                                                    check_pins=sdc_support_signatures.validate_pins, transport_factory=SupportTransport,
                                                    confirm=_confirm, budget=budget)
                messages = {'cancelled': '同意登録を中止しました。', 'accepted': '同意登録のACKを保存しました。診断はmcs support runまたはwatchを本人が起動した場合だけ照合します。',
                            'ack_recorded_inactive': 'ACKは保存しましたが、同意は停止済みまたは期限切れです。新規要求は開始しません。'}
                print(messages[result['state']])
        return 0
    except (EOFError, KeyboardInterrupt):
        print('中止しました。送信開始済みの場合、結果は未確認です。mcs support statusでローカル記録を確認してください。')
        return 1
    except _Expired:
        print('SDC_SUPPORT_DEADLINE: 制限時間に達しました。送信開始済みの結果は未確認の場合があります。statusで記録を確認してください。', file=sys.stderr)
        return 1
    except BlockingIOError:
        message = '停止未完了です。支援stateが使用中のため、少し待って再実行してください。' if args.command == 'stop' else '支援または監視処理中です。少し待って再実行してください。'
        print('SDC_SUPPORT_BUSY: ' + message, file=sys.stderr)
        return 1
    except Exception:
        # Never expose arbitrary exception text, remote response, secrets or local paths.
        message = '停止未完了です。保存状態を確認し、再実行してください。' if args.command == 'stop' else '承認済み支援設定と初期provision、監視接続、期限を確認してください。欠落・破損は自動修復しません。送信開始済みの結果はstatusで確認してください。'
        print('SDC_SUPPORT_UNAVAILABLE: ' + message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
