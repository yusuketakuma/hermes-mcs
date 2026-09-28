import copy
import json
import stat

import pytest

import semantic_blind as blind
from semantic_evaluation import EvaluationError


def test_blind_packet_separates_key_and_refuses_mixed_inputs(tmp_path):
    row = {"case_id": "c1", "account_id": "a", "project_id": 1, "split": "test",
           "bundle_fingerprint": "fixed", "source_text": "synthetic source",
           "outputs": {m: {"bundle_fingerprint": "fixed", "text": "text " + str(i)}
                       for i, m in enumerate(blind.METHODS)}}
    source = tmp_path / "input.jsonl"
    source.write_text(json.dumps(row) + "\n")
    output = tmp_path / "packet"
    assert blind.main(["--input", str(source), "--output-dir", str(output)]) == 0
    worksheet = json.loads((output / "worksheet.jsonl").read_text())
    key = json.loads((output / "coordinator-key.jsonl").read_text())
    assert worksheet["review_id"] == key["review_id"]
    assert worksheet["human_labels"] is None
    assert set(worksheet) == {"review_id", "source_text", "choices", "human_labels"}
    for choice in worksheet["choices"]:
        assert choice["text"] == row["outputs"][key["methods"][choice["label"]]]["text"]
        assert choice["claims"] == [{"claim_id": "c1", "text": choice["text"]}]
        assert choice["limitations"] == []
    assert stat.S_IMODE((output / "coordinator-key.jsonl").stat().st_mode) == 0o600
    assert blind.main(["--input", str(source), "--output-dir", str(output)]) == 2
    wrong = copy.deepcopy(row)
    wrong["outputs"]["baseline"]["bundle_fingerprint"] = "other"
    with pytest.raises(EvaluationError, match="output_bundle_mismatch"):
        blind.prepare([wrong])
    wrong = copy.deepcopy(row)
    wrong.update(case_id="c2", split="calibration")
    with pytest.raises(EvaluationError, match="patient_split_leak"):
        blind.prepare([row, wrong])


def test_fixed_bundle_outputs_bind_artifacts_before_baseline_call(tmp_path):
    import semantic
    from semantic_testkit import _seeded
    db = _seeded(tmp_path)
    try:
        bundle = semantic.thread_bundle(db, 1, 1)
        target = bundle["members"][0]
        meta = {"fingerprint": bundle["source_fingerprint"],
                "target_revision": target["revision"], "policy_fingerprint": "policy",
                "publication_mode": "shadow"}
        candidate = {"message_id": 1, "project_id": 1,
                     "meta": dict(meta, stage="pre_audit"),
                     "content": {"claims": [{"text": "draft"}], "limitations": [],
                                 "target_message_id": 1, "input_bundle_id": bundle["bundle_id"]}}
        final = copy.deepcopy(candidate)
        final["meta"] = dict(meta, audit_status="NEEDS_REVIEW")
        final["content"]["claims"][0]["text"] = "final"
        calls = []
        def baseline(body, **_):
            calls.append(body)
            return {"summary": "baseline"}
        outputs = blind.fixed_bundle_outputs(bundle, 1, candidate, final, baseline)
        assert calls == [target["body_original"]]
        assert outputs["assisted"]["text"] == "draft"
        assert outputs["audited"]["text"] == "final"
        swapped = copy.deepcopy(final)
        swapped["content"]["target_message_id"] = 2
        with pytest.raises(EvaluationError, match="artifact_content_source_mismatch"):
            blind.fixed_bundle_outputs(bundle, 1, candidate, swapped, baseline)
        assert len(calls) == 1
        mixed = copy.deepcopy(bundle)
        mixed["members"][1]["project_id"] = 99
        mixed["source_fingerprint"] = semantic.bundle_fingerprint(mixed["members"])
        mixed_candidate, mixed_final = copy.deepcopy(candidate), copy.deepcopy(final)
        for artifact in (mixed_candidate, mixed_final):
            artifact["meta"]["fingerprint"] = mixed["source_fingerprint"]
        with pytest.raises(EvaluationError, match="bundle_scope_invalid"):
            blind.fixed_bundle_outputs(mixed, 1, mixed_candidate, mixed_final, baseline)
        assert len(calls) == 1
        final["meta"]["target_revision"] = "changed"
        with pytest.raises(EvaluationError, match="artifact_source_mismatch"):
            blind.fixed_bundle_outputs(bundle, 1, candidate, final, baseline)
        assert len(calls) == 1
    finally:
        db.close()


