"""初期設定後の動作確認を繰り返し、失敗時のAIエージェント向け修正依頼を保存する。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable

import safe_io

REPOSITORY_URL = "https://github.com/yusuketakuma/hermes-mcs"
COMMAND_TIMEOUT = 90
OUTPUT_LIMIT = 6000


def _default_runner(argv: list[str], timeout: float) -> dict:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return {"rc": done.returncode, "output": (done.stdout or "") + (done.stderr or "")}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"rc": 2, "output": f"確認コマンドを完了できませんでした（{type(error).__name__}）"}


def _probe_commands(repo: Path) -> list[tuple[str, list[str]]]:
    setup = str(repo / "mcs" / "ops" / "mcs_setup.py")
    return [
        ("check", [sys.executable, setup, "check"]),
        ("doctor", [sys.executable, setup, "doctor", "--probe", "services", "--probe", "data"]),
    ]


def _clip(text: str) -> str:
    return text if len(text) <= OUTPUT_LIMIT else "…省略…\n" + text[-OUTPUT_LIMIT:]


def _private_write(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    safe_io.atomic_write(str(path), lambda stream: stream.write(text), 0o600, makedirs=False)


def _write_status(path: Path, status: dict) -> None:
    _private_write(path, json.dumps(status, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


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
        parts.extend([
            "",
            f"### {probe['name']} (exit {probe['rc']})",
            "",
            "```text",
            _clip(probe["output"]).rstrip() or "出力なし",
            "```",
        ])
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
    if duration <= 0 or duration > 3600 or interval <= 0 or interval > 300 or not 1 <= stable <= 5:
        out("確認時間・間隔・成功回数の指定を確認してください")
        return 2
    runner = runner or _default_runner
    root = Path(repo)
    state_dir = Path(home) / "data" / "postinstall-watch"
    status_path = state_dir / "status.json"
    brief_path = state_dir / "agent-repair-brief.md"
    start = clock()
    deadline = start + duration
    cycle = 0
    healthy_streak = 0
    while True:
        cycle += 1
        probes = []
        for name, argv in _probe_commands(root):
            result = runner(argv, COMMAND_TIMEOUT)
            probes.append({"name": name, "rc": int(result["rc"]), "output": str(result.get("output", ""))})
        failed = [probe for probe in probes if probe["rc"] != 0]
        status = {"version": 1, "cycle": cycle, "healthy": not failed,
                  "healthy_streak": 0 if failed else healthy_streak + 1,
                  "checks": [{"name": p["name"], "rc": p["rc"]} for p in probes],
                  "updated_at": time.time()}
        try:
            _write_status(status_path, status)
            if failed:
                _private_write(brief_path, _agent_brief(cycle, probes))
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
