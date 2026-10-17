"""Local master workflows route real synthetic inputs without activating a dictionary."""
import hashlib
import json

import import_drug_master as importer
import mcs_drug
from test_import_drug_master import _input, _row, _write_result


def test_related_and_surface_cli(tmp_path, capsys):
    source, pin = _input(tmp_path, [_row(), _row(code='900000002', name='架空ミドリ錠５ｍｇ')])
    path, _ = _write_result(tmp_path, importer.convert(source, pin))
    assert mcs_drug.main(['related', '架空アオ錠５ｍｇ', '--dictionary', str(path),
                          '--sha256', hashlib.sha256(path.read_bytes()).hexdigest()]) == 0
    report = json.loads(capsys.readouterr().out)
    assert '架空ミドリ錠５ｍｇ' in report['names']
    assert mcs_drug.main(['check-surface', '架空錠5mg', '--reference', '架空錠10mg']) == 0
    report = json.loads(capsys.readouterr().out)
    assert 'strength_difference' in report['reasons']
    assert not report['equivalence_confirmed']


def test_master_impact_cli_preserves_pin_and_missing_adoption(tmp_path, monkeypatch, capsys):
    old_dir, new_dir = tmp_path / 'old', tmp_path / 'new'
    old_dir.mkdir()
    new_dir.mkdir()
    before, before_pin = _input(old_dir)
    after, after_pin = _input(new_dir, [_row(name='架空アオ新版錠５ｍｇ')])
    pins = []
    for directory, pin in ((old_dir, before_pin), (new_dir, after_pin)):
        file = directory / 'pin.json'
        file.write_text(json.dumps(pin))
        pins.append(file)
    monkeypatch.setattr(mcs_drug, 'load_config', lambda: {})
    args = ['master-impact', '--before', str(before), '--after', str(after),
            '--before-pin', str(pins[0]), '--after-pin', str(pins[1])]
    assert mcs_drug.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['total'] == 1 and report['adoption_coverage'] == 'unknown'
    assert not report['activation']
    before_pin['sha256'] = '0' * 64
    pins[0].write_text(json.dumps(before_pin))
    assert mcs_drug.main(args) == 1
    assert '架空' not in capsys.readouterr().err
