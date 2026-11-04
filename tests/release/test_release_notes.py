"""実データ・ネットワークなしで生成と拒否条件を検証する。"""

import importlib.util
import copy
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "development"))


SPEC = importlib.util.spec_from_file_location(
    "release_notes", Path(__file__).resolve().parents[2] / "scripts/development/release_notes.py")
notes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notes)
import sync_release_notes as sync  # noqa: E402 - development scripts path is installed above


class ReleaseNotesTest(unittest.TestCase):
    def test_private_repository_images_map_only_pinned_or_versioned_sources(self):
        base = "https://github.com/yusuketakuma/hermes-mcs/raw/"
        for ref in ("refs/tags/v1.1.1", "a" * 40):
            self.assertEqual(notes.repository_image_path(base + ref + "/docs/example.png"),
                             "docs/example.png")
        asset = "https://github.com/yusuketakuma/hermes-mcs/releases/download/v1.1.1/example.png"
        self.assertEqual(notes.repository_image_path(asset), "docs/releases/v1.1.1/example.png")
        notes.check_release_images(f"![添付]({asset})")
        for target in ("docs/example.png", "https://example.com/example.png",
                       "https://raw.githubusercontent.com/example/public/main/example.png"):
            self.assertIsNone(notes.repository_image_path(target))
        for target in (
            "https://raw.githubusercontent.com/yusuketakuma/hermes-mcs/v1.1.1/docs/example.png",
            base + "main/docs/example.png", base + "refs/heads/main/docs/example.png",
            base + "refs/tags/v1.1.1/../example.png", base + "refs/tags/v1.1.1/docs//example.png",
            base + "refs/tags/v1.1.1/docs/%2e%2e/example.png",
            base + "refs/tags/v1.1.1/docs%2fexample.png",
            base + "refs/tags/v1.1.1/docs/example.png?token=synthetic",
            base + "refs/tags/v1.1.1/docs/example.png#part",
            (base + "refs/tags/v1.1.1/docs/example.png").replace("github.com", "user@github.com"),
            (base + "refs/tags/v1.1.1/docs/example.png").replace("github.com", "github.com:443"),
            (base + "refs/tags/v1.1.1/docs/example.png").replace("/yusuketakuma", "/%79usuketakuma"),
            asset + "?token=synthetic", asset + "#part", asset.replace("/v1.1.1/", "/main/"),
            asset.replace("/example.png", "/../example.png"),
        ):
            with self.subTest(target=target), self.assertRaises(ValueError):
                notes.repository_image_path(target)

    def test_latest_image_gate_preserves_historical_notes_and_checks_export(self):
        old = notes.render([self.item()], "旧版", "旧版の説明です。")
        raw = "https://raw.githubusercontent.com/yusuketakuma/hermes-mcs/v1.0.7/docs/example.png"
        old = old.replace("### 不具合修正", f"![旧版]({raw})\n\n### 不具合修正")
        new = notes.render([self.item()], "新版", "新版の説明です。")
        text = "## [Unreleased]\n\n## [1.0.8] — 2026-10-01\n\n" + new
        history = "\n## [1.0.7] — 2026-09-29\n\n" + old
        self.assertEqual(notes.check_changelog(text + history), ["1.0.8", "1.0.7"])
        native = "https://github.com/yusuketakuma/hermes-mcs/raw/refs/tags/v1.0.8/docs/example.png"
        for target in (raw, native, "docs/example.png", "https:docs/image.png", "https:///docs/image.png"):
            bad = text.replace("### 不具合修正", f"![新版]({target})\n\n### 不具合修正") + history
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "CHANGELOG.md").write_text(bad)
                with self.assertRaises(SystemExit):
                    notes.main(["--root", tmp, "export", "--version", "1.0.8",
                                "--output", str(root / "out.md")])
                self.assertFalse((root / "out.md").exists())

    def item(self):
        return dict(category="fixed", title="重複を修正", summary="返信を更新します。",
                    upgrade="再起動が必要です。", details=["投稿IDを再利用。"],
                    refs=["#20"])

    def test_release_numbering_preserves_numeric_order_and_resets_lower_parts(self):
        for old, new in (("1.0.29", "1.1.0"), ("1.1.0", "1.1.1"),
                         ("1.1.7", "1.2.0"), ("1.9.9", "1.10.0"),
                         ("1.9.9", "2.0.0"), (None, "1.0.0")):
            with self.subTest(old=old, new=new):
                notes.check_version_bump(old, new)
        for old, new in (("1.1.0", "1.1.0"), ("1.1.0", "1.0.30"),
                         ("1.1.0", "2.1.0"), ("1.1.0", "2.0.1"),
                         ("1.1.0", "1.2.1"), ("1.1.0", "01.2.0"),
                         (None, "1.00.0")):
            with self.subTest(old=old, new=new), self.assertRaises(ValueError):
                notes.check_version_bump(old, new)

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
            readme = root / "README.md"
            readme.write_text("# Preserved intro\n<!-- BEGIN GENERATED:release -->\n"
                              "<!-- END GENERATED:release -->\nPreserved footer\n", encoding="utf-8")
            args = ["--root", tmp, "build", "--version", "1.0.8", "--date", "2026-10-01",
                    "--headline", "通知の改善", "--summary", "返信を更新します。"]
            self.assertEqual(notes.main(args), 0)
            self.assertTrue((root / "changes/archive/1.0.8/001.json").is_file())
            text = changelog.read_text(encoding="utf-8")
            self.assertIn("v1.0.8", readme.read_text(encoding="utf-8"))
            self.assertIn("Preserved footer", readme.read_text(encoding="utf-8"))
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

    def test_invalid_bump_leaves_pending_records_and_release_files_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "changes").mkdir()
            record = root / "changes/fix.json"
            record.write_text(json.dumps(self.item()), encoding="utf-8")
            changelog = root / "CHANGELOG.md"
            changelog.write_text("## [Unreleased]\n\n## [1.1.0] — 2026-10-01\n\n"
                                 + notes.render([self.item()], "旧版", "旧版です。"), encoding="utf-8")
            readme = root / "README.md"
            readme.write_text("unchanged", encoding="utf-8")
            before = {p: p.read_bytes() for p in (record, changelog, readme)}
            for target in ("1.0.30", "1.2.1", "2.1.0", "01.2.0"):
                with self.subTest(target=target), self.assertRaises(SystemExit):
                    notes.main(["--root", tmp, "build", "--version", target,
                                "--date", "2026-10-23", "--headline", "変更", "--summary", "説明。"])
                self.assertEqual({p: p.read_bytes() for p in before}, before)
                self.assertFalse((root / "changes/archive").exists())

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

    def test_adapter_runtime_changes_require_fragment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = ("adapters/slack/delivery.py", "adapters/discord/actions.py",
                     "adapters/lineworks/client.py", "lineworks_adapter/__main__.py")
            for path in paths:
                with self.subTest(path=path):
                    result = subprocess.CompletedProcess([], 0, path + "\n", "")
                    with patch.object(notes.subprocess, "run", return_value=result), self.assertRaises(ValueError):
                        notes.require_fragment(root, "base")
            (root / "changes").mkdir()
            (root / "changes/001.json").write_text(json.dumps(self.item()), encoding="utf-8")
            result = subprocess.CompletedProcess([], 0, "\n".join(paths) + "\nchanges/001.json\n", "")
            with patch.object(notes.subprocess, "run", return_value=result):
                notes.require_fragment(root, "base")
            result = subprocess.CompletedProcess([], 0, "adapters/README.md\n", "")
            with patch.object(notes.subprocess, "run", return_value=result):
                notes.require_fragment(root, "base")


    def test_runtime_scripts_require_fragments_but_development_scripts_do_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = ("scripts/mcs", "scripts/install_local.py", "scripts/mcs_upgrade.py",
                     "scripts/keychain_to_env.py")
            for path in paths:
                with self.subTest(path=path):
                    result = subprocess.CompletedProcess([], 0, path + "\n", "")
                    with patch.object(notes.subprocess, "run", return_value=result), self.assertRaises(ValueError):
                        notes.require_fragment(root, "base")
            result = subprocess.CompletedProcess([], 0, "scripts/development/update_readme.py\n", "")
            with patch.object(notes.subprocess, "run", return_value=result):
                notes.require_fragment(root, "base")
            (root / "changes").mkdir()
            (root / "changes/001.json").write_text(json.dumps(self.item()), encoding="utf-8")
            result = subprocess.CompletedProcess([], 0, "\n".join(paths) + "\nchanges/001.json\n", "")
            with patch.object(notes.subprocess, "run", return_value=result):
                notes.require_fragment(root, "base")


    def test_release_commit_archives_count_only_for_a_new_version(self):
        def scenario(path, payload, version="1.0.1"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)

                def git(*args):
                    return subprocess.run(
                        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                         *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()

                git("init")
                (root / "mcs").mkdir()
                (root / "mcs/example.py").write_text("# synthetic\n", encoding="utf-8")
                (root / "changes/archive/1.0.0").mkdir(parents=True)
                (root / "changes/archive/1.0.0/old.json").write_text(
                    json.dumps(self.item()), encoding="utf-8")
                (root / "CHANGELOG.md").write_text(
                    "## [Unreleased]\n\n## [1.0.0] — 2026-01-01\n", encoding="utf-8")
                git("add", ".")
                git("commit", "-m", "base")
                base = git("rev-parse", "HEAD")
                (root / "mcs/example.py").write_text("# synthetic change\n", encoding="utf-8")
                (root / "CHANGELOG.md").write_text(
                    f"## [Unreleased]\n\n## [{version}] — 2026-01-02\n\n## [1.0.0] — 2026-01-01\n",
                    encoding="utf-8")
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text(payload, encoding="utf-8")
                git("add", ".")
                git("commit", "-m", "release")
                notes.require_fragment(root, base)

        # the release build moved the new version's records into its archive
        scenario("changes/archive/1.0.1/a.json", json.dumps(self.item()))
        for path, payload, version in (
                # a version already released at base is history, not a record
                ("changes/archive/1.0.0/new.json", json.dumps(self.item()), "1.0.1"),
                # a record for a version without a new CHANGELOG section
                ("changes/archive/1.0.2/a.json", json.dumps(self.item()), "1.0.1"),
                # unrelated json is never a change record
                ("docs/a.json", json.dumps(self.item()), "1.0.1")):
            with self.subTest(path=path), self.assertRaises(ValueError):
                scenario(path, payload, version)
        broken = dict(self.item(), category="unknown")
        with self.assertRaises(ValueError):
            scenario("changes/archive/1.0.1/a.json", json.dumps(broken))

    def test_unreadable_base_changelog_never_accepts_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "changes/archive/1.0.1").mkdir(parents=True)
            (root / "changes/archive/1.0.1/a.json").write_text(json.dumps(self.item()), encoding="utf-8")
            (root / "CHANGELOG.md").write_text("## [1.0.1] — 2026-01-02\n", encoding="utf-8")
            calls = []

            def run(argv, *args, **kwargs):
                calls.append(argv)
                if argv[1] == "show":
                    raise subprocess.CalledProcessError(128, argv)
                return subprocess.CompletedProcess(
                    argv, 0, "mcs/example.py\nchanges/archive/1.0.1/a.json\n", "")

            with patch.object(notes.subprocess, "run", side_effect=run), \
                    self.assertRaises(subprocess.CalledProcessError):
                notes.require_fragment(root, "base")
            self.assertEqual(calls[-1][:2], ["git", "show"])


