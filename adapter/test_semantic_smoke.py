"""The opt-in wire smoke must spend at most one synthetic request."""
import pytest

import semantic_jev as jev


def test_wire_smoke_does_not_retry_a_synthetic_503(monkeypatch):
    calls = []
    real_client = jev.JevClient

    def post(_body, _timeout):
        calls.append(True)
        return 503, {}, b""

    def client_factory(*args, **kwargs):
        kwargs["post_fn"] = post
        return real_client(*args, **kwargs)

    monkeypatch.setattr(jev, "JevClient", client_factory)
    monkeypatch.setattr(jev.time, "sleep", lambda _seconds: None)
    with pytest.raises(jev.JevError, match="transport"):
        jev.wire_smoke("synthetic", timeout=1)
    assert len(calls) == 1
