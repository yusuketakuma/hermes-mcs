"""Build bounded, unconfirmed dictionary hints from exact source text."""

import hashlib
import math
import re
import time

import drug_map

MAX_BODY = 3000
MAX_ALIASES = 200000
MAX_OCCURRENCES = 128
MAX_MATCHES = 20
MAX_CANDIDATES = 3
VERSION = "drug-mentions/1"


def _word(char):
    return bool(char) and (char.isascii() and char.isalnum()
                           or "\u30a0" <= char <= "\u30ff"
                           or "\uff66" <= char <= "\uff9f"
                           or "\u4e00" <= char <= "\u9fff"
                           or char in "ー-－")


def build(db, cfg, body, *, deadline):
    """Return private candidate hints bound to an active dictionary and one text chunk."""
    empty = {"version": VERSION, "status": "invalid_input", "source_sha256": None, "dictionary": None,
             "matches": [], "overlapping_spans_omitted": 0}
    if (not isinstance(body, str) or len(body) > MAX_BODY
            or isinstance(deadline, bool) or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)):
        return empty
    try:
        empty["source_sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
    except UnicodeError:
        return empty
    if time.monotonic() >= deadline:
        return {**empty, "status": "incomplete"}
    dictionary, state = drug_map.active_dictionary(db, cfg)
    if dictionary is None:
        return {**empty, "status": state}
    if time.monotonic() >= deadline:
        return {**empty, "status": "incomplete"}
    matches, scanned, occurrences = {}, 0, 0
    for entry in dictionary.entries.values():
        if entry.kind == "class":
            continue
        for alias in entry.aliases:
            scanned += 1
            if scanned > MAX_ALIASES or time.monotonic() >= deadline:
                return {**empty, "status": "incomplete"}
            if (not 3 <= len(alias) <= 80 or any(ord(c) < 32 or c in "<>" for c in alias)
                    or not (any("\u30a0" <= c <= "\u30ff" or "\uff66" <= c <= "\uff9f"
                                or len(alias) >= 4 and "\u4e00" <= c <= "\u9fff" for c in alias)
                            or sum(c.isascii() and c.isalpha() for c in alias) >= 3)):
                continue
            start = body.find(alias)
            while start >= 0:
                occurrences += 1
                if occurrences > MAX_OCCURRENCES or time.monotonic() >= deadline:
                    return {**empty, "status": "incomplete"}
                end = start + len(alias)
                before = body[start - 1] if start else ""
                after = body[end] if end < len(body) else ""
                if not _word(before) and (not _word(after) or after in "錠剤散液注"):
                    matches.setdefault((start, end), {})[entry.id] = entry
                start = body.find(alias, start + 1)
    spans = sorted(matches)
    if len(spans) > MAX_MATCHES:
        return {**empty, "status": "incomplete"}
    # Conflicting spans are omitted instead of selecting a longer drug name.
    kept = [span for span in spans if not any(
        other != span and span[0] < other[1] and other[0] < span[1] for other in spans)]
    rows = []
    for start, end in kept:
        entries = matches[(start, end)]
        rows.append({"surface": body[start:end], "span": [start, end],
                     "candidate_count": len(entries),
                     "candidates_truncated": len(entries) > MAX_CANDIDATES,
                     "candidates": [{"id": e.id, "display": e.display, "kind": e.kind}
                                    for e in (entries[key] for key in sorted(entries)[:MAX_CANDIDATES])]})
    current, state = drug_map.active_dictionary(db, cfg)
    if time.monotonic() >= deadline:
        return {**empty, "status": "incomplete"}
    if (current is None or current.dict_id != dictionary.dict_id
            or current.sha256 != dictionary.sha256):
        return {**empty, "status": state if current is None else "inactive"}
    return {"version": VERSION, "status": "active", "source_sha256": empty["source_sha256"],
            "dictionary": {"id": dictionary.dict_id,
            "sha256": dictionary.sha256, "resolver_version": drug_map.RESOLVER_VERSION},
            "matches": rows, "overlapping_spans_omitted": len(spans) - len(kept)}


def validate(snapshot, body):
    """Validate a stored hint snapshot against its exact source chunk, without I/O."""
    if (not isinstance(body, str) or len(body) > MAX_BODY or not isinstance(snapshot, dict)
            or set(snapshot) != {"version", "status", "source_sha256", "dictionary", "matches", "overlapping_spans_omitted"}
            or snapshot["version"] != VERSION or not isinstance(snapshot["matches"], list)
            or type(snapshot["overlapping_spans_omitted"]) is not int
            or not 0 <= snapshot["overlapping_spans_omitted"] <= MAX_OCCURRENCES):
        return False
    try:
        if snapshot["source_sha256"] != hashlib.sha256(body.encode("utf-8")).hexdigest():
            return False
    except UnicodeError:
        return False
    if snapshot["status"] != "active":
        return (snapshot["status"] in ("unconfigured", "inactive", "invalid", "incomplete")
                and snapshot["dictionary"] is None and not snapshot["matches"]
                and snapshot["overlapping_spans_omitted"] == 0)
    dictionary = snapshot["dictionary"]
    if (not isinstance(dictionary, dict) or set(dictionary) != {"id", "sha256", "resolver_version"}
            or not drug_map.valid_text(dictionary["id"])
            or not isinstance(dictionary["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", dictionary["sha256"])
            or dictionary["resolver_version"] != drug_map.RESOLVER_VERSION
            or len(snapshot["matches"]) > MAX_MATCHES):
        return False
    previous_end = 0
    for row in snapshot["matches"]:
        if (not isinstance(row, dict) or set(row) != {"surface", "span", "candidate_count",
                                                      "candidates_truncated", "candidates"}):
            return False
        span, count, candidates = row["span"], row["candidate_count"], row["candidates"]
        if (not isinstance(span, list) or len(span) != 2 or any(type(n) is not int for n in span)
                or not previous_end <= span[0] < span[1] <= len(body)
                or not isinstance(row["surface"], str) or body[span[0]:span[1]] != row["surface"]
                or not 3 <= len(row["surface"]) <= 80 or type(count) is not int
                or not 1 <= count <= drug_map.MAX_ENTRIES or not isinstance(candidates, list)
                or len(candidates) != min(count, MAX_CANDIDATES)
                or type(row["candidates_truncated"]) is not bool
                or row["candidates_truncated"] != (count > MAX_CANDIDATES)):
            return False
        ids = []
        for candidate in candidates:
            if (not isinstance(candidate, dict) or set(candidate) != {"id", "display", "kind"}
                    or not drug_map.valid_text(candidate["id"]) or not drug_map.valid_text(candidate["display"])
                    or candidate["kind"] not in ("ingredient", "general_name", "product")):
                return False
            ids.append(candidate["id"])
        if ids != sorted(set(ids)):
            return False
        previous_end = span[1]
    return True
