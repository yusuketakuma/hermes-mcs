"""ci/issue_status.py: one status comment per issue, recomputed from synthetic
GitHub data; reviews decide only from the owner's verdict on the current head."""
import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ci"))
_spec = importlib.util.spec_from_file_location("issue_status", ROOT / "ci/issue_status.py")
st = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(st)

OWNER = "owner-example"
HEAD = "a" * 40
OLD = "b" * 40
NOW = datetime(2026, 11, 17, 3, 0, tzinfo=timezone.utc)


def _issue(number=5, labels=("担当:claude",), state="open", closed_at=None, reason=None):
    return {"number": number, "state": state, "labels": [{"name": n} for n in labels],
            "created_at": "2026-11-17T00:00:00Z", "updated_at": "2026-11-17T00:00:00Z",
            "closed_at": closed_at, "state_reason": reason, "html_url": f"https://example.test/i/{number}"}


def _pr(number=9, branch="agent/claude-issue-5", base="main", merged_at=None, state="open", body="Fixes #5",
        draft=False, mergeable_state="clean", head=HEAD):
    return {"number": number, "state": state, "body": body, "draft": draft, "merged_at": merged_at,
            "mergeable_state": mergeable_state, "created_at": "2026-11-17T01:00:00Z",
            "updated_at": "2026-11-17T01:00:00Z", "html_url": f"https://example.test/p/{number}",
            "head": {"sha": head, "ref": branch}, "base": {"ref": base}}


def _ci(conclusion="success", status="completed", sha=HEAD, run_id=100):
    return {"id": run_id, "path": st.CI_PATH, "head_sha": sha, "status": status, "conclusion": conclusion,
            "created_at": "2026-11-17T01:10:00Z", "updated_at": "2026-11-17T01:20:00Z", "run_attempt": 1,
            "html_url": f"https://example.test/r/{run_id}"}


def _review(state, commit=HEAD, user=OWNER, body="", at="2026-11-17T02:00:00Z"):
    return {"state": state, "commit_id": commit, "user": {"login": user}, "body": body, "submitted_at": at,
            "html_url": "https://example.test/review"}


def _run(title, status="in_progress", conclusion=None, run_id=7):
    return {"id": run_id, "display_title": title, "status": status, "conclusion": conclusion,
            "created_at": "2026-11-17T00:30:00Z", "html_url": f"https://example.test/r/{run_id}"}


def _bundle(issue=None, pr=None, **kw):
    bundle = {"issue": issue or _issue(), "pr": pr, "events": [], "comments": [], "others": [],
              "ci_runs": [], "ci_jobs": [], "status": None, "reviews": [], "claude_runs": [], "repair_runs": []}
    bundle.update(kw)
    return bundle


def _current(bundle):
    rows, notes = st.decide(bundle, OWNER)
    return rows[st.current_index(rows)], rows, notes


def _names(rows):
    return [row[1] for row in rows]


def test_issue_without_pr_walks_from_filing_to_implementation():
    row, rows, _ = _current(_bundle(_issue(labels=())))
    assert row[1] == "起票" and rows[1][0] == "todo"
    labeled = [{"event": "labeled", "label": {"name": "担当:claude"}, "created_at": "2026-11-17T00:10:00Z"}]
    row, rows, _ = _current(_bundle(events=labeled))
    assert rows[1][:3] == ("done", "担当決定", "2026-11-17T00:10:00Z")
    assert row[1] == "実装中"
    row, _, _ = _current(_bundle(claude_runs=[_run("Agent Claude #5 題名")]))
    assert row[1] == "実装中" and "run 7" in row[3]


def test_skipped_and_unrelated_agent_runs_are_not_this_issue():
    runs = [_run("Agent Claude #5 x", status="completed", conclusion="skipped"), _run("Agent Claude #51 y", run_id=8)]
    row, _, _ = _current(_bundle(claude_runs=runs))
    assert "run" not in row[3]


def test_ci_stages_follow_the_latest_run_on_the_current_head():
    pr = _pr()
    assert _current(_bundle(pr=pr))[0][1] == "CI 実行待ち"
    assert _current(_bundle(pr=pr, ci_runs=[_ci(sha=OLD)]))[0][1] == "CI 実行待ち", "an older head says nothing"
    assert _current(_bundle(pr=pr, ci_runs=[_ci(status="in_progress", conclusion=None)]))[0][1] == "CI 実行中"
    jobs = [{"name": "lint-test", "conclusion": "failure"}, {"name": "macos", "conclusion": "success"}]
    row, _, _ = _current(_bundle(_issue(labels=("担当:claude", "試行:2")), pr=pr,
                                 ci_runs=[_ci("failure")], ci_jobs=jobs))
    assert row[1] == "CI 失敗（試行:2）" and "lint-test" in row[3] and "macos" not in row[3]
    repair = [_run("Agent Claude repair #9")]
    row, _, _ = _current(_bundle(pr=pr, ci_runs=[_ci("failure")], repair_runs=repair))
    assert row[1] == "CI 失敗", "the failure stays current while its repair runs"
    _, rows, _ = _current(_bundle(pr=pr, ci_runs=[_ci("failure")], repair_runs=repair))
    assert ("active", "自動修正中") == rows[_names(rows).index("自動修正中")][:2]


