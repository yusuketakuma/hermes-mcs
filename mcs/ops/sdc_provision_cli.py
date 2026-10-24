"""監視・支援の個別配布を本人確認付きで取り込み、登録後の受領を照合する。"""
import json
import os
from pathlib import Path
import stat
import sys
import time

from adapters.sdc import provision
from adapters.sdc.storage import Store
from adapters.sdc.transport import ProvisionTransport, TransportError


_EXPECTED = (
    ('console_origin', '認証済みConsoleのorigin（https://から、末尾/なし）'),
    ('bundle_hash', 'Consoleに表示されたbundle SHA-256（64文字）'),
    ('tenant', '対象のtenant ID'),
    ('installation', '対象のinstallation ID'),
    ('environment', '対象のenvironment'),
    ('enrollment_id', '対象のenrollment ID'),
    ('profile_id', '対象のprofile ID（monitor / support / restart）'),
    ('origin', '対象profileのorigin（https://から、末尾/なし）'),
    ('audience', '対象profileのaudience'),
)


class _Cancelled(Exception):
    pass


def add_arguments(parser):
    """Add exclusive provision actions to the existing connection entry."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--prepare-provision-root', action='store_true', help='新しい私有領域を準備し、Console発行用のroot IDを保存する')
    group.add_argument('--import-provision', type=Path, metavar='FILE', help='Consoleから受け取った公開bundleを取り込む')
    group.add_argument('--provision-ack', choices=('monitor', 'support', 'restart'), metavar='PROFILE', help='R1登録後に対象profileの配布受領を中央へ照合・送信する')
    group.add_argument('--provision-status', choices=('monitor', 'support', 'restart'), metavar='PROFILE', help='対象profileの配布状態を中央へ照会する')
    parser.add_argument('--confirm-human', action='store_true', help='この配布操作、または connect / rotate-key / disconnect の資格情報変更を本人が確認した')
    parser.add_argument('--reason', help='配布操作・資格情報変更の記録用理由（秘密を含まない半角文字、128文字以内）')


def requested(args):
    return bool(args.prepare_provision_root or args.import_provision or args.provision_ack or args.provision_status)


def _reason_ok(args):
    return (args.confirm_human and isinstance(args.reason, str) and 1 <= len(args.reason) <= 128
            and bool(args.reason.strip()) and all(32 <= ord(c) <= 126 for c in args.reason))


def validate_arguments(parser, args):
    if not requested(args):
        if (args.confirm_human or args.reason is not None) and (
                args.command not in ('connect', 'rotate-key', 'disconnect') or not _reason_ok(args)):
            parser.error('--confirm-human / --reason は配布操作か connect / rotate-key / disconnect の資格情報変更と一緒に、'
                         '秘密を含まない半角の理由で指定してください')
        return
    command = 'connect' if args.prepare_provision_root or args.import_provision else 'connection'
    if args.command != command or (args.customer and not args.import_provision) or args.state_root is None:
        parser.error('配布操作は対応する connect / connection と --state-root で指定し、--customer は import 後の登録だけに指定してください')
    if not args.provision_status and not _reason_ok(args):
        parser.error('保存・受領操作には --confirm-human と秘密を含まない半角の --reason が必要です')
    if args.provision_status and (args.confirm_human or args.reason is not None):
        parser.error('状態照会は新しい同意を作りません。--confirm-human / --reason は指定しないでください')


def _read_bundle(path):
    """Read one bounded public regular file without following path symlinks."""
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ValueError('SDC_PROVISION_FILE_INVALID')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        child = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            before = os.fstat(child)
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 16384:
                raise ValueError('SDC_PROVISION_FILE_INVALID')
            with os.fdopen(child, 'rb', closefd=False) as stream:
                raw = stream.read(16385)
            after = os.fstat(child)
            if len(raw) != before.st_size or len(raw) > 16384 or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError('SDC_PROVISION_FILE_INVALID')
            return raw
        finally:
            os.close(child)
    finally:
        os.close(fd)


def _confirmed(prompt):
    if input(prompt + ' 1: 進む / 2: 中止 [2]: ').strip() != '1':
        raise _Cancelled


def _expected():
    print('信頼しているConsoleを自分で開き、対象とhashを確認してください。'
          '次の値は受け取ったファイルから転記せず、Consoleの表示から入力します。秘密・登録コードは入力しません。')
    _confirmed('独立したConsole表示を確認できましたか？')
    expected = {}
    for name, label in _EXPECTED:
        value = input(label + '（空欄で中止）: ').strip()
        if not value:
            raise _Cancelled
        if len(value) > 512 or any(not 32 <= ord(c) <= 126 for c in value):
            raise ValueError('SDC_PROVISION_EXPECTED_INVALID')
        expected[name] = value
    return expected


def _show(value):
    # Core APIs return only their closed, non-secret status/receipt projection.
    print(json.dumps(value, ensure_ascii=True, sort_keys=True))


def _initialize_profile(store, profile):
    from adapters.sdc import restart_client, support_client
    import sdc_support_signatures

    if profile == 'support':
        def initialize():
            support_client.initialize_local_state(store, check_pins=sdc_support_signatures.validate_pins)
    elif profile == 'restart':
        def initialize():
            restart_client.initialize_local_state(store)
    else:
        raise ValueError('SDC_PROVISION_UNSUPPORTED')
    provision.initialize_profile(store, profile=profile, now_ms=int(time.time() * 1000), initializer=initialize)


def _check_existing_profile(store, profile):
    from adapters.sdc import restart_client, support_client

    now_ms = int(time.time() * 1000)
    provision.profile_snapshot(store, profile=profile, now_ms=now_ms)
    client = support_client if profile == 'support' else restart_client
    client.status(store, now_ms=now_ms)


def run(args, *, connect):
    """Execute one explicit operation; registration and service activation stay separate."""
    try:
        if args.prepare_provision_root:
            print('新規導入用の私有領域とroot IDを保存します。既存・復元した領域は使えません。'
                  '過去の導入や復元履歴がないことは導入担当が別に確認してください。')
            _confirmed('新規導入として領域を準備しますか？')
            with Store(args.state_root, create_new=True) as store:
                _show(provision.prepare_root(store, confirm_human=args.confirm_human, reason=args.reason, now_ms=int(time.time() * 1000)))
            print('保存したroot IDを認証済みConsoleで指定し、monitor bundleを発行してください。監視送信はまだ始まりません。')
        elif args.import_provision:
            raw = _read_bundle(args.import_provision)
            expected = _expected()
            with Store(args.state_root) as store:
                plan = provision.prepare_import(store, raw, expected=expected, now_ms=int(time.time() * 1000))
                profile = plan['bundle']['profile_id']
                if args.customer and profile != 'monitor':
                    raise ValueError('SDC_PROVISION_UNSUPPORTED')
                previous = (provision.status(store, now_ms=int(time.time() * 1000), profile=profile)
                            if profile != 'monitor' else None)
                print('ファイルの正規形式・hash・保存先を照合しました。確認した対象:')
                _show(expected)
                _confirmed('この対象の' + profile + '配布を保存しますか？')
                receipt = provision.commit_import(store, plan, confirm_human=args.confirm_human, reason=args.reason, now_ms=int(time.time() * 1000))
                _show(receipt)
                if profile != 'monitor':
                    if previous['local_receipt_id'] is None:
                        _initialize_profile(store, profile)
                        print('対象profileの空の支援記録を準備しました。診断への同意やサービス操作はまだ開始しません。')
                    else:
                        _check_existing_profile(store, profile)
                        print('保存済みのprofileと支援記録を確認しました。停止状態や同意は作り直しません。')
                    print('配布の状態: mcs connection --state-root <同じ私有領域> --provision-status ' + profile)
                    print('配布の受領: mcs connection --state-root <同じ私有領域> --provision-ack ' + profile
                          + ' --confirm-human --reason <秘密を含まない半角の理由>')
                    print('次の操作: ' + ('mcs support connect（本人確認済みcase UUID）' if profile == 'support'
                                          else 'mcs support plan-restart（本人確認済みcase UUID）'))
                    return 0
                print('配布を保存しました。Consoleの対象と登録コードを確認してから、既存のR1登録へ進めます。'
                      'R1登録後には connection --provision-status monitor / --provision-ack monitor で配布との一致を確認します。')
                print('次の登録用途: ' + ('顧客（Keychain保存）' if args.customer else '自社検証。顧客として登録する場合は保留し、--customer を追加して同じ取り込みを再実行してください。'))
                if input('このまま同じ領域で状態監視の登録へ進みますか？ 1: 進む / 2: 保留 [2]: ').strip() == '1':
                    print('接続を完了すると、既存の監視ジョブがある場合はこの領域へ切り替わります。'
                          'この後に表示される監視内容へ別途同意するまで登録は完了しません。')
                    return connect(store)
                print('配布記録は保存済みで、監視登録は保留しました。再開は同じ --import-provision FILE と --state-root で、'
                      'Consoleの独立確認・--confirm-human・--reason を指定して再承認してください。')
        else:
            action = 'ack' if args.provision_ack else 'status'
            with Store(args.state_root) as store:
                if action == 'ack':
                    print('R1登録の成功だけでは配布対象の一致は確定しません。保存済みbundle・現在の端末・中央の配布状態を照合します。')
                    _confirmed('保存済み配布の受領を確認しますか？')
                result = provision.ack_once(store, profile=args.provision_ack or args.provision_status,
                                            transport_factory=ProvisionTransport, clock=time.time, action=action,
                                            confirm_human=args.confirm_human, reason=args.reason)
                _show(result)
                if not result.get('acknowledged'):
                    print('配布の受領はまだ確認できていません。結果不明を未送信とは扱いません。'
                          '同じ領域とprofileの --provision-status で確認し、対象やIDを作り直さないでください。')
                    return 1
        return 0
    except (_Cancelled, EOFError, KeyboardInterrupt):
        print('操作を中止しました。保存済みの記録は保持します。送信後の結果が不明な場合は同じ対象の状態を照会してください。')
        return 1
    except TimeoutError:
        profile = args.provision_ack or args.provision_status
        if profile in ('monitor', 'support', 'restart'):
            print('配布の受領・状態照会が時間内に完了せず、結果は確定していません。'
                  '保存済み記録を保持し、同じ私有領域とprofileで照合してください。対象やIDを作り直さないでください。\n'
                  '照合: mcs connection --state-root <同じ私有領域> --provision-status ' + profile, file=sys.stderr)
        else:
            print('配布操作が時間内に完了しませんでした。保存済み記録を保持し、'
                  'Consoleの対象・期限・私有領域を確認してください。秘密値を共有しないでください。', file=sys.stderr)
        return 1
    except BlockingIOError:
        print('送信処理が終了待ちです。同じ指定で再実行してください。保存済みの配布と同意を保持し、完了は再実行後の結果で確認してください。', file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError, TransportError):
        print('配布操作を完了できませんでした。保存済み記録を保持し、Consoleの対象・期限・私有領域を確認してください。秘密値を共有しないでください。', file=sys.stderr)
        return 1
