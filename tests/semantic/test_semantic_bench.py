"""Offline contracts for the live detection command's reported outcome."""
from types import SimpleNamespace
import json
from pathlib import Path

import pytest

import semantic_bench as bench


def test_corpus_cli_freezes_only_checked_in_synthetic_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "LedgerReader", lambda *_: pytest.fail("ledger accessed"))
    output = tmp_path / "corpus.json"
    monkeypatch.setattr("sys.argv", ["semantic_bench", "corpus", "--n", "25", "--out", str(output)])
    assert bench.main() == 0
    corpus = json.loads(output.read_text())
    sources = json.loads((Path(__file__).resolve().parents[2] /
                          "evaluation/extract_cases.json").read_text())["cases"][:25]
    assert corpus["corpus_version"] == 1 and len(corpus["cases"]) == 25
    assert corpus["source"] == "fully_synthetic"
    for case, fixture in zip(corpus["cases"], sources, strict=True):
        assert case["members"][-1]["body_original"] == fixture["body"]
        assert case["target_id"] == case["members"][-1]["message_id"]
    assert output.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("damage", ["legacy", "marked_but_unbound"])
@pytest.mark.parametrize("live_jev", [False, True])
def test_run_refuses_unbound_corpus_before_model_or_credentials(
        tmp_path, monkeypatch, damage, live_jev):
    import semantic
    member = {"message_id": 1, "body_original": "合成の未登録本文", "revision": "r1"}
    corpus = {"corpus_version": 1, "cases": [{"case_id": "old", "project_id": 1,
              "root_id": 1, "target_id": 1, "members": [member]}]}
    if damage == "marked_but_unbound":
        corpus["source"] = "fully_synthetic"
    source, output = tmp_path / "input.json", tmp_path / "result.json"
    source.write_text(json.dumps(corpus))
    monkeypatch.setattr(semantic, "llm_chat", lambda *_: pytest.fail("model dispatched"))
    monkeypatch.setattr(bench, "env_value", lambda *_: pytest.fail("credentials read"))
    assert bench.cmd_run(SimpleNamespace(corpus=source, out=str(output), tag="synthetic",
                                        job_budget=1, jev=live_jev)) == 2
    assert not output.exists()


def test_run_accepts_frozen_synthetic_corpus_with_injected_pipeline(tmp_path, monkeypatch):
    source, output = tmp_path / "input.json", tmp_path / "result.json"
    monkeypatch.setattr(bench, "LedgerReader", lambda *_: pytest.fail("ledger accessed"))
    assert bench.cmd_corpus(SimpleNamespace(n=2, out=str(source))) == 0
    import semantic
    calls = []
    def llm(prompt, **_):
        calls.append(prompt)
        return json.dumps({"facts": [], "claims": [], "limitations": []})
    monkeypatch.setattr(semantic, "llm_chat", llm)
    assert bench.cmd_run(SimpleNamespace(corpus=source, out=str(output), tag="synthetic",
                                        job_budget=1, jev=False)) == 0
    result = json.loads(output.read_text())
    assert len(calls) == 4 and result["aggregate"]["cases"] == 2
    assert all(case["facts_complete"] for case in result["results"])


@pytest.mark.parametrize("count", [-1, 0, True])
def test_corpus_rejects_invalid_count_without_ledger_or_output(tmp_path, monkeypatch, count):
    monkeypatch.setattr(bench, "LedgerReader", lambda *_: pytest.fail("ledger accessed"))
    output = tmp_path / "corpus.json"
    assert bench.cmd_corpus(SimpleNamespace(n=count, out=str(output))) == 2
    assert not output.exists()


def test_run_rejects_malformed_json_without_dispatch_or_output(tmp_path, monkeypatch):
    source, output = tmp_path / "input.json", tmp_path / "result.json"
    source.write_text("{synthetic invalid json")
    monkeypatch.setattr(bench, "env_value", lambda *_: pytest.fail("credentials read"))
    assert bench.cmd_run(SimpleNamespace(corpus=source, out=str(output), jev=True)) == 2
    assert not output.exists()


