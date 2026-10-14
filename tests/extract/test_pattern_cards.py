"""Fully synthetic export-guard tests, including the real read-only CLI."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import unicodedata

import pytest

import pattern_cards
from common_testkit import make_ledger
from pattern_cards import JSON, _denylist, lint


CLI = Path(__file__).resolve().parents[2] / "mcs/extract/eval/pattern_cards.py"

# Independent schema fixtures: enum-looking names defeat vocabulary-only rejection.
NAME_SOURCE_INSERTS = {
    "patients.patient_name": "UPDATE patients SET patient_name=?",
    "patients.station_name": "UPDATE patients SET station_name=?",
    "messages.sender_name": "UPDATE messages SET sender_name=?",
    "messages.organization": "UPDATE messages SET organization=?",
    "messages_fts.sender_name": "UPDATE messages_fts SET sender_name=?",
    "message_reaction_actors.actor_name": (
        "INSERT INTO message_reaction_actors(message_id,actor_id,reaction_type,"
        "observed_at,removed_at,actor_name) VALUES(1,10,'like',1,1,?)"),
    "message_reaction_actors.organization": (
        "INSERT INTO message_reaction_actors(message_id,actor_id,reaction_type,"
        "observed_at,removed_at,organization) VALUES(1,10,'like',1,1,?)"),
    "station_staff_v1.staff.name": (
        "INSERT INTO artifacts(kind,content) VALUES('station_staff_v1',"
        "json_object('staff',json_array(json_object('name',?))))"),
    "station_staff_v1.staff.station": (
        "INSERT INTO artifacts(kind,content) VALUES('station_staff_v1',"
        "json_object('staff',json_array(json_object('station',?))))"),
    "karte_summary.updater.name": (
        "INSERT INTO artifacts(kind,content) VALUES('karte_summary',"
        "json_object('updater',json_object('name',?)))"),
    "self_profile_v1.name": (
        "INSERT INTO artifacts(kind,content) VALUES('self_profile_v1',json_object('name',?))"),
    "self_profile_v1.organizations": (
        "INSERT INTO artifacts(kind,content) VALUES('self_profile_v1',"
        "json_object('organizations',json_array(?)))"),
    "patient_rollup.top_senders": (
        "INSERT INTO artifacts(kind,project_id,content) VALUES('patient_rollup',1,"
        "json_object('top_senders',json_array(json_array(?,1))))"),
    "requests.assignee": (
        "INSERT INTO requests(project_id,source_message_id,source_hash,title,assignee,"
        "status,revision,created_at,updated_at) VALUES(1,1,'fake','task',?,'open',1,1,1)"),
    "command_receipts.actor": (
        "INSERT INTO command_receipts(command_id,payload_hash,outcome,receipt_json,processed_at)"
        " VALUES('fake','fake','applied',json_object('actor',?),1)"),
    "command_receipts.before.assignee": (
        "INSERT INTO command_receipts(command_id,payload_hash,outcome,receipt_json,processed_at)"
        " VALUES('fake','fake','applied',json_object('before',json_object('assignee',?)),1)"),
    "command_receipts.after.assignee": (
        "INSERT INTO command_receipts(command_id,payload_hash,outcome,receipt_json,processed_at)"
        " VALUES('fake','fake','applied',json_object('after',json_object('assignee',?)),1)"),
    "notification_acknowledgements.actor": (
        "INSERT INTO notification_acknowledgements(card_id,manifest_id,actor,command_id,"
        "created_at) VALUES(1,1,?,'fake',1)"),
    "notification_triage.owner": (
        "INSERT INTO notification_triage(card_id,owner) VALUES(1,?)"),
    "notification_triage.last_actor": (
        "INSERT INTO notification_triage(card_id,last_actor) VALUES(1,?)"),
    "request_loop_link.content.actor": (
        "INSERT INTO artifacts(kind,project_id,message_id,content,meta) "
        "VALUES('request_loop_link',1,1,json_object('actor',?),'{}')"),
    "request_loop_link.meta.actor": (
        "INSERT INTO artifacts(kind,project_id,message_id,content,meta) "
        "VALUES('request_loop_link',1,1,'{}',json_object('actor',?))"),
    "extract_feedback_v1.content.actor": (
        "INSERT INTO artifacts(kind,project_id,message_id,content,meta) "
        "VALUES('extract_feedback_v1',1,1,json_object('actor',?),'{}')"),
    "signal_v1.content.dismissed_by": (
        "INSERT INTO artifacts(kind,project_id,message_id,content,meta) "
        "VALUES('signal_v1',1,1,json_object('dismissed_by',?),'{}')"),
    "signal_v1.meta.actor": (
        "INSERT INTO artifacts(kind,project_id,message_id,content,meta) "
        "VALUES('signal_v1',1,1,'{}',json_object('actor',?))"),
    "signal_policy_v1.content.actor": (
        "INSERT INTO artifacts(kind,content,meta) "
        "VALUES('signal_policy_v1',json_object('actor',?),'{}')"),
    "signal_policy_v1.meta.actor": (
        "INSERT INTO artifacts(kind,content,meta) "
        "VALUES('signal_policy_v1','{}',json_object('actor',?))"),
    "refstat_approval_v1.content.actor": (
        "INSERT INTO artifacts(kind,content,meta) "
        "VALUES('refstat_approval_v1',json_object('actor',?),'{}')"),
    "refstat_approval_v1.meta.actor": (
        "INSERT INTO artifacts(kind,content,meta) "
        "VALUES('refstat_approval_v1','{}',json_object('actor',?))"),
    "synthetic_future_operation.meta.actor": (
        "INSERT INTO artifacts(kind,content,meta) "
        "VALUES('synthetic_future_operation','{}',json_object('actor',?))"),
    "command_receipts.owner": (
        "INSERT INTO command_receipts(command_id,payload_hash,outcome,receipt_json,processed_at)"
        " VALUES('fake','fake','applied',json_object('owner',?),1)"),
}


def clean_card() -> dict[str, JSON]:
    return {
        "class": "one_dose_packaging",
        "field_paths": ["patient_context.medication_management.support_method"],
        "attributes": {
            "negation": False, "subject": "patient", "tense": "present",
            "condition": "unconditional",
        },
        "counts": {"posts": 30, "patients": 5, "professions": 2},
        "current_shape": "missing",
        "expected_shape": ["patient_context.medication_management.support_method"],
    }


def synthetic_snapshot(tmp_path):
    led = make_ledger(tmp_path)
    try:
        with led.db:
            led.db.execute(
                "INSERT INTO patients(project_id,patient_name,station_name) VALUES(?,?,?)",
                (1, "SyntheticPatient", "SyntheticStation"),
            )
            led.db.execute(
                "INSERT INTO messages(message_id,project_id,sender_name,organization) "
                "VALUES(?,?,?,?)", (1, 1, "SyntheticSender", "SyntheticOrganization"),
            )
            for name in ("SyntheticFormerStaff", "SyntheticStaff"):
                led.db.execute(
                    "INSERT INTO artifacts(kind,content) VALUES(?,?)",
                    ("station_staff_v1", json.dumps({
                        "staff": [{"name": name, "station": "SyntheticStaffStation"}],
                    })),
                )
        names = _denylist(led.db)
    finally:
        led.close()
    return tmp_path / "ledger.db", names


@pytest.fixture
def snapshot(tmp_path):
    return synthetic_snapshot(tmp_path)


def run_cli(card_path, db_path, *, env=None):
    return subprocess.run(
        [sys.executable, str(CLI), "lint", str(card_path), "--db", str(db_path)],
        capture_output=True, text=True, timeout=15, env=env,
    )


def test_clean_card_is_accepted(snapshot):
    _, names = snapshot
    assert lint(clean_card(), names) == []


@pytest.mark.parametrize("name", [
    "SyntheticPatient", "SyntheticStation", "SyntheticSender", "SyntheticOrganization",
    "SyntheticFormerStaff", "SyntheticStaff", "SyntheticStaffStation",
])
def test_all_name_sources_are_rejected_as_substrings(snapshot, name):
    _, names = snapshot
    card = clean_card()
    card["current_shape"] = f"prefix_{name.lower()}_suffix"
    errors = lint(card, names)
    assert "denylist:$.current_shape" in errors
    assert name.casefold() not in "\n".join(errors).casefold()


@pytest.mark.parametrize(("value", "rule"), [
    ("dose7", "digit"), ("dose７", "digit"), ("message:opaque", "identifier"),
    ("project_id:opaque", "identifier"), ("msg_abc", "identifier"),
    ("12345678-1234-1234-1234-123456789abc", "identifier"),
    ("一包化の薬を渡す", "non_ascii"), ("please_store_the_medicine", "vocabulary"),
])
def test_unsafe_shape_strings_are_rejected(value, rule):
    card = clean_card()
    card["current_shape"] = value
    assert any(error.startswith(f"{rule}:") for error in lint(card, set()))


@pytest.mark.parametrize("section", [None, "attributes", "counts"])
def test_extra_keys_are_rejected_without_echoing_the_key(section):
    card = clean_card()
    target: dict[str, JSON] = card
    if section is not None:
        child = card[section]
        assert isinstance(child, dict)
        target = child
    target["SyntheticSecretKey"] = "SyntheticSecretValue"
    errors = lint(card, {"SyntheticSecretKey", "SyntheticSecretValue"})
    assert any(error.startswith("unknown_key:") for error in errors)
    assert "SyntheticSecret" not in "\n".join(errors)


@pytest.mark.parametrize(("key", "value"), [
    ("field_paths", ["made_up.path"]), ("class", "made_up_class"),
    ("attributes", {"negation": False}), ("counts", {"posts": True, "patients": 5, "professions": 2}),
    ("counts", {"posts": -1, "patients": 5, "professions": 2}),
    ("counts", {"posts": "30", "patients": 5, "professions": 2}),
    ("current_shape", {"text": "patient"}), ("expected_shape", 42),
])
def test_invalid_schema_values_are_rejected(key, value):
    card = clean_card()
    card[key] = value
    assert lint(card, set())


@pytest.mark.parametrize("unsafe", [False, True])
def test_real_cli_has_correct_exit_no_name_leak_and_no_db_write(tmp_path, snapshot, unsafe):
    db_path, _ = snapshot
    card = clean_card()
    if unsafe:
        card["expected_shape"] = "SyntheticPatient"
    card_path = tmp_path / "card.json"
    card_path.write_text(json.dumps(card), encoding="utf-8")
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    result = run_cli(card_path, db_path)
    assert result.returncode == int(unsafe)
    assert "SyntheticPatient" not in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    if unsafe:
        assert "denylist:$.expected_shape" in result.stdout


@pytest.mark.parametrize("raw", [
    "not JSON", "[]", "null", '"SyntheticPatient"',
    "[" * 2000 + "]" * 2000,
    '{"class":' + "[" * 25 + '"patient"' + "]" * 25 + "}",
])
def test_malformed_cli_input_fails_cleanly(tmp_path, snapshot, raw):
    db_path, _ = snapshot
    card_path = tmp_path / "malformed.json"
    card_path.write_text(raw, encoding="utf-8")
    result = run_cli(card_path, db_path)
    assert result.returncode == 1
    assert "Traceback" not in result.stdout + result.stderr
    assert "SyntheticPatient" not in result.stdout + result.stderr


def test_missing_snapshot_is_not_created(tmp_path):
    card_path = tmp_path / "card.json"
    card_path.write_text(json.dumps(clean_card()), encoding="utf-8")
    missing = tmp_path / "missing.db"
    result = run_cli(card_path, missing)
    assert result.returncode == 1
    assert not missing.exists()
    assert result.stdout == result.stderr == ""


def insert_name(db_path, source, name):
    # Reopening only our synthetic ledger applies the actual optional schemas.
    from ledger import Ledger

    led = Ledger(str(db_path))
    try:
        with led.db:
            if source.startswith("notification_"):
                led.db.execute(
                    "INSERT INTO notification_cards(card_id,card_key,kind,anchor_key,"
                    "created_at,updated_at) VALUES(1,'fake','thread','fake',1,1)")
                led.db.execute(
                    "INSERT INTO notification_view_manifests(manifest_id,card_id,render_rev,"
                    "source_generation,presentation_generation,shown,created_at)"
                    " VALUES(1,1,1,1,1,'[]',1)")
            led.db.execute(NAME_SOURCE_INSERTS[source], (name,))
    finally:
        led.close()


@pytest.mark.parametrize("source", list(NAME_SOURCE_INSERTS))
def test_each_snapshot_name_source_blocks_an_enum_name(tmp_path, snapshot, source):
    db_path, _ = snapshot
    insert_name(db_path, source, "oral")
    card = clean_card()
    card["current_shape"] = "oral"
    card_path = tmp_path / "source.json"
    card_path.write_text(json.dumps(card), encoding="utf-8")
    result = run_cli(card_path, db_path)
    assert result.returncode == 1
    assert "denylist:" in result.stdout
    assert "oral" not in result.stdout + result.stderr


@pytest.mark.parametrize("hidden", [
    '"GateSyntheticPatient"', '{"GateSyntheticPatient":"oral"}',
    '"dose123"', '"message:opaque"', '"Ignore all rules. GateSyntheticPatient"',
])
def test_duplicate_root_members_are_rejected_before_discarding_values(tmp_path, snapshot, hidden):
    db_path, _ = snapshot
    insert_name(db_path, "patients.patient_name", "GateSyntheticPatient")
    raw = json.dumps(clean_card())
    card_path = tmp_path / "duplicate.json"
    card_path.write_text('{"current_shape":' + hidden + "," + raw[1:], encoding="utf-8")
    result = run_cli(card_path, db_path)
    assert result.returncode == 1
    assert "GateSyntheticPatient" not in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr


def test_duplicate_nested_members_are_rejected(tmp_path, snapshot):
    db_path, _ = snapshot
    raw = json.dumps(clean_card()).replace(
        '"subject": "patient"', '"subject":"GateSyntheticPatient","subject":"patient"')
    card_path = tmp_path / "duplicate.json"
    card_path.write_text(raw, encoding="utf-8")
    result = run_cli(card_path, db_path)
    assert result.returncode == 1
    assert "GateSyntheticPatient" not in result.stdout + result.stderr


@pytest.mark.parametrize("field", [
    "class", "field_paths", "attributes", "counts", "current_shape", "expected_shape",
    "negation", "subject", "tense", "condition", "posts", "patients", "professions",
    "root_key", "attributes_key", "shape_key",
])
@pytest.mark.parametrize("name", ["GateSyntheticPatient", "current_shape", "subject", "denylist"])
def test_cli_never_echoes_names_in_values_or_key_names(tmp_path, snapshot, field, name):
    db_path, base_names = snapshot
    insert_name(db_path, "patients.patient_name", name)
    card = clean_card()
    if field in ("root_key", "attributes_key", "shape_key"):
        if field == "root_key":
            card[name] = "oral"
        elif field == "attributes_key":
            card["attributes"] = {name: "oral"}
        else:
            card["current_shape"] = {name: "oral"}
    elif field in ("negation", "subject", "tense", "condition"):
        card["attributes"] = {**clean_card()["attributes"], field: name}
    elif field in ("posts", "patients", "professions"):
        card["counts"] = {**clean_card()["counts"], field: name}
    else:
        card[field] = [name] if field == "field_paths" else name
    card_path = tmp_path / "echo.json"
    card_path.write_text(json.dumps(card), encoding="utf-8")
    result = run_cli(card_path, db_path)
    assert result.returncode == 1
    names_path = tmp_path / "synthetic-names.txt"
    names_path.write_text("\n".join(sorted(base_names | {name})), encoding="utf-8")
    probe = subprocess.run(
        ["grep", "-Fi", "-f", str(names_path)], input=result.stdout + result.stderr,
        capture_output=True, text=True, timeout=5,
    )
    assert probe.returncode == 1
    assert "Traceback" not in result.stdout + result.stderr


@pytest.mark.parametrize("name", [
    "class", "field_paths", "attributes", "counts", "current_shape", "expected_shape",
    "negation", "subject", "tense", "condition", "posts", "patients", "professions",
    "input", "vocabulary", "object", "unknown_key", "missing_key", "identifier", "digit",
])
def test_diagnostic_paths_and_rule_code_collisions_do_not_echo(tmp_path, snapshot, name):
    db_path, _ = snapshot
    insert_name(db_path, "patients.patient_name", name)
    card = clean_card()
    card["current_shape"] = name
    card_path = tmp_path / "collision.json"
    card_path.write_text(json.dumps(card), encoding="utf-8")
    result = run_cli(card_path, db_path)
    assert result.returncode == 1
    probe = subprocess.run(
        ["grep", "-Fi", name], input=result.stdout + result.stderr,
        capture_output=True, text=True, timeout=5,
    )
    assert probe.returncode == 1


@pytest.mark.parametrize(("stored", "value"), [
    ("ｏｒａｌ", "oral"), ("ｃａｒｅ", "care"),
    ("o\u200bral", "oral"), ("ｏ\u200bｒａｌ", "oral"),
    ("Ｋａｒｉｎ", "Karin"), ("ｶﾅ", unicodedata.normalize("NFKC", "ｶﾅ")),
    ("東野", "東野"), ("SyntheticPatient", "Synthetic\u200bPatient"),
    ("oral", "ｏｒａｌ"), ("SyntheticPatient", "１２３"), ("SyntheticPatient", "一二三"),
])
def test_unicode_name_and_digit_variants_are_rejected(tmp_path, snapshot, stored, value):
    db_path, _ = snapshot
    insert_name(db_path, "patients.patient_name", stored)
    card = clean_card()
    card["current_shape"] = value
    card_path = tmp_path / "unicode.json"
    card_path.write_text(json.dumps(card), encoding="utf-8")
    result = run_cli(card_path, db_path)
    assert result.returncode == 1
    assert value not in result.stdout + result.stderr


@pytest.mark.parametrize(("name", "shapes"), [
    ("oralcare", ("oral", "care")),
    ("ｏｒａｌｃａｒｅ", ("oral", "care")),
    ("oralcare", ("care", "oral")),
    ("oralcarepast", (["oral", "care"], "past")),
])
def test_names_split_across_allowed_fields_are_rejected(tmp_path, snapshot, name, shapes):
    db_path, _ = snapshot
    insert_name(db_path, "patients.patient_name", name)
    card = clean_card()
    card["current_shape"], card["expected_shape"] = shapes
    card_path = tmp_path / "split.json"
    card_path.write_text(json.dumps(card), encoding="utf-8")
    result = run_cli(card_path, db_path)
    assert result.returncode == 1
    assert name not in result.stdout + result.stderr


def test_checkpointed_snapshot_has_no_new_files_or_db_changes(tmp_path, snapshot):
    db_path, _ = snapshot
    card_path = tmp_path / "card.json"
    card_path.write_text(json.dumps(clean_card()), encoding="utf-8")
    # Ledger leaves journal_mode=WAL, but its closed, checkpointed copy has no sidecars.
    assert not Path(str(db_path) + "-wal").exists()
    assert not Path(str(db_path) + "-shm").exists()
    before_files = set(tmp_path.iterdir())
    before_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
    result = run_cli(card_path, db_path)
    assert result.returncode == 0
    assert set(tmp_path.iterdir()) == before_files
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before_hash


@pytest.mark.parametrize("variant", ["live", "frozen_wal", "frozen_wal_shm"])
def test_wal_only_name_is_rejected_without_changing_original_files(tmp_path, variant):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    db_path, _ = synthetic_snapshot(source_dir)
    reader_temp = tmp_path / "reader-temp"
    reader_temp.mkdir()
    card = clean_card()
    card["current_shape"] = "oral"
    card_path = tmp_path / "card.json"
    card_path.write_text(json.dumps(card), encoding="utf-8")
    writer = sqlite3.connect(db_path)
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO patients(project_id,patient_name) VALUES(99,'oral')")
        writer.commit()
        assert writer.execute("SELECT patient_name FROM patients WHERE project_id=99").fetchone() == ("oral",)
        assert Path(str(db_path) + "-wal").stat().st_size > 0
        if variant != "live":
            frozen_dir = tmp_path / "frozen"
            frozen_dir.mkdir()
            frozen = frozen_dir / "snapshot.db"
            for suffix in ("", "-wal", "-shm") if variant == "frozen_wal_shm" else ("", "-wal"):
                shutil.copyfile(str(db_path) + suffix, str(frozen) + suffix)
            db_path, source_dir = frozen, frozen_dir
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in source_dir.iterdir()}
        result = run_cli(card_path, db_path, env={**os.environ, "TMPDIR": str(reader_temp)})
        assert result.returncode == 1
        assert "denylist:" in result.stdout
        assert "oral" not in result.stdout + result.stderr
        assert {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in source_dir.iterdir()} == before
        assert list(reader_temp.iterdir()) == []
    finally:
        writer.close()


def test_denylist_load_failure_never_echoes_a_name_and_removes_copies(tmp_path, snapshot):
    db_path, _ = snapshot
    insert_name(db_path, "patients.patient_name", "input")
    db = sqlite3.connect(db_path)
    try:
        with db:
            # A non-JSON artifact reads as no names; a harvest bound still fails the load.
            db.execute("INSERT INTO artifacts(kind,content) VALUES('station_staff_v1','{bad')")
            db.execute("CREATE TABLE future_identity_payload(payload TEXT)")
            db.execute("INSERT INTO future_identity_payload VALUES(?)",
                       ('{"name":' + "[" * 40 + '"x"' + "]" * 40 + "}",))
    finally:
        db.close()
    reader_temp = tmp_path / "reader-temp"
    reader_temp.mkdir()
    card_path = tmp_path / "card.json"
    card_path.write_text(json.dumps(clean_card()), encoding="utf-8")
    before_files = set(tmp_path.iterdir())
    before_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
    result = run_cli(card_path, db_path, env={**os.environ, "TMPDIR": str(reader_temp)})
    assert result.returncode == 1
    probe = subprocess.run(
        ["grep", "-Fi", "input"], input=result.stdout + result.stderr,
        capture_output=True, text=True, timeout=5,
    )
    assert probe.returncode == 1
    assert "Traceback" not in result.stdout + result.stderr
    assert list(reader_temp.iterdir()) == []
    assert set(tmp_path.iterdir()) == before_files
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before_hash


@pytest.mark.parametrize("failure", [
    sqlite3.OperationalError("GateSyntheticSensitive"),
    json.JSONDecodeError("GateSyntheticSensitive", "input", 0),
    OSError("GateSyntheticSensitive"),
])
def test_loading_exception_is_silent_and_private_directory_is_removed(
    tmp_path, snapshot, monkeypatch, capsys, failure,
):
    db_path, _ = snapshot
    insert_name(db_path, "patients.patient_name", "input")
    created = []
    mkdtemp = pattern_cards.tempfile.mkdtemp

    def private_dir(*, prefix):
        path = Path(mkdtemp(prefix=prefix, dir=tmp_path))
        created.append(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        return str(path)

    def fail_loading(_db):
        raise failure

    monkeypatch.setattr(pattern_cards.tempfile, "mkdtemp", private_dir)
    monkeypatch.setattr(pattern_cards, "_denylist", fail_loading)
    result = pattern_cards.main(["lint", str(tmp_path / "card.json"), "--db", str(db_path)])
    output = capsys.readouterr()
    assert result == 1
    assert output.out == output.err == ""
    assert len(created) == 1
    assert not created[0].exists()


def test_invalid_cli_argument_does_not_echo_argument_values(tmp_path, snapshot):
    db_path, _ = snapshot
    result = subprocess.run(
        [sys.executable, str(CLI), "GateSyntheticSensitive", "card.json", "--db", str(db_path)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 1
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("key", [
    "NAME", "legacy_name", "SenderIdentity", "organization_label", "ORG",
    "staff_label", "AuthorAlias", "DISPLAY_LABEL", "NICKNAME",
    "PatientLabel", "FamilyLabel", "KANA", "FURIGANA",
])
@pytest.mark.parametrize("location", ["column", "json"])
def test_new_table_name_fields_and_nested_json_are_harvested(tmp_path, snapshot, key, location):
    db_path, _ = snapshot
    db = sqlite3.connect(db_path)
    try:
        with db:
            # Quoted table identifiers and undeclared column types are legitimate SQLite.
            column = key if location == "column" else "future_payload"
            db.execute(f'CREATE TABLE "future ""identities""" ("{column}")')
            value = "oral" if location == "column" else json.dumps({
                "envelope": [{"future_layer": {key: ["oral"]}}],
            })
            db.execute(f'INSERT INTO "future ""identities""" ("{column}") VALUES(?)', (value,))
    finally:
        db.close()
    reader_temp = tmp_path / "reader-temp"
    reader_temp.mkdir()
    card = clean_card()
    card["current_shape"] = "oral"
    card_path = tmp_path / "card.json"
    card_path.write_text(json.dumps(card), encoding="utf-8")
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
              for path in tmp_path.iterdir() if path.is_file()}
    result = run_cli(card_path, db_path, env={**os.environ, "TMPDIR": str(reader_temp)})
    assert result.returncode == 1
    assert "denylist:" in result.stdout
    assert "oral" not in result.stdout + result.stderr
    assert {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in tmp_path.iterdir() if path.is_file()} == before
    assert list(reader_temp.iterdir()) == []


