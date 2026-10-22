#!/bin/bash
# Owner-machine benchmark for proposal 5 (background serial vs concurrent,
# then -ub 128/256/512). Two phases to keep the memory budgets separate for
# llama-bench and the resident llama-server at once (same model loaded
# twice swaps). -ub is a start flag of the Hermes-owned llamacpp plist;
# this script never edits a plist and never stops or starts a service.
# Label names must match service_contract.RESIDENT_LABELS.
# Every recorded value is numeric/boolean/reason code: no prompt or
# response text is kept, and the prompts are generated synthetic text.
set -u

CURL=/usr/bin/curl
LAUNCHCTL=/bin/launchctl
LLAMA_BENCH=/opt/homebrew/bin/llama-bench
PY=/usr/bin/python3
PS=/bin/ps
PGREP=/usr/bin/pgrep
SYSCTL=/usr/sbin/sysctl
SERVER=http://127.0.0.1:8080            # loopback only, --noproxy '*' on every call
MODEL=${MCS_BENCH_MODEL:-$HOME/.hermes/models/Qwen3.5-9B-Q4_K_M.gguf}
LABELS="ai.mcs.extract-drainer ai.mcs.extract-drainer-2"
TIMEOUT=300   # = extract_llm.TIMEOUT
MAX_TOKENS=1600   # = extract_llm.MAX_TOKENS / patient_nightly
REPS=3
UB_ORDER="512 128 256 512"       # A B C A, after one discarded warm-up at 512
OUT_ROOT=$HOME/.local/state/mcs-quality/v1.0.27/llm-bench

PHASE=""
STOPPED=0
DRY_RUN=0
REPORT=0
RUN_DIR=""

usage() {
  cat >&2 <<EOF
usage: llm_lane_bench.sh --phase bench|e2e --i-stopped-the-workers [--dry-run]
       llm_lane_bench.sh --report

The resident workers must be stopped first (both labels):
  対象: ai.mcs.extract-drainer, ai.mcs.extract-drainer-2
  停止: launchctl bootout gui/\$(id -u)/<label>
  復旧: launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/<label>.plist
  bench phase additionally needs the llama-server agent stopped (same commands,
  label ai.hermes.llamacpp or ai.mcs.llamaserver); e2e needs it running.
  -ub は ai.hermes.llamacpp.plist（Hermes 所有）の起動フラグの変更であり、
  本 script は plist を編集しない。-ub を変えた窓ごとに本 script を実行し、
  --report で run ごとに記録された実 argv を照合する。
exit codes: 2 usage, 3 workers not confirmed stopped, 4 wrong phase for
server state, 5 server never idle, 6 llama-bench failed.
EOF
  exit 2
}

guard_unloaded() {
  local code=$1 labels=$2 label rc
  for label in $labels; do
    "$LAUNCHCTL" print "gui/$(id -u)/$label" >/dev/null 2>&1
    rc=$?
    # 113 = not found is the only proof the agent is unloaded (mcs_recover
    # convention); 0 = loaded, 97 = guard stub, anything else = unverifiable
    if [ "$rc" != 113 ]; then
      echo "service still loaded or unverifiable (launchctl print rc=$rc): $label" >&2
      exit "$code"
    fi
  done
}

server_reachable() {
  "$CURL" --noproxy '*' -s -m 3 -o /dev/null "$SERVER/slots"
}

guard_phase() {
  local rc
  if [ "$PHASE" = bench ]; then
    server_reachable
    rc=$?
    # An HTTP timeout is not proof the model was unloaded.
    if [ "$rc" != 7 ]; then
      echo "server still answering or unverifiable (curl rc=$rc) — stop the server agent first" >&2
      exit 4
    fi
    guard_unloaded 4 "ai.hermes.llamacpp ai.mcs.llamaserver"
    "$PGREP" -x llama-server >/dev/null 2>&1
    if [ "$?" != 1 ]; then
      echo "llama-server process still present or unverifiable" >&2
      exit 4
    fi
  elif ! server_reachable; then
    echo "llama-server is not answering on $SERVER: the e2e phase needs the resident server" >&2
    exit 4
  fi
}

busy_slots() {
  "$CURL" --noproxy '*' -s -m 3 "$SERVER/slots" 2>/dev/null | "$PY" -c '
import json, sys
try:
    slots = json.load(sys.stdin)
    if (not isinstance(slots, list) or not slots
            or any(not isinstance(s, dict) or type(s.get("is_processing")) is not bool
                   for s in slots)):
        raise ValueError("invalid slot snapshot")
    print(sum(s["is_processing"] for s in slots))
except Exception:
    print(-1)
'
}

