"""The tick's rule pass (stage_derive) follows extract.upgrade_scope at its real call site.

Unset and "all" call the rule pass exactly as before (no scope fragment);
"none", out-of-scope patients and malformed values keep earlier rule-version
rows, while new arrivals and edited bodies still run. Synthetic bodies, a
temp DB and a temp config file only; the LLM lane gets no budget."""
import json
import time
from contextlib import closing

import pytest

import extract
import mcs_util
import run_check
from extract_testkit import _hash, _ledger, _message
from inference_version import CURRENT_VERSION, VERSION_KEY

OLD = extract.RULE_VERSION - 1
ALL = {1, 2, 3, 4}
NEW_WORK = {3, 4}  # never extracted, edited body
_UNSET = object()


def _rule_row(db, mid, version, *, pid, chash=None):
    return db.artifact_add(extract.KIND, '{"synthetic_old":true}', project_id=pid, message_id=mid,
        model="rules-v1", meta={VERSION_KEY: CURRENT_VERSION, "hash": chash or _hash(db, mid),
                                "rule_version": version})


def _seed(db):
    db.save_messages([_message(1, "合成の訪問実施一", project_id=1), _message(2, "合成の訪問実施二", project_id=2),
                      _message(3, "合成の訪問実施三", project_id=2), _message(4, "合成の訪問実施四", project_id=2),
                      _message(5, "合成の訪問実施五", project_id=2)])
    _rule_row(db, 1, OLD, pid=1)
    _rule_row(db, 2, OLD, pid=2)
    _rule_row(db, 4, OLD, pid=2, chash="0" * 64)
    _rule_row(db, 5, extract.RULE_VERSION, pid=2)


def _current(db):
    return {row["message_id"] for row in db.artifacts(extract.KIND)
            if json.loads(row["meta"]).get("rule_version") == extract.RULE_VERSION} - {5}


def _host_config(tmp_path, monkeypatch, cfg):
    path = tmp_path / "config.json"
    if cfg is not _UNSET:
        path.write_text(cfg if isinstance(cfg, str) else json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(mcs_util, "CONF_PATH", str(path))
    return {} if cfg is _UNSET or isinstance(cfg, str) else cfg


@pytest.mark.parametrize("cfg,expected", [
    (_UNSET, ALL), ({}, ALL), ({"extract": {}}, ALL), ({"extract": {"upgrade_scope": "all"}}, ALL),
    ({"extract": {"upgrade_scope": "none"}}, NEW_WORK),
    ({"extract": {"upgrade_scope": {"project_ids": [1]}}}, NEW_WORK | {1}),
    ({"extract": {"upgrade_scope": {"project_ids": [9]}}}, NEW_WORK),
    ({"extract": {"upgrade_scope": "everything"}}, NEW_WORK),
    ({"extract": {"upgrade_scope": {"project_ids": []}}}, NEW_WORK),
    ({"extract": "none"}, NEW_WORK),                      # the container itself is malformed
    ('{"extract": {"upgrade_scope": "all"', NEW_WORK),     # an unreadable host config
], ids=lambda value: "unset" if value is _UNSET else repr(value))
def test_stage_derive_rule_pass_follows_the_scope(tmp_path, monkeypatch, cfg, expected):
    loaded = _host_config(tmp_path, monkeypatch, cfg)
    calls = []
    real = extract.run_pending

    def spy(ledger, **kwargs):
        calls.append(kwargs)
        return real(ledger, **kwargs)
    monkeypatch.setattr(extract, "run_pending", spy)
    with closing(_ledger(tmp_path)) as db:
        _seed(db)
        result = {"errors": []}
        run_check.stage_derive(db, result, time.monotonic() + 600, loaded, llm_budget_cap=0)
        assert result["extracted"] == len(expected) and _current(db) == expected
        assert len(calls) == 1 and set(calls[0]) == {"deadline", "scope_sql"}
        # an open scope passes an empty fragment, so the rule query is byte-identical
        assert (calls[0]["scope_sql"] == "") is (expected == ALL)
        # a second tick finds nothing more to rebuild under the same scope
        again = {"errors": []}
        run_check.stage_derive(db, again, time.monotonic() + 600, loaded, llm_budget_cap=0)
        assert again["extracted"] == 0
