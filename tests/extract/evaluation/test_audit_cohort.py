"""Fixed audit cohort pin/compare on wholly synthetic audit rows and private files."""
import hashlib
import json
import os
from fractions import Fraction

import pytest

import audit_cohort as ac
import mcs_util

CODES = ("storage", "leftover", "adherence")
DEFINITION = "synthetic-definition"


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def row(mid, *, version=7, reader="reader_extract", captured=False, code="storage",
        content="h", held=False, quality="complete", output=None, **extra):
    return {"message_id": mid, "project_id": 1, "content_hash": f"{content}{mid}",
            "reader": reader, "reason": "none", "extract_version": version,
            "classes": {code: captured} if code else {},
            "output_digest": digest(output or f"{mid}:{version}:{captured}"),
            "quality": quality, "held_prior": held, "hold_unknown": False,
            "newer_artifact_unknown": False, "profession": "pharmacist",
            "length": "len_s", "month": "month_202610", **extra}


def pin_of(rows):
    return ac.build_pin(rows, DEFINITION, CODES, {"since": None, "until": None},
                        {"generation_id": "g", "generated_at": 1})


def compare(pinned, rows, labels=None):
    return ac.compare(pinned, rows, DEFINITION, CODES, labels)


def test_migration_and_new_posts_do_not_count_as_improvement():
    pinned = pin_of([row(1), row(2), *(row(i, version=4) for i in (3, 4, 5))])
    assert [r["set"] for r in pinned["rows"]] == ["acceptance"] * 2 + ["inventory"] * 3
    after = [row(1, version=8), row(2, version=8),
             *(row(i, version=8, captured=True) for i in (3, 4, 5)),
             *(row(i, version=8, captured=True) for i in range(100, 150))]
    report = compare(pinned, after)
    assert (report["same_cohort"]["n"], report["same_cohort"]["before_missed"],
            report["same_cohort"]["after_missed"]) == (2, 2, 2)
    assert report["inventory"]["reanalysed"] == 3 and report["inventory"]["after_missed"] == 0
    assert report["new_posts"] == 50
    assert report["lexical_gate"] == "fail" and report["quality_gate"] != "pass"


def test_a_real_fix_is_measured_on_the_same_rows():
    pinned = pin_of([row(1), row(2)])
    report = compare(pinned, [row(1, version=8, captured=True), row(2, version=8)])
    assert report["same_cohort"]["classes"]["storage"] | {} == {
        **report["same_cohort"]["classes"]["storage"], "n": 2, "before_missed": 2,
        "after_missed": 1, "direction": "improved"}
    # two rows cannot show a non-overlapping interval, and labels are missing
    assert report["lexical_gate"] == "fail"
    assert report["semantic_gate"]["gate"] == "not_evaluable"


def test_not_yet_reanalysed_rows_leave_both_before_and_after():
    pinned = pin_of([row(1), row(2)])
    report = compare(pinned, [row(1, version=8, captured=True), row(2)])
    assert report["excluded"]["not_yet_reanalysed"] == 1
    assert (report["same_cohort"]["n"], report["same_cohort"]["before_missed"]) == (1, 1)
    report = compare(pinned, [row(1, version=8, captured=True), row(2, version=8)])
    assert report["excluded"]["not_yet_reanalysed"] == 0 and report["same_cohort"]["n"] == 2


def test_hash_change_deletion_reader_change_and_other_version_are_excluded_and_add_up():
    pinned = pin_of([row(i) for i in range(1, 6)])
    report = compare(pinned, [row(1, version=8, content="changed"),
                              row(3, version=8, reader="reader_projection"),
                              row(4, version=9), row(5, version=8)])
    assert {k: v for k, v in report["excluded"].items() if v} == {
        "hash_changed": 1, "deleted": 1, "reader_changed": 1, "other_version": 1}
    assert report["pinned"]["acceptance"] == report["same_cohort"]["n"] + sum(report["excluded"].values())
    assert report["retention"] == 0.2 and report["lexical_gate"] == "not_evaluable"


