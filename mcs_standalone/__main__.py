"""Standalone (Hermes-free) Slack/Discord runtime: run, send, check.

Exit codes for ``send`` follow the `hermes send` contract notify_flush
relies on: 0 delivered, 2 refused before any network use (config,
target, payload), 75 provably not accepted (safe to retry), anything
else unknown (never retried automatically).
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import re
import sys
import tempfile
import warnings
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "mcs_standalone"

import mcs_standalone  # noqa: E402,F401  (path bootstrap)
from adapters.common.paths import read_verified_attachment  # noqa: E402
from mcs_util import HOME  # noqa: E402
from safe_io import fdopen_owned  # noqa: E402

from . import config  # noqa: E402
from .credential_gate import CredentialGateUnavailable  # noqa: E402
from .host import NotSent, Refused, log  # noqa: E402

EX_TEMPFAIL = 75
_SETUP_INCOMPLETE = {"credentials_missing", "env_file_permissions",
                     "standalone_credentials_missing", "standalone_credentials_invalid",
                     "standalone_configuration_invalid"}
_MAX_FILES = 10
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_REQUIREMENTS = Path(__file__).resolve().parents[1] / "deployment" / "requirements-standalone.txt"


def _runtime(transport: str):
    if transport == "discord":
        from adapters.discord import standalone
    else:
        from adapters.slack import standalone
    return standalone


async def _serve(root: str, cfg: dict, transport: str, *, restart_provision=None) -> int:
    from .runtime import serve
    return await serve(root, cfg, _runtime(transport).run if transport else None,
                       restart_provision=restart_provision)


def run(root: str) -> int:
    try:
        cfg = config.load(root)
    except config.ConfigError as exc:
        if str(exc) == "runtime_mode_not_standalone":
            # Switched back to Hermes: exit without serving. The service
            # keeps restarting this (launchd KeepAlive / systemd
            # Restart=always, every 30 s) until `mcs_setup.py services`
            # retires it; each start only logs this code.
            log("runtime_mode_not_standalone")
            return 0
        raise
    transport = config.interactive(cfg)
    if check(root):
        return 1
    return asyncio.run(_serve(root, cfg, transport))


def _payload(root: str) -> dict:
    """notify_flush's sealed JSON: text plus pinned attachment metadata."""
    raw = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
    try:
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("too_large")
        payload = json.loads(raw.decode("utf-8"))
        text, files = payload["text"], payload.get("files", [])
        if not isinstance(text, str) or not 0 < len(text) <= 2000 \
                or not isinstance(files, list) or len(files) > _MAX_FILES:
            raise ValueError("payload")
        data_root = str(Path(root).expanduser().resolve() / "data")
        for item in files:
            name = item["name"]
            if not isinstance(name, str) or not 0 < len(name) <= 255 \
                    or any(c in name for c in "/\\\x00\r\n") or name in (".", ".."):
                raise ValueError("attachment_name")
            path = Path(item["path"]).resolve(strict=True)
            # confined to <data>/attachments, as the Slack/Discord senders are
            blob = read_verified_attachment(str(path), item, root=data_root)
            if blob is None:
                raise ValueError("attachment_mismatch")
        return payload
    except (ValueError, KeyError, TypeError, UnicodeError, OSError, RecursionError):
        raise config.ConfigError("payload_invalid") from None


def send(root: str, target: str) -> int:
    cfg = config.load(root)
    transport, _ = config.channel(cfg, target)
    # The Slack connector loads both tokens even to send (load_credentials).
    config.tokens(root, transport, socket=transport == "slack")
    payload = _payload(root)
    # SDK problems surface here, before any network use: safe to retry.
    # After this point an ImportError is as unknown as any other failure.
    problem = _sdk_problem()
    if problem:
        raise NotSent(problem)
    try:
        result = asyncio.run(_runtime(transport).send(root, target, payload))
    except CredentialGateUnavailable:
        raise NotSent("credential_gate_unavailable") from None
    except config.ConfigError:
        raise   # setup codes keep their exit status (75 when incomplete)
    except ValueError:
        raise Refused("configuration_invalid") from None
    if result.get("result") == "delivered":
        return 0
    if result.get("result") == "not_sent":
        # The connector's fixed code is the last stderr word: notify_flush
        # tells a transient outage from a permanent refusal by it.
        code = result.get("error_code")
        raise NotSent(code if isinstance(code, str) and _CODE.fullmatch(code)
                      else "delivery_not_accepted")
    return 1


