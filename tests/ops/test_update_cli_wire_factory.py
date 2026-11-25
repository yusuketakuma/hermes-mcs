"""_v3_consent_wire factoryの合成検証。"""
import json

import update_cli
import release_auto_trust as trust

PIN = {'kid': 'b' * 64, 'purposes': ['mcs-auto-offer-v3'],
       'spki': 'a2V5', 'not_before': 0, 'expires_at': 2**53 - 1}
DEVICE = '12345678-1234-4234-8234-123456789abc'
KEY = 'c' * 64
ENDPOINT = {'origin': 'https://synthetic.example.com', 'audience': 'sdc:test', 'environment': 'test'}


def _enrolled(root):
    from adapters.sdc.storage import Store
    with Store(root, create=True) as store:
        store.write('endpoint.json', dict(ENDPOINT))
        store.write('state.json', {'state': 'active', 'device_id': DEVICE, 'key_id': DEVICE,
                                   'claim_id': DEVICE, 'sequence': 1, 'enabled': True})
        store.write('credential', {'key': KEY, 'key_id': DEVICE})


def _pins(control):
    control.mkdir(parents=True, exist_ok=True)
    (control / 'auto-v3-pins.json').write_text(
        json.dumps({'schema_version': trust.SCHEMA_VERSION, 'pins': [PIN]},
                   separators=(',', ':'), sort_keys=True))


def _patch(monkeypatch, sdc_root, control):
    monkeypatch.setattr(update_cli.mcs_util, 'load_config',
                        lambda: {'sdc': {'state_root': str(sdc_root)}})
    monkeypatch.setattr(update_cli, '_v3_state_root', lambda: str(control))


def test_factory_builds_wire_when_enrolled_and_pinned(tmp_path, monkeypatch):
    sdc_root, control = tmp_path / 'sdc', tmp_path / 'control'
    _enrolled(sdc_root)
    _pins(control)
    _patch(monkeypatch, sdc_root, control)
    wire = update_cli._v3_consent_wire()
    assert wire is not None
    assert callable(wire.signed_send) and callable(wire.verify)
    # networkには出さず、credential/pin解決だけを確認する
    config, state, key = wire._credential()
    assert config['origin'] == ENDPOINT['origin']
    assert state['device_id'] == DEVICE and key == KEY
    assert wire._pins() == [PIN]


def test_factory_fail_closed_without_pins(tmp_path, monkeypatch):
    sdc_root, control = tmp_path / 'sdc', tmp_path / 'control'
    _enrolled(sdc_root)
    control.mkdir()
    _patch(monkeypatch, sdc_root, control)
    assert update_cli._v3_consent_wire() is None


def test_factory_fail_closed_without_enrollment(tmp_path, monkeypatch):
    sdc_root, control = tmp_path / 'sdc', tmp_path / 'control'
    sdc_root.mkdir()
    _pins(control)
    _patch(monkeypatch, sdc_root, control)
    assert update_cli._v3_consent_wire() is None


def test_factory_fail_closed_when_not_active(tmp_path, monkeypatch):
    sdc_root, control = tmp_path / 'sdc', tmp_path / 'control'
    _enrolled(sdc_root)
    from adapters.sdc.storage import Store
    with Store(sdc_root) as store:
        store.write('state.json', {'state': 'revoked', 'device_id': DEVICE, 'key_id': DEVICE})
    _pins(control)
    _patch(monkeypatch, sdc_root, control)
    assert update_cli._v3_consent_wire() is None
