"""capture と shadow のスタンプ観測を件数だけで照合する読取り専用レポート。

投稿・患者 ID・本文・氏名は出さない。DB は mode=ro で開き、書き込まない。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401

_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
NOTE = "不一致0は未読保持の証明ではありません（照合できた投稿の件数だけです）。"
LAG_BUCKETS = ((3600, "lt_1h"), (6 * 3600, "1h_6h"), (86400, "6h_24h"),
               (7 * 86400, "1d_7d"), (float("inf"), "ge_7d"))


def _by_type(reactions):
    return {r["type"]: r for r in reactions}


def _lag_bucket(seconds):
    if seconds < 0:
        return "shadow_before_capture"
    return next(name for limit, name in LAG_BUCKETS if seconds < limit)


def _watch(reader, now):
    """Due watch-set targets via the ledger's own selector (read only)."""
    try:
        targets = reader.metadata_watch_targets(limit=-1, now=now)
    except sqlite3.Error:
        return {"available": False}
    checked = []
    for row in targets:
        hit = reader.db.execute(
            "SELECT checked_at FROM message_metadata WHERE message_id=? "
            "AND source='shadow'", (row["message_id"],)).fetchone()
        if hit is not None:
            checked.append(hit[0])
    return {"available": True, "due": len(targets),
            "due_never_attempted": len(targets) - len(checked),
            "oldest_shadow_check_age_s":
                round(now - min(checked)) if checked else None}


def build_report(reader, now=None) -> dict:
    """Count-only comparison of capture and shadow rows."""
    from message_metadata import _read_metadata
    now = time.time() if now is None else now
    db = reader.db
    counts = Counter()
    count_diff, self_diff = Counter(), Counter()
    failures, lag, direction = Counter(), Counter(), Counter()
    has_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                           "AND name='message_metadata'").fetchone()
    mids = [r[0] for r in db.execute(
        "SELECT DISTINCT message_id FROM message_metadata")] if has_table else []
    for mid in mids:
        cap = _read_metadata(db, mid, "capture")
        sh = _read_metadata(db, mid, "shadow")
        cap_seen = cap["reactions"] is not None
        if sh["checked_at"] is None:
            counts["shadow_not_attempted" if cap_seen
                   else "capture_without_reactions"] += 1
            continue
        if sh["last_error"]:
            counts["shadow_failed"] += 1
            raw = db.execute("SELECT last_error FROM message_metadata WHERE "
                             "message_id=? AND source='shadow'", (mid,)).fetchone()[0]
            # codes only: anything not code-shaped is never echoed
            for code in str(raw).split(","):
                failures[code if _CODE.fullmatch(code) else "metadata_error"] += 1
            continue
        if sh["reactions"] is None:
            counts["shadow_invalid" if sh["reactions_status"] == "invalid"
                   else "shadow_without_reactions"] += 1
            continue
        if not cap_seen:
            counts["shadow_only"] += 1
            continue
        lag[_lag_bucket(sh["checked_at"] - cap["reactions_observed_at"])] += 1
        c, s = _by_type(cap["reactions"]), _by_type(sh["reactions"])
        types = sorted(set(c) | set(s))
        diff_types = [t for t in types
                      if c.get(t, {}).get("count") != s.get(t, {}).get("count")]
        flag_types = [t for t in types
                      if c.get(t, {}).get("self_reacted", False)
                      != s.get(t, {}).get("self_reacted", False)]
        count_diff.update(diff_types)
        self_diff.update(flag_types)
        outcome = "mismatch" if diff_types or flag_types else "match"
        counts[outcome] += 1
        newer = ("capture_newer" if cap["checked_at"] > sh["checked_at"] else
                 "shadow_newer" if cap["checked_at"] < sh["checked_at"] else
                 "same_time")
        direction[f"{outcome}_{newer}"] += 1
    return {"as_of": round(now), "messages": len(mids),
            "outcomes": dict(sorted(counts.items())),
            "count_diff_by_type": dict(sorted(count_diff.items())),
            "self_flag_diff_by_type": dict(sorted(self_diff.items())),
            "shadow_failures_by_code": dict(sorted(failures.items())),
            "time_direction": dict(sorted(direction.items())),
            "capture_to_shadow_lag": dict(sorted(lag.items())),
            "watch": _watch(reader, now), "note": NOTE}


def render_text(rep) -> str:
    lines = [f"メタデータ照合（{rep['messages']}投稿分・件数のみ）"]
    for title, key in (("分類", "outcomes"), ("種別別の件数差", "count_diff_by_type"),
                       ("種別別の本人フラグ差", "self_flag_diff_by_type"),
                       ("shadow失敗の理由コード", "shadow_failures_by_code"),
                       ("時刻の向き", "time_direction"),
                       ("capture観測→shadow取得の経過", "capture_to_shadow_lag")):
        body = ", ".join(f"{k}={v}" for k, v in rep[key].items()) or "なし"
        lines.append(f"{title}: {body}")
    w = rep["watch"]
    lines.append("監視集合: 読取り不可" if not w["available"] else
                 f"監視集合: 期限到来{w['due']}件（未試行{w['due_never_attempted']}件）"
                 f"・shadow最古経過 {w['oldest_shadow_check_age_s']}秒")
    lines.append(rep["note"])
    return "\n".join(lines)


def main(argv=None) -> int:
    from ledger import LedgerReader
    from mcs_util import DB
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=DB)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not os.path.isfile(args.db):
        print(json.dumps({"ok": False, "error": "db_not_found"}))
        return 1
    reader = LedgerReader(args.db)
    try:
        rep = build_report(reader)
    finally:
        reader.close()
    print(json.dumps(rep, ensure_ascii=False) if args.json else render_text(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
