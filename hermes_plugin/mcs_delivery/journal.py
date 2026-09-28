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
its successor. A long-lived worker rotates to a new segment file
(``journal-<worker>~<n>.jsonl``) so its closed segments can be compacted.

Compaction (``compact``) only rewrites closed files and only drops an
attempt whose every row sits in that one file and which the caller
proves settled and no longer needed. The runner's post-restore
reconcile (mcs/notify/notify_reconcile.py) treats these rows as the
witness of effects a restored DB lost, so the caller also bounds pruning
by the oldest restorable backup.
"""
from __future__ import annotations

import json
import os
import time
from bisect import bisect_left
from collections.abc import Mapping

from .paths import atomic_write, fsync_dir

PHASES = ("claimed", "begin", "granted", "denied",
          "started", "result", "receipt")


def _path(state_dir: str, worker_id: str, segment: int = 0) -> str:
    # "~" sorts after ".", so every reader's sorted() listing keeps a
    # worker's segments in write order after its first file
    name = worker_id if not segment else f"{worker_id}~{segment:06d}"
    return os.path.join(state_dir, f"journal-{name}.jsonl")


def append(state_dir: str, worker_id: str, record: dict, *,
           segment: int = 0) -> str:
    """Append one fsync'd record. Returns the journal path."""
    path = _path(state_dir, worker_id, segment)
    row = {"v": 1, "worker_id": worker_id, "ts": time.time(), **record}
    if row.get("phase") not in PHASES:
        raise ValueError("bad_phase")
    line = json.dumps(row, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")) + "\n"
    created = not os.path.exists(path)
    with open(path, "ab") as handle:
        handle.write(line.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    if created:
        # A file fsync alone does not make its new directory entry durable.
        # Losing the journal name must not turn a sent attempt into not_sent.
        fsync_dir(state_dir)
    return path


def _scan_file(path: str, out: dict) -> None:
    try:
        with open(path, "rb") as handle:
            # line by line: rows read before an I/O error still count
            for raw in handle:
                _parse_rows(raw.rstrip(b"\n"), out)
    except OSError:
        return


def _names(state_dir: str) -> list[str]:
    return sorted(n for n in os.listdir(state_dir)
                  if n.startswith("journal-") and n.endswith(".jsonl"))


def compact(state_dir: str, *, active: str, file_ok, prunable) -> int:
    """Drop settled attempts from closed journal files. Returns rows dropped.

    ``active`` is the caller's live segment path, never touched. A file
    is rewritten only when every line parses (a torn/corrupt line keeps
    the file as-is: restore reconcile treats it as tainted evidence) and
    ``file_ok(rows)`` accepts it. An attempt is dropped only when all of
    its rows are in that file and ``prunable(attempt_id, rows)`` holds.
    Each rewrite is atomic (tmp + fsync + rename + dir fsync), so a crash
    leaves either the old or the new file, never half an attempt.
    """
    try:
        names = _names(state_dir)
    except OSError:
        return 0
    files: dict[str, list] = {}
    homes: dict[str, set] = {}
    for name in names:
        path = os.path.join(state_dir, name)
        lines, clean = [], True
        try:
            with open(path, "rb") as handle:
                for raw in handle:
                    if not raw.strip():
                        continue
                    try:
                        row = json.loads(raw)
                    except (ValueError, RecursionError):
                        row = None
                    aid = row.get("attempt_id") if isinstance(row, dict) \
                        else None
                    if not isinstance(aid, str) or not aid \
                            or not raw.endswith(b"\n"):
                        clean = False
                        continue
                    homes.setdefault(aid, set()).add(name)
                    lines.append((raw, row, aid))
        except OSError:
            clean = False
        if clean and path != active:
            files[name] = lines
    dropped = 0
    for name, lines in files.items():
        if not lines or not file_ok([row for _, row, _ in lines]):
            continue
        by_aid: dict[str, list] = {}
        for _, row, aid in lines:
            by_aid.setdefault(aid, []).append(row)
        gone = {aid for aid, rows in by_aid.items()
                if homes.get(aid) == {name} and prunable(aid, rows)}
        if not gone:
            continue
        keep = [raw for raw, _, aid in lines if aid not in gone]
        path = os.path.join(state_dir, name)
        try:
            if keep:
                atomic_write(path, b"".join(keep), tmp_prefix=".journal-",
                             mode=0o600)
            else:
                os.unlink(path)
                fsync_dir(state_dir)
        except OSError:
            continue
        dropped += len(lines) - len(keep)
    return dropped


def scan(state_dir: str) -> dict[str, list[dict]]:
    """attempt_id -> ordered records, across every worker journal."""
    out: dict[str, list[dict]] = {}
    try:
        names = _names(state_dir)
    except OSError:
        return out
    for name in names:
        _scan_file(os.path.join(state_dir, name), out)
    return out


def _parse_rows(data: bytes, out: dict) -> None:
    for raw in data.split(b"\n"):
        try:
            row = json.loads(raw)
        except ValueError:
            continue  # torn tail line — earlier rows still count
        if not isinstance(row, dict):
            continue
        aid = row.get("attempt_id")
        if isinstance(aid, str):
            out.setdefault(aid, []).append(row)


class _View(Mapping):
    """scan()'s result, assembled lazily from per-file indexes: rows of
    an attempt in sorted-file order, committed lines before the torn
    tail — the same lists a full scan would return. A snapshot: later
    refreshes append to the shared per-file lists, so each file keeps
    only the first ``n`` committed rows it had when the view was made."""

    def __init__(self, files: list) -> None:
        # [(committed rows, aid -> ascending row indexes, n, tail index)]
        self._files = files

    def __getitem__(self, aid: str) -> list[dict]:
        rows = []
        for done, index, n, tail in self._files:
            idx = index.get(aid, ())
            rows += [done[i] for i in idx[:bisect_left(idx, n)]]
            rows += tail.get(aid, ())
        if not rows:
            raise KeyError(aid)
        return rows

    def __iter__(self):
        seen: dict[str, None] = {}
        for _done, index, n, tail in self._files:
            for aid, idx in index.items():
                if idx[0] >= n:
                    break        # first-seen order: the rest came later
                seen[aid] = None
            seen.update(dict.fromkeys(tail))
        return iter(seen)

    def __len__(self) -> int:
        return sum(1 for _ in self)


class ScanCache:
    """Incremental ``scan``: each refresh reads only the bytes appended
    since the previous one, so a worker re-reading the journal once per
    card pays O(new rows), not O(journal).

    A file is re-read from the start when it is new, its inode changed
    (compact's atomic rewrite), it shrank, or the last committed line no
    longer sits where it was read (a same-name file recreated after an
    unlink). Only newline-terminated lines are committed; the torn tail
    is re-parsed on every refresh, exactly as a full scan would see it.
    Any OSError drops the cache and returns a full ``scan``. Not
    thread-safe: one refresh at a time; a returned view stays a valid
    snapshot across later refreshes.
    """

    def __init__(self, state_dir: str) -> None:
        self._dir = state_dir
        # name -> [ino key, committed offset, last committed line,
        #          committed rows, aid -> row indexes, tail index]
        self._files: dict[str, list] = {}

    def invalidate(self) -> None:
        self._files = {}

    def refresh(self) -> Mapping:
        try:
            return self._refresh()
        except OSError:
            self._files = {}
            return scan(self._dir)

    def _refresh(self) -> Mapping:
        files: dict[str, list] = {}
        for name in _names(self._dir):
            try:
                handle = open(os.path.join(self._dir, name), "rb")
            except FileNotFoundError:
                continue           # compacted away between list and open
            with handle:
                st = os.fstat(handle.fileno())
                key = (st.st_dev, st.st_ino)
                ent = self._files.get(name)
                if ent is not None and (ent[0] != key
                                        or st.st_size < ent[1]):
                    ent = None
                if ent is not None and ent[2]:
                    handle.seek(ent[1] - len(ent[2]))
                    if handle.read(len(ent[2])) != ent[2]:
                        ent = None
                if ent is None:
                    ent = [key, 0, b"", [], {}, {}]
                handle.seek(ent[1])
                data = handle.read()
            cut = data.rfind(b"\n") + 1
            if cut:
                new: dict = {}
                _parse_rows(data[:cut], new)
                done, index = ent[3], ent[4]
                # row order within the file is only needed per attempt
                for aid, rows in new.items():
                    index.setdefault(aid, []).extend(
                        range(len(done), len(done) + len(rows)))
                    done += rows
                ent[1] += cut
                ent[2] = data[data.rfind(b"\n", 0, cut - 1) + 1:cut]
            ent[5] = {}
            _parse_rows(data[cut:], ent[5])
            files[name] = ent
        self._files = files
        return _View([(ent[3], ent[4], len(ent[3]), ent[5])
                      for ent in files.values()])


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
        if "result" in phases or "denied" in phases or "receipt" in phases:
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