wait_idle() {
  local i
  for i in $(seq 1 120); do
    [ "$(busy_slots)" = 0 ] && return 0
    sleep 1
  done
  echo "server slots never went idle within 120 s" >&2
  exit 5
}

mem_sample() {
  local pid
  pid=$("$PGREP" -x llama-server | head -1)
  "$PY" - "$pid" "$("$PS" -o rss= -p "${pid:-0}" 2>/dev/null)" "$("$SYSCTL" -n vm.swapusage 2>/dev/null)" <<'PYEOF'
import json, re, sys
pid, rss, swap = sys.argv[1:4]
m = re.search(r"used = ([0-9.]+)M", swap)
print(json.dumps({"pid": int(pid) if pid.isdigit() else None,
                  "rss_kb": int(rss.strip()) if rss.strip().isdigit() else None,
                  "swap_used_mb": float(m.group(1)) if m else None}))
PYEOF
}

write_env() {
  local model_name hw_model memsize bench_version server_args git_rev
  model_name=$(basename "$MODEL")
  hw_model=null; memsize=null; bench_version=null; server_args=null; git_rev=null
  if [ "$DRY_RUN" != 1 ]; then
    hw_model=$("$SYSCTL" -n hw.model 2>/dev/null)
    memsize=$("$SYSCTL" -n hw.memsize 2>/dev/null)
    git_rev=$(git -C "$(dirname "$0")" rev-parse HEAD 2>/dev/null)
    if [ "$PHASE" = bench ]; then
      bench_version=$("$LLAMA_BENCH" --version 2>&1 | head -1)
    else
      server_args=$("$PS" -o args= -p "$("$PGREP" -x llama-server | head -1)" 2>/dev/null)
    fi
  fi
  "$PY" - "$RUN_DIR/env.json" "$PHASE" "$model_name" "$hw_model" "$memsize" "$bench_version" "$server_args" "$git_rev" "$DRY_RUN" <<'PYEOF'
import json, re, sys
out, phase, model, hw, mem, bench, args, rev, dry = sys.argv[1:10]
def opt(v):
    return None if v in ("", "null") else v
ub = None
if opt(args):
    m = re.search(r"(?:^|\s)(?:-ub|--ubatch-size)\s+(\d+)", args)
    ub = int(m.group(1)) if m else 512   # llama.cpp default when the flag is absent
env = {"phase": phase, "model": model, "hw_model": opt(hw),
       "hw_memsize": int(mem) if mem.isdigit() else None,
       "llama_bench_version": opt(bench), "server_args": opt(args),
       "server_ub": ub, "git_rev": opt(rev), "dry_run": dry == "1"}
json.dump(env, open(out, "w"), ensure_ascii=False, indent=1)
PYEOF
}

write_prompts() {
  mkdir -p "$RUN_DIR/prompts"
  "$PY" - "$RUN_DIR/prompts" "$REPS" <<'PYEOF'
import json, sys
out, reps = sys.argv[1], int(sys.argv[2])
# synthetic stand-in for the production spec: same order of length
# (~1,500 bytes), so the slot prefix cache covers it like the real one
spec = ("合成指示: 在宅医療チャットの記録を構造化する合成ベンチ用の指示です。"
        "出力はJSONのみ。項目は summary と items。items の各要素は text を持つ。"
        "本文に無い値を補わない。否定・過去・予定・家族の区別を保つ。"
        "薬剤名は本文の表記のまま。数量と日付は本文の表記のまま。\n") * 3 + (
        "出力契約: {\"summary\": 文字列, \"items\": [{\"text\": 文字列}]}。"
        "説明文や前置きを出力しない。\n\n")
line = "合成: 薬剤Aを朝に1錠、薬剤Bを夕に2錠。2000-01-01 に確認。数値は12で変化なし。\n"
sizes = {"short": 600, "long": 4000, "context": 12000}
for shape, size in sizes.items():
    for rep in range(1, reps + 1):
        body = ""
        while len(body) < size:
            body += line
        body += f"合成: rep={rep}\n"
        open(f"{out}/{shape}-r{rep}.txt", "w").write(spec + body)
schema = {"type": "object",
          "properties": {"summary": {"type": "string"},
                         "items": {"type": "array",
                                   "items": {"type": "object",
                                             "properties": {"text": {"type": "string"}},
                                             "required": ["text"]}}},
          "required": ["summary"]}
json.dump(schema, open(f"{out}/schema.json", "w"))
PYEOF
}