def test_an_empty_cohort_is_never_a_pass():
    pinned = pin_of([row(1), row(2)])
    report = compare(pinned, [])
    assert report["same_cohort"]["n"] == 0 and report["excluded"]["deleted"] == 2
    assert report["lexical_gate"] == report["quality_gate"] == "not_evaluable"


@pytest.mark.parametrize("after,failure", [
    (row(1, reader="no_output", version=None), "no_output"),
    (row(1, version=7, held=True, captured=True), "held_prior"),
    (row(1, version=8, captured=True, quality="attention"), "attention"),
])
def test_failed_held_or_attention_results_stay_misses(after, failure):
    report = compare(pin_of([row(1)]), [after])
    assert report["same_cohort"]["n"] == 1 and report["same_cohort"]["after_missed"] == 1
    assert report["same_cohort"]["after_failures"][failure] == 1


def test_a_worse_direction_fails_even_with_separated_intervals():
    pinned = pin_of([row(i, captured=True) for i in range(60)])
    report = compare(pinned, [row(i, version=8, captured=i >= 40) for i in range(60)])
    storage = report["same_cohort"]["classes"]["storage"]
    assert storage["after_ci"]["low"] > storage["before_ci"]["high"]
    assert storage["direction"] == "worse" and storage["gate"] == "fail"


def test_a_definition_change_refuses_to_compare():
    with pytest.raises(ac.CohortError, match="definition_changed"):
        ac.compare(pin_of([row(1)]), [], "other-definition", CODES)


def test_pin_keeps_only_allowlisted_fields_and_skips_cue_free_posts():
    pinned = pin_of([row(1, body="合成本文", meta={"x": 1}), row(2, code=None)])
    assert [set(r) for r in pinned["rows"]] == [{
        "message_id", "project_id", "content_hash", "reader", "extract_version",
        "classes", "output_digest", "set"}]
    with pytest.raises(ac.CohortError, match="bad_audit_row"):
        pin_of([row(1, code="unknown_cue")])


@pytest.mark.parametrize('uncertain', [
    {'hold_unknown': True}, {'newer_artifact_unknown': True},
    {'held_prior': True}, {'quality': 'attention'},
    {'reader': 'no_output', 'version': None, 'reason': 'unknown'},
])
def test_uncertain_baseline_refuses_entire_pin_without_dropping_cases(uncertain):
    with pytest.raises(ac.CohortError, match='baseline_uncertain'):
        pin_of([row(1), row(2, **uncertain)])


def test_pin_requires_uncertainty_signals_but_preserves_known_inventory():
    incomplete = row(1)
    incomplete.pop('newer_artifact_unknown')
    with pytest.raises(ac.CohortError, match='bad_audit_row'):
        pin_of([incomplete])
    pinned = pin_of([row(1), row(2, version=4, quality='attention'),
                     row(3, reader='no_output', version=None, reason='missing')])
    assert [item['set'] for item in pinned['rows']] == ['acceptance', 'inventory', 'inventory']
    # New checks do not alter the schema or matching of an existing pinned row.
    ac._check_pin_rows(pinned['rows'], CODES)


def test_cohort_definition_binds_baseline_selection(monkeypatch):
    before = ac._cohort_definition(DEFINITION)
    def changed_pin(*args):
        return None
    monkeypatch.setattr(ac, 'build_pin', changed_pin)
    assert ac._cohort_definition(DEFINITION) != before


@pytest.mark.parametrize('name', ['_gate', '_overall', '_retained', '_wilson', '_semantic',
                                  '_check_pin_rows', '_pinned_scope', '_audit_rows'])
def test_cohort_definition_rejects_changed_comparison_helpers(monkeypatch, name):
    before = ac._cohort_definition(DEFINITION)
    pinned = ac.build_pin([row(1)], before, CODES, {'since': None, 'until': None},
                          {'generation_id': 'g', 'generated_at': 1})
    def changed(*args, **kwargs):
        return None
    monkeypatch.setattr(ac, name, changed)
    after = ac._cohort_definition(DEFINITION)
    assert before != after
    with pytest.raises(ac.CohortError, match='definition_changed'):
        ac.compare(pinned, [row(1, version=8)], after, CODES)