@pytest.mark.parametrize("column", ["sender_name", "organization"])
def test_semantic_bundle_preserves_historical_name_rejection(tmp_path, snapshot, column):
    from semantic_store import _member

    db_path, _ = snapshot
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        with db:
            original = db.execute(f"SELECT {column} FROM messages WHERE message_id=1").fetchone()[0]
            db.execute(f"UPDATE messages SET {column}='oral' WHERE message_id=1")
            member = _member(db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
            db.execute(
                "INSERT INTO artifacts(kind,project_id,message_id,content,meta) "
                "VALUES('semantic_bundle',1,1,?,'{}')",
                (json.dumps({"members": [member]}),),
            )
            db.execute(f"UPDATE messages SET {column}=? WHERE message_id=1", (original,))
            assert db.execute(f"SELECT {column} FROM messages WHERE message_id=1").fetchone()[0] != "oral"
            assert db.execute("SELECT sender_name FROM messages_fts WHERE rowid=1").fetchone()[0] != "oral"
    finally:
        db.close()
    card = clean_card()
    card["current_shape"] = "oral"
    card_path = tmp_path / "history.json"
    card_path.write_text(json.dumps(card), encoding="utf-8")
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    result = run_cli(card_path, db_path)
    assert result.returncode == 1
    assert "denylist:" in result.stdout
    assert "oral" not in result.stdout + result.stderr
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("variant", ["empty", "sentinel", "hot"])
def test_rollback_journal_presence_always_fails_closed(tmp_path, snapshot, variant):
    db_path, _ = snapshot
    journal = Path(str(db_path) + "-journal")
    if variant == "hot":
        db = sqlite3.connect(db_path)
        try:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("INSERT INTO patients(project_id,patient_name) VALUES(99,'oral')")
            db.executemany(
                "INSERT INTO patients(project_id,patient_name,disease) VALUES(?,?,?)",
                [(i, "GateSyntheticFiller", "x" * 500) for i in range(1000, 1500)],
            )
            db.commit()
        finally:
            db.close()
        crashed = subprocess.run(
            [sys.executable, "-c",
             "import os,sqlite3,sys;"
             "c=sqlite3.connect(sys.argv[1]);"
             "c.execute('PRAGMA cache_size=5');"
             "c.execute('PRAGMA cache_spill=ON');"
             "c.execute('PRAGMA synchronous=FULL');"
             "c.execute('BEGIN IMMEDIATE');"
             "c.execute(\"UPDATE patients SET patient_name='GateSyntheticReplacement' WHERE project_id=99\");"
             "c.execute('UPDATE patients SET disease=? WHERE project_id>=1000',('y'*500,));"
             "os._exit(0)", str(db_path)],
            capture_output=True, text=True, timeout=15,
        )
        assert crashed.returncode == 0
        assert journal.stat().st_size > 512
    else:
        journal.write_bytes(b"" if variant == "empty" else b"synthetic journal")
    reader_temp = tmp_path / "reader-temp"
    reader_temp.mkdir()
    card = clean_card()
    card["current_shape"] = "oral"
    card_path = tmp_path / "card.json"
    card_path.write_text(json.dumps(card), encoding="utf-8")
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
              for path in tmp_path.iterdir() if path.is_file()}
    result = run_cli(card_path, db_path, env={**os.environ, "TMPDIR": str(reader_temp)})
    assert result.returncode == 1
    assert "oral" not in result.stdout + result.stderr
    assert "GateSynthetic" not in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    assert {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in tmp_path.iterdir() if path.is_file()} == before
    assert list(reader_temp.iterdir()) == []


