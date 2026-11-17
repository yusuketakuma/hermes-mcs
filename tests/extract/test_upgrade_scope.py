"""extract.upgrade_scope: unset keeps today's behaviour, anything else closes, and counts stay exact.

Synthetic configuration, temp DB and synthetic bodies only."""
import json
import time

import pytest

import extract
import extract_llm as llm
import mcs_setup
import mcs_util
import upgrade_scope
from extract_testkit import _hash, _ledger, _message
from inference_version import CURRENT_VERSION, VERSION_KEY
from mcs_queries import EXTRACT_FEEDBACK_KIND, V4_PROJECTION_KIND
from semantic_projection import PROJECTION_VERSION

BASE = {"mcs_login_id": "synthetic", "notify_target": "slack:#synthetic"}
INVALID = [{}, {"project_ids": []}, {"project_ids": [0]}, {"project_ids": [-1]}, {"project_ids": ["1"]},
           {"project_ids": [1, 1]}, {"project_ids": [True]}, {"project_ids": [1], "extra": 1},
           {"projects": [1]}, "ALL", "None", "", True, False, 1, 0, None, [], [1]]


@pytest.mark.parametrize("cfg", [{}, {"extract": {}}, {"extract": {"upgrade_scope": "all"}},
                                 {"extract": {"patient_state_context": True}}])
def test_unset_and_all_are_open_and_silent(cfg):
    scope = upgrade_scope.resolve(cfg)
    assert scope.is_open and scope.error is None
    assert upgrade_scope.allows(scope, 1) and upgrade_scope.allows(scope, None)
    assert upgrade_scope.selection_sql(scope, "llm") == upgrade_scope.selection_sql(scope, "rule") == ""
    assert mcs_setup.validate_config({**BASE, **cfg}) == ([], [])


def test_none_closes_without_an_error_and_warns():
    cfg = {"extract": {"upgrade_scope": "none"}}
    scope = upgrade_scope.resolve(cfg)
    assert not scope.is_open and scope.error is None and not upgrade_scope.allows(scope, 1)
    assert mcs_setup.validate_config({**BASE, **cfg}) == (
        [], ["過去世代の更新を絞っています（extract.upgrade_scope）"])


def test_project_ids_open_only_those_patients():
    cfg = {"extract": {"upgrade_scope": {"project_ids": [7, 3]}}}
    scope = upgrade_scope.resolve(cfg)
    assert scope.mode == "projects" and scope.project_ids == (3, 7) and scope.error is None
    assert [upgrade_scope.allows(scope, pid) for pid in (3, 7, 4, None, "3", True)] == [
        True, True, False, False, False, False]
    assert mcs_setup.validate_config({**BASE, **cfg}) == (
        [], ["過去世代の更新を絞っています（extract.upgrade_scope）"])


@pytest.mark.parametrize("value", INVALID, ids=repr)
def test_every_other_value_closes_and_is_a_config_error(value):
    cfg = {"extract": {"upgrade_scope": value}}
    scope = upgrade_scope.resolve(cfg)
    assert not scope.is_open and not upgrade_scope.allows(scope, 1) and scope.error
    errors, warnings = mcs_setup.validate_config({**BASE, **cfg})
    assert errors == [f"extract.upgrade_scope: {scope.error}"]
    assert warnings == ["過去世代の更新を絞っています（extract.upgrade_scope）"]


