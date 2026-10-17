"""Fictional official-layout rows exercise cross-name and update review contracts."""
import hashlib
import json

import pytest

import drug_map
import drug_master_compare as impact
import import_drug_master as importer
from test_import_drug_master import _input, _row, _write_result


def test_products_preserve_form_unit_and_group_names(tmp_path):
    a, b = _row(), _row(code='900000002', name='架空ミドリ錠５ｍｇ')
    a[27] = b[27] = '1'
    path, pin = _input(tmp_path, [a, b])
    _, dictionary = _write_result(tmp_path, importer.convert(path, pin))
    result = dictionary.related_names(a[4])
    assert result['status'] == 'resolved'
    assert a[4] in result['names'] and b[4] in result['names'] and a[37] in result['names']
    assert result['product_count'] == 2
    assert result['products'][0] == {'medicine': a[2], 'name': a[4], 'unit': '錠', 'form_code': '1'}
    assert dictionary.related_names(a[4], limit=1)['truncated']
    assert not result['approved']
    assert dictionary.related_names('不明薬')['status'] == 'unresolved'


def test_old_source_dictionary_without_products_remains_readable(tmp_path):
    path, pin = _input(tmp_path)
    payload = json.loads(importer.convert(path, pin).payload)
    for entry in payload['entries']:
        del entry['products']
    private = tmp_path / 'old.json'
    private.write_text(json.dumps(payload))
    private.chmod(0o600)
    dictionary = drug_map.load(private, expected_sha256=hashlib.sha256(private.read_bytes()).hexdigest())
    assert dictionary.related_names('架空アオ錠５ｍｇ')['metadata_available'] is False
    assert dictionary.resolve('架空アオ錠５ｍｇ')['status'] == 'resolved'
    assert next(iter(dictionary.aliases.values()))[0].source_codes == ('900000001',)


@pytest.mark.parametrize('left,right,units,reason', [
    ('架空錠5mg', '架空錠10mg', ('錠', '錠'), 'strength_difference'),
    ('架空錠5mg', '架空カプセル5mg', ('錠', '錠'), 'form_difference'),
    ('架空錠5mg', '架空錠5mg', ('錠', '包'), 'unit_difference'),
])
def test_explicit_difference_is_only_review(left, right, units, reason):
    result = drug_map.compare_surfaces(left, right, observed_unit=units[0], reference_unit=units[1])
    assert result['status'] == 'review' and reason in result['reasons']
    assert result['equivalence_confirmed'] is False


def test_missing_information_never_confirms_a_match():
    result = drug_map.compare_surfaces('架空', '架空錠5mg')
    assert result['status'] == 'unknown'
    assert set(result['missing']) == {'strength', 'form', 'unit'}


def test_impact_retired_rows_are_not_lost_by_candidate_filter(tmp_path):
    old_dir, new_dir = tmp_path / 'old', tmp_path / 'new'
    old_dir.mkdir()
    new_dir.mkdir()
    a, b, c = _row(), _row(code='900000002'), _row(code='900000003')
    old, old_pin = _input(old_dir, [a, b, c])
    _, adopted = _write_result(tmp_path, importer.convert(old, old_pin))
    a[4] = '架空アオ新錠５ｍｇ'
    a[3] = str(len(a[4]))
    b[0], b[30] = '9', '20261001'
    c[33] = '20261101'
    new, new_pin = _input(new_dir, [a, b, c])
    result = impact.compare(old, old_pin, new, new_pin, adopted=adopted, limit=2)
    assert result['total'] == 3 and result['truncated']
    assert result['counts']['retirement_review'] == 1
    assert result['counts']['transition_deadline_changed'] == 1
    assert result['counts']['name_changed'] == 1
    assert result['adopted_affected_count'] == 3
    assert result['adoption_coverage'] == 'complete'
    assert all(i['requires_review'] for i in result['items'])
    assert not result['activation']


def test_catalog_explicit_older_edition_and_pinned_csv(tmp_path):
    path, pin = _input(tmp_path, zipped=False)
    old_path = path.with_name('y_20260930.csv')
    path.rename(old_path)
    pin.update(edition='20260930', member=old_path.name, as_of='2026-09-30')
    assert importer.catalog(old_path, pin)['edition'] == '20260930'
    with pytest.raises(importer.MasterError, match='pin_schema_layout_or_edition'):
        importer.convert(old_path, pin)
    pin['sha256'] = '0' * 64
    with pytest.raises(importer.MasterError, match='source_hash_mismatch'):
        importer.catalog(old_path, pin)


def test_related_ambiguity_and_malformed_product_binding(tmp_path):
    a = _row()
    b = _row(code='900000002', general='GEN000000002', general_text='【般】架空別錠５ｍｇ')
    path, pin = _input(tmp_path, [a, b])
    result = importer.convert(path, pin)
    _, dictionary = _write_result(tmp_path, result)
    related = dictionary.related_names(a[4])
    assert related['status'] == 'ambiguous'
    assert len(related['identities']) == 2
    payload = json.loads(result.payload)
    payload['entries'][0]['products'][0]['medicine'] = '900000099'
    bad = tmp_path / 'bad.json'
    bad.write_text(json.dumps(payload))
    bad.chmod(0o600)
    with pytest.raises(ValueError, match='drug_map_product_metadata'):
        drug_map.load(bad, expected_sha256=hashlib.sha256(bad.read_bytes()).hexdigest())
