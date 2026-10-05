"""A failed post-merge schema-bump rollback in apply()'s bail must stay
recoverable: recovery retries the DB restore (or escalates) instead of
'finishing' as interrupted_pre_merge with old code on the newer DB."""
import json
from pathlib import Path

import mcs_update
from ops_testkit import _git, _make_repo, _mk_schema
from test_mcs_update import updater  # noqa: F401


def test_failed_bail_rollback_is_not_finished_as_pre_merge(
        updater, tmp_path, monkeypatch):  # noqa: F811
    import maintenance
    repo, _ = _make_repo(tmp_path)
    _git(repo, "reset", "-q", "--hard", "v1.0.0")
    monkeypatch.setattr(mcs_update, "REPO", str(repo))
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    sha = _git(repo, "rev-list", "-n1", "v1.1.0").stdout.strip()
    live = str(tmp_path / "data" / "ledger.db")
    monkeypatch.setattr(mcs_update, "LEDGER", live)
    back = tmp_path / "data" / "backups" / "pre.db"
    back.parent.mkdir(parents=True, exist_ok=True)
    _mk_schema(back, 7, messages=2)
    _mk_schema(live, 8, messages=5)
    monkeypatch.setattr(mcs_update, "load_config",
                        lambda: {"update": {"mode": "notify"}})
    monkeypatch.setattr(mcs_update, "precheck_local", lambda c: [])
    monkeypatch.setattr(mcs_update, "remote_tag_sha", lambda t: sha)
    monkeypatch.setattr(mcs_update, "precheck_tag",
                        lambda t: ["schema_bump:7->8"])
    monkeypatch.setattr(maintenance, "preupdate_backup", lambda p: str(back))
    monkeypatch.setattr(mcs_update, "_baseline_check", lambda c: [])
    monkeypatch.setattr(mcs_update, "quiesce",
                        lambda: (mcs_update._write_marker(), [])[1])
    monkeypatch.setattr(mcs_update, "restart_agents",
                        lambda **k: (mcs_update._remove_marker(), [])[1])
    monkeypatch.setattr(mcs_update, "_services_reconcile", lambda: None)
    monkeypatch.setattr(mcs_update, "_reconcile_membership", lambda m: [])
    monkeypatch.setattr(mcs_update, "_postcheck", lambda s, e: [])
    monkeypatch.setattr(mcs_update, "_enqueue_notice", lambda *a, **k: True)
    monkeypatch.setattr(mcs_update, "restart_gateway", lambda c: None)

    def post_merge(_sha):
        back.write_bytes(b"garbage")          # restore will fail
        raise mcs_update.UpdateError("post_merge_failed: synthetic")
    monkeypatch.setattr(mcs_update, "_run_post_merge", post_merge)

    assert mcs_update.apply("v1.1.0", sha, "cid") == 1
    st = mcs_update.load_state()
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before
    # journal is rollback-shaped: target is prev, restore still owed
    assert st["applying"]["rollback"] is True
    assert st["applying"]["sha"] == before

    mcs_update.recover_interrupted()
    st = mcs_update.load_state()
    report = json.loads(Path(mcs_update.REPORT_PATH).read_text())
    assert report["result"] != "interrupted_pre_merge"
    assert report["result"] == "escalate"
    assert "rollback db restore" in report["detail"]
    assert st["applying"] is not None          # still visible to humans
    assert (st.get("executed") or {}).get("cid", {}).get("result") \
        != "interrupted_recovered"
    assert mcs_update._db_version(live) == 8
