"""v3 module wiring census: name the wired and the waiting release-auto modules.

A ``v3`` module is a non-test ``.py`` file whose module docstring's first
line contains ``v3``. ``scripts/development/release_publish.py`` is the publisher entry
point itself (W11 CLI), not a module awaiting wiring, so it is pinned in
``V3_ENTRIES`` instead of the module census.

A module counts as **wired** when it is reachable from a pinned set of
product entry points through non-test imports. Reachability is deliberately
shallow evidence: ``release_auto_consent`` importing ``_det_uuid`` from the
owner marks the owner wired, it does not make ``owner.tick`` live. The tick
driver is live through ``release_auto_driver`` and ``update_cli auto-tick``;
``recover_tick`` is still W14 work, so the census additionally pins that no
product file outside the driver invokes them — test files and helper-function
imports are never evidence of a running tick.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKIP_DIRS = {'tests', 'integration', 'docs', 'resources', 'changes',
             'evaluation', 'deployment', '.git'}

# Product entry points whose imports count as wiring (device side only —
# publisher tooling under scripts/development/ is not a product entry).
PRODUCT_ENTRIES = {
    'adapters/sdc/protocol.py',
    'hermes_plugin/__init__.py',
    'lineworks_adapter/__main__.py',
    'mcs/ops/mcs_setup.py',
    'mcs/ops/update_cli.py',
    'mcs_standalone/__main__.py',
    'mcs_standalone/runtime.py',
}

# v3-marked entry points: counted separately, never as modules.
V3_ENTRIES = {'scripts/development/release_publish.py'}

WIRED = {
    'release_auto_apply', 'release_auto_backup', 'release_auto_consent',
    'release_auto_contract', 'release_auto_deps', 'release_auto_drain',
    'release_auto_driver', 'release_auto_evidence', 'release_auto_git',
    'release_auto_health', 'release_auto_holds', 'release_auto_journal',
    'release_auto_local', 'release_auto_locks', 'release_auto_offer',
    'release_auto_owner', 'release_auto_revoke', 'release_auto_services',
    'release_auto_stage', 'release_auto_state', 'release_auto_status',
    'release_auto_transport', 'release_auto_wire', 'release_auto_witness',
    'release_quarantine', 'release_runtime_proof', 'runtime_entry',
}

WAITING = {
    'release_auto_bridge', 'release_auto_migration', 'release_auto_recovery',
    'release_publish_api',
}

# Entry -> v3 modules it imports today; any edit of these references is
# the wiring signal this census exists to name.
ENTRY_IMPORTS = {
    'adapters/sdc/protocol.py': {'release_auto_transport'},
    'hermes_plugin/__init__.py': {'runtime_entry'},
    'lineworks_adapter/__main__.py': {'runtime_entry'},
    'mcs/ops/mcs_setup.py': {'release_auto_status', 'runtime_entry'},
    'mcs/ops/update_cli.py': {'release_auto_consent', 'release_auto_contract',
                              'release_auto_driver', 'release_auto_state',
                              'release_auto_status', 'runtime_entry'},
    'mcs_standalone/__main__.py': {'runtime_entry'},
    'mcs_standalone/runtime.py': {'runtime_entry'},
}


def _files():
    return [p for p in sorted(ROOT.rglob('*.py'))
            if not (set(p.relative_to(ROOT).parts) & SKIP_DIRS)
            and not p.name.startswith('test_')]


def _parse(path):
    try:
        return ast.parse(path.read_text(encoding='utf-8'))
    except (SyntaxError, UnicodeDecodeError):
        return None


def _docstring_first_line(tree):
    doc = ast.get_docstring(tree)
    return doc.split('\n')[0] if doc else ''


def _imports(tree):
    deps = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            deps.update(alias.name.split('.')[-1] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                deps.add(node.module.split('.')[-1])
            deps.update(alias.name for alias in node.names)
    return deps


def _census():
    trees = {p: _parse(p) for p in _files()}
    v3_marked = {p.stem: p for p, t in trees.items()
                 if t is not None and 'v3' in _docstring_first_line(t)}
    imports = {p: _imports(t) for p, t in trees.items() if t is not None}
    return v3_marked, imports


def test_census_names_every_v3_file_exactly_once():
    v3_marked, _ = _census()
    modules = set(WIRED) | set(WAITING)
    entries = {Path(e).stem for e in V3_ENTRIES}
    by_stem = {stem: str(path.relative_to(ROOT)) for stem, path in v3_marked.items()}
    assert set(v3_marked) == modules | entries, (
        f"v3 census drift — new: {sorted(set(v3_marked) - modules - entries)}, "
        f"gone: {sorted(modules - set(v3_marked))} (paths {by_stem})")
    assert not (set(WIRED) & set(WAITING))


def test_wired_modules_reachability_names_both_sides():
    v3_marked, imports = _census()
    modules = set(v3_marked) - {Path(e).stem for e in V3_ENTRIES}
    stem_to_path = {p.stem: p for p in imports}
    seeds = set()
    for rel in PRODUCT_ENTRIES:
        path = ROOT / rel
        assert path in imports, f"product entry missing: {rel}"
        seeds |= imports[path] & modules
    wired = set(seeds)
    changed = True
    while changed:
        changed = False
        for stem in list(wired):
            for dep in imports.get(stem_to_path.get(stem), ()):
                if dep in modules and dep not in wired:
                    wired.add(dep)
                    changed = True
    waiting = modules - wired
    assert wired == WIRED, (f"unexpectedly wired: {sorted(wired - WIRED)}, "
                            f"dropped out: {sorted(WIRED - wired)}")
    assert waiting == WAITING, (f"newly wired: {sorted(WAITING - waiting)}, "
                               f"newly waiting: {sorted(waiting - WAITING)}")


def test_entry_reference_changes_are_named():
    v3_marked, imports = _census()
    modules = set(v3_marked)
    for rel, expected in ENTRY_IMPORTS.items():
        path = ROOT / rel
        assert path in imports, f"product entry missing: {rel}"
        actual = imports[path] & modules
        assert actual == expected, (
            f"{rel}: v3 imports changed {sorted(expected ^ actual)} "
            f"(added {sorted(actual - expected)}, removed {sorted(expected - actual)})")


def test_tick_invocation_stays_at_single_driver_boundary():
    """owner.tick is live only through release_auto_driver; recover_tick stays waiting."""
    driver = ROOT / 'mcs/ops/release_auto_driver.py'
    trees = {p: _parse(p) for p in _files()}
    owner_offenders = []
    recovery_offenders = []
    for path, tree in trees.items():
        if tree is None:
            continue
        owner_names = set()
        recovery_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == 'release_auto_owner':
                    owner_names |= {a.asname or a.name for a in node.names
                                    if a.name == 'tick'}
                if node.module == 'release_auto_recovery':
                    recovery_names |= {a.asname or a.name for a in node.names
                                       if a.name == 'recover_tick'}
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == 'release_auto_owner':
                        owner_names.add((a.asname or 'release_auto_owner') + '.tick')
                    if a.name == 'release_auto_recovery':
                        recovery_names.add((a.asname or 'release_auto_recovery') + '.recover_tick')
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = None
            if isinstance(func, ast.Name):
                called = func.id
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                called = f'{func.value.id}.{func.attr}'
            if called in owner_names and path != driver:
                owner_offenders.append(path)
            if called in recovery_names:
                recovery_offenders.append(path)
    assert not owner_offenders, \
        f"owner.tick invoked outside driver: {[str(p) for p in owner_offenders]}"
    assert not recovery_offenders, \
        f"recover_tick invoked: {[str(p) for p in recovery_offenders]}"
