"""release_auto_holds の未解決hold列挙を合成stateで検証。

実MCS・実config・実dataには触れない。update_state.jsonとrestore markerは
tmp_path配下の合成fileのみ。
"""
import json
import os

import release_auto_holds as holds


def _state(data_dir, **fields):
    state = {"v": 1, "stages": [], "applied": [], "attempts": {},
             "executed": {}, "applying": None}
    state.update(fields)
    path = os.path.join(data_dir, 'update_state.json')
    with open(path, 'w') as f:
        json.dump(state, f)
    return path


def test_clean(tmp_path):
    assert holds.unresolved_holds(str(tmp_path)) == []
    _state(str(tmp_path))
    assert holds.unresolved_holds(str(tmp_path)) == []


def test_applying_is_hold(tmp_path):
    _state(str(tmp_path), applying={"tag": "v1.2.3", "sha": "a" * 40})
    assert holds.unresolved_holds(str(tmp_path)) == ['update_applying']


def test_corrupt_state_is_hold(tmp_path):
    """破損stateはjournal自体とrestore holdの両方を未解決として報告する。"""
    with open(tmp_path / 'update_state.json', 'w') as f:
        f.write('{not json')
    assert holds.unresolved_holds(str(tmp_path)) == [
        'update_state_unreadable', 'restore_hold']


def test_restore_consent_is_hold(tmp_path):
    _state(str(tmp_path), restore_consent={"report_id": "r" * 32})
    assert holds.unresolved_holds(str(tmp_path)) == ['restore_hold']


def test_restore_marker_is_hold(tmp_path):
    _state(str(tmp_path))
    marker = {"v": 1, "phase": "awaiting_consent", "by": "mcs_update",
              "at": 1700000000.0}
    with open(tmp_path / 'restore_pending.json', 'w') as f:
        json.dump(marker, f)
    assert holds.unresolved_holds(str(tmp_path)) == ['restore_hold']


def test_backup_restore_marker_without_adoption_is_hold(tmp_path):
    """adoption証拠を伴わないbackup restore markerはunreadable=hold(fail-closed)。"""
    _state(str(tmp_path))
    marker = {"v": 1, "phase": "restored", "by": "mcs_backup_restore",
              "restored_at": 1700000000.0, "at": 1700000000.0}
    with open(tmp_path / 'restore_pending.json', 'w') as f:
        json.dump(marker, f)
    assert holds.unresolved_holds(str(tmp_path)) == ['restore_hold']


def test_native_holds_adapter(tmp_path):
    native = holds.NativeHolds(str(tmp_path))
    assert native.holds() == []
    _state(str(tmp_path), applying={"tag": "v1.2.3"})
    assert native.holds() == ['update_applying']
