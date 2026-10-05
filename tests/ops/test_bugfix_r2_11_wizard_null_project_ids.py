import mcs_setup


def test_wizard_keeps_explicit_null_project_ids(monkeypatch):
    # semantic_config accepts project_ids: null as "all projects"; Enter
    # must keep it instead of looping on the required-item prompt.
    answers = iter([""] * 3)
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers))
    cur = mcs_setup._get_key(
        {"semantic": {"project_ids": None}}, "semantic.project_ids")
    assert mcs_setup._ask("semantic.project_ids", "reqintlist", None, "d",
                          cur, True) is None
    cfg = {"semantic": {"mode": "shadow", "project_ids": None}}
    assert mcs_setup._has_key(cfg, "semantic.project_ids")
    assert not mcs_setup._has_key(cfg, "semantic.extract_qc")
    mcs_setup._set_key(cfg, "semantic.project_ids", None)
    assert "project_ids" in cfg["semantic"]


def test_wizard_missing_project_ids_still_required(monkeypatch):
    answers = iter(["", "3"])
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers))
    assert mcs_setup._ask("semantic.project_ids", "reqintlist", None, "d",
                          None, False) == [3]
