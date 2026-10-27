#!/usr/bin/env python3
"""Pin a fixed pharmacy-cue audit cohort and compare it after one extraction generation change.

  audit_cohort.py pin --snapshot <published.db> --out <data/...jsonl> [--since --until --project]
  audit_cohort.py compare --snapshot <published.db> --pin <data/...jsonl> [--labels <data/...jsonl>]

The rows come from extraction_audit.iter_audit_rows (the shared reader
selection and lexical capture), never re-implemented here. A pin keeps only
IDs, hashes, reader, version, cue capture booleans and the output digest in a
private 0600 JSONL file under the MCS data directory; stdout carries ASCII
aggregates only. Lexical capture is a proxy: the final quality gate also
needs paired semantic labels bound to the exact before/after outputs, so a
proxy improvement alone never passes.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import re
import sys
from collections import Counter
from fractions import Fraction
from pathlib import Path

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every module dir (any depth) as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
import _mcs_path  # noqa: F401,E402
import inference_version  # noqa: E402
from safe_io import atomic_write, read_regular_nofollow  # noqa: E402

SCHEMA = "mcs-audit-cohort/v1"
# The plan migrates generation 7 to 8 once; no general version setting.
BASELINE_VERSION = 7
TARGET_VERSION = 8
READERS = frozenset({"reader_extract", "reader_projection", "no_output"})
QUALITIES = frozenset({"complete", "attention", "no_output"})
LABEL_SOURCES = frozenset({"internal_agent", "human"})
LABEL_SAMPLE = 50
# A conservative engineering gate for this cohort, not a universal statistic.
MIN_RETENTION = Fraction(4, 5)
EXCLUSIONS = ("deleted", "hash_changed", "reader_changed", "not_yet_reanalysed", "other_version")
_DIGEST = re.compile(r"[0-9a-f]{64}")


class CohortError(ValueError):
    """An ASCII reason code; never carries row or label content."""


def _wilson(missed: int, n: int) -> dict:
    from stats_math import wilson_interval
    return wilson_interval(missed, n)


def _pin_row(row: dict, cue_codes: frozenset) -> dict | None:
    """Select the allowlisted fields of one audit row; None when it has no cue."""
    try:
        mid, pid, digest = row["message_id"], row["project_id"], row.get("output_digest")
        classes, version = row["classes"], row.get("extract_version")
        ok = (type(mid) is int and type(pid) is int and isinstance(row["content_hash"], str)
              and row["reader"] in READERS and (version is None or type(version) is int)
              and isinstance(classes, dict) and set(classes) <= cue_codes
              and all(type(v) is bool for v in classes.values())
              and (digest is None or (isinstance(digest, str) and _DIGEST.fullmatch(digest))))
    except (KeyError, TypeError):
        ok = False
    if not ok:
        raise CohortError("bad_audit_row")
    if not classes:
        return None
    return {"message_id": mid, "project_id": pid, "content_hash": row["content_hash"],
            "reader": row["reader"], "extract_version": version, "classes": dict(classes),
            "output_digest": digest}


def build_pin(rows, definition: str, cue_codes, scope: dict, snapshot: dict) -> dict:
    """Pin every cue post: generation-7 extract rows gate, the rest is inventory."""
    cue_codes = frozenset(cue_codes)
    pinned, seen = [], set()
    for row in rows:
        kept = _pin_row(row, cue_codes)
        if kept is None:
            continue
        held, quality = row.get('held_prior'), row.get('quality')
        unknown_hold, unknown_newer = row.get('hold_unknown'), row.get('newer_artifact_unknown')
        if (type(held) is not bool or quality not in QUALITIES
                or type(unknown_hold) is not bool or type(unknown_newer) is not bool):
            raise CohortError('bad_audit_row')
        if (unknown_hold or unknown_newer
                or kept['reader'] == 'no_output' and row.get('reason') == 'unknown'
                or kept['reader'] == 'reader_extract' and kept['extract_version'] == BASELINE_VERSION
                   and (held or quality != 'complete')):
            # Do not drop a hard case from the fixed population or turn it
            # into a successful baseline. No pin is written on this path.
            raise CohortError('baseline_uncertain')
        if kept["message_id"] in seen:
            raise CohortError("duplicate_message")
        seen.add(kept["message_id"])
        kept["set"] = ("acceptance" if kept["reader"] == "reader_extract"
                       and kept["extract_version"] == BASELINE_VERSION else "inventory")
        pinned.append(kept)
    header = {"schema": SCHEMA, "definition": definition,
              "baseline_version": BASELINE_VERSION, "target_version": TARGET_VERSION,
              "scope": {key: scope.get(key) for key in ("since", "until", "as_of", "project_id")},
              "snapshot": {key: snapshot.get(key) for key in ("generation_id", "generated_at")}}
    return {"header": header, "rows": pinned}


def _now_rows(rows, cue_codes) -> dict:
    now = {}
    for row in rows:
        kept = _pin_row(row, frozenset(cue_codes))
        if kept is None:
            continue
        held, quality, reason = row.get("held_prior"), row.get("quality"), row.get("reason")
        unknown_hold, unknown_newer = row.get('hold_unknown'), row.get('newer_artifact_unknown')
        if (type(held) is not bool or quality not in QUALITIES
                or not isinstance(reason, str)
                or type(unknown_hold) is not bool or type(unknown_newer) is not bool):
            raise CohortError("bad_audit_row")
        kept.update(held_prior=held, quality=quality, reason=reason,
                    hold_unknown=unknown_hold, newer_artifact_unknown=unknown_newer)
        if kept["message_id"] in now:
            raise CohortError("duplicate_message")
        now[kept["message_id"]] = kept
    return now


def _gate(n: int, before: int, after: int) -> dict:
    """Miss counts on one fixed membership; a worse direction always fails."""
    result = {"n": n, "before_missed": before, "after_missed": after,
              "before_ci": _wilson(before, n), "after_ci": _wilson(after, n)}
    if n == 0:
        result["gate"] = "not_evaluable"
    elif after > before:
        result["gate"] = "fail"
    elif before == 0:
        result["gate"] = "pass"  # nothing was missed and nothing is
    else:
        improved = after < before and result["after_ci"]["high"] < result["before_ci"]["low"]
        result["gate"] = "pass" if improved else "fail"
    result["direction"] = ("improved" if after < before else "worse" if after > before else "unchanged")
    return result


def _overall(gates) -> str:
    gates = list(gates)
    if not gates or any(g == "not_evaluable" for g in gates):
        return "not_evaluable"
    return "pass" if all(g == "pass" for g in gates) else "fail"


def _retained(kept: int, baseline: int) -> bool:
    # integer comparison: 1999 of 2500 is below 0.8 even though it rounds to it
    return baseline > 0 and kept * MIN_RETENTION.denominator >= baseline * MIN_RETENTION.numerator


def _semantic(cohort: list[tuple[dict, dict | None]], labels, needed_cap: int,
              baseline: Counter) -> dict:
    """Paired labels bound to the exact before/after outputs of the same rows."""
    if labels is None:
        return {"gate": "not_evaluable", "reason": "labels_missing"}
    if not baseline:
        return {"gate": "not_evaluable", "reason": "empty_baseline"}
    by_key = {(pin["message_id"], code): (pin, now) for pin, now in cohort for code in pin["classes"]}
    valid, stale, sources = {}, 0, {source: 0 for source in sorted(LABEL_SOURCES)}
    for label in labels:
        key = (label.get("message_id"), label.get("class")) if isinstance(label, dict) else None
        pair = by_key.get(key)
        pin, now = pair if pair else (None, None)
        if (pin is None or label.get("content_hash") != pin["content_hash"]
                or label.get("before_output_digest") != pin["output_digest"]
                # a failed or held attempt has no after output: its label pins none
                or label.get("after_output_digest") != (now["output_digest"] if now else None)
                or type(label.get("before_captured")) is not bool
                or type(label.get("after_captured")) is not bool
                or label.get("label_source") not in LABEL_SOURCES or key in valid):
            stale += 1
            continue
        valid[key] = {**label, "after_captured": bool(now) and label["after_captured"]}
        sources[label["label_source"]] += 1
    classes, gates = {}, []
    for code in sorted(baseline):
        # the pinned baseline sets the sample, so an excluded rare class cannot vanish
        population = [key for key in by_key if key[1] == code]
        needed = min(needed_cap, baseline[code])
        got = [valid[key] for key in population if key in valid]
        if len(got) < needed:
            classes[code] = {"gate": "not_evaluable", "needed": needed, "labelled": len(got)}
        else:
            classes[code] = {**_gate(len(got), sum(not x["before_captured"] for x in got),
                                     sum(not x["after_captured"] for x in got)),
                             "needed": needed, "labelled": len(got)}
        gates.append(classes[code]["gate"])
    result = {"gate": "not_evaluable" if stale else _overall(gates), "classes": classes,
              "stale_labels": stale, "label_sources": sources,
              "human_review": "separate"}  # internal labels are not the G6 human labels
    if stale:
        result["reason"] = "stale_or_incomplete_labels"
    return result


def _check_pin_rows(rows, cue_codes) -> None:
    """A pinned row must be exactly what build_pin writes, or nothing is counted."""
    seen = set()
    for row in rows:
        try:
            kept = _pin_row(row, frozenset(cue_codes)) if isinstance(row, dict) else None
        except CohortError:
            kept = None
        acceptance = (kept is not None and kept["reader"] == "reader_extract"
                      and kept["extract_version"] == BASELINE_VERSION)
        if (kept is None or {**kept, "set": row.get("set")} != row
                or row["set"] != ("acceptance" if acceptance else "inventory")
                or kept["message_id"] in seen):
            raise CohortError("pin_schema")
        seen.add(kept["message_id"])


def compare(pin: dict, rows, definition: str, cue_codes, labels=None) -> dict:
    """Same IDs/hashes on the pinned membership; failures stay misses, exclusions add up."""
    header = pin["header"]
    if (header.get("schema") != SCHEMA or header.get("baseline_version") != BASELINE_VERSION
            or header.get("target_version") != TARGET_VERSION):
        raise CohortError("pin_schema")
    if header.get("definition") != definition:
        raise CohortError("definition_changed")
    _check_pin_rows(pin["rows"], cue_codes)
    now = _now_rows(rows, cue_codes)
    excluded = dict.fromkeys(EXCLUSIONS, 0)
    failures = {"no_output": 0, "held_prior": 0, "attention": 0}
    inventory = {"reanalysed": 0, "reanalysis_failed": 0, "not_reanalysed": 0, "projection": 0,
                 "changed": 0, "after_missed": 0}
    cohort, pinned = [], {"acceptance": 0, "inventory": 0}
    unverified_members = 0
    for row in pin["rows"]:
        pinned[row["set"]] += 1
        cur = now.get(row["message_id"])
        if row["set"] == "inventory":
            if cur is None or cur["content_hash"] != row["content_hash"]:
                inventory["changed"] += 1
            elif (cur['hold_unknown'] or cur['newer_artifact_unknown']
                  or cur['reader'] == 'no_output' and cur['reason'] == 'unknown'):
                inventory['reanalysis_failed'] += 1
                inventory['after_missed'] += len(row['classes'])
            elif cur["reader"] == "reader_projection":
                inventory["projection"] += 1
            elif cur["reader"] == "no_output" or cur["held_prior"] or (
                    cur["extract_version"] == TARGET_VERSION and cur["quality"] != "complete"):
                # an unconfirmed attempt is no capture, as in the gated rows
                inventory["reanalysis_failed"] += 1
                inventory["after_missed"] += len(row["classes"])
            elif cur["extract_version"] == TARGET_VERSION:
                inventory["reanalysed"] += 1
                inventory["after_missed"] += sum(not cur["classes"].get(code, False)
                                                 for code in row["classes"])
            else:
                inventory["not_reanalysed"] += 1
            continue
        if cur is None:
            excluded["deleted"] += 1
        elif cur["content_hash"] != row["content_hash"]:
            excluded["hash_changed"] += 1
        elif (cur['hold_unknown'] or cur['newer_artifact_unknown']
              or cur['reader'] == 'no_output' and cur['reason'] == 'unknown'):
            failures['no_output' if cur['reader'] == 'no_output' else 'attention'] += 1
            unverified_members += 1
            cohort.append((row, None))
        elif cur["reader"] == "reader_projection":
            excluded["reader_changed"] += 1
        elif cur["reader"] == "no_output":
            failures["no_output"] += 1
            cohort.append((row, None))
        elif cur["held_prior"]:
            failures["held_prior"] += 1  # a rejected attempt keeping the prior is no success
            cohort.append((row, None))
        elif cur["extract_version"] == BASELINE_VERSION:
            excluded["not_yet_reanalysed"] += 1
        elif cur["extract_version"] != TARGET_VERSION:
            excluded["other_version"] += 1
        elif cur["quality"] != "complete":
            failures["attention"] += 1
            cohort.append((row, None))
        else:
            cohort.append((row, cur))
    if pinned["acceptance"] != len(cohort) + sum(excluded.values()) or pinned["inventory"] != sum(
            v for k, v in inventory.items() if k != "after_missed"):
        raise CohortError("reconciliation")
    pinned_ids = {row["message_id"] for row in pin["rows"]}
    baseline = Counter(code for row in pin["rows"] if row["set"] == "acceptance"
                       for code in row["classes"])
    classes = {}
    for code in sorted(baseline):
        members = [(row, cur) for row, cur in cohort if code in row["classes"]]
        classes[code] = _gate(len(members), sum(not row["classes"][code] for row, _ in members),
                              sum(cur is None or not cur["classes"].get(code, False)
                                  for _, cur in members))
        classes[code].update(baseline=baseline[code],
                             retention=round(len(members) / baseline[code], 3))
        if not _retained(len(members), baseline[code]):
            classes[code]["gate"] = "not_evaluable"  # each class keeps its own population
    n = len(cohort)
    retention = round(n / pinned["acceptance"], 3) if pinned["acceptance"] else None
    lexical = _overall(c["gate"] for c in classes.values())
    if unverified_members or not _retained(n, pinned["acceptance"]):
        lexical = "not_evaluable"
    semantic = _semantic(cohort, labels, LABEL_SAMPLE, baseline)
    quality = ("not_evaluable" if "not_evaluable" in (lexical, semantic["gate"])
               else "pass" if lexical == semantic["gate"] == "pass" else "fail")
    return {
        "schema": SCHEMA, "definition": definition,
        "baseline_version": BASELINE_VERSION, "target_version": TARGET_VERSION,
        "pinned": pinned,
        "same_cohort": {"n": n, "before_missed": sum(c["before_missed"] for c in classes.values()),
                        "after_missed": sum(c["after_missed"] for c in classes.values()),
                        "after_failures": failures, "classes": classes},
        "excluded": excluded, "inventory": inventory,
        "new_posts": sum(mid not in pinned_ids for mid in now),
        "retention": retention, "min_retention": float(MIN_RETENTION),
        "lexical_gate": lexical, "semantic_gate": semantic, "quality_gate": quality,
    }


def _data_root() -> Path:
    import mcs_util
    return Path(os.path.realpath(os.path.join(mcs_util.HOME, "data")))


def _private_path(path: Path, *, write: bool = False) -> Path:
    """Resolve symlinks and keep private files inside the data directory at 0600."""
    real = Path(os.path.realpath(path))
    root = _data_root()
    if root not in real.parents:
        raise CohortError("path_outside_data")
    if real.exists():
        if not real.is_file():
            raise CohortError("not_a_file")
        if real.stat().st_mode & 0o077:
            raise CohortError("mode_too_wide")
    elif not write:
        raise CohortError("missing_file")
    return real


def _read_jsonl(path: Path) -> list:
    real = _private_path(path)
    try:
        return [json.loads(line) for line in real.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (UnicodeError, json.JSONDecodeError):
        raise CohortError("bad_jsonl") from None


def write_pin(path: Path, pin: dict) -> None:
    """Atomically replace the private pin.

    Any failure raises and the pin must be rerun.  The error is classified
    conservatively by what is visible afterwards, not by the failing stage:
    ``pin_not_durable`` when the file now holds exactly the new bytes (also
    possible for a pre-rename failure over an identical old pin), otherwise
    the original OSError (also possible after the rename when the bounded
    read-back fails).  A pre-rename failure keeps the old pin in place."""
    real = _private_path(path, write=True)
    data = "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                   for record in [pin["header"], *pin["rows"]]).encode("utf-8")
    try:
        atomic_write(str(real), lambda handle: handle.write(data), mode=0o600,
                     tmp_prefix=".audit-cohort-", makedirs=False, binary=True)
    except OSError:
        try:   # bounded: an unrelated old pin is never read in full
            published = read_regular_nofollow(real, len(data), OSError) == data
        except OSError:
            published = False
        if published:
            raise CohortError("pin_not_durable") from None
        raise


def read_pin(path: Path) -> dict:
    records = _read_jsonl(path)
    if not records or not isinstance(records[0], dict) or records[0].get("schema") != SCHEMA:
        raise CohortError("pin_schema")
    rows = records[1:]
    if not all(isinstance(r, dict) and r.get("set") in ("acceptance", "inventory") for r in rows):
        raise CohortError("pin_schema")
    return {"header": records[0], "rows": rows}


# Only these leave the audit iterator; its body/document/meta are dropped per row.
_ROW_KEYS = ("message_id", "project_id", "content_hash", "reader", "extract_version",
             "classes", "output_digest", "quality", "held_prior", "reason", "hold_unknown",
             "newer_artifact_unknown")


def _cohort_definition(audit_definition):
    from stats_math import wilson_interval
    functions = (_pin_row, build_pin, _now_rows, _gate, _overall, _retained,
                 _wilson, wilson_interval, _semantic, _check_pin_rows, compare,
                 _pinned_scope, _audit_rows)
    contract = {
        'audit_definition': audit_definition,
        'functions': [(fn.__name__, inspect.getsource(fn)) for fn in functions],
        'schema': SCHEMA,
        'baseline_version': BASELINE_VERSION,
        'target_version': TARGET_VERSION,
        'readers': sorted(READERS),
        'qualities': sorted(QUALITIES),
        'label_sources': sorted(LABEL_SOURCES),
        'label_sample': LABEL_SAMPLE,
        'min_retention': [MIN_RETENTION.numerator, MIN_RETENTION.denominator],
        'exclusions': list(EXCLUSIONS),
        'digest_pattern': [_DIGEST.pattern, _DIGEST.flags],
        'row_keys': list(_ROW_KEYS),
    }
    return hashlib.sha256(json.dumps(contract, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def _pinned_scope(header: dict) -> dict:
    """Replay the pinned population window, so another scope never becomes new posts."""
    scope = header.get("scope")
    if not isinstance(scope, dict) or set(scope) != {"since", "until", "as_of", "project_id"}:
        raise CohortError("pin_scope")
    for key in ("since", "until", "as_of"):
        if scope[key] is not None and (type(scope[key]) not in (int, float) or scope[key] != scope[key]):
            raise CohortError("pin_scope")
    if scope["as_of"] is None or (scope["project_id"] is not None and type(scope["project_id"]) is not int):
        raise CohortError("pin_scope")
    return {**scope, "limit": 20}  # the iterator reads every eligible post regardless


def _audit_rows(snapshot: Path, args: argparse.Namespace, scope: dict | None = None):
    import extraction_audit
    import stats_contract
    import mcs_view
    view = mcs_view.View(snapshot)
    try:
        if scope is None:
            scope = stats_contract.resolve_scope({"since": getattr(args, "since", None),
                                      "until": getattr(args, "until", None),
                                      "project": getattr(args, "project", None)},
                                     view.meta["generated_at"])
        elif scope["as_of"] > view.meta["generated_at"]:
            raise CohortError("snapshot_older_than_pin")
        public_version = getattr(args, 'inference_version', inference_version.CURRENT_VERSION)
        options = {'target_version': TARGET_VERSION, 'inference_target': public_version,
                   'hold_mode': 'historical'}
        rows = [{key: row.get(key) for key in _ROW_KEYS}
                for row in extraction_audit.iter_audit_rows(view.db, scope, **options)]
        return (rows, _cohort_definition(extraction_audit.definition_fingerprint(**options)),
                extraction_audit.CUE_CODES, scope, view.meta)
    finally:
        view.close()


def cmd_pin(args) -> dict:
    _private_path(args.out, write=True)  # refuse before reading the snapshot
    rows, definition, codes, scope, meta = _audit_rows(args.snapshot, args)
    pin = build_pin(rows, definition, codes, scope, meta)
    write_pin(args.out, pin)
    sets = [row["set"] for row in pin["rows"]]
    return {"schema": SCHEMA, "definition": definition,
            "pinned": {"acceptance": sets.count("acceptance"), "inventory": sets.count("inventory")}}


def cmd_compare(args) -> dict:
    pin = read_pin(args.pin)
    labels = _read_jsonl(args.labels) if args.labels else None
    rows, definition, codes, _scope, _meta = _audit_rows(
        args.snapshot, args, _pinned_scope(pin["header"]))
    return compare(pin, rows, definition, codes, labels)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CohortError("arguments")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    pin = sub.add_parser("pin")
    pin.add_argument("--snapshot", type=Path, required=True)
    pin.add_argument("--out", type=Path, required=True)
    pin.add_argument("--since")
    pin.add_argument("--until")
    pin.add_argument("--project", type=int)
    pin.add_argument("--inference-version", choices=(inference_version.LEGACY_VERSION,
                     inference_version.CURRENT_VERSION), default=inference_version.CURRENT_VERSION)
    pin.set_defaults(fn=cmd_pin)
    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("--snapshot", type=Path, required=True)
    cmp_.add_argument("--pin", type=Path, required=True)
    cmp_.add_argument("--labels", type=Path)
    cmp_.add_argument("--inference-version", choices=(inference_version.LEGACY_VERSION,
                      inference_version.CURRENT_VERSION), default=inference_version.CURRENT_VERSION)
    cmp_.set_defaults(fn=cmd_compare)
    try:
        args = parser.parse_args(argv)
        report = args.fn(args)
    except CohortError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError):
        # never echo paths, rows or labels from a failure
        print("error: failed", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0 if report.get("quality_gate", "pass") == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
