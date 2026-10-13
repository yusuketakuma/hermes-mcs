"""Shared synthetic LINE WORKS REST client wiring — credentials, a scripted
wire transport and the token response.  Not a test module (no ``test_`` prefix);
sibling files import it via the tests/ sys.path bootstrap."""
import json

from adapters.lineworks import Credentials, LineWorksClient


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
