"""ci/agent_scan.py: a scan files only bounded, new findings about real files,
and the scanning job itself cannot write to the repository or the tracker."""
import importlib.util
import json
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


def test_areas_rotate_over_packages_that_hold_code(repo):
    assert scan.areas(repo) == ["mcs/core", "mcs/notify", "adapters"]
    assert [scan.pick_area(number, root=repo) for number in range(4)] == ["mcs/core", "mcs/notify", "adapters", "mcs/core"]
    assert scan.pick_area(0, "adapters", root=repo) == "adapters"
    with pytest.raises(ValueError):
        scan.pick_area(0, "../etc", root=repo)


def test_real_areas_cover_the_runtime_packages():
    found = scan.areas()
    assert "mcs/notify" in found and set(scan.TOP_LEVEL_AREAS) <= set(found)


def test_a_finding_becomes_an_issue_with_its_place_and_marker(repo):
    (issue,) = scan.issues_to_file([_finding()], [], "mcs/notify", root=repo)
    assert issue["title"] == "スキャン: 空の一覧で例外になる"
    assert "`mcs/notify/cards.py` 12 行目付近" in issue["body"]
    assert "## 直し方の案\n- 空を先に返す" in issue["body"]
    assert scan.filed_fingerprints([issue]) == {scan.fingerprint("mcs/notify/cards.py", "空の一覧で例外になる")}


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
    assert scan.issues_to_file(findings, [], "mcs/notify", root=repo) == []
    assert scan.issues_to_file({"title": "x"}, [], "mcs/notify", root=repo) == []


def test_filed_findings_are_not_filed_again_even_when_closed(repo):
    (first,) = scan.issues_to_file([_finding()], [], "mcs/notify", root=repo)
    closed = [{"number": 9, "state": "CLOSED", "body": first["body"]}]
    again = [_finding(title="空の一覧で 例外になる"), _finding(), _finding(title="別の指摘")]
    assert [issue["title"] for issue in scan.issues_to_file(again, closed, "mcs/notify", root=repo)] == ["スキャン: 別の指摘"]


def test_issues_are_bounded_and_ping_nobody(repo):
    many = [_finding(title=f"指摘{number}", problem="@someone " + "長" * 5000) for number in range(6)]
    issues = scan.issues_to_file(many, [], "mcs/notify", root=repo)
    assert len(issues) == scan.MAX_ISSUES
    assert all("@" not in issue["body"] and len(issue["body"]) < 4 * scan.MAX_TEXT for issue in issues)
    long_title = scan.issues_to_file([_finding(title="題\n" + "名" * 200)], [], "mcs/notify", root=repo)[0]["title"]
    assert "\n" not in long_title and len(long_title) <= scan.MAX_TITLE + len("スキャン: ")


def test_cli_reads_files_and_tolerates_a_missing_findings_file(repo, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(scan, "ROOT", ROOT)
    assert scan.main(["area", "--run-number", "0"]) == 0
    assert capsys.readouterr().out.strip() == scan.areas()[0]
    assert scan.main(["area", "--run-number", "0", "--requested", "nowhere"]) == 2
    capsys.readouterr()
    existing = tmp_path / "existing.json"
    existing.write_text("[]", encoding="utf-8")
    assert scan.main(["issues", "--findings", str(tmp_path / "none.json"), "--existing", str(existing), "--area", "mcs/notify"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_workflow_keeps_the_scanning_job_read_only():
    text = (ROOT / ".github/workflows/agent-scan.yml").read_text(encoding="utf-8")
    scan_job, file_job = text.split("\n  file:\n")
    assert "vars.AUTOMATION_ENABLED == 'true'" in scan_job
    assert "contents: read\n      issues: read" in scan_job and "write" not in scan_job.split("jobs:")[1].split("steps:")[0]
    assert '--allowedTools "Read,Glob,Grep,Write(/tmp/agent-scan/**)"' in scan_job
    assert "Bash" not in scan_job.split("claude_args:")[1].split("- id: issues")[0]
    assert "anthropics/claude-code-action@61a9c274fa7110a66dfafc1bbbced03833eb25fe" in scan_job
    assert "issues: write" in file_job and "contents" not in file_job
    assert f'--label "{scan.SCAN_LABEL}" --label "{scan.TRIAGE_LABEL}"' in file_job
    assert "担当:" not in file_job
    assert f"最大{scan.MAX_ISSUES}件" in scan_job
    labels = {label["name"] for label in json.loads((ROOT / ".github/labels.json").read_text(encoding="utf-8"))["labels"]}
    assert {scan.SCAN_LABEL, scan.TRIAGE_LABEL} <= labels
