"""Synthetic official LINE WORKS authentication and REST boundary tests."""
import base64
import hashlib
import hmac
import io
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from adapters.lineworks import ClientError, Credentials, LineWorksClient, verify_signature
from adapters.lineworks import client as module


def credentials():
    return Credentials("client-synthetic", "secret-synthetic", "service@example.invalid",
                       "/synthetic/private.pem", "2000001")


class Wire:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body, timeout))
        status, data = self.responses.pop(0)
        return status, {}, data if isinstance(data, bytes) else json.dumps(data).encode()


TOKEN = {"access_token": "token-synthetic", "token_type": "Bearer", "expires_in": "3600"}


def client(wire, **kwargs):
    return LineWorksClient(credentials(), transport=wire,
                           signer=lambda *args: b"synthetic-signature", **kwargs)


def test_official_jwt_form_and_message_receipt():
    wire = Wire([(200, TOKEN), (201, b"")])
    api = client(wire, clock=lambda: 1700000000)
    assert api.send_message({"type": "text", "text": "完全合成"},
                            user_id="user@example.invalid") == {"status": 201}
    form = parse_qs(wire.calls[0][3].decode())
    assertion = form["assertion"][0].split(".")
    def decode(value):
        return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    assert decode(assertion[0]) == {"alg": "RS256", "typ": "JWT"}
    assert decode(assertion[1]) == {"iss": "client-synthetic", "sub": "service@example.invalid",
                                   "iat": 1699999970, "exp": 1700003300}
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    assert form["scope"] == ["bot.message"]
    assert "Authorization" not in wire.calls[0][2]
    assert wire.calls[1][1].endswith("/users/user%40example.invalid/messages")
    assert wire.calls[1][2]["Authorization"] == "Bearer token-synthetic"
    assert json.loads(wire.calls[1][3]) == {"content": {"type": "text", "text": "完全合成"}}


def test_token_cache_refresh_and_concurrent_single_auth():
    now = [0]
    wire = Wire([(200, TOKEN)] + [(201, b"")] * 12 + [(200, TOKEN), (201, b"")])
    api = client(wire, monotonic=lambda: now[0])
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: api.send_message({"type": "text", "text": "x"},
                                                                channel_id="room"), range(12)))
    assert results == [{"status": 201}] * 12
    assert sum(c[1] == module.AUTH_URL for c in wire.calls) == 1
    now[0] = 3570
    api.send_message({"type": "text", "text": "x"}, channel_id="room")
    assert sum(c[1] == module.AUTH_URL for c in wire.calls) == 2


@pytest.mark.parametrize("status", [401, 403, 429, 500, 302])
def test_failed_send_never_retries_and_redacts_response(status):
    wire = Wire([(200, TOKEN), (status, {"secret": "DO-NOT-PRINT"})])
    api = client(wire)
    with pytest.raises(ClientError) as error:
        api.send_message({"type": "text", "text": "x"}, channel_id="room")
    assert error.value.status == status
    assert "DO-NOT-PRINT" not in str(error.value)
    assert len(wire.calls) == 2
    if status == 401:
        assert api._token == ""


def test_auth_401_does_not_deadlock_or_send_message():
    wire = Wire([(401, b"credential-value")])
    with pytest.raises(ClientError) as error:
        client(wire).send_message({"type": "text", "text": "x"}, channel_id="room")
    assert error.value.status == 401
    assert len(wire.calls) == 1


def test_rate_limit_reset_is_case_insensitive_and_prevents_early_new_calls():
    now = [100.0]
    wire = Wire([(200, TOKEN), (429, b"rejected"), (201, b"")])

    def transport(*args):
        status, headers, body = wire(*args)
        return status, {"rAtElImIt-ReSeT": "12"} if status == 429 else headers, body

    api = client(transport, monotonic=lambda: now[0])
    for instant in (100, 111):
        now[0] = instant
        with pytest.raises(ClientError, match="rate_limited"):
            api.send_message({"type": "text", "text": "合成"}, channel_id="room")
        assert len(wire.calls) == 2
    now[0] = 112
    assert api.send_message({"type": "text", "text": "合成"}, channel_id="room") == {"status": 201}
    assert len(wire.calls) == 3


