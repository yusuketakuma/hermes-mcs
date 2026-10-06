"""Malformed deeply nested model text must not stop format detection."""
import json

import pytest

import local_llm


@pytest.mark.parametrize('schema', [None, {'type': 'object'}])
def test_format_probe_rejects_deep_reply_without_raising(schema):
    text = '{"nested":' + '[' * 20000 + '0' + ']' * 20000 + '}'

    def response(*args, **kwargs):
        body = {'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}]}
        return 200, {}, json.dumps(body).encode()

    assert local_llm.probe_format(local_llm.ENDPOINT, local_llm.MODEL, schema,
                                  request_fn=response) == 'plain'
