"""Deterministic test-file shards for parallel CI pytest processes.

Every shard process collects the same tests and computes the same plan, then
keeps only its own part. The plan balances recorded per-file seconds with
longest-processing-time-first assignment; files missing from the table weigh
the table's median, every file weighs at least MIN_SECONDS so no shard is left
empty while there are files for it, and an empty table weighs each file by its
test count.
Files listed under "split" are cut into contiguous chunks of their sorted
node ids so one heavy file cannot bound the whole run. The digests let the
runner prove that the shards together selected every collected test once.
Standard library only; loaded by path from the root conftest.py.
"""
import hashlib
import json
import math
from pathlib import Path
import statistics


# Half the 0.1 s rounding of ci/update_pytest_durations.py.
MIN_SECONDS = 0.05


def parse_spec(text):
    """Return (index, count) for a 1-based "I/N" shard spec, or raise ValueError."""
    index, sep, count = str(text).partition("/")
    if not sep or not index.isdigit() or not count.isdigit():
        raise ValueError(f"--mcs-shard must look like I/N, got {text!r}")
    index, count = int(index), int(count)
    if count < 1 or not 1 <= index <= count:
        raise ValueError(f"--mcs-shard needs 1 <= I <= N, got {text!r}")
    return index, count


def load_durations(path):
    """Return ({file: seconds}, {split files}); a missing or unreadable table is empty."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, OverflowError):
        return {}, set()
    if not isinstance(data, dict):
        return {}, set()
    files = data.get("files", {})
    split = data.get("split", [])
    files = {str(key): seconds for key, seconds in
             ((key, _seconds(value)) for key, value in files.items()) if seconds is not None} \
        if isinstance(files, dict) else {}
    split = {str(name) for name in split} if isinstance(split, list) else set()
    return files, split


def _seconds(value):
    """A finite, non-negative number of seconds, else None (inf, NaN, huge ints, text, bools)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        seconds = float(value)
    except OverflowError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def file_of(nodeid):
    return nodeid.split("::", 1)[0]


def _digest(ids):
    return hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()


def id_sum(ids):
    """Order-independent sum of per-id hashes; shard sums add up to the whole run's sum."""
    total = 0
    for nodeid in ids:
        total += int.from_bytes(hashlib.sha256(nodeid.encode()).digest(), "big")
    return total % (1 << 256)


def plan(nodeids, count, durations=None, split=()):
    """Assign every node id to one of `count` shards (0-based).

    Returns (assignment {nodeid: shard}, info) where info holds the weight
    unit, per-shard planned load and the heaviest single unit."""
    durations = durations or {}
    by_file = {}
    for nodeid in nodeids:
        by_file.setdefault(file_of(nodeid), []).append(nodeid)
    if durations:
        unit = "seconds"
        default = statistics.median(durations.values())
        # Recorded 0.0 s (fast files are rounded to 0.1 s) still needs a positive
        # weight, or LPT would stack every such file on the same lightest shard
        # and leave the others empty.
        weight = {name: max(durations.get(name, default), MIN_SECONDS) for name in by_file}
    else:
        unit = "tests"
        weight = {name: float(len(ids)) for name, ids in by_file.items()}
    target = sum(weight.values()) / count if by_file else 0.0
    units, pieces = [], {}   # units: (weight, file, chunk, chunks)
    for name in sorted(by_file):
        chunks = 1
        if name in split and target > 0:
            chunks = max(1, min(count, len(by_file[name]), math.ceil(weight[name] / target)))
        pieces[name] = chunks
        for chunk in range(chunks):
            units.append((weight[name] / chunks, name, chunk, chunks))
    units.sort(key=lambda u: (-u[0], u[1], u[2]))
    loads = [0.0] * count
    owner = {}
    for size, name, chunk, chunks in units:
        shard = min(range(count), key=lambda i: (loads[i], i))
        loads[shard] += size
        owner[(name, chunk)] = shard
    assignment = {}
    for name, ids in by_file.items():
        ordered = sorted(ids)
        for position, nodeid in enumerate(ordered):
            assignment[nodeid] = owner[(name, position * pieces[name] // len(ordered))]
    heaviest = max(units, key=lambda u: (u[0], u[1]), default=None)
    info = {
        "unit": unit,
        "files_total": len(by_file),
        "planned": [round(load, 3) for load in loads],
        "target": round(target, 3),
        "heaviest": None if heaviest is None else
        {"file": heaviest[1], "weight": round(heaviest[0], 3), "chunks": heaviest[3]},
    }
    return assignment, info


def report(nodeids, index, count, assignment, info, collect_not_passed):
    """Collection-time shard report (1-based index)."""
    mine = [nodeid for nodeid in nodeids if assignment[nodeid] == index - 1]
    return {
        "index": index,
        "count": count,
        "total": len(nodeids),
        "total_digest": _digest(nodeids),
        "total_sum": format(id_sum(nodeids), "064x"),
        "selected": len(mine),
        "selected_sum": format(id_sum(mine), "064x"),
        "files_total": info["files_total"],
        "files_selected": len({file_of(nodeid) for nodeid in mine}),
        "collect_not_passed": collect_not_passed,
        "unit": info["unit"],
        "planned": info["planned"][index - 1],
        "target": info["target"],
        "heaviest": info["heaviest"],
        "finished": False,
    }
