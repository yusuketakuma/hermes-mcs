"""Configure, check, serve and send through the independent LINE WORKS adapter."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import signal
import stat
import sys
import tempfile
import threading
import time
import warnings
from contextlib import ExitStack
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "adapters.lineworks"

from adapters.common import eventlog, paths, registry
from mcs_util import HOME
from safe_io import fdopen_owned, read_regular_nofollow
from .actions import Actions
from .client import ClientError, Credentials, _sign, valid_protocol_text, valid_filename
from .config import credentials_path, destination, load_credentials, settings
from .delivery import (SENDER_BUSY_TRIES, SENDER_BUSY_WAIT, DeliveryWorker, Sender, api_lock,
                       failure, notify_dirs)
from .server import CallbackInbox, callback_result, callback_server


# Callers pass only fixed event/error codes; no content, user IDs or secrets.
_log = eventlog.make_logger("mcs.lineworks")


def _worker_witness(data_root, reason):
    """Best-effort start evidence for doctor; a status write never stops delivery."""
    try:
        import card_worker_status
        # LINE WORKS has no Hermes profile; card_worker_status.diagnose reads 'default'.
        card_worker_status.record(data_root, "lineworks", "default", reason)
    except (ImportError, OSError, ValueError):
        _log("worker_status_failed")


def initialize(root):
    # Text-only LINE WORKS targets also send with these credentials (see check/send).
    scope = settings(root, require_interactive=False)
    path = credentials_path(root)
    if os.path.lexists(path):
        print("既存の認証設定を保持します。checkで確認してください。")
        return 0
    if not sys.stdin.isatty():
        raise ClientError("credential_entry_requires_terminal")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    def parent_identity():
        info = os.stat(path.parent, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ClientError('lineworks_credentials_parent_untrusted')
        return info.st_dev, info.st_ino, info.st_uid, info.st_mode
    parent = parent_identity()
    print("LINE WORKSの開発者画面で発行した認証情報を入力します。入力は表示されません。")
    values = {"bot_id": scope["application_id"]}
    for key, label in (("client_id", "アプリのClient ID"), ("client_secret", "アプリのClient Secret"),
                       ("service_account", "サービスアカウント（Service Account）"), ("bot_secret", "Botの認証用秘密値（Bot Secret）")):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                values[key] = getpass.getpass(label + ": ").strip()
        except getpass.GetPassWarning:
            raise ClientError("credential_input_not_hidden") from None
    key = Path(input("ダウンロードした秘密鍵の絶対パス: ").strip()).expanduser().resolve(strict=True)
    values["private_key_path"] = str(key)
    if not key.is_file() or key.stat().st_mode & 0o077 or key.stat().st_size > 16384:
        raise ClientError("private_key_permissions")
    # Validate local signing before persisting; no token/API request is made.
    _sign(b"mcs-local-key-check", str(key), 10)
    Credentials(**{k: v for k, v in values.items() if k != "bot_secret"})
    if not valid_protocol_text(values["bot_secret"], 4096):
        raise ClientError("credentials_invalid")
    body = json.dumps(values, ensure_ascii=False).encode()
    if len(body) > 16384:
        raise ClientError("credentials_invalid")
    if parent_identity() != parent or settings(root, require_interactive=False) != scope:
        raise ClientError('lineworks_configuration_changed')
    # Kept inline: unlike publish_new_bytes, a failed staging cleanup must fail init.
    fd, temporary = tempfile.mkstemp(prefix=".lineworks-init-", dir=path.parent)
    try:
        with fdopen_owned(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        if parent_identity() != parent or settings(root, require_interactive=False) != scope:
            raise ClientError('lineworks_configuration_changed')
        # Exclusive publication: another init's existing file is never overwritten.
        os.link(temporary, path)
        paths.fsync_dir(str(path.parent))
    finally:
        os.unlink(temporary)
    try:
        saved, info = read_regular_nofollow(path, 16384, lambda: ValueError('credentials_unverified'), with_stat=True)
        current = os.stat(path, follow_symlinks=False)
        if (parent_identity() != parent or settings(root, require_interactive=False) != scope
                or saved != body or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or (current.st_dev, current.st_ino, current.st_mode, current.st_size, current.st_mtime_ns, current.st_ctime_ns)
                != (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)):
            raise ValueError('credentials_unverified')
    except (OSError, ValueError, ClientError):
        raise OSError('credentials_unverified') from None
    print("LINE WORKS認証設定を保存しました。次にcheckを実行してください。")
    return 0


def credential_dialog(root):
    """Offer first registration while preserving existing credentials."""
    if not sys.stdin.isatty():
        raise ClientError("credential_entry_requires_terminal")
    existing = os.path.lexists(credentials_path(root))
    if existing:
        print("LINE WORKSの認証設定があります。")
        prompt = "1. 現在の設定を保持する（推奨）  2. 変更について確認する [1]: "
    else:
        print("LINE WORKSの認証情報を端末で登録できます。")
        prompt = "1. 今回は設定しない（推奨）  2. 新しく登録する [1]: "
    while True:
        try:
            answer = input(prompt).strip() or "1"
        except (EOFError, KeyboardInterrupt):
            print("\nLINE WORKSの認証設定は変更していません。")
            return 0
        if answer in ("0", "1"):
            print("LINE WORKSの認証設定は変更していません。")
            return 0
        if answer == "2":
            break
        print("1 または 2 を入力してください。")
    if existing:
        print("実行中の旧LINE WORKS接続が認証情報を使っていないことを確認できないため、"
              "変更しません。送信や接続を停止し、担当者に変更方法を相談してください。")
        return 1
    try:
        return initialize(root)
    except (OSError, KeyboardInterrupt):
        print("LINE WORKSの認証情報の登録結果を確認できません。秘密値を表示せず、"
              "担当者に保存状態の確認を依頼してください。")
        return 1


def check(root):
    scope = settings(root, require_interactive=False)
    client, _ = load_credentials(root, scope)
    _sign(b"mcs-local-key-check", client.credentials.private_key_path, 10)
    print("LINE WORKS: 設定と秘密鍵による署名の確認が完了しました（外部通信はしていません）")
    return 0


async def serve(root, port):
    try:
        scope = settings(root)
        client, bot_secret = load_credentials(root, scope)
    except (ClientError, OSError, ValueError):
        _worker_witness(str(Path(root).expanduser().resolve() / "data"), "worker_start_failed")
        raise
    dirs = paths.ensure_dirs(scope["data_root"], notify_dirs(scope["data_root"]))
    reg = registry.Registry(dirs["state"], scope=scope)
    sender = Sender(client, scope, scope["data_root"])
    worker = DeliveryWorker(sender=sender, settings=scope, root=scope["data_root"],
                            reg=reg, worker_id=registry.new_worker_id(), log=_log)
    if not worker.acquire_scope_lock():
        raise ClientError("adapter_already_running")
    server = None
    thread = None
    server_started = False
    delivery_task = None
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered_signals = []
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stopping.set)
            registered_signals.append(sig)
        reg.reload()
        with reg.batch():
            await worker.reconcile()
        inbox = CallbackInbox(Path(dirs["state"]) / "callbacks", scope, bot_secret)
        actions = Actions(scope, dirs, reg, sender, _log)
        server = callback_server(inbox, port=port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        server_started = True
        _log("adapter_ready")
        _worker_witness(scope["data_root"], "worker_started")
        while not stopping.is_set():
            try:
                # Reconfiguration must never leave an old authorized sender alive.
                try:
                    current = settings(root)
                except ClientError:
                    _log("configuration_invalid_adapter_stopped")
                    break
                if current != scope:
                    _log("configuration_changed_restart_required")
                    break
                # One event loop owns the registry; no concurrent reload or second delivery tick.
                try:
                    inbox.expire()
                    for pending in inbox.pending():
                        working, event = inbox.take(pending)
                        try:
                            await actions.handle(event)
                        except Exception as exc:
                            inbox.finish(working, "unknown")
                            _log("callback_processing_unknown", error=type(exc).__name__)
                        else:
                            inbox.finish(working, "processed")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _log("callback_processing_failed", error=type(exc).__name__)
                if delivery_task is not None and delivery_task.done():
                    finished, delivery_task = delivery_task, None
                    finished.result()
                if delivery_task is None:
                    delivery_task = asyncio.create_task(worker.tick())
                    await asyncio.sleep(0)
                await actions.sweep_followups()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log("tick_failed", error=type(exc).__name__)
            try:
                await asyncio.wait_for(stopping.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass
    finally:
        worker.stop()
        try:
            if delivery_task is not None:
                try:
                    await delivery_task
                except Exception as exc:
                    _log("tick_failed", error=type(exc).__name__)
        finally:
            try:
                if server:
                    try:
                        if server_started:
                            await asyncio.to_thread(server.shutdown)
                    finally:
                        server.server_close()
            finally:
                try:
                    if thread and server_started:
                        thread.join(timeout=5)
                finally:
                    try:
                        worker.release_scope_lock()
                    finally:
                        for sig in registered_signals:
                            loop.remove_signal_handler(sig)


def _wait_api_lock(root):
    """Enter api_lock, waiting out a brief adapter hold as delivery.locked does."""
    for attempt in range(SENDER_BUSY_TRIES):
        stack = ExitStack()
        try:
            stack.enter_context(api_lock(root))
            return stack
        except ClientError as exc:
            if exc.error_code != "sender_busy" or attempt == SENDER_BUSY_TRIES - 1:
                raise
        time.sleep(SENDER_BUSY_WAIT)


def send(root, target):
    """Send one text (+ sealed files); failures that provably sent nothing carry not_sent."""
    stage = "local"
    try:
        scope = settings(root, require_interactive=False)
        destination(target, scope)
        client, _ = load_credentials(root, scope)
        raw = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ClientError("validation_invalid")
        try:
            payload = json.loads(raw.decode("utf-8"))
            content = payload["text"]
            files = payload.get("files", [])
            if not isinstance(content, str) or not 0 < len(content) <= 2000 \
                    or not isinstance(files, list) or len(files) > 10:
                raise ValueError("payload")
            blobs = []
            for item in files:
                if not isinstance(item, dict) or not valid_filename(item.get("name")):
                    raise ValueError("attachment_name")
                path = Path(item["path"]).resolve(strict=True)
                data_root = Path(root).resolve() / "data"
                if not path.is_relative_to(data_root / "attachments"):
                    raise ValueError("attachment_path")
                blob = paths.read_verified_attachment(str(path), item, root=str(data_root))
                if blob is None or len(blob) > client.max_upload_bytes:
                    raise ValueError("attachment_mismatch")
                blobs.append((blob, item["name"]))
        except (ValueError, KeyError, TypeError, UnicodeError, OSError, RecursionError):
            raise ClientError("validation_invalid") from None
        # The collector journals its in-flight marker before invoking this single attempt.
        with _wait_api_lock(scope["data_root"]):
            stage = "first"
            client.send_message({"type": "text", "text": content}, channel_id=scope["channel_id"])
            stage = "accepted"
            for blob, name in blobs:
                fid = client.upload_file(blob, name)
                client.send_message({"type": "file", "fileId": fid}, channel_id=scope["channel_id"])
        return 0
    except (ClientError, OSError, ValueError) as exc:
        # Before any wire call, or a definitive reject of the first post, nothing
        # was accepted; once the text is accepted the outcome stays unknown.
        if stage == "local" or (stage == "first" and failure(exc)["result"] == "not_sent"):
            exc.not_sent = True
        raise


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="独立LINE WORKS Botアダプター")
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "credentials", "check", "run", "send", "service", "status"):
        descriptions = {"init": "LINE WORKSの認証情報を設定します", "check": "設定と秘密鍵を確認します",
                        "credentials": "LINE WORKSの認証設定を対話で確認します",
                        "run": "LINE WORKSへの接続を開始します", "send": "指定先へ通知を送ります",
                        "service": "常駐サービスの設定を生成します",
                        "status": "機械処理用のJSONで処理待ち件数を表示します"}
        sub = subs.add_parser(name, help=descriptions[name], description=descriptions[name])
        sub.add_argument("--root", default=HOME, help="設定とdataの保存先（既定: ~/.mcs）")
        if name == "run":
            sub.add_argument("--port", type=int, default=8788)
        if name == "send":
            sub.add_argument("--to", required=True)
            sub.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            return initialize(args.root)
        if args.command == "credentials":
            return credential_dialog(args.root)
        if args.command == "check":
            return check(args.root)
        if args.command == "send":
            return send(args.root, args.to)
        if args.command == "service":
            from .service import generate
            settings(args.root)
            print(generate(args.root))
            return 0
        if args.command == "status":
            scope = settings(args.root)
            callbacks = Path(notify_dirs(scope["data_root"])["state"]) / "callbacks"
            unknown = sum(1 for _ in callbacks.glob("*.working"))
            unknown += sum(callback_result(p) == "unknown"
                           for p in callbacks.glob("*.done"))
            print(json.dumps({"pending_callbacks": sum(1 for _ in callbacks.glob("*.json")),
                              "unknown_callbacks": unknown}))
            return 0
        if not 1 <= args.port <= 65535:
            raise ClientError("callback_port_invalid")
        asyncio.run(serve(args.root, args.port))
        return 0
    except (ClientError, OSError, ValueError) as exc:
        # Never echo native exceptions: they can embed local paths or secret values.
        if args.command in ("init", "credentials", "check"):
            print("LINE WORKSの設定を確認できません。認証情報と秘密鍵の場所・アクセス権を確認してください。")
        print(getattr(exc, "error_code", "local_configuration_error"), file=sys.stderr)
        # EX_TEMPFAIL: provably nothing was accepted, so the collector may retry.
        return 75 if getattr(exc, "not_sent", False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