def test_content_limits_fail_before_authentication():
    wire = Wire([])
    api = client(wire)
    for content in ({"type": "text", "text": "x" * 2001},
                    {"type": "file", "fileId": "bad\n"},
                    {"type": "unknown"},
                    {"type": "button_template", "contentText": "x" * 1001, "actions": []},
                    {"type": "button_template", "contentText": "合成", "actions": [
                        {"type": "message", "label": "操作", "postback": "x"}] * 11}):
        with pytest.raises(ClientError, match="validation_invalid"):
            api.send_message(content, channel_id="room")
    assert wire.calls == []


def test_attachment_local_size_limit_fails_before_authentication():
    wire = Wire([])
    with pytest.raises(ClientError, match="validation_invalid"):
        client(wire, max_upload_bytes=4).upload_file(b"12345", "synthetic.txt")
    assert wire.calls == []


def test_empty_file_remains_a_file_upload_not_a_dropped_attachment():
    wire = Wire([(200, TOKEN), (200, {"fileId": "file", "uploadUrl":
                                      "https://storage.worksmobile.com/k/emsg/empty"}),
                 (201, {"fileId": "file-empty"})])
    assert client(wire).upload_file(b"", "empty.txt") == "file-empty"
    assert len(wire.calls) == 3


@pytest.mark.parametrize("response", [[], {**TOKEN, "token_type": []},
                                      {**TOKEN, "expires_in": True},
                                      {**TOKEN, "expires_in": "nan"},
                                      {**TOKEN, "expires_in": "9" * 5000},
                                      {**TOKEN, "access_token": "bad\r\nheader"}])
def test_malformed_token_prevents_send(response):
    wire = Wire([(200, response)])
    with pytest.raises(ClientError, match="response_invalid"):
        client(wire).send_message({"type": "text", "text": "x"}, channel_id="room")
    assert len(wire.calls) == 1


@pytest.mark.parametrize("targets", [{}, {"user_id": "u", "channel_id": "c"},
                                     {"user_id": "me"}, {"channel_id": "bad\n"}])
def test_invalid_target_never_authenticates(targets):
    wire = Wire([])
    with pytest.raises(ClientError, match="validation_invalid"):
        client(wire).send_message({"type": "text", "text": "x"}, **targets)
    assert wire.calls == []


def test_file_upload_official_multipart_and_file_id():
    url = "https://apis-storage.worksmobile.com/k/emsg/r/jp1/synthetic/report.txt"
    wire = Wire([(200, TOKEN), (200, {"fileId": "file-synthetic", "uploadUrl": url}),
                 (201, {"fileId": "file-synthetic", "fileName": "完全合成.txt", "fileSize": 7})])
    assert client(wire).upload_file(b"fixture", "完全合成.txt") == "file-synthetic"
    assert json.loads(wire.calls[1][3]) == {"fileName": "完全合成.txt"}
    method, endpoint, headers, body, _ = wire.calls[2]
    assert method == "POST" and endpoint == url
    assert headers["Authorization"] == "Bearer token-synthetic"
    assert b'name="Filedata"' in body and b'name="resourceName"' in body
    assert 'filename="完全合成.txt"'.encode() in body
    assert b"\r\nfixture\r\n" in body
    boundary = headers["Content-Type"].split("boundary=", 1)[1].encode()
    assert body.endswith(b"--" + boundary + b"--\r\n")


@pytest.mark.parametrize("url", ["http://apis-storage.worksmobile.com/k/emsg/x",
                                 "https://apis-storage.worksmobile.com.evil.invalid/k/emsg/x",
                                 "https://evil.invalid/k/emsg/x", "https://127.0.0.1/k/emsg/x",
                                 "https://apis-storage.worksmobile.com:444/k/emsg/x",
                                 "https://user@apis-storage.worksmobile.com/k/emsg/x",
                                 "https://apis-storage.worksmobile.com/k/emsg/x#fragment",
                                 "https://apis-storage.worksmobile.com/unrelated/x"])
def test_untrusted_upload_url_never_receives_file_or_token(url):
    wire = Wire([(200, TOKEN), (200, {"fileId": "file", "uploadUrl": url})])
    with pytest.raises(ClientError, match="upload_url_invalid"):
        client(wire).upload_file(b"fixture", "fixture.txt")
    assert len(wire.calls) == 2


def test_upload_uses_final_file_id_and_oversized_response_fails_closed():
    wire = Wire([(200, TOKEN), (200, {"fileId": "file", "uploadUrl":
                                     "https://storage.worksmobile.com/k/emsg/x"}),
                 (201, {"fileId": "different"})])
    assert client(wire).upload_file(b"fixture", "fixture.txt") == "different"
    wire = Wire([(200, b"x" * (module.MAX_RESPONSE_BYTES + 1))])
    with pytest.raises(ClientError, match="response_invalid"):
        client(wire).send_message({"type": "text", "text": "x"}, channel_id="room")