@pytest.mark.parametrize('name,value', [
    ('LABEL_SAMPLE', 51), ('LABEL_SOURCES', frozenset({'human'})),
    ('MIN_RETENTION', Fraction(3, 4)),
    ('EXCLUSIONS', ('deleted',)), ('READERS', frozenset({'reader_extract'})),
    ('QUALITIES', frozenset({'complete'})),
])
def test_cohort_definition_rejects_changed_acceptance_constants(monkeypatch, name, value):
    before = ac._cohort_definition(DEFINITION)
    monkeypatch.setattr(ac, name, value)
    assert ac._cohort_definition(DEFINITION) != before


def test_cohort_definition_binds_wilson_implementation(monkeypatch):
    import stats_math
    before = ac._cohort_definition(DEFINITION)
    def different_interval(successes, denominator, z=1.96):
        return {'low': None, 'high': None}
    monkeypatch.setattr(stats_math, 'wilson_interval', different_interval)
    assert ac._cohort_definition(DEFINITION) != before


def test_pin_rejects_changed_input_json_validation(monkeypatch):
    import extraction_audit
    options = {'target_version': 8, 'inference_target': '0.1', 'hold_mode': 'historical'}
    before = ac._cohort_definition(extraction_audit.definition_fingerprint(**options))
    pinned = ac.build_pin([row(1)], before, CODES, {'since': None, 'until': None},
                          {'generation_id': 'g', 'generated_at': 1})
    def different_json(raw):
        return {}
    monkeypatch.setattr(extraction_audit, 'strict_json', different_json)
    after = ac._cohort_definition(extraction_audit.definition_fingerprint(**options))
    assert after != before
    with pytest.raises(ac.CohortError, match='definition_changed'):
        ac.compare(pinned, [row(1, version=8)], after, CODES)


def _big_fix(n=60, fixed=50):
    pinned = pin_of([row(i) for i in range(n)])
    after = [row(i, version=8, captured=i < fixed) for i in range(n)]
    return pinned, after


def _labels(pinned, after, **change):
    now = {r["message_id"]: r for r in after}
    return [{"message_id": r["message_id"], "content_hash": r["content_hash"], "class": code,
             "before_output_digest": r["output_digest"],
             "after_output_digest": now[r["message_id"]]["output_digest"],
             "before_captured": False, "after_captured": now[r["message_id"]]["classes"][code],
             "label_source": "internal_agent", **change}
            for r in pinned["rows"] if r["message_id"] in now for code in r["classes"]]


def test_a_proxy_improvement_alone_never_passes_the_quality_gate():
    pinned, after = _big_fix()
    report = compare(pinned, after)
    assert report["lexical_gate"] == "pass"
    assert report["semantic_gate"]["gate"] == "not_evaluable"
    assert report["quality_gate"] == "not_evaluable"


@pytest.mark.parametrize('uncertainty', ['hold_unknown', 'newer_artifact_unknown'])
def test_unverified_projection_cannot_be_excluded_to_pass_a_fixed_cohort(uncertainty):
    pinned, after = _big_fix()
    after[-1] = row(59, version=8, reader='reader_projection', captured=True,
                    quality='attention', **{uncertainty: True})
    report = compare(pinned, after, _labels(pinned, after))
    assert report['same_cohort']['n'] == 60 and report['excluded']['reader_changed'] == 0
    assert report['same_cohort']['after_failures']['attention'] == 1
    assert report['same_cohort']['after_missed'] == 10
    assert report['lexical_gate'] == report['quality_gate'] == 'not_evaluable'


def test_known_projection_remains_a_reader_change_and_unknown_inventory_is_failure():
    pinned = pin_of([row(1), row(2, version=4), row(3)])
    after = [row(1, version=8, reader='reader_projection'),
             row(2, version=8, reader='reader_projection', newer_artifact_unknown=True),
             row(3, version=8)]
    report = compare(pinned, after)
    assert report['excluded']['reader_changed'] == 1
    assert report['inventory']['reanalysis_failed'] == 1 and report['inventory']['projection'] == 0
    assert report['inventory']['after_missed'] == 1


