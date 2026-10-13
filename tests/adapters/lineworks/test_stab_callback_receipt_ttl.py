"""Regression: content-free unknown callback receipts expire after a long TTL."""
import hashlib
import os
from pathlib import Path

from adapters.lineworks import server
from notify_testkit import NOW
from lineworks_testkit import SCOPE, SECRET, event, raw, signature, world


def _box(tmp_path, now):
    w = world(tmp_path)
    return server.CallbackInbox(Path(w.dirs["state"]) / "callbacks", w.settings, SECRET,
                                clock=lambda: now[0])


def test_unknown_receipt_survives_short_expiry_but_not_the_long_ttl(tmp_path):
    now = [NOW]
    box = _box(tmp_path, now)
    body = raw(event(text="SYNTHETIC-INPUT"))
    assert box.accept(body, signature(body), SCOPE["application_id"]) == 200
    working, _ = box.take(box.pending()[0])
    box.finish(working, "unknown")
    done = box.directory / (hashlib.sha256(body).hexdigest() + ".done")
    assert done.exists()

    # Within the replay window the receipt still dedups the same body.
    assert box.accept(body, signature(body), SCOPE["application_id"]) == 200
    assert box.pending() == []

    os.utime(done, (now[0] - 1201, now[0] - 1201))
    box.expire()
    assert done.exists() and server.callback_result(done) == "unknown"

    old = now[0] - server.UNKNOWN_DONE_TTL_S - 1
    os.utime(done, (old, old))
    box.expire()
    assert not done.exists()


def test_processed_receipt_still_expires_after_the_short_window(tmp_path):
    now = [NOW]
    box = _box(tmp_path, now)
    body = raw(event(text="SYNTHETIC-INPUT"))
    assert box.accept(body, signature(body), SCOPE["application_id"]) == 200
    working, _ = box.take(box.pending()[0])
    box.finish(working, "processed")
    done = box.directory / (hashlib.sha256(body).hexdigest() + ".done")
    os.utime(done, (now[0] - 1201, now[0] - 1201))
    box.expire()
    assert not done.exists()
