"""Offline contracts for the live detection command's reported outcome."""
from types import SimpleNamespace
import json

import pytest

import semantic_bench as bench


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
