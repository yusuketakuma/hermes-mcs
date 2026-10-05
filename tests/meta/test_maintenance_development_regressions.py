"""開発入口の検査を一時文書・合成PNG・通信スタブだけで確認する。"""

import copy
import importlib.util
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import zlib

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts/development"
sys.path.insert(0, str(SCRIPTS))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", ["modules", "signals", "stats", "cli", "release"])
@pytest.mark.parametrize("kind", ["begin_only", "end_only", "duplicate", "reversed"])
def test_generated_marker_damage_keeps_manual_text(monkeypatch, name, kind):
    module = _load("update_readme")
    begin = f"<!-- BEGIN GENERATED:{name} -->"
    end = f"<!-- END GENERATED:{name} -->"
    cases = {"begin_only": begin, "end_only": end,
             "duplicate": f"{begin}\nold\n{end}\nmanual\n{begin}\nold\n{end}",
             "reversed": f"{end}\nmanual\n{begin}"}
    old = "preface\n" + cases[kind] + "\nepilogue\n"
    calls = []
    monkeypatch.setitem(module.GENERATORS, name, lambda: calls.append(name) or "fresh")
    new, failed = module.render(old)
    assert failed == [name]
    assert new == old
    assert calls == []


@pytest.mark.parametrize("check", [False, True])
def test_missing_all_generated_docs_is_not_success(monkeypatch, tmp_path, capsys, check):
    module = _load("update_readme")
    monkeypatch.setattr(module, "README", tmp_path / "README.md")
    monkeypatch.setattr(module, "DEV_DOC", tmp_path / "DEVELOPMENT.md")
    monkeypatch.setattr(sys, "argv", ["update_readme.py"] + (["--check"] if check else []))
    assert module.main() == 1
    assert "no documentation targets" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


def test_marker_failure_in_second_doc_does_not_write_first(monkeypatch, tmp_path):
    module = _load("update_readme")
    readme, dev = tmp_path / "README.md", tmp_path / "DEVELOPMENT.md"
    old = "<!-- BEGIN GENERATED:signals -->\nold\n<!-- END GENERATED:signals -->\n"
    readme.write_text(old)
    dev.write_text("<!-- BEGIN GENERATED:stats -->\nmanual\n")
    monkeypatch.setattr(module, "README", readme)
    monkeypatch.setattr(module, "DEV_DOC", dev)
    monkeypatch.setitem(module.GENERATORS, "signals", lambda: "fresh")
    monkeypatch.setattr(sys, "argv", ["update_readme.py"])
    assert module.main() == 1
    assert readme.read_text() == old
    assert dev.read_text().endswith("manual\n")


def test_single_enabled_doc_and_absent_sections_remain_supported(monkeypatch, tmp_path):
    module = _load("update_readme")
    dev = tmp_path / "DEVELOPMENT.md"
    dev.write_text("manual\n<!-- BEGIN GENERATED:signals -->\nold\n<!-- END GENERATED:signals -->\n")
    monkeypatch.setattr(module, "README", tmp_path / "absent.md")
    monkeypatch.setattr(module, "DEV_DOC", dev)
    monkeypatch.setitem(module.GENERATORS, "signals", lambda: "fresh")
    monkeypatch.setattr(sys, "argv", ["update_readme.py"])
    assert module.main() == 0
    assert "fresh" in dev.read_text() and dev.read_text().startswith("manual\n")


def _png(color, filter_byte):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    width, height = 1650, 3
    pixels = bytes(width * (3 if color == 2 else 4))
    raw = (bytes([filter_byte]) + pixels) * height
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


@pytest.mark.parametrize("color", [2, 6])
@pytest.mark.parametrize("filter_byte", [5, 9, 255])
def test_png_check_rejects_invalid_scanline_filters(tmp_path, color, filter_byte):
    module = _load("generate_slack_gallery")
    path = tmp_path / "synthetic.png"
    path.write_bytes(_png(color, filter_byte))
    with pytest.raises(ValueError, match="PNG image data"):
        module.verify_png(path, '<svg height="2"/>')


@pytest.mark.parametrize("color", [2, 6])
@pytest.mark.parametrize("filter_byte", range(5))
def test_png_check_keeps_all_standard_filters(tmp_path, color, filter_byte):
    module = _load("generate_slack_gallery")
    path = tmp_path / "synthetic.png"
    path.write_bytes(_png(color, filter_byte))
    module.verify_png(path, '<svg height="2"/>')


def _release_fixture(tmp_path):
    notes = _load("release_notes")
    item = dict(category="fixed", title="合成修正", summary="合成通知を修正します。",
                upgrade="追加操作は不要です。", details=[], refs=["#1"])
    text = "## [Unreleased]\n\n## [1.0.0] — 2026-09-21\n\n" + notes.render(
        [item], "合成修正", "合成の検査です。")
    (tmp_path / "CHANGELOG.md").write_text(text)
    return notes, text


