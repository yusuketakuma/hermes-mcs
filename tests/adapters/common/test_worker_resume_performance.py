"""Settled-card recovery indexes primary outcomes once per batch."""
import asyncio
from types import SimpleNamespace

import pytest

from adapters.common import journal, worker


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


@pytest.mark.parametrize("ts", [None, True, "2", "", [], {}, float("nan"),
                               float("inf"), float("-inf")])
def test_bad_timestamp_holds_only_its_delivery(ts):
    records = {
        "bad": [{"phase": "result", "delivery_id": "bad", "ts": 1,
                 "result": "delivered", "message_id": "earlier"},
                {"phase": "result", "delivery_id": "bad", "ts": ts,
                 "result": "delivered", "message_id": "unsafe"}],
        "good": [{"phase": "result", "delivery_id": "good", "ts": 2,
                  "result": "delivered", "message_id": "safe"}],
    }
    assert worker._card_message_ids(records) == {"bad": None, "good": "safe"}
    with pytest.raises(ValueError, match="card_result_timestamp_invalid"):
        worker._card_message_id(records, "bad")
    assert worker._card_message_id(records, "good") == "safe"
    done, resumed, errors = [], [], []
    instance = object.__new__(worker.DeliveryWorker)
    instance._jview = SimpleNamespace(refresh=lambda: records)
    instance._reg = SimpleNamespace(put_parts_done=done.append)
    instance._worker_id = "synthetic"

    def log(*args, **kwargs):
        errors.append((args, kwargs))

    instance._log = log

    async def drive(claim, manifest, ctx, history):
        resumed.append((claim["spec"]["delivery_id"], ctx["card_message_id"]))

    instance._drive_parts = drive
    specs = [{"delivery_id": did, "delivery": {},
              "parts": {"manifest": [{"kind": "card"}, {"kind": "body_part"}]}}
             for did in ("bad", "good")]
    asyncio.run(instance._resume_dead(specs))
    assert resumed == [("good", "safe")]
    assert done == []
    assert errors == [(("parts_resume_error",), {"delivery_id": "bad", "error": "ValueError"})]
    with pytest.raises(ValueError, match="card_result_timestamp_invalid"):
        asyncio.run(instance._resume_parts(specs[0]))
    assert done == []


def test_cached_view_uses_flat_rows_and_preserves_attempt_order_ties():
    class View(journal._View):
        lookups = 0

        def __getitem__(self, aid):
            self.lookups += 1
            return super().__getitem__(aid)

    def result(aid, did, mid, ts=10, **fields):
        return {"phase": "result", "attempt_id": aid, "delivery_id": did,
                "ts": ts, "result": "delivered", "message_id": mid, **fields}

    files = [
        ([result("a", "same", "a1"), result("b", "same", "b"),
          result("c", "unknown", "old")], {"a": [0], "b": [1], "c": [2]}, 3, {}),
        ([result("a", "same", "a2"), result("c", "unknown", None, result="unknown")],
         {"a": [0], "c": [1]}, 2,
         {"a": [result("a", "same", "tail")]}),
    ]
    # Add many distinct attempts/files: values() would probe every file
    # for every attempt, while the flat path never calls __getitem__.
    for f in range(30):
        rows = [result(f"x{f}-{i}", f"x{f}-{i}", str(i)) for i in range(40)]
        files.append((rows, {r["attempt_id"]: [i] for i, r in enumerate(rows)}, len(rows), {}))
    view = View(files)
    expected = {}
    for rows in view.values():
        for row in rows:
            expected.setdefault(row["delivery_id"], []).append(row)
    expected = {did: sorted(rows, key=lambda row: row["ts"])[-1]
                for did, rows in expected.items()}
    expected = {did: row["message_id"] if row["result"] == "delivered" else None
                for did, row in expected.items()}
    assert expected["same"] == "b" and expected["unknown"] is None
    view.lookups = 0
    assert worker._card_message_ids(view) == expected
    assert view.lookups == 0