def _coded(call, *args, errors=(ValueError,), **kwargs):
    """Surface the fixed, secret-free code of a known raise site.

    Only wraps call sites whose exceptions carry fixed codes; arbitrary
    native exceptions still print just their type name in main().
    """
    try:
        return call(*args, **kwargs)
    except errors as exc:
        raise config.ConfigError(str(exc)) from None


def _existing_credentials_choice(transport):
    print(f"{transport}: 認証情報の操作を選んでください。")
    while True:
        try:
            choice = input("1: 保持（推奨） / 2: 変更を希望 / 0: 中止 [1]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n認証情報は変更せず中止しました。")
            return False
        if choice in ("", "1"):
            return True
        if choice == "2":
            print("認証情報の変更は保留中です。古い接続や送信処理の停止を確認できる仕組みが必要です。"
                  "現在の認証情報は変更していません。")
            return False
        if choice == "0":
            print("認証情報は変更せず中止しました。")
            return False
        print("1、2、0のいずれかを入力してください。")


def _credential_env_snapshot(root):
    from safe_io import read_private_bytes
    try:
        return read_private_bytes(Path(root).expanduser().resolve() / ".env", 65536)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise config.ConfigError("credential_existing_state_unverifiable") from None


def _hidden_credential(prompt):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt)
    except getpass.GetPassWarning:
        raise config.ConfigError("credential_hidden_input_unavailable") from None


def _env_has_transport_key(raw, transport):
    if raw is None:
        return False
    try:
        keys = set(config.TOKENS[transport])
        if transport == "slack":
            keys.add(config.SOCKET_TOKEN)
        return any(sep and key in keys for key, sep, _ in
                   (line.partition("=") for line in raw.decode("utf-8").splitlines()))
    except (UnicodeError, KeyError):
        raise config.ConfigError("credential_existing_state_unverifiable") from None


