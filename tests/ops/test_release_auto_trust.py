"""release_auto_trustの合成検証。"""
import json
import os

import pytest

import release_auto_trust as trust

PIN = {'kid': 'a' * 64, 'purposes': ['mcs-auto-offer-v3'],
       'spki': 'a2V5', 'not_before': 0, 'expires_at': 2**53 - 1}


def _write(path, value, raw=None):
    data = raw if raw is not None else json.dumps(value, separators=(',', ':'), sort_keys=True).encode()
    path.write_bytes(data)


def test_load_valid_pins(tmp_path):
    path = tmp_path / 'auto-v3-pins.json'
    _write(path, {'schema_version': trust.SCHEMA_VERSION, 'pins': [PIN]})
    pins = trust.load_v3_pins(path)
    assert pins == [PIN]


def test_missing_file_is_fail_closed(tmp_path):
    with pytest.raises(trust.TrustError) as e:
        trust.load_v3_pins(tmp_path / 'none.json')
    assert e.value.code == 'TRUST_UNAVAILABLE'


def test_wrong_schema_version_rejected(tmp_path):
    path = tmp_path / 'auto-v3-pins.json'
    _write(path, {'schema_version': 'mcs.auto-v3-pins.v0', 'pins': [PIN]})
    with pytest.raises(trust.TrustError):
        trust.load_v3_pins(path)


def test_unknown_top_key_rejected(tmp_path):
    path = tmp_path / 'auto-v3-pins.json'
    _write(path, {'schema_version': trust.SCHEMA_VERSION, 'pins': [PIN], 'extra': 1})
    with pytest.raises(trust.TrustError):
        trust.load_v3_pins(path)


def test_duplicate_kid_rejected(tmp_path):
    path = tmp_path / 'auto-v3-pins.json'
    _write(path, {'schema_version': trust.SCHEMA_VERSION, 'pins': [PIN, PIN]})
    with pytest.raises(trust.TrustError):
        trust.load_v3_pins(path)


def test_symlink_rejected(tmp_path):
    target = tmp_path / 'target.json'
    _write(target, {'schema_version': trust.SCHEMA_VERSION, 'pins': [PIN]})
    link = tmp_path / 'auto-v3-pins.json'
    os.symlink(target, link)
    with pytest.raises(trust.TrustError):
        trust.load_v3_pins(link)


def test_oversize_rejected(tmp_path):
    path = tmp_path / 'auto-v3-pins.json'
    path.write_bytes(b' ' * (trust.MAX_BYTES + 1))
    with pytest.raises(trust.TrustError):
        trust.load_v3_pins(path)


def test_pins_path_joins_control_root(tmp_path):
    assert trust.pins_path(tmp_path) == os.path.join(os.fspath(tmp_path), 'auto-v3-pins.json')


def test_malformed_pin_fields_rejected(tmp_path):
    for bad in [
        {**PIN, 'purposes': None},
        {**PIN, 'spki': ['a2V5']},
        {**PIN, 'not_before': True},
        {**PIN, 'expires_at': 0},
    ]:
        path = tmp_path / 'auto-v3-pins.json'
        _write(path, {'schema_version': trust.SCHEMA_VERSION, 'pins': [bad]})
        import pytest as _pytest
        with _pytest.raises(trust.TrustError):
            trust.load_v3_pins(path)


def test_symlinked_parent_rejected(tmp_path):
    real = tmp_path / 'real'
    real.mkdir()
    _write(real / 'auto-v3-pins.json', {'schema_version': trust.SCHEMA_VERSION, 'pins': [PIN]})
    link = tmp_path / 'linkdir'
    os.symlink(real, link)
    import pytest as _pytest
    with _pytest.raises(trust.TrustError):
        trust.load_v3_pins(link / 'auto-v3-pins.json')


def test_open_errno_distinguishes_symlink_from_permission(tmp_path, monkeypatch):
    import pytest as _pytest
    target = tmp_path / 'target.json'
    _write(target, {'schema_version': trust.SCHEMA_VERSION, 'pins': [PIN]})
    link = tmp_path / 'auto-v3-pins.json'
    os.symlink(target, link)
    # stat段階をすり抜けた場合でも、openのELOOPはINVALID(形状不備)を返す
    real_stat = os.stat
    real_lstat = os.lstat
    resolved = os.path.realpath(link)
    def fake_stat(pth, **kw):
        kw.pop('follow_symlinks', None)
        return real_stat(resolved if os.fspath(pth) == os.fspath(link) else pth, **kw)
    def fake_lstat(pth, **kw):
        if os.fspath(pth) == os.fspath(link):
            return real_stat(resolved)
        return real_lstat(pth, **kw)
    monkeypatch.setattr(trust.os, 'stat', fake_stat)
    monkeypatch.setattr(trust.os, 'lstat', fake_lstat)
    with _pytest.raises(trust.TrustError) as e:
        trust.load_v3_pins(link)
    assert e.value.code == 'TRUST_INVALID'


def test_permission_denied_is_unavailable(tmp_path):
    import pytest as _pytest
    path = tmp_path / 'auto-v3-pins.json'
    _write(path, {'schema_version': trust.SCHEMA_VERSION, 'pins': [PIN]})
    os.chmod(path, 0o000)
    try:
        with _pytest.raises(trust.TrustError) as e:
            trust.load_v3_pins(path)
        assert e.value.code == 'TRUST_UNAVAILABLE'
    finally:
        os.chmod(path, 0o600)
