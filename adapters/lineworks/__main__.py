"""Configure, check, serve and send through the independent LINE WORKS adapter."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import signal
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "adapters.lineworks"

from adapters.common import paths, registry
from mcs_util import HOME
from .actions import Actions
from .client import ClientError, Credentials, _sign, _text, valid_filename
from .config import credentials_path, destination, load_credentials, settings
from .delivery import (SENDER_BUSY_TRIES, SENDER_BUSY_WAIT, DeliveryWorker, Sender, api_lock,
                       failure, notify_dirs)
from .server import CallbackInbox, callback_result, callback_server


def _log(event, **fields):
    # Callers pass only fixed event/error codes; no content, user IDs or secrets.
    logging.getLogger("mcs.lineworks").info("%s %s", event, json.dumps(fields))


def initialize(root):
    scope = settings(root)
    path = credentials_path(root)
    if path.exists():
        print("既存の認証設定を保持します。checkで確認してください。")
        return 0
    if not sys.stdin.isatty():
        raise ClientError("credential_entry_requires_terminal")
    values = {"bot_id": scope["application_id"]}
    for key, label in (("client_id", "Client ID"), ("client_secret", "Client Secret"),
                       ("service_account", "Service Account"), ("bot_secret", "Bot Secret")):
        values[key] = getpass.getpass(label + ": ").strip()
    key = Path(input("ダウンロードした秘密鍵の絶対パス: ").strip()).expanduser().resolve(strict=True)
    values["private_key_path"] = str(key)
    if not key.is_file() or key.stat().st_mode & 0o077 or key.stat().st_size > 16384:
        raise ClientError("private_key_permissions")
    # Validate local signing before persisting; no token/API request is made.
    _sign(b"mcs-local-key-check", str(key), 10)
    Credentials(**{k: v for k, v in values.items() if k != "bot_secret"})
    if not _text(values["bot_secret"], 4096):
        raise ClientError("credentials_invalid")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".lineworks-init-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(values, ensure_ascii=False).encode())
            stream.flush()
            os.fsync(stream.fileno())
        # Exclusive publication: another init's existing file is never overwritten.
        os.link(temporary, path)
        paths.fsync_dir(str(path.parent))
    finally:
        os.unlink(temporary)
    print("LINE WORKS認証設定を保存しました。次にcheckを実行してください。")
    return 0


def check(root):
    scope = settings(root, require_interactive=False)
    client, _ = load_credentials(root, scope)
    _sign(b"mcs-local-key-check", client.credentials.private_key_path, 10)
    print("LINE WORKS: local configuration and RSA signing OK (no network request)")
    return 0


async def serve(root, port):
    scope = settings(root)
    client, bot_secret = load_credentials(root, scope)
    dirs = paths.ensure_dirs(scope["data_root"], notify_dirs(scope["data_root"]))
    reg = registry.Registry(dirs["state"], scope=scope)
    sender = Sender(client, scope, scope["data_root"])
    worker = DeliveryWorker(sender=sender, settings=scope, root=scope["data_root"],
                            reg=reg, worker_id=registry.new_worker_id(), log=_log)
    if not worker.acquire_scope_lock():
        raise ClientError("adapter_already_running")
    server = None
    thread = None
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)
    try:
        reg.reload()
        with reg.batch():
            await worker.reconcile()
        inbox = CallbackInbox(Path(dirs["state"]) / "callbacks", scope, bot_secret)
        actions = Actions(scope, dirs, reg, sender, _log)
        server = callback_server(inbox, port=port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _log("adapter_ready")
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
                await worker.tick()
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
        if server:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
        if thread:
            thread.join(timeout=5)
        worker.release_scope_lock()
        for sig in (signal.SIGTERM, signal.SIGINT):
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
                if not path.is_relative_to(Path(root).resolve() / "data" / "attachments"):
                    raise ValueError("attachment_path")
                blob = paths.read_verified_attachment(str(path), item)
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
    for name in ("init", "check", "run", "send", "service", "status"):
        sub = subs.add_parser(name)
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
            callbacks = Path(scope["data_root"]) / "lineworks_state" / "callbacks"
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
        print(getattr(exc, "error_code", "local_configuration_error"), file=sys.stderr)
        # EX_TEMPFAIL: provably nothing was accepted, so the collector may retry.
        return 75 if getattr(exc, "not_sent", False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
