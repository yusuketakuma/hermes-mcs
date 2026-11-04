"""Synthetic stored progress keeps absence, invalidity and valid generations distinct."""
import hashlib
import json
import sqlite3

import pytest

import drug_map


GENERATION = ['synthetic-dictionary', 'a' * 64, False, drug_map.RESOLVER_VERSION]


@pytest.mark.parametrize('raw', [None, True, False, 0, 1.0, {}, [], '', '{', 'null', 'true', 'false',
                                  '0', '1.0', '"text"', '[]', '{}', '[{}]',
                                  '{"dictionary":null}', '{"cursor":0}',
                                  '{"dictionary":null,"cursor":0,"extra":1}',
                                  '{"dictionary":null,"cursor":0,"cursor":1}',
                                  '{"dictionary":null,"cursor":NaN}',
                                  '[' * 1100 + '0' + ']' * 1100])
def test_invalid_raw_progress_is_never_legacy_absence(raw):
    assert drug_map.parse_annotation_progress(raw) == {'invalid': True}


@pytest.mark.parametrize('cursor', [True, False, -1, 2**63, 0.0, '0', None, {}, []])
def test_invalid_cursor_never_becomes_a_valid_generation(cursor):
    assert drug_map.parse_annotation_progress(json.dumps({'dictionary': GENERATION, 'cursor': cursor})) == {'invalid': True}


@pytest.mark.parametrize('generation', [[], {}, 'dictionary', [1, 'a'*64, False, drug_map.RESOLVER_VERSION],
    [' ', 'a'*64, False, drug_map.RESOLVER_VERSION],
    ['x'*513, 'a'*64, False, drug_map.RESOLVER_VERSION],
    ['synthetic', 'A'*64, False, drug_map.RESOLVER_VERSION],
    ['synthetic', 'a'*63, False, drug_map.RESOLVER_VERSION],
    ['synthetic', 'a'*64, 0, drug_map.RESOLVER_VERSION],
    ['synthetic', 'a'*64, True, 'unknown'],
    ['synthetic', 'a'*64, True, {'nested': drug_map.RESOLVER_VERSION}]])
def test_invalid_generation_shape_is_preserved(generation):
    assert drug_map.parse_annotation_progress(json.dumps({'dictionary': generation, 'cursor': 0})) == {'invalid': True}


@pytest.mark.parametrize('generation', [None, GENERATION, [*GENERATION[:2], True, GENERATION[3]]])
@pytest.mark.parametrize('cursor', [0, 1, 2**63-1])
def test_valid_progress_preserves_cursor_and_generation(generation, cursor):
    value = {'dictionary': generation, 'cursor': cursor}
    assert drug_map.parse_annotation_progress(json.dumps(value)) == value
    assert drug_map.parse_annotation_progress(json.dumps(value).encode()) == value


def test_reader_reuses_exact_query_and_distinguishes_missing_from_null():
    class DB:
        row = None

        def execute(self, sql, params):
            assert sql == drug_map.ANNOTATION_PROGRESS_SQL
            assert sql == 'SELECT content FROM artifacts WHERE kind=? ORDER BY artifact_id DESC LIMIT 1'
            assert params == (drug_map.PROGRESS_KIND,)
            return self

        def fetchone(self):
            return self.row

    db = DB()
    assert drug_map.read_annotation_progress(db) is None
    db.row = (None,)
    assert drug_map.read_annotation_progress(db) == {'invalid': True}
    value = {'dictionary': GENERATION, 'cursor': 3}
    db.row = (json.dumps(value),)
    assert drug_map.read_annotation_progress(db) == value


def test_latest_marker_and_generation_signature_keep_original_semantics():
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY, kind TEXT, content TEXT)')
        legacy = drug_map.generation_signature(db)
        assert legacy == hashlib.sha256(json.dumps('legacy', sort_keys=True).encode()).hexdigest()
        for cursor in (0, 17):
            db.execute('INSERT INTO artifacts(kind,content) VALUES(?,?)',
                       (drug_map.PROGRESS_KIND, json.dumps({'dictionary': GENERATION, 'cursor': cursor})))
            assert drug_map.read_annotation_progress(db)['cursor'] == cursor
            assert drug_map.generation_signature(db) == hashlib.sha256(json.dumps(GENERATION, sort_keys=True).encode()).hexdigest()
        db.execute('INSERT INTO artifacts(kind,content) VALUES(?,NULL)', (drug_map.PROGRESS_KIND,))
        invalid = drug_map.generation_signature(db)
        assert invalid != legacy
        assert invalid == hashlib.sha256(json.dumps('invalid', sort_keys=True).encode()).hexdigest()
        db.execute('INSERT INTO artifacts(kind,content) VALUES(?,?)',
                   (drug_map.PROGRESS_KIND, json.dumps({'dictionary': GENERATION, 'cursor': -1})))
        assert drug_map.generation_signature(db) == invalid
