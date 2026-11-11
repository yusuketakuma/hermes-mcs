"""release_auto_local のlocal facts面を検証。

実MCS・実config・実dataには触れない。時計/容量/schemaはtmp_path配下の
合成pathのみ。boot_idは実sysctl観測(読み取りのみ・副作用なし)。
"""
import sqlite3

import pytest

import release_auto_local as local


def _facts(tmp_path, reserved=4096):
    return local.LocalFacts(
        capacity_path=str(tmp_path),
        ledger_path=str(tmp_path / 'ledger.db'),
        reserved_owner_bytes=reserved)


def test_clocks_and_boot(tmp_path):
    facts = _facts(tmp_path)
    assert type(facts.now_ms()) is int and facts.now_ms() > 0
    assert type(facts.now_s()) is float
    assert type(facts.mono()) is float
    boot = facts.boot_id()
    assert type(boot) is str and len(boot) == 64
    int(boot, 16)  # hex形式


def test_reserved_owner_bytes(tmp_path):
    facts = _facts(tmp_path, reserved=1024)
    assert facts.reserved_owner_bytes() == 1024
    for bad in (0, -1, local.MAX_RESERVED_OWNER_BYTES + 1, 'x', True, None):
        with pytest.raises(ValueError):
            _facts(tmp_path, reserved=bad)


def test_capacity_ok(tmp_path):
    facts = _facts(tmp_path)
    assert facts.capacity_ok(1) is True
    assert facts.capacity_ok(10 ** 18) is False
    assert facts.capacity_ok(-1) is False
    assert facts.capacity_ok('x') is False
    assert facts.capacity_ok(True) is False
    missing = local.LocalFacts(
        capacity_path=str(tmp_path / 'absent'),
        ledger_path=str(tmp_path / 'ledger.db'),
        reserved_owner_bytes=1)
    assert missing.capacity_ok(1) is False


def _ledger(path, user_version):
    con = sqlite3.connect(str(path))
    try:
        con.execute(f'PRAGMA user_version={user_version}')
        con.commit()
    finally:
        con.close()


def test_installed_schema(tmp_path):
    ledger = tmp_path / 'ledger.db'
    _ledger(ledger, 3)
    facts = local.LocalFacts(
        capacity_path=str(tmp_path), ledger_path=str(ledger),
        reserved_owner_bytes=1)
    assert facts.installed_schema() == 3


def test_installed_schema_unobserved(tmp_path):
    facts = _facts(tmp_path)
    with pytest.raises(RuntimeError, match='SCHEMA_UNOBSERVED'):
        facts.installed_schema()  # ledger不存在
    ledger = tmp_path / 'ledger.db'
    _ledger(ledger, 0)
    facts = local.LocalFacts(
        capacity_path=str(tmp_path), ledger_path=str(ledger),
        reserved_owner_bytes=1)
    with pytest.raises(RuntimeError, match='SCHEMA_UNOBSERVED'):
        facts.installed_schema()  # user_version=0はPositive契約外
