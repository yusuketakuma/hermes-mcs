"""Append-only delivery journal — the crash-safety witness.

One JSON line per phase transition, each append fsync'd. The ordering
contract the whole recovery story hangs on:

- ``started`` is written and fsync'd BEFORE the HTTP request fires, so
  a crash between grant and send leaves a record saying "grant held,
  send began" — never ambiguous with "never sent".
- ``result`` is written and fsync'd the moment Discord's answer is
  known, BEFORE the receipt file is published.
- An attempt with ``granted``/``begin`` but no ``started`` provably
  never started HTTP — eligible for a factual ``not_sent`` receipt.
- An attempt with ``started`` but no ``result`` is honestly ``unknown``
  — never resent, never silently dropped; it goes to reconcile.

Per-worker files keep a crashed worker's tail intact for inspection by
its successor.
"""
from __future__ import annotations

import json
import os
import time

PHASES = ("claimed", "begin", "granted", "denied",
          "started", "result", "receipt")


def _path(state_dir: str, worker_id: str) -> str:
    return os.path.join(state_dir, f"journal-{worker_id}.jsonl")


def append(state_dir: str, worker_id: str, record: dict) -> str:
    """Append one fsync'd record. Returns the journal path."""
    path = _path(state_dir, worker_id)
    row = {"v": 1, "worker_id": worker_id, "ts": time.time(), **record}
    if row.get("phase") not in PHASES:
        raise ValueError("bad_phase")
    line = json.dumps(row, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")) + "\n"
    with open(path, "ab") as handle:
        handle.write(line.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    return path


def _scan_file(path: str, out: dict) -> None:
    try:
        with open(path, "rb") as handle:
            for raw in handle:
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue  # torn tail line — earlier rows still count
                if not isinstance(row, dict):
                    continue
                aid = row.get("attempt_id")
                if isinstance(aid, str):
                    out.setdefault(aid, []).append(row)
    except OSError:
        return


def scan(state_dir: str) -> dict[str, list[dict]]:
    """attempt_id -> ordered records, across every worker journal."""
    out: dict[str, list[dict]] = {}
    try:
        names = sorted(n for n in os.listdir(state_dir)
                       if n.startswith("journal-")
                       and n.endswith(".jsonl"))
    except OSError:
        return out
    for name in names:
        _scan_file(os.path.join(state_dir, name), out)
    return out


def unfinished(records: dict[str, list[dict]]) -> dict[str, dict]:
    """Classify attempts that never reached a factual result.

    Returns attempt_id -> {"phase": last_phase, "record": last row} for
    attempts whose story is incomplete:
    - ``pre_http``: granted/begin/claimed without ``started`` — the send
      provably never began (journal integrity holds), eligible for
      ``not_sent`` reporting.
    - ``post_http``: ``started`` without ``result`` — honestly unknown;
      reconcile, never resend.
    """
    out: dict[str, dict] = {}
    for aid, rows in records.items():
        phases = {r.get("phase") for r in rows}
        if "result" in phases or "denied" in phases:
            continue
        last = rows[-1]
        kind = "post_http" if "started" in phases else "pre_http"
        out[aid] = {"phase": kind, "record": last, "rows": rows}
    return out


def unreported(records: dict[str, list[dict]]) -> dict[str, dict]:
    """Attempts with a recorded ``result`` but no ``receipt`` record —
    the journal proves the outcome; the runner may never have seen it.
    Re-publishing the same factual receipt is idempotent runner-side."""
    out: dict[str, dict] = {}
    for aid, rows in records.items():
        phases = {r.get("phase") for r in rows}
        if "result" in phases and "receipt" not in phases:
            result_row = next(r for r in reversed(rows)
                              if r.get("phase") == "result")
            out[aid] = {"record": result_row, "rows": rows}
    return out