@pytest.mark.parametrize("payload", [
    '{"name":"' + "x" * (1024 * 1024) + '"}',
    '{"name":' + "[" * 40 + '"oral"' + "]" * 40 + "}",
    json.dumps({"items": [None] * 10001, "name": "oral"}),
], ids=["bytes", "depth", "nodes"])
def test_identity_harvest_limits_fail_closed_without_copy_leaks(tmp_path, snapshot, payload):
    db_path, _ = snapshot
    db = sqlite3.connect(db_path)
    try:
        with db:
            db.execute("CREATE TABLE future_identity_payload(payload TEXT)")
            db.execute("INSERT INTO future_identity_payload VALUES(?)", (payload,))
    finally:
        db.close()
    reader_temp = tmp_path / "reader-temp"
    reader_temp.mkdir()
    card_path = tmp_path / "card.json"
    card_path.write_text(json.dumps(clean_card()), encoding="utf-8")
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    result = run_cli(card_path, db_path, env={**os.environ, "TMPDIR": str(reader_temp)})
    assert result.returncode == 1
    assert result.stdout == result.stderr == ""
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    assert list(reader_temp.iterdir()) == []


def test_non_identity_json_strings_do_not_reject_an_allowed_enum(tmp_path, snapshot):
    db_path, _ = snapshot
    db = sqlite3.connect(db_path)
    try:
        with db:
            db.execute("CREATE TABLE future_metadata(payload TEXT)")
            db.execute("INSERT INTO future_metadata VALUES(?)",
                       (json.dumps({"envelope": [{"status": "oral"}]}),))
    finally:
        db.close()
    card = clean_card()
    card["current_shape"] = "oral"
    card_path = tmp_path / "card.json"
    card_path.write_text(json.dumps(card), encoding="utf-8")
    result = run_cli(card_path, db_path)
    assert result.returncode == 0


