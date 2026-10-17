"""Explicit dictionary changes validate synthetic files before setup writes."""
import copy
import hashlib
import json

import pytest

import drug_map
import mcs_setup
from test_drug_map import DOCUMENT, _source_document
from test_mcs_setup import _init_env


def _dictionary(tmp_path, name, document=None):
    path = tmp_path / name
    raw = json.dumps(DOCUMENT if document is None else document, ensure_ascii=False).encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


@pytest.fixture
def setup_state(tmp_path, monkeypatch):
    old = _dictionary(tmp_path, "old.json")
    cfg = {"mcs_login_id": "fictional-user", "notify_target": "slack", "drug_map": old}
    _init_env(monkeypatch, tmp_path, cfg)
    monkeypatch.setattr(mcs_setup, "load_config", lambda: copy.deepcopy(cfg))
    files = (tmp_path / "c.json", tmp_path / ".env", tmp_path / "shared-settings.json")
    files[0].write_text(json.dumps(cfg))
    files[1].write_text("MCS_PASSWORD=fictional-old-password\n")
    files[2].write_text('{"fictional_existing_setting":true}')
    events = []
    monkeypatch.setenv("MCS_SETUP_PASSWORD", "fictional-new-password")
    monkeypatch.setattr(mcs_setup, "_keychain_store",
                        lambda *args: events.append("keychain") or True)

    def integration(config, args):
        events.append("shared-settings")
        files[2].write_text('{"fictional_new_setting":true}')
        return True

    monkeypatch.setattr(mcs_setup, "_apply_plugin_integration", integration)
    monkeypatch.setattr(mcs_setup.subprocess, "run", lambda *a, **k: pytest.fail("service access"))
    return cfg, files, events


def _run(monkeypatch, settings):
    argv = ["mcs_setup", "init", "--yes"]
    for value in settings:
        argv.extend(("--set", value))
    monkeypatch.setattr(mcs_setup.sys, "argv", argv)
    return mcs_setup.main()


@pytest.mark.parametrize("damage,reason", [
    ("missing", "missing_dictionary"), ("sha", "invalid_dictionary"),
    ("malformed", "invalid_dictionary"), ("shared", "invalid_dictionary"),
    ("symlink", "invalid_dictionary"), ("directory", "invalid_dictionary"),
])
def test_invalid_explicit_dictionary_preserves_all_writes(
        tmp_path, monkeypatch, setup_state, capsys, damage, reason):
    cfg, files, events = setup_state
    before = [path.read_bytes() for path in files]
    proposed = _dictionary(tmp_path, "FICTIONAL_PRIVATE_PATH.json")
    path = tmp_path / "FICTIONAL_PRIVATE_PATH.json"
    if damage == "missing":
        path.unlink()
    elif damage == "sha":
        proposed["sha256"] = "0" * 64
    elif damage == "malformed":
        raw = b'{"FICTIONAL_PRIVATE_CONTENT":'
        path.write_bytes(raw)
        proposed["sha256"] = hashlib.sha256(raw).hexdigest()
    elif damage == "shared":
        path.chmod(0o644)
    elif damage == "symlink":
        path.unlink()
        path.symlink_to(cfg["drug_map"]["path"])
    elif damage == "directory":
        path.unlink()
        path.mkdir()
    assert _run(monkeypatch, ["drug_map=" + json.dumps(proposed)]) == 1
    output = capsys.readouterr().out
    assert output == f"初期設定: 薬剤辞書の設定を確認してください（詳細: {reason}）。変更は保存していません\n"
    assert events == []
    assert [path.read_bytes() for path in files] == before
    assert not list(tmp_path.glob("c.json.corrupt-*"))
    assert not (tmp_path / "data").exists()


def test_missing_dictionary_does_not_archive_corrupt_config(
        tmp_path, monkeypatch, setup_state):
    _, files, events = setup_state
    files[0].write_bytes(b'{"fictional-corrupt":')
    before = [path.read_bytes() for path in files]
    proposed = {"path": str(tmp_path / "absent.json"), "sha256": "a" * 64}
    assert _run(monkeypatch, ["drug_map=" + json.dumps(proposed)]) == 1
    assert [path.read_bytes() for path in files] == before and events == []
    assert not list(tmp_path.glob("c.json.corrupt-*"))


