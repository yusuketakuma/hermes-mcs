"""launcher coverage: 全業務入口がruntime_entry bootstrapを通り、迂回経路がないことを固定する。"""
import re
from pathlib import Path

import runtime_entry

ROOT = Path(__file__).resolve().parents[2]

# 常駐業務入口。各々がapp importより前にQ SHを取得する(m3-autoupdate-design §3.3)。
_MANAGED = {
    'mcs_standalone/__main__.py': '_runtime_entry.acquire()',
    'lineworks_adapter/__main__.py': '_runtime_entry.acquire()',
    'adapters/lineworks/__main__.py': '_runtime_entry.acquire()',
    'hermes_plugin/__init__.py': 'runtime_entry.acquire()',
}

# mcs/ops/*.py の __main__ は操作者CLI(都度起動・非永続)であり業務入口ではない。
# ここに無い __main__.py が増えたら、業務入口かどうかを判定してこの表を更新する。
_NON_BUSINESS_MAIN = frozenset()


def _main_files():
    found = set()
    for path in ROOT.rglob('__main__.py'):
        rel = path.relative_to(ROOT).as_posix()
        if '.git' in rel.split('/') or '__pycache__' in rel.split('/'):
            continue
        found.add(rel)
    return found


def test_every_main_module_is_classified():
    """新しい __main__.py は managed か非業務のどちらかに明示分類される。"""
    unclassified = _main_files() - set(_MANAGED) - set(_NON_BUSINESS_MAIN)
    assert not unclassified, f'unclassified __main__.py: {sorted(unclassified)}'


def test_managed_entries_present():
    # hermes_plugin は plugin 形式で __init__.py が入口(__main__.py ではない)
    missing = {rel for rel in _MANAGED if not (ROOT / rel).is_file()}
    assert not missing, f'managed entrypoint missing: {sorted(missing)}'


def test_managed_entries_acquire_q():
    for rel, marker in _MANAGED.items():
        text = (ROOT / rel).read_text()
        assert marker in text, f'{rel}: {marker} not found'


def test_no_role_flag_bypass():
    """--role/--as等の引数でQ取得を迂回する分岐が入口に存在しない。"""
    for rel in _MANAGED:
        text = (ROOT / rel).read_text()
        for pattern in (r'--role', r'add_argument\(.role', r'argv.*role'):
            assert not re.search(pattern, text), f'{rel}: role bypass pattern {pattern}'


def test_child_spawn_surface_contract():
    """M5/M6 launcherが使う公開面: child_env/pass_fds/pycache_prefix/adopted。"""
    legacy = runtime_entry.RuntimeEntry()
    assert legacy.child_env() == {}
    assert legacy.pass_fds() == ()
    assert legacy.pycache_prefix is None or isinstance(legacy.pycache_prefix, str)
    assert legacy.managed is False and legacy.adopted is False


def test_standalone_workers_inherit_q():
    """mcs_standalone の子process起動が runtime_entry.current() 経由でQを継承する。"""
    text = (ROOT / 'mcs_standalone/runtime.py').read_text()
    assert 'runtime_entry.current()' in text
    assert 'child_env()' in text and 'pass_fds()' in text
