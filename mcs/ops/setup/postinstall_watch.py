"""初期設定後の動作確認を繰り返し、失敗時のAIエージェント向け修正依頼を保存する。"""
from __future__ import annotations

from contextlib import ExitStack, suppress
import fcntl
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from typing import Callable

import safe_io

REPOSITORY_URL = "https://github.com/yusuketakuma/hermes-mcs"
COMMAND_TIMEOUT = 90


def _default_runner(argv: list[str], timeout: float) -> dict:
    try:
        doctor = argv[2] == "doctor"
        done = subprocess.run(argv, stdout=subprocess.PIPE if doctor else subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=timeout)
        if doctor and done.returncode == 0:
            return _doctor_result(done.stdout)
        return {"rc": done.returncode}
    except (OSError, subprocess.TimeoutExpired):
        return {"rc": 2}


def _doctor_result(raw: bytes) -> dict:
    from support_diagnostics import SCHEMA_VERSION
    try:
        report = json.loads(raw)
        if report["schema_version"] != SCHEMA_VERSION:
            raise ValueError
        mode = report["software"]["mode"]
        if mode not in ("standalone", "hermes"):
            raise ValueError
        checks = report["checks"]
        required = ["card_worker", "restore_delivery"]
        if sys.platform == "darwin" or mode == "standalone":
            required.append("services")
        for name in required:
            if checks[name]["status"] != "healthy":
                return {"rc": 1, "reason": name}
        if report["collection"]["status"] != "healthy":
            return {"rc": 1, "reason": "collection"}
    except (KeyError, TypeError, ValueError, RecursionError):
        return {"rc": 1, "reason": "diagnosis_unverified"}
    return {"rc": 0}


def _probe_commands(repo: Path) -> list[tuple[str, list[str]]]:
    setup = str(repo / "mcs" / "ops" / "mcs_setup.py")
    return [
        ("check", [sys.executable, setup, "check"]),
        ("doctor", [sys.executable, setup, "doctor", "--probe", "services", "--probe", "data", "--share"]),
    ]


def _write_at(directory: int, name: str, text: str) -> None:
    tmp = ".postinstall-" + secrets.token_hex(12)
    fd = safe_io.create_exclusive(tmp, dir_fd=directory)
    try:
        with safe_io.fdopen_owned(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=directory)


def _publish(home: str, status: dict, brief: str | None) -> None:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    with ExitStack() as stack:
        directory = os.open(home, flags)
        stack.callback(os.close, directory)
        for name in ("data", "postinstall-watch"):
            with suppress(FileExistsError):
                os.mkdir(name, 0o700, dir_fd=directory)
            directory = os.open(name, flags, dir_fd=directory)
            stack.callback(os.close, directory)
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise PermissionError("unsafe directory")
        os.fchmod(directory, 0o700)
        fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if brief is None:
            with suppress(FileNotFoundError):
                os.unlink("agent-repair-brief.md", dir_fd=directory)
        else:
            _write_at(directory, "agent-repair-brief.md", brief)
        _write_at(directory, "status.json", json.dumps(status, ensure_ascii=False, indent=2) + "\n")
        os.fsync(directory)


def _agent_brief(cycle: int, probes: list[dict]) -> str:
    failed = [probe for probe in probes if probe["rc"] != 0]
    parts = [
        "# MCS 初期導入後の動作確認: 修正依頼",
        "",
        "このファイルは `mcs setup postinstall` が失敗を検出したときに更新します。",
        "AIエージェントはリポジトリのセッションを終了せず、以下の失敗を修正してから同じコマンドを再実行してください。",
        "",
        f"- リポジトリURL: {REPOSITORY_URL}",
        "- 実行手順: docs/guides/SETUP_AGENT.md",
        "- 確認コマンド: `mcs setup postinstall`",
        f"- 失敗した確認回: {cycle}",
        "",
        "## 失敗した確認",
    ]
    for probe in failed:
        parts.append(f"- {probe['name']} (exit {probe['rc']}): {probe['reason']}")
    parts.extend([
        "",
        "## 修正の境界",
        "",
        "設定値・秘密値・患者データをこのファイルや会話へ貼らないでください。",
        "実データ・稼働サービス・外部送信を変える操作は、利用者の明示承認がある範囲だけで行ってください。",
        "修正後は `mcs setup postinstall` が終了コード0になるまで確認を続けてください。",
    ])
    return "\n".join(parts) + "\n"


def run(*, home: str, repo: str, duration: float = 600.0, interval: float = 60.0,
        stable: int = 2, once: bool = False, runner: Callable | None = None,
        sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
        out: Callable[[str], None] = print) -> int:
    """Run bounded read-only checks until ``stable`` consecutive healthy cycles."""
    if not 0 < duration <= 3600 or not 0 < interval <= 300 or not 1 <= stable <= 5:
        out("確認時間・間隔・成功回数の指定を確認してください")
        return 2
    runner = runner or _default_runner
    root = Path(repo)
    state_dir = Path(home) / "data" / "postinstall-watch"
    brief_path = state_dir / "agent-repair-brief.md"
    start = clock()
    deadline = start + duration
    cycle = 0
    healthy_streak = 0
    while True:
        cycle += 1
        probes = []
        for name, argv in _probe_commands(root):
            remaining = deadline - clock()
            if remaining <= 0:
                probes.append({"name": name, "rc": 1, "reason": "deadline"})
                break
            result = runner(argv, min(COMMAND_TIMEOUT, remaining))
            if clock() >= deadline:
                probes.append({"name": name, "rc": 1, "reason": "deadline"})
                break
            probes.append({"name": name, "rc": int(result["rc"]),
                           "reason": result.get("reason", "command_failed")})
        failed = [probe for probe in probes if probe["rc"] != 0]
        status = {"version": 1, "cycle": cycle, "healthy": not failed,
                  "healthy_streak": 0 if failed else healthy_streak + 1,
                  "checks": [{"name": p["name"], "rc": p["rc"]} for p in probes],
                  "updated_at": time.time()}
        try:
            _publish(home, status, _agent_brief(cycle, probes) if failed else None)
        except OSError:
            out("確認結果の保存先を準備できませんでした。保存先の権限と空き容量を確認してください")
            return 1
        if failed:
            healthy_streak = 0
            out(f"導入後の動作確認: 要修正（{cycle}回目）。AIエージェント向け修正依頼: {brief_path}")
        else:
            healthy_streak += 1
            out(f"導入後の動作確認: 正常（{healthy_streak}/{stable}回）")
            if once or healthy_streak >= stable:
                return 0
        if once:
            return 1
        now = clock()
        if now >= deadline:
            out("導入後の動作確認: 正常終了を確認できませんでした。修正後に `mcs setup postinstall` を再実行してください")
            return 1
        sleep(min(interval, max(0.0, deadline - now)))
        if clock() >= deadline:
            out("導入後の動作確認: 正常終了を確認できませんでした。修正後に `mcs setup postinstall` を再実行してください")
            return 1
