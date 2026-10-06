"""KeepAlive llama-server must not crash-loop at launchd's 10s default."""
from pathlib import Path
import plistlib

ROOT = Path(__file__).resolve().parents[2]


def test_llamaserver_keepalive_is_throttled():
    obj = plistlib.loads(
        (ROOT / "deployment/launchagents/ai.mcs.llamaserver.plist").read_bytes())
    assert obj["KeepAlive"] is True
    assert obj["ThrottleInterval"] >= 30
