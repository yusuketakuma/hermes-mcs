"""Drug CLI contracts over private fictional dictionaries; no services or live DB."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys

import pytest

import ledger
import mcs_drug
from test_drug_map import DOCUMENT, _seed

ROOT = Path(__file__).resolve().parents[2]
_REAL_CONNECT = sqlite3.connect


def _private(tmp_path, name="dictionary.json", document=None):
    path = tmp_path / name
    payload = json.dumps(document or DOCUMENT, ensure_ascii=False).encode()
    path.write_bytes(payload)
    path.chmod(0o600)
    return path, hashlib.sha256(payload).hexdigest()


def _args(path, sha):
    return ["--dictionary", str(path), "--sha256", sha]


def _state(directory):
    return {p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
            for p in directory.iterdir() if p.is_file()}


def _run(tmp_path, argv, json_output=True):
    root = tmp_path / "mcs-home"
    root.mkdir(exist_ok=True)
    env = dict(os.environ, MCS_ROOT=str(root), HOME=str(tmp_path),
               MCS_LIFECYCLE_PINNED="1", PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, str(ROOT / "mcs/ops/mcs_cli.py"), "drug", *argv,
                           *(["--json"] if json_output else [])],
                          env=env, capture_output=True, text=True, timeout=15)


@pytest.fixture(autouse=True)
def forbid_live_access(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("drug CLI must not access config/DB/network unexpectedly")
    monkeypatch.setattr(mcs_drug, "load_config", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.mark.parametrize("command,name", [("lookup", "共通架空"), ("search", "キラ")])
@pytest.mark.parametrize("limit", [0, 1])
def test_subprocess_lookup_and_search_keep_counts_and_bound_printed_fields(tmp_path, command, name, limit):
    path, sha = _private(tmp_path)
    before = _state(tmp_path)
    result = _run(tmp_path, [command, name, *_args(path, sha), "--limit", str(limit)])
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["read_only"] is report["candidate_only"] is True
    assert report["dictionary"]["sha256"] == sha
    if command == "lookup":
        assert report["status"] == "ambiguous" and report["candidate_count"] == 2
        assert len(report["cands"]) == limit and len(report["matched_aliases"]) <= limit
        assert report["alias_count"] > limit and report["truncated"] is True
    else:
        assert report["total"] == 2 and len(report["items"]) == limit
        assert all(len(row["matching_aliases"]) <= limit for row in report["items"])
        assert report["truncated"] is True
    assert _state(tmp_path) == before


@pytest.mark.parametrize("command", ["lookup", "search"])
def test_human_unapproved_reference_only_controls_and_operator_identity(tmp_path, capsys, command):
    document = deepcopy(DOCUMENT)
    document["source"].pop("approved_by")
    document["entries"][0]["display"] = "架空成分\x1b[31m\n甲"
    document["source"]["name"] = "架空出所\x07\n"
    path, sha = _private(tmp_path, document=document)
    before = _state(tmp_path)
    assert mcs_drug.main([command, "キラナ", *_args(path, sha)]) == 0
    output = capsys.readouterr().out
    assert "未承認辞書・参考のみ" in output
    assert not any(ord(c) < 32 and c != "\n" for c in output)
    assert "架空成分" in output
    assert _state(tmp_path) == before
    document["source"]["approved_by"] = "FICTIONAL_OPERATOR_PRIVATE_CANARY"
    path, sha = _private(tmp_path, document=document)
    assert mcs_drug.main([command, "キラナ", *_args(path, sha), "--json"]) == 0
    output = capsys.readouterr().out
    assert "FICTIONAL_OPERATOR_PRIVATE_CANARY" not in output and "approved_by" not in output


def test_default_configured_dictionary_reads_only_expected_setting(tmp_path, monkeypatch, capsys):
    path, sha = _private(tmp_path)
    calls = []
    monkeypatch.setattr(mcs_drug, "load_config", lambda: calls.append(1) or {
        "drug_map": {"path": str(path), "sha256": sha}})
    before = _state(tmp_path)
    assert mcs_drug.main(["lookup", "キラナ", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "resolved" and calls == [1]
    assert _state(tmp_path) == before


@pytest.mark.parametrize("setting", [None, {}, {"path": "/fictional"},
                                     {"path": "/fictional", "sha256": "a" * 64, "extra": True}])
def test_unconfigured_or_invalid_setting_is_safe_no_output(tmp_path, monkeypatch, capsys, setting):
    monkeypatch.setattr(mcs_drug, "load_config", lambda: {"drug_map": setting})
    assert mcs_drug.main(["lookup", "キラナ", "--json"]) == 1
    output = capsys.readouterr()
    assert output.out == "" and "所有者権限・SHA256" in output.err
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("damage", ["path_only", "sha_only", "sha", "permissions", "symlink",
                                    "relative", "missing", "owner"])
def test_explicit_pin_private_file_and_pair_guards(tmp_path, monkeypatch, capsys, damage):
    path, sha = _private(tmp_path)
    arguments = _args(path, sha)
    if damage == "path_only":
        arguments = ["--dictionary", str(path)]
    elif damage == "sha_only":
        arguments = ["--sha256", sha]
    elif damage == "sha":
        arguments = _args(path, "0" * 64)
    elif damage == "permissions":
        path.chmod(0o644)
    elif damage == "symlink":
        linked = tmp_path / "linked.json"
        linked.symlink_to(path)
        arguments = _args(linked, sha)
    elif damage == "relative":
        arguments = _args(Path("dictionary.json"), sha)
    elif damage == "missing":
        arguments = _args(tmp_path / "missing.json", sha)
    else:
        uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: uid + 1)
    before = _state(tmp_path)
    assert mcs_drug.main(["lookup", "キラナ", *arguments, "--json"]) == 1
    output = capsys.readouterr()
    assert output.out == "" and str(path) not in output.err
    assert _state(tmp_path) == before


def _pair(tmp_path):
    before, sha_before = _private(tmp_path, "before.json")
    document = deepcopy(DOCUMENT)
    document["dict_id"] = "fictional-next"
    document["source"]["approved_by"] = "FICTIONAL_OPERATOR_PRIVATE_CANARY"
    document["entries"][1]["aliases"].append("キラナ")
    document["entries"].extend({"id": f"fiction-new-{i}", "kind": "ingredient",
        "display": f"架空追加{i}", "aliases": [], "codes": {}, "forms": []} for i in range(3))
    after, sha_after = _private(tmp_path, "after.json", document)
    return ["compare", "--before", str(before), "--before-sha256", sha_before,
            "--after", str(after), "--after-sha256", sha_after, "--limit", "1"]


def test_subprocess_compare_complete_counts_deterministic_bounded_changes(tmp_path):
    argv = _pair(tmp_path)
    before = _state(tmp_path)
    result = _run(tmp_path, argv)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["counts"]["added"] == 3 and report["counts"]["changed"] == 1
    assert report["counts"]["new_ambiguous_aliases"] == 1
    assert len(report["changes"]) == 1 and report["truncated"]["changes"] is True
    assert len(report["collisions"]) == 1 and len(report["collisions"][0]["after_candidates"]) == 1
    assert report["collisions"][0]["after_count"] == 2
    assert "approved_by" not in result.stdout and "FICTIONAL_OPERATOR_PRIVATE_CANARY" not in result.stdout
    assert _run(tmp_path, argv).stdout == result.stdout
    assert _state(tmp_path) == before


def test_human_compare_announces_truncation_without_changing_full_counts(tmp_path, capsys):
    argv = _pair(tmp_path)
    before = _state(tmp_path)
    assert mcs_drug.main(argv) == 0
    output = capsys.readouterr().out
    assert "added=3" in output and "changed=1" in output
    assert "表示上限" in output and "全件の集計" in output
    assert "FICTIONAL_OPERATOR_PRIVATE_CANARY" not in output
    assert _state(tmp_path) == before


@pytest.mark.parametrize("limit", ["-1", "201", "1.1", "abc"])
def test_cli_invalid_limits_exit_two(tmp_path, limit):
    path, sha = _private(tmp_path)
    with pytest.raises(SystemExit) as error:
        mcs_drug.main(["search", "架空", *_args(path, sha), "--limit", limit])
    assert error.value.code == 2


def test_subprocess_uses_existing_temporary_config_without_mutation(tmp_path):
    path, sha = _private(tmp_path)
    root = tmp_path / "mcs-home"
    root.mkdir()
    config = root / "config.json"
    payload = json.dumps({"drug_map": {"path": str(path), "sha256": sha},
                          "synthetic_marker": "must-remain"}).encode()
    config.write_bytes(payload)
    config.chmod(0o600)
    before = _state(root)
    result = _run(tmp_path, ["lookup", "キラナ"])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["dictionary"]["sha256"] == sha
    assert _state(root) == before


def _impact_corpus(tmp_path):
    """Synthetic live ledger published to the default snapshot location."""
    monkey = sqlite3.connect
    sqlite3.connect = _REAL_CONNECT
    try:
        (tmp_path / "live").mkdir()
        live = ledger.Ledger(str(tmp_path / "live" / "ledger.db"))
        live.ensure_patient(1)
        _seed(live, 1, ("キラナ", "共通架空", "架空未登録薬"))
        live.close()
        snapshot = ledger.publish_snapshot(str(tmp_path / "live" / "ledger.db"),
                                           str(tmp_path / "mcs-home" / "data" / "snapshots"))
    finally:
        sqlite3.connect = monkey
    assert snapshot
    argv = _pair(tmp_path)
    argv[0] = "impact"
    return argv, Path(snapshot)


def test_subprocess_impact_reads_default_published_snapshot_only(tmp_path):
    argv, snapshot = _impact_corpus(tmp_path)
    before = {**_state(tmp_path), **_state(snapshot.parent), **_state(tmp_path / "live")}
    result = _run(tmp_path, argv)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["read_only"] is report["candidate_only"] is True
    assert report["messages"] == {"evaluated": 1, "unevaluated_no_extraction": 0,
                                  "unevaluated_malformed": 0}
    assert report["counts"]["unchanged"] == 2 and report["counts"]["became_ambiguous"] == 1
    assert report["examples"][0]["name"] == "キラナ" and report["truncated"]["examples"] is False
    assert len(report["snapshot"]["generation_id"]) > 0
    assert "message_id" not in result.stdout and "project_id" not in result.stdout
    assert "FICTIONAL_OPERATOR_PRIVATE_CANARY" not in result.stdout
    assert _run(tmp_path, argv).stdout == result.stdout
    assert {**_state(tmp_path), **_state(snapshot.parent), **_state(tmp_path / "live")} == before


def test_human_impact_opens_only_the_snapshot_and_prints_japanese_labels(tmp_path, monkeypatch, capsys):
    argv, snapshot = _impact_corpus(tmp_path)
    opened = []
    monkeypatch.setattr(sqlite3, "connect", lambda target, *a, **k: opened.append(target)
                        or _REAL_CONNECT(target, *a, **k))
    assert mcs_drug.main([*argv, "--snapshot", str(snapshot), "--limit", "0"]) == 0
    output = capsys.readouterr().out
    assert opened == [snapshot.resolve().as_uri() + "?mode=ro"]
    for label in ("変化なし: 2件", "新たに候補あり: 0件", "候補なしに変化", "複数候補に変化: 1件",
                  "単一候補に変化", "候補が変化", "表示上限", "診療上の問題件数ではありません"):
        assert label in output
    assert not any(ord(c) < 32 and c != "\n" for c in output)


@pytest.mark.parametrize("damage", ["missing", "unpublished", "not_db"])
def test_impact_snapshot_errors_are_clear_and_exit_one(tmp_path, monkeypatch, capsys, damage):
    argv, snapshot = _impact_corpus(tmp_path)
    monkeypatch.setattr(sqlite3, "connect", _REAL_CONNECT)
    target = {"missing": tmp_path / "absent.db", "unpublished": tmp_path / "live" / "ledger.db",
              "not_db": tmp_path / "before.json"}[damage]
    before = (tmp_path / "live" / "ledger.db").read_bytes()
    assert mcs_drug.main([*argv, "--snapshot", str(target), "--json"]) == 1
    output = capsys.readouterr()
    assert output.out == "" and "公開スナップショット" in output.err
    assert str(target) not in output.err
    # sqlite may touch the WAL index (-shm) when probing a live DB; content stays unchanged.
    assert (tmp_path / "live" / "ledger.db").read_bytes() == before


def test_impact_dictionary_error_keeps_existing_message(tmp_path, capsys):
    argv, snapshot = _impact_corpus(tmp_path)
    argv[argv.index("--before-sha256") + 1] = "0" * 64
    assert mcs_drug.main([*argv, "--snapshot", str(snapshot)]) == 1
    assert "所有者権限・SHA256" in capsys.readouterr().err
