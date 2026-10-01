"""GitHubへ接続せず、既存Releaseの本文だけが同期されることを検証する。"""

import copy
import importlib.util
from pathlib import Path
import sys
import unittest


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "development"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("sync_release_notes", SCRIPTS / "sync_release_notes.py")
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)
notes = __import__("release_notes")


class SyncTest(unittest.TestCase):
    def setUp(self):
        item = dict(category="fixed", title="合成の修正", summary="通知を更新します。",
                    upgrade="追加操作は不要です。", details=[], refs=["#1"])
        self.text = "## [Unreleased]\n\n## [1.0.0] — 2026-09-21\n\n" + notes.render(
            [item], "通知を修正", "合成の説明です。")
        self.record = dict(id=1, tag_name="v1.0.0", name="old", body="old body", draft=False,
                           prerelease=False, target_commitish="main", created_at="a",
                           published_at="b", updated_at="c")
        self.writes = []

    def request(self, endpoint, payload=None):
        if "?" in endpoint:
            return [copy.deepcopy(self.record)]
        if payload is not None:
            self.writes.append(payload)
            self.record.update(payload)
        return copy.deepcopy(self.record)

    def test_dry_run_does_not_write(self):
        result = sync.synchronize(self.text, self.request)
        self.assertEqual(result["would_update"], ["v1.0.0"])
        self.assertEqual(self.writes, [])

    def test_exact_body_and_idempotence(self):
        before = copy.deepcopy(self.record)
        result = sync.synchronize(self.text, self.request, apply=True)
        self.assertEqual(result["updated"], ["v1.0.0"])
        self.assertEqual(set(self.writes[0]), {"name", "body"})
        self.assertEqual(self.record["body"], notes.section(self.text, "1.0.0"))
        for key in sync.IDENTITY:
            self.assertEqual(self.record[key], before[key])
        self.assertEqual(sync.synchronize(self.text, self.request, apply=True)["updated"], [])
        self.assertEqual(len(self.writes), 1)

    def test_unknown_version_fails_before_any_write(self):
        def request(endpoint, payload=None):
            return [self.record, self.record | {"id": 2, "tag_name": "v9.0.0"}]
        with self.assertRaises(ValueError):
            sync.synchronize(self.text, request, apply=True)
        self.assertEqual(self.writes, [])

    def test_concurrent_edit_is_not_overwritten(self):
        def request(endpoint, payload=None):
            if "?" in endpoint:
                return [copy.deepcopy(self.record)]
            return self.record | {"updated_at": "newer", "body": "edited by another user"}
        with self.assertRaises(ValueError):
            sync.synchronize(self.text, request, apply=True)


if __name__ == "__main__":
    unittest.main()
