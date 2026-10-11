"""Reject unrepresentable HTTP budgets before transport or admission work."""
import pytest

import bounded_http
import local_llm


@pytest.mark.parametrize('entry', ['http', 'chat'])
@pytest.mark.parametrize('field', ['timeout', 'deadline'])
@pytest.mark.parametrize('sign', [1, -1])
def test_unrepresentable_budget_is_a_validation_error(monkeypatch, entry, field, sign):
    monkeypatch.setattr(bounded_http.subprocess, 'Popen',
                        lambda *args, **kwargs: pytest.fail('invalid budget spawned worker'))
    kw = {field: sign * 10**1000}
    with pytest.raises(ValueError, match=field + '_invalid'):
        if entry == 'http':
            bounded_http.bounded_http_request(local_llm.ENDPOINT, 'GET', None,
                                             **({'timeout': 1} | kw))
        else:
            local_llm.chat('synthetic', request_fn=lambda *args: pytest.fail('invalid send'), **kw)


@pytest.mark.parametrize('url', ['http://localhost:abc/v1', 'http://127.0.0.1:99999/',
                                 'http://[::1]:-1/'])
def test_loopback_endpoint_rejects_malformed_port(url):
    assert bounded_http._loopback_endpoint_allowed(url) is False


def test_loopback_endpoint_keeps_valid_ports():
    for url in ('http://127.0.0.1:8080/v1', 'http://localhost/v1', 'http://[::1]:65535/'):
        assert bounded_http._loopback_endpoint_allowed(url) is True
