"""The updater's environment check (baseline and post-merge) hands the
check the updater's own AGENTS_DIR, so a test or root override reaches
both callers (synthetic stubs only)."""
from types import SimpleNamespace

import environment_diagnostics
import ledger
import mcs_update
import mcs_util


def test_baseline_and_postcheck_pass_the_updater_agents_dir(monkeypatch, tmp_path):
    agents = str(tmp_path / "synthetic-agents")
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", agents)
    monkeypatch.setattr(mcs_update, "_service_paths", lambda: {})
    monkeypatch.setattr(mcs_update, "_head_sha", lambda: "synthetic-sha")
    monkeypatch.setattr(mcs_update, "_tree_clean", lambda: True)
    monkeypatch.setattr(mcs_update, "_agent_pid", lambda label: 123)
    monkeypatch.setattr(mcs_util, "load_config", lambda: {})
    monkeypatch.setattr(ledger, "Ledger", lambda path:
                        SimpleNamespace(db=SimpleNamespace(close=lambda: None)))
    seen = []
    monkeypatch.setattr(environment_diagnostics, "check_environment",
                        lambda cfg, **facts: seen.append(facts) or ([], []))

    assert mcs_update._baseline_check({}) == []
    assert mcs_update._postcheck({}, "synthetic-sha") == []
    assert [facts["agents_dir"] for facts in seen] == [agents, agents]