def test_missing_dictionary_on_first_init_writes_nothing(tmp_path, monkeypatch, setup_state):
    _, files, events = setup_state
    for path in files:
        path.unlink()
    proposed = {"path": str(tmp_path / "absent.json"), "sha256": "a" * 64}
    assert _run(monkeypatch, ["drug_map=" + json.dumps(proposed)]) == 1
    assert not any(path.exists() for path in files) and events == []


@pytest.mark.parametrize("kind", ["legacy-approved", "legacy-reference", "source-reference"])
def test_valid_explicit_dictionary_is_loaded_once_before_saving(
        tmp_path, monkeypatch, setup_state, kind):
    _, files, events = setup_state
    document = copy.deepcopy(DOCUMENT) if kind.startswith("legacy") else _source_document()
    if kind.endswith("reference"):
        document["source"].pop("approved_by")
    proposed = _dictionary(tmp_path, "new.json", document)
    real_load = drug_map.load
    calls = []

    def checked(path, *, expected_sha256):
        assert events == []
        calls.append((path, expected_sha256))
        dictionary = real_load(path, expected_sha256=expected_sha256)
        assert dictionary.approved is (kind == "legacy-approved")
        return dictionary

    monkeypatch.setattr(drug_map, "load", checked)
    assert _run(monkeypatch, ["drug_map=" + json.dumps(proposed)]) == 0
    assert calls == [(proposed["path"], proposed["sha256"])]
    assert events == ["keychain", "shared-settings"]
    assert json.loads(files[0].read_text())["drug_map"] == proposed
    assert "fictional-new-password" in files[1].read_text()


def test_dotted_dictionary_override_uses_final_pin_once(tmp_path, monkeypatch, setup_state):
    _, files, events = setup_state
    proposed = _dictionary(tmp_path, "new.json")
    real_load = drug_map.load
    calls = []

    def checked(path, *, expected_sha256):
        assert events == []
        calls.append((path, expected_sha256))
        return real_load(path, expected_sha256=expected_sha256)

    monkeypatch.setattr(drug_map, "load", checked)
    assert _run(monkeypatch, [
        "drug_map.sha256=" + json.dumps("0" * 64),
        " drug_map.path =" + json.dumps(proposed["path"]),
        "drug_map.sha256=" + json.dumps(proposed["sha256"]),
    ]) == 0
    assert calls == [(proposed["path"], proposed["sha256"])]
    assert json.loads(files[0].read_text())["drug_map"] == proposed


def test_dotted_dictionary_override_cannot_save_a_bad_pin(monkeypatch, setup_state):
    _, files, events = setup_state
    before = [path.read_bytes() for path in files]
    assert _run(monkeypatch, ["drug_map.sha256=" + json.dumps("0" * 64)]) == 1
    assert [path.read_bytes() for path in files] == before and events == []


@pytest.mark.parametrize("configured", [True, False])
def test_unrelated_init_never_loads_a_dictionary(monkeypatch, setup_state, configured):
    cfg, files, _ = setup_state
    if not configured:
        cfg.pop("drug_map")
    else:
        cfg["drug_map"]["path"] += ".missing"
    monkeypatch.setattr(drug_map, "load", lambda *a, **k: pytest.fail("unexpected dictionary read"))
    assert _run(monkeypatch, ["self_posts=true"]) == 0
    saved = json.loads(files[0].read_text())
    assert saved.get("drug_map") == cfg.get("drug_map") and saved["self_posts"] is True


def test_explicit_rollback_to_legacy_dictionary_is_allowed(tmp_path, monkeypatch, setup_state):
    cfg, files, _ = setup_state
    old = dict(cfg["drug_map"])
    new = _dictionary(tmp_path, "new.json", _source_document())
    monkeypatch.setattr(mcs_setup, "load_config", lambda: json.loads(files[0].read_text()))
    assert _run(monkeypatch, ["drug_map=" + json.dumps(new)]) == 0
    assert json.loads(files[0].read_text())["drug_map"] == new
    assert _run(monkeypatch, ["drug_map=" + json.dumps(old)]) == 0
    assert json.loads(files[0].read_text())["drug_map"] == old
