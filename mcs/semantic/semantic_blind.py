"""Prepare local blinded worksheets from three fixed-bundle outputs.

Default mode only reads supplied text. Explicit snapshot mode generates a local baseline. The coordinator keeps the key;
reviewers receive only the worksheet. Text itself may still reveal a method.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sqlite3
from contextlib import closing
import sys
import uuid
from pathlib import Path

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every first-level subdir as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401

from semantic_evaluation import EvaluationError, _dict, _id, load_jsonl

METHODS = ("baseline", "assisted", "audited")


def prepare(records: list[dict]) -> tuple[list[dict], list[dict]]:
    worksheets, keys = [], []
    cases, scopes = set(), {}
    for record in records:
        record = _dict(record, "blind_record")
        case = _id(record.get("case_id"), "case_id")
        if case in cases:
            raise EvaluationError("duplicate_case")
        cases.add(case)
        account = _id(record.get("account_id"), "account_id")
        project = _id(record.get("project_id"), "project_id")
        split = record.get("split")
        if split not in ("dev", "calibration", "test"):
            raise EvaluationError("split_invalid")
        scope = (account, project)
        if scope in scopes and scopes[scope] != split:
            raise EvaluationError("patient_split_leak")
        scopes[scope] = split
        fingerprint = _id(record.get("bundle_fingerprint"), "bundle_fingerprint")
        source = record.get("source_text")
        if not isinstance(source, str) or not source.strip():
            raise EvaluationError("source_text_missing")
        outputs = _dict(record.get("outputs"), "outputs")
        if set(outputs) != set(METHODS):
            raise EvaluationError("three_methods_required")
        methods = list(METHODS)
        random.SystemRandom().shuffle(methods)
        review_id = uuid.uuid4().hex
        choices, mapping, candidate_hashes = [], {}, {}
        for label, method in zip(("A", "B", "C"), methods):
            output = _dict(outputs[method], "output")
            if output.get("bundle_fingerprint") != fingerprint:
                raise EvaluationError("output_bundle_mismatch")
            text = output.get("text")
            if not isinstance(text, str) or not text.strip():
                raise EvaluationError("output_text_missing")
            claim_texts = output.get("claim_texts", [text])
            limitations = output.get("limitations", [])
            if (not isinstance(claim_texts, list) or not isinstance(limitations, list)
                    or any(not isinstance(item, str) or not item.strip()
                           for item in claim_texts + limitations)
                    or "\n".join(claim_texts + limitations) != text):
                raise EvaluationError("output_claim_text_mismatch")
            choices.append({"label": label, "text": text,
                            "claims": [{"claim_id": f"c{i + 1}", "text": item}
                                       for i, item in enumerate(claim_texts)],
                            "limitations": limitations})
            if "evaluation_candidate" in output:
                candidate = _dict(output["evaluation_candidate"], "evaluation_candidate")
                claims = candidate.get("claims")
                if not isinstance(claims, list) or any(not isinstance(c, dict) for c in claims) or [
                        {"claim_id": c.get("claim_id"), "text": c.get("text")}
                        for c in claims] != choices[-1]["claims"]:
                    raise EvaluationError("evaluation_claims_mismatch")
                frozen = json.dumps(candidate, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False)
                candidate_hashes[label] = hashlib.sha256(frozen.encode()).hexdigest()
                # Show the structured predictions that the evaluator will score.
                frozen_candidate = json.loads(frozen)
                choices[-1]["predictions"] = {field: frozen_candidate.get(field)
                                               for field in ("facts", "loops", "status")}
            mapping[label] = method
        worksheets.append({"review_id": review_id, "source_text": source,
                           "choices": choices, "human_labels": None})
        keys.append({"review_id": review_id, "case_id": case,
                     "account_id": account, "project_id": project,
                     "split": split, "bundle_fingerprint": fingerprint,
                     "methods": mapping, "candidate_sha256": candidate_hashes,
                     "worksheet_sha256": hashlib.sha256(json.dumps(
                         worksheets[-1], ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode()).hexdigest()})
        if "artifact_ids" in record:
            ids = _dict(record["artifact_ids"], "artifact_ids")
            if set(ids) != {"bundle_id", "candidate_id", "final_id"} or any(
                    type(value) is not int or value <= 0 for value in ids.values()):
                raise EvaluationError("artifact_ids_invalid")
            keys[-1]["artifact_ids"] = dict(ids)
    if not worksheets:
        raise EvaluationError("blind_records_empty")
    return worksheets, keys


def fixed_bundle_outputs(bundle: dict, target_id: int, candidate: dict,
                         final: dict, baseline_fn) -> dict:
    """Build comparison texts; baseline_fn is explicitly supplied by the caller.

    candidate/final are decoded artifact rows (content and meta are objects).
    The baseline receives the target body plus the same thread context the
    production extractor would see (earlier bundle members of its thread),
    so it measures the same input configuration as run_pending.
    """
    import semantic
    from summary_review import _validate_candidate, _validate_baseline
    bundle = _dict(bundle, "bundle")
    members = bundle.get("members")
    if not isinstance(members, list) or not members:
        raise EvaluationError("bundle_members_missing")
    project, root = bundle.get("project_id"), bundle.get("root_id")
    if any(type(value) is not int or value <= 0 for value in (project, root, target_id)):
        raise EvaluationError("bundle_scope_invalid")
    ids = set()
    for member in members:
        member = _dict(member, "bundle_member")
        mid = member.get("message_id")
        if (type(mid) is not int or mid <= 0 or mid in ids
                or type(member.get("project_id")) is not int
                or member["project_id"] != project
                or (mid == root and member.get("parent_id") is not None)
                or (mid != root and (type(member.get("parent_id")) is not int
                                     or member["parent_id"] != root))):
            raise EvaluationError("bundle_scope_invalid")
        ids.add(mid)
    if root not in ids or target_id not in ids:
        raise EvaluationError("bundle_scope_invalid")
    fingerprint = semantic.bundle_fingerprint(members)
    if bundle.get("source_fingerprint") != fingerprint:
        raise EvaluationError("bundle_fingerprint_invalid")
    targets = [m for m in members if m["message_id"] == target_id]
    if len(targets) != 1 or targets[0]["body_state"] != "full":
        raise EvaluationError("target_not_full")
    target = targets[0]
    for artifact in (candidate, final):
        meta = _dict(artifact.get("meta"), "artifact_meta")
        if (artifact.get("message_id") != target_id
                or artifact.get("project_id") != target["project_id"]
                or meta.get("fingerprint") != fingerprint
                or meta.get("target_revision") != target["revision"]):
            raise EvaluationError("artifact_source_mismatch")
        content = _dict(artifact.get("content"), "artifact_content")
        if (type(content.get("target_message_id")) is not int
                or content["target_message_id"] != target_id
                or not bundle.get("bundle_id")
                or content.get("input_bundle_id") != bundle["bundle_id"]):
            raise EvaluationError("artifact_content_source_mismatch")
        if not _validate_candidate(content):
            raise EvaluationError("candidate_empty")
    if (candidate["meta"].get("stage") != "pre_audit"
            or final["meta"].get("audit_status") not in ("PASS", "NEEDS_REVIEW")
            or not candidate["meta"].get("policy_fingerprint")
            or candidate["meta"]["policy_fingerprint"] != final["meta"].get("policy_fingerprint")
            or candidate["meta"].get("publication_mode") != final["meta"].get("publication_mode")):
        raise EvaluationError("artifact_stage_mismatch")
    # The production extractor receives earlier thread members as
    # reference context — the baseline must see the same input or it
    # measures a different configuration than production.
    from extract_llm import _ctx_lines
    t_ts = target.get("posted_at") or ""
    ctx_rows = [
        {"message_id": m["message_id"], "body_text": m["body_original"],
         "posted_at_ts": m.get("posted_at") or "",
         "who": m.get("sender") or "投稿者"}
        for m in members
        if m.get("body_state") == "full" and m.get("body_original")
        and (m["message_id"] == root or m.get("parent_id") == root)
        and (m.get("posted_at") or "") < t_ts
    ]
    ctx = "\n".join(_ctx_lines(ctx_rows, root)) or None
    baseline = baseline_fn(target["body_original"], context=ctx)
    if not isinstance(baseline, dict) or not _validate_baseline(baseline):
        raise EvaluationError("baseline_failed")
    claims = {"baseline": [text for text in
              [baseline.get("summary") or ""] + (baseline.get("points") or [])
              if text.strip()]}
    limitations = {"baseline": []}
    for method, artifact in (("assisted", candidate), ("audited", final)):
        content = artifact["content"]
        claims[method] = [c["text"] for c in content["claims"]]
        limitations[method] = [text for text in content.get("limitations", []) if text.strip()]
    outputs = {method: {"bundle_fingerprint": fingerprint,
                        "text": "\n".join(claims[method] + limitations[method]),
                        "claim_texts": claims[method], "limitations": limitations[method]}
               for method in METHODS}
    if any(not output["text"].strip() for output in outputs.values()):
        raise EvaluationError("comparison_text_empty")
    return outputs


def snapshot_records(path: str, selections: list[dict], baseline_fn) -> list[dict]:
    """Read explicit IDs in one snapshot; validate every case before inference."""
    records, inputs = [], []
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        for selection in selections:
            selection = _dict(selection, "selection")
            artifacts = []
            for field, kind in (("bundle_id", "semantic_bundle"),
                                ("candidate_id", "semantic_candidate"),
                                ("final_id", "semantic_summary")):
                aid = selection.get(field)
                if type(aid) is not int or aid <= 0:
                    raise EvaluationError("artifact_id_invalid")
                row = db.execute("SELECT * FROM artifacts WHERE artifact_id=? AND kind=?",
                                 (aid, kind)).fetchone()
                if row is None:
                    raise EvaluationError("artifact_missing")
                artifact = dict(row)
                for key in ("content", "meta"):
                    artifact[key] = _dict(json.loads(artifact[key]), "artifact_" + key)
                artifacts.append(artifact)
            bundle_row, candidate, final = artifacts
            bundle = bundle_row["content"]
            target = candidate["message_id"]
            if (selection.get("project_id") != candidate["project_id"]
                    or bundle_row["project_id"] != candidate["project_id"]):
                raise EvaluationError("selection_project_mismatch")
            outputs = fixed_bundle_outputs(bundle, target, candidate, final,
                                           lambda body, **_:
                                           {"summary": "validation only"})
            record = {key: selection.get(key) for key in
                      ("case_id", "account_id", "project_id", "split")}
            record.update(artifact_ids={key: selection[key] for key in
                          ("bundle_id", "candidate_id", "final_id")},
                          bundle_fingerprint=bundle["source_fingerprint"],
                          source_text=json.dumps({
                              "target_message_id": target,
                              "content_quality": bundle.get("content_quality"),
                              "context_complete": bundle.get("context_complete"),
                              "missing_replies": bundle.get("missing_replies"),
                              "attachments_interpreted": False,
                              "messages": [{key: m.get(key) for key in
                                  ("message_id", "parent_id", "revision", "posted_at",
                                   "sender", "body_state", "body_original", "attachments")}
                                  for m in bundle["members"]]},
                              ensure_ascii=False, indent=2),
                          outputs=outputs)
            records.append(record)
            inputs.append((bundle, target, candidate, final))
    prepare(records)  # Reject split leakage/duplicate cases before local generation.
    for record, args in zip(records, inputs):
        record["outputs"] = fixed_bundle_outputs(*args, baseline_fn)
    return records


def unblind(worksheets: list[dict], keys: list[dict]) -> list[dict]:
    """Restore method names without inferring or approving human judgments."""
    index = {}
    for key in keys:
        key = _dict(key, "coordinator_key")
        rid = _id(key.get("review_id"), "review_id")
        if rid in index:
            raise EvaluationError("duplicate_review_key")
        mapping = _dict(key.get("methods"), "methods")
        if set(mapping) != {"A", "B", "C"} or set(mapping.values()) != set(METHODS):
            raise EvaluationError("method_mapping_invalid")
        index[rid] = key
    restored, seen = [], set()
    for worksheet in worksheets:
        worksheet = _dict(worksheet, "worksheet")
        rid = _id(worksheet.get("review_id"), "review_id")
        if rid not in index or rid in seen:
            raise EvaluationError("review_identity_mismatch")
        seen.add(rid)
        key = index[rid]
        original = dict(worksheet, human_labels=None)
        digest = hashlib.sha256(json.dumps(original, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest()
        if digest != key.get("worksheet_sha256"):
            raise EvaluationError("worksheet_changed")
        labels = _dict(worksheet.get("human_labels"), "human_labels")
        if set(labels) != {"A", "B", "C"} or any(
                not isinstance(value, dict) or not value for value in labels.values()):
            raise EvaluationError("human_labels_incomplete")
        restored.append({"review_id": rid, "case_id": key["case_id"],
                         "account_id": key["account_id"], "project_id": key["project_id"],
                         "worksheet_sha256": key["worksheet_sha256"],
                         "bundle_fingerprint": key["bundle_fingerprint"],
                         "split": key["split"],
                         "labels_by_method": {key["methods"][label]: value
                                              for label, value in labels.items()}})
        if "artifact_ids" in key:
            restored[-1]["artifact_ids"] = key["artifact_ids"]
    if seen != set(index) or not restored:
        raise EvaluationError("review_set_incomplete")
    return restored


def merge_labels(records, worksheets, keys, method, manifest):
    """Attach explicit labels only to the exact reviewed candidate text."""
    from semantic_evaluation import validate_manifest, validate_records
    if method not in METHODS:
        raise EvaluationError("method_invalid")
    reviewed = unblind(worksheets, keys)
    by_case = {row["case_id"]: row for row in reviewed}
    if len(by_case) != len(reviewed):
        raise EvaluationError("duplicate_review_case")
    sheets = {row["review_id"]: row for row in worksheets}
    key_index = {row["review_id"]: row for row in keys}
    merged, seen = [], set()
    for record in records:
        record = _dict(record, "evaluation_record")
        case = _id(record.get("case_id"), "case_id")
        if case not in by_case or case in seen:
            raise EvaluationError("evaluation_case_mismatch")
        seen.add(case)
        review = by_case[case]
        for field in ("account_id", "project_id", "split"):
            if _id(record.get(field), field) != _id(review.get(field), field):
                raise EvaluationError("evaluation_identity_mismatch")
        bundle = _dict(record.get("bundle"), "bundle")
        if bundle.get("fingerprint") != review["bundle_fingerprint"]:
            raise EvaluationError("evaluation_bundle_mismatch")
        label = next(k for k, v in key_index[review["review_id"]]["methods"].items() if v == method)
        choice = next(c for c in sheets[review["review_id"]]["choices"] if c["label"] == label)
        candidate = _dict(record.get("candidate"), "candidate")
        claims = candidate.get("claims")
        if not isinstance(claims, list) or [
                {"claim_id": c.get("claim_id"), "text": c.get("text")}
                for c in claims if isinstance(c, dict)] != choice["claims"]:
            raise EvaluationError("evaluation_claims_mismatch")
        expected = key_index[review["review_id"]].get("candidate_sha256", {}).get(label)
        if not expected:
            raise EvaluationError("evaluation_candidate_not_frozen")
        digest = hashlib.sha256(json.dumps(candidate, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        if digest != expected:
            raise EvaluationError("evaluation_candidate_mismatch")
        if record.get("label") is not None:
            raise EvaluationError("evaluation_label_already_present")
        merged.append({**record, "label": review["labels_by_method"][method]})
    if seen != set(by_case):
        raise EvaluationError("evaluation_cases_incomplete")
    validate_records(merged, validate_manifest(manifest))
    return merged


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--snapshot", help="read-only SQLite; input contains explicit artifact IDs")
    parser.add_argument("--generate-local-baseline", action="store_true")
    parser.add_argument("--unblind-key", help="restore methods from completed worksheets")
    parser.add_argument("--evaluation-records")
    parser.add_argument("--manifest")
    parser.add_argument("--method", choices=METHODS)
    args = parser.parse_args(argv)
    merge_options = (args.evaluation_records, args.manifest, args.method)
    if any(merge_options) and (not all(merge_options) or not args.unblind_key):
        parser.error("label merge requires evaluation-records, manifest, method and unblind-key")
    if args.unblind_key and (args.snapshot or args.generate_local_baseline):
        parser.error("unblinding cannot generate model output")
    if bool(args.snapshot) != args.generate_local_baseline:
        parser.error("snapshot requires --generate-local-baseline")
    try:
        directory = Path(args.output_dir)
        directory.mkdir(mode=0o700)  # reserve before any model call
        records = load_jsonl(args.input)
        if args.snapshot:
            from extract_llm import llm_extract
            records = snapshot_records(args.snapshot, records, llm_extract)
        if args.evaluation_records:
            from semantic_evaluation import load_json
            files = (("evaluation.jsonl", merge_labels(
                load_jsonl(args.evaluation_records), records, load_jsonl(args.unblind_key),
                args.method, load_json(args.manifest))),)
        elif args.unblind_key:
            files = (("reviewed.jsonl", unblind(records, load_jsonl(args.unblind_key))),)
        else:
            worksheet, key = prepare(records)
            files = (("coordinator-key.jsonl", key), ("worksheet.jsonl", worksheet))
        for name, rows in files:
            path = directory / name
            with open(path, "x", encoding="utf-8", opener=lambda p, flags: os.open(p, flags, 0o600)) as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
    except (EvaluationError, OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        print("blind packet creation failed; inspect input and output path")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
