"""_v3_consent_wire factoryの合成検証。"""
import json

import pytest

import update_cli
import release_auto_trust as trust
from test_sdc_provision import V, NOW, world  # noqa: F401
import test_sdc_provision as tp
from test_sdc_provision_profiles import install

AUTO8 = ['mcs-auto-offer-v3', 'mcs-auto-compatibility-v3', 'mcs-auto-grant-v3',
         'mcs-auto-ack-v3', 'mcs-auto-status-v3', 'mcs-auto-health-ack-v3',
         'mcs-auto-consent-preview-v3', 'mcs-auto-consent-receipt-v3']
AUTO_BUNDLE = next(v['value'] for v in V['positive']
                   if v['definition'] == 'Bundle' and v['value']['profile_id'] == 'auto')
PIN = dict(AUTO_BUNDLE['pins'][0])
DEVICE = '12345678-1234-4234-8234-123456789abc'
KEY = 'c' * 64
ENDPOINT = {'origin': 'https://synthetic.example.com', 'audience': 'sdc:test', 'environment': 'test'}


def _auto_provisioned(store):
    """monitor登録済みのstoreへauto bundleをimportしinitializeする。"""
    install(store, 'auto', ready=False)


@pytest.fixture
def provisioned(world):  # noqa: F811
    from test_sdc_provision import active
    active(world)
    _auto_provisioned(world)
    return world


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
    import release_auto_wire_factory
    monkeypatch.setattr(update_cli.mcs_util, 'load_config',
                        lambda: {'sdc': {'state_root': str(sdc_root)}})
    monkeypatch.setattr(release_auto_wire_factory, 'control_root_path',
                        lambda home: str(control))


def test_factory_builds_wire_when_enrolled_and_pinned(tmp_path, monkeypatch, provisioned):
    control = tmp_path / 'control'
    _pins(control)
    _patch(monkeypatch, provisioned.root, control)
    # provision vectorの時間窓(NOW基準)内で評価する
    import time as time_mod
    monkeypatch.setattr(time_mod, 'time', lambda: NOW / 1000)
    wire = update_cli._v3_consent_wire()
    assert wire is not None
    assert callable(wire.signed_send) and callable(wire.verify)
    # networkには出さず、credential/pin解決だけを確認する。
    # endpointはauto bundleのorigin/audienceで監視endpoint.jsonではない
    config, state, key = wire._credential()
    assert config['audience'] == 'sdc:auto'  # install時のmaterial()がvector値をoverride
    assert state['device_id'] == tp.DEVICE and key == 'a' * 64
    assert wire._pins() == [PIN]


def test_factory_fail_closed_when_bundle_pins_differ(tmp_path, monkeypatch, provisioned):
    control = tmp_path / 'control'
    control.mkdir(parents=True)
    other = dict(PIN)
    other['kid'] = 'e' * 64
    (control / 'auto-v3-pins.json').write_text(
        json.dumps({'schema_version': trust.SCHEMA_VERSION, 'pins': [other]},
                   separators=(',', ':'), sort_keys=True))
    _patch(monkeypatch, provisioned.root, control)
    import time as time_mod
    monkeypatch.setattr(time_mod, 'time', lambda: NOW / 1000)
    assert update_cli._v3_consent_wire() is None


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


def test_factory_fail_closed_with_malformed_pins(tmp_path, monkeypatch):
    sdc_root, control = tmp_path / 'sdc', tmp_path / 'control'
    _enrolled(sdc_root)
    control.mkdir(parents=True)
    bad = dict(PIN)
    bad['purposes'] = None
    (control / 'auto-v3-pins.json').write_text(
        json.dumps({'schema_version': trust.SCHEMA_VERSION, 'pins': [bad]},
                   separators=(',', ':'), sort_keys=True))
    _patch(monkeypatch, sdc_root, control)
    assert update_cli._v3_consent_wire() is None