def test_merge_gate_then_review_wait_after_green_ci():
    status = {"statuses": [{"context": "merge-gate", "state": "success", "description": "必須ジョブ実行済み・成功・要人間承認",
                            "updated_at": "2026-11-17T01:30:00Z"}]}
    row, rows, _ = _current(_bundle(pr=_pr(), ci_runs=[_ci()], status=status))
    assert row[1] == "レビュー待ち"
    gate = rows[_names(rows).index("マージ判定")]
    assert gate[0] == "done" and "要人間承認" in gate[3]
    pending = {"statuses": [{"context": "merge-gate", "state": "failure", "description": "x"}]}
    assert _current(_bundle(pr=_pr(), ci_runs=[_ci()], status=pending))[0][1] == "マージ判定"


def test_owner_verdict_on_the_current_head_decides_with_seat():
    body = "独立レビュー（codex2、作者でない席）: pass。対象 head " + OLD
    row, _, _ = _current(_bundle(pr=_pr(), ci_runs=[_ci()], reviews=[_review("APPROVED", body=body)]))
    assert row[1] == "承認（codex2）"
    row, _, _ = _current(_bundle(pr=_pr(), ci_runs=[_ci()], reviews=[_review("CHANGES_REQUESTED")]))
    assert row[1] == "修正依頼" and "commit aaaaaaa" in row[3], "no seat in the body: no seat in the stage"


def test_body_sha_never_overrides_commit_id():
    body = "独立レビュー（claude2、作者でない席）: pass。対象 head " + HEAD
    row, _, _ = _current(_bundle(pr=_pr(), ci_runs=[_ci()], reviews=[_review("APPROVED", commit=OLD, body=body)]))
    assert row[1] == "再レビュー待ち" and "bbbbbbb" in row[3]


def test_stale_changes_request_waits_for_rereview_and_a_running_fix_is_current():
    reviews = [_review("CHANGES_REQUESTED", commit=OLD)]
    row, rows, _ = _current(_bundle(pr=_pr(), ci_runs=[_ci()], reviews=reviews))
    assert row[1] == "再レビュー待ち"
    assert [r[0] for r in rows if r[1] in ("修正依頼", "修正中")] == ["done", "done"]
    running = [_run("Agent Claude #9 PR")]
    row, _, _ = _current(_bundle(pr=_pr(), ci_runs=[_ci()], reviews=reviews, claude_runs=running))
    assert row[1] == "修正中"


def test_only_owner_verdicts_count_and_comments_never_decide():
    reviews = [_review("APPROVED", user="someone-else"), _review("CHANGES_REQUESTED", user="bot[bot]"),
               _review("COMMENTED", at="2026-11-17T09:00:00Z")]
    assert _current(_bundle(pr=_pr(), ci_runs=[_ci()], reviews=reviews))[0][1] == "レビュー待ち"
    reviews.append(_review("CHANGES_REQUESTED", at="2026-11-17T01:00:00Z"))
    assert _current(_bundle(pr=_pr(), ci_runs=[_ci()], reviews=reviews))[0][1] == "修正依頼"


def test_main_merge_and_closing_complete_the_issue():
    merged = _pr(state="closed", merged_at="2026-11-17T02:30:00Z")
    row, _, _ = _current(_bundle(pr=merged, ci_runs=[_ci()]))
    assert row[1] == "マージ済み（main）"
    closed = _issue(state="closed", closed_at="2026-11-17T02:31:00Z")
    row, rows, _ = _current(_bundle(closed, pr=merged, ci_runs=[_ci()]))
    assert row[1] == "完了" and "active" not in [r[0] for r in rows]


def test_group_merge_does_not_complete():
    merged = _pr(state="closed", base="group/test-hardening", merged_at="2026-11-17T02:30:00Z")
    row, rows, _ = _current(_bundle(pr=merged, ci_runs=[_ci()]))
    assert row[1] == "グループへ統合済み（group/test-hardening）"
    assert rows[-1][:2] == ("todo", "完了")


def test_side_states_are_listed():
    issue = _issue(labels=("担当:claude", "要オーナー対応", "保留", "失敗:CI基盤", "bug"))
    _, _, notes = _current(_bundle(issue, pr=_pr(draft=True, mergeable_state="dirty"), others=[11]))
    assert notes == ["要オーナー対応", "保留", "失敗:CI基盤", "下書きPR", "衝突あり", "ほかの PR: #11"]


def test_dots_is_treated_like_claude_and_codex_branches_are_targets():
    dots = _current(_bundle(_issue(labels=("担当:dots",))))
    claude = _current(_bundle(_issue(labels=("担当:claude",))))
    assert [r[:3] for r in dots[1][2:]] == [r[:3] for r in claude[1][2:]]
    issues = [_issue(5, labels=()), _issue(6, labels=("担当:dots",)), _issue(7, labels=())]
    pulls = [_pr(9, branch="codex/fix-5", body="Fixes #5")]
    assert st.plan_targets(issues, pulls, now=NOW) == [{"issue": 5, "pr": 9}, {"issue": 6, "pr": None}]


