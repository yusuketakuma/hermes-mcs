"""v3 wire factory: enrollment credentialとpin fileからconsent/owner wireを組み立てる。

署名credentialは既存SDC connector store(state_root)から使用毎に解決し、
v3 pinはrelease control rootのauto-v3-pins.jsonだけから読む。
送信先endpointはauto provision bundleのorigin/audienceを使い、監視用
endpoint.jsonは使わない(v3 device APIはSUPPORT_HOST)。bundle pinsは
pin fileとの相互照合専用で不一致はfail-closed。enrollment未完了・
auto provision未適用・pin未配置・credential不整合ではNone(fail-closed)を返す。
release_metadataのattestation trustは未配線のまま(consent/owner経路では不使用)。
"""

import os
import time


def control_root_path(runtime_home):
    """v3 release control root。dir未作成=未移行の判定は呼出側が行う。"""
    try:
        from runtime_entry import control_root
        return str(control_root(runtime_home))
    except Exception:
        return None


def build_wire(runtime_home):
    """enrollment済みcredentialとpin配置が揃った端末だけにwireを返す。"""
    try:
        import mcs_util
        cfg = mcs_util.load_config()
        sdc = cfg.get('sdc') if isinstance(cfg, dict) else None
        state_root = sdc.get('state_root') if isinstance(sdc, dict) else None
        control = control_root_path(runtime_home)
        if type(state_root) is not str or not state_root or control is None \
                or not os.path.isdir(state_root):
            return None
        import release_auto_trust
        pins_file = release_auto_trust.pins_path(control)
        if not os.path.isfile(pins_file):
            return None
        from adapters.sdc.connector import Connector
        from adapters.sdc.release_auto_transport import ReleaseAutoTransport
        from adapters.sdc.storage import Store
        from release_auto_wire import Wire

        def credential():
            with Store(state_root) as store:
                config = store.read('endpoint.json')
                state = store.read('state.json')
                cred = store.read('credential')
                if type(state) is not dict or state.get('state') != 'active':
                    raise ValueError('SDC_CREDENTIAL_REQUIRED')
                key = Connector(store, None).secret(state, cred)
                # 署名audienceはauto bundleの値 — 監視endpoint.jsonのaudienceで
                # 署名するとautoサーバが拒否する。endpoint_cfgは構築時点で
                # 検証済みなのでclosureの後置bindでよい。
                return dict(config, **endpoint_cfg), state, key

        def pins():
            return release_auto_trust.load_v3_pins(pins_file)

        # pin fileの形状/期限不備をwire構築時にfail-closedへ倒す。
        # 後回しにするとverify時点でcentral側だけ状態が進む分裂が起きる。
        file_pins = pins()

        # v3 device APIはSUPPORT_HOSTにある — 監視enrollmentのendpoint.jsonは
        # 使わず、auto provision bundleのorigin/audienceを送信先とする。
        # bundleのpinsは正本pin fileとの相互照合専用で、不一致はfail-closed。
        now = int(time.time() * 1000)
        with Store(state_root) as store:
            from adapters.sdc import provision
            snap = provision.profile_snapshot(store, profile='auto', now_ms=now)
        bundle = snap['bundle']
        endpoint_cfg = {k: bundle[k] for k in ('origin', 'audience', 'environment')}

        def canon_pin(pin):
            from adapters.sdc.support_contract import canonical
            return canonical(pin, max_bytes=4096)

        bundle_pins = sorted(canon_pin(p) for p in bundle['pins'])
        file_pins_c = sorted(canon_pin(p) for p in file_pins)
        if bundle_pins != file_pins_c:
            return None

        config, _state, _key = credential()
        transport = ReleaseAutoTransport(dict(endpoint_cfg))
        return Wire(transport=transport, credential=credential, pins=pins,
                    now_ms=lambda: int(time.time() * 1000))
    except Exception:
        return None
