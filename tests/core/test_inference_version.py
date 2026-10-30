"""Public inference-version truth table, including malformed stored metadata."""
import json
import sqlite3

import pytest

from inference_version import (
    CURRENT_VERSION, LEGACY_VERSION, LLM_EXTRACT_VERSION, VERSION_KEY,
    current_meta_pred, is_current, read_version,
)


# Expected values are independent contract examples, not computed by the reader.
CASES = [
    (None, None),
    ('', None),
    ('{broken', None),
    ('null', None),
    ('[]', None),
    ('"1.0"', None),
    ('1', None),
    ('true', None),
    ('{}', '0.1'),
    ('{"engine_version":4}', '0.1'),
    ('{"inference_engine_version":"0.1"}', '0.1'),
    ('{"inference_engine_version":"1.0","engine_version":4}', '1.0'),
    ('{"inference_engine_version":"1.1"}', '1.1'),
    ('{"inference_engine_version":"1.2"}', '1.2'),
    ('{"inference_engine_version":"2.0"}', '2.0'),
    ('{"inference_engine_version":null}', None),
    ('{"inference_engine_version":1.0}', None),
    ('{"inference_engine_version":true}', None),
    ('{"inference_engine_version":false}', None),
    ('{"inference_engine_version":{}}', None),
    ('{"inference_engine_version":[]}', None),
    ('{"inference_engine_version":"01.0"}', None),
    ('{"inference_engine_version":"1.00"}', None),
    ('{"inference_engine_version":"1"}', None),
    ('{"inference_engine_version":"v1.0"}', None),
    ('{"inference_engine_version":"1.0.0"}', None),
    ('{"inference_engine_version":"1.0\\n"}', None),
    ('{"inference_engine_version":"１.０"}', None),
    (json.dumps({VERSION_KEY: "1.0' OR 1=1 --"}), None),
    (json.dumps({VERSION_KEY: '9' * 10000 + '.0'}), None),
]


def _decode(raw):
    try:
        return json.loads(raw) if raw is not None else None
    except json.JSONDecodeError:
        return None


@pytest.mark.parametrize('raw,expected', CASES)
def test_version_truth_table_matches_python_and_sql(raw, expected):
    meta = _decode(raw)
    assert read_version(meta) == expected
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE artifacts(meta TEXT COLLATE NOCASE)')
        db.execute('INSERT INTO artifacts VALUES (?)', (raw,))
        for target in ('0.1', '1.0', '1.1', '1.2', '2.0', '0.0', '10.20'):
            wanted = expected == target
            assert is_current(meta, target=target) is wanted
            actual = db.execute(
                f'SELECT {current_meta_pred(target=target)} FROM artifacts a').fetchone()[0]
            assert type(actual) is int
            assert actual == int(wanted)
        assert is_current(meta) is (expected == '1.0')


def test_public_version_does_not_replace_internal_semantic_structure_version():
    assert VERSION_KEY == 'inference_engine_version'
    assert LEGACY_VERSION == '0.1'
    assert CURRENT_VERSION == '1.0'
    assert LLM_EXTRACT_VERSION == 11
    meta = {'engine_version': 4, VERSION_KEY: '1.0'}
    assert is_current(meta)
    assert meta == {'engine_version': 4, VERSION_KEY: '1.0'}
    assert not is_current({'engine_version': 4})


@pytest.mark.parametrize('target', [
    None, 1, 1.0, True, {}, [], '', '1', 'v1.0', '1.0.0',
    '-1.0', '+1.0', '01.0', '1.00', '1.0 ', '1.0\n', '１.０',
    "1.0' OR 1=1 --", '9' * 10000 + '.0',
])
def test_invalid_targets_fail_closed_in_both_apis(target):
    with pytest.raises(ValueError, match='invalid inference version target'):
        is_current({}, target=target)
    with pytest.raises(ValueError, match='invalid inference version target'):
        current_meta_pred(target=target)


@pytest.mark.parametrize('column', [
    None, 1, '', 'a.meta OR 1=1', 'a.meta; DROP TABLE artifacts',
    'json_extract(a.meta,\'$.other\')', 'a.meta--', 'a.meta.extra', 'a."meta"',
])
def test_sql_api_rejects_expressions_and_non_internal_column_forms(column):
    with pytest.raises(ValueError, match='invalid inference metadata column'):
        current_meta_pred(column)


@pytest.mark.parametrize('version', ['0.0', '10.20', '9' * 30 + '.0'])
def test_canonical_multi_digit_versions_are_bounded_strings_not_integers(version):
    meta = {VERSION_KEY: version}
    assert read_version(meta) == version
    assert is_current(meta, target=version)
    assert not is_current(meta)
    with sqlite3.connect(':memory:') as db:
        assert db.execute(f'SELECT {current_meta_pred("meta", target=version)} '
                          'FROM (SELECT ? AS meta)', (json.dumps(meta),)).fetchone()[0] == 1
