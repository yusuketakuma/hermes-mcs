"""Draft generation supports legacy tags without bypassing newer bundle checks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]
CHECKER = "scripts/development/check_drug_master_bundle.py"


class DraftTagCompatibility(unittest.TestCase):
    def _run(self, tag, *, checker=False, bundle=False, checker_status=0):
        workflow = (ROOT / ".github/workflows/release-notes.yml").read_text()
        step = workflow.split("      - name: Generate notes from tagged CHANGELOG\n", 1)[1]
        script = textwrap.dedent(step.split("        run: |\n", 1)[1].split("      - name:", 1)[0])
        with tempfile.TemporaryDirectory(prefix="synthetic-release-") as directory:
            repo = Path(directory)
            commands = repo / "commands.txt"
            binary = repo / "bin"
            binary.mkdir()
            python = binary / "python3"
            python.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$SYNTHETIC_COMMANDS"
if [ "$1" = scripts/development/check_drug_master_bundle.py ]; then
    [ -f "$1" ] || exit 2
    exit "$SYNTHETIC_CHECKER_STATUS"
fi
exit 0
''')
            python.chmod(0o700)
            if checker:
                target = repo / CHECKER
                target.parent.mkdir(parents=True)
                target.write_text("# synthetic checker\n")
            if bundle:
                target = repo / "resources/drug-master"
                target.parent.mkdir()
                if bundle == "dangling":
                    target.symlink_to("synthetic-missing")
                else:
                    target.mkdir()
            marker = repo / "synthetic-source"
            marker.write_text("synthetic release\n")
            for args in (["init", "-q"], ["add", "."],
                         ["-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid",
                          "commit", "-qm", "synthetic release"], ["tag", tag]):
                subprocess.run(["git", "-C", directory, *args], check=True,
                               capture_output=True, text=True, timeout=10)
            result = subprocess.run(["bash", "-e", "-c", script], cwd=directory,
                                    env={**os.environ, "PATH": str(binary) + os.pathsep + os.defpath,
                                         "RELEASE_TAG": tag, "SYNTHETIC_COMMANDS": str(commands),
                                         "SYNTHETIC_CHECKER_STATUS": str(checker_status)},
                                    capture_output=True, text=True, timeout=10)
            return result, commands.read_text() if commands.exists() else ""

    def test_legacy_v1015_without_bundle_reaches_canonical_export(self):
        result, commands = self._run("v1.0.15")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(CHECKER, commands)
        self.assertIn("release_notes.py export --version 1.0.15", commands)
        self.assertIn("readme_release.py --check --version 1.0.15", commands)

    def test_newer_tags_without_checker_stop_before_export(self):
        for tag in ("v1.0.16", "v1.0.17", "v1.1.0", "v2.0.0"):
            with self.subTest(tag=tag):
                result, commands = self._run(tag)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("checker is missing", result.stderr)
                self.assertNotIn("export", commands)

    def test_legacy_tag_with_bundle_cannot_hide_a_missing_checker(self):
        for bundle in (True, "dangling"):
            with self.subTest(bundle=bundle):
                result, commands = self._run("v1.0.15", bundle=bundle)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("export", commands)

    def test_present_checker_is_always_required_even_on_legacy_tags(self):
        for tag in ("v1.0.15", "v1.0.16"):
            with self.subTest(tag=tag):
                result, commands = self._run(tag, checker=True, bundle=True, checker_status=19)
                self.assertEqual(result.returncode, 19)
                self.assertIn(CHECKER, commands)
                self.assertNotIn("export", commands)

    def test_new_tag_with_verified_bundle_reaches_export(self):
        result, commands = self._run("v1.0.16", checker=True, bundle=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(CHECKER, commands)
        self.assertIn("release_notes.py export --version 1.0.16", commands)


class ExistingDraftCompatibility(unittest.TestCase):
    def _run(self, pages, *, api_status=0, sync_status=0):
        workflow = (ROOT / ".github/workflows/release-notes.yml").read_text()
        step = workflow.split("      - name: Create a draft release\n", 1)[1]
        script = textwrap.dedent(step.split("        run: |\n", 1)[1])
        with tempfile.TemporaryDirectory(prefix="synthetic-draft-reuse-") as directory:
            repo = Path(directory)
            commands = repo / "commands.jsonl"
            releases = repo / "releases.json"
            releases.write_text(pages if isinstance(pages, str) else json.dumps(pages))
            (repo / "release-title.txt").write_text("Synthetic title\n")
            (repo / "release-notes.md").write_text("Synthetic notes\n")
            binary = repo / "bin"
            binary.mkdir()
            for name, body in {
                "gh": '''
import json, os, sys
from pathlib import Path
with open(os.environ["SYNTHETIC_COMMANDS"], "a") as stream:
    stream.write(json.dumps(["gh", *sys.argv[1:]]) + "\\n")
if sys.argv[1] == "api":
    print(Path(os.environ["SYNTHETIC_RELEASES"]).read_text())
    sys.exit(int(os.environ["SYNTHETIC_API_STATUS"]))
sys.exit(0 if sys.argv[1:3] == ["release", "create"] else 97)
''',
                "python3": '''
import json, os, sys
if sys.argv[1] == "-":
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
with open(os.environ["SYNTHETIC_COMMANDS"], "a") as stream:
    stream.write(json.dumps(["python3", *sys.argv[1:]]) + "\\n")
sys.exit(int(os.environ["SYNTHETIC_SYNC_STATUS"]) if sys.argv[1] == "scripts/development/sync_release_notes.py" else 97)
''',
            }.items():
                target = binary / name
                target.write_text(f"#!{sys.executable}\n" + body)
                target.chmod(0o700)
            result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], cwd=repo,
                                    env={**os.environ, "PATH": str(binary) + os.pathsep + os.defpath,
                                         "RELEASE_TAG": "v1.1.2", "GH_REPO": "synthetic/repo",
                                         "SYNTHETIC_COMMANDS": str(commands), "SYNTHETIC_RELEASES": str(releases),
                                         "SYNTHETIC_API_STATUS": str(api_status), "SYNTHETIC_SYNC_STATUS": str(sync_status)},
                                    capture_output=True, text=True, timeout=10)
            calls = [json.loads(line) for line in commands.read_text().splitlines()]
            self.assertEqual(calls[0], ["gh", "api", "--paginate", "--slurp", "repos/synthetic/repo/releases"])
            return result, calls[1:]

    def test_absent_tag_creates_one_verified_draft(self):
        result, calls = self._run([[{"tag_name": "v1.1.1", "draft": False}], []])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [["gh", "release", "create", "v1.1.2", "--verify-tag", "--draft",
                                 "--title", "Synthetic title", "--notes-file", "release-notes.md"]])

    def test_existing_draft_on_a_later_page_uses_safe_synchronization(self):
        result, calls = self._run([[], [{"tag_name": "v1.1.2", "draft": True}]])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [["python3", "scripts/development/sync_release_notes.py", "--repo", "synthetic/repo",
                                 "--tag", "v1.1.2", "--apply"]])

    def test_duplicate_or_published_release_stops_before_writes(self):
        for pages in ([[{"tag_name": "v1.1.2", "draft": False}]],
                      [[{"tag_name": "v1.1.2", "draft": True}], [{"tag_name": "v1.1.2", "draft": True}]]):
            with self.subTest(pages=pages):
                result, calls = self._run(pages)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, [])

    def test_malformed_pages_or_records_stop_before_writes(self):
        for pages in ("{", {}, [], [None], [[None]], [[{}]], [[{"tag_name": 1, "draft": True}]],
                      [[{"tag_name": "", "draft": True}]], [[{"tag_name": "v1.1.2"}]],
                      [[{"tag_name": "v1.1.2", "draft": 1}]],
                      [[{"tag_name": "v1.1.2", "draft": True}], [{"draft": False}]]):
            with self.subTest(pages=pages):
                result, calls = self._run(pages)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, [])

    def test_lookup_or_sync_failure_never_falls_back_to_create(self):
        result, calls = self._run([[]], api_status=19)
        self.assertEqual(result.returncode, 19)
        self.assertEqual(calls, [])
        result, calls = self._run([[{"tag_name": "v1.1.2", "draft": True}]], sync_status=23)
        self.assertEqual(result.returncode, 23)
        self.assertEqual(calls, [["python3", "scripts/development/sync_release_notes.py", "--repo", "synthetic/repo",
                                 "--tag", "v1.1.2", "--apply"]])