def test_snapshot_cli_uses_explicit_artifacts_without_database_writes(tmp_path, monkeypatch):
    import time
    import semantic
    import extract_llm
    from semantic_testkit import _seeded, _cfg, _FakeJev, _llm
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.execute("INSERT INTO attachments(message_id,file_id,name,url,local_path,state) "
                          "VALUES(1,'f','scan.pdf','https://invalid.example/secret','/private/secret','pending')")
        db.semantic_seed(1, [1, 2], {"source": "replay"})
        result = semantic.run_due(db, _cfg(), {"errors": []}, time.monotonic() + 60,
                                  jev_client=_FakeJev(), llm_fn=_llm)
        assert result["done"] == 1
        selection = {"case_id": "c", "account_id": "synthetic", "project_id": 1, "split": "test"}
        for field, kind in (("bundle_id", "semantic_bundle"), ("candidate_id", "semantic_candidate"),
                            ("final_id", "semantic_summary")):
            selection[field] = db.artifacts(kind, message_id=1)[-1]["artifact_id"]
        before = list(db.db.iterdump())
        calls = []
        def baseline(body, **_):
            calls.append(body)
            return {"summary": "synthetic baseline"}
        monkeypatch.setattr(extract_llm, "llm_extract", baseline)
        source = tmp_path / "selections.jsonl"
        source.write_text(json.dumps(selection) + "\n")
        args = ["--input", str(source), "--output-dir", str(tmp_path / "packet"),
                "--snapshot", str(tmp_path / "ledger.db"), "--generate-local-baseline"]
        assert blind.main(args) == 0 and len(calls) == 1
        worksheet = json.loads((tmp_path / "packet" / "worksheet.jsonl").read_text())
        key = json.loads((tmp_path / "packet" / "coordinator-key.jsonl").read_text())
        assert key["artifact_ids"] == {k: selection[k] for k in
                                      ("bundle_id", "candidate_id", "final_id")}
        assert "artifact_ids" not in worksheet
        import hashlib
        digest = hashlib.sha256(json.dumps(worksheet, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest()
        assert key["worksheet_sha256"] == digest
        context = json.loads(worksheet["source_text"])
        assert context["target_message_id"] == 1
        assert context["attachments_interpreted"] is False
        assert context["context_complete"] is True
        assert context["missing_replies"] == 0
        assert "https://invalid.example/secret" not in worksheet["source_text"]
        assert "/private/secret" not in worksheet["source_text"]
        parent, reply = context["messages"]
        assert reply["parent_id"] == parent["message_id"]
        assert parent["posted_at"] and parent["sender"]["id"] == 1
        assert parent["body_original"] == calls[0]
        assert parent["attachments"][0]["name"] == "scan.pdf"
        assert blind.main(args) == 2 and len(calls) == 1
        assert list(db.db.iterdump()) == before
        wrong = dict(selection, case_id="other", split="calibration")
        with pytest.raises(EvaluationError, match="patient_split_leak"):
            blind.snapshot_records(str(tmp_path / "ledger.db"), [selection, wrong], baseline)
        assert len(calls) == 1
    finally:
        db.close()


def test_unblind_rejects_modified_text_and_preserves_human_judgments(tmp_path):
    row = {"case_id": "c", "account_id": "a", "project_id": 1, "split": "test",
           "bundle_fingerprint": "fp", "source_text": "source",
           "outputs": {m: {"bundle_fingerprint": "fp", "text": m} for m in blind.METHODS}}
    worksheets, keys = blind.prepare([row])
    with pytest.raises(EvaluationError):
        blind.unblind(worksheets, keys)
    worksheets[0]["human_labels"] = {label: {"supported": label != "B"} for label in "ABC"}
    restored, = blind.unblind(worksheets, keys)
    for label, method in keys[0]["methods"].items():
        assert restored["labels_by_method"][method] == worksheets[0]["human_labels"][label]
    source, keyfile = tmp_path / "completed.jsonl", tmp_path / "key.jsonl"
    source.write_text(json.dumps(worksheets[0]) + "\n")
    keyfile.write_text(json.dumps(keys[0]) + "\n")
    output = tmp_path / "reviewed"
    assert blind.main(["--input", str(source), "--unblind-key", str(keyfile),
                       "--output-dir", str(output)]) == 0
    persisted = json.loads((output / "reviewed.jsonl").read_text())
    assert persisted == restored
    assert persisted["account_id"] == row["account_id"]
    assert persisted["project_id"] == keys[0]["project_id"]
    assert persisted["worksheet_sha256"] == keys[0]["worksheet_sha256"]
    assert stat.S_IMODE((output / "reviewed.jsonl").stat().st_mode) == 0o600
    worksheets[0]["choices"][0]["text"] = "changed"
    with pytest.raises(EvaluationError, match="worksheet_changed"):
        blind.unblind(worksheets, keys)


def test_review_labels_merge_into_exact_candidate_and_keep_synthetic_provenance(tmp_path):
    import semantic_evaluation as evaluation
    from semantic_testkit import _record, MANIFEST, CRITERIA
    record = _record()
    record["bundle"]["fingerprint"] = "fixed"
    labels = record.pop("label")
    for i, claim in enumerate(record["candidate"]["claims"]):
        claim["text"] = "statement " + str(i)
    texts = [c["text"] for c in record["candidate"]["claims"]]
    case = {key: record[key] for key in ("case_id", "account_id", "project_id", "split")}
    case.update(bundle_fingerprint="fixed", source_text="synthetic source",
                outputs={method: {"bundle_fingerprint": "fixed", "text": "\n".join(texts),
                                  "claim_texts": texts, "evaluation_candidate": copy.deepcopy(record["candidate"])}
                         for method in blind.METHODS})
    sheets, keys = blind.prepare([case])
    for choice in sheets[0]["choices"]:
        assert choice["predictions"] == {field: record["candidate"].get(field)
                                          for field in ("facts", "loops", "status")}
    sheets[0]["human_labels"] = {label: copy.deepcopy(labels) for label in "ABC"}
    merged = blind.merge_labels([record], sheets, keys, "audited", MANIFEST)
    assert merged[0]["label"] == labels
    assert "label" not in record
    assert not evaluation.evaluate_records(merged, MANIFEST, CRITERIA)["gate"]["pass"]
    paths = {name: tmp_path / name for name in ("sheets", "keys", "records", "manifest", "criteria")}
    for name, value in (("sheets", sheets[0]), ("keys", keys[0]), ("records", record),
                        ("manifest", MANIFEST), ("criteria", CRITERIA)):
        paths[name].write_text(json.dumps(value) + "\n")
    output = tmp_path / "merged"
    assert blind.main(["--input", str(paths["sheets"]), "--unblind-key", str(paths["keys"]),
                       "--evaluation-records", str(paths["records"]), "--manifest", str(paths["manifest"]),
                       "--method", "audited", "--output-dir", str(output)]) == 0
    report_path = tmp_path / "report.json"
    assert evaluation.main(["--input", str(output / "evaluation.jsonl"),
                            "--manifest", str(paths["manifest"]), "--criteria", str(paths["criteria"]),
                            "--output", str(report_path)]) == 0
    report = json.loads(report_path.read_text())
    assert not report["gate"]["g6_eligible"]
    assert report["label_provenance"]["synthetic"] == 1
    assert report["metrics"]["final_recall"]["denominator"] == 2
    for field, replacement in (("facts", []), ("loops", [{"loop_id": "invented", "resolved": True}]),
                               ("usage", {"requests": 0}), ("status", "DEFER")):
        changed = copy.deepcopy(record)
        changed["candidate"][field] = replacement
        with pytest.raises(EvaluationError, match="evaluation_candidate_mismatch"):
            blind.merge_labels([changed], sheets, keys, "audited", MANIFEST)
    unbound = copy.deepcopy(case)
    for output_row in unbound["outputs"].values():
        output_row.pop("evaluation_candidate")
    old_sheets, old_keys = blind.prepare([unbound])
    old_sheets[0]["human_labels"] = sheets[0]["human_labels"]
    with pytest.raises(EvaluationError, match="evaluation_candidate_not_frozen"):
        blind.merge_labels([record], old_sheets, old_keys, "audited", MANIFEST)
    record["candidate"]["claims"][0]["text"] = "unreviewed replacement"
    with pytest.raises(EvaluationError, match="evaluation_claims_mismatch"):
        blind.merge_labels([record], sheets, keys, "audited", MANIFEST)


def test_candidate_on_a_subset_of_methods_is_refused():
    """U06-F08: predictions appear only on choices whose output carries
    an evaluation_candidate — a subset would reveal the method behind a
    label, so prepare requires all three or none."""
    from semantic_testkit import _record
    record = _record()
    for i, claim in enumerate(record["candidate"]["claims"]):
        claim["text"] = "statement " + str(i)
    texts = [c["text"] for c in record["candidate"]["claims"]]
    outputs = {method: {"bundle_fingerprint": "fixed", "text": "\n".join(texts),
                        "claim_texts": texts} for method in blind.METHODS}
    outputs[blind.METHODS[0]]["evaluation_candidate"] = copy.deepcopy(record["candidate"])
    case = {key: record[key] for key in ("case_id", "account_id", "project_id", "split")}
    case.update(bundle_fingerprint="fixed", source_text="synthetic source", outputs=outputs)
    with pytest.raises(EvaluationError, match="evaluation_candidate_partial"):
        blind.prepare([case])
