"""Keep obsolete PR checks from delaying current checks, not release writes."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ReleaseNotesConcurrency(unittest.TestCase):
    def test_only_pull_requests_cancel_in_progress(self):
        workflow = (ROOT / ".github/workflows/release-notes.yml").read_text()
        block = workflow.split("\nconcurrency:\n", 1)[1].split("\njobs:\n", 1)[0]
        self.assertIn(
            "  cancel-in-progress: ${{ github.event_name == 'pull_request' "
            "|| inputs.action == 'pr_check' }}\n",
            block,
        )
        # Different PRs remain isolated; all non-PR writers keep the same queue.
        self.assertIn(
            "  group: japanese-release-notes-${{ (github.event_name == 'pull_request' "
            "|| inputs.action == 'pr_check') && github.ref || 'publish' }}\n",
            block,
        )
        self.assertEqual(block.count("cancel-in-progress:"), 1)

    def test_pr_check_dispatch_runs_only_the_read_only_check(self):
        workflow = (ROOT / ".github/workflows/release-notes.yml").read_text()
        self.assertIn("options: [sync_existing, draft, pr_check]", workflow)
        jobs = workflow.split("\njobs:\n", 1)[1]
        check, rest = jobs.split("\n  sync-existing:\n", 1)
        self.assertNotIn("write", check)
        self.assertIn("inputs.action == 'pr_check') && github.ref || 'main'", check)
        # the writing jobs are selected by other actions only
        self.assertNotIn("pr_check", rest)
        self.assertIn("inputs.action == 'sync_existing'", rest)
        self.assertIn("inputs.action == 'draft'", rest)


if __name__ == "__main__":
    unittest.main()
