"""I1相互運用fixture生成: Python側がopensslで署名したv3 envelopeを出力する。

秘密鍵はfixtureへ含めない。canonical/message組立は本番と同じ
adapters.sdc.support_contract.canonicalとSDC-SIGNED-v1規則を使う。
実行: python3 tests/adapters/sdc/gen_auto_v3_signature_fixture.py OUT.json
"""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'mcs'))
import _mcs_path  # noqa: E402,F401  flat-import root登録
from adapters.sdc.support_contract import canonical  # noqa: E402
from mcs.ops.sdc_support_signatures import _release_public_key  # noqa: E402

EXAMPLES = Path(os.environ.get('V3_EXAMPLES', ''))
PURPOSES = {
    'start-grant.json': 'mcs-auto-grant-v3',
    'health-ack.json': 'mcs-auto-health-ack-v3',
}
EXPECTED = {
    'mcs-auto-grant-v3': lambda p: {'grant_id': p['grant_id'], 'start_request_id': p['start_request_id'],
                                    'operation_id': p['operation_id']},
    'mcs-auto-health-ack-v3': lambda p: {'request_id': p['request_id'], 'report_id': p['report_id'],
                                         'observation_sha256': p['observation_sha256']},
}


def b64u(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def openssl(*args, input_bytes=None):
    return subprocess.run(['/usr/bin/openssl', *args], input=input_bytes,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout


def main(out_path):
    with tempfile.TemporaryDirectory(prefix='mcs-v3-fixture-') as directory:
        work = Path(directory)
        priv, spki = work / 'key.pem', work / 'spki.der'
        openssl('genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072', '-out', str(priv))
        openssl('pkey', '-in', str(priv), '-pubout', '-outform', 'DER', '-out', str(spki))
        spki_raw = spki.read_bytes()
        kid = _release_public_key(b64u(spki_raw))['kid']
        vectors = []
        for name, purpose in PURPOSES.items():
            payload = json.loads((EXAMPLES / name).read_text())['data']
            payload_raw = canonical(payload, max_bytes=8192).encode('ascii')
            message = b'SDC-SIGNED-v1\n' + purpose.encode('ascii') + b'\n' + kid.encode('ascii') + b'\n' + payload_raw
            msg, sig = work / 'msg.bin', work / 'sig.bin'
            msg.write_bytes(message)
            openssl('dgst', '-sha256', '-sign', str(priv), '-sigopt', 'rsa_padding_mode:pss',
                    '-sigopt', 'rsa_pss_saltlen:32', '-sigopt', 'rsa_mgf1_md:sha256',
                    '-out', str(sig), str(msg))
            pin = {'kid': kid, 'purposes': [purpose], 'spki': b64u(spki_raw),
                   'not_before': 0, 'expires_at': 4102444800000}
            vectors.append({
                'name': f'{name[:-5]}-python-signed', 'purpose': purpose,
                'now_ms': payload['issued_at'] + 1000,
                'pins': [pin], 'expected': EXPECTED[purpose](payload),
                'envelope': {'alg': 'PS256', 'kid': kid, 'purpose': purpose,
                             'payload': b64u(payload_raw), 'signature': b64u(sig.read_bytes())},
            })
    Path(out_path).write_text(json.dumps(vectors))
    print('wrote', out_path, 'vectors', len(vectors), 'kid', kid)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('usage: gen_auto_v3_signature_fixture.py OUT.json')
    main(sys.argv[1])
