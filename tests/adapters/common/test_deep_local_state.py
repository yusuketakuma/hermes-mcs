"""Deep synthetic local JSON follows the same refusal/default path as invalid JSON."""
import asyncio
from pathlib import Path

import pytest

from adapters.common import registry, summary
from test_lineworks_adapter import SCOPE, overflow_spec, world

DEEP = b"[" * 20000 + b"]" * 20000


@pytest.mark.parametrize("raw", [b"{", DEEP], ids=["malformed", "deep"])
def test_unreadable_legacy_registry_never_imports_unproven_authority(tmp_path, raw):
    legacy = tmp_path / "registry.json"
    legacy.write_bytes(raw)
    reg = registry.Registry(str(tmp_path), scope=SCOPE)
    assert reg.claims() == {} and reg._data["tokens"] == {}
    assert reg._data["pending_confirms"] == {} and reg.followups() == {}
    assert legacy.read_bytes() == raw


@pytest.mark.parametrize("raw", [b"{", DEEP], ids=["malformed", "deep"])
def test_unreadable_summary_config_preserves_private_scope_and_defaults(tmp_path, monkeypatch, raw):
    # Only a temporary synthetic snapshot and stub render/view are opened.
    snapshot = tmp_path / "data" / "snapshots" / "synthetic.db"
    snapshot.parent.mkdir(parents=True)
    snapshot.touch()
    (tmp_path / "config.json").write_bytes(raw)
    seen = []

    class View:
        @staticmethod
        def view(db, cfg, scope, **kwargs):
            seen.append((cfg, scope, kwargs["allowed"]))
            return {"parts": {"footer": []}}

    class Render:
        @staticmethod
        def parts_text(parts, dialect):
            return "合成サマリー"

    monkeypatch.setattr(summary, "_modules", lambda: (View, Render))
    assert summary.answer(snapshot, "all", allowed=[1], now=snapshot.stat().st_mtime) == {
        "text": "合成サマリー", "parts": {"footer": []}}
    assert seen == [({}, "all", [1])]


@pytest.mark.parametrize("raw", [b"{", DEEP], ids=["malformed", "deep"])
def test_corrupt_shared_cooldown_proves_no_wire_attempt(tmp_path, raw):
    w = world(tmp_path)
    Path(w.dirs["state"], "rate-limit.json").write_bytes(raw)
    outcome = asyncio.run(w.sender.perform(overflow_spec()))
    assert outcome == {"result": "not_sent", "error_code": "rate_state_invalid"}
    assert w.client.calls == []
    assert Path(w.dirs["state"], "rate-limit.json").read_bytes() == raw
