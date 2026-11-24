"""The rule pass skips closed-scope earlier-generation rows given by the caller's fragment.

``run_pending`` takes the fragment its caller built with
``upgrade_scope.selection_sql``; the manual entry point builds the same
fragment from the live configuration. Synthetic bodies and a temp DB only."""
import json
import os

import pytest

import extract
import mcs_util
import upgrade_scope
from extract_testkit import _hash, _ledger, _message
from inference_version import CURRENT_VERSION, VERSION_KEY

OLD = extract.RULE_VERSION - 1
ALL = {1, 2, 3, 4}
NEW_WORK = {3, 4}  # never extracted, edited body


def _rule_row(db, mid, version, *, pid, chash=None):
    return db.artifact_add(extract.KIND, '{"synthetic_old":true}', project_id=pid, message_id=mid,
        model="rules-v1", meta={VERSION_KEY: CURRENT_VERSION, "hash": chash or _hash(db, mid),
                                "rule_version": version})


def _seed(db):
    db.save_messages([_message(1, "合成の訪問実施一", project_id=1), _message(2, "合成の訪問実施二", project_id=2),
                      _message(3, "合成の訪問実施三", project_id=2), _message(4, "合成の訪問実施四", project_id=2),
                      _message(5, "合成の訪問実施五", project_id=2)])
    old = {1: _rule_row(db, 1, OLD, pid=1), 2: _rule_row(db, 2, OLD, pid=2)}
    _rule_row(db, 4, OLD, pid=2, chash="0" * 64)
    _rule_row(db, 5, extract.RULE_VERSION, pid=2)
    return old


def _current(db):
    return {row["message_id"] for row in db.artifacts(extract.KIND)
            if json.loads(row["meta"]).get("rule_version") == extract.RULE_VERSION} - {5}


@pytest.mark.parametrize("value,expected", [
    (None, ALL), ("all", ALL), ("none", NEW_WORK), ({"project_ids": [1]}, NEW_WORK | {1}),
    ({"project_ids": [2]}, NEW_WORK | {2}), ({"project_ids": "1"}, NEW_WORK), (False, NEW_WORK),
], ids=repr)
def test_rule_pass_follows_the_scope(tmp_path, value, expected):
    db = _ledger(tmp_path)
    try:
        old = _seed(db)
        cfg = {} if value is None else {"extract": {"upgrade_scope": value}}
        fragment = upgrade_scope.selection_sql(upgrade_scope.resolve(cfg), "rule")
        result = extract.run_pending(db, scope_sql=fragment)
        assert result["done"] == len(expected) and _current(db) == expected
        kept = {mid for mid, aid in old.items()
                if db.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()}
        assert kept == set(old) - expected  # a skipped earlier row is kept as it was
        assert extract.run_pending(db, scope_sql=fragment)["done"] == 0
    finally:
        db.close()


@pytest.mark.parametrize("value,expected", [
    (None, ALL), ("all", ALL), ("none", NEW_WORK), ({"project_ids": [1]}, NEW_WORK | {1}),
    ({"project_ids": [2]}, NEW_WORK | {2}), ({"project_ids": "1"}, NEW_WORK), (False, NEW_WORK),
], ids=repr)
def test_rule_cli_follows_the_live_scope(tmp_path, monkeypatch, capsys, value, expected):
    """``extract.py --all`` honours the live upgrade scope like the tick path."""
    db = _ledger(tmp_path)
    try:
        _seed(db)
        path = tmp_path / "config.json"
        if value is not None:
            path.write_text(json.dumps({"extract": {"upgrade_scope": value}}), encoding="utf-8")
        monkeypatch.setattr(mcs_util, "CONF_PATH", str(path))
        monkeypatch.setattr(extract, "DB", str(tmp_path / "ledger.db"))
        lock_fd = os.open(tmp_path / "run.lock", os.O_CREAT | os.O_RDWR, 0o600)
        monkeypatch.setattr(extract, "acquire_run_lock", lambda: lock_fd)
        monkeypatch.setattr(extract.sys, "argv", ["extract.py", "--all"])
        assert extract.main() == 0
        out = json.loads(capsys.readouterr().out.splitlines()[0])
        assert out["extracted"] == len(expected) and _current(db) == expected
    finally:
        db.close()


def test_default_rule_pass_is_unrestricted(tmp_path):
    db = _ledger(tmp_path)
    try:
        _seed(db)
        assert extract.run_pending(db)["done"] == len(ALL) and _current(db) == ALL
    finally:
        db.close()