def test_unknown_flags_do_not_override_a_real_source_change():
    pinned = pin_of([row(1)])
    report = compare(pinned, [row(1, content='different', reader='reader_projection', hold_unknown=True)])
    assert report['excluded']['hash_changed'] == 1 and report['same_cohort']['n'] == 0


def test_unknown_no_output_cannot_pass_even_when_most_of_cohort_improves():
    pinned, after = _big_fix()
    after[-1] = row(59, version=None, reader='no_output', reason='unknown')
    report = compare(pinned, after, _labels(pinned, after))
    assert report['same_cohort']['n'] == 60 and report['excluded']['reader_changed'] == 0
    assert report['same_cohort']['after_failures']['no_output'] == 1
    assert report['same_cohort']['after_missed'] == 10
    assert report['lexical_gate'] == report['quality_gate'] == 'not_evaluable'


def test_known_no_output_remains_a_failure_without_becoming_unknown():
    pinned, after = _big_fix()
    after[-1] = row(59, version=None, reader='no_output', reason='failed')
    report = compare(pinned, after)
    assert report['same_cohort']['after_failures']['no_output'] == 1
    assert report['lexical_gate'] == 'pass'  # known failure counts as a miss, not unknown


def test_comparison_requires_explicit_uncertainty_flags():
    current = row(1, version=8)
    current.pop('hold_unknown')
    with pytest.raises(ac.CohortError, match='bad_audit_row'):
        compare(pin_of([row(1)]), [current])


def test_comparison_requires_an_explicit_output_reason():
    current = row(1, version=None, reader='no_output')
    current.pop('reason')
    with pytest.raises(ac.CohortError, match='bad_audit_row'):
        compare(pin_of([row(1)]), [current])


def test_bound_paired_labels_complete_the_quality_gate():
    pinned, after = _big_fix()
    report = compare(pinned, after, _labels(pinned, after))
    semantic = report["semantic_gate"]
    assert semantic["classes"]["storage"]["needed"] == 50
    assert semantic["gate"] == "pass" and report["quality_gate"] == "pass"
    assert semantic["label_sources"] == {"human": 0, "internal_agent": 60}
    # the semantic judgement, not the proxy, decides: no real fix fails
    report = compare(pinned, after, _labels(pinned, after, after_captured=False))
    assert report["semantic_gate"]["gate"] == "fail" and report["quality_gate"] == "fail"


@pytest.mark.parametrize("change", [
    {"after_output_digest": digest("an older output")},
    {"before_output_digest": digest("another baseline")},
    {"content_hash": "edited"},
    {"label_source": "model"},
    {"after_captured": "yes"},
])
def test_stale_or_unbound_labels_make_the_semantic_gate_not_evaluable(change):
    pinned, after = _big_fix()
    labels = _labels(pinned, after)
    labels[0] = {**labels[0], **change}
    report = compare(pinned, after, labels)
    assert report["semantic_gate"]["gate"] == "not_evaluable"
    assert report["semantic_gate"]["stale_labels"] == 1


def test_too_few_labels_for_a_rare_class_are_not_evaluable():
    pinned, after = _big_fix()
    report = compare(pinned, after, _labels(pinned, after)[:49])
    assert report["semantic_gate"]["classes"]["storage"] | {} == {
        **report["semantic_gate"]["classes"]["storage"], "gate": "not_evaluable",
        "needed": 50, "labelled": 49}


