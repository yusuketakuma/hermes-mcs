"""GitHubへ接続せず、既存Releaseの本文だけが同期されることを検証する。"""

import copy
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
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
        self.assertEqual(set(self.writes[0]), {"name", "body", "tag_name"})
        self.assertEqual(self.record["body"], notes.section(self.text, "1.0.0"))
        for key in sync.IDENTITY:
            self.assertEqual(self.record[key], before[key])
        self.assertEqual(sync.synchronize(self.text, self.request, apply=True)["updated"], [])
        self.assertEqual(len(self.writes), 1)

    def test_draft_keeps_its_tag_when_api_requires_it(self):
        self.record.update(draft=True, published_at=None)
        before = copy.deepcopy(self.record)

        def request(endpoint, payload=None):
            if payload is not None and "tag_name" not in payload:
                self.record["tag_name"] = "untagged-example"
            return self.request(endpoint, payload)

        sync.synchronize(self.text, request, apply=True)
        for key in sync.IDENTITY:
            self.assertEqual(self.record[key], before[key])
        self.assertEqual(self.record["body"], notes.section(self.text, "1.0.0"))
        self.assertEqual(sync.synchronize(self.text, request, apply=True)["updated"], [])

    def test_identity_change_on_verification_is_detected(self):
        def request(endpoint, payload=None):
            result = self.request(endpoint, payload)
            if payload is None and "?" not in endpoint and self.writes:
                result["tag_name"] = "untagged-example"
            return result

        with self.assertRaises(ValueError):
            sync.synchronize(self.text, request, apply=True)

    def test_api_rejects_state_fields_and_invalid_tags_before_transport(self):
        for payload in [
            dict(name="example", body="example", tag_name="v1.0.0", draft=False),
            dict(name="example", body="example", tag_name="untagged-example"),
            dict(name="example", body="example"),
        ]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                sync.api("example/repo", "releases/1", payload)

    def test_sync_command_failure_fails_ci(self):
        workflow = (SCRIPTS.parents[1] / ".github/workflows/release-notes.yml").read_text()
        step = workflow.split("- name: Synchronize existing release notes only\n", 1)[1]
        step = step.split("      - name:", 1)[0]
        run = step.split("        run:", 1)[1]
        script = textwrap.dedent(run.removeprefix(" |\n")) if run.startswith(" |\n") else run.strip()
        with tempfile.TemporaryDirectory() as directory:
            python = Path(directory) / "python3"
            python.write_text("#!/bin/sh\nexit 17\n")
            python.chmod(0o700)
            result = subprocess.run(
                ["bash", "-e", "-c", script], cwd=directory,
                env=os.environ | {"PATH": directory + os.pathsep + os.defpath},
                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 17, result.stderr)

    def test_unknown_version_fails_before_any_write(self):
        def request(endpoint, payload=None):
            if "?" in endpoint and payload is None:
                return [copy.deepcopy(self.record),
                        self.record | {"id": 2, "tag_name": "v9.0.0"}]
            return self.request(endpoint, payload)
        with self.assertRaises(ValueError):
            sync.synchronize(self.text, request, apply=True)
        self.assertEqual(self.writes, [])

    def test_concurrent_edit_is_not_overwritten(self):
        def request(endpoint, payload=None):
            result = self.request(endpoint, payload)
            if "?" not in endpoint and payload is None:
                return result | {"updated_at": "newer", "body": "edited by another user"}
            return result
        with self.assertRaises(ValueError):
            sync.synchronize(self.text, request, apply=True)
        self.assertEqual(self.writes, [])

    def test_created_at_drift_after_patch_is_accepted(self):
        # GitHub derives created_at from the target tag's date, so a PATCH that
        # re-resolves tag_name may legitimately return a different value.
        drifted = "2099-01-01T00:00:00Z"

        def request(endpoint, payload=None):
            if payload is not None and "?" not in endpoint:
                self.record["created_at"] = drifted
            return self.request(endpoint, payload)

        result = sync.synchronize(self.text, request, apply=True)
        self.assertEqual(result["updated"], ["v1.0.0"])
        self.assertEqual(self.record["created_at"], drifted)
        self.assertEqual(sync.synchronize(self.text, request, apply=True)["updated"], [])
        self.assertEqual(len(self.writes), 1)

    def test_created_at_drift_before_patch_is_rejected(self):
        def request(endpoint, payload=None):
            result = self.request(endpoint, payload)
            if "?" not in endpoint and payload is None:
                return result | {"created_at": "drifted"}
            return result
        with self.assertRaises(ValueError):
            sync.synchronize(self.text, request, apply=True)
        self.assertEqual(self.writes, [])

    def test_created_at_missing_or_invalid_after_patch_is_rejected(self):
        sentinel = object()
        for bad in (sentinel, None, 123, ""):
            for when in ("patch", "verify"):
                with self.subTest(bad=repr(bad), when=when):
                    self.setUp()

                    def request(endpoint, payload=None):
                        result = self.request(endpoint, payload)
                        hit = (payload is not None if when == "patch"
                               else payload is None and bool(self.writes))
                        if "?" not in endpoint and hit:
                            if bad is sentinel:
                                result.pop("created_at", None)
                            else:
                                result["created_at"] = bad
                        return result

                    with self.assertRaises(ValueError):
                        sync.synchronize(self.text, request, apply=True)

    def test_other_identity_fields_still_reject_post_write_drift(self):
        mutations = {
            "id": 2, "tag_name": "v2.0.0", "draft": True, "prerelease": True,
            "target_commitish": "other", "published_at": "2099-01-01T00:00:00Z",
        }
        for field, value in mutations.items():
            for when in ("patch", "verify"):
                with self.subTest(field=field, when=when):
                    self.setUp()

                    def request(endpoint, payload=None):
                        result = self.request(endpoint, payload)
                        hit = (payload is not None if when == "patch"
                               else payload is None and bool(self.writes))
                        if "?" not in endpoint and hit:
                            result[field] = value
                        return result

                    with self.assertRaises(ValueError):
                        sync.synchronize(self.text, request, apply=True)

    def test_name_or_body_mismatch_after_write_is_rejected(self):
        for field in ("name", "body"):
            with self.subTest(field=field):
                self.setUp()

                def request(endpoint, payload=None):
                    result = self.request(endpoint, payload)
                    if "?" not in endpoint and payload is None and self.writes:
                        result[field] = "tampered"
                    return result

                with self.assertRaises(ValueError):
                    sync.synchronize(self.text, request, apply=True)


if __name__ == "__main__":
    unittest.main()
