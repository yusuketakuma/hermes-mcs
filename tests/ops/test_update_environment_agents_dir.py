"""The updater's environment check (baseline and post-merge) hands the
check the updater's own AGENTS_DIR, so a test or root override reaches
both callers (synthetic stubs only)."""
import update_apply
import update_execution
import update_lifecycle_context
import update_planning
import update_preflight
import update_recovery
import update_repository
import update_restore
import update_runtime_migration
import update_services
from types import SimpleNamespace

import environment_diagnostics
import ledger
import mcs_update
import mcs_util


def test_baseline_and_postcheck_pass_the_updater_agents_dir(monkeypatch, tmp_path):
    agents = str(tmp_path / "synthetic-agents")
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", agents)
    monkeypatch.setattr(update_lifecycle_context, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(update_preflight, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(update_services, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(mcs_update, "_service_paths", lambda: {})
    monkeypatch.setattr(update_execution, '_service_paths', mcs_update._service_paths)
    monkeypatch.setattr(update_lifecycle_context, '_service_paths', mcs_update._service_paths)
    monkeypatch.setattr(update_planning, '_service_paths', mcs_update._service_paths)
    monkeypatch.setattr(update_preflight, '_service_paths', mcs_update._service_paths)
    monkeypatch.setattr(update_recovery, '_service_paths', mcs_update._service_paths)
    monkeypatch.setattr(update_restore, '_service_paths', mcs_update._service_paths)
    monkeypatch.setattr(mcs_update, "_head_sha", lambda: "synthetic-sha")
    monkeypatch.setattr(update_apply, '_head_sha', mcs_update._head_sha)
    monkeypatch.setattr(update_execution, '_head_sha', mcs_update._head_sha)
    monkeypatch.setattr(update_recovery, '_head_sha', mcs_update._head_sha)
    monkeypatch.setattr(update_repository, '_head_sha', mcs_update._head_sha)
    monkeypatch.setattr(update_restore, '_head_sha', mcs_update._head_sha)
    monkeypatch.setattr(update_runtime_migration, '_head_sha', mcs_update._head_sha)
    monkeypatch.setattr(mcs_update, "_tree_clean", lambda: True)
    monkeypatch.setattr(update_apply, '_tree_clean', mcs_update._tree_clean)
    monkeypatch.setattr(update_execution, '_tree_clean', mcs_update._tree_clean)
    monkeypatch.setattr(update_preflight, '_tree_clean', mcs_update._tree_clean)
    monkeypatch.setattr(update_recovery, '_tree_clean', mcs_update._tree_clean)
    monkeypatch.setattr(update_restore, '_tree_clean', mcs_update._tree_clean)
    monkeypatch.setattr(update_runtime_migration, '_tree_clean', mcs_update._tree_clean)
    monkeypatch.setattr(mcs_update, "_agent_pid", lambda label: 123)
    monkeypatch.setattr(update_execution, '_agent_pid', mcs_update._agent_pid)
    monkeypatch.setattr(update_services, '_agent_pid', mcs_update._agent_pid)
    monkeypatch.setattr(mcs_util, "load_config", lambda: {})
    monkeypatch.setattr(ledger, "Ledger", lambda path:
                        SimpleNamespace(db=SimpleNamespace(close=lambda: None)))
    seen = []
    monkeypatch.setattr(environment_diagnostics, "check_environment",
                        lambda cfg, **facts: seen.append(facts) or ([], []))

    assert mcs_update._baseline_check({}) == []
    assert mcs_update._postcheck({}, "synthetic-sha") == []
    assert [facts["agents_dir"] for facts in seen] == [agents, agents]