def test_a_failed_attempt_label_binds_to_no_after_output():
    pinned = pin_of([row(i) for i in range(3)])
    after = [row(0, reader="no_output", version=None), row(1, version=8, captured=True),
             row(2, version=8, captured=True)]
    labels = _labels(pinned, [row(0, version=8)] + after[1:])
    labels[0]["after_output_digest"] = None
    labels[0]["after_captured"] = True  # a failed attempt cannot be a semantic success
    report = compare(pinned, after, labels)
    assert report["semantic_gate"]["stale_labels"] == 0
    assert report["semantic_gate"]["classes"]["storage"]["after_missed"] == 1


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("MCS_ROOT", str(tmp_path / "root"))
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path / "root"))
    data = tmp_path / "root" / "data"
    data.mkdir(parents=True)
    return data


def test_private_pin_round_trip_is_0600_inside_data(data_root):
    pinned = pin_of([row(1)])
    path = data_root / "cohort.jsonl"
    ac.write_pin(path, pinned)
    assert path.stat().st_mode & 0o777 == 0o600
    assert ac.read_pin(path) == pinned


def test_pin_paths_outside_data_symlinks_out_or_wide_modes_are_refused(data_root, tmp_path):
    pinned = pin_of([row(1)])
    with pytest.raises(ac.CohortError, match="path_outside_data"):
        ac.write_pin(tmp_path / "outside.jsonl", pinned)
    (data_root / "escape").symlink_to(tmp_path)
    with pytest.raises(ac.CohortError, match="path_outside_data"):
        ac.write_pin(data_root / "escape" / "pin.jsonl", pinned)
    wide = data_root / "wide.jsonl"
    wide.write_text("{}\n")
    wide.chmod(0o644)
    with pytest.raises(ac.CohortError, match="mode_too_wide"):
        ac.write_pin(wide, pinned)
    with pytest.raises(ac.CohortError, match="mode_too_wide"):
        ac.read_pin(wide)