def test_an_unreadable_host_config_closes_the_scope(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(mcs_util, "CONF_PATH", str(path))
    assert upgrade_scope.live().is_open  # no file: the shipped default
    path.write_text(json.dumps({"extract": {"upgrade_scope": "none"}}), encoding="utf-8")
    assert upgrade_scope.live().mode == "none"
    path.write_text(json.dumps({"extract": {"upgrade_scope": "all"}}), encoding="utf-8")
    assert upgrade_scope.live().is_open
    path.write_text('{"extract": {"upgrade_scope": "all"', encoding="utf-8")
    scope = upgrade_scope.live()
    assert not scope.is_open and scope.error  # a failed read never reads as "unset"
    assert not upgrade_scope.resolve(None).is_open


@pytest.mark.parametrize("container", ["none", "all", None, [], 0, True], ids=repr)
def test_a_non_object_extract_block_closes_like_its_config_error(tmp_path, monkeypatch, container):
    cfg = {"extract": container}
    scope = upgrade_scope.resolve(cfg)
    assert not scope.is_open and scope.error and not upgrade_scope.allows(scope, 1)
    assert upgrade_scope.selection_sql(scope, "rule") and upgrade_scope.selection_sql(scope, "llm")
    errors, _ = mcs_setup.validate_config({**BASE, **cfg})
    assert len(errors) == 1 and errors[0].startswith("extract: ")  # reported once, by the block check
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(mcs_util, "CONF_PATH", str(path))
    assert not upgrade_scope.live().is_open


def _llm_row(db, mid, version, *, pid=1, chash=None, **meta):
    return db.artifact_add(llm.KIND, '{"summary":"合成の結果"}', project_id=pid, message_id=mid,
        model=llm.MODEL, meta={VERSION_KEY: CURRENT_VERSION, "hash": chash or _hash(db, mid),
                               "extract_version": version, **meta})


def _rule_row(db, mid, version, *, pid=1):
    return db.artifact_add(extract.KIND, "{}", project_id=pid, message_id=mid, model="rules-v1",
        meta={VERSION_KEY: CURRENT_VERSION, "hash": _hash(db, mid), "rule_version": version})


def _hold(db, mid, prior, *, pid=1):
    return db.artifact_add(llm.UPGRADE_KIND, json.dumps({
        "state": "held_prior", "hash": _hash(db, mid), "prior_artifact_id": prior,
        "target_version": llm.EXTRACT_VERSION, "contract": llm._upgrade_contract(),
        "reason": "protected_loss"}), project_id=pid, message_id=mid,
        meta={VERSION_KEY: CURRENT_VERSION})


def _seed_counts(db):
    """Two patients; every earlier-generation outcome once, plus rows that are not earlier ones."""
    bodies = {mid: f"完全合成の経過記録{mid}です。" for mid in range(1, 11)}
    db.save_messages([_message(mid, body, project_id=1 if mid <= 5 else 2) for mid, body in bodies.items()])
    old = llm.EXTRACT_VERSION - 1
    _llm_row(db, 1, old)                                  # runnable, patient 1
    _llm_row(db, 6, old, pid=2)                           # runnable, patient 2
    _llm_row(db, 7, old, pid=2)                           # sealed by a current v4 row
    db.artifact_add(V4_PROJECTION_KIND, '{"facts":[]}', project_id=2, message_id=7,
        meta={VERSION_KEY: CURRENT_VERSION, "hash": _hash(db, 7), "engine_version": 4,
              "projection_version": PROJECTION_VERSION})
    _hold(db, 8, _llm_row(db, 8, old, pid=2), pid=2)      # held under the current contract
    _llm_row(db, 9, old, pid=2)                           # five failed attempts at this generation
    db.artifact_add(llm.KIND, "{}", project_id=2, message_id=9, model=llm.MODEL,
        meta={VERSION_KEY: CURRENT_VERSION, "hash": _hash(db, 9), "extract_version": llm.EXTRACT_VERSION,
              "error": 1, "attempts": 5, "next_try": time.time() - 1})
    human = _llm_row(db, 10, old, pid=2)                  # runnable, admitted by a human report
    db.artifact_add(EXTRACT_FEEDBACK_KIND, json.dumps({"artifact_id": human}), project_id=2, message_id=10)
    _llm_row(db, 2, llm.EXTRACT_VERSION)                  # already the running generation
    _llm_row(db, 3, old, chash="0" * 64)                  # edited body: a new source, not earlier
    # mid 4, 5: never extracted
    _rule_row(db, 1, extract.RULE_VERSION - 1)
    _rule_row(db, 6, extract.RULE_VERSION - 1, pid=2)
    _rule_row(db, 2, extract.RULE_VERSION)


@pytest.mark.parametrize("value,outside", [
    (None, {"llm": 0, "rule": 0, "attachment_pages": 0}),
    ("all", {"llm": 0, "rule": 0, "attachment_pages": 0}),
    ("none", {"llm": 2, "rule": 2, "attachment_pages": 0}),
    ({"project_ids": [1]}, {"llm": 1, "rule": 1, "attachment_pages": 0}),
    ({"project_ids": [2]}, {"llm": 1, "rule": 1, "attachment_pages": 0}),
    ("ALL", {"llm": 2, "rule": 2, "attachment_pages": 0}),
])
def test_remaining_counts_every_outcome_once_and_the_scope_part(tmp_path, value, outside):
    db = _ledger(tmp_path)
    try:
        _seed_counts(db)
        cfg = {} if value is None else {"extract": {"upgrade_scope": value}}
        db.db.execute("PRAGMA query_only=ON")
        counts = upgrade_scope.prior_generation_remaining(db.db, cfg)
        assert counts == {"llm": 3, "rule": 2, "attachment_pages": 0, "sealed": 1, "held": 1,
                          "failed": 1, "outside_scope": outside}
    finally:
        db.close()


def test_new_arrivals_and_edits_never_add_to_the_remaining_counts(tmp_path):
    db = _ledger(tmp_path)
    try:
        _seed_counts(db)
        cfg = {"extract": {"upgrade_scope": "none"}}
        before = upgrade_scope.prior_generation_remaining(db.db, cfg)
        db.save_messages([_message(11, "完全合成の新着です。", project_id=1),
                          _message(1, "完全合成の経過記録1を編集しました。", project_id=1)])
        after = upgrade_scope.prior_generation_remaining(db.db, cfg)
        # The edited post is a new source; the new arrival never had an earlier result.
        assert after["llm"] == before["llm"] - 1 and after["rule"] == before["rule"] - 1
        assert after["outside_scope"]["llm"] == before["outside_scope"]["llm"] - 1
    finally:
        db.close()


def test_selection_fragment_rejects_unknown_kinds_and_versions():
    with pytest.raises(ValueError):
        upgrade_scope.selection_sql(upgrade_scope.parse("none"), "attachment")
    with pytest.raises(ValueError):
        upgrade_scope.prior_generation_sql("llm", version=-1)


@pytest.mark.parametrize("value", [None, "all", "none", {"project_ids": [1]}, "ALL"], ids=repr)
def test_a_post_without_a_patient_is_counted_as_it_is_selected(tmp_path, value):
    """A NULL project id is never admitted by a project list: the count agrees with the selection."""
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(1, "完全合成の経過記録です。", project_id=None),
                          _message(2, "完全合成の経過記録二です。", project_id=1)])
        _rule_row(db, 1, extract.RULE_VERSION - 1, pid=None)
        _rule_row(db, 2, extract.RULE_VERSION - 1)
        cfg = {} if value is None else {"extract": {"upgrade_scope": value}}
        scope = upgrade_scope.resolve(cfg)
        counts = upgrade_scope.prior_generation_remaining(db.db, cfg)
        selectable = {r[0] for r in db.db.execute(
            "SELECT m.message_id FROM messages m WHERE 1"
            + upgrade_scope.selection_sql(scope, "rule"))}
        assert counts["rule"] == 2
        assert counts["rule"] - counts["outside_scope"]["rule"] == len(selectable & {1, 2})
    finally:
        db.close()
