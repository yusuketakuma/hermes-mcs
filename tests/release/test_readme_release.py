"""合成資料でREADME同期・リリース見直しの拒否条件を検証する。"""

import json
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "development"))
readme = __import__("readme_release")
notes = __import__("release_notes")


class ReadmeReleaseTest(unittest.TestCase):
    def item(self, **changes):
        return dict(category="fixed", title="合成の修正", summary="表示を修正します。",
                    upgrade="合成の確認手順です。", details=["内部の詳細です。"],
                    refs=["synthetic.py"]) | changes

    def changelog(self, version="1.0.8"):
        items = [self.item(), self.item(title="もう一つの修正", upgrade="別の確認手順です。"),
                 self.item(category="added", title="合成の追加"),
                 self.item(category="breaking", title="合成の設定変更")]
        return (f"## [Unreleased]\n\n## [{version}] — 2026-10-01\n\n"
                + notes.render(items, "合成リリース", "合成データだけを使用します。"))

    def template(self):
        return ("# Intro\n<a name=\"faq\"></a>\n[FAQ](#faq)\n"
                + readme.BEGIN + "\nold\n" + readme.END + "\nFooter\n")

    def fixture(self, root):
        (root / "docs/development").mkdir(parents=True)
        (root / "synthetic.py").write_text("# synthetic\n", encoding="utf-8")
        (root / "CHANGELOG.md").write_text(self.changelog(), encoding="utf-8")
        (root / "README.md").write_text(readme.render(self.template(), self.changelog()), encoding="utf-8")
        review = {"version": "1.0.8", "sections": {
            name: {"notes": "合成のソースを照合しました。", "sources": ["synthetic.py"]}
            for name in readme.REVIEW_SECTIONS}}
        (root / "docs/development/readme-review.json").write_text(json.dumps(review), encoding="utf-8")
        return review

    def test_latest_release_excerpt_preserves_all_upgrade_notes_and_manual_text(self):
        new = readme.render(self.template(), self.changelog())
        self.assertTrue(new.startswith("# Intro"))
        self.assertTrue(new.endswith("Footer\n"))
        self.assertIn("不具合修正 · 合成の修正", new)
        self.assertNotIn("不具合修正 · もう一つの修正", new)
        self.assertIn("別の確認手順です。", new)
        self.assertIn("> 更新前の確認", new)
        self.assertNotIn("内部の詳細です。", new)
        self.assertEqual(readme.render(new, self.changelog()), new)

    def test_comparison_table_keeps_rows_and_stays_out_of_summary(self):
        table = ("| 変更 | 以前 | 今回 | 利用者のメリット |\n"
                 "|---|---|---|---|\n"
                 "| 表示 | 別々 | 同じ場所 | 比較できる |")
        changelog = self.changelog().replace("### 新機能", table + "\n\n### 新機能")
        rendered = readme.render(self.template(), changelog)
        self.assertEqual(rendered.count(table), 1)
        self.assertIn("合成データだけを使用します。\n\n", rendered)
        self.assertLess(rendered.index(table), rendered.index("<details>"))
        self.assertEqual(readme.render(rendered, changelog), rendered)

    def test_malformed_markers_never_mutate_release_build_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            (root / "changes").mkdir()
            (root / "changes/001.json").write_text(json.dumps(self.item()), encoding="utf-8")
            original = (root / "CHANGELOG.md").read_bytes()
            for broken in (self.template().replace(readme.END, ""),
                           readme.END + self.template().replace(readme.END, ""),
                           self.template() + readme.BEGIN):
                (root / "README.md").write_text(broken, encoding="utf-8")
                with self.subTest(broken=broken), self.assertRaises(SystemExit):
                    notes.main(["--root", tmp, "build", "--version", "1.0.9",
                                "--date", "2026-10-01", "--headline", "合成の新版",
                                "--summary", "合成の説明です。"])
                self.assertEqual((root / "CHANGELOG.md").read_bytes(), original)
                self.assertEqual((root / "README.md").read_text(encoding="utf-8"), broken)
                self.assertTrue((root / "changes/001.json").is_file())
                self.assertFalse((root / "changes/archive").exists())

    def test_review_is_required_for_each_new_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review = self.fixture(root)
            readme.check(root, "1.0.8")
            (root / "CHANGELOG.md").write_text(self.changelog("1.0.9"), encoding="utf-8")
            self.assertEqual(readme.main(["--root", tmp]), 0)
            with self.assertRaisesRegex(ValueError, "見直し"):
                readme.check(root)
            review["version"] = "1.0.9"
            (root / "docs/development/readme-review.json").write_text(json.dumps(review), encoding="utf-8")
            readme.check(root, "1.0.9")
            with self.assertRaisesRegex(ValueError, "tag"):
                readme.check(root, "1.0.8")

    def test_missing_review_sections_sources_and_explanations_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review = self.fixture(root)
            for entry in ({"notes": "", "sources": ["synthetic.py"]},
                          {"notes": "確認", "sources": []},
                          {"notes": "確認", "sources": ["missing.py"]},
                          {"notes": "確認", "sources": ["../outside.py"]}):
                review["sections"]["features"] = entry
                (root / "docs/development/readme-review.json").write_text(json.dumps(review), encoding="utf-8")
                with self.subTest(entry=entry), self.assertRaises(ValueError):
                    readme.check(root)
            review["sections"].pop("features")
            (root / "docs/development/readme-review.json").write_text(json.dumps(review), encoding="utf-8")
            with self.assertRaises(ValueError):
                readme.check(root)

    def test_broken_assets_anchors_and_stale_overview_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            original = (root / "README.md").read_text(encoding="utf-8")
            for extra in ("[missing](#missing)", "![missing](missing.svg)", "<details>"):
                with self.subTest(extra=extra), self.assertRaises(ValueError):
                    readme.check_links(root, original + extra)
            (root / "README.md").write_text(original.replace("v1.0.8", "v1.0.7"), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "古く"):
                readme.check(root)

    def test_repository_readme_matches_latest_changelog_and_review(self):
        readme.check(Path(__file__).resolve().parents[2])


if __name__ == "__main__":
    unittest.main()
