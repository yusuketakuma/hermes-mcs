"""ci/agent_scan.py: a scan files only bounded, new findings about real files,
and the scanning job itself cannot write to the repository or the tracker."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("agent_scan", ROOT / "ci/agent_scan.py")
scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scan)


@pytest.fixture
def repo(tmp_path):
    for path in ("mcs/notify/cards.py", "mcs/core/db.py", "adapters/slack/send.py"):
        target = tmp_path / path
        target.parent.mkdir(parents=True)
        target.write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "mcs/__pycache__").mkdir()
    return tmp_path


def _finding(**changes):
    finding = {"title": "空の一覧で例外になる", "path": "mcs/notify/cards.py", "line": 12,
               "problem": "一覧が空だと落ちる", "evidence": "cards.py の先頭要素参照", "suggestion": ["空を先に返す"]}
    return {**finding, **changes}


def test_plan_puts_recently_changed_areas_first_and_rotates_the_rest(repo, monkeypatch):
    assert scan.areas(repo) == ["mcs/core", "mcs/notify", "adapters"]
    monkeypatch.setattr(scan, "AREAS_PER_RUN", 2)
    monkeypatch.setattr(scan, "RECENT_AREAS", 1)
    changed = ["mcs/notify/cards.py", "mcs/notify/other.py", "adapters/slack/send.py", "README.md"]
    first = scan.plan(0, changed, root=repo)
    assert first == {"areas": ["mcs/notify", "mcs/core"], "recent": ["mcs/notify/cards.py", "mcs/notify/other.py"], "manual": False}
    assert [scan.plan(number, changed, root=repo)["areas"][1] for number in range(1, 4)] == ["adapters", "adapters", "mcs/core"]
    assert [scan.plan(number, [], root=repo)["areas"] for number in range(2)] == [["mcs/core", "mcs/notify"], ["mcs/notify", "adapters"]]


def test_plan_scans_a_requested_area_alone_and_waits_on_a_full_backlog(repo):
    assert scan.plan(0, ["mcs/core/db.py"], requested="adapters", root=repo) == {"areas": ["adapters"], "recent": [], "manual": True}
    with pytest.raises(ValueError):
        scan.plan(0, [], requested="../etc", root=repo)
    assert scan.plan(0, ["mcs/core/db.py"], open_issues=scan.MAX_OPEN, root=repo) == {"areas": [], "recent": [], "manual": False}
    assert scan.plan(0, [], open_issues=scan.MAX_OPEN, requested="adapters", root=repo)["areas"] == ["adapters"]


def test_recent_files_reads_the_commit_history(repo):
    def git(*arguments):
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *arguments],
                       check=True, capture_output=True)

    git("init", "-q")
    git("add", "-A")
    git("commit", "-qm", "first")
    (repo / "mcs/core/db.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "mcs/core/gone.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "second")
    git("rm", "-q", "mcs/core/gone.py")
    git("commit", "-qm", "third")
    recent = scan.recent_files(repo)
    assert recent[0] == "mcs/core/db.py" and "mcs/core/gone.py" not in recent
    assert set(recent) == {"mcs/core/db.py", "mcs/notify/cards.py", "adapters/slack/send.py"}


def test_real_areas_cover_the_runtime_packages():
    found = scan.areas()
    assert "mcs/notify" in found and set(scan.TOP_LEVEL_AREAS) <= set(found)


def test_a_finding_becomes_an_issue_with_its_place_and_marker(repo):
    (issue,) = scan.issues_to_file([_finding()], [], ["mcs/notify"], root=repo)
    assert issue["title"] == "スキャン: 空の一覧で例外になる"
    assert "`mcs/notify/cards.py` 12 行目付近" in issue["body"] and "対象 `mcs/notify`" in issue["body"]
    assert "## 直し方の案\n- 空を先に返す" in issue["body"]
    assert scan.filed([issue]) == [({scan.fingerprint("mcs/notify/cards.py", "空の一覧で例外になる")}, "mcs/notify/cards.py", "空の一覧で例外になる", 12)]


def test_findings_outside_the_area_or_incomplete_are_dropped(repo):
    findings = [
        _finding(path="mcs/core/db.py"),
        _finding(path="../outside.py"),
        _finding(path="/etc/passwd"),
        _finding(path="mcs/notify/missing.py"),
        _finding(evidence=""),
        _finding(title=None),
        "not a finding",
    ]
    assert scan.issues_to_file(findings, [], ["mcs/notify"], root=repo) == []
    assert scan.issues_to_file({"title": "x"}, [], ["mcs/notify"], root=repo) == []


def test_filed_findings_are_not_filed_again_even_when_closed(repo):
    (first,) = scan.issues_to_file([_finding()], [], ["mcs/notify"], root=repo)
    closed = [{"number": 9, "state": "CLOSED", "body": first["body"]}]
    again = [_finding(title="空の一覧で 例外になる"), _finding(), _finding(title="別の指摘")]
    assert [issue["title"] for issue in scan.issues_to_file(again, closed, ["mcs/notify"], root=repo)] == ["スキャン: 別の指摘"]


def test_a_reworded_title_about_the_same_file_is_the_same_finding(repo):
    (first,) = scan.issues_to_file([_finding()], [], ["mcs/notify", "adapters"], root=repo)
    closed = [{"state": "closed", **first}]
    again = [
        _finding(title="空の一覧を開くと例外になる"),
        _finding(title="空の一覧を開くと例外になる", path="adapters/slack/send.py"),
        _finding(title="再送のたびに重複して記録される"),
        _finding(title="再送するたび重複して記録される"),
    ]
    filed = scan.issues_to_file(again, closed, ["mcs/notify", "adapters"], root=repo)
    assert [issue["body"].split("`")[3] + " " + issue["title"] for issue in filed] == [
        "adapters/slack/send.py スキャン: 空の一覧を開くと例外になる",
        "mcs/notify/cards.py スキャン: 再送のたびに重複して記録される",
    ]


def test_a_like_worded_finding_far_away_in_the_file_is_its_own_finding(repo):
    (first,) = scan.issues_to_file([_finding(title="通知の作成時に本文が欠落する", line=10)], [], ["mcs/notify"], root=repo)
    closed = [{"state": "closed", **first}]
    again = [
        _finding(title="通知の更新時に本文が欠落する", line=100),
        _finding(title="通知の更新時に本文が欠落する", line=30),
        _finding(title="通知の更新時に本文が欠落する", line=None),
        _finding(title="通知の作成時に本文が欠落する", line=20),
        _finding(title="通知の作成時に本文が欠落する", line=200),
    ]
    filed = scan.issues_to_file(again, closed, ["mcs/notify"], root=repo)
    assert [issue["body"].split("## 場所\n")[1].splitlines()[0] for issue in filed] == [
        "`mcs/notify/cards.py` 100 行目付近", "`mcs/notify/cards.py` 200 行目付近"]


def test_a_backlog_of_open_scan_issues_limits_what_is_filed(repo):
    backlog = [{"state": "open", "body": ""}] * (scan.MAX_OPEN - 1) + [{"state": "CLOSED", "body": ""}]
    findings = [_finding(title="一つ目"), _finding(title="二つ目", path="adapters/slack/send.py")]
    filed = scan.issues_to_file(findings, backlog, ["mcs/notify", "adapters"], root=repo)
    assert [issue["title"] for issue in filed] == ["スキャン: 一つ目"]
    full = backlog + [{"state": "OPEN", "body": ""}]
    assert scan.issues_to_file(findings, full, ["mcs/notify", "adapters"], root=repo) == []
    assert len(scan.issues_to_file(findings, full, ["mcs/notify", "adapters"], root=repo, manual=True)) == 2


def test_a_finding_carrying_a_token_like_string_is_dropped(repo):
    token = "tok" + "A1b2" * 10
    findings = [_finding(evidence=f"環境に {token} がある"), _finding(title="別の指摘", suggestion=token),
                _finding(title="長い名前", evidence="test_a_like_worded_finding_far_away_in_the_file_is_its_own_finding")]
    assert [issue["title"] for issue in scan.issues_to_file(findings, [], ["mcs/notify"], root=repo)] == ["スキャン: 長い名前"]


def test_issues_are_bounded_and_ping_nobody(repo):
    many = [_finding(title=f"指摘{number}", problem="@someone " + "長" * 5000) for number in range(6)]
    issues = scan.issues_to_file(many, [], ["mcs/notify"], root=repo)
    assert len(issues) == scan.MAX_ISSUES
    assert all("@" not in issue["body"] and len(issue["body"]) < 4 * scan.MAX_TEXT for issue in issues)
    long_title = scan.issues_to_file([_finding(title="題\n" + "名" * 200)], [], ["mcs/notify"], root=repo)[0]["title"]
    assert "\n" not in long_title and len(long_title) <= scan.MAX_TITLE + len("スキャン: ")


def test_cli_plans_then_files_and_refuses_unusable_input(tmp_path, capsys):
    existing = tmp_path / "existing.json"
    existing.write_text("[]", encoding="utf-8")
    assert scan.main(["plan", "--run-number", "0", "--existing", str(existing)]) == 0
    planned = capsys.readouterr().out
    assert len(json.loads(planned)["areas"]) == scan.AREAS_PER_RUN
    assert scan.main(["plan", "--run-number", "0", "--existing", str(existing), "--requested", "nowhere"]) == 2
    assert scan.main(["plan", "--run-number", "0", "--existing", str(tmp_path / "none.json")]) == 2
    plan = tmp_path / "plan.json"
    plan.write_text(planned, encoding="utf-8")
    result = tmp_path / "result.json"
    arguments = ["issues", "--plan", str(plan), "--result", str(result), "--existing", str(existing)]
    for unusable in (None, "", "[]", '{"findings": "none"}'):
        if unusable is not None:
            result.write_text(unusable, encoding="utf-8")
        assert scan.main(arguments) == 2
    capsys.readouterr()
    result.write_text('{"findings": []}', encoding="utf-8")
    assert scan.main(arguments) == 0
    assert json.loads(capsys.readouterr().out) == []
    for bad_plan in ('{"areas": []}', '{"areas": ["nowhere"]}'):
        plan.write_text(bad_plan, encoding="utf-8")
        assert scan.main(arguments) == 2


def test_workflow_keeps_the_scanning_job_read_only():
    text = (ROOT / ".github/workflows/agent-scan.yml").read_text(encoding="utf-8")
    scan_job, file_job = text.split("\n  file:\n")
    assert "vars.AUTOMATION_ENABLED == 'true'" in scan_job
    assert "contents: read\n      issues: read" in scan_job and "write" not in scan_job.split("jobs:")[1].split("steps:")[0]
    # Read-only tools: the result comes back as structured output, not as a file the model writes.
    assert '--allowedTools "Read,Glob,Grep"\n' in scan_job and "--json-schema" in scan_job
    assert "steps.claude.outputs.structured_output" in scan_job
    assert 'plan=$(python3 ci/agent_scan.py plan' in scan_job and "if: steps.plan.outputs.areas != ''" in scan_job
    assert 'cron: "40 */2 * * *"' in scan_job and "fetch-depth: 300" in scan_job
    assert "gh api --paginate --slurp" in scan_job
    assert "anthropics/claude-code-action@61a9c274fa7110a66dfafc1bbbced03833eb25fe" in scan_job
    assert "issues: write" in file_job and "contents" not in file_job
    assert "vars.AUTOMATION_ENABLED == 'true'" in file_job
    assert f'--label "{scan.SCAN_LABEL}" --label "{scan.TRIAGE_LABEL}"' in file_job
    assert "担当:" not in file_job
    assert f"最大{scan.MAX_ISSUES}件" in scan_job
    labels = {label["name"] for label in json.loads((ROOT / ".github/labels.json").read_text(encoding="utf-8"))["labels"]}
    assert {scan.SCAN_LABEL, scan.TRIAGE_LABEL} <= labels
