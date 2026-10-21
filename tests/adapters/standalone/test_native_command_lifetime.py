"""Native thread lifetime survives repeated listener cancellation."""
import asyncio
import threading

import pytest

from adapters.common.commands import run_native


async def _wait_event(event):
    deadline = asyncio.get_running_loop().time() + 5
    while not event.is_set():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("synthetic worker did not reach its checkpoint")
        await asyncio.sleep(0.005)


@pytest.mark.parametrize("fails", [False, True])
def test_repeated_cancel_keeps_ownership_and_observes_worker_failure(fails):
    started, release, ended = (threading.Event() for _ in range(3))

    def handler():
        started.set()
        try:
            assert release.wait(5), "synthetic worker was not released"
            if fails:
                raise ValueError("synthetic worker failure")
            return "queued"
        finally:
            ended.set()

    async def scenario():
        unhandled = []
        asyncio.get_running_loop().set_exception_handler(lambda _loop, ctx: unhandled.append(ctx))
        task = asyncio.create_task(run_native(handler))
        try:
            await _wait_event(started)
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
                assert not ended.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
            assert ended.is_set()
            await asyncio.sleep(0)
            assert not unhandled
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_uncancelled_native_result_and_error_are_preserved():
    error = ValueError("synthetic failure")

    def fail():
        raise error

    async def scenario():
        assert await run_native(lambda value, *, suffix: value + suffix, "queued", suffix="!") == "queued!"
        with pytest.raises(ValueError) as caught:
            await run_native(fail)
        assert caught.value is error

    asyncio.run(scenario())