def test_plan_narrows_by_hints_and_drops_old_closed_issues():
    issues = [_issue(5), _issue(6), _issue(8, state="closed", closed_at="2026-11-10T00:00:00Z"),
              {"number": 9, "pull_request": {}, "labels": []}]
    pulls = [_pr(9, body="Fixes #5"), _pr(12, body="Closes #6", head=OLD)]
    assert [t["issue"] for t in st.plan_targets(issues, pulls, now=NOW)] == [5, 6]
    assert st.plan_targets(issues, pulls, "#9", now=NOW) == [{"issue": 5, "pr": 9}]
    assert st.plan_targets(issues, pulls, f"ci {OLD}", now=NOW) == [{"issue": 6, "pr": 12}]
    assert st.plan_targets(issues, pulls, "#999", now=NOW) == []
    merged = _pr(13, state="closed", merged_at="2026-11-17T00:00:00Z", body="Fixes #5")
    assert st.choose_pr([merged, _pr(9)])["number"] == 9
    assert st.choose_pr([_pr(14, state="closed", body="Fixes #5"), merged])["number"] == 13


def test_unchanged_body_is_not_written_and_only_our_comment_is_edited():
    body = st.render_body(*st.decide(_bundle(pr=_pr(), ci_runs=[_ci()]), OWNER))
    assert body.startswith(st.MARKER) and "**現在**" in body
    pasted = {"id": 1, "user": {"login": "someone"}, "body": st.MARKER + " copy"}
    ours = {"id": 2, "user": {"login": st.BOT}, "body": body.replace("\n", "\r\n")}
    assert st.comment_plan(body, [pasted]) == {"comment_id": None, "write": True, "body": body}
    assert st.comment_plan(body, [pasted, ours])["write"] is False
    assert st.comment_plan(body + "x", [ours]) == {"comment_id": 2, "write": True, "body": body + "x"}


def test_rendered_cells_never_mention_or_break_the_table():
    status = {"statuses": [{"context": "merge-gate", "state": "failure", "description": "@claude a|b\nc"}]}
    body = st.render_body(*st.decide(_bundle(pr=_pr(), ci_runs=[_ci()], status=status), OWNER))
    assert "@claude" not in body and "a\\|b c" in body


def test_cli_renders_from_a_work_directory(tmp_path, capsys):
    (tmp_path / "open.json").write_text(json.dumps([[_issue()]]))
    (tmp_path / "pulls.json").write_text(json.dumps([_pr()]))
    d = tmp_path / "5"
    d.mkdir()
    (d / "pr.json").write_text(json.dumps(_pr()))
    (d / "ci_runs.json").write_text(json.dumps([_ci("failure")]))
    assert st.main(["plan", "--work", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == [{"issue": 5, "pr": 9}]
    assert st.main(["failed-ci-run", "--work", str(tmp_path), "--issue", "5"]) == 0
    assert capsys.readouterr().out.strip() == "100"
    assert st.main(["render", "--work", str(tmp_path), "--issue", "5", "--owner", OWNER]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["write"] is True and plan["comment_id"] is None and "CI 失敗" in plan["body"]


def _top_level(text, key):
    match = re.search(rf"^{key}:\n((?:[ #].*\n|\n)*)", text, re.M)
    return match.group(1) if match else ""


def test_workflow_is_read_only_except_issue_comments():
    text = (ROOT / ".github/workflows/issue-status.yml").read_text()
    triggers = _top_level(text, "on")
    assert "pull_request_target" not in text
    for event in ("issues:", "workflow_run:", "pull_request_review:", "schedule:", "workflow_dispatch:"):
        assert event in triggers
    assert "permissions: {}" in text
    granted = re.findall(r"^      ([a-z-]+): (read|write)$", text, re.M)
    assert sorted(granted) == sorted([("issues", "write"), ("pull-requests", "read"), ("actions", "read"),
                                      ("checks", "read"), ("statuses", "read"), ("contents", "read")])
    # The only checkout is the default branch's ci/; no PR ref, SHA or merge ref is ever checked out.
    assert text.count("actions/checkout@") == 1
    assert re.findall(r"^\s+ref: (.*)$", text, re.M) == ["${{ github.event.repository.default_branch }}"]
    assert "sparse-checkout: ci" in text and "persist-credentials: false" in text
    assert "group: issue-status" in text
    assert "/labels" not in text and "gh issue close" not in text and "state=closed" not in text.split("gh api -X PATCH")[1]
    # Event text reaches the script only through env, never interpolated into the shell.
    run = text.split("run: |", 1)[1]
    assert "${{" not in run
    for workflow in ("agent-claude.yml", "agent-claude-repair.yml", "agent-control.yml"):
        assert "\nrun-name: " in (ROOT / ".github/workflows" / workflow).read_text()
