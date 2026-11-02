"""Freeze source-scoped medication references without exporting a private dictionary."""
import hashlib
import json
import sqlite3
import time

import drug_map
import drug_mentions
from clinical_chunking import plan_chunks
from mcs_util import payload_hash

VERSION = "extraction-medications/1"
MAX_PARTS = 32
MAX_REFERENCE_CHARS = 2400
_STATUSES = {"active", "partial", "unconfigured", "inactive", "invalid", "incomplete"}
_INSTRUCTION = (
    "薬剤抽出の確認: 用量のない薬名や略称も、対象本文にある表記のまま拾う。"
    "製品名に含まれるmg・%などの規格と、実際の服用量・頻度を区別し、"
    "服用量が書かれていなければdoseへ規格を転記しない。"
    "薬名・用量・頻度・変更・対象者・否定・過去・予定は対象本文だけから読み、"
    "evidenceは対象本文から引用する。参考候補の名称を本文の薬名へ置き換えず、"
    "辞書候補を処方・成分・同等性の確定と扱わない。\n")


def _source_hash(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _configuration(cfg):
    return payload_hash((cfg or {}).get("drug_map"))


def build(db, cfg, body, *, private, deadline):
    """Capture bounded reference-only hints on the ledger owner thread before inference."""
    if not isinstance(body, str) or type(private) is not bool:
        raise ValueError("medication_reference_input_invalid")
    doc = {"version": VERSION, "source_sha256": _source_hash(body), "private": private,
           "status": "incomplete" if private else "not_requested", "parts": [],
           "configuration": _configuration(cfg) if private else None,
           "generation": drug_map.generation_signature(db) if private else None}
    if not private:
        return doc
    pieces = plan_chunks(body, drug_mentions.MAX_BODY)
    if len(pieces) > MAX_PARTS:
        return doc
    start = 0
    for piece in pieces:
        hint = drug_mentions.build(db, cfg, piece, deadline=deadline)
        if not drug_mentions.validate(hint, piece):
            raise ValueError("medication_reference_snapshot_invalid")
        doc["parts"].append({"start": start, "end": start + len(piece), "hint": hint})
        start += len(piece)
    if (_configuration(cfg) != doc["configuration"]
            or drug_map.generation_signature(db) != doc["generation"]):
        doc["parts"] = []
        return doc
    states = {part["hint"]["status"] for part in doc["parts"]}
    pins = {payload_hash(part["hint"]["dictionary"]) for part in doc["parts"]
            if part["hint"]["status"] in ("active", "partial")}
    if len(pins) > 1 or states & {"active", "partial"} and states - {"active", "partial", "incomplete"}:
        doc["parts"] = []
        return doc
    doc["status"] = ("partial" if states & {"active", "partial"} and states != {"active"}
                     else next(iter(states)) if len(states) == 1 else "incomplete")
    return doc


def valid(snapshot, body):
    """Validate the immutable reference payload against its complete source, without I/O."""
    if (not isinstance(snapshot, dict) or not isinstance(body, str)
            or set(snapshot) != {"version", "source_sha256", "private", "status", "parts",
                                 "configuration", "generation"}
            or snapshot["version"] != VERSION or type(snapshot["private"]) is not bool
            or not isinstance(snapshot["parts"], list) or len(snapshot["parts"]) > MAX_PARTS):
        return False
    try:
        if snapshot["source_sha256"] != _source_hash(body):
            return False
    except UnicodeError:
        return False
    if not snapshot["private"]:
        return (snapshot["status"] == "not_requested" and not snapshot["parts"]
                and snapshot["configuration"] is None and snapshot["generation"] is None)
    if (snapshot["status"] not in _STATUSES or any(
            not isinstance(snapshot[key], str) or len(snapshot[key]) != 64
            or any(c not in "0123456789abcdef" for c in snapshot[key])
            for key in ("configuration", "generation"))):
        return False
    end = 0
    states, pins = set(), set()
    for part in snapshot["parts"]:
        if (not isinstance(part, dict) or set(part) != {"start", "end", "hint"}
                or type(part["start"]) is not int or type(part["end"]) is not int
                or part["start"] != end or not end < part["end"] <= len(body)
                or not drug_mentions.validate(part["hint"], body[end:part["end"]])):
            return False
        end = part["end"]
        states.add(part["hint"]["status"])
        if part["hint"]["status"] in ("active", "partial"):
            pins.add(payload_hash(part["hint"]["dictionary"]))
    if not snapshot["parts"]:
        return snapshot["status"] == "incomplete"
    expected = ("partial" if states & {"active", "partial"} and states != {"active"}
                else next(iter(states)) if len(states) == 1 else "incomplete")
    return (end == len(body) and len(pins) <= 1
            and not (states & {"active", "partial"} and states - {"active", "partial", "incomplete"})
            and snapshot["status"] == expected)


def has_private(snapshot):
    return bool(snapshot and any(part["hint"]["matches"] for part in snapshot["parts"]))


def uses_dictionary(snapshot):
    return bool(snapshot and any(part["hint"]["status"] in ("active", "partial")
                                 for part in snapshot["parts"]))


def current(db, cfg, snapshot, *, body=None, deadline=None):
    """Recheck approval and generation before using or committing private reference inputs."""
    if snapshot is None:
        return True
    try:
        if not valid(snapshot, body):
            return False
        if not uses_dictionary(snapshot):
            return True
        if (_configuration(cfg) != snapshot["configuration"]
                or drug_map.generation_signature(db) != snapshot["generation"]):
            return False
        dictionary, state = drug_map.active_dictionary(db, cfg)
        if state != "active" or dictionary is None:
            return False
        limit = time.monotonic() + 1.0
        if deadline is not None:
            limit = min(limit, deadline)
        for part in snapshot["parts"]:
            hint = part["hint"]
            if hint["status"] not in ("active", "partial"):
                continue
            if not drug_mentions.matches_snapshot(dictionary, hint, body[part["start"]:part["end"]],
                                                  deadline=limit):
                return False
        return True
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
        return False


def block(snapshot, body, start, piece, *, private):
    """Render only references wholly inside one target piece; cloud gets public instructions."""
    if snapshot is None:
        return ""
    if not valid(snapshot, body) or type(start) is not int or body[start:start + len(piece)] != piece:
        raise ValueError("medication_reference_source_changed")
    rows = []
    if private:
        for part in snapshot["parts"]:
            for match in part["hint"]["matches"]:
                left, right = (part["start"] + n for n in match["span"])
                if start <= left < right <= start + len(piece):
                    rows.append({"surface": match["surface"],
                                 "candidates": [{"matched_surface": candidate["matched_surface"],
                                                 "name": candidate["display"], "kind": candidate["kind"]}
                                                for candidate in match["candidates"]],
                                 "candidate_count": match["candidate_count"],
                                 "candidates_truncated": match["candidates_truncated"]})
    if not rows:
        return _INSTRUCTION
    selected = []
    for row in rows:
        trial = {"candidates": selected + [row], "unshown_matches": len(rows) - len(selected) - 1,
                 "scan_status": snapshot["status"]}
        if len(json.dumps(trial, ensure_ascii=False, separators=(",", ":"))) > MAX_REFERENCE_CHARS:
            break
        selected.append(row)
    raw = json.dumps({"candidates": selected, "unshown_matches": len(rows) - len(selected),
                      "scan_status": snapshot["status"]}, ensure_ascii=False, separators=(",", ":"))
    raw = raw.replace("<<<", "＜＜＜").replace(">>>", "＞＞＞").replace("対象本文:", "対象本文：").replace("JSON:", "JSON：")
    return (_INSTRUCTION + "承認辞書の参考候補（未確定・この区間の本文表記だけが対象）:\n<<<\n"
            + raw + "\n>>>\n\n")
