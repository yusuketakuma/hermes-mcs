"""Shared diagnostics retain observed states without private local details."""
import json

import pytest

import collection_diagnostics
import support_diagnostics


def observation(errors):
    return {"observed_at": 1000,
            "errors": collection_diagnostics.error_counts(errors),
            "incomplete": collection_diagnostics.unknown_incomplete()}


def test_share_projects_fixed_facts_and_discards_private_details():
    secret = "synthetic-private-patient-token"
    report = {
        "mode": "hermes", "python": "3.14.9", "sqlite": "3.51.3",
        "checks": {"services": {"status": "warning", "errors": 0, "warnings": 1,
                                  "detail": secret}, secret: {"status": "healthy"}},
        "source": {"disk": {"revision": "a" * 40, "dirty": False, "path": secret},
                   "gateway": {"source_revision": None, "pid": 321, "started_at": 1000}},
        "collection": {"status": "warning", "counts": observation(["deadline_exceeded " + secret])},
        "card_worker": {"restore_delivery_held": True, "profile": secret},
        "runtimes": {"path": secret}, "patient_context_nightly": {"complete": 111},
        "operations": {"raw": secret}, "unknown": secret,
    }
    result = support_diagnostics.shared_report(report)
    wire = json.dumps(result)
    assert secret not in wire
    assert all(k not in wire for k in ("started_at", '"pid"', "patient_context", "runtimes"))
    assert result["checks"] == {"services": {"status": "warning", "errors": 0, "warnings": 1}}
    assert result["software"]["loaded_commit"] is None
    assert result["software"]["disk_commit"] == "a" * 40
    assert result["restore_delivery_held"] is True
    assert result["collection"]["errors"]["reasons"]["deadline"] == 1
    assert result["collection"]["observed_at"] == 1000
    assert "incomplete" not in result["collection"]
    assert len(wire.encode()) < 16 * 1024
    assert report["checks"]["services"]["detail"] == secret


@pytest.mark.parametrize("raw", [None, [], "private", 42])
def test_malformed_top_level_does_not_become_healthy(raw):
    result = support_diagnostics.shared_report(raw)
    assert result["software"]["mode"] == "unknown"
    assert result["software"]["loaded_commit"] is None
    assert result["collection"]["status"] == "unknown"
    assert result["collection"]["errors"]["state"] == "unknown"
    assert result["collection"]["observed_at"] is None
    assert result["restore_delivery_held"] is None


@pytest.mark.parametrize("bad_count", [True, -1, 1.5, "private", 1_000_000_001])
def test_invalid_check_count_is_unknown(bad_count):
    result = support_diagnostics.shared_report({
        "checks": {"services": {"status": "healthy", "errors": bad_count, "warnings": 0}}})
    assert result["checks"]["services"] == {"status": "unknown", "errors": None, "warnings": 0}


def test_partial_collection_preserves_coverage_without_patient_counts():
    result = support_diagnostics.shared_report({
        "collection": {"status": "unknown", "counts": observation(["network_error"] * 300)}})
    errors = result["collection"]["errors"]
    assert errors["state"] == "partial" and errors["total"] == 300
    assert errors["scanned"] == 256 and errors["reasons"]["network"] == 256
    assert result["collection"]["status"] == "unknown"


@pytest.mark.parametrize("counts", [None, observation(None), observation([]), observation(["timeout"] * 300)])
def test_healthy_collection_requires_complete_valid_observation(counts):
    result = support_diagnostics.shared_report({"collection": {"status": "healthy", "counts": counts}})
    assert result["collection"]["status"] == "unknown"


@pytest.mark.parametrize("status", ["not_checked", "stale"])
def test_unobserved_collection_preserves_not_checked_and_stale(status):
    result = support_diagnostics.shared_report({"collection": {"status": status, "counts": None}})
    assert result["collection"]["status"] == status


@pytest.mark.parametrize("errors,expected", [([], "healthy"), (["timeout"], "warning")])
def test_complete_collection_observation_controls_healthy_claim(errors, expected):
    counts = observation(errors)
    counts["incomplete"] = {
        "state": "known", "projects": 0,
        "reasons": dict.fromkeys(collection_diagnostics.FETCH_LABELS, 0),
        "parent_bodies": 0, "oldest_observed_age_s": None, "age_unknown": 0,
    }
    result = support_diagnostics.shared_report({"collection": {"status": "healthy", "counts": counts}})
    assert result["collection"]["status"] == expected
    assert "projects" not in json.dumps(result)


def test_unknown_fields_and_arbitrary_status_versions_are_never_echoed():
    secret = "synthetic-private-path"
    result = support_diagnostics.shared_report({
        "mode": secret, "python": secret, "sqlite": secret,
        "checks": {"services": {"status": secret, "errors": 0, "warnings": 0}},
        "source": {"disk": {"revision": secret, "dirty": 1},
                   "gateway": {"source_revision": secret}},
        "collection": {"status": secret, "counts": {"errors": secret}}})
    assert secret not in json.dumps(result)
    assert result["checks"]["services"]["status"] == "unknown"
    assert result["software"]["working_tree_dirty"] is None


@pytest.mark.parametrize("blocked", [False, True])
def test_doctor_share_cli_does_not_add_probes_or_change_exit(monkeypatch, capsys, blocked):
    import environment_diagnostics
    import lifecycle_io
    import local_llm
    import mcs_setup
    import mcs_util
    import runtime_inspection

    monkeypatch.setattr(mcs_util, "load_config", lambda: {
        "mcs_login_id": "synthetic-private-id", "notify_target": "local"})
    monkeypatch.setattr(mcs_setup, "_config_problem", lambda: None)
    monkeypatch.setattr(runtime_inspection, "probe_python", lambda _: {
        "python": "3.14.8", "sqlite": "3.51.3", "packages": {}})
    monkeypatch.setattr(mcs_setup.sqlite3, "sqlite_version_info", (3, 50, 4) if blocked else (3, 51, 3))
    monkeypatch.setattr(environment_diagnostics, "env_value", lambda *_a, **_k: pytest.fail("secret read"))
    monkeypatch.setattr(lifecycle_io, "run_bounded", lambda *_a, **_k: pytest.fail("service probe"))
    monkeypatch.setattr(local_llm, "bounded_request", lambda *_a, **_k: pytest.fail("network"))
    monkeypatch.setattr(mcs_setup, "_queue_warnings", lambda *_a: pytest.fail("database probe"))
    monkeypatch.setattr(environment_diagnostics, "source_status", lambda *_a, **_k: {
        "disk": {"revision": "b" * 40, "dirty": False},
        "gateway": {"source_revision": None, "pid": 456}})
    assert mcs_setup.main(["doctor", "--share", "--json"]) == int(blocked)
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["schema_version"] == support_diagnostics.SCHEMA_VERSION
    assert result["checks"]["interpreter"]["status"] == ("blocked" if blocked else "healthy")
    assert all(result["checks"][s]["status"] == "not_checked"
               for s in ("services", "llm", "credentials", "data"))
    assert "synthetic-private-id" not in output and '"pid"' not in output
