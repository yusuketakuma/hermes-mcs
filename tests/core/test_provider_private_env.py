"""Synthetic credentials exercise the shared strict owner-only dotenv boundary."""
import json
import os
from types import SimpleNamespace

import pytest

import cloud_policy
import mcs_util
import semantic_jev
import semantic_policy


@pytest.mark.parametrize("loader,key", [(cloud_policy.api_key,"MCS_ANTHROPIC_API_KEY"),
                                        (semantic_jev.api_key,"TYPESAFE_API_KEY")])
def test_provider_key_only_comes_from_private_mcs_file(tmp_path, monkeypatch, loader, key):
    monkeypatch.setenv(key, "must-not-use-process-key")
    assert loader(str(tmp_path)) is None
    path = tmp_path / ".env"
    path.write_text(key + '=' + json.dumps("synthetic-file-key") + '\n')
    path.chmod(0o600)
    assert loader(str(tmp_path)) == "synthetic-file-key"
    path.chmod(0o644)
    assert loader(str(tmp_path)) is None


@pytest.mark.parametrize("kind", ["link", "fifo", "directory", "large", "foreign", "invalid_utf8"])
def test_private_key_reader_rejects_unsafe_files(tmp_path, monkeypatch, kind):
    path = tmp_path / ".env"
    if kind == "link":
        target = tmp_path / "elsewhere"
        target.write_text("TYPESAFE_API_KEY=synthetic\n")
        target.chmod(0o600)
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"TYPESAFE_API_KEY=synthetic\n" if kind != "invalid_utf8" else b'\xff')
        path.chmod(0o600)
        if kind == "large":
            path.write_bytes(b"x" * (mcs_util.DOTENV_MAX_BYTES + 1))
        elif kind == "foreign":
            actual = os.fstat

            def foreign(fd):
                st = actual(fd)
                return SimpleNamespace(st_uid=st.st_uid + 1, st_mode=st.st_mode, st_size=st.st_size)

            monkeypatch.setattr(mcs_util.os, "fstat", foreign)
    assert semantic_jev.api_key(str(tmp_path)) is None


@pytest.mark.parametrize("value", ['', ' ', '" "', '"bad\\nkey"', 'bad key', '"bad\\u0000key"', '123\nTYPESAFE_API_KEY=other'])
def test_blank_control_whitespace_and_duplicate_keys_are_rejected(tmp_path, value):
    path = tmp_path / ".env"
    path.write_text("TYPESAFE_API_KEY=" + value + "\n")
    path.chmod(0o600)
    assert semantic_jev.api_key(str(tmp_path)) is None


def test_runtime_policy_reads_key_only_for_omitted_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(semantic_jev, "api_key", lambda root: pytest.fail("explicit mode reads no key"))
    cfg, errors = semantic_policy.runtime_config({"semantic": {"mode": "off"}}, root=str(tmp_path))
    assert not errors and cfg["mode"] == "off"
    monkeypatch.setattr(semantic_jev, "api_key", lambda root: "synthetic")
    cfg, errors = semantic_policy.runtime_config({}, root=str(tmp_path))
    assert not errors and cfg["mode"] == "assist"
    assert cfg["project_ids"] is None and cfg["daily_request_budget"] == 10000
