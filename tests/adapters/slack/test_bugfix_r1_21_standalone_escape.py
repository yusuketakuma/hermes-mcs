import asyncio

from adapters.slack import standalone
from test_slack_standalone import Client, config, wire_client  # noqa: F401


def test_send_escapes_slack_control_sequences(config, tmp_path, monkeypatch):  # noqa: F811
    client = Client()
    wire_client(monkeypatch, client)
    payload = {"text": "<!channel> a&b <@U1>"}
    outcome = asyncio.run(standalone.send(tmp_path, "slack:C_SYNTHETIC", payload))
    assert outcome["result"] == "delivered"
    post = next(kwargs for op, kwargs in client.calls if op == "create")
    assert post["text"] == "&lt;!channel&gt; a&amp;b &lt;@U1&gt;"
