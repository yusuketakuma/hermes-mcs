"""実データ・ネットワークなしで生成と拒否条件を検証する。"""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import subprocess


SPEC = importlib.util.spec_from_file_location(
    "release_notes", Path(__file__).resolve().parents[2] / "scripts/release_notes.py")
notes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notes)


class ReleaseNotesTest(unittest.TestCase):
    def item(self):
        return dict(category="fixed", title="重複を修正", summary="返信を更新します。",
                    upgrade="再起動が必要です。", details=["投稿IDを再利用。"],
                    refs=["#20"])

    def test_validate_rejects_missing_unknown_and_multiline(self):
        for change in ({"refs": []}, {"category": "unknown"}, {"category": []},
                       {"title": "a\nb"}, {"summary": "<details>"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                notes.validate(self.item() | change)
        with self.assertRaises(ValueError):
            notes.validate(self.item() | {"unexpected": True})

    def test_deterministic_render_and_upgrade_deduplication(self):
        items = [self.item(), self.item() | {"title": "別の修正"}]
        rendered = notes.render(items, "通知を改善", "返信を確認しやすくします。")
        self.assertEqual(rendered, notes.render(items, "通知を改善", "返信を確認しやすくします。"))
        self.assertEqual(rendered.count("再起動が必要です。"), 1)
        self.assertLess(rendered.index("### 不具合修正"), rendered.index("### 更新時の注意"))
        self.assertIn("<details>", rendered)
        notes.check_body(rendered, "1.0.8")

    def test_export_exact_version_and_duplicate_failure(self):
        text = "## [Unreleased]\n\n## [1.0.8] — 2026-10-01\n\nnew\n\n## [1.0.7]\nold\n"
        self.assertEqual(notes.section(text, "1.0.8"), "new\n")
        for v in ("1.0.9", "../1.0.8", "1.0.8-rc.1"):
            with self.assertRaises(ValueError):
                notes.section(text, v)
        with self.assertRaises(ValueError):
            notes.section(text + "## [1.0.8]\nduplicate\n", "1.0.8")

    def test_build_archive_export_and_refuse_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "changes").mkdir()
            (root / "changes/001.json").write_text(json.dumps(self.item()), encoding="utf-8")
            changelog = root / "CHANGELOG.md"
            prior = notes.render([self.item()], "旧版", "旧版の変更です。")
            changelog.write_text("# Changelog\n\n## [Unreleased]\n\n## [1.0.7] — 2026-09-29\n\n" + prior,
                                 encoding="utf-8")
            args = ["--root", tmp, "build", "--version", "1.0.8", "--date", "2026-10-01",
                    "--headline", "通知の改善", "--summary", "返信を更新します。"]
            self.assertEqual(notes.main(args), 0)
            self.assertTrue((root / "changes/archive/1.0.8/001.json").is_file())
            text = changelog.read_text(encoding="utf-8")
            output = root / "release.md"
            title = root / "title.txt"
            self.assertEqual(notes.main(["--root", tmp, "export", "--version", "1.0.8",
                                         "--output", str(output), "--title-output", str(title)]), 0)
            self.assertEqual(title.read_text(encoding="utf-8"), "v1.0.8 — 通知の改善\n")
            self.assertEqual(output.read_text(encoding="utf-8"), notes.section(text, "1.0.8"))
            self.assertIn(prior, text)
            # Restore a pending record: duplicate version still must not mutate history.
            (root / "changes/002.json").write_text(json.dumps(self.item()), encoding="utf-8")
            with self.assertRaises(SystemExit):
                notes.main(args)
            self.assertEqual(changelog.read_text(encoding="utf-8"), text)

    def test_format_rejects_old_empty_and_reordered_headings(self):
        valid = notes.render([self.item()], "通知を改善", "返信を更新します。")
        for invalid in (valid.replace("### 不具合修正", "### 修正"),
                        valid.replace("### 更新時の注意", "### 改善"),
                        valid.replace("- **重複を修正**\n  返信を更新します。", ""),
                        valid.replace("### 技術詳細\n\n", "### 技術詳細\n")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                notes.check_body(invalid, "1.0.8")

    def test_all_versions_follow_format(self):
        root = Path(__file__).resolve().parents[2]
        versions = notes.check_changelog((root / "CHANGELOG.md").read_text(encoding="utf-8"))
        self.assertTrue(versions)

    def test_deleted_runtime_file_requires_fragment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def git(*args):
                return subprocess.run(
                    ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                     *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()

            git("init")
            (root / "mcs").mkdir()
            (root / "mcs/example.py").write_text("# synthetic\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "base")
            base = git("rev-parse", "HEAD")
            (root / "mcs/example.py").unlink()
            git("add", "-A")
            git("commit", "-m", "delete runtime file")
            with self.assertRaises(ValueError):
                notes.require_fragment(root, base)
            (root / "changes").mkdir()
            (root / "changes/001.json").write_text(json.dumps(self.item()), encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "document deletion")
            notes.require_fragment(root, base)


if __name__ == "__main__":
    unittest.main()
