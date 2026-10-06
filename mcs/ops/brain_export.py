#!/usr/bin/env python3
"""Export published-snapshot summaries as markdown for external knowledge stores."""
import argparse
import json
import os
import re
import secrets
import sqlite3
import sys
import time
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from stat import S_ISREG

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every first-level subdir as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401
import mcs_stats
import mcs_signals
import mcs_view
import read_model
from export_schema import project_record
from mcs_queries import item_unverified
from drug_map import candidate_note
from rollup import current_cached_refs
import mcs_util
from mcs_util import loads_dict

HOME = Path(mcs_util.HOME)
SNAPSHOT = HOME / "data" / "snapshots" / "ledger-snapshot.db"
OUT_DIR = HOME / "data" / "exports"
# dated export copies are derived snapshots of the published read —
# they expire on a bounded retention; the original ledger/messages are
# never touched by this sweep (T15)
EXPORT_RETENTION_DAYS = 62
_JST = timezone(timedelta(hours=9))
HONESTY = ("候補・数値は公開スナップショット由来です。"
           "記録の欠如は対応の欠如を意味しません。")
FACTS_HEADER = ("| # | claim | kind | who | weight | since | source "
                "| resolved | quality | evidence | value | unit | by |")
FACTS_SEP = "| - | ----- | ---- | --- | ------ | ----- | ------ | -------- | ------- | -------- | ----- | ---- | -- |"


def _check_directory(directory: Path) -> None:
    if directory.is_symlink() or directory.exists() and not directory.is_dir():
        raise ValueError("export_directory_unsafe")


@contextmanager
def _directory_fd(root: Path, child: str, *, create: bool = False):
    # Keep the selected root and generated child stable through publication
    # and cleanup. A pathname recheck cannot close the symlink-swap window.
    if create:
        root.mkdir(parents=True, exist_ok=True)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NONBLOCK)
    try:
        fd = root_fd
        if child != ".":
            if create:
                try:
                    os.mkdir(child, mode=0o700, dir_fd=root_fd)
                except FileExistsError:
                    pass
            try:
                fd = os.open(child, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                             | os.O_NONBLOCK, dir_fd=root_fd)
            except OSError:
                raise ValueError("export_directory_unsafe") from None
        try:
            yield fd
        finally:
            if fd != root_fd:
                os.close(fd)
    finally:
        os.close(root_fd)


def _write(root: Path, rel: str, text: str) -> None:
    """Atomic rewrite — a crash mid-write must not leave a torn file."""
    dest = root / rel
    if dest.parent != root:
        _check_directory(dest.parent)
    with _directory_fd(root, str(dest.parent.relative_to(root)), create=True) as directory:
        tmp = f".{dest.name}-{secrets.token_hex(12)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, dest.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            with suppress(OSError):
                os.unlink(tmp, dir_fd=directory)


def _fm(title: str) -> str:
    # json.dumps yields a valid YAML double-quoted scalar — names containing
    # ':' or newlines must not corrupt the front-matter.
    return (f"---\ntitle: {json.dumps(title, ensure_ascii=False)}\n"
            "type: note\ntags: [mcs, export]\n---\n\n")


def _jst(timestamp: float, pattern: str) -> str:
    return datetime.fromtimestamp(timestamp, _JST).strftime(pattern)


def _banner(view) -> str:
    # deterministic for a fixed snapshot: identical input -> identical bytes
    return (f"> snapshot gen={view.meta['generation_id']} "
            f"at {_jst(view.meta['generated_at'], '%Y-%m-%d %H:%M JST')}\n"
            f"> {HONESTY}\n\n")


def _meta_md(view) -> str:
    gen_at = view.meta["generated_at"]
    return (_fm("MCS export meta") + "# MCS export meta\n\n" + _banner(view)
            + f"- snapshot generation: {view.meta['generation_id']}\n"
            + f"- snapshot generated_at: {_jst(gen_at, '%Y-%m-%dT%H:%M:%S%z')}\n"
            + "- source: published snapshot (read-only; live ledger untouched)\n")


