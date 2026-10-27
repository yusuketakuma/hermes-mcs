"""Checked local config preserves the existing strict reader's safety contract."""
import pytest

import mcs_util


def test_checked_config_distinguishes_empty_object_from_missing(tmp_path):
    path = tmp_path / "config.json"
    assert mcs_util.load_config_checked(str(path)) is None
    assert mcs_util.load_config(str(path)) == {}
    path.write_text("{}", encoding="utf-8")
    assert mcs_util.load_config_checked(str(path)) == {}
    assert mcs_util.load_config(str(path)) == {}
    path.write_text('{"mode":"synthetic", "nested":{"enabled":true}}', encoding="utf-8")
    expected = {"mode": "synthetic", "nested": {"enabled": True}}
    assert mcs_util.load_config_checked(str(path)) == expected
    assert mcs_util.load_config(str(path)) == expected


@pytest.mark.parametrize("raw", [
    b'{"x":1,"x":2}', b'{"nested":{"x":1,"x":2}}', b'{"x":NaN}',
    b'{"x":Infinity}', b'{"x":-Infinity}', b'\xff', b'{',
    b'[]', b'null', b'true', b'123', b'"synthetic"',
])
def test_checked_config_rejects_invalid_or_non_object_without_details(tmp_path, capsys, raw):
    path = tmp_path / "config.json"
    path.write_bytes(raw)
    assert mcs_util.load_config_checked(str(path)) is None
    assert mcs_util.load_config(str(path)) == {}
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


def test_checked_config_rejects_permissions_without_exposing_error(monkeypatch, capsys):
    def denied(_path, _limit):
        raise PermissionError("synthetic-secret error details")

    monkeypatch.setattr(mcs_util, "_read_local", denied)
    assert mcs_util.load_config_checked("synthetic-path") is None
    assert mcs_util.load_config("synthetic-path") == {}
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


def test_checked_config_keeps_bounded_regular_file_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(mcs_util, "CONFIG_MAX_BYTES", 8)
    path = tmp_path / "config.json"
    path.write_bytes(b'{"x": 1}')
    assert mcs_util.load_config_checked(str(path)) == {"x": 1}
    path.write_bytes(b'{"x": 12}')
    assert mcs_util.load_config_checked(str(path)) is None
    assert mcs_util.load_config(str(path)) == {}
    path.write_bytes(b"{}")
    link = tmp_path / "link.json"
    link.symlink_to(path)
    assert mcs_util.load_config_checked(str(link)) is None
    assert mcs_util.load_config(str(link)) == {}