@pytest.mark.parametrize("driver", ["release_notes", "readme_release", "sync_release_notes"])
def test_deep_json_cli_has_controlled_offline_diagnostic(monkeypatch, tmp_path, capsys, driver):
    _, text = _release_fixture(tmp_path)
    depth = sys.getrecursionlimit() + 100
    deep = "[" * depth + "0" + "]" * depth
    module = _load(driver)
    if driver == "release_notes":
        changes = tmp_path / "changes"
        changes.mkdir()
        (changes / "synthetic.json").write_text(deep)
        with pytest.raises(SystemExit) as error:
            module.main(["--root", str(tmp_path), "check"])
        assert error.value.code == 1
    elif driver == "readme_release":
        (tmp_path / "README.md").write_text(module.render(module.BEGIN + "\n" + module.END, text))
        review = tmp_path / "docs/development"
        review.mkdir(parents=True)
        (review / "readme-review.json").write_text(deep)
        assert module.main(["--root", str(tmp_path), "--check"]) == 1
    else:
        def api(*args, **kwargs):
            return json.loads(deep)
        monkeypatch.setattr(module, "api", api)
        with pytest.raises(SystemExit) as error:
            module.main(["--root", str(tmp_path), "--repo", "synthetic/repo"])
        assert error.value.code == 1
    diagnostic = capsys.readouterr().err
    assert diagnostic and "Traceback" not in diagnostic and deep not in diagnostic


@pytest.mark.parametrize("bad", [None, [], {}, {"tag_name": 7}, {"draft": "false"},
                                  {"prerelease": 0}, {"target_commitish": None},
                                  {"created_at": None}, {"updated_at": None},
                                  {"published_at": 7}, {"body": []}])
def test_release_response_contract_rejected_before_any_write(tmp_path, bad):
    _, text = _release_fixture(tmp_path)
    module = _load("sync_release_notes")
    valid = dict(id=1, tag_name="v1.0.0", name="old", body="old", draft=False,
                 prerelease=False, target_commitish="main", created_at="a",
                 published_at="b", updated_at="c")
    invalid = valid | bad if isinstance(bad, dict) and bad else bad
    writes = []
    def request(endpoint, payload=None):
        if "?" in endpoint:
            return [copy.deepcopy(invalid)]
        if payload is not None:
            writes.append(payload)
            invalid.update(payload)
        return copy.deepcopy(invalid)
    with pytest.raises(ValueError):
        module.synchronize(text, request, apply=True)
    assert writes == []


@pytest.mark.parametrize("budget", ["nan", "inf", "-inf", "0", "-1"])
def test_shadow_cli_rejects_nonfinite_or_nonpositive_budget_before_model(
        monkeypatch, tmp_path, budget):
    module = _load("semantic_shadow_e2e")
    cases, output = tmp_path / "synthetic.json", tmp_path / "report.json"
    cases.write_text(json.dumps({"cases": [{"id": "synthetic", "body": "合成の投稿"}]}))
    calls = []
    monkeypatch.setitem(sys.modules, "semantic", SimpleNamespace(llm_chat=None))
    monkeypatch.setitem(sys.modules, "semantic_evaluation", SimpleNamespace(
        run_shadow_e2e=lambda *a, **kw: calls.append(kw) or {"cases": 1, "complete": 0}))
    monkeypatch.setattr(sys, "argv", ["shadow", "--cases", str(cases), "--out", str(output),
                                      f"--deadline={budget}"])
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
    assert calls == [] and not output.exists()


@pytest.mark.parametrize("budget", ["0.25", "300"])
@pytest.mark.parametrize("thread", [False, True])
def test_shadow_cli_preserves_valid_operator_fixture_and_budget(monkeypatch, tmp_path, budget, thread):
    module = _load("semantic_shadow_e2e")
    cases, output = tmp_path / "synthetic.json", tmp_path / "report.json"
    synthetic = [{"id": "synthetic-operator", "body": "合成の投稿"}]
    if thread:
        synthetic = [{"id": "synthetic-thread", "messages": [{"body": "合成の投稿"}]}]
    cases.write_text(json.dumps({"cases": synthetic}))
    calls = []
    def run(actual, model, client, **kwargs):
        calls.append((actual, model, client, kwargs))
        return {"cases": 1, "complete": 0}
    model = object()
    monkeypatch.setitem(sys.modules, "semantic", SimpleNamespace(llm_chat=model))
    monkeypatch.setitem(sys.modules, "semantic_evaluation", SimpleNamespace(run_shadow_e2e=run))
    monkeypatch.setattr(sys, "argv", ["shadow", "--cases", str(cases), "--out", str(output),
                                      f"--deadline={budget}"])
    assert module.main() == 0
    assert calls == [(synthetic, model, None, {"deadline_s": float(budget)})]
    assert json.loads(output.read_text()) == {"cases": 1, "complete": 0}


@pytest.mark.parametrize("kind", ["deep", "null_case", "number_case", "null_message", "string_messages"])
def test_shadow_cli_malformed_cases_stop_before_model(monkeypatch, tmp_path, capsys, kind):
    module = _load("semantic_shadow_e2e")
    cases, output = tmp_path / "synthetic.json", tmp_path / "report.json"
    depth = sys.getrecursionlimit() + 100
    malformed = {"deep": '{"cases":[' + "[" * depth + "0" + "]" * depth + "]}",
                 "null_case": '{"cases":[null]}', "number_case": '{"cases":[7]}',
                 "null_message": '{"cases":[{"messages":[null]}]}',
                 "string_messages": '{"cases":[{"messages":"synthetic"}]}'}[kind]
    cases.write_text(malformed)
    calls = []
    monkeypatch.setitem(sys.modules, "semantic", SimpleNamespace(llm_chat=None))
    monkeypatch.setitem(sys.modules, "semantic_evaluation", SimpleNamespace(
        run_shadow_e2e=lambda *a, **kw: calls.append(kw) or {"cases": 1, "complete": 0}))
    monkeypatch.setattr(sys, "argv", ["shadow", "--cases", str(cases), "--out", str(output)])
    assert module.main() == 1
    assert calls == [] and not output.exists()
    assert "Traceback" not in capsys.readouterr().err
