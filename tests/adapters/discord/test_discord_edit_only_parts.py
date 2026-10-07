"""Exact prior edits never match another post or upload a file on uncertainty."""
import asyncio
from types import SimpleNamespace
import pytest
from test_mcs_discord_delivery import _mkworker, FakeThread, _HistMsg, _discord
from discord_delivery_testkit import _spec, _claim, FakeHTTP, _sent_parts, _state


@pytest.fixture
def world(tmp_path, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "discord", _discord)
    worker, _, bot = _mkworker(tmp_path)
    thread = FakeThread(7700)
    thread.parent_id, thread.guild = 42, SimpleNamespace(id=7)
    bot.channels[7700] = thread
    target = _HistMsg(6001, "old summary")
    other = _HistMsg(6002, "placeholder")
    thread.sent = [target, other]
    spec = _spec(["placeholder"], op="update", thread_id="7700", prior={"body:0001": "6001"})
    part = spec["parts"]["manifest"][2]
    part["edit_only"] = True
    return worker, thread, target, other, spec, part


def run(world, **ctx_extra):
    w, t, *_rest, spec, part = world
    ctx = {"thread_id": "7700", "thread": t, "history": None, "consumed": set(), **ctx_extra}
    return asyncio.run(w._part(_claim(spec), part, ctx))


def test_exact_edit_ignores_identical_other_post_and_clears_old_view(world):
    out = run(world)
    assert out == {"result": "delivered", "remote_id": "6001"}
    assert world[2].edits == 1 and world[3].edits == 0
    assert world[2].content == "placeholder" and world[2].view is None
    assert len(world[1].sent) == 2


@pytest.mark.parametrize("fault", ["foreign", "scope", "timeout", "consumed", "lookup_gone", "edit_gone", "permission"])
def test_unknown_or_gone_never_posts(world, fault):
    if fault == "foreign":
        world[2].author = SimpleNamespace(id=999)
    elif fault == "scope":
        world[1].parent_id = 999
    elif fault == "timeout":
        world[2].edit_fail = TimeoutError()
    elif fault == "lookup_gone":
        world[1].fetch_fail = FakeHTTP(404)
    elif fault == "edit_gone":
        world[2].edit_fail = FakeHTTP(410)
    elif fault == "permission":
        world[2].edit_fail = FakeHTTP(403)
    out = run(world, consumed={6001} if fault == "consumed" else set())
    expected = "delivered" if fault in ("lookup_gone", "edit_gone") else "not_sent" if fault == "permission" else "unknown"
    assert out["result"] == expected
    assert len(world[1].sent) == 2 and world[3].edits == 0


def test_file_caption_edits_exact_owned_message_without_upload(world):
    world[2].attachments = [SimpleNamespace(filename="synthetic.pdf", size=10)]
    part = {"kind": "attachment_part", "part_id": "attach:0001", "prior_remote_id": "6001",
            "edit_only": True, "name": "synthetic.pdf", "bytes": 10, "sha256": "a" * 64, "caption": "metadata only"}
    w, t, _, _, spec, _ = world
    out = asyncio.run(w._part(_claim(spec), part, {"thread_id": "7700", "thread": t, "consumed": set()}))
    assert out == {"result": "delivered", "remote_id": "6001"}
    assert world[2].content == "metadata only" and len(t.sent) == 2
    part["name"] = "other.pdf"
    out = asyncio.run(w._part(_claim(spec), part, {"thread_id": "7700", "thread": t, "consumed": set()}))
    assert out["result"] == "unknown" and len(t.sent) == 2


def test_edit_unknown_is_journaled_and_never_resubmitted_on_resume(world, tmp_path):
    w, t, target, _, spec, part = world
    target.edit_fail = TimeoutError()
    claim = _claim(spec)
    ctx = {"thread_id": "7700", "thread": t, "consumed": set()}
    calls = []
    original = target.edit
    async def edit(**kwargs):
        calls.append(kwargs)
        return await original(**kwargs)
    target.edit = edit
    asyncio.run(w._attempt_part(claim, part, ctx))
    assert _sent_parts(_state(tmp_path))[part["part_id"]]["result"] == "unknown"
    target.edit_fail = None
    records = w._jview.refresh()
    asyncio.run(w._drive_parts(claim, [part], ctx, records))
    assert len(calls) == 1 and len(t.sent) == 2