def _count(db, sql, *params) -> int | None:
    try:
        return int(db.execute(sql, params).fetchone()[0])
    except sqlite3.Error:
        return None


def _num(value) -> str:
    return "n/a" if value is None else str(value)


def _cell(value) -> str:
    """Escape a value for a markdown table cell."""
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _health_md(view) -> str:
    db = view.db
    lines = [_fm("MCS collection health"), "# MCS collection health\n\n",
             _banner(view)]
    lines.append("## recent runs\n\n| finished | status | error |\n| --- | --- | --- |\n")
    for r in db.execute(
            "SELECT finished_at,status,error FROM runs ORDER BY run_id DESC LIMIT 10"):
        ts = _jst(r["finished_at"], "%m-%d %H:%M") \
            if r["finished_at"] else "?"
        err = (r["error"] or "")[:60]
        lines.append(f"| {ts} | {r['status']} | {err} |\n")
    pending = _count(db, "SELECT COUNT(*) FROM notify_outbox WHERE state='pending'")
    failed = _count(db, "SELECT COUNT(*) FROM notify_outbox WHERE state='failed'")
    jobs = _count(db, "SELECT COUNT(*) FROM fetch_jobs")
    lines.append(f"\n## queues\n\n- notify_outbox pending: {_num(pending)}\n"
                 f"- notify_outbox failed: {_num(failed)}\n"
                 f"- fetch_jobs: {_num(jobs)}\n")
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


def _facts_block(db, snap_ts: float, open_signals: int) -> str:
    # `since` is the SNAPSHOT's date — an unchanged snapshot yields byte-
    # identical output across days (the dated file name carries wall time).
    since = _jst(snap_ts, "%Y-%m-%d")
    rows = [FACTS_HEADER, FACTS_SEP]
    for i, (name, val) in enumerate(_metrics(db, snap_ts, open_signals), 1):
        rows.append(f"| {i} | {name} | metric | mcs | 1.0 | {since} "
                    f"| brain_export |  |  |  | {_num(val)} | count |  |")
    return "\n".join(rows) + "\n"


def _stats_md(view, today: str, open_signals: int,
              stats_results: dict) -> str:
    lines = [_fm(f"MCS stats {today}"), f"# MCS stats — {today}\n\n",
             _banner(view), "## Facts\n\n",
             _facts_block(view.db, view.meta["generated_at"],
                          open_signals)]
    for preset, res in stats_results.items():
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
        detected = _jst(at, "%Y-%m-%d") if at else "?"
        note = str(c.get("note") or "")[:120].replace("|", "\\|")
        ev = json.dumps(c.get("evidence"), ensure_ascii=False)[:120] \
            if c.get("evidence") else ""
        lines.append(f"| {_cell(typ)} | {pid} | {detected} | {note} "
                     f"| {_cell(ev)} |\n")
    if res.get("truncated"):
        lines.append(f"\n> truncated: {res['total']} total\n")
    return "".join(lines)


