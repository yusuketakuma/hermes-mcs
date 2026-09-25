"""Offline contracts for the live detection command's reported outcome."""
from types import SimpleNamespace

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