@pytest.mark.parametrize("kind", [
    "station_staff_v1", "karte_summary", "self_profile_v1", "patient_rollup", "loop_event"])
def test_non_json_rows_do_not_block_linting_and_valid_names_still_count(tmp_path, snapshot, kind):
    db_path, _ = snapshot
    db = sqlite3.connect(db_path)
    try:
        with db:
            for content, meta in (("not json", "{}"), ("{}", "not json"), ("[truncated", None)):
                db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,meta) "
                           "VALUES(?,1,1,?,?)", (kind, content, meta))
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            if "command_receipts" in tables:
                columns = [row[1] for row in db.execute("PRAGMA table_info(command_receipts)")]
                db.execute(f"INSERT INTO command_receipts({','.join(columns)}) "
                           f"VALUES({','.join('?' for _ in columns)})",
                           ["not json" if column == "receipt_json" else "applied" if column == "outcome" else f"synthetic-{column}"
                            for column in columns])
    finally:
        db.close()
    card_path = tmp_path / "card.json"
    card_path.write_text(json.dumps(clean_card()), encoding="utf-8")
    result = run_cli(card_path, db_path)
    assert result.returncode == 0 and result.stdout == result.stderr == ""
    db = sqlite3.connect(db_path)
    try:
        with db:
            db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,meta) "
                       "VALUES('feedback_v1',1,1,?,'{}')", (json.dumps({"actor": "oral"}),))
    finally:
        db.close()
    card = clean_card()
    card["current_shape"] = "oral"
    card_path.write_text(json.dumps(card), encoding="utf-8")
    result = run_cli(card_path, db_path)
    assert result.returncode == 1 and "oral" not in result.stdout + result.stderr


