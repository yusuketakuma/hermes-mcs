"""Verify local routing and transport with complete synthetic doubles."""
import io
from pathlib import Path
import ssl
from types import SimpleNamespace

import pytest

from adapters.sdc.protocol import endpoint
from adapters.sdc.storage import Store
from adapters.sdc.transport import Transport, TransportError
import mcs_cli
import sdc_cli
import service_contract

CONFIG = {'origin': 'https://ingest.invalid', 'audience': 'sdc:synthetic', 'environment': 'synthetic'}


class Response(io.BytesIO):
    def __init__(self, raw, code=200, headers=None):
        super().__init__(raw)
        self.code, self.headers = code, headers or {}


def test_transport_fixes_origin_tls_redirect_proxy_raw_bytes_and_timeout():
    transport = Transport(CONFIG)
    proxies = [handler for handler in transport.opener.handlers if hasattr(handler, 'proxies')]
    assert proxies == [] or all(handler.proxies == {} for handler in proxies)
    assert any(type(handler).__name__ == 'NoRedirect' for handler in transport.opener.handlers)
    assert all(handler._context.verify_mode == ssl.CERT_REQUIRED for handler in transport.opener.handlers if hasattr(handler, '_context'))
    calls = []
    def open_request(request, timeout):
        calls.append((request.full_url, request.data, timeout, request.get_header('User-agent')))
        return Response(b'{"code":"accepted"}')
    transport.opener = SimpleNamespace(open=open_request)
    raw = b'{"sequence": 1}'
    assert transport.send('/v2/heartbeats', raw)['code'] == 'accepted'
    # The edge bans the default Python-urllib signature (Cloudflare error 1010).
    assert calls == [('https://ingest.invalid/v2/heartbeats', raw, 10, 'sdc-connector/0.1.0')]
    with pytest.raises(ValueError):
        transport.send('/v2/heartbeats', raw, {'Host': 'evil.invalid'})
    with pytest.raises(ValueError):
        transport.send('/v2/commands', raw)


@pytest.mark.parametrize('origin', ['http://ingest.invalid', 'https://ingest.invalid:443', 'https://ingest.invalid/path', 'https://ingest.invalid?token=secret', 'https://user@ingest.invalid', 'https://ingest..invalid'])
def test_unapproved_endpoint_shapes_are_rejected(origin):
    with pytest.raises(ValueError):
        endpoint({**CONFIG, 'origin': origin})


def test_transport_bounds_and_redacts_response_errors():
    transport = Transport(CONFIG)
    for raw in (b'x' * 16385, b'{"a":1,"a":2}', b'not json'):
        transport.opener = SimpleNamespace(open=lambda *a, _raw=raw, **kw: Response(_raw))
        with pytest.raises(TransportError) as caught:
            transport.send('/v2/heartbeats', b'{}')
        assert str(caught.value) == 'SDC_NETWORK_UNAVAILABLE'
    transport.opener = SimpleNamespace(open=lambda *a, **kw: Response(b'{"code":"RETRY_LATER","secret":"SYNTHETIC_PRIVATE"}', 429, {'Retry-After': '600'}))
    with pytest.raises(TransportError) as caught:
        transport.send('/v2/heartbeats', b'{}')
    assert caught.value.retry_after == 600
    assert 'SYNTHETIC_PRIVATE' not in str(caught.value)


@pytest.mark.parametrize('command', ['connect', 'connection', 'disconnect', 'rotate-key'])
def test_existing_cli_routes_sdc_only_after_runtime_pinning(monkeypatch, command):
    monkeypatch.setenv('MCS_LIFECYCLE_PINNED', '1')
    calls = []
    monkeypatch.setattr(sdc_cli, 'main', lambda argv: calls.append(argv) or 0)
    assert mcs_cli.main([command, '--state-root', '/synthetic/private']) == 0
    assert calls == [[command, '--state-root', '/synthetic/private']]


def test_service_contract_is_opt_in_in_both_modes_and_uses_selected_python():
    for mode in ('standalone', 'hermes'):
        cfg = {'runtime_mode': mode}
        original = service_contract.desired_agent_labels(cfg)
        assert service_contract.SDC_LABEL not in original
        cfg['sdc'] = {'enabled': True, 'state_root': '/synthetic/private'}
        assert service_contract.desired_agent_labels(cfg) == original + [service_contract.SDC_LABEL]
        values = service_contract.service_substitutions(cfg, root='/synthetic/root', checkout='/synthetic/repo', standalone_runtime_home='/synthetic/runtime', hermes_runtime_home='/synthetic/hermes', hermes_python='/synthetic/pinned/python')
        assert values['SDC_ROOT'] == '/synthetic/private'
        assert values['PYTHON'] == ('/synthetic/runtime/venv/bin/python3' if mode == 'standalone' else '/synthetic/pinned/python')
        body = (Path(__file__).resolve().parents[3] / 'deployment/launchagents/local.mcs-sdc.plist').read_text()
        assert '<integer>300</integer>' in body
        assert '<key>RunAtLoad</key><false/>' in body
        assert 'sdc_tick.py' in body