write_plan() {
  "$PY" - "$RUN_DIR/plan.json" "$PHASE" "$REPS" "$UB_ORDER" "$TIMEOUT" <<'PYEOF'
import json, sys
out, phase, reps, ub_order, timeout = sys.argv[1:6]
reps, timeout = int(reps), int(timeout)
plan = {"phase": phase, "reps": reps}
if phase == "bench":
    plan["ub_order"] = [int(u) for u in ub_order.split()]
    plan["warmup"] = {"ub": 512, "discarded": True}
    plan["estimated_minutes"] = 15
else:
    plan["blocks"] = ["serial", "concurrent", "serial", "concurrent"]
    plan["serial"] = {"wires": [1]}
    plan["concurrent"] = {"wires": [1, 2]}
    plan["realtime"] = {"wire": 0, "shape": "short",
                        "injected_with": ["long", "context"]}
    plan["shapes"] = ["short", "long", "context"]
    plan["request_timeout_s"] = timeout
    plan["estimated_minutes"] = 15
json.dump(plan, open(out, "w"), indent=1)
PYEOF
}

run_bench() {
  local i=0 ub
  mkdir -p "$RUN_DIR/bench"
  "$LLAMA_BENCH" -m "$MODEL" -ngl 99 -fa 1 -ctk q4_0 -ctv q4_0 -ub 512 -p 512 -n 32 -r 1 -o json \
    > "$RUN_DIR/bench/warmup.json" || { echo "llama-bench warm-up failed" >&2; exit 6; }
  for ub in $UB_ORDER; do
    i=$((i + 1))
    "$LLAMA_BENCH" -m "$MODEL" -ngl 99 -fa 1 -ctk q4_0 -ctv q4_0 -ub "$ub" -p 512,2048,8192 -n 128 -r 5 -o json \
      > "$RUN_DIR/bench/ub-$ub-seq$i.json" || { echo "llama-bench failed at -ub $ub" >&2; exit 6; }
  done
}

# chat WIRE SHAPE REP MODE BLOCK [warmup]
chat() {
  local wire=$1 shape=$2 rep=$3 mode=$4 block=$5 warmup=${6:-false}
  local body resp started busy rc out
  body=$(mktemp "$RUN_DIR/e2e/body.XXXXXX")
  resp=$(mktemp "$RUN_DIR/e2e/resp.XXXXXX")
  "$PY" - "$RUN_DIR/prompts/$shape-r$rep.txt" "$RUN_DIR/prompts/schema.json" "$wire" "$body" <<'PYEOF'
import json, sys
prompt_path, schema_path, wire, out = sys.argv[1:5]
body = {"model": "local",
        "messages": [{"role": "user", "content": open(prompt_path).read()}],
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
        "max_tokens": 1600,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "bench", "schema": json.load(open(schema_path))}},
        "id_slot": int(wire),
        "cache_prompt": True}
json.dump(body, open(out, "w"), ensure_ascii=False)
PYEOF
  busy=$(busy_slots)
  started=$("$PY" -c 'import time; print(time.time())')  # BSD date has no %N
  out=$("$CURL" --noproxy '*' -s -m "$TIMEOUT" -o "$resp" -w '%{http_code} %{time_total}' \
        -X POST -H 'Content-Type: application/json' --data-binary "@$body" \
        "$SERVER/v1/chat/completions")
  rc=$?
  "$PY" - "$resp" "$rc" "$out" "$wire" "$shape" "$rep" "$mode" "$block" "$warmup" "$busy" "$started" >> "$RUN_DIR/e2e/requests.jsonl" <<'PYEOF'
import json, sys
resp, rc, out, wire, shape, rep, mode, block, warmup, busy, started = sys.argv[1:12]
code, wall = (out.split() + ["0", "0"])[:2]
row = {"wire": int(wire), "shape": shape, "rep": int(rep), "mode": mode, "block": block,
       "warmup": warmup == "true", "curl_rc": int(rc), "http_code": int(code or 0),
       "wall_ms": int(float(wall) * 1000), "timings": None, "finish_reason": None,
       "json_valid": None, "foreign_busy_before": int(busy), "started_at": float(started)}