@pytest.mark.parametrize("kind,content", [
    ("station_staff_v1", {"staff": ["scalar staff member", 7, None, {"name": "GateSynthetic"}]}),
    ("patient_rollup", {"top_senders": ["scalar sender", 3, ["GateSynthetic", 2]]}),
])
def test_scalar_array_members_do_not_block_linting(tmp_path, snapshot, kind, content):
    db_path, _ = snapshot
    db = sqlite3.connect(db_path)
    try:
        with db:
            db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,meta) "
                       "VALUES(?,1,1,?,'{}')", (kind, json.dumps(content)))
    finally:
        db.close()
    card_path = tmp_path / "card.json"
    card_path.write_text(json.dumps(clean_card()), encoding="utf-8")
    result = run_cli(card_path, db_path)
    assert result.returncode == 0 and result.stdout == result.stderr == ""
    # the well-formed member beside the scalar ones still blocks its name
    card = clean_card()
    card["current_shape"] = "GateSynthetic"
    card_path.write_text(json.dumps(card), encoding="utf-8")
    result = run_cli(card_path, db_path)
    assert result.returncode == 1 and "GateSynthetic" not in result.stdout + result.stderr


def test_sender_type_is_a_role_and_same_word_as_a_real_name_still_blocks(tmp_path, snapshot):
    db_path, _ = snapshot
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE messages SET sender_type='patient'")
    path = tmp_path / "card.json"
    path.write_text(json.dumps(clean_card()))
    result = run_cli(path, db_path)
    assert result.returncode == 0 and result.stdout == result.stderr == ""
    insert_name(db_path, "patients.patient_name", "patient")
    result = run_cli(path, db_path)
    assert result.returncode == 1 and "patient" not in result.stdout + result.stderr


