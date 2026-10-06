"""Settled-card recovery indexes primary outcomes once per batch."""
import asyncio
from types import SimpleNamespace

from adapters.common import worker


def test_resume_batch_scans_primary_results_once():
    class Records(dict):
        scans = 0

        def values(self):
            self.scans += 1
            return super().values()

    records = Records({
        "old": [{"phase": "result", "delivery_id": "d0", "ts": 10,
                 "result": "delivered", "message_id": "old"}],
        "new": [{"phase": "result", "delivery_id": "d0", "ts": 10,
                 "result": "delivered", "message_id": "latest"},
                {"phase": "result", "delivery_id": "d0", "ts": 11,
                 "part_id": "body:1", "result": "delivered", "message_id": "part"}],
        "failed": [{"phase": "result", "delivery_id": "d1", "ts": 1,
                    "result": "delivered", "message_id": "old"},
                   {"phase": "result", "delivery_id": "d1", "ts": 2,
                    "result": "unknown"}],
        "missing": [{"phase": "started", "delivery_id": "d2"}],
        **{f"a{i}": [{"phase": "result", "delivery_id": f"d{i}",
                       "result": "delivered", "message_id": str(i)}]
           for i in range(3, 100)},
    })
    expected = {f"d{i}": worker._card_message_id(records, f"d{i}") for i in range(100)}
    assert worker._card_message_ids(records) == {
        did: mid for did, mid in expected.items() if did != "d2"}
    assert expected["d0"] == "latest" and expected["d1"] is None
    records.scans = 0
    done, resumed = [], {}
    instance = object.__new__(worker.DeliveryWorker)
    instance._jview = SimpleNamespace(refresh=lambda: records)
    instance._reg = SimpleNamespace(put_parts_done=done.append)

    def log(*args, **kwargs):
        raise AssertionError((args, kwargs))

    instance._log = log

    async def drive(claim, manifest, ctx, history):
        assert history is records
        resumed[claim["spec"]["delivery_id"]] = ctx["card_message_id"]

    instance._drive_parts = drive
    instance._worker_id = "synthetic"
    specs = [{"delivery_id": did, "delivery": {},
              "parts": {"manifest": [{"kind": "card"}, {"kind": "body_part"}]}}
             for did in expected]
    asyncio.run(instance._resume_dead(specs))
    assert records.scans == 1
    assert resumed == {did: mid for did, mid in expected.items() if mid is not None}
    assert set(done) == {"d1", "d2"}
    asyncio.run(instance._resume_dead([]))
    assert records.scans == 1