try:
    data = json.load(open(resp))
    t = data.get("timings") or {}
    row["timings"] = {k: t.get(k) for k in ("prompt_n", "prompt_ms", "predicted_n", "predicted_ms", "cache_n")}
    choice = (data.get("choices") or [{}])[0]
    row["finish_reason"] = choice.get("finish_reason")
    content = (choice.get("message") or {}).get("content")
    try:
        row["json_valid"] = isinstance(json.loads(content), dict)
    except Exception:
        row["json_valid"] = False
except Exception:
    pass
print(json.dumps(row))
PYEOF
  rm -f "$body" "$resp"
}

# series WIRE MODE BLOCK REALTIME(0|1): short,long,context x REPS on one wire;
# the realtime short on wire 0 starts together with each long/context request
series() {
  local wire=$1 mode=$2 block=$3 realtime=$4 rep shape
  for rep in $(seq 1 "$REPS"); do
    for shape in short long context; do
      if [ "$realtime" = 1 ] && [ "$shape" != short ]; then
        chat 0 short "$rep" realtime "$block" &
      fi
      chat "$wire" "$shape" "$rep" "$mode" "$block"
      wait
    done
  done
}

run_e2e() {
  local block=0 mode before after started
  mkdir -p "$RUN_DIR/e2e"
  wait_idle
  for wire in 0 1 2; do
    chat "$wire" short 1 warmup warmup true
  done
  for mode in serial concurrent serial concurrent; do
    block=$((block + 1))
    wait_idle
    before=$(mem_sample)
    started=$("$PY" -c 'import time; print(time.time())')  # BSD date has no %N
    if [ "$mode" = serial ]; then
      series 1 serial "$block" 1
    else
      series 1 concurrent "$block" 1 &
      series 2 concurrent "$block" 0 &
      wait
    fi
    after=$(mem_sample)
    "$PY" -c 'import json,sys,time; print(json.dumps({"block": int(sys.argv[1]), "mode": sys.argv[2], "wall_s": round(time.time()-float(sys.argv[3]),1), "mem_before": json.loads(sys.argv[4]), "mem_after": json.loads(sys.argv[5])}))' \
      "$block" "$mode" "$started" "$before" "$after" >> "$RUN_DIR/e2e/blocks.jsonl"
  done
}

summarize() {
  "$PY" - "$RUN_DIR" <<'PYEOF'
import glob, json, os, statistics, sys
run = sys.argv[1]
env = json.load(open(os.path.join(run, "env.json")))
summary = {"phase": env["phase"], "server_ub": env.get("server_ub"), "git_rev": env.get("git_rev"),
           "model": env.get("model"), "run": os.path.basename(run)}
lines = []

def pct(values, p):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round((len(values) - 1) * p)))]

if env["phase"] == "bench":
    per_ub = {}
    for path in glob.glob(os.path.join(run, "bench", "ub-*-seq*.json")):
        try:
            rows = json.load(open(path))
        except Exception:
            continue
        for r in rows:
            ub = r.get("n_ubatch")
            name = (f"pp{r['n_prompt']}" if r.get("n_prompt") else f"tg{r.get('n_gen')}")
            per_ub.setdefault(str(ub), {}).setdefault(name, []).append((r.get("avg_ts"), r.get("stddev_ts")))
    summary["bench"] = {ub: {name: {"avg_ts": round(statistics.mean(a for a, _ in v), 2),
                                     "stddev_ts": round(max(s or 0 for _, s in v), 2), "n": len(v)}
                             for name, v in tests.items()} for ub, tests in per_ub.items()}
    summary["note"] = "llama-bench has no --spec-type/-np; candidates only, decide on the e2e phase"
    lines.append("bench (llama-bench, t/s avg ± max stddev; no --spec-type/-np):")
    for ub, tests in sorted(summary["bench"].items(), key=lambda kv: int(kv[0])):
        lines.append(f"  -ub {ub}: " + ", ".join(f"{n} {t['avg_ts']}±{t['stddev_ts']}" for n, t in sorted(tests.items())))
