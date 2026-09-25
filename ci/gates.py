#!/usr/bin/env python3
"""Static CI gates distilled from this repo's incident history.

Each gate encodes a defect class that actually bit this codebase (see
docs/dev-records/ and ci/gates-coverage.json). A gate returns a list of
human-readable violations; empty means pass. Run: ``python3 ci/gates.py``
(exit 1 on any violation, 0 when clean). Stdlib only, offline.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MCS = ROOT / "mcs"
PLUGIN = ROOT / "hermes_plugin"

# Files allowed to construct a write-mode Ledger. Anything else opening
# the ledger read-write is a regression of the snapshot/read-only contract.
LEDGER_WRITERS = {
    "extract.py", "extract_llm.py", "init_data.py", "rollup.py",
    "run_check.py", "semantic.py", "semantic_drain.py",
}

_LOCAL_MODULES = {p.stem for p in MCS.rglob("*.py")} | {"hermes_plugin"}

# These adapters use the messaging SDK already owned by Hermes. Core
# collectors must remain dependency-free, and importing /mcs must work
# without an SDK installed. No new client/token ownership is delegated.
_SDK_FILES = {"mcs_discord/actions.py", "mcs_discord/cards.py"}
_ASYNC_FILES = {"mcs_discord/actions.py", "mcs_discord/delivery.py",
                "mcs_discord/tasks.py", "mcs_delivery/worker.py",
                "mcs_slack/actions.py", "mcs_slack/delivery.py",
                "mcs_slack/tasks.py"}
_ASYNC_MEMBERS = {"sleep", "to_thread", "CancelledError"}
_SDK_MEMBERS = {
    "ui.LayoutView", "ui.TextDisplay", "ui.ActionRow", "ui.Button",
    "ui.Modal", "ui.TextInput", "ui.View", "ui.Container",
    "ButtonStyle", "ButtonStyle.success", "ButtonStyle.secondary",
    "ButtonStyle.primary", "TextStyle.short", "TextStyle.paragraph",
    "Webhook.partial", "WebhookType.application",
}


def _plugin_path(path: Path) -> str:
    return path.relative_to(PLUGIN).as_posix() if path.is_relative_to(PLUGIN) else ""


def _lazy_sdk_import(path, node, parents) -> bool:
    if (_plugin_path(path) not in _SDK_FILES or not isinstance(node, ast.Import)
            or not all(a.name == "discord" for a in node.names)):
        return False
    ancestor = parents.get(node)
    while ancestor is not None:
        if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return True
        ancestor = parents.get(ancestor)
    return False


def _module_surface(tree, module: str, permitted: set[str]) -> list[str]:
    """Check imported module aliases without granting dynamic API access."""
    parents = {child: parent for parent in ast.walk(tree)
               for child in ast.iter_child_nodes(parent)}
    aliases = {alias.asname or module for node in ast.walk(tree)
               if isinstance(node, ast.Import) for alias in node.names
               if alias.name == module}
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Name) or node.id not in aliases:
            continue
        member, current = [], node
        while isinstance(parents.get(current), ast.Attribute) \
                and parents[current].value is current:
            current = parents[current]
            member.append(current.attr)
        if ".".join(member) not in permitted:
            bad.append(f"{node.lineno} forbidden {module} surface")
    return bad


def _py_files(*dirs: Path) -> list[Path]:
    # mcs/ modules live in first-level subdirs (flat import namespace —
    # the filename stays the identity); skip __pycache__ artifacts.
    out = []
    for d in dirs:
        out.extend(sorted(p for p in d.rglob("*.py")
                          if "__pycache__" not in p.parts))
    return out


def gate_stdlib_only() -> list[str]:
    """Stdlib/local only, except deferred Hermes-owned Discord UI imports."""
    bad = []
    stdlib = sys.stdlib_module_names
    for path in _py_files(MCS, PLUGIN):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as e:
            bad.append(f"{path.name}: unparseable ({e})")
            continue
        parents = {child: parent for parent in ast.walk(tree)
                   for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [] if node.level else [node.module.split(".")[0]]
            else:
                continue
            for m in mods:
                if m == "discord" and _lazy_sdk_import(path, node, parents):
                    continue
                if m not in stdlib and m not in _LOCAL_MODULES:
                    bad.append(f"{path.name}:{node.lineno} imports {m}")
        bad.extend(f"{path.name}:{v}" for v in
                   _module_surface(tree, "discord", _SDK_MEMBERS))
    return bad


_PLATFORM_URLS = re.compile(
    r"discord\.com/api|api\.telegram\.org|slack\.com/api|discordapp\.com")
_PLATFORM_TOKENS = re.compile(
    r"DISCORD_BOT_TOKEN|DISCORD_TOKEN|TELEGRAM_BOT_TOKEN|"
    r"SLACK_BOT_TOKEN|SLACK_APP_TOKEN")


def gate_no_direct_platform_api() -> list[str]:
    """Delivery goes through `hermes send`. Direct platform REST/env-token
    code in mcs/ is the removed pre-standard implementation — forbid it."""
    bad = []
    for path in _py_files(MCS):
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if _PLATFORM_URLS.search(line):
                bad.append(f"{path.name}:{i} direct platform API URL")
            if _PLATFORM_TOKENS.search(line):
                bad.append(f"{path.name}:{i} platform token env read")
    return bad


# Modules the plugin must never import (network/process surfaces). Checked
# via AST so the local `mcs_requests` alias bound as `requests` is not a
# false positive.
_PLUGIN_FORBIDDEN_IMPORTS = {
    "subprocess", "socket", "urllib", "requests", "http", "ftplib",
    "smtplib", "asyncio",
}
_PLUGIN_FORBIDDEN_TEXT = re.compile(r"\bos\.environ\b|\bshutil\.rmtree\b")


def gate_plugin_sandbox() -> list[str]:
    """The Hermes plugin is an untrusted-context adapter: no ambient env,
    no direct network, no subprocess; the native Discord adapter uses
    only the host's client plus sleep/to_thread/cancellation primitives."""
    bad = []
    for path in _py_files(PLUGIN):
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            bad.append(f"{path.name}: unparseable")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [] if node.level else [node.module.split(".")[0]]
            else:
                continue
            for m in mods:
                if (m == "asyncio" and _plugin_path(path) in _ASYNC_FILES
                        and isinstance(node, ast.Import)
                        and all(a.name == "asyncio" for a in node.names)):
                    continue
                if m in _PLUGIN_FORBIDDEN_IMPORTS:
                    bad.append(f"{path.name}:{node.lineno} imports {m}")
        bad.extend(f"{path.name}:{v}" for v in
                   _module_surface(tree, "asyncio", _ASYNC_MEMBERS))
        for i, line in enumerate(text.splitlines(), 1):
            if _PLUGIN_FORBIDDEN_TEXT.search(line):
                bad.append(f"{path.name}:{i} forbidden surface: "
                           f"{line.strip()[:60]}")
            if re.search(r"\bLedger\s*\(", line):
                bad.append(f"{path.name}:{i} write-mode Ledger open")
    return bad