def test_an_interrupted_write_keeps_the_old_pin_and_leaves_no_temp(data_root, monkeypatch):
    path = data_root / "cohort.jsonl"
    ac.write_pin(path, pin_of([row(1)]))
    before = path.read_bytes()

    def broken(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(ac.os, "fsync", broken)
    with pytest.raises(OSError):
        ac.write_pin(path, pin_of([row(2)]))
    assert path.read_bytes() == before
    assert sorted(os.listdir(data_root)) == ["cohort.jsonl"]


def test_failure_after_the_rename_reports_a_visible_but_unproven_pin(data_root, monkeypatch):
    import safe_io

    path = data_root / "cohort.jsonl"
    ac.write_pin(path, pin_of([row(1)]))
    before = path.read_bytes()

    def broken(*args, **kwargs):
        raise OSError("synthetic directory fsync failure")
    monkeypatch.setattr(safe_io, "fsync_dir", broken)
    with pytest.raises(ac.CohortError, match="pin_not_durable"):
        ac.write_pin(path, pin_of([row(2)]))
    assert path.read_bytes() != before         # the new pin is published, not restored
    assert path.stat().st_mode & 0o777 == 0o600
    assert sorted(os.listdir(data_root)) == ["cohort.jsonl"]


def test_failed_write_reads_back_at_most_the_new_pin_size(data_root, monkeypatch):
    path = data_root / "cohort.jsonl"
    ac.write_pin(path, pin_of([row(i) for i in range(40)]))    # old pin larger than the new one
    before = path.read_bytes()
    limits = []
    real_read = ac.read_regular_nofollow

    def spy(target, limit, error, **kwargs):
        limits.append(limit)
        return real_read(target, limit, error, **kwargs)

    def broken(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(ac, "read_regular_nofollow", spy)
    monkeypatch.setattr(ac.os, "fsync", broken)
    with pytest.raises(OSError, match="disk full"):
        ac.write_pin(path, pin_of([row(1)]))
    monkeypatch.undo()
    assert len(limits) == 1 and limits[0] < len(before)        # bounded by the new pin, not the old file
    assert path.read_bytes() == before


def test_report_is_ascii_aggregates_without_row_content():
    pinned, after = _big_fix()
    text = json.dumps(compare(pinned, after, _labels(pinned, after)), ensure_ascii=False)
    assert text.isascii()
    assert not any(r["content_hash"] in text or r["output_digest"] in text for r in after)


def _cli(*args, root):
    import subprocess
    import sys
    from pathlib import Path
    cli = Path(ac.__file__)
    return subprocess.run([sys.executable, str(cli), *map(str, args)], capture_output=True,
                          text=True, timeout=60, env={**os.environ, "MCS_ROOT": str(root)})


@pytest.mark.parametrize('public_version', ['0.1', '1.0'])
def test_cli_pins_and_compares_a_published_synthetic_snapshot(tmp_path, data_root, public_version):
    import time

    import ledger
    from extract_testkit import _ledger, _message
    from inference_version import LEGACY_VERSION, VERSION_KEY

    body = "本人の薬は合成ボックスで保管しています。"
    (tmp_path / "live").mkdir()
    db = _ledger(tmp_path / "live")
    db.save_messages([_message(1, body=body), _message(2, body="本日は訪問のみでした。")], project_id=1)
    content_hash = db.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0]
    # Keep two independent synthetic corpora. Missing version is historical
    # 0.1; a current public-generation corpus is explicitly stamped.
    complete = {"integrity": {"completeness_chunks": 1, "completeness": {
        "0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}}}

    def extract(version, document):
        db.db.execute(
            "INSERT INTO artifacts(kind,project_id,message_id,content,meta,created_at) "
            "VALUES('extract_llm',1,1,?,?,?)",
            (json.dumps(document, ensure_ascii=False),
             json.dumps({**({} if public_version == LEGACY_VERSION else {VERSION_KEY: public_version}), "hash": content_hash,
                         "extract_version": version, **complete}), time.time()))
        db.db.commit()

    def publish(name):
        return ledger.publish_snapshot(str(tmp_path / "live" / "ledger.db"), str(tmp_path / name))

    extract(7, {"summary": "合成の要約"})
    first = publish("snap1")
    pin = data_root / "cohort.jsonl"
    result = _cli("pin", "--snapshot", first, "--out", pin,
                  "--inference-version", public_version, root=data_root.parent)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["pinned"] == {"acceptance": 1, "inventory": 0}
    assert pin.stat().st_mode & 0o777 == 0o600 and "保管" not in pin.read_text()

    extract(8, {"summary": "合成の要約", "patient_context": [{
        "category": "medication_management", "text": "合成ボックスで保管しています",
        "evidence": "本人の薬は合成ボックスで保管しています。", "subject": "patient"}]})
    second = publish("snap2")
    result = _cli("compare", "--snapshot", second, "--pin", pin,
                  "--inference-version", public_version, root=data_root.parent)
    assert result.returncode == 1  # labels missing: never a quality pass
    report = json.loads(result.stdout)
    assert (report["same_cohort"]["n"], report["same_cohort"]["before_missed"],
            report["same_cohort"]["after_missed"]) == (1, 1, 0)
    assert report["quality_gate"] == "not_evaluable"
    assert result.stdout.isascii() and result.stderr == ""

    other = '1.0' if public_version == '0.1' else '0.1'
    mismatch = _cli('compare', '--snapshot', second, '--pin', pin,
                    '--inference-version', other, root=data_root.parent)
    assert mismatch.returncode == 2 and 'definition_changed' in mismatch.stderr

    outside = _cli("pin", "--snapshot", first, "--out", tmp_path / "outside.jsonl", root=data_root.parent)
    assert outside.returncode == 2 and outside.stdout == ""
    assert "path_outside_data" in outside.stderr and not (tmp_path / "outside.jsonl").exists()


def test_cli_does_not_publish_pin_for_newer_unverified_artifact(tmp_path, data_root):
    import ledger
    from extract_testkit import _ledger, _message
    from inference_version import CURRENT_VERSION, VERSION_KEY

    (tmp_path / 'live').mkdir()
    db = _ledger(tmp_path / 'live')
    try:
        db.save_messages([_message(1, body='本人の薬は合成ボックスで保管しています。')], project_id=1)
        digest = db.db.execute('SELECT content_hash FROM messages WHERE message_id=1').fetchone()[0]
        meta = {VERSION_KEY: CURRENT_VERSION, 'hash': digest, 'extract_version': 7,
                'integrity': {'completeness_chunks': 1, 'completeness': {
                    '0': {'state': 'hint_gaps_resolved', 'remaining_hint_candidates': 0}}}}
        db.artifact_add('extract_llm', '{"summary":"合成の要約"}', project_id=1, message_id=1, meta=meta)
        db.artifact_add('extract_llm', '{}', project_id=1, message_id=1, meta=meta)
        db.db.execute("UPDATE artifacts SET meta='{bad' WHERE artifact_id=(SELECT MAX(artifact_id) FROM artifacts)")
        db.db.commit()
        snapshot = ledger.publish_snapshot(str(tmp_path / 'live' / 'ledger.db'), str(tmp_path / 'snap'))
        pin = data_root / 'uncertain.jsonl'
        result = _cli('pin', '--snapshot', snapshot, '--out', pin, root=data_root.parent)
        assert result.returncode == 2 and 'baseline_uncertain' in result.stderr
        assert not pin.exists()
    finally:
        db.close()


def test_a_rare_class_excluded_entirely_keeps_its_baseline_and_is_not_evaluable():
    pinned = pin_of([*(row(i) for i in range(60)), row(60, code="leftover")])
    after = [row(i, version=8, captured=True) for i in range(60)]
    report = compare(pinned, after, _labels(pinned, after))
    leftover = report["same_cohort"]["classes"]["leftover"]
    assert (leftover["baseline"], leftover["n"], leftover["gate"]) == (1, 0, "not_evaluable")
    assert report["semantic_gate"]["classes"]["leftover"]["needed"] == 1
    assert report["lexical_gate"] == report["quality_gate"] == "not_evaluable"


def test_retention_compares_the_unrounded_fraction():
    pinned = pin_of([row(i) for i in range(2500)])
    after = [row(i, version=8, captured=True) for i in range(1999)]
    report = compare(pinned, after)
    assert report["retention"] == 0.8 and report["lexical_gate"] == "not_evaluable"
    assert compare(pinned, after + [row(1999, version=8)])["lexical_gate"] != "not_evaluable"
    assert type(report["min_retention"]) is float and report["min_retention"] == 0.8


@pytest.mark.parametrize("after", [
    row(1, version=8, captured=True, quality="attention"),
    row(1, version=8, captured=True, held=True),
    row(1, reader="no_output", version=None),
])
def test_an_unconfirmed_inventory_reanalysis_is_a_miss(after):
    report = compare(pin_of([row(1, version=4)]), [after])
    assert report["inventory"]["reanalysis_failed"] == 1
    assert report["inventory"]["after_missed"] == 1 and report["inventory"]["reanalysed"] == 0


def test_compare_replays_the_pinned_scope_and_drops_row_content(data_root, monkeypatch):
    import argparse

    import extraction_audit
    import mcs_view
    pinned = ac.build_pin([row(1)], ac._cohort_definition(DEFINITION), CODES,
                          {"since": 100, "until": 200, "as_of": 200, "project_id": 1},
                          {"generation_id": "old", "generated_at": 200})
    path = data_root / "pin.jsonl"
    ac.write_pin(path, pinned)
    seen = []

    class View:
        meta = {"generation_id": "new", "generated_at": 300}
        db = None

        def __init__(self, *args):
            pass

        def close(self):
            pass

    def rows(db, scope, *, target_version, inference_target, hold_mode):
        assert target_version == ac.TARGET_VERSION
        assert inference_target == '1.0' and hold_mode == 'historical'
        seen.append(dict(scope))
        yield row(1, version=8, captured=True, _body="合成本文", _document={}, _meta={})
    monkeypatch.setattr(mcs_view, "View", View)
    monkeypatch.setattr(extraction_audit, "iter_audit_rows", rows)
    def definition(*, target_version, inference_target, hold_mode):
        assert target_version == ac.TARGET_VERSION and inference_target == '1.0' and hold_mode == 'historical'
        return DEFINITION
    monkeypatch.setattr(extraction_audit, "definition_fingerprint", definition)
    monkeypatch.setattr(extraction_audit, "CUE_CODES", CODES)
    args = argparse.Namespace(pin=path, labels=None, snapshot=data_root / "s.db")
    report = ac.cmd_compare(args)
    assert {k: seen[0][k] for k in ("since", "until", "as_of", "project_id")} == {
        "since": 100, "until": 200, "as_of": 200, "project_id": 1}
    assert report["same_cohort"]["after_missed"] == 0
    rows_kept = ac._audit_rows(data_root / "s.db", args, ac._pinned_scope(pinned["header"]))[0]
    assert all(set(r) == set(ac._ROW_KEYS) for r in rows_kept)
    View.meta = {"generation_id": "older", "generated_at": 150}
    with pytest.raises(ac.CohortError, match="snapshot_older_than_pin"):
        ac.cmd_compare(args)


@pytest.mark.parametrize("corrupt", [
    lambda r: r.pop("content_hash"),
    lambda r: r.update(extra="x"),
    lambda r: r.update(classes={"unknown_cue": False}),
    lambda r: r.update(classes={"storage": "no"}),
    lambda r: r.update(classes={}),
    lambda r: r.update(output_digest="not-a-digest"),
    lambda r: r.update(set="inventory"),
    lambda r: r.update(message_id="1"),
], ids=["missing", "extra", "unknown_class", "nonbool", "empty_classes", "digest", "wrong_set", "id_type"])
def test_a_corrupted_pin_row_is_refused_before_counting(corrupt):
    pinned = pin_of([row(1), row(2)])
    corrupt(pinned["rows"][0])
    with pytest.raises(ac.CohortError, match="pin_schema"):
        compare(pinned, [row(1, version=8, captured=True), row(2, version=8)])


def test_a_duplicated_pin_row_is_refused_and_a_valid_pin_still_compares():
    pinned = pin_of([row(1), row(2)])
    assert compare(pinned, [row(1, version=8, captured=True), row(2, version=8)])["same_cohort"]["n"] == 2
    pinned["rows"].append(dict(pinned["rows"][0]))
    with pytest.raises(ac.CohortError, match="pin_schema"):
        compare(pinned, [row(1, version=8), row(2, version=8)])


@pytest.mark.parametrize("failure", ["adoption", "chmod", "fsync"])
def test_pin_descriptor_is_closed_before_failed_staging_is_removed(
        data_root, monkeypatch, failure):
    import errno
    import safe_io

    path = data_root / "cohort.jsonl"
    ac.write_pin(path, pin_of([row(1)]))
    original = path.read_bytes()
    descriptors, removed = [], []
    mkstemp, unlink = safe_io.tempfile.mkstemp, ac.os.unlink

    def owned_temp(*args, **kwargs):
        descriptor, name = mkstemp(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor, name

    def fail(*args, **kwargs):
        raise OSError("synthetic pin fault")

    def remove_after_close(name):
        with pytest.raises(OSError) as closed:
            os.fstat(descriptors[-1])
        assert closed.value.errno == errno.EBADF
        removed.append(name)
        return unlink(name)

    monkeypatch.setattr(safe_io.tempfile, "mkstemp", owned_temp)
    monkeypatch.setattr(ac.os, "unlink", remove_after_close)
    if failure == "adoption":
        monkeypatch.setattr(safe_io.os, "fdopen", fail)
    else:
        monkeypatch.setattr(ac.os, failure, fail)
    try:
        with pytest.raises(OSError, match="synthetic pin fault"):
            ac.write_pin(path, pin_of([row(2)]))
        assert len(descriptors) == len(removed) == 1
        with pytest.raises(OSError) as closed:
            os.fstat(descriptors[0])
        assert closed.value.errno == errno.EBADF
        assert path.read_bytes() == original
        assert sorted(os.listdir(data_root)) == ["cohort.jsonl"]
    finally:
        # The test must reclaim a leaked descriptor even if its oracle fails.
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise
