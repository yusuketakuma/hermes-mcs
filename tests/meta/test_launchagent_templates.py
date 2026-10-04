"""LaunchAgent templates make launchd-created logs owner-only."""
from pathlib import Path
import plistlib

ROOT = Path(__file__).resolve().parents[2]


def test_every_launchagent_template_sets_private_umask():
    templates = sorted((ROOT / "deployment/launchagents").glob("*.plist"))
    assert templates
    for path in templates:
        obj = plistlib.loads(path.read_bytes())
        assert obj.get("Umask") == 0o077, path.name