def test_cli_failed_claim_does_not_print_token_or_key(tmp_path, monkeypatch, capsys):
    root = (tmp_path / 'private').resolve()
    with Store(root, create=True) as store:
        store.write('endpoint.json', CONFIG)
    monkeypatch.setattr(sdc_cli.getpass, 'getpass', lambda prompt: 'SYNTHETIC_PRIVATE_TOKEN')
    class Offline:
        def __init__(self, _):
            pass
        def send(self, *args, **kwargs):
            raise TransportError(503, 'SYNTHETIC_PRIVATE_TOKEN')
    monkeypatch.setattr(sdc_cli, 'Transport', Offline)
    assert sdc_cli.main(['connect', '--state-root', str(root)]) == 1
    printed = capsys.readouterr()
    assert 'SYNTHETIC_PRIVATE_TOKEN' not in printed.out + printed.err
    with Store(root) as store:
        key = store.read('credential')['key']
    assert key not in printed.out + printed.err


def test_disconnect_stops_owned_job_before_any_network(tmp_path, monkeypatch, capsys):
    root = (tmp_path / 'private').resolve()
    with Store(root, create=True) as store:
        store.write('endpoint.json', CONFIG)
        store.write('credential', {'key': 'a1' * 32, 'key_id': '22222222-2222-4222-8222-222222222222'})
        store.write('state.json', {'state': 'active', 'enabled': True, 'device_id': '11111111-1111-4111-8111-111111111111', 'key_id': '22222222-2222-4222-8222-222222222222'})
    calls = []
    monkeypatch.setattr(sdc_cli, 'service', lambda enabled, root: calls.append(('service', enabled)))
    monkeypatch.setattr('builtins.input', lambda prompt: '2')
    class Offline:
        def __init__(self, _):
            pass
        def send(self, *args):
            calls.append(('network', None))
            raise TransportError()
    monkeypatch.setattr(sdc_cli, 'Transport', Offline)
    assert sdc_cli.main(['disconnect', '--state-root', str(root)]) == 0
    assert calls == [('service', False), ('network', None)]
    with Store(root) as store:
        state = store.read('state.json')
    assert state['enabled'] is False
    assert state['central_suspended'] is False
    assert '未確認' in capsys.readouterr().out


def test_facts_project_only_fixed_version_and_process_sources(monkeypatch):
    calls = []
    monkeypatch.setattr(sdc_cli.mcs_util, 'load_config', lambda: {'runtime_mode': 'standalone', 'patient_body': 'SYNTHETIC_PRIVATE'})
    monkeypatch.setattr(sdc_cli.platform, 'mac_ver', lambda: ('26.0', (), ''))
    def run(argv, timeout):
        calls.append((argv, timeout))
        if 'rev-parse' in argv:
            return SimpleNamespace(returncode=0, stdout='a' * 40 + '\n')
        if 'status' in argv:
            return SimpleNamespace(returncode=0, stdout='')
        return SimpleNamespace(returncode=0, stdout='state = running\n')
    monkeypatch.setattr(sdc_cli.lifecycle_io, 'run_bounded', run)
    result = sdc_cli.facts()
    assert result['mode'] == 'standalone'
    assert result['loaded_commit'] is None
    assert result['mcs_commit'] == 'a' * 40
    assert all(timeout == 2 for _, timeout in calls)
    assert 'SYNTHETIC_PRIVATE' not in str(result)
    assert all('doctor' not in argv for argv, _ in calls)


def test_redirect_is_a_permanent_denial_without_following_location():
    transport = Transport(CONFIG)
    calls = []
    def redirect(*args, **kwargs):
        calls.append(1)
        return Response(b'not json', 302, {'Location': 'https://evil.invalid'})
    transport.opener = SimpleNamespace(open=redirect)
    with pytest.raises(TransportError) as caught:
        transport.send('/v2/heartbeats', b'{}')
    assert caught.value.status == 403
    assert caught.value.code == 'SDC_REDIRECT_DENIED'
    assert len(calls) == 1


def test_disconnect_busy_lock_explains_required_retry(tmp_path, monkeypatch, capsys):
    root = (tmp_path / 'private').resolve()
    calls = []
    monkeypatch.setattr(sdc_cli, 'service', lambda enabled, root: calls.append(enabled))
    with Store(root, create=True) as store, store.lock():
        assert sdc_cli.main(['disconnect', '--state-root', str(root)]) == 1
    assert calls == [False]
    assert '再実行' in capsys.readouterr().err
