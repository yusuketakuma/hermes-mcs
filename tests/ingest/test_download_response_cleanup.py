"""Unused attachment HTTPError responses close without reading their bodies."""
import io
import urllib.error

import pytest

from mcs_contract import MCSError
import mcs_download


@pytest.mark.parametrize("case", [401, 404, 425, 503, "redirect_success",
                                 "redirect_rejected", "redirect_follow_failure",
                                 "close_failure"])
def test_attachment_closes_every_consumed_http_error(tmp_path, monkeypatch, case):
    bodies, errors, requests = [], [], []

    class Body(io.BytesIO):
        def read(self, *args):
            raise AssertionError("unused HTTP error body must never be drained")

        def close(self):
            already_closed = self.closed
            super().close()
            if case == "close_failure" and not already_closed:
                raise OSError("synthetic close failure")

    def error(url, status, headers):
        body = Body(b"synthetic error body")
        bodies.append(body)
        value = urllib.error.HTTPError(url, status, "synthetic", headers, body)
        # Retain errors so GC cannot mask missing explicit cleanup.
        errors.append(value)
        return value

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            if len(requests) == 1:
                if case in ("redirect_success", "redirect_follow_failure"):
                    location = "https://files.medical-care.net/signed"
                    raise error(request.full_url, 302, {"location": location})
                if case == "redirect_rejected":
                    raise error(request.full_url, 302,
                                {"location": "https://outside.invalid/f"})
                raise error(request.full_url, 425 if case == "close_failure" else case, {})
            if case == "redirect_follow_failure":
                raise error(request.full_url, 425, {})
            return io.BytesIO(b"synthetic attachment")

    monkeypatch.setattr(mcs_download, "no_proxy_opener", lambda *_: Opener())
    partial = tmp_path / "synthetic.part"
    if case == "redirect_success":
        result = mcs_download.download_to_part(
            "https://www.medical-care.net/f", str(partial),
            token="synthetic-token", timeout=1)
        assert result["bytes"] == len(b"synthetic attachment")
        assert partial.read_bytes() == b"synthetic attachment"
    else:
        with pytest.raises(MCSError) as raised:
            mcs_download.download_to_part(
                "https://www.medical-care.net/f", str(partial),
                token="synthetic-token", timeout=1)
        status = 425 if case in ("redirect_follow_failure", "close_failure") else case
        if case == "redirect_rejected":
            assert raised.value.kind == "url_not_allowed"
        else:
            assert raised.value.kind == "http_error"
            assert raised.value.status == status
            assert raised.value.retryable is (status in (425, 503))
        assert raised.value.__cause__ is errors[-1]
        assert not partial.exists()
    assert errors and all(body.closed for body in bodies)
    assert requests[0].get_header("Authorization") == "Bearer synthetic-token"
    if len(requests) > 1:
        assert requests[1].get_header("Authorization") is None
