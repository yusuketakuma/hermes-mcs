"""ci/issue_status.py: one status comment per issue, recomputed from synthetic
GitHub data; reviews decide from the shared review verdict of the current head."""
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
DOTS = "dots-example"
ACTORS = (DOTS, "claude[bot]")


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


def _current(bundle, actors=ACTORS, dots_actor=DOTS):
    rows, notes = st.decide(bundle, OWNER, "main", list(actors), dots_actor)
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


def test_removed_assignment_refreshes_only_an_existing_comment():
    labeled = [{"event": "labeled", "label": {"name": "担当:claude"}, "created_at": "2026-11-17T00:10:00Z"}]
    row, rows, _ = _current(_bundle(_issue(labels=()), events=labeled))
    assert row[1] == "起票" and rows[1][0] == "todo" and rows[2][0] == "todo"
    assert st.plan_targets([_issue(labels=())], [], now=NOW) == []
    assert st.plan_targets([_issue(labels=())], [], "#5", now=NOW) == [{"issue": 5, "pr": None}]
    body = st.render_body(*st.decide(_bundle(_issue(labels=())), OWNER))
    assert st.comment_plan(body, [], tracked=False)["write"] is False
    ours = {"id": 3, "user": {"login": st.BOT}, "body": st.MARKER + " old"}
    assert st.comment_plan(body, [ours], tracked=False)["write"] is True


def test_skipped_and_unrelated_agent_runs_are_not_this_issue():
    runs = [_run("Agent Claude #5 x", status="completed", conclusion="skipped"), _run("Agent Claude #51 y", run_id=8)]
    row, _, _ = _current(_bundle(claude_runs=runs))
    assert "run" not in row[3]


def test_running_run_is_found_among_many_skipped_ones():
    skipped = [_run("Agent Claude #5 x", status="completed", conclusion="skipped", run_id=100 + n) for n in range(40)]
    row, _, _ = _current(_bundle(claude_runs=skipped + [_run("Agent Claude #5 x", run_id=99)]))
    assert row[1] == "実装中" and "run 99" in row[3]


def test_pending_runs_in_the_writer_queue_count_as_running():
    pending = _run("Agent Claude #5 x", status="pending")
    assert _current(_bundle(claude_runs=[pending]))[0][1] == "実装中"
    on_pr = _run("Agent Claude #9 PR", status="pending")
    reviews = [_review("CHANGES_REQUESTED", commit=OLD, user=DOTS)]
    assert _current(_bundle(pr=_pr(), ci_runs=[_ci()], reviews=reviews, claude_runs=[on_pr]))[0][1] == "修正中"
    # a fix run on the PR shows 修正中 even when the request came from a review that does not count (the owner's)
    owner_only = [_review("CHANGES_REQUESTED", commit=OLD)]
    assert _current(_bundle(pr=_pr(), ci_runs=[_ci()], reviews=owner_only, claude_runs=[on_pr]))[0][1] == "修正中"
    repair = [_run("Agent Claude repair #9", status="requested")]
    _, rows, _ = _current(_bundle(pr=_pr(), ci_runs=[_ci("failure")], repair_runs=repair))
    assert rows[_names(rows).index("自動修正中")][0] == "active"


def test_a_run_that_ended_without_a_pr_shows_a_stopped_stage():
    for conclusion, name in (("failure", "実装停止（失敗）"), ("cancelled", "実装停止（取消）"),
                             ("timed_out", "実装停止（時間切れ）"), ("success", "実装停止（PR なしで終了）")):
        ended = _run("Agent Claude #5 x", status="completed", conclusion=conclusion)
        row, _, _ = _current(_bundle(claude_runs=[ended]))
        assert row[1] == name and "run 7" in row[3]
    newer = dict(_run("Agent Claude #5 x", status="queued", run_id=8), created_at="2026-11-17T00:40:00Z")
    ended = _run("Agent Claude #5 x", status="completed", conclusion="failure")
    assert _current(_bundle(claude_runs=[ended, newer]))[0][1] == "実装中", "a queued retry is implementation again"
    assert _current(_bundle(pr=_pr(), claude_runs=[ended]))[0][1] == "CI 実行待ち", "with a PR the run is history"


