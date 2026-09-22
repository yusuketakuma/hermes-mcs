#!/usr/bin/env python3
"""Export published-snapshot summaries as markdown for external knowledge stores."""
import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcs_stats
import mcs_signals
import mcs_view

HOME = Path(os.path.expanduser("~/.mcs"))
SNAPSHOT = HOME / "data" / "snapshots" / "ledger-snapshot.db"
OUT_DIR = HOME / "data" / "exports"
HONESTY = ("候補・数値は公開スナップショット由来です。"
           "記録の欠如は対応の欠如を意味しません。")
FACTS_HEADER = ("| # | claim | kind | who | weight | since | source "
                "| resolved | quality | evidence | value | unit | by |")
FACTS_SEP = "| - | ----- | ---- | --- | ------ | ----- | ------ | -------- | ------- | -------- | ----- | ---- | -- |"


def _write(root: Path, rel: str, text: str) -> None:
    """Atomic rewrite — a crash mid-write must not leave a torn file."""
    dest = root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, dest)


def _fm(title: str) -> str:
    return f"---\ntitle: {title}\ntype: note\ntags: [mcs, export]\n---\n\n"


def _banner(view) -> str:
    # deterministic for a fixed snapshot: identical input -> identical bytes
    return (f"> snapshot gen={view.meta['generation_id']} "
            f"at {time.strftime('%Y-%m-%d %H:%M JST', time.localtime(view.meta['generated_at']))}\n"
            f"> {HONESTY}\n\n")


def _meta_md(view) -> str:
    gen_at = view.meta["generated_at"]
    return (_fm("MCS export meta") + "# MCS export meta\n\n" + _banner(view)
            + f"- snapshot generation: {view.meta['generation_id']}\n"
            + f"- snapshot generated_at: {time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(gen_at))}\n"
            + "- source: published snapshot (read-only; live ledger untouched)\n")


def _count(db, sql, *params) -> int:
    try:
        return int(db.execute(sql, params).fetchone()[0])
    except sqlite3.Error:
        return -1


def _health_md(view) -> str:
    db = view.db
    lines = [_fm("MCS collection health"), "# MCS collection health\n\n",
             _banner(view)]
    lines.append("## recent runs\n\n| finished | status | error |\n| --- | --- | --- |\n")
    for r in db.execute(
            "SELECT finished_at,status,error FROM runs ORDER BY run_id DESC LIMIT 10"):
        ts = time.strftime("%m-%d %H:%M", time.localtime(r["finished_at"])) \
            if r["finished_at"] else "?"
        err = (r["error"] or "")[:60]
        lines.append(f"| {ts} | {r['status']} | {err} |\n")
    pending = _count(db, "SELECT COUNT(*) FROM notify_outbox WHERE state='pending'")
    failed = _count(db, "SELECT COUNT(*) FROM notify_outbox WHERE state='failed'")
    jobs = _count(db, "SELECT COUNT(*) FROM fetch_jobs")
    lines.append(f"\n## queues\n\n- notify_outbox pending: {pending}\n"
                 f"- notify_outbox failed: {failed}\n- fetch_jobs: {jobs}\n")
    return "".join(lines)


def _metrics(db, snap_ts: float, open_signals: int) -> list[tuple]:
    # windows are snapshot-relative — output stays deterministic per snapshot
    total = _count(db, "SELECT COUNT(*) FROM messages")
    day = _count(db, "SELECT COUNT(*) FROM messages WHERE posted_at_ts>=?",
                 snap_ts - 86400)
    week = _count(db, "SELECT COUNT(*) FROM messages WHERE posted_at_ts>=?",
                  snap_ts - 7 * 86400)
    pats = _count(db, "SELECT COUNT(*) FROM patients")
    return [("mcs.patients.total", pats), ("mcs.messages.total", total),
            ("mcs.messages.24h", day), ("mcs.messages.7d", week),
            ("mcs.signals.open", open_signals)]


def _facts_block(db, snap_ts: float, today: str, open_signals: int) -> str:
    rows = [FACTS_HEADER, FACTS_SEP]
    for i, (name, val) in enumerate(_metrics(db, snap_ts, open_signals), 1):
        rows.append(f"| {i} | {name} | metric | mcs | 1.0 | {today} "
                    f"| brain_export |  |  |  | {val} | count |  |")
    return "\n".join(rows) + "\n"


def _stats_md(view, today: str, open_signals: int) -> str:
    args = {"preset": "operational", "limit": 50}
    result = mcs_stats.run_stats(view.db, view.meta["generated_at"], args)
    args2 = {"preset": "pharmacy", "limit": 50}
    result2 = mcs_stats.run_stats(view.db, view.meta["generated_at"], args2)
    lines = [_fm(f"MCS stats {today}"), f"# MCS stats — {today}\n\n",
             _banner(view), "## Facts\n\n",
             _facts_block(view.db, view.meta["generated_at"], today,
                          open_signals)]
    for preset, res in (("operational", result), ("pharmacy", result2)):
        lines.append(f"\n## preset: {preset}\n\n")
        for name, stat in (res.get("stats") or {}).items():
            status = stat.get("status") if isinstance(stat, dict) else "?"
            lines.append(f"### {name} ({status})\n\n```json\n"
                         + json.dumps(stat, ensure_ascii=False,
                                      allow_nan=False)[:4000]
                         + "\n```\n")
    return "".join(lines)