else:
    rows = [json.loads(l) for l in open(os.path.join(run, "e2e", "requests.jsonl")) if l.strip()]
    blocks = [json.loads(l) for l in open(os.path.join(run, "e2e", "blocks.jsonl")) if l.strip()]
    e2e = {}
    for mode in ("serial", "concurrent"):
        bids = [b["block"] for b in blocks if b["mode"] == mode]
        bg = [r for r in rows if r["mode"] == mode and not r["warmup"]]
        rt = [r for r in rows if r["mode"] == "realtime" and str(r["block"]) in {str(b) for b in bids}]
        valid = [r for r in bg if r["http_code"] == 200 and r["curl_rc"] == 0 and r["json_valid"]]
        rt_valid = [r for r in rt if r["http_code"] == 200 and r["curl_rc"] == 0
                    and r["json_valid"] is True and r.get("finish_reason") == "stop"]
        block_s = sum(b["wall_s"] for b in blocks if b["mode"] == mode)
        t = [r["timings"] for r in bg if r["timings"]]
        pn = [x for x in t if isinstance(x.get("predicted_n"), (int, float)) and x.get("predicted_ms")]
        pp = [x for x in t if isinstance(x.get("prompt_n"), (int, float)) and x.get("prompt_ms")]
        cc = [x for x in t if isinstance(x.get("cache_n"), (int, float)) and isinstance(x.get("prompt_n"), (int, float))]
        mem = [b for b in blocks if b["mode"] == mode]
        e2e[mode] = {
            "n": len(bg),
            "errors": sum(1 for r in bg if r["http_code"] != 200 or r["curl_rc"] != 0),
            "timeouts": sum(1 for r in bg if r["curl_rc"] == 28),
            "timeout_rate": (sum(1 for r in bg if r["curl_rc"] == 28) / len(bg)) if bg else None,
            "json_valid_rate": (len(valid) / len(bg)) if bg else None,
            "decode_tps": round(sum(x["predicted_n"] for x in pn) / (sum(x["predicted_ms"] for x in pn) / 1000), 2) if pn else None,
            "prompt_tps": round(sum(x["prompt_n"] for x in pp) / (sum(x["prompt_ms"] for x in pp) / 1000), 2) if pp else None,
            "cache_ratio": round(sum(x["cache_n"] for x in cc) / sum(x["cache_n"] + x["prompt_n"] for x in cc), 3)
                           if cc and sum(x["cache_n"] + x["prompt_n"] for x in cc) else None,
            # proxy for "saved valid results per hour": lock contention and QC at save time are not included
            "valid_per_hour": round(len(valid) / block_s * 3600, 1) if block_s else None,
            "block_s": round(block_s, 1) if block_s else None,
            "realtime_n": len(rt),
            "realtime_valid_n": len(rt_valid),
            "realtime_p50": pct([r["wall_ms"] for r in rt_valid], 0.5),
            "realtime_p95": pct([r["wall_ms"] for r in rt_valid], 0.95),
            "rss_kb_before": mem[0]["mem_before"].get("rss_kb") if mem else None,
            "rss_kb_after": mem[-1]["mem_after"].get("rss_kb") if mem else None,
            "swap_used_mb_before": mem[0]["mem_before"].get("swap_used_mb") if mem else None,
            "swap_used_mb_after": mem[-1]["mem_after"].get("swap_used_mb") if mem else None,
            "foreign_busy": sum(1 for r in bg + rt if (r.get("foreign_busy_before") or 0) > 0),
        }
    summary["e2e"] = e2e
    lines.append(f"e2e (-ub {summary['server_ub']}; valid_per_hour = 保存済み有効結果/時間の代理値):")
    for mode, s in e2e.items():
        def show(v):
            return "未計測" if v is None else v
        lines.append(f"  {mode}: n={s['n']} errors={s['errors']} timeouts={s['timeouts']} "
                     f"json_valid={show(s['json_valid_rate'])} decode_tps={show(s['decode_tps'])} "
                     f"cache={show(s['cache_ratio'])} valid/h={show(s['valid_per_hour'])} "
                     f"rt p50/p95={show(s['realtime_p50'])}/{show(s['realtime_p95'])} (valid={s['realtime_valid_n']}/{s['realtime_n']}) "
                     f"rss={show(s['rss_kb_before'])}->{show(s['rss_kb_after'])} "
                     f"swap={show(s['swap_used_mb_before'])}->{show(s['swap_used_mb_after'])} "
                     f"foreign_busy={s['foreign_busy']}")
json.dump(summary, open(os.path.join(run, "summary.json"), "w"), ensure_ascii=False, indent=1)
open(os.path.join(run, "summary.txt"), "w").write("\n".join(lines) + "\n")
PYEOF
}