def test_closed_unmerged_pr_ends_the_stages_while_the_issue_stays_open():
    closed = dict(_pr(state="closed"), closed_at="2026-11-17T02:40:00Z")
    row, rows, _ = _current(_bundle(pr=closed, ci_runs=[_ci()]))
    assert row[1] == "PR 閉鎖（未マージ）" and "active" not in [r[0] for r in rows]


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


def _mark(verdict, head=HEAD, user=OWNER, at="2026-11-17T02:30:00Z"):
    return {"user": {"login": user}, "created_at": at, "updated_at": at, "html_url": "https://example.test/c",
            "body": f"<!-- dots-review verdict={verdict} head={head} -->\n本文"}


def test_the_shared_verdict_names_its_source():
    green = {"pr": _pr(), "ci_runs": [_ci()]}
    row, _, _ = _current(_bundle(**green, reviews=[_review("APPROVED", user=DOTS)]))
    assert row[1] == "承認（dots）" and "commit aaaaaaa" in row[3]
    row, _, _ = _current(_bundle(**green, reviews=[_review("CHANGES_REQUESTED", user="claude[bot]")]))
    assert row[1] == "修正依頼（claude）"
    # Claude's approval is no verdict
    assert _current(_bundle(**green, reviews=[_review("APPROVED", user="claude[bot]")]))[0][1] == "レビュー待ち"
    row, _, _ = _current(_bundle(**green, pr_comments=[_mark("approved")]), dots_actor="")
    assert row[1] == "承認（dots・つなぎ）"
    # Dots beats Claude on the same head
    both = [_review("APPROVED", user="claude[bot]", at="2026-11-17T03:00:00Z"), _review("CHANGES_REQUESTED", user=DOTS)]
    assert _current(_bundle(**green, reviews=both))[0][1] == "修正依頼（dots）"


def test_owner_and_seat_reviews_are_a_reference_only():
    body = "独立レビュー（codex2、作者でない席）: pass。対象 head " + OLD
    row, _, notes = _current(_bundle(pr=_pr(), ci_runs=[_ci()], reviews=[_review("APPROVED", body=body)]))
    assert row[1] == "レビュー待ち"
    assert notes == ["参考（判定に数えない）: 所有者の承認（codex2） commit aaaaaaa"]
    reviews = [_review("APPROVED", user="someone-else"), _review("COMMENTED", user=DOTS)]
    assert _current(_bundle(pr=_pr(), ci_runs=[_ci()], reviews=reviews))[0][1] == "レビュー待ち"
    # no REVIEW_ACTORS: nothing decides
    approved = [_review("APPROVED", user=DOTS)]
    assert _current(_bundle(pr=_pr(), ci_runs=[_ci()], reviews=approved), actors=())[0][1] == "レビュー待ち"


def test_body_sha_never_overrides_commit_id():
    body = "独立レビュー（claude2、作者でない席）: pass。対象 head " + HEAD
    row, _, _ = _current(_bundle(pr=_pr(), ci_runs=[_ci()], reviews=[_review("APPROVED", user=DOTS, commit=OLD,
                                                                              body=body)]))
    assert row[1] == "再レビュー待ち" and "bbbbbbb" in row[3] and "（dots）" in row[3]


def test_stale_changes_request_waits_for_rereview_and_a_running_fix_is_current():
    reviews = [_review("CHANGES_REQUESTED", user=DOTS, commit=OLD)]
    row, rows, _ = _current(_bundle(pr=_pr(), ci_runs=[_ci()], reviews=reviews))
    assert row[1] == "再レビュー待ち"
    assert [r[0] for r in rows if r[1] in ("修正依頼（dots）", "修正中")] == ["done", "done"]
    running = [_run("Agent Claude #9 PR")]
    row, _, _ = _current(_bundle(pr=_pr(), ci_runs=[_ci()], reviews=reviews, claude_runs=running))
    assert row[1] == "修正中"