# maintenance.py opens its BACKUP TARGET (a fresh tmp file) read-write —
# that connect() writes the copy, never the source ledger.
_SQLITE_RW_OK = {"maintenance.py"}


def gate_snapshot_readonly() -> list[str]:
    """Only whitelisted modules may construct a write-mode Ledger(); any
    sqlite3.connect elsewhere must carry mode=ro/immutable (snapshot
    contract — mcs_view/brain_export/plugin read published snapshots)."""
    bad = []
    for path in _py_files(MCS, PLUGIN):
        text = path.read_text(encoding="utf-8")
        if path.is_relative_to(MCS) \
                and path.name in LEDGER_WRITERS | {"ledger.py"}:
            continue
        for m in re.finditer(r"\bLedger\s*\(", text):
            line = text.count("\n", 0, m.start()) + 1
            bad.append(f"{path.name}:{line} write-mode Ledger(")
        if path.name in _SQLITE_RW_OK:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            # the read-only marker must sit on the connect() line itself —
            # a plain "uri" token previously exempted `connect(uri)` where
            # the URI was built (possibly without mode=ro) elsewhere
            # (FIX-G1)
            if "sqlite3.connect" in line and "mode=ro" not in line \
                    and "immutable" not in line:
                bad.append(f"{path.name}:{i} sqlite3.connect without "
                           f"mode=ro/immutable")
    return bad


def gate_writer_lock() -> list[str]:
    """FIX-R00-01: every module that opens a write-mode Ledger must hold
    acquire_run_lock — manual CLIs previously wrote the DB unflocked."""
    bad = []
    for path in _py_files(MCS):
        text = path.read_text(encoding="utf-8")
        if path.name == "ledger.py":
            continue
        if re.search(r"\bLedger\s*\(", text) and \
                "acquire_run_lock" not in text:
            bad.append(f"{path.name}: write-mode Ledger without "
                       f"acquire_run_lock")
    return bad


_CHANNEL_LITERAL = re.compile(r"discord:\d{10,}|\b\d{17,20}\b")


def gate_notify_fail_closed() -> list[str]:
    """No hardcoded delivery destination: a missing notify_target must
    skip, never fall back to a baked-in channel (patient-content risk)."""
    bad = []
    for path in _py_files(MCS, PLUGIN):
        for i, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1):
            if _CHANNEL_LITERAL.search(line) and "test" not in path.name:
                bad.append(f"{path.name}:{i} hardcoded channel/chat id")
    return bad


def gate_install_pin() -> list[str]:
    """install.sh must pin hermes-agent to a 40-hex commit, not float."""
    bad = []
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    m = re.search(r'^HERMES_PIN="([0-9a-f]{40})"$', text, re.M)
    if not m:
        bad.append("install.sh: HERMES_PIN missing or not a 40-hex commit")
    if 'HERMES_REPO="https://github.com/' not in text:
        bad.append("install.sh: HERMES_REPO pin missing")
    return bad


def gate_records_isolation() -> list[str]:
    """docs/dev-records must not gain executable config or secrets —
    they are evidence, not runtime input."""
    bad = []
    # rglob — a record nested in a subdirectory must be audited too, not
    # silently skipped (FIX-G2)
    for path in sorted((ROOT / "docs" / "dev-records").rglob("*")):
        if not path.is_file():
            continue
        if path.suffix not in {".md", ".json"}:
            bad.append(f"{path.name}: unexpected file type in dev-records")
        if path.suffix == ".json":
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                bad.append(f"{path.name}: invalid JSON ({e})")
    return bad


GATES = {
    "stdlib_only": gate_stdlib_only,
    "no_direct_platform_api": gate_no_direct_platform_api,
    "plugin_sandbox": gate_plugin_sandbox,
    "snapshot_readonly": gate_snapshot_readonly,
    "writer_lock": gate_writer_lock,
    "notify_fail_closed": gate_notify_fail_closed,
    "install_pin": gate_install_pin,
    "records_isolation": gate_records_isolation,
}


def main() -> int:
    failed = 0
    for name, fn in GATES.items():
        violations = fn()
        if violations:
            failed += 1
            print(f"FAIL {name} ({len(violations)} violations)")
            for v in violations[:20]:
                print(f"  - {v}")
        else:
            print(f"pass {name}")
    print(f"\n{len(GATES) - failed}/{len(GATES)} gates pass")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
