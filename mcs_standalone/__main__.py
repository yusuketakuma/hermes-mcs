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
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "mcs_standalone"

import mcs_standalone  # noqa: E402,F401  (path bootstrap)
from adapters.common.paths import read_verified_attachment  # noqa: E402
from mcs_util import HOME  # noqa: E402

from . import config  # noqa: E402
from .host import NotSent, Refused, log  # noqa: E402

EX_TEMPFAIL = 75
_SETUP_INCOMPLETE = {"credentials_missing", "env_file_permissions",
                     "standalone_credentials_missing", "standalone_credentials_invalid",
                     "standalone_configuration_invalid"}
_MAX_FILES = 10
_REQUIREMENTS = Path(__file__).resolve().parents[1] / "deployment" / "requirements-standalone.txt"


def _runtime(transport: str):
    if transport == "discord":
        from adapters.discord import standalone
    else:
        from adapters.slack import standalone
    return standalone


async def _serve(root: str, cfg: dict, transport: str) -> int:
    from .runtime import serve
    return await serve(root, cfg, _runtime(transport).run if transport else None)


def run(root: str) -> int:
    try:
        cfg = config.load(root)
    except config.ConfigError as exc:
        if str(exc) == "runtime_mode_not_standalone":
            log("runtime_mode_not_standalone")   # switched back: stay down
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
        allowed = Path(root).expanduser().resolve() / "data" / "attachments"
        for item in files:
            name = item["name"]
            if not isinstance(name, str) or not 0 < len(name) <= 255 \
                    or any(c in name for c in "/\\\x00\r\n") or name in (".", ".."):
                raise ValueError("attachment_name")
            path = Path(item["path"]).resolve(strict=True)
            if not path.is_relative_to(allowed):
                raise ValueError("attachment_path")
            blob = read_verified_attachment(str(path), item)
            if blob is None:
                raise ValueError("attachment_mismatch")
        return payload
    except (ValueError, KeyError, TypeError, UnicodeError, OSError, RecursionError):
        raise config.ConfigError("payload_invalid") from None


def send(root: str, target: str) -> int:
    cfg = config.load(root)
    transport, _ = config.channel(cfg, target)
    config.tokens(root, transport)
    payload = _payload(root)
    # SDK problems surface here, before any network use: safe to retry.
    # After this point an ImportError is as unknown as any other failure.
    problem = _sdk_problem()
    if problem:
        raise NotSent(problem)
    try:
        result = asyncio.run(_runtime(transport).send(root, target, payload))
    except ValueError:
        raise Refused("configuration_invalid") from None
    if result.get("result") == "delivered":
        return 0
    if result.get("result") == "not_sent":
        raise NotSent("delivery_not_accepted")
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


def initialize(root, transport, *, yes=False):
    _coded(config.connector_settings, root, transport, require_interactive=False)
    path = config.credentials_path(root, transport)
    if os.path.lexists(path):
        config.load_credentials(root, transport)
        print("既存の認証設定を保持しました。")
        return 0
    try:
        value = config.load_credentials(root, transport)
    except ValueError:
        if yes or not sys.stdin.isatty():
            raise config.ConfigError("credential_entry_requires_terminal") from None
        fields = ["bot_token", *(["app_token"] if transport == "slack" else [])]
        value = {key: getpass.getpass(transport + " " + key + ": ").strip() for key in fields}
        _coded(config.validate_credentials, value, transport)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".standalone-init-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        from adapters.common.paths import fsync_dir
        fsync_dir(str(path.parent))
    finally:
        os.unlink(temporary)
    print("認証設定を0600で保存しました。")
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
    used = {t for t in (config.interactive(cfg),) if t}
    for key in ("notify_target", "notify_system_target"):
        target = cfg.get(key)
        if isinstance(target, str) and target.split(":", 1)[0] in config.TOKENS:
            used.add(config.channel(cfg, target)[0])
    for transport in sorted(used):
        serving = config.interactive(cfg) == transport
        config.tokens(root, transport, socket=serving)
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
    print("standalone: " + (", ".join(sorted(used)) or "no Slack/Discord use")
          + " — local configuration OK (no network request)")
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
        sub = subs.add_parser(name)
        sub.add_argument("--root", default=HOME, help="設定とdataの保存先（既定: ~/.mcs）")
        if name == "send":
            sub.add_argument("--to", required=True)
            sub.add_argument("--quiet", action="store_true")
        if name == "init":
            sub.add_argument("--transport", choices=("slack", "discord"), required=True)
            sub.add_argument("--yes", action="store_true")
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
    except Exception as exc:
        print(f"failed {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