class ReleaseSyncTargetTest(unittest.TestCase):
    def setUp(self):
        item = dict(category="fixed", title="合成の修正", summary="通知を更新します。",
                    upgrade="追加操作は不要です。", details=[], refs=["#1"])
        body = notes.render([item], "通知を修正", "合成の説明です。")
        self.text = ("## [Unreleased]\n\n## [1.0.1] — 2026-10-02\n\n" + body
                     + "\n## [1.0.0] — 2026-10-01\n\n" + body)
        base = dict(name="old", body="old", draft=False, prerelease=False,
                    target_commitish="main", created_at="a", published_at="b", updated_at="c")
        self.records = [dict(base, id=1, tag_name="v1.0.0"),
                        dict(base, id=2, tag_name="v1.0.1")]
        self.writes = []

    def request(self, endpoint, payload=None):
        if "?" in endpoint:
            return copy.deepcopy(self.records)
        record = next(r for r in self.records if endpoint == f"releases/{r['id']}")
        if payload is not None:
            self.writes.append((endpoint, copy.deepcopy(payload)))
            record.update(payload)
        return copy.deepcopy(record)

    def test_tag_limits_writes_and_default_still_updates_all(self):
        result = sync.synchronize(self.text, self.request, apply=True, tag="v1.0.1")
        self.assertEqual(result["releases_checked"], 1)
        self.assertEqual(result["updated"], ["v1.0.1"])
        self.assertEqual([endpoint for endpoint, _ in self.writes], ["releases/2"])
        result = sync.synchronize(self.text, self.request, apply=True)
        self.assertEqual(result["updated"], ["v1.0.0"])

    def test_invalid_missing_duplicate_or_changed_target_never_writes(self):
        for tag in ("1.0.1", "v1.0.1-rc1", "v9.0.0", ""):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                sync.synchronize(self.text, self.request, apply=True, tag=tag)
        with self.assertRaises(ValueError):
            sync.synchronize(self.text, self.request, apply=True, tag="v1.0.1", release_id=1)
        self.records.append(copy.deepcopy(self.records[1]))
        with self.assertRaises(ValueError):
            sync.synchronize(self.text, self.request, apply=True, tag="v1.0.1")
        self.records.pop()
        self.records.pop()
        with self.assertRaises(ValueError):
            sync.synchronize(self.text, self.request, apply=True, tag="v1.0.1")
        self.records.append(dict(self.records[0], id=2, tag_name="v1.0.1"))

        def changed(endpoint, payload=None):
            result = self.request(endpoint, payload)
            if "?" not in endpoint and payload is None:
                result["updated_at"] = "changed"
            return result

        with self.assertRaises(ValueError):
            sync.synchronize(self.text, changed, apply=True, tag="v1.0.1")
        self.assertEqual(self.writes, [])

    def test_release_event_uses_canonical_target_and_rejects_mismatches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CHANGELOG.md").write_text(self.text, encoding="utf-8")
            event_path = root / "event.json"
            event = {"release": {"id": 2, "tag_name": "v1.0.1"},
                     "repository": {"full_name": "example/repo"}}
            event_path.write_text(json.dumps(event), encoding="utf-8")
            env = {"GITHUB_EVENT_NAME": "release", "GITHUB_EVENT_PATH": str(event_path),
                   "GITHUB_REPOSITORY": "example/repo", "GITHUB_REF": "refs/tags/v1.0.1"}
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(sync, "api", side_effect=lambda repo, endpoint, payload=None:
                                 self.request(endpoint, payload)), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sync.main(["--root", tmp, "--apply"]), 0)
            self.assertEqual([endpoint for endpoint, _ in self.writes], ["releases/2"])
            self.writes.clear()
            for changes, args in (({"GITHUB_EVENT_PATH": ""}, []),
                                  ({"GITHUB_REF": "refs/tags/v1.0.0"}, []),
                                  ({"GITHUB_REPOSITORY": "other/repo"}, []),
                                  ({}, ["--tag", "v1.0.0"]),
                                  ({}, ["--tag", "v1.0.1", "--tag", "v1.0.1"])):
                with self.subTest(changes=changes, args=args), patch.dict(os.environ, env | changes, clear=True), \
                        patch.object(sync, "api", side_effect=AssertionError("unexpected API call")), \
                        contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    sync.main(["--root", tmp, "--apply", *args])
            for payload in ('{"release": {"id": 2, "tag_name": "v1.0.1", '
                            '"tag_name": "v1.0.0"}}',
                            json.dumps(event | {"release": {"id": True, "tag_name": "v1.0.1"}}),
                            json.dumps(event | {"repository": {"full_name": "other/repo"}})):
                event_path.write_text(payload, encoding="utf-8")
                with self.subTest(payload=payload), patch.dict(os.environ, env, clear=True), \
                        patch.object(sync, "api", side_effect=AssertionError("unexpected API call")), \
                        contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    sync.main(["--root", tmp, "--apply"])
            self.assertEqual(self.writes, [])


if __name__ == "__main__":
    unittest.main()