def initialize(root, transport, *, yes=False):
    _coded(config.connector_settings, root, transport, require_interactive=False)
    path = config.credentials_path(root, transport)
    if os.path.lexists(path):
        config.load_credentials(root, transport)
        if not yes and sys.stdin.isatty() and not _existing_credentials_choice(transport):
            return 1
        print("既存の認証設定を保持しました。")
        return 0
    env_snapshot = _credential_env_snapshot(root)
    try:
        value = config.load_credentials(root, transport)
    except ValueError as exc:
        if str(exc) != "credentials_missing" or _env_has_transport_key(env_snapshot, transport):
            raise config.ConfigError("credential_existing_state_unverifiable") from None
        if yes or not sys.stdin.isatty():
            raise config.ConfigError("credential_entry_requires_terminal") from None
        fields = ["bot_token", *(["app_token"] if transport == "slack" else [])]
        labels = {"bot_token": "Botトークン（Slackはxoxb-で始まる値）",
                  "app_token": "アプリトークン（xapp-で始まる値）"}
        print("接続先の管理画面で発行したトークンを入力してください。入力は表示されません。")
        value = {key: _hidden_credential(f"{transport}: {labels[key]}: ").strip() for key in fields}
        _coded(config.validate_credentials, value, transport)
    else:
        if not yes and sys.stdin.isatty() and not _existing_credentials_choice(transport):
            return 1
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Not safe_io.publish_new_bytes: that one suppresses a failed staging unlink, and a
    # leftover staging copy of the secret must not end in the success message below.
    fd, temporary = tempfile.mkstemp(prefix=".standalone-init-", dir=path.parent)
    try:
        with fdopen_owned(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        if _credential_env_snapshot(root) != env_snapshot:
            raise config.ConfigError("credential_existing_state_changed")
        os.link(temporary, path)
        from adapters.common.paths import fsync_dir
        fsync_dir(str(path.parent))
    finally:
        os.unlink(temporary)
    print("認証設定を保存しました（本人だけが読み書きできる権限）。")
    return 0


def _pinned() -> dict[str, str]:
    pins = {}
    for line in _REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ";" in line:
            # marker-gated pins (python_version conditionals) are not
            # installed on every interpreter — they cannot be verified
            continue
        name, sep, version = line.partition("==")
        if sep:
            pins[name.strip()] = version.strip()
    return pins


def _sdk_problem() -> str | None:
    """The installed SDKs must be exactly the pinned, verified versions."""
    from importlib.metadata import PackageNotFoundError, version
    for name, pinned in _pinned().items():
        try:
            installed = version(name)
        except PackageNotFoundError:
            installed = None
        if installed != pinned:
            return f"sdk {name}: {installed or 'missing'} (required {pinned})"
    return None


def check(root: str) -> int:
    """Local-only verification: config, tokens and pinned SDKs. No network."""
    cfg = config.load(root)
    from scheduled_jobs import configured_cron_jobs
    # Runtime() schedules these jobs; fail here with the code, not a crash-loop.
    _coded(configured_cron_jobs, cfg)
    used = {t for t in (config.interactive(cfg),) if t}
    for key in ("notify_target", "notify_system_target"):
        target = cfg.get(key)
        if isinstance(target, str) and target.split(":", 1)[0] in config.TOKENS:
            used.add(config.channel(cfg, target)[0])
    for transport in sorted(used):
        serving = config.interactive(cfg) == transport
        # send loads both Slack tokens too (load_credentials), not only run.
        config.tokens(root, transport, socket=serving or transport == "slack")
        if serving:
            # The connector builds its settings with connector_settings,
            # which is stricter than config.load: validate the same rules
            # here so check/run fail with the code instead of crash-looping.
            _coded(config.connector_settings, root, transport)
    if "lineworks" in _coded(config.transports, root):
        from adapters.lineworks.__main__ import check as lineworks_check
        from adapters.lineworks.client import ClientError
        _coded(lineworks_check, root, errors=(ClientError,))
    problem = _sdk_problem() if used else None
    if problem:
        print(problem, file=sys.stderr)
        return 1
    print("standalone: " + (", ".join(sorted(used)) or "Slack/Discordの接続設定なし")
          + " — 接続設定の確認が完了しました（外部通信はしていません）")
    return 0


def main(argv=None) -> int:
    # Slack's SDK adopts *_PROXY from the environment; MCS traffic (PHI)
    # never goes through a proxy (no-proxy safety gate).
    for key in [k for k in os.environ if k.lower() in (
            "http_proxy", "https_proxy", "all_proxy", "ws_proxy", "wss_proxy")]:
        os.environ.pop(key)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # SDK debug logs can carry request bodies; keep them at warnings.
    for name in ("discord", "slack_sdk", "slack_bolt", "aiohttp"):
        logging.getLogger(name).setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description="Hermes なしで Slack / Discord と接続する MCS ランタイム")
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "send", "check", "init", "status", "service"):
        descriptions = {"run": "MCSの常駐処理を開始します", "send": "指定先へ通知を送ります",
                        "check": "必要な設定を確認します", "init": "通知先の認証情報を設定します",
                        "status": "機械処理用のJSONで稼働状態を表示します",
                        "service": "常駐サービスの設定内容を表示します"}
        sub = subs.add_parser(name, help=descriptions[name], description=descriptions[name])
        sub.add_argument("--root", default=HOME, help="設定とdataの保存先（既定: ~/.mcs）")
        if name == "send":
            sub.add_argument("--to", required=True)
            sub.add_argument("--quiet", action="store_true")
        if name == "init":
            sub.add_argument("--transport", choices=("slack", "discord"), required=True, help="認証情報を登録する通知先")
            sub.add_argument("--yes", action="store_true", help="質問しません。認証情報が未登録なら停止します")
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return run(args.root)
        if args.command == "send":
            return send(args.root, args.to)
        if args.command == "init":
            return initialize(args.root, args.transport, yes=args.yes)
        if args.command in ("status", "service"):
            from . import service
            if args.command == "status":
                print(json.dumps(service.status(args.root)))
            else:
                print(service.render(args.root)[1])
            return 0
        return check(args.root)
    except config.ConfigError as exc:
        # Codes only: native exceptions can embed paths or secret values.
        if args.command == "init" and str(exc) in (
                "credential_existing_state_unverifiable", "credential_existing_state_changed"):
            print("既存の認証情報を確認できないか、入力中に変更されました。"
                  "新しい認証情報は保存していません。設定の管理担当者に確認してください。")
        elif args.command == "init" and str(exc) == "credential_hidden_input_unavailable":
            print("入力を非表示にできないため、認証情報は保存していません。端末から再実行してください。")
        elif args.command in ("init", "check"):
            print("接続設定を確認できません。通知先・操作権限・認証情報を確認してください。")
        print(str(exc), file=sys.stderr)
        if args.command != "send":
            return 1
        # standalone setup not finished yet (e.g. tokens arrive with the
        # later `init`): nothing was sent — retry later, do not hold
        return EX_TEMPFAIL if str(exc) in _SETUP_INCOMPLETE else 2
    except Refused as exc:
        print(f"refused {exc}", file=sys.stderr)
        return 2
    except NotSent as exc:
        print(f"not_sent {exc}", file=sys.stderr)
        return EX_TEMPFAIL
    except OSError:
        if args.command == "init":
            print("認証情報の保存結果を確認できません。現在の状態を管理担当者に確認してください。")
        print("failed OSError", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"failed {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