def _patient_md(pid: int, name: str, info: dict, roll: dict) -> str:
    title = f"MCS {name}"
    lines = [_fm(title), f"# {name}\n\n"]
    lines.append(f"- project_id: {pid}\n")
    lines.extend(f"- {k}: {info[k]}\n"
                 for k in ("project_type", "disease", "station_name")
                 if info.get(k))
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
        lines.append("\n## current medications\n\n| name | dose | last | 成分候補（未確認） |\n"
                     "| --- | --- | --- | --- |\n")
        lines.extend(f"| {_cell(m.get('name'))} | {_cell(m.get('dose') or '')} "
                     f"| {m.get('last') or ''} | {_cell(candidate_note(m.get('ref')))} |\n"
                     for m in roll["medications"])
    if roll.get("recent_symptoms"):
        lines.append("\n## recent symptoms\n\n| symptom | last |\n| --- | --- |\n")
        lines.extend(f"| {_cell(s.get('symptom'))} | {s.get('last')} |\n"
                     for s in roll["recent_symptoms"])
    # unverified requests never share the confirmed table; a missing flag
    # (rule rows) reads as confirmed, any other non-False value fails closed
    reqs, cands = [], []
    for r in roll.get("recent_requests") or []:
        (cands if item_unverified(r) else reqs).append(r)
    for title, rows in (("open-looking requests", reqs),
                        ("依頼候補（未確認）", cands)):
        if rows:
            lines.append(f"\n## {title}\n\n| at | kind | ctx |\n| --- | --- | --- |\n")
            for r in rows:
                ctx = str(r.get("ctx") or "")[:80]
                lines.append(f"| {r.get('at')} | {_cell(r.get('kind'))} | {_cell(ctx)} |\n")
    if roll.get("next_planned"):
        lines.append(f"\n## next planned\n\n{roll['next_planned']}\n")
    if roll.get("top_senders"):
        lines.append("\n## top senders\n\n"
                     + ", ".join(f"{n}({c})" for n, c in roll["top_senders"]) + "\n")
    if roll.get("possibly_deleted"):
        lines.append(f"\n> possibly_deleted: {len(roll['possibly_deleted'])} message ids\n")
    return "".join(lines)


def _records_jsonl(view, sig_res: dict, stats_results: dict,
                   model: dict) -> str:
    """The machine surface (T15): one complete JSON record per line —
    never sliced, never markdown. Every line carries the contract id and
    the exact snapshot generation it was read from, so a rotated
    snapshot is detectable instead of silently mixing generations."""
    gen = view.meta["generation_id"]
    lines = []

    def rec(type_, **kw):
        kw.update({"type": type_, "contract": read_model.CONTRACT,
                   "snapshot_generation_id": gen})
        lines.append(json.dumps(project_record(kw), ensure_ascii=False,
                                allow_nan=False))

    rec("meta", snapshot=dict(view.meta))
    rec("coverage", coverage=model["coverage"])
    for preset, res in stats_results.items():
        for name, stat in (res.get("stats") or {}).items():
            rec("stat", preset=preset, name=name, value=stat)
    for c in sig_res["items"]:
        # aggregate scope: type/project/ids only — signal 'note' text is
        # human-surface content and stays out of the machine records
        rec("signal", signal_type=c.get("type"),
            project_id=c.get("project_id"), detected_at=c.get("detected_at"),
            evidence=c.get("evidence"))
    if sig_res.get("truncated"):
        rec("signals_truncated", total=sig_res["total"])
    for a in model["attachments"] or []:
        rec("attachment", **a)
    for r in model["records"]:
        rec("message", **r)
    return "\n".join(lines) + "\n"


def _sweep_exports(out_dir: Path, now: float) -> list:
    """Bounded retention for dated export copies — only filenames this
    exporter itself generates (stats|signals/YYYY-MM-DD.*, top-level
    export-YYYY-MM-DD.jsonl); anything else in a user-chosen --out dir
    is not ours to delete."""
    cutoff = now - EXPORT_RETENTION_DAYS * 86400
    expired = []
    for sub, pattern in (
            (out_dir / "stats", r"(\d{4}-\d{2}-\d{2})\.md"),
            (out_dir / "signals", r"(\d{4}-\d{2}-\d{2})\.md"),
            (out_dir, r"export-(\d{4}-\d{2}-\d{2})\.jsonl")):
        if not sub.is_dir() or (sub != out_dir and sub.is_symlink()):
            continue
        with _directory_fd(out_dir, str(sub.relative_to(out_dir))) as directory:
            for name in os.listdir(directory):
                m = re.fullmatch(pattern, name)
                if not m or not S_ISREG(os.stat(
                        name, dir_fd=directory, follow_symlinks=False).st_mode):
                    continue
                try:
                    day = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=_JST).timestamp()
                except (ValueError, OverflowError):
                    continue
                if day >= cutoff:
                    continue
                os.unlink(name, dir_fd=directory)
                expired.append(str((sub / name).relative_to(out_dir)))
    return expired