report() {
  "$PY" - "$OUT_ROOT" <<'PYEOF'
import glob, json, os, sys
root = sys.argv[1]
rows = []
for path in sorted(glob.glob(os.path.join(root, "*", "summary.json"))):
    try:
        s = json.load(open(path))
    except Exception:
        continue
    if s.get("phase") != "e2e":
        continue
    for mode, m in (s.get("e2e") or {}).items():
        rows.append({"run": s.get("run") or os.path.basename(os.path.dirname(path)),
                     "server_ub": s.get("server_ub"), "mode": mode, **m})
base = next((r for r in rows if r["server_ub"] == 512 and r["mode"] == "serial"), None)
# higher is better / lower is better, per recommendations.md 5
UP = ("valid_per_hour", "json_valid_rate")
DOWN = ("realtime_p95", "timeout_rate", "rss_kb_after", "swap_used_mb_after")
print("run | -ub | mode | valid/h | rt p95 (valid/n) | json_valid | timeout | rss_after | swap_after | 判定")
for r in rows:
    if base is None:
        verdict = "未計測のため判定不可 (baseline 512 serial なし)"
    elif any(type(x.get("realtime_n")) is not int or x["realtime_n"] < 1
             or type(x.get("realtime_valid_n")) is not int
             or x["realtime_valid_n"] != x["realtime_n"] for x in (base, r)):
        verdict = "新着応答の失敗・欠測により判定不可"
    elif r is base:
        verdict = "基準"
    elif any(r.get(k) is None or base.get(k) is None for k in UP + DOWN):
        verdict = "未計測のため判定不可"
    else:
        worse = [k for k in UP if r[k] < base[k]] + [k for k in DOWN if r[k] > base[k]]
        if r["valid_per_hour"] <= base["valid_per_hour"]:
            worse.append("valid_per_hour")
        verdict = "条件成立" if not worse else "不成立 (" + ", ".join(sorted(set(worse))) + ")"
    def show(v):
        return "未計測" if v is None else v
    print(f"{r['run']} | {show(r['server_ub'])} | {r['mode']} | {show(r.get('valid_per_hour'))} | "
          f"{show(r.get('realtime_p95'))} ({show(r.get('realtime_valid_n'))}/{show(r.get('realtime_n'))}) | {show(r.get('json_valid_rate'))} | "
          f"{show(r.get('timeout_rate'))} | {show(r.get('rss_kb_after'))} | {show(r.get('swap_used_mb_after'))} | {verdict}")
print()
print("採用規則 (recommendations.md 5): 保存済み有効結果/時間が baseline (-ub 512 × serial) より改善し、"
      "新着 p95・JSON 妥当性・timeout 率・RSS/swap のいずれも悪化しない候補のみ。欠測は 0 扱いしない。")
print("新着応答は基準・候補とも全件成功・JSON 妥当・正常終了が必須。失敗と欠測は判定不可。")
print("最終判断は所有者。本記録の判定は pending measurement window。")
PYEOF
}

main() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --phase) [ $# -ge 2 ] || usage; PHASE=$2; shift 2 ;;
      --i-stopped-the-workers) STOPPED=1; shift ;;
      --dry-run) DRY_RUN=1; shift ;;
      --report) REPORT=1; shift ;;
      *) usage ;;
    esac
  done
  if [ "$REPORT" = 1 ]; then
    report
    exit 0
  fi
  case "$PHASE" in bench|e2e) ;; *) usage ;; esac
  [ "$STOPPED" = 1 ] || usage
  guard_unloaded 3 "$LABELS"
  RUN_DIR=$OUT_ROOT/$(date -u +%Y%m%dT%H%M%SZ)
  mkdir -p "$RUN_DIR"
  if [ "$DRY_RUN" = 1 ]; then
    write_env; write_prompts; write_plan
    echo "dry-run: wrote $RUN_DIR/{env.json,plan.json,prompts/}"
    if [ "$PHASE" = bench ]; then
      echo "plan: $LLAMA_BENCH -m $MODEL -ngl 99 -fa 1 -ctk q4_0 -ctv q4_0 -ub <512 warmup, then $UB_ORDER> -p 512,2048,8192 -n 128 -r 5 -o json"
    else
      echo "plan: blocks serial,concurrent,serial,concurrent; $REPS reps x short/long/context on wire 1 (+ wire 2 concurrent); realtime short on wire 0 with each long/context; POST $SERVER/v1/chat/completions id_slot=<wire> max_tokens=$MAX_TOKENS timeout=$TIMEOUT s"
    fi
    exit 0
  fi
  guard_phase
  write_env; write_prompts; write_plan
  if [ "$PHASE" = bench ]; then run_bench; else run_e2e; fi
  summarize
  cat "$RUN_DIR/summary.txt"
}

main "$@"