def _signals_md(view, res: dict) -> str:
    cands = res["items"]
    lines = [_fm("MCS review signals"), "# MCS review-candidate signals\n\n",
             _banner(view)]
    if not cands:
        lines.append("open候補なし\n")
        return "".join(lines)
    lines.append("| type | project | detected | note | evidence |\n"
                 "| --- | --- | --- | --- | --- |\n")
    for c in cands:
        typ = c.get("type") or "?"
        pid = c.get("project_id", "?")
        at = c.get("detected_at")
        detected = time.strftime("%Y-%m-%d", time.localtime(at)) if at else "?"
        note = str(c.get("note") or "")[:120].replace("|", "\\|")
        ev = json.dumps(c.get("evidence"), ensure_ascii=False)[:120] \
            if c.get("evidence") else ""
        lines.append(f"| {typ} | {pid} | {detected} | {note} | {ev} |\n")
    if res.get("truncated"):
        lines.append(f"\n> truncated: {res['total']} total\n")
    return "".join(lines)


def _patient_md(pid: int, name: str, info: dict, roll: dict) -> str:
    title = f"MCS {name}"
    lines = [_fm(title), f"# {name}\n\n"]
    lines.append(f"- project_id: {pid}\n")
    for k in ("project_type", "disease", "station_name"):
        if info.get(k):
            lines.append(f"- {k}: {info[k]}\n")
    lines.append(f"- last_activity: {roll.get('last_activity', '?')}\n"
                 f"- messages: {roll.get('msg_count', 0)} "
                 f"(replies {roll.get('reply_count', 0)})\n")
    if roll.get("summary"):
        s = roll["summary"]
        lines.append(f"\n## summary ({s.get('at', '?')})\n\n{s.get('text', '')}\n")
    vit = roll.get("latest_vitals")
    if vit:
        rows = {k: v for k, v in vit.items() if k != "at"}
        lines.append(f"\n## latest vitals ({vit.get('at')})\n\n"
                     + "\n".join(f"- {k}: {v}" for k, v in rows.items()) + "\n")
    if roll.get("current_med_period"):
        lines.append("\n## current med period\n\n```json\n"
                     + json.dumps(roll["current_med_period"],
                                  ensure_ascii=False)[:1000] + "\n```\n")
    if roll.get("medications"):
        lines.append("\n## current medications\n\n| name | dose | last |\n| --- | --- | --- |\n")
        for m in roll["medications"]:
            lines.append(f"| {m.get('name')} | {m.get('dose') or ''} | {m.get('last') or ''} |\n")
    if roll.get("recent_symptoms"):
        lines.append("\n## recent symptoms\n\n| symptom | last |\n| --- | --- |\n")
        for s in roll["recent_symptoms"]:
            lines.append(f"| {s.get('symptom')} | {s.get('last')} |\n")
    if roll.get("recent_requests"):
        lines.append("\n## open-looking requests\n\n| at | kind | ctx |\n| --- | --- | --- |\n")
        for r in roll["recent_requests"]:
            ctx = str(r.get("ctx") or "")[:80]
            lines.append(f"| {r.get('at')} | {r.get('kind')} | {ctx} |\n")
    if roll.get("next_planned"):
        lines.append(f"\n## next planned\n\n{roll['next_planned']}\n")
    if roll.get("top_senders"):
        lines.append("\n## top senders\n\n"
                     + ", ".join(f"{n}({c})" for n, c in roll["top_senders"]) + "\n")
    if roll.get("possibly_deleted"):
        lines.append(f"\n> possibly_deleted: {len(roll['possibly_deleted'])} message ids\n")
    return "".join(lines)


def run(out_dir: Path, snapshot: Path) -> dict:
    view = mcs_view.View(str(snapshot))
    try:
        today = time.strftime("%Y-%m-%d")
        sig_res = mcs_signals.current_open(view.db, limit=200)
        _write(out_dir, "meta.md", _meta_md(view))
        _write(out_dir, "health.md", _health_md(view))
        stats = _stats_md(view, today, sig_res["total"])
        _write(out_dir, "stats/latest.md", stats)
        _write(out_dir, f"stats/{today}.md", stats)
        _write(out_dir, "signals/latest.md", _signals_md(view, sig_res))
        info = {r["project_id"]: dict(r) for r in view.db.execute(
            "SELECT project_id,patient_name,project_type,disease,station_name"
            " FROM patients")}
        rolls = {}
        for row in view.db.execute(
                "SELECT project_id,content FROM artifacts "
                "WHERE kind='patient_rollup' ORDER BY artifact_id"):
            try:
                rolls[row["project_id"]] = json.loads(row["content"])
            except (json.JSONDecodeError, TypeError):
                continue
        seen = set()
        for pid, roll in rolls.items():
            name = (info.get(pid) or {}).get("patient_name") or f"project-{pid}"
            _write(out_dir, f"patients/p{pid}.md",
                   _patient_md(pid, name, info.get(pid) or {}, roll)
                   + "\n---\n\n" + _banner(view))
            seen.add(f"p{pid}.md")
        pdir = out_dir / "patients"
        if pdir.is_dir():
            for stale in pdir.glob("p*.md"):
                if stale.name not in seen:
                    stale.unlink()
        return {"ok": True, "patients": len(seen),
                "signals": sig_res["total"],
                "out": str(out_dir)}
    finally:
        view.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--snapshot", default=str(SNAPSHOT))
    args = ap.parse_args(argv)
    try:
        result = run(Path(args.out), Path(args.snapshot))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, OSError, sqlite3.Error, RecursionError) as e:
        code = str(e) if type(e) is ValueError and \
            str(e).replace("_", "").isalnum() else type(e).__name__
        print(json.dumps({"ok": False, "error": code}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