def test_unavailable_synthetic_asset_cannot_fall_back_to_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "LedgerReader", lambda *_: pytest.fail("ledger accessed"))
    monkeypatch.setattr(bench.Path, "read_text", lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError()))
    output = tmp_path / "corpus.json"
    assert bench.cmd_corpus(SimpleNamespace(n=1, out=str(output))) == 2
    assert not output.exists()


@pytest.mark.parametrize("unevaluated", [0, 1, 2])
def test_detection_requires_every_probe_to_be_evaluated(monkeypatch, capsys, unevaluated):
    probes = [bench._probe_case("reject", "source", "claim", True),
              bench._probe_case("support", "source", "claim", False)]
    replies = iter([([], False) if i < unevaluated else (
        [{"code": "claim_not_supported"}] if p["expect_reject"] else [], True)
        for i, p in enumerate(probes)])
    monkeypatch.setattr(bench, "_PROBES", probes)
    monkeypatch.setattr(bench, "env_value", lambda name: "synthetic")
    monkeypatch.setattr(bench.jev, "JevClient", lambda **kw: SimpleNamespace(requests_made=2))
    monkeypatch.setattr(bench, "audit_claims", lambda *args: next(replies))

    assert bench.cmd_detect(SimpleNamespace()) == (1 if unevaluated else 0)
    assert f"unevaluated={unevaluated}" in capsys.readouterr().out


def test_private_output_replaces_link_without_changing_target(tmp_path):
    target = tmp_path / "other.json"
    target.write_text("unchanged")
    output = tmp_path / "output.json"
    output.symlink_to(target)
    bench._write_json_private(str(output), {"synthetic": True})
    assert target.read_text() == "unchanged"
    assert not output.is_symlink()
    assert json.loads(output.read_text()) == {"synthetic": True}
    assert output.stat().st_mode & 0o777 == 0o600


def test_serialization_failure_preserves_previous_output(tmp_path):
    output = tmp_path / "output.json"
    output.write_text("previous")
    with pytest.raises(TypeError):
        bench._write_json_private(str(output), {"bad": object()})
    assert output.read_text() == "previous"


def test_calibration_skips_malformed_or_nonprobability_records(monkeypatch, capsys):
    meta = [[], {"claim_audit": []}, {"claim_audit": {"bad": None}},
            {"claim_audit": {"bad": {"confidence": True}}},
            {"claim_audit": {"bad": {"confidence": float("nan")}}},
            {"claim_audit": {"bad": {"confidence": 10**1000}}},
            {"claim_audit": {"ok": {"confidence": 0.9, "choice": "supports"}}}]
    fake = SimpleNamespace(
        db=SimpleNamespace(execute=lambda _: SimpleNamespace(
            fetchall=lambda: [(json.dumps(value),) for value in meta])),
        close=lambda: None)
    monkeypatch.setattr(bench, "LedgerReader", lambda _: fake)
    assert bench.cmd_calibrate(SimpleNamespace()) == 0
    assert "1 claim verdicts collected" in capsys.readouterr().out


def test_report_delta_uses_the_same_reads_as_the_table(tmp_path, monkeypatch, capsys):
    paths = [tmp_path / f"{tag}.json" for tag in ("first", "second")]
    for path, count in zip(paths, (2, 1)):
        path.write_text(json.dumps({"tag": path.stem, "aggregate": {
            "cases": 1, "claims": 2, "findings_total": count,
            "findings_per_claim": count / 2, "finding_codes": {"synthetic": count}}}))
    read_text = bench.Path.read_text
    reads = []

    def read_once(path, *args, **kwargs):
        assert path not in reads
        reads.append(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(bench.Path, "read_text", read_once)
    assert bench.cmd_report(SimpleNamespace(results=paths)) == 0
    output = capsys.readouterr().out
    assert "[first] cases=1 claims=2 findings=2 per-claim=1.0" in output
    assert "[second] cases=1 claims=2 findings=1 per-claim=0.5" in output
    assert "synthetic: 2 -> 1" in output
    assert reads == paths
