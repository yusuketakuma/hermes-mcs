"""v3 pin配布fileのfail-closed loader。

spec ENC04のv3 pinは「HTTP応答からの自己登録」を禁止する信任境界の根拠。
本moduleは管理者または署名済みrelease closureが配置した
`auto-v3-pins.json`を読み、閉じたschemaで検査して返すだけを担う。
file不在・破損・schema違反は全てfail-closed(例外)。鍵の用途検査と
有効期間の評価は`validate_auto_v3_pins`/verify側の責務で、ここでは
形状と閉じた集合だけを確認する。

配布file形式(本moduleが規定するv1):
  {"schema_version":"mcs.auto-v3-pins.v1",
   "pins":[{"kid":sha256hex,"purposes":[v3 purpose...],
            "spki":base64url,"not_before":ms,"expires_at":ms}, ...]}
pins以外のkey、pins内の未知key、16件超のpinは拒否する。
"""
import os
import re

from adapters.sdc.support_contract import canonical, parse_canonical

SCHEMA_VERSION = 'mcs.auto-v3-pins.v1'
MAX_BYTES = 16384
_MAX_PINS = 16
_KID = re.compile(r'[0-9a-f]{64}')
_PIN_KEYS = {'kid', 'purposes', 'spki', 'not_before', 'expires_at'}


class TrustError(RuntimeError):
    """pin配布fileの不備。codeは固定token。"""

    def __init__(self, code='TRUST_INVALID'):
        super().__init__(code)
        self.code = code


def pins_path(control_root):
    """配布fileの既定path。control root外へsymlinkで逃げる配置はload側で拒否する。"""
    return os.path.join(os.fspath(control_root), 'auto-v3-pins.json')


def load_v3_pins(path):
    """配布fileを読みpin listを返す。検査済みlist以外は返さない。

    - symlink・非regular file・16KiB超・canonical JSONでない入力は拒否
    - pinsの形状(鍵集合・kid一意・件数)だけを検査し、用途・期限の評価は
      検証側(validate_auto_v3_pins)へ残す
    """
    path = os.fspath(path)
    import stat as stat_mod
    try:
        parent = os.lstat(os.path.dirname(path) or os.sep)
        if stat_mod.S_ISLNK(parent.st_mode):
            raise TrustError('TRUST_INVALID')
        stat = os.stat(path, follow_symlinks=False)
    except TrustError:
        raise
    except OSError as e:
        raise TrustError('TRUST_UNAVAILABLE') from e
    if not stat_mod.S_ISREG(stat.st_mode) or stat.st_size > MAX_BYTES:
        raise TrustError('TRUST_INVALID')
    try:
        # O_NOFOLLOWで開きfile自身のsymlink置換を拒否する。
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except (OSError, AttributeError) as e:
        raise TrustError('TRUST_INVALID') from e
    try:
        with os.fdopen(fd, 'rb') as fh:
            raw = fh.read(MAX_BYTES + 1)
    except OSError as e:
        raise TrustError('TRUST_UNAVAILABLE') from e
    if not raw or len(raw) > MAX_BYTES:
        raise TrustError('TRUST_INVALID')
    try:
        value = parse_canonical(raw.decode('ascii'), max_bytes=MAX_BYTES)
    except (ValueError, TypeError, UnicodeError, RecursionError) as e:
        raise TrustError('TRUST_INVALID') from e
    if (type(value) is not dict or set(value) != {'schema_version', 'pins'}
            or value['schema_version'] != SCHEMA_VERSION
            or type(value['pins']) is not list
            or not 1 <= len(value['pins']) <= _MAX_PINS):
        raise TrustError('TRUST_INVALID')
    kids = set()
    for pin in value['pins']:
        if (type(pin) is not dict or set(pin) != _PIN_KEYS
                or type(pin['kid']) is not str or not _KID.fullmatch(pin['kid'])
                or pin['kid'] in kids):
            raise TrustError('TRUST_INVALID')
        kids.add(pin['kid'])
        uses = pin['purposes']
        if (type(uses) is not list or not uses
                or any(type(use) is not str or not use.startswith('mcs-') for use in uses)
                or len(set(uses)) != len(uses)
                or type(pin['spki']) is not str or not pin['spki']
                or type(pin['not_before']) is not int or type(pin['not_before']) is bool
                or type(pin['expires_at']) is not int or type(pin['expires_at']) is bool
                or not 0 <= pin['not_before'] < pin['expires_at'] <= 2**53 - 1):
            raise TrustError('TRUST_INVALID')
    # canonical再符号化で浮動小数点・非canonical表現の混入を排除する
    try:
        canonical(value['pins'], max_bytes=MAX_BYTES)
    except (ValueError, TypeError, RecursionError) as e:
        raise TrustError('TRUST_INVALID') from e
    return value['pins']
