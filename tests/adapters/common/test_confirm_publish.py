"""Shared confirm publish step: queued/failed/success keep the confirm
lifecycle, and the followup window walks round-robin in bounded slices."""
import asyncio

import pytest

from adapters.common import confirm, registry


def _taken(tmp_path):
    reg = registry.Registry(str(tmp_path))
    cid = confirm.open_confirm(reg, token="t", actor="a", origin={}, payload={})
    assert reg.confirm(cid)["token"] == "t"
    assert reg.take_confirm(cid, False) == "taken"
    return reg, cid


def _run(reg, cid, *, fail, queued, reraise=False):
    said = []

    async def publish():
        if fail:
            raise OSError("disk")

    async def is_queued():
        return queued

    async def note(text):
        said.append(text)

    ok = asyncio.run(confirm.publish_confirmed(
        reg, cid, publish, is_queued,
        on_queued=lambda: note("queued"), on_failed=lambda: note("failed"),
        reraise_queued=reraise))
    return ok, said


def test_success_consumes(tmp_path):
    reg, cid = _taken(tmp_path)
    assert _run(reg, cid, fail=False, queued=False) == (True, [])
    assert reg.confirm(cid)["consumed"] is True


def test_unqueued_failure_releases_for_retry(tmp_path):
    reg, cid = _taken(tmp_path)
    assert _run(reg, cid, fail=True, queued=False) == (False, ["failed"])
    assert reg.take_confirm(cid, False) == "taken"


def test_possibly_queued_stays_in_flight(tmp_path):
    reg, cid = _taken(tmp_path)
    assert _run(reg, cid, fail=True, queued=True) == (False, ["queued"])
    assert reg.take_confirm(cid, False) == "busy"


def test_possibly_queued_reraises_when_asked(tmp_path):
    reg, cid = _taken(tmp_path)
    with pytest.raises(OSError):
        _run(reg, cid, fail=True, queued=True, reraise=True)
    assert reg.take_confirm(cid, False) == "busy"


def test_followup_window_round_robin(tmp_path):
    reg = registry.Registry(str(tmp_path))
    assert confirm.followup_window(reg, 7) == (0, [])
    for i in range(5):
        reg.put_followup(f"c{i}", {"kind": "human"})
    cursor, window = confirm.followup_window(reg, 3, limit=4)
    assert [cid for cid, _ in window] == ["c3", "c4", "c0", "c1"]
    assert cursor == 2