@pytest.mark.parametrize("file_id", [None, [], "", "bad\r\n"])
def test_upload_invalid_final_file_id_fails_closed(file_id):
    wire = Wire([(200, TOKEN), (200, {"fileId": "reserved", "uploadUrl":
                                     "https://storage.worksmobile.com/k/emsg/x"}),
                 (201, {"fileId": file_id})])
    with pytest.raises(ClientError, match="response_invalid"):
        client(wire).upload_file(b"fixture", "fixture.txt")


def test_callback_signature_is_over_raw_body_and_constant_time():
    body, secret = b'{ "type": "message" }', "secret-synthetic"
    signature = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    assert verify_signature(body, signature, secret)
    assert not verify_signature(body.replace(b" ", b""), signature, secret)
    assert not verify_signature(body, signature, "wrong")
    for invalid in ("", "?" * 44, "ü" * 44, None):
        assert not verify_signature(body, invalid, secret)


def test_openssl_signer_stdin_timeout_redaction_and_permissions(monkeypatch, tmp_path):
    key = tmp_path / "synthetic.pem"
    key.write_text("not-a-real-key")
    key.chmod(0o600)
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"s" * 256)

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module._sign(b"signing-input", str(key), 3) == b"s" * 256
    args, kwargs = calls[0]
    assert "signing-input" not in args and kwargs["input"] == b"signing-input"
    assert kwargs["timeout"] == 3 and kwargs["stderr"] is subprocess.DEVNULL
    assert args[-2:] == ["-sigopt", "rsa_padding_mode:pkcs1"]
    key.chmod(0o644)
    with pytest.raises(ClientError, match="private_key_permissions"):
        module._sign(b"x", str(key), 3)
    assert len(calls) == 1


def test_default_transport_absolute_deadline_and_unknown_no_retry(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        raise subprocess.TimeoutExpired(args, kwargs["timeout"], stderr=b"SECRET")

    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(ClientError, match="transport_unknown") as error:
        module._http_request("POST", module.AUTH_URL, {"Content-Type": "application/json"}, b"{}", 3)
    assert "SECRET" not in str(error.value) and len(calls) == 1
    assert calls[0][1]["timeout"] == 3
    assert module._NoRedirect().redirect_request(None, None, 302, None, None, "https://evil.invalid") is None


@pytest.mark.skipif(shutil.which("openssl") is None, reason="OpenSSL signing dependency unavailable")
def test_rs256_signature_verifies_with_synthetic_generated_rsa_key(tmp_path):
    key, public, signature = (tmp_path / name for name in ("key.pem", "public.pem", "signature.bin"))
    key.touch(mode=0o600)
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    subprocess.run(["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(public)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    signing_input = b"synthetic-header.synthetic-claims"
    signature.write_bytes(module._sign(signing_input, str(key), 5))
    verification = subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", str(public), "-signature", str(signature)],
        input=signing_input, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
    assert verification.returncode == 0 and verification.stdout.strip() == b"Verified OK"


def test_worker_disables_proxy_and_redirect_bounds_response(monkeypatch):
    observed = []
    class Response:
        code = 201
        headers = {"RateLimit-Reset": "12"}
        def read(self, limit):
            observed.append(limit)
            return b"{}"
        def close(self):
            observed.append("closed")

    class Opener:
        def open(self, req, timeout):
            assert req.full_url == module.AUTH_URL and timeout == 2
            return Response()

    def build(*handlers):
        assert handlers[0].proxies == {} and isinstance(handlers[1], module._NoRedirect)
        return Opener()

    envelope = {"url": module.AUTH_URL, "timeout": 2, "method": "POST",
                "body": base64.b64encode(b"{}").decode(),
                "headers": {"Content-Type": "application/json"}}
    monkeypatch.setattr(module.urllib.request, "build_opener", build)
    monkeypatch.setattr(module.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(envelope).encode())))
    output = io.StringIO()
    monkeypatch.setattr(module.sys, "stdout", output)
    module._http_worker()
    assert json.loads(output.getvalue())["status"] == 201
    assert json.loads(output.getvalue())["headers"] == {"RateLimit-Reset": "12"}
    assert observed == [module.MAX_RESPONSE_BYTES + 1, "closed"]
