"""llm_lane_bench.sh: fail-closed guards, synthetic-only dry run and the
adoption-rule report; stubs only, never the real server or launchctl."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/evaluation/llm_lane_bench.sh"
TOOLS = {"/usr/bin/curl": "curl", "/bin/launchctl": "launchctl",
         "/opt/homebrew/bin/llama-bench": "llama-bench", "/usr/bin/pgrep": "pgrep",
         "/bin/ps": "ps", "/usr/sbin/sysctl": "sysctl"}
OUT = ".local/state/mcs-quality/v1.0.27/llm-bench"


def _render(tmp_path, launchctl_body="exit 113\n", curl_body="exit 7\n", bench_body="exit 0\n",
            pgrep_body="exit 1\n"):
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    bodies = {"launchctl": launchctl_body, "curl": curl_body, "llama-bench": bench_body,
              "pgrep": pgrep_body}
    body = SCRIPT.read_text(encoding="utf-8").replace("/usr/bin/python3", sys.executable)
    for path, name in TOOLS.items():
        (stub / name).write_text('#!/bin/sh\ntouch "$0.called"\nprintf "%s\\n" "$@" >> "$0.args"\n'
                                 + bodies.get(name, "exit 0\n"))
        (stub / name).chmod(0o755)
        body = body.replace(path, str(stub / name))
    runner = tmp_path / "runner.sh"
    runner.write_text(body)
    return runner


def _run(tmp_path, runner, *args):
    return subprocess.run(["bash", str(runner), *args], capture_output=True, text=True,
                          timeout=60, env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})


def _called(tmp_path):
    return sorted(p.name[:-len(".called")] for p in (tmp_path / "stub").glob("*.called"))


def test_refuses_without_flag_and_invokes_nothing(tmp_path):
    proc = _run(tmp_path, _render(tmp_path), "--phase", "e2e")
    assert proc.returncode == 2
    for text in ("ai.mcs.extract-drainer", "ai.mcs.extract-drainer-2", "bootout", "bootstrap",
                 "ai.hermes.llamacpp.plist"):
        assert text in proc.stderr
    assert _called(tmp_path) == []


def test_refuses_while_a_drainer_is_loaded(tmp_path):
    for rc in (0, 97):
        proc = _run(tmp_path, _render(tmp_path, launchctl_body=f"exit {rc}\n"),
                    "--phase", "e2e", "--i-stopped-the-workers")
        assert proc.returncode == 3 and "ai.mcs.extract-drainer" in proc.stderr
        assert _called(tmp_path) == ["launchctl"]


def test_dry_run_writes_synthetic_prompts_only(tmp_path):
    proc = _run(tmp_path, _render(tmp_path), "--phase", "e2e", "--i-stopped-the-workers", "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert _called(tmp_path) == ["launchctl"]   # the worker guard is the only external call
    runs = list((tmp_path / OUT).iterdir())
    assert len(runs) == 1
    run = runs[0]
    prompts = sorted(p.name for p in (run / "prompts").glob("*-r*.txt"))
    assert prompts == sorted(f"{s}-r{r}.txt" for s in ("short", "long", "context") for r in (1, 2, 3))
    texts = {name: (run / "prompts" / name).read_text() for name in prompts}
    prefix = os.path.commonprefix(list(texts.values()))
    spec = texts["short-r1.txt"].split("\n\n", 1)[0] + "\n\n"   # the synthetic spec block
    assert 1000 < len(spec.encode()) < 2500 and prefix.startswith(spec)
    for text in texts.values():
        body = text[len(spec):]
        assert all(line.startswith("合成:") for line in body.splitlines() if line)
        assert not re.search(r"[様@]|さん|\d{7,}", text)
    assert texts["short-r1.txt"] != texts["short-r2.txt"]
    assert json.load(open(run / "prompts/schema.json"))["type"] == "object"
    plan = json.load(open(run / "plan.json"))
    assert plan["blocks"] == ["serial", "concurrent", "serial", "concurrent"]
    assert plan["realtime"]["wire"] == 0
    env = json.load(open(run / "env.json"))
    assert env["dry_run"] is True and env["server_args"] is None
    proc = _run(tmp_path, _render(tmp_path), "--phase", "bench", "--i-stopped-the-workers", "--dry-run")
    assert proc.returncode == 0, proc.stderr
    bench_plan = json.load(open(sorted((tmp_path / OUT).iterdir())[-1] / "plan.json"))
    assert bench_plan["ub_order"] == [512, 128, 256, 512]
    assert _called(tmp_path) == ["launchctl"]


def test_phase_guards_never_reach_the_tools(tmp_path):
    proc = _run(tmp_path, _render(tmp_path, curl_body="exit 0\n"),
                "--phase", "bench", "--i-stopped-the-workers")
    assert proc.returncode == 4 and "llama-bench" not in _called(tmp_path)
    proc = _run(tmp_path, _render(tmp_path, curl_body="exit 7\n"),
                "--phase", "e2e", "--i-stopped-the-workers")
    assert proc.returncode == 4
    args = (tmp_path / "stub/curl.args").read_text()
    assert "POST" not in args and "chat/completions" not in args


@pytest.mark.parametrize("curl_rc,server_rc,pgrep_rc", [
    (28, 113, 1), (7, 0, 1), (7, 97, 1), (7, 113, 0), (7, 113, 2),
])
def test_bench_requires_server_and_process_confirmed_absent(tmp_path, curl_rc, server_rc, pgrep_rc):
    runner = _render(
        tmp_path,
        launchctl_body=f'case "$2" in */ai.mcs.extract-drainer*) exit 113;; esac\nexit {server_rc}\n',
        curl_body=f"exit {curl_rc}\n", pgrep_body=f"exit {pgrep_rc}\n",
        bench_body='if [ "$1" = --version ]; then echo synthetic; exit 0; fi\nexit 88\n')
    proc = _run(tmp_path, runner, "--phase", "bench", "--i-stopped-the-workers")
    assert proc.returncode == 4, proc.stderr
    assert "llama-bench" not in _called(tmp_path)


def test_bench_starts_only_after_all_absence_checks_pass(tmp_path):
    runner = _render(tmp_path, bench_body='if [ "$1" = --version ]; then echo synthetic; exit 0; fi\nexit 88\n')
    proc = _run(tmp_path, runner, "--phase", "bench", "--i-stopped-the-workers")
    assert proc.returncode == 6   # the synthetic benchmark failure, after successful guards
    assert "-m" in (tmp_path / "stub/llama-bench.args").read_text().splitlines()
    service_calls = (tmp_path / "stub/launchctl.args").read_text()
    assert "ai.hermes.llamacpp" in service_calls and "ai.mcs.llamaserver" in service_calls
    assert "-x\nllama-server\n" in (tmp_path / "stub/pgrep.args").read_text()


def _summary(tmp_path, name, ub, mode, **values):
    base = {"n": 9, "errors": 0, "timeouts": 0, "timeout_rate": 0.0, "json_valid_rate": 1.0,
            "valid_per_hour": 100.0, "realtime_p95": 4000, "realtime_n": 6, "realtime_valid_n": 6,
            "rss_kb_after": 7000000, "swap_used_mb_after": 0.0}
    run = tmp_path / OUT / name
    run.mkdir(parents=True)
    (run / "summary.json").write_text(json.dumps(
        {"phase": "e2e", "server_ub": ub, "run": name, "e2e": {mode: {**base, **values}}}))


def test_report_applies_adoption_rule(tmp_path):
    _summary(tmp_path, "r1", 512, "serial")
    _summary(tmp_path, "r2", 256, "serial", valid_per_hour=120.0, realtime_p95=5000)
    _summary(tmp_path, "r3", 256, "concurrent", valid_per_hour=130.0, realtime_p95=3500,
             rss_kb_after=6900000)
    proc = _run(tmp_path, _render(tmp_path), "--report")
    assert proc.returncode == 0, proc.stderr
    rows = [ln for ln in proc.stdout.splitlines() if re.match(r"r\d \|", ln)]
    assert len(rows) == 3 and "基準" in rows[0]
    assert "不成立" in rows[1] and "realtime_p95" in rows[1]
    assert "条件成立" in rows[2]
    assert "pending" in proc.stdout and "所有者" in proc.stdout
    assert _called(tmp_path) == []
    _summary(tmp_path, "r4", 128, "serial", valid_per_hour=140.0, rss_kb_after=None)
    proc = _run(tmp_path, _render(tmp_path), "--report")
    assert "未計測のため判定不可" in [ln for ln in proc.stdout.splitlines() if ln.startswith("r4 |")][0]


@pytest.mark.parametrize("bad", [
    {"http_code": 503}, {"curl_rc": 28}, {"json_valid": False}, {"finish_reason": "length"},
])
def test_fast_realtime_failures_cannot_pass_adoption_rule(tmp_path, bad):
    _summary(tmp_path, "r1", 512, "serial")
    run = tmp_path / OUT / "r2"
    (run / "e2e").mkdir(parents=True)
    (run / "env.json").write_text(json.dumps({"phase": "e2e", "server_ub": 256}))
    ok = {"warmup": False, "block": "1", "http_code": 200, "curl_rc": 0,
          "json_valid": True, "finish_reason": "stop", "timings": None,
          "wall_ms": 1000, "foreign_busy_before": 0}
    rows = [{**ok, "mode": "serial"} for _ in range(9)] + [
        {**ok, "mode": "realtime", "wall_ms": 10, **bad} for _ in range(6)]
    (run / "e2e/requests.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    mem = {"rss_kb": 6000000, "swap_used_mb": 0}
    block = {"block": 1, "mode": "serial", "wall_s": 100, "mem_before": mem, "mem_after": mem}
    (run / "e2e/blocks.jsonl").write_text(json.dumps(block) + "\n")
    runner = _render(tmp_path)
    runner.write_text(runner.read_text().rsplit('main "$@"', 1)[0]
                      + '\nRUN_DIR="$OUT_ROOT/r2"\nsummarize\nreport\n')
    proc = _run(tmp_path, runner)
    assert proc.returncode == 0, proc.stderr
    result = json.load(open(run / "summary.json"))["e2e"]["serial"]
    assert result["realtime_n"] == 6 and result["realtime_valid_n"] == 0
    assert result["realtime_p95"] is None
    row = next(line for line in proc.stdout.splitlines() if line.startswith("r2 | 256 | serial |"))
    assert "判定不可" in row and "条件成立" not in row
    assert _called(tmp_path) == []


@pytest.mark.parametrize("which,valid_n", [("r1", 5), ("r1", None), ("r2", 5), ("r2", None)])
def test_report_requires_complete_realtime_evidence_for_baseline_and_candidate(tmp_path, which, valid_n):
    for name, ub, rate in (("r1", 512, 100), ("r2", 256, 120)):
        extra = {"realtime_valid_n": valid_n} if name == which else {}
        _summary(tmp_path, name, ub, "serial", valid_per_hour=rate, **extra)
    proc = _run(tmp_path, _render(tmp_path), "--report")
    row = next(line for line in proc.stdout.splitlines() if line.startswith("r2 |"))
    assert proc.returncode == 0 and "判定不可" in row and "条件成立" not in row


def test_script_static_safety():
    body = SCRIPT.read_text(encoding="utf-8")
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    assert "--noproxy '*'" in body and " -m " in body
    assert not re.search(r"https?://(?!127\.0\.0\.1)", body)
    assert not re.search(r"sqlite|\.db\b|data/|ledger|plutil|sed -i", body)
    # recovery commands are only ever printed as prose, never executed
    assert not re.search(r"^\s*(\$LAUNCHCTL|launchctl)\s+(bootout|bootstrap|kickstart)", body, re.M)
    for text in ("/opt/homebrew/bin/llama-bench", "enable_thinking", '"max_tokens": 1600', "id_slot",
                 '"temperature": 0', "json_schema"):
        assert text in body
