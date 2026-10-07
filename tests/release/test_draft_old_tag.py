"""Draft generation supports legacy tags without bypassing newer bundle checks."""
import os
from pathlib import Path
import subprocess
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
