"""v3 owner depsのlocal facts面: 時計・容量・schema・boot識別子。

network・資格情報・外部serviceに触れない純粋ローカル観測のみ。
観測不能な値は例外でfail-closed。capacity_okはbool契約のため
観測失敗をFalse(=容量不足扱い)で返す。
"""
import os
import time

import release_quarantine_native
import restore_evidence

MAX_RESERVED_OWNER_BYTES = 8388608  # spec 03: reserved_owner_bytesの範囲上限


class LocalFacts:
    """deps契約の時計/容量/schema/boot面。

    capacity_path/ledger_path/reserved_owner_bytesはfactoryが配備構成から
    注入する — このmoduleは既定値を推測しない。
    """

    def __init__(self, *, capacity_path, ledger_path, reserved_owner_bytes):
        if (type(reserved_owner_bytes) is not int
                or not 1 <= reserved_owner_bytes <= MAX_RESERVED_OWNER_BYTES):
            raise ValueError('RESERVED_OWNER_BYTES')
        self._capacity_path = os.fspath(capacity_path)
        self._ledger_path = os.fspath(ledger_path)
        self._reserved = reserved_owner_bytes

    def now_ms(self):
        return release_quarantine_native.native_now_ms()

    def now_s(self):
        return time.time()

    def mono(self):
        return time.monotonic()

    def boot_id(self):
        return release_quarantine_native.native_boot_id()

    def reserved_owner_bytes(self):
        return self._reserved

    def capacity_ok(self, required_bytes):
        if type(required_bytes) is not int or required_bytes < 0:
            return False
        try:
            st = os.statvfs(self._capacity_path)
        except OSError:
            return False
        return st.f_bavail * st.f_frsize >= required_bytes

    def installed_schema(self):
        value = restore_evidence.database_version(self._ledger_path)
        if type(value) is not int or value < 1:
            raise RuntimeError('SCHEMA_UNOBSERVED')
        return value
