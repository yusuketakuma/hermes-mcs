"""Build bounded, unconfirmed dictionary hints from exact source text."""

import hashlib
import math
import re
import sqlite3
import time
import unicodedata

import clinical_values
import drug_map
import extract_validation

MAX_BODY = 3000
MAX_ALIASES = 200000
MAX_OCCURRENCES = 128
MAX_MATCHES = 20
MAX_CANDIDATES = 3
VERSION = "drug-mentions/2"


_MED_CONTEXT = re.compile(r"薬|処方|服用|内服|投与|錠|剤|開始|中止|増量|減量|頓服")


def _safe_label(value):
    return (isinstance(value, str) and 0 < len(value) <= 200 and bool(value.strip())
            and not any(unicodedata.category(c).startswith("C")
                        or unicodedata.category(c) in ("Zl", "Zp") for c in value))


def _expanded_end(body, start, alias):
    end = start + len(alias)
    base = clinical_values.medication_surface(alias)["base"]
    for stop in range(end + 1, min(len(body), end + 32, start + 80) + 1):
        surface = clinical_values.medication_surface(body[start:stop])
        if surface["base"] == base and (surface["form"] or surface["strength"]):
            end = stop
    return end


def _short_context(body, start, end):
    delimiters = "。！？!?;；、,\n"
    left = max(body.rfind(char, 0, start) for char in delimiters) + 1
    stop = re.search(r"[。！？!?;；、,\n]", body[end:])
    right = end + stop.start() if stop else len(body)
    return bool(_MED_CONTEXT.search(body[max(left, start - 16):min(right, end + 24)]))


def _active(db, cfg):
    try:
        return drug_map.active_dictionary(db, cfg)
    except (sqlite3.Error, OSError):
        return None, "invalid"


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
    dictionary, state = _active(db, cfg)
    if dictionary is None:
        return {**empty, "status": state}
    if time.monotonic() >= deadline:
        return {**empty, "status": "incomplete"}
    aliases, scanned, partial = {}, 0, False
    for entry in dictionary.entries.values():
        if entry.kind == "class":
            continue
        for alias in entry.aliases:
            scanned += 1
            if scanned > MAX_ALIASES or time.monotonic() >= deadline:
                return {**empty, "status": "incomplete"}
            if (not 2 <= len(alias) <= 80 or not _safe_label(alias) or any(c in "<>" for c in alias)
                    or not (any("\u30a0" <= c <= "\u30ff" or "\uff66" <= c <= "\uff9f"
                                or len(alias) >= 4 and "\u4e00" <= c <= "\u9fff" for c in alias)
                            or sum(c.isascii() and c.isalpha() for c in alias) >= 3)):
                continue
            if not _safe_label(entry.id) or not _safe_label(entry.display):
                partial = True
                continue
            aliases.setdefault(alias, {})[entry.id] = entry
    matches = {}
    for alias, entries in aliases.items():
        start = body.find(alias)
        while start >= 0:
            if time.monotonic() >= deadline:
                return {**empty, "status": "incomplete"}
            end = _expanded_end(body, start, alias)
            surface = body[start:end]
            if ((len(alias) > 2 or _short_context(body, start, end))
                    and (start, end) in extract_validation._med_name_spans(body, surface)):
                key = (start, end)
                if key not in matches and len(matches) >= MAX_OCCURRENCES:
                    partial = True
                else:
                    found = matches.setdefault(key, {})
                    for code, entry in entries.items():
                        old = found.get(code)
                        if old is None or len(alias) > len(old[1]):
                            found[code] = (entry, alias)
            start = body.find(alias, start + 1)
    spans = sorted(matches)
    # Only containment can be merged without making two mentions one.
    kept = [span for span in spans if not any(
        other != span and other[0] <= span[0] and span[1] <= other[1] for other in spans)]
    crossing = {span for span in kept if any(
        other != span and span[0] < other[1] and other[0] < span[1] for other in kept)}
    if crossing:
        partial = True
    kept = [span for span in kept if span not in crossing]
    if len(kept) > MAX_MATCHES:
        partial = True
        kept = kept[:MAX_MATCHES]
    rows = []
    for start, end in kept:
        entries = {}
        for span in spans:
            if start <= span[0] and span[1] <= end:
                for code, value in matches[span].items():
                    old = entries.get(code)
                    if old is None or len(value[1]) > len(old[1]):
                        entries[code] = value
        rows.append({"surface": body[start:end], "span": [start, end],
                     "candidate_count": len(entries),
                     "candidates_truncated": len(entries) > MAX_CANDIDATES,
                     "candidates": [{"id": e.id, "display": e.display, "kind": e.kind,
                                     "matched_surface": alias}
                                    for e, alias in (entries[key] for key in sorted(entries)[:MAX_CANDIDATES])]})
    current, state = _active(db, cfg)
    if time.monotonic() >= deadline:
        return {**empty, "status": "incomplete"}
    if (current is None or current.dict_id != dictionary.dict_id
            or current.sha256 != dictionary.sha256):
        return {**empty, "status": state if current is None else "inactive"}
    return {"version": VERSION, "status": "partial" if partial else "active", "source_sha256": empty["source_sha256"],
            "dictionary": {"id": dictionary.dict_id,
            "sha256": dictionary.sha256, "resolver_version": drug_map.RESOLVER_VERSION},
            "matches": rows, "overlapping_spans_omitted": len(crossing)}


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
    if snapshot["status"] not in ("active", "partial"):
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
                or not 2 <= len(row["surface"]) <= 80 or type(count) is not int
                or not 1 <= count <= drug_map.MAX_ENTRIES or not isinstance(candidates, list)
                or len(candidates) != min(count, MAX_CANDIDATES)
                or type(row["candidates_truncated"]) is not bool
                or row["candidates_truncated"] != (count > MAX_CANDIDATES)):
            return False
        ids = []
        for candidate in candidates:
            if (not isinstance(candidate, dict) or set(candidate) != {"id", "display", "kind", "matched_surface"}
                    or not _safe_label(candidate["id"]) or not _safe_label(candidate["display"])
                    or not _safe_label(candidate["matched_surface"])
                    or not 2 <= len(candidate["matched_surface"]) <= 80
                    or candidate["matched_surface"] not in row["surface"]
                    or candidate["kind"] not in ("ingredient", "general_name", "product")):
                return False
            ids.append(candidate["id"])
        if ids != sorted(set(ids)):
            return False
        previous_end = span[1]
    return True
