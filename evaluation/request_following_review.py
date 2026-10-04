#!/usr/bin/env python3
"""Validate/export a pending synthetic held-out queue, never G6 human labels."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import NotRequired, Required, TypedDict
import unicodedata

ROOT = Path(__file__).resolve().parent
FOCUSES = ("request", "question", "self_plan", "reply", "ack", "false_done",
           "conditional", "dependency", "time", "recipient_ambiguity", "requester_ambiguity")
FIELDS = ("request_to", "request_from", "due_text", "condition")
REQUEST_KINDS = ("request", "question", "self_plan", "none", "unknown")
REPLY_KINDS = ("none", "ack", "intent", "progress", "answer", "done", "cancel", "hold", "unknown")


class SourceCase(TypedDict):
    case_id: str
    focus: str
    messages: list[list[str]]
    human_review_status: str
    promotion_eligible: bool
    source_time: NotRequired[str]


class Quote(TypedDict):
    message_index: int
    quote: str


class Proposal(TypedDict, total=False):
    case_id: Required[str]
    request_kind: Required[str]
    reply_kind: str
    false_done_risk: bool
    evidence: Required[list[Quote]]
    request_to: str
    request_from: str
    due_text: str
    condition: str


class ReviewReport(TypedDict):
    version: str
    cases: int
    unique_sources: int
    unique_targets: int
    primary_focus: dict[str, int]
    proposed_request_kinds: dict[str, int]
    proposed_reply_kinds: dict[str, int]
    quoted_known_fields: dict[str, int]
    false_done_risk_cases: int
    cases_with_context_quotes: int
    pending: int
    human_verified_labels: int
    promotion_eligible: bool
    g6_status: str
    g6_min_human_labels: int
    synthetic_declaration_not_authenticity_proof: bool


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def validate(sources: list[SourceCase], proposals: list[Proposal]) -> ReviewReport:
    """Validate fixed pending identities and quoted synthetic suggestions."""
    if not isinstance(sources, list) or not isinstance(proposals, list):
        raise ValueError("queue_arrays_required")
    if len(sources) < 200:
        raise ValueError("queue_min_200_required")
    cases, source_keys, bodies, focuses = {}, set(), set(), Counter()
    for case in sources:
        if not isinstance(case, dict) or set(case) - {
                "case_id", "focus", "messages", "source_time",
                "human_review_status", "promotion_eligible"}:
            raise ValueError("source_schema_invalid")
        cid = case.get("case_id")
        if not isinstance(cid, str) or not re.fullmatch(r"rfh-\d{3}", cid) or cid in cases:
            raise ValueError("case_identity_invalid")
        if case.get("human_review_status") != "pending" or case.get("promotion_eligible") is not False:
            raise ValueError("source_must_remain_pending")
        if case.get("focus") not in FOCUSES:
            raise ValueError("coverage_focus_invalid")
        messages = case.get("messages")
        if not isinstance(messages, list) or not 2 <= len(messages) <= 4:
            raise ValueError("multi_message_context_required")
        for message in messages:
            if (not isinstance(message, list) or len(message) != 2
                    or not all(isinstance(text, str) and text.strip() for text in message)
                    or not message[0].startswith("架空") or len(message[1]) > 1200):
                raise ValueError("fictional_message_invalid")
        if "source_time" in case:
            stamp = datetime.fromisoformat(case["source_time"])
            if stamp.tzinfo is None:
                raise ValueError("source_time_timezone_required")
        # Ignore identities, speaker names, whitespace and ASCII number changes.
        key = re.sub(r"\d+", "#", "".join(
            unicodedata.normalize("NFKC", message[1]) for message in messages))
        key = re.sub(r"\s+", "", key)
        if key in source_keys:
            raise ValueError("duplicate_source_content")
        target = re.sub(r"\s+", "", unicodedata.normalize("NFKC", messages[-1][1]))
        if target in bodies:
            raise ValueError("duplicate_target_content")
        source_keys.add(key)
        bodies.add(target)
        cases[cid] = case
        focuses[case["focus"]] += 1
    if set(focuses) != set(FOCUSES) or min(focuses.values()) < 15:
        raise ValueError("coverage_focus_insufficient")
    seen, request_kinds, reply_kinds, known = set(), Counter(), Counter(), Counter()
    false_done, context_quotes = 0, 0
    for proposed in proposals:
        if not isinstance(proposed, dict) or set(proposed) - {
                "case_id", "request_kind", "reply_kind", "false_done_risk", "evidence", *FIELDS}:
            raise ValueError("proposal_schema_invalid")
        cid = proposed.get("case_id")
        if cid not in cases or cid in seen:
            raise ValueError("proposal_identity_invalid")
        seen.add(cid)
        if proposed.get("request_kind") not in REQUEST_KINDS \
                or proposed.get("reply_kind", "none") not in REPLY_KINDS \
                or type(proposed.get("false_done_risk", False)) is not bool:
            raise ValueError("proposal_classification_invalid")
        evidence = proposed.get("evidence")
        messages = cases[cid]["messages"]
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("proposal_quote_required")
        quotes, indices = [], set()
        for ev in evidence:
            if (not isinstance(ev, dict) or set(ev) != {"message_index", "quote"}
                    or type(ev["message_index"]) is not int
                    or not 1 <= ev["message_index"] <= len(messages)
                    or not isinstance(ev["quote"], str) or not ev["quote"]
                    or ev["quote"] not in messages[ev["message_index"] - 1][1]):
                raise ValueError(f"proposal_quote_invalid:{cid}")
            quotes.append(ev["quote"])
            indices.add(ev["message_index"])
        if len(messages) not in indices:
            raise ValueError("proposal_target_quote_required")
        context_quotes += int(any(index < len(messages) for index in indices))
        for field in FIELDS:
            value = proposed.get(field, "unknown")
            if (not isinstance(value, str) or not value.strip()
                    or len(value) > (30 if field in FIELDS[:2] else 60)
                    or value != "unknown" and not any(value in quote for quote in quotes)):
                raise ValueError(f"proposal_field_unquoted:{cid}:{field}")
            known[field] += int(value != "unknown")
        request_kinds[proposed["request_kind"]] += 1
        reply_kinds[proposed.get("reply_kind", "none")] += 1
        false_done += int(proposed.get("false_done_risk", False))
    if seen != set(cases):
        raise ValueError("proposal_set_incomplete")
    if min(known.values(), default=0) < 20 or false_done < 20:
        raise ValueError("request_field_coverage_insufficient")
    return {"version": "request-following-review-report/1", "cases": len(cases),
            "unique_sources": len(source_keys), "unique_targets": len(bodies),
            "primary_focus": dict(sorted(focuses.items())),
            "proposed_request_kinds": dict(sorted(request_kinds.items())),
            "proposed_reply_kinds": dict(sorted(reply_kinds.items())),
            "quoted_known_fields": dict(sorted(known.items())),
            "false_done_risk_cases": false_done, "cases_with_context_quotes": context_quotes,
            "pending": len(cases), "human_verified_labels": 0, "promotion_eligible": False,
            "g6_status": "not_evaluated", "g6_min_human_labels": 200,
            "synthetic_declaration_not_authenticity_proof": True}


def load_queue(directory: Path = ROOT) -> tuple[list[SourceCase], list[Proposal], ReviewReport]:
    """Read frozen local source/proposal files only; never read product data."""
    manifest = json.loads((directory / "request_following_review_manifest.json").read_text())
    if (not isinstance(manifest, dict)
            or manifest.get("version") != "request-following-review-queue/1"
            or manifest.get("source") != "fully_synthetic" or manifest.get("split") != "test"
            or manifest.get("promotion_eligible") is not False
            or manifest.get("human_review_status") != "pending"
            or manifest.get("min_cases") != 200):
        raise ValueError("queue_manifest_invalid")
    loaded = {}
    for key in ("source_files", "proposal_files"):
        files = manifest.get(key)
        if not isinstance(files, dict) or not files:
            raise ValueError("queue_manifest_files_invalid")
        rows = []
        for filename, expected_hash in files.items():
            if not isinstance(filename, str) or Path(filename).name != filename:
                raise ValueError("queue_path_invalid")
            raw = (directory / filename).read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected_hash:
                raise ValueError("queue_file_identity_changed")
            decoded = json.loads(raw)
            if not isinstance(decoded, list):
                raise ValueError("queue_file_array_required")
            rows.extend(decoded)
        loaded[key] = rows
    report = validate(loaded["source_files"], loaded["proposal_files"])
    if report["cases"] != manifest.get("case_count"):
        raise ValueError("queue_case_count_changed")
    return loaded["source_files"], loaded["proposal_files"], report


def worksheet(case: SourceCase):
    """Source-first review row with no proposal, signature or fake human label."""
    cid = case["case_id"]
    return {"version": "request-following-review-worksheet/1", "case_id": cid,
            "account_id": "fictional-request-following-review",
            "project_id": "fictional-project-" + cid, "thread_id": "fictional-thread-" + cid,
            "split": "test", "source": "fully_synthetic", "source_fingerprint": fingerprint(case),
            "messages": [{"message_id": f"m{i}", "speaker": speaker, "body": body}
                         for i, (speaker, body) in enumerate(case["messages"], 1)],
            "target_message_id": f"m{len(case['messages'])}",
            "source_time": case.get("source_time"),
            "human_review_status": "pending", "promotion_eligible": False,
            "human_verified_labels": None, "human_receipt": None,
            "model_outputs": None, "telemetry": None}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "export", "export-proposals"))
    parser.add_argument("--directory", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        sources, proposals, report = load_queue(args.directory)
        if args.command == "validate":
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        elif args.command == "export":
            # Stable mixed order avoids showing the authoring focus blocks.
            for case in sorted(sources, key=fingerprint):
                print(json.dumps(worksheet(case), ensure_ascii=False, allow_nan=False))
        else:
            for proposed in sorted(proposals, key=lambda row: row["case_id"]):
                print(json.dumps({"source": "synthetic_proposal", "promotion_eligible": False,
                    **proposed, **{key: proposed.get(key, "unknown") for key in FIELDS}},
                    ensure_ascii=False, allow_nan=False))
    except (ValueError, TypeError, KeyError, OSError) as error:
        print(json.dumps({"state": "invalid", "reason": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