def test_merge_ready_is_its_own_stage():
    def status(state):
        return {"statuses": [{"context": "merge-gate", "state": "success", "description": "ok"},
                             {"context": "merge-ready", "state": state, "description": f"{state} reason",
                              "creator": {"login": "github-actions[bot]"},
                              "updated_at": "2026-11-17T02:40:00Z"}]}
    approved = [_review("APPROVED", user=DOTS)]
    row, _, _ = _current(_bundle(pr=_pr(), ci_runs=[_ci()], reviews=approved, status=status("success")))
    assert row[1] == "マージ可（merge-ready）" and row[0] == "done"
    row, _, _ = _current(_bundle(pr=_pr(), ci_runs=[_ci()], reviews=approved, status=status("pending")))
    assert row[1] == "マージ可（merge-ready）" and row[0] == "active" and "pending reason" in row[3]
    # a merge-ready posted by anyone else is not shown
    forged = status("success")
    forged["statuses"][1]["creator"] = {"login": "someone"}
    row, _, _ = _current(_bundle(pr=_pr(), ci_runs=[_ci()], reviews=approved, status=forged))
    assert row[1] == "承認（dots）"
    # a waiting review stays current ahead of merge-ready
    assert _current(_bundle(pr=_pr(), ci_runs=[_ci()], status=status("pending")))[0][1] == "レビュー待ち"


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
    merged = _pr(state="closed", base="group/a|b", merged_at="2026-11-17T02:30:00Z")
    assert "**現在: グループへ統合済み（group/a\\|b）**" in st.render_body(*st.decide(_bundle(pr=merged), OWNER))


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
    (d / "ci_runs.json").write_text(json.dumps([_ci()]))
    (d / "pr_comments.json").write_text(json.dumps([[_mark("approved")]]))
    assert st.main(["render", "--work", str(tmp_path), "--issue", "5", "--owner", OWNER,
                    "--actors", f"{DOTS}, claude[bot]"]) == 0
    assert "承認（dots・つなぎ）" in json.loads(capsys.readouterr().out)["body"]


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
    assert re.findall(r"^\s+ref: (.*)$", text, re.M) == [
        "${{ github.event.repository.default_branch && format('refs/heads/{0}', github.event.repository.default_branch)"
        " || github.ref }}"]
    assert "github.event.workflow_run.conclusion != 'skipped'" in text
    # Running Claude runs are fetched by status, so skipped runs cannot crowd them off the page.
    assert "runs requested queued pending waiting in_progress success failure cancelled timed_out" in text
    assert "wf=agent-claude-repair.yml runs requested queued pending waiting in_progress" in text
    assert "sparse-checkout: ci" in text and "persist-credentials: false" in text
    assert "group: issue-status" in text
    assert "/labels" not in text and "gh issue close" not in text and "state=closed" not in text.split("gh api -X PATCH")[1]
    # Event text reaches the script only through env, never interpolated into the shell.
    run = text.split("run: |", 1)[1]
    assert "${{" not in run
    assert '--actors "${ACTORS:-}" --dots-actor "${DOTS:-}"' in run and "/comments?per_page=100\" > \"$d/pr_comments.json\"" in run
    for workflow in ("agent-claude.yml", "agent-claude-repair.yml", "agent-control.yml"):
        assert "\nrun-name: " in (ROOT / ".github/workflows" / workflow).read_text()
    control = (ROOT / ".github/workflows/agent-control.yml").read_text()
    assert "|| inputs.sha ||" in control.split("run-name: ", 1)[1].splitlines()[0], "main-sync dispatch carries the head"