@pytest.mark.parametrize("kind,content", [
    ("extract_llm", {field: [{"name": "oral"}]} ) for field in ("meds", "labs")
] + [
    ("patient_rollup", {field: [{"name": "oral"}]}) for field in (
        "medications", "unverified_medications", "planned_medications", "recent_labs")
] + [
    ("extract_qc", {"items": [{"section": section, "item": {"name": "oral"}}]})
    for section in ("meds", "labs")
])
def test_only_known_clinical_name_positions_are_not_person_names(tmp_path, snapshot, kind, content):
    db_path, _ = snapshot
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO artifacts(kind,content) VALUES(?,?)", (kind, json.dumps(content)))
    path = tmp_path / "card.json"
    card = clean_card()
    card["current_shape"] = "oral"
    path.write_text(json.dumps(card))
    result = run_cli(path, db_path)
    assert result.returncode == 0 and result.stdout == result.stderr == ""
    insert_name(db_path, "patients.patient_name", "oral")
    result = run_cli(path, db_path)
    assert result.returncode == 1 and "oral" not in result.stdout + result.stderr


@pytest.mark.parametrize("kind,content", [
    ("extract_llm", {"contacts": [{"name": "oral"}]}),
    ("extract_llm", {"meds": [{"name": "合成薬", "actor": {"name": "oral"}}]}),
    ("extract_llm", {"meds": {"name": "oral"}}),
    ("extract_qc", {"items": [{"section": "requests", "item": {"name": "oral"}}]}),
    ("extract_qc", {"items": [{"item": {"name": "oral", "section": "meds"}}]}),
    ("patient_rollup", {"contacts": [{"name": "oral"}]}),
    ("unknown_synthetic_kind", {"meds": [{"name": "oral"}]}),
])
def test_names_elsewhere_in_clinical_or_unknown_documents_remain_protected(tmp_path, snapshot, kind, content):
    db_path, _ = snapshot
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO artifacts(kind,content) VALUES(?,?)", (kind, json.dumps(content)))
    path = tmp_path / "card.json"
    card = clean_card()
    card["current_shape"] = "oral"
    path.write_text(json.dumps(card))
    result = run_cli(path, db_path)
    assert result.returncode == 1 and "oral" not in result.stdout + result.stderr


@pytest.mark.parametrize("origin", ["short_person", "unknown_role_column", "clinical_metadata"])
def test_known_nonidentity_exceptions_do_not_spread_to_real_names_or_unknown_sources(tmp_path, snapshot, origin):
    db_path, _ = snapshot
    with sqlite3.connect(db_path) as db:
        if origin == "short_person":
            db.execute("UPDATE patients SET patient_name='p'")
        elif origin == "unknown_role_column":
            db.execute("CREATE TABLE future_identity(sender_type TEXT)")
            db.execute("INSERT INTO future_identity VALUES('patient')")
        else:
            db.execute("INSERT INTO artifacts(kind,meta) VALUES('extract_llm',?)",
                       (json.dumps({"labs": [{"name": "oral"}]}),))
    card = clean_card()
    card["current_shape"] = "oral"
    path = tmp_path / "card.json"
    path.write_text(json.dumps(card))
    result = run_cli(path, db_path)
    assert result.returncode == 1
    name = {"short_person": "p", "unknown_role_column": "patient", "clinical_metadata": "oral"}[origin]
    assert name not in result.stdout + result.stderr
