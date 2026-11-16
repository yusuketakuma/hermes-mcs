"""Keep obsolete PR checks from delaying current checks, not release writes."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ReleaseNotesConcurrency(unittest.TestCase):
    def test_only_pull_requests_cancel_in_progress(self):
        workflow = (ROOT / ".github/workflows/release-notes.yml").read_text()
        block = workflow.split("\nconcurrency:\n", 1)[1].split("\njobs:\n", 1)[0]
        self.assertIn(
            "  cancel-in-progress: ${{ github.event_name == 'pull_request' }}\n",
            block,
        )
        # Different PRs remain isolated; all non-PR writers keep the same queue.
        self.assertIn(
            "  group: japanese-release-notes-${{ github.event_name == 'pull_request' "
            "&& github.ref || 'publish' }}\n",
            block,
        )
        self.assertEqual(block.count("cancel-in-progress:"), 1)


if __name__ == "__main__":
    unittest.main()
