"""Jev value evaluation dispatches only checked-in synthetic cases."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

import mcs_setup
import semantic_evaluation
import semantic_jev


def _case(name="extract_cases.json"):
    asset = Path(mcs_setup.__file__).resolve().parents[2] / "evaluation" / name
    return deepcopy(json.loads(asset.read_text())["cases"][0])


def _isolate(monkeypatch):
    calls = []
    monkeypatch.setattr(mcs_setup, "load_config", lambda: calls.append("config") or {})
    monkeypatch.setattr(mcs_setup, "env_value", lambda name: calls.append("credentials") or "synthetic-key")
    monkeypatch.setattr(semantic_jev, "JevClient", lambda **kw: calls.append("client") or object())
    def evaluate(cases, llm_fn, client, **kw):
        calls.append(("evaluate", cases, kw))
        return {"cases": len(cases), "evaluated": True,
                "audit": {"deterministic_findings": 0, "jev_incremental_findings": 0}}
    monkeypatch.setattr(semantic_evaluation, "evaluate_jev_incremental", evaluate)
    return calls


@pytest.mark.parametrize("alteration", ["unknown", "body", "expect", "duplicate", "id-type"])
def test_unbound_cases_are_refused_before_credentials_or_dispatch(tmp_path, monkeypatch, alteration):
    case = _case()
    if alteration == "unknown":
        case["id"] = "synthetic-unbound-case"
    elif alteration == "body":
        case["body"] = "完全合成の改変本文"
    elif alteration == "expect":
        case["expect"] = {"facts": [{"statement": "完全合成の改変期待値"}]}
    elif alteration == "id-type":
        case["id"] = []
    cases = [case, deepcopy(case)] if alteration == "duplicate" else [case]
    source, out = tmp_path / "cases.json", tmp_path / "report.json"
    source.write_text(json.dumps({"source": "fully_synthetic", "cases": cases}))
    calls = _isolate(monkeypatch)
    assert mcs_setup.main(["jev-value", "--cases", str(source), "--out", str(out)]) == 1
    assert calls == [] and not out.exists()


@pytest.mark.parametrize("deadline", ["nan", "inf", "-inf", "0", "-1"])
def test_invalid_deadline_is_refused_before_dispatch(tmp_path, monkeypatch, deadline):
    source, out = tmp_path / "cases.json", tmp_path / "report.json"
    source.write_text(json.dumps({"cases": [_case()]}))
    calls = _isolate(monkeypatch)
    assert mcs_setup.main(["jev-value", "--cases", str(source), "--out", str(out),
                           "--deadline=" + deadline]) == 1
    assert calls == [] and not out.exists()


@pytest.mark.parametrize("contents", [b"[" * 10000 + b"]" * 10000, b'{"cases":["\xff"]}'],
                         ids=["deep-json", "invalid-utf8"])
def test_unreadable_json_is_a_clean_refusal(tmp_path, monkeypatch, contents):
    source, out = tmp_path / "cases.json", tmp_path / "report.json"
    source.write_bytes(contents)
    calls = _isolate(monkeypatch)
    assert mcs_setup.main(["jev-value", "--cases", str(source), "--out", str(out)]) == 1
    assert calls == [] and not out.exists()


@pytest.mark.parametrize("asset", ["extract_cases.json", "extract_cases_labs.json",
                                  "semantic_completeness_cases.json"])
def test_copied_synthetic_subset_preserves_evaluation_and_report(tmp_path, monkeypatch, asset):
    source, out = tmp_path / "cases.json", tmp_path / "report.json"
    cases = [_case(asset)]
    source.write_text(json.dumps({"cases": cases}))
    calls = _isolate(monkeypatch)
    assert mcs_setup.main(["jev-value", "--cases", str(source), "--out", str(out)]) == 0
    assert calls == ["config", "credentials", "client", ("evaluate", cases, {"deadline_s": 120.0})]
    assert json.loads(out.read_text())["label_provenance"] == {"human": 0, "synthetic": 1}


def test_missing_synthetic_asset_refuses_before_credentials(tmp_path, monkeypatch):
    source, out = tmp_path / "cases.json", tmp_path / "report.json"
    source.write_text(json.dumps({"cases": [_case()]}))
    calls = _isolate(monkeypatch)
    def missing(*args, **kwargs):
        raise OSError("synthetic-asset-missing")
    monkeypatch.setattr(Path, "read_text", missing)
    assert mcs_setup.main(["jev-value", "--cases", str(source), "--out", str(out)]) == 1
    assert calls == [] and not out.exists()
