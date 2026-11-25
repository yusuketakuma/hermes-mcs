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
