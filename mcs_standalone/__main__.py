"""Standalone (Hermes-free) Slack/Discord runtime: run, send, check.

Exit codes for ``send`` follow the `hermes send` contract notify_flush
relies on: 0 delivered, 2 refused before any network use (config,
target, payload), 75 provably not accepted (safe to retry), anything
else unknown (never retried automatically).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
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
                     "standalone_configuration_invalid"}
_MAX_FILES = 10
_REQUIREMENTS = Path(__file__).resolve().parents[1] / "deployment" / "requirements-standalone.txt"


def _runtime(transport: str):
    if transport == "discord":
        from . import discord_runtime
        return discord_runtime
    from . import slack_runtime
    return slack_runtime


async def _serve(root: str, cfg: dict, transport: str) -> int:
    settings = config.settings(cfg, root, transport)
    tokens = config.tokens(root, transport, socket=True)
    stop = asyncio.Event()
    changed = []
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async def watch_config():
        # A changed scope or grant must never keep serving under the old
        # one: stop, and let launchd start a fresh process on the new config.
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
            try:
                current = config.load(root)
                same = (config.interactive(current) == transport
                        and config.settings(current, root, transport) == settings
                        and config.tokens(root, transport, socket=True) == tokens)
            except (OSError, ValueError, KeyError, TypeError):
                same = False
            if not same:
                log("configuration_changed_restart")
                changed.append(True)
                stop.set()

    watcher = asyncio.create_task(watch_config())
    try:
        await _runtime(transport).run(settings, tokens, stop)
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
    return 3 if changed else 0


def run(root: str) -> int:
    try:
        cfg = config.load(root)
    except config.ConfigError as exc:
        if str(exc) == "runtime_mode_not_standalone":
            log("runtime_mode_not_standalone")   # switched back: stay down
            return 0
        raise
    transport = config.interactive(cfg)
    if transport is None:
        log("no_interactive_transport")
        return 0                      # launchd restarts only failures
    problem = _sdk_problem()
    if problem:
        raise config.ConfigError(problem)
    # non-zero after a config change so launchd starts the new config
    return asyncio.run(_serve(root, cfg, transport))


def _payload(root: str) -> tuple[str, list[tuple[bytes, str]]]:
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
        blobs = []
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
            blobs.append((blob, name))
        return text, blobs
    except (ValueError, KeyError, TypeError, UnicodeError, OSError, RecursionError):
        raise config.ConfigError("payload_invalid") from None


def send(root: str, target: str) -> int:
    cfg = config.load(root)
    transport, channel = config.channel(cfg, target)
    tokens = config.tokens(root, transport)
    text, files = _payload(root)
    token = tokens["DISCORD_BOT_TOKEN" if transport == "discord" else "SLACK_BOT_TOKEN"]
    # SDK problems surface here, before any network use: safe to retry.
    # After this point an ImportError is as unknown as any other failure.
    problem = _sdk_problem()
    if problem:
        raise NotSent(problem)
    asyncio.run(_runtime(transport).send(token, channel, text, files))
    return 0


def _pinned() -> dict[str, str]:
    pins = {}
    for line in _REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        name, sep, version = line.partition("==")
        if sep and not line.startswith("#"):
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
            config.settings(cfg, root, transport)
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
    for name in ("run", "send", "check"):
        sub = subs.add_parser(name)
        sub.add_argument("--root", default=HOME, help="設定とdataの保存先（既定: ~/.mcs）")
        if name == "send":
            sub.add_argument("--to", required=True)
            sub.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return run(args.root)
        if args.command == "send":
            return send(args.root, args.to)
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
