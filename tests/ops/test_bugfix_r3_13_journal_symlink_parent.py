"""Regression: a state dir under a symlinked parent still supports bulk journal scans."""
import pytest

import ext_contract as ext
from test_c1_withdraw_receive import _cli, _bundle, staged  # noqa: F401


def test_generation_withdraw_and_health_under_symlinked_parent(staged, capsys):  # noqa: F811
    state, sink, eids = staged
    link = state.parent / "linked"
    link.symlink_to(state.parent, target_is_directory=True)
    via_link = link / state.name
    _, out = _cli(capsys, ["withdraw", "--state", str(via_link), "--sink", str(sink),
                           "--generation", "gen-envelope-synth"])
    assert {r["status"] for r in out["results"]} == {"delete_held"}
    _, out = _cli(capsys, ["reconcile", "--state", str(via_link), "--sink", str(sink),
                           "--all"])
    assert len(out["results"]) == 3
    assert "journal_directory_invalid" not in ext.health(via_link)["reasons"]


def test_symlinked_journal_directory_still_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / "journal").symlink_to(real, target_is_directory=True)
    with pytest.raises(ext.ContractError) as e:
        list(ext._journal_paths(state))
    assert str(e.value) == "journal_directory_invalid"