def run(out_dir: Path, snapshot: Path) -> dict:
    view = mcs_view.View(str(snapshot))
    try:
        # Generated subdirectories must belong to this export, including
        # when no patient pages remain and only cleanup would visit them.
        for name in ("patients", "stats", "signals"):
            directory = out_dir / name
            _check_directory(directory)
        info = {r["project_id"]: dict(r) for r in view.db.execute(
            "SELECT project_id,patient_name,project_type,disease,station_name"
            " FROM patients")}
        rolls = {r["project_id"]: (r["content"], r["meta"]) for r in view.db.execute(
            "SELECT project_id,content,meta FROM artifacts "
            "WHERE kind='patient_rollup' ORDER BY artifact_id")}
        # Validate/render every latest rollup before replacing or pruning any
        # export. Corrupt input is not evidence that a patient disappeared.
        pages = {}
        for pid, (content, meta) in rolls.items():
            roll = loads_dict(content)
            if roll is None:
                raise ValueError("patient_rollup_invalid")
            roll = current_cached_refs(view.db, pid, roll, meta)
            name = (info.get(pid) or {}).get("patient_name") or f"project-{pid}"
            try:
                pages[f"p{pid}.md"] = (
                    _patient_md(pid, name, info.get(pid) or {}, roll)
                    + "\n---\n\n" + _banner(view))
            except (AttributeError, TypeError, ValueError, RecursionError):
                raise ValueError("patient_rollup_invalid") from None
        today = _jst(time.time(), "%Y-%m-%d")
        sig_res = mcs_signals.current_open(view.db, limit=200)
        model = read_model.read_model(view.db, scope="aggregate")
        _write(out_dir, "meta.md", _meta_md(view))
        _write(out_dir, "health.md", _health_md(view))
        stats_results = {
            "operational": mcs_stats.run_stats(
                view.db, view.meta["generated_at"],
                {"preset": "operational", "limit": 50}),
            "pharmacy": mcs_stats.run_stats(
                view.db, view.meta["generated_at"],
                {"preset": "pharmacy", "limit": 50})}
        stats = _stats_md(view, today, sig_res["total"], stats_results)
        _write(out_dir, "stats/latest.md", stats)
        _write(out_dir, f"stats/{today}.md", stats)
        _write(out_dir, "signals/latest.md", _signals_md(view, sig_res))
        records = _records_jsonl(view, sig_res, stats_results, model)
        _write(out_dir, "export.jsonl", records)
        _write(out_dir, f"export-{today}.jsonl", records)
        seen = set(pages)
        for filename, text in pages.items():
            _write(out_dir, f"patients/{filename}", text)
        pdir = out_dir / "patients"
        _check_directory(pdir)
        if pdir.is_dir():
            with _directory_fd(out_dir, "patients") as directory:
                for name in os.listdir(directory):
                    # Only generated regular pages belong to the exporter.
                    if name not in seen and re.fullmatch(r"p\d+\.md", name) \
                            and S_ISREG(os.stat(name, dir_fd=directory,
                                                    follow_symlinks=False).st_mode):
                        os.unlink(name, dir_fd=directory)
        expired = _sweep_exports(out_dir, time.time())
        return {"ok": True, "patients": len(seen),
                "signals": sig_res["total"],
                "snapshot_generation_id": view.meta["generation_id"],
                "contract": read_model.CONTRACT,
                "records": str(out_dir / "export.jsonl"),
                "retention": {"keep_days": EXPORT_RETENTION_DAYS,
                              "expired": expired},
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
