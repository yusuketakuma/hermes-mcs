"""mcs_setup check names what a closed extract.upgrade_scope holds back, read-only.

Synthetic temp ledger only; the ledger file is hashed before and after."""
import hashlib
import json

import pytest

import extract
import extract_llm as llm
import ledger
import mcs_setup
import setup_integration
from inference_version import CURRENT_VERSION, VERSION_KEY
from extract_testkit import _message


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "data").mkdir(parents=True)
    monkeypatch.setattr(mcs_setup, "HOME", str(home))
    monkeypatch.setattr(setup_integration, "HOME", str(home))
    return home


def _seed(home, *, earlier):
    db = ledger.Ledger(str(home / "data" / "ledger.db"))
    try:
        db.save_messages([_message(1, "完全合成の経過記録です。", project_id=1),
                          _message(2, "完全合成の別の経過記録です。", project_id=2)])
        for mid, pid in ((1, 1), (2, 2)):
            chash = db.db.execute("SELECT content_hash FROM messages WHERE message_id=?", (mid,)).fetchone()[0]
            db.artifact_add(llm.KIND, '{"summary":"合成の結果"}', project_id=pid, message_id=mid,
                meta={VERSION_KEY: CURRENT_VERSION, "hash": chash,
                      "extract_version": llm.EXTRACT_VERSION - earlier})
            db.artifact_add(extract.KIND, "{}", project_id=pid, message_id=mid,
                meta={VERSION_KEY: CURRENT_VERSION, "hash": chash,
                      "rule_version": extract.RULE_VERSION - earlier})
        db.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()


def _scope_lines(cfg):
    return [line for line in mcs_setup._queue_warnings(cfg) if "upgrade_scope" in line]


@pytest.mark.parametrize("value,line", [
    ("none", "upgrade_scope で止まっている過去世代: 投稿2件・ルール2件・添付0頁"
             "（extract.upgrade_scope を開くまで作り直しません）"),
    ({"project_ids": [1]}, "upgrade_scope で止まっている過去世代: 投稿1件・ルール1件・添付0頁"
                           "（extract.upgrade_scope を開くまで作り直しません）"),
    ("ALL", "upgrade_scope で止まっている過去世代: 投稿2件・ルール2件・添付0頁"
            "（extract.upgrade_scope を開くまで作り直しません）"),
], ids=repr)
def test_closed_scope_with_earlier_results_is_named_with_counts(home, value, line):
    _seed(home, earlier=1)
    path = home / "data" / "ledger.db"
    before = _digest(path)
    assert _scope_lines({"extract": {"upgrade_scope": value}}) == [line]
    assert _digest(path) == before


@pytest.mark.parametrize("cfg", [{}, {"extract": {"upgrade_scope": "all"}}], ids=json.dumps)
def test_open_scope_is_silent(home, cfg):
    _seed(home, earlier=1)
    assert _scope_lines(cfg) == []


def test_closed_scope_without_earlier_results_is_silent(home):
    _seed(home, earlier=0)
    path = home / "data" / "ledger.db"
    before = _digest(path)
    assert _scope_lines({"extract": {"upgrade_scope": "none"}}) == []
    assert _digest(path) == before
