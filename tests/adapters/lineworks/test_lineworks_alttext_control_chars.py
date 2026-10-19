"""A C0 control character in a post preview must not block its LINE WORKS card delivery."""
import asyncio
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "lineworks"))

from lineworks_testkit import overflow_spec, world  # noqa: E402

from adapters.common import envelopes  # noqa: E402
from adapters.lineworks.client import AUTH_URL, Credentials, LineWorksClient  # noqa: E402


def _real_client(calls):
    def transport(method, url, headers, body, timeout):
        calls.append(url)
        if url == AUTH_URL:
            return 200, {}, json.dumps({"access_token": "synthetic-token", "token_type": "Bearer",
                                        "expires_in": 3600}).encode()
        return 201, {}, b"{}"

    creds = Credentials(client_id="synthetic-client", client_secret="synthetic-secret",
                        service_account="synthetic@example.invalid",
                        private_key_path="/nonexistent/synthetic.key", bot_id="2000001")
    return LineWorksClient(creds, transport=transport, signer=lambda *a: b"s" * 256)


@pytest.mark.parametrize("PREVIEW", ["合成 患者: 合成本文ベル / 合成さん",
                                     "合成 患者: 合成本文\x07ベル / 合成さん"], ids=["clean", "bel"])
def test_card_with_control_char_in_preview_is_still_delivered(tmp_path, PREVIEW):
    # A staff post body containing a non-whitespace C0 control (BEL) survives
    # notify_render.inline_text / text.notification_preview (" ".join(split()))
    # and becomes the flex altText, which client._validate_content rejects.
    w = world(tmp_path)
    calls = []
    w.sender.client = _real_client(calls)
    spec = overflow_spec()
    parts = spec["parts"]
    parts["preview_text"] = PREVIEW
    payload = envelopes.canonical({k: parts[k] for k in
                                   ("containers", "footer", "action_rows", "preview_text")})
    parts["manifest"][0].update(bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    out = asyncio.run(w.sender.perform(spec))
    assert out["result"] == "delivered", out
