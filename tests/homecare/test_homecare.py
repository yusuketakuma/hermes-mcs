"""Synthetic-only home-care core: modules, migrations, validation, versions, care team, links, audit and writes."""
import ast
from email.message import Message
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import time
import urllib.request
from urllib.parse import parse_qs, urlsplit

import pytest

import homecare_api
import homecare_catalog
import homecare_links as links
import homecare_modules
import homecare_records as records
import homecare_route as route
import homecare_store
import homecare_team as team
import homecare_visits as visits
import pharmacy_prescriptions as prescriptions
import mcs_local_chat as chat

REGISTRY = homecare_catalog.registry(["pharmacy"])


@pytest.fixture
def db(tmp_path):
    connection = homecare_store.connect(tmp_path / "homecare.db", REGISTRY.modules)
    yield connection
    connection.close()


def first_staff(db, **values):
    return records.create(db, "staff", {"name": "合成 薬剤師", "qualifications": ["pharmacy.pharmacist"], **values},
                          actor=None, registry=REGISTRY)


def staff(db, actor, name="合成 事務", codes=("core.clerk",), **values):
    return records.create(db, "staff", {"name": name, "qualifications": list(codes), **values},
                          actor=actor, registry=REGISTRY)


def patient(db, actor, **values):
    return records.create(db, "patient", {"name": "合成 花子", "kana": "ゴウセイ ハナコ", "phone": "000-0000-0000", **values},
                          actor=actor, registry=REGISTRY)


def test_modules_declare_only_their_own_namespace_and_unknown_modules_are_refused():
    with pytest.raises(ValueError, match="namespace"):
        homecare_modules.Module("nursing", "訪問看護", qualifications={"pharmacy.pharmacist": "薬剤師"})
    with pytest.raises(ValueError, match="unknown_module"):
        homecare_catalog.registry(["pharmacy", "unknown"])
    assert [module.name for module in REGISTRY.modules] == ["core", "pharmacy"]
    assert REGISTRY.describe()["roles"]["pharmacy.primary_pharmacist"]["requires"] == ["pharmacy.pharmacist"]


def test_modules_import_only_the_core():
    """The catalog is the one place that meets the modules; the core never imports one."""
    base = Path(homecare_catalog.__file__).parent
    modules = {path.stem for path in base.glob("*/*.py")}
    core = {path.stem for path in (base / "core").glob("*.py")}
    for path in base.glob("*/*.py"):
        tree = ast.parse(path.read_text())
        names = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names] + [
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module]
        imported = {name.split(".")[0] for name in names} & modules  # function-level imports included
        owner = path.parent.name
        allowed = core if owner == "core" else core | {p.stem for p in path.parent.glob("*.py")}
        assert imported <= allowed, (path.name, imported - allowed)


def test_database_is_private_migrated_per_module_and_reopenable(tmp_path):
    path = tmp_path / "private" / "homecare.db"
    homecare_store.connect(path, REGISTRY.modules).close()
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    again = homecare_store.connect(path, REGISTRY.modules)
    assert [tuple(row) for row in again.execute("SELECT module, number FROM schema_migrations")] == [
        ("core", 1), ("core", 2), ("core", 3), ("pharmacy", 1)]
    again.close()


def test_opening_a_migrated_ledger_does_not_wait_for_a_writer(tmp_path):
    path = tmp_path / "homecare.db"
    writer = homecare_store.connect(path, REGISTRY.modules)
    writer.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        homecare_store.connect(path, REGISTRY.modules).close()
        assert time.monotonic() - started < 5
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_applied_migration_must_not_change_and_newer_database_is_refused(tmp_path):
    path = tmp_path / "homecare.db"
    homecare_store.connect(path, REGISTRY.modules).close()
    edited = tmp_path / "edited"
    edited.mkdir()
    for number, name, sql in homecare_store.migrations(homecare_modules.CORE.migrations):
        (edited / name).write_text(sql + ("\n-- edited\n" if number == 1 else ""))
    with pytest.raises(RuntimeError, match="changed"):
        homecare_store.connect(path, [homecare_modules.Module("core", "共通", edited)])
    with pytest.raises(RuntimeError, match="newer"):
        homecare_store.connect(path, [homecare_modules.Module("core", "共通", tmp_path / "none")])


def test_migrations_must_be_numbered_without_gaps(tmp_path):
    for name in ("0001_a.sql", "0003_c.sql"):
        (tmp_path / name).write_text("SELECT 1;\n")
    with pytest.raises(RuntimeError, match="gaps"):
        homecare_store.migrations(tmp_path)


def test_only_the_first_staff_member_needs_no_actor_and_becomes_administrator(db):
    me = first_staff(db, tailscale_login="Synthetic@Example.invalid")
    assert me["tailscale_login"] == "synthetic@example.invalid" and me["admin"] == 1
    assert me["qualifications"] == ["pharmacy.pharmacist"]
    with pytest.raises(ValueError, match="actor_required"):
        staff(db, None)
    clerk = staff(db, me["staff_id"])
    assert clerk["admin"] == 0
    with pytest.raises(ValueError, match="admin_required"):
        staff(db, clerk["staff_id"], name="合成 三人目")
    with pytest.raises(ValueError, match="actor_required"):
        patient(db, None)
    patient(db, clerk["staff_id"])
    events = db.execute("SELECT actor_staff_id, kind FROM audit_events ORDER BY event_id").fetchall()
    assert [tuple(row) for row in events] == [(me["staff_id"], "staff.create"), (me["staff_id"], "staff.create"),
                                             (clerk["staff_id"], "patient.create")]


@pytest.mark.parametrize("codes", [[], ["nursing.nurse"], ["core.clerk", "core.clerk"], "core.clerk", [1]])
def test_staff_need_known_distinct_qualifications(db, codes):
    with pytest.raises(records.InvalidFields) as error:
        first_staff(db, qualifications=codes)
    assert error.value.fields == ["qualifications"]


def test_qualifications_of_a_disabled_module_survive_an_update(tmp_path):
    path = tmp_path / "homecare.db"
    db = homecare_store.connect(path, REGISTRY.modules)
    me = first_staff(db)
    core_only = homecare_modules.Registry([])
    after = records.update(db, "staff", me["staff_id"], {"kana": "ゴウセイ"}, expected_version=1,
                           actor=me["staff_id"], registry=core_only)
    assert after["qualifications"] == ["pharmacy.pharmacist"]
    db.close()


@pytest.mark.parametrize("values, fields", [
    ({"name": " "}, ["name"]),
    ({"phone": "abc"}, ["phone"]),
    ({"postal_code": "1234"}, ["postal_code"]),
    ({"birth_date": "2026/01/01"}, ["birth_date"]),
    ({"birth_date": "2026-02-31"}, ["birth_date"]),
    ({"preferred_contact": "keyperson"}, ["keyperson_name", "keyperson_phone", "keyperson_relation"]),
    ({"preferred_contact": "facility"}, ["contact_phone"]),
    ({"phone": "", "preferred_contact": "patient"}, ["phone"]),
    ({"phone": "-", "preferred_contact": "patient"}, ["phone"]),
    ({"status": "deleted"}, ["status"]),
    ({"primary_pharmacist_id": 1}, ["values"]),  # an unknown key is never echoed
    ({"address": "あ" * 201}, ["address"])])
def test_patient_fields_are_validated_by_name(db, values, fields):
    me = first_staff(db)
    with pytest.raises(records.InvalidFields) as error:
        patient(db, me["staff_id"], **values)
    assert error.value.fields == fields
    assert db.execute("SELECT count(*) FROM patients").fetchone()[0] == 0


def test_dates_must_name_a_real_day():
    assert records.calendar_date("2028-02-29") and not records.calendar_date("2026-02-29")
    assert not any(map(records.calendar_date, ("2026-04-31", "20260101", "2026-1-01", None, 20260101)))


def test_update_checks_version_merges_fields_and_audits(db):
    me = first_staff(db)
    row = patient(db, me["staff_id"])
    after = records.update(db, "patient", row["patient_id"], {"caution_note": "合成の注意"},
                           expected_version=1, actor=me["staff_id"], registry=REGISTRY)
    assert after["version"] == 2 and after["phone"] == row["phone"] and after["caution_note"] == "合成の注意"
    with pytest.raises(homecare_store.Conflict, match="version_conflict"):
        records.update(db, "patient", row["patient_id"], {"caution_note": "古い入力"}, expected_version=1,
                       actor=me["staff_id"], registry=REGISTRY)
    stored = db.execute("SELECT before_json, after_json FROM audit_events WHERE kind='patient.update'").fetchall()
    assert len(stored) == 1 and json.loads(stored[0]["before_json"])["version"] == 1
    assert records.get(db, "patient", row["patient_id"])["caution_note"] == "合成の注意"


def test_a_handover_seen_on_an_old_screen_is_refused_even_when_the_same_person_holds_the_role_again(db):
    me = first_staff(db)
    other = staff(db, me["staff_id"], name="合成 薬剤師二", codes=("pharmacy.pharmacist",))
    third = staff(db, me["staff_id"], name="合成 薬剤師三", codes=("pharmacy.pharmacist",))
    row, role = patient(db, me["staff_id"]), "pharmacy.primary_pharmacist"
    first = team.assign(db, REGISTRY, row["patient_id"], me["staff_id"], role, expected_assignments=[],
                        actor=me["staff_id"])
    second = team.assign(db, REGISTRY, row["patient_id"], other["staff_id"], role, expected_assignments=[first],
                         actor=me["staff_id"])
    again = team.assign(db, REGISTRY, row["patient_id"], me["staff_id"], role, expected_assignments=[second],
                        actor=me["staff_id"])
    with pytest.raises(homecare_store.Conflict, match="team_changed"):  # the old screen still shows the first A
        team.assign(db, REGISTRY, row["patient_id"], third["staff_id"], role, expected_assignments=[first],
                    actor=me["staff_id"])
    assert [member["assignment_id"] for member in team.current(db, row["patient_id"])] == [again]


@pytest.mark.parametrize("change", [{"active": False}, {"admin": False}, {"tailscale_login": "two@example.invalid"},
                                    {"tailscale_login": None}])
def test_staff_cannot_lock_themselves_out_and_logins_are_unique(db, change):
    me = first_staff(db, tailscale_login="one@example.invalid")
    with pytest.raises(records.InvalidFields) as error:
        records.update(db, "staff", me["staff_id"], change, expected_version=1, actor=me["staff_id"], registry=REGISTRY)
    assert error.value.fields == list(change)
    with pytest.raises(records.InvalidFields) as error:
        staff(db, me["staff_id"], tailscale_login="ONE@example.invalid")
    assert error.value.fields == ["tailscale_login"]


def test_a_lone_administrator_adds_a_first_login_then_reaches_resident_mode(db):
    me = first_staff(db)
    assert me["tailscale_login"] is None
    saved = records.update(db, "staff", me["staff_id"], {"tailscale_login": "Admin@example.invalid"},
                           expected_version=1, actor=me["staff_id"], registry=REGISTRY)
    assert saved["tailscale_login"] == "admin@example.invalid"
    assert records.staff_for_login(db, "admin@example.invalid")["staff_id"] == me["staff_id"]


def test_care_team_roles_need_the_declared_qualification_and_hand_over(db):
    me = first_staff(db)
    other = staff(db, me["staff_id"], name="合成 薬剤師二", codes=("pharmacy.pharmacist",))
    clerk = staff(db, me["staff_id"])
    row = patient(db, me["staff_id"])
    role = "pharmacy.primary_pharmacist"
    with pytest.raises(ValueError, match="not_qualified"):
        team.assign(db, REGISTRY, row["patient_id"], clerk["staff_id"], role, expected_assignments=[], actor=me["staff_id"])
    with pytest.raises(ValueError, match="role_not_allowed"):
        team.assign(db, REGISTRY, row["patient_id"], me["staff_id"], "nursing.primary_nurse", expected_assignments=[],
                    actor=me["staff_id"])
    team.assign(db, REGISTRY, row["patient_id"], me["staff_id"], role, expected_assignments=[], actor=me["staff_id"])
    with pytest.raises(homecare_store.Conflict, match="already_assigned"):
        team.assign(db, REGISTRY, row["patient_id"], me["staff_id"], role, expected_assignments=[], actor=me["staff_id"])
    with pytest.raises(homecare_store.Conflict, match="team_changed"):
        # The client still shows the role as empty, so it must not silently end the current holder.
        team.assign(db, REGISTRY, row["patient_id"], other["staff_id"], role, expected_assignments=[], actor=clerk["staff_id"])
    mine = [member["assignment_id"] for member in team.current(db, row["patient_id"])]
    team.assign(db, REGISTRY, row["patient_id"], other["staff_id"], role, expected_assignments=mine,
                actor=clerk["staff_id"])
    assert [member["staff_id"] for member in team.current(db, row["patient_id"])] == [other["staff_id"]]
    ended = db.execute("SELECT ended_by FROM care_team WHERE staff_id=?", (me["staff_id"],)).fetchone()
    assert ended["ended_by"] == clerk["staff_id"]
    team.end(db, REGISTRY, team.current(db, row["patient_id"])[0]["assignment_id"], actor=me["staff_id"])
    assert team.current(db, row["patient_id"]) == []
    kinds = [r["kind"] for r in db.execute("SELECT kind FROM audit_events WHERE kind LIKE 'care_team.%'")]
    assert kinds == ["care_team.assign", "care_team.end", "care_team.assign", "care_team.end"]


@pytest.mark.parametrize("change", [{"active": False}, {"qualifications": ["core.clerk"]}])
def test_staff_holding_a_role_must_hand_it_over_before_leaving_or_losing_its_qualification(db, change):
    me = first_staff(db)
    other = staff(db, me["staff_id"], name="合成 薬剤師二", codes=("pharmacy.pharmacist",))
    row = patient(db, me["staff_id"])
    team.assign(db, REGISTRY, row["patient_id"], other["staff_id"], "pharmacy.primary_pharmacist",
                expected_assignments=[], actor=me["staff_id"])
    with pytest.raises(ValueError, match="still_assigned"):
        records.update(db, "staff", other["staff_id"], change, expected_version=1, actor=me["staff_id"],
                       registry=REGISTRY)
    assert records.get(db, "staff", other["staff_id"])["version"] == 1
    team.end(db, REGISTRY, team.current(db, row["patient_id"])[0]["assignment_id"], actor=me["staff_id"])
    records.update(db, "staff", other["staff_id"], change, expected_version=1, actor=me["staff_id"],
                   registry=REGISTRY)


def test_an_existing_file_that_is_not_a_homecare_database_is_left_untouched(tmp_path):
    snapshot = tmp_path / "snapshot.db"
    with sqlite3.connect(snapshot) as other:
        other.execute("CREATE TABLE patients(project_id INTEGER)")
    other.close()
    before = snapshot.read_bytes()
    alias = tmp_path / "alias.db"
    alias.symlink_to(snapshot)
    for path in (snapshot, alias):
        with pytest.raises(RuntimeError, match="not a homecare database"):
            homecare_store.connect(path, REGISTRY.modules)
    assert snapshot.read_bytes() == before and not (tmp_path / "snapshot.db-wal").exists()
    # Another tool's migration table, and one shaped like ours but without the core's first migration.
    for name, tables in (("other", ["CREATE TABLE schema_migrations(version TEXT)"]),
                         ("shaped", ["CREATE TABLE schema_migrations(module TEXT, number INTEGER, name TEXT, "
                                     "sha256 TEXT, applied_at REAL, PRIMARY KEY(module, number))",
                                     "CREATE TABLE orders(order_id INTEGER)"])):
        lookalike = tmp_path / f"{name}.db"
        with sqlite3.connect(lookalike) as other:
            for statement in tables:
                other.execute(statement)
        other.close()
        before = lookalike.read_bytes()
        with pytest.raises(RuntimeError, match="not a homecare database"):
            homecare_store.connect(lookalike, REGISTRY.modules)
        assert lookalike.read_bytes() == before
        with sqlite3.connect(f"{lookalike.as_uri()}?mode=ro", uri=True) as other:
            assert other.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        other.close()


def test_links_are_one_to_one_append_only_and_suggested_only_on_exact_name(db):
    me = first_staff(db)
    one, two = patient(db, me["staff_id"]), patient(db, me["staff_id"], name="合成 太郎")
    mcs = [{"project_id": 11, "patient_name": "合成　花子"}, {"project_id": 12, "patient_name": "合成 花"}]
    assert [row["project_id"] for row in links.candidates(db, one["patient_id"], mcs)] == [11]
    links.link(db, one["patient_id"], 11, actor=me["staff_id"])
    assert links.project_of(db, one["patient_id"]) == 11 and links.patient_of(db, 11) == one["patient_id"]
    with pytest.raises(homecare_store.Conflict, match="already_linked"):
        links.link(db, two["patient_id"], 11, actor=me["staff_id"])
    with pytest.raises(homecare_store.Conflict, match="already_linked"):
        links.link(db, one["patient_id"], 12, actor=me["staff_id"])
    with pytest.raises(homecare_store.Conflict, match="not_linked"):
        links.unlink(db, two["patient_id"], 11, actor=me["staff_id"])
    links.unlink(db, one["patient_id"], 11, actor=me["staff_id"], reason="合成の取り違え")
    assert links.project_of(db, one["patient_id"]) is None
    links.link(db, two["patient_id"], 11, actor=me["staff_id"])
    assert [row["action"] for row in links.history(db, one["patient_id"])] == ["unlink", "link"]  # newest first
    for statement in ("UPDATE patient_mcs_links SET project_id=99", "DELETE FROM patient_mcs_links",
                      "UPDATE audit_events SET kind='x'", "DELETE FROM audit_events"):
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            db.execute(statement)


def test_api_scopes_links_and_returns_browser_safe_errors(db):
    me = first_staff(db)
    row = patient(db, me["staff_id"])
    scope = lambda: [{"project_id": 11, "patient_name": "合成 花子"}]  # noqa: E731
    for project, allowed in ((99, {11}), (12, None)):  # outside the launch scope; not in the snapshot
        with pytest.raises(ValueError, match="project_not_allowed"):
            homecare_api.write(db, "link", {"patient": row["patient_id"], "project": project, "actor": me["staff_id"]},
                               registry=REGISTRY, allowed=allowed, mcs_patients=scope)
    homecare_api.write(db, "link", {"patient": row["patient_id"], "project": 11, "actor": me["staff_id"]},
                       registry=REGISTRY, allowed={11}, mcs_patients=scope)
    homecare_api.write(db, "team.assign", {"patient": row["patient_id"], "staff": me["staff_id"],
                                           "role": "pharmacy.primary_pharmacist", "assignments": [],
                                           "actor": me["staff_id"]},
                       registry=REGISTRY, allowed={11}, mcs_patients=scope)
    assert homecare_api.read(db, "linked", {"project": "11"}, registry=REGISTRY,
                             mcs_patients=scope)["patient"]["patient_id"] == row["patient_id"]
    with pytest.raises(ValueError, match="project_not_allowed"):
        homecare_api.read(db, "linked", {"project": "11"}, registry=REGISTRY, mcs_patients=lambda: [])
    read = homecare_api.read(db, "patient", {"patient": str(row["patient_id"])}, registry=REGISTRY, mcs_patients=scope)
    assert read["mcs"] == {"project_id": 11, "patient_name": "合成 花子"} and read["candidates"] == []
    assert [member["role"] for member in read["team"]] == ["pharmacy.primary_pharmacist"]
    outside = homecare_api.read(db, "patient", {"patient": str(row["patient_id"])}, registry=REGISTRY,
                                mcs_patients=lambda: [])
    assert outside["mcs"] == {"project_id": 11, "patient_name": None}
    assert homecare_api.read(db, "patients", {"q": "ハナコ"}, registry=REGISTRY,
                             mcs_patients=scope)["items"][0]["project_id"] == 11
    assert homecare_api.read(db, "meta", {}, registry=REGISTRY, mcs_patients=scope) == REGISTRY.describe()
    assert homecare_api.error_body(records.InvalidFields(["phone"])) == {"error": "invalid_fields", "fields": ["phone"]}
    assert homecare_api.error_body(ValueError("/private/path")) == {"error": "bad_request"}
    # A key the form does not know is reported as the whole input, so request text is never echoed.
    with pytest.raises(records.InvalidFields) as error:
        records.create(db, "patient", {"name": "合成", "phone": "000-0000-0000", "合成の秘密": 1}, actor=me["staff_id"], registry=REGISTRY)
    assert error.value.fields == ["values"]
    assert not db.in_transaction  # each read is one snapshot that ends with the read
    for bad in ("0", "-1", "1.5", True, None):
        with pytest.raises(ValueError, match="bad_request"):
            homecare_api.read(db, "patient", {"patient": bad}, registry=REGISTRY, mcs_patients=scope)


def post(server, body, headers=None, op="staff.create"):
    obj = chat.ChatHandler.__new__(chat.ChatHandler)
    obj.server = server
    obj.path = "/synthetic-capability/api/hc/" + op
    obj.headers = Message()
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    for name, value in (headers if headers is not None else without(None, length=len(data))):
        obj.headers[name] = value
    obj.rfile = BytesIO(data)
    results = []
    obj._send = lambda *args: results.append(args)
    obj.do_POST()
    return results[0]


@pytest.fixture
def server(tmp_path):
    path = tmp_path / "homecare.db"
    homecare_store.connect(path, REGISTRY.modules).close()
    return SimpleNamespace(server_port=8765, token="synthetic-capability", csrf="synthetic-csrf", snapshot="unused",
                           allowed={1}, homecare=path, homecare_registry=REGISTRY, route_settings=None, tailnet_host=None)


BODY = {"values": {"name": "合成 薬剤師", "qualifications": ["pharmacy.pharmacist"]}, "actor": None}


def without(name, replacement=None, length=None):
    rows = [("Host", "127.0.0.1:8765"), ("Origin", "http://127.0.0.1:8765"), ("Sec-Fetch-Site", "same-origin"),
            ("Content-Type", "application/json"), ("Content-Length", str(length or len(json.dumps(BODY).encode()))),
            ("X-MCS-CSRF", "synthetic-csrf")]
    return [(key, replacement if key == name else value) for key, value in rows
            if key != name or replacement is not None]


@pytest.mark.parametrize("headers, status", [
    (without("Origin"), 403), (without("Origin", "http://evil.invalid"), 403),
    (without("Sec-Fetch-Site", "same-site"), 403), (without("Sec-Fetch-Site", "none"), 403),
    (without("X-MCS-CSRF"), 403), (without("X-MCS-CSRF", "guess"), 403),
    (without("Content-Type", "text/plain"), 403), (without("Content-Length"), 411)])
def test_writes_need_same_origin_csrf_json_and_length(server, headers, status):
    assert post(server, BODY, headers)[0] == status
    assert homecare_store.connect(server.homecare, REGISTRY.modules).execute(
        "SELECT count(*) FROM staff").fetchone()[0] == 0


def test_write_route_saves_and_maps_errors(server):
    status, data, _mime = post(server, BODY)
    assert status == 200 and json.loads(data)["record"]["name"] == "合成 薬剤師"
    status, data, _mime = post(server, {"values": {"name": "", "qualifications": ["core.clerk"]}, "actor": 1})
    assert status == 400 and json.loads(data) == {"error": "invalid_fields", "fields": ["name"]}
    assert post(server, b"{not json")[0] == 400
    assert post(server, b"x" * (chat.MAX_BODY + 1), without("Content-Length", str(chat.MAX_BODY + 1)))[0] == 413


def test_write_without_homecare_database_is_refused(server):
    server.homecare = None
    status, data, _mime = post(server, BODY)
    assert status == 400 and json.loads(data) == {"error": "bad_request"}


TAILNET = "synthetic-mac.example-tail.ts.net"


def resident_request(server, method, path, body=None, login="nurse@example.invalid", headers=None):
    obj = chat.ChatHandler.__new__(chat.ChatHandler)
    obj.server, obj.path, obj.headers = server, path, Message()
    data = b"" if body is None else json.dumps(body).encode()
    rows = headers if headers is not None else [
        ("Host", TAILNET), ("Origin", "https://" + TAILNET), ("Sec-Fetch-Site", "same-origin"),
        ("Content-Type", "application/json"), ("Content-Length", str(len(data))), ("X-MCS-CSRF", "synthetic-csrf")]
    for name, value in rows + ([(chat.IDENTITY, login)] if login else []):
        obj.headers[name] = value
    obj.rfile = BytesIO(data)
    results = []
    obj._send = lambda *args: results.append(args)
    obj.do_POST() if method == "POST" else obj.do_GET()
    return results[0][0], (results[0][1:] or (b"",))[0]


def test_resident_mode_knows_each_request_by_its_tailscale_login(server):
    server.tailnet_host = TAILNET
    assert resident_request(server, "GET", "/", login=None)[0] == 403  # no login: tailscale serve was bypassed
    status, data = resident_request(server, "GET", "/api/session")
    assert status == 200 and json.loads(data)["user"] is None and json.loads(data)["resident"] is True
    assert resident_request(server, "GET", "/api/patients")[1] == chat.UNREGISTERED  # no MCS record before staff
    # Before the first registration the register reads as empty and only registering answers.
    assert json.loads(resident_request(server, "GET", "/api/hc/staff")[1]) == {"items": []}
    assert resident_request(server, "GET", "/api/hc/patients")[1] == chat.UNREGISTERED
    # The first registration takes the requester's own login, whatever the page sent.
    status, data = resident_request(server, "POST", "/api/hc/staff.create", {
        "values": {"name": "合成 薬剤師", "qualifications": ["pharmacy.pharmacist"], "tailscale_login": "other@x"},
        "actor": 999})
    first = json.loads(data)["record"]
    assert status == 200 and first["tailscale_login"] == "nurse@example.invalid" and first["admin"] == 1
    session = json.loads(resident_request(server, "GET", "/api/session", login="NURSE@example.invalid")[1])
    assert session["user"] == {"staff_id": first["staff_id"], "name": "合成 薬剤師"}
    stranger = resident_request(server, "GET", "/", login="stranger@example.invalid")
    assert stranger == (403, chat.UNREGISTERED)
    assert resident_request(server, "POST", "/api/hc/staff.create", BODY, login="stranger@example.invalid")[0] == 403
    # The actor is the login's staff member, not the one the body names.
    status, data = resident_request(server, "POST", "/api/hc/patient.create", {
        "values": {"name": "合成 花子", "phone": "000-0000-0000"}, "actor": 12345})
    assert status == 200, data
    db = homecare_store.connect(server.homecare, REGISTRY.modules)
    assert db.execute("SELECT actor_staff_id FROM audit_events WHERE kind='patient.create'").fetchone()[0] == first["staff_id"]
    db.close()


@pytest.mark.parametrize("headers, path, status", [
    ([("Host", "evil.invalid")], "/", 403),
    ([("Host", TAILNET), ("Host", TAILNET)], "/", 403),
    ([("Host", TAILNET), ("Origin", "http://127.0.0.1:8765")], "/", 403),
    ([("Host", TAILNET), (chat.IDENTITY, "nurse@example.invalid")], "/", 403),  # two logins on one request
    ([("Host", TAILNET)], "/synthetic-capability/", 403),  # no capability URL here; nothing for a non-staff login
    ([("Host", "127.0.0.1:8765")], "/", 200)])
def test_resident_mode_checks_host_origin_and_one_login(server, headers, path, status):
    server.tailnet_host = TAILNET
    assert resident_request(server, "GET", path, headers=headers)[0] == status


@pytest.mark.parametrize("kwargs", [{"homecare": None, "port": 8765}, {"port": 0}, {"port": 8765, "host": "mac.local"}])
def test_resident_mode_needs_a_ledger_a_fixed_port_and_a_tailnet_name(tmp_path, kwargs):
    with pytest.raises(ValueError, match="resident mode"):
        chat.ChatServer("unused", None, kwargs["port"], homecare=kwargs.get("homecare", tmp_path / "homecare.db"),
                        tailnet_host=kwargs.get("host", TAILNET))


def test_register_script_is_served_and_never_builds_html():
    assert chat.FILES["homecare.js"][0] == "homecare.js"
    js = (chat.ASSETS / "homecare.js").read_text()
    assert "innerHTML" not in js and "localStorage" not in js and "sessionStorage" not in js
    assert '"X-MCS-CSRF"' in js and 'credentials: "omit"' in js
    assert "pharmacist" not in js, "the core screen must read occupation codes from api/hc/meta"
    module_js = (chat.ASSETS / chat.FILES["pharmacy.js"][0]).read_text()
    assert "innerHTML" not in module_js and "fetch(" not in module_js, "module screens post through the core's hcPost"
    assert "pharmacy.js" in (chat.ASSETS / "index.html").read_text()


def visit_values(patient_id, **values):
    return {"patient_id": patient_id, "kind": "pharmacy.home_visit", "visit_date": "2026-11-24", **values}


def test_visits_are_validated_versioned_and_audited(db):
    me = first_staff(db)
    row = patient(db, me["staff_id"])
    for bad, fields in (({"kind": "nursing.visit"}, ["kind"]), ({"visit_date": "2026-13-01"}, ["visit_date"]),
                        ({"visit_date": "2026-02-30"}, ["visit_date"]),
                        ({"start_time": "25:00"}, ["start_time"]), ({"status": "done"}, ["staff_id"]),
                        ({"patient_id": 999}, ["patient_id"]), ({"extra": 1}, ["values"]),
                        ({"kind": None}, ["kind"]), ({"kind": ["pharmacy.home_visit"]}, ["kind"]),
                        ({"kind": {"pharmacy.home_visit": 1}}, ["kind"])):
        with pytest.raises(records.InvalidFields) as error:
            visits.create(db, REGISTRY, visit_values(row["patient_id"]) | bad, actor=me["staff_id"])
        assert error.value.fields == fields
    later = visits.create(db, REGISTRY, visit_values(row["patient_id"], start_time="14:00"), actor=me["staff_id"])
    early = visits.create(db, REGISTRY, visit_values(row["patient_id"], start_time="09:30", staff_id=me["staff_id"]),
                          actor=me["staff_id"])
    assert [item["visit_id"] for item in visits.for_day(db, "2026-11-24")] == [early["visit_id"], later["visit_id"]]
    done = visits.update(db, REGISTRY, early["visit_id"], {"status": "done", "note": "合成の記録"},
                         expected_version=1, actor=me["staff_id"])
    assert done["status"] == "done" and done["version"] == 2 and done["staff_name"] == "合成 薬剤師"
    with pytest.raises(homecare_store.Conflict):
        visits.update(db, REGISTRY, early["visit_id"], {"note": "x"}, expected_version=1, actor=me["staff_id"])
    # A kind whose module was since disabled stays valid while it is left unchanged.
    kept = visits.update(db, homecare_modules.Registry([]), later["visit_id"], {"note": "合成"},
                         expected_version=1, actor=me["staff_id"])
    assert kept["kind"] == "pharmacy.home_visit"
    with pytest.raises(records.InvalidFields):
        visits.update(db, REGISTRY, early["visit_id"], {"patient_id": 2}, expected_version=2, actor=me["staff_id"])
    with pytest.raises(ValueError, match="actor_required"):
        visits.create(db, REGISTRY, visit_values(row["patient_id"]), actor=None)
    kinds = [r["kind"] for r in db.execute("SELECT kind FROM audit_events WHERE kind LIKE 'visit.%'")]
    assert kinds == ["visit.create", "visit.create", "visit.update", "visit.update"]


def test_a_done_visit_by_staff_who_since_left_can_still_be_corrected(db):
    me = first_staff(db)
    left = staff(db, me["staff_id"], name="合成 退職者", codes=("pharmacy.pharmacist",))
    row = patient(db, me["staff_id"])
    made = visits.create(db, REGISTRY, visit_values(row["patient_id"], staff_id=left["staff_id"], status="done"),
                         actor=me["staff_id"])
    records.update(db, "staff", left["staff_id"], {"active": False}, expected_version=1, actor=me["staff_id"],
                   registry=REGISTRY)
    fixed = visits.update(db, REGISTRY, made["visit_id"], {"note": "合成の訂正"}, expected_version=1,
                          actor=me["staff_id"])
    assert fixed["staff_id"] == left["staff_id"] and fixed["note"] == "合成の訂正"
    other = visits.create(db, REGISTRY, visit_values(row["patient_id"]), actor=me["staff_id"])
    with pytest.raises(records.InvalidFields) as error:  # a new choice of staff must still be active
        visits.update(db, REGISTRY, other["visit_id"], {"staff_id": left["staff_id"]}, expected_version=1,
                      actor=me["staff_id"])
    assert error.value.fields == ["staff_id"]


def test_only_pharmacists_record_a_home_visit_as_done_while_clerks_plan_deliver_and_enter_prescriptions(db):
    me = first_staff(db)
    clerk = staff(db, me["staff_id"])
    row = patient(db, me["staff_id"])
    planned = visits.create(db, REGISTRY, visit_values(row["patient_id"], staff_id=me["staff_id"]), actor=clerk["staff_id"])
    for actor, staff_id in ((clerk, me), (me, clerk)):  # the writer and the visitor both need the qualification
        with pytest.raises(ValueError, match="visit_not_qualified"):
            visits.update(db, REGISTRY, planned["visit_id"], {"status": "done", "staff_id": staff_id["staff_id"]},
                          expected_version=1, actor=actor["staff_id"])
    done = visits.update(db, REGISTRY, planned["visit_id"], {"status": "done"}, expected_version=1, actor=me["staff_id"])
    with pytest.raises(ValueError, match="visit_not_qualified"):  # a clerk does not rewrite the clinical record
        visits.update(db, REGISTRY, done["visit_id"], {"note": "合成"}, expected_version=2, actor=clerk["staff_id"])
    with pytest.raises(ValueError, match="visit_not_qualified"):  # nor cancels a visit that took place
        visits.update(db, REGISTRY, done["visit_id"], {"status": "cancelled"}, expected_version=2,
                      actor=clerk["staff_id"])
    delivered = visits.create(db, REGISTRY, visit_values(row["patient_id"], kind="pharmacy.delivery", status="done",
                                                         staff_id=clerk["staff_id"]), actor=clerk["staff_id"])
    assert delivered["status"] == "done"
    rx = prescriptions.create(db, {"patient_id": row["patient_id"], "issued_on": "2026-11-20",
                                   "lines": [{"drug_name": "合成錠A"}]}, actor=clerk["staff_id"])
    assert rx["lines"][0]["drug_name"] == "合成錠A"
    assert REGISTRY.describe()["done_requires"] == {"pharmacy.home_visit": ["pharmacy.pharmacist"]}


def test_prescriptions_keep_lines_in_order_with_an_optional_master_code(db):
    me = first_staff(db)
    row = patient(db, me["staff_id"])
    values = {"patient_id": row["patient_id"], "issued_on": "2026-11-20", "institution": "合成クリニック",
              "lines": [{"drug_name": "合成錠A", "code_system": "mhlw_medicine", "drug_code": "1234567F1020",
                         "dose": "1錠", "usage": "朝食後", "days": 14}, {"drug_name": "合成散B"}]}
    for line, field in (({"drug_name": ""}, "lines.1.drug_name"), ({"drug_name": "x", "drug_code": "1"},
                        "lines.1.drug_code"), ({"drug_name": "x", "days": 0}, "lines.1.days")):
        with pytest.raises(records.InvalidFields) as error:
            prescriptions.create(db, values | {"lines": [line]}, actor=me["staff_id"])
        assert error.value.fields == [field]
    with pytest.raises(records.InvalidFields) as error:
        prescriptions.create(db, values | {"lines": []}, actor=me["staff_id"])
    assert error.value.fields == ["lines"]
    created = prescriptions.create(db, values, actor=me["staff_id"])
    assert [line["drug_name"] for line in created["lines"]] == ["合成錠A", "合成散B"]
    updated = prescriptions.update(db, created["prescription_id"], {"lines": [{"drug_name": "合成散B"}]},
                                   expected_version=1, actor=me["staff_id"])
    assert updated["version"] == 2 and [line["drug_name"] for line in updated["lines"]] == ["合成散B"]
    assert [item["prescription_id"] for item in prescriptions.for_patient(db, row["patient_id"])] == [
        created["prescription_id"]]


def test_visit_sheet_and_module_routes_go_through_the_core_api(db):
    me = first_staff(db)
    row = patient(db, me["staff_id"])
    links.link(db, row["patient_id"], 11, actor=me["staff_id"])
    scope = lambda: [{"project_id": 11, "patient_name": "合成 花子"}]  # noqa: E731
    write = lambda op, body: homecare_api.write(db, op, {"actor": me["staff_id"], **body}, registry=REGISTRY,  # noqa: E731
                                                allowed=None, mcs_patients=scope)
    write("visit.create", {"values": visit_values(row["patient_id"], visit_date="2026-11-30")})
    write("visit.create", {"values": visit_values(row["patient_id"], visit_date="2026-11-10")})  # overdue plan
    for day in range(1, 61):  # many later plans must not push the latest done visit out of the sheet
        write("visit.create", {"values": visit_values(row["patient_id"], visit_date=f"2027-{1 + day // 28:02d}-"
                                                                            f"{1 + day % 28:02d}")})
    done = write("visit.create", {"values": visit_values(row["patient_id"], visit_date="2026-11-01",
                                                         staff_id=me["staff_id"], status="done")})
    write("pharmacy/prescription.create", {"values": {"patient_id": row["patient_id"], "issued_on": "2026-11-20",
                                                      "lines": [{"drug_name": "合成錠A"}]}})
    assert homecare_api.read(db, "sheet", {"patient": str(row["patient_id"])}, registry=REGISTRY,
                             mcs_patients=scope)["patient"]["patient_id"] == row["patient_id"]
    sheet = homecare_api.sheet(db, row["patient_id"], registry=REGISTRY, mcs_patients=scope, today="2026-11-20")
    assert [item["visit_date"] for item in sheet["next_visits"]] == ["2026-11-30", "2027-01-02", "2027-01-03"]
    assert sheet["recent_visits"][0]["visit_id"] == done["record"]["visit_id"]
    assert sheet["modules"]["pharmacy"]["prescriptions"][0]["lines"][0]["drug_name"] == "合成錠A"
    # 2026-11-01 with no start time: from the start of that day in Japan.
    assert sheet["mcs"] == {"project_id": 11, "patient_name": "合成 花子", "since": 1793458800}
    outside = homecare_api.read(db, "sheet", {"patient": str(row["patient_id"])}, registry=REGISTRY,
                                mcs_patients=lambda: [])
    assert outside["mcs"] == {"project_id": None, "patient_name": None, "since": None}
    listed = homecare_api.read(db, "pharmacy/prescriptions", {"patient": str(row["patient_id"])},
                               registry=REGISTRY, mcs_patients=scope)
    assert len(listed["items"]) == 1 and listed["more"] is False
    first = homecare_api.read(db, "visits", {"patient": str(row["patient_id"])}, registry=REGISTRY, mcs_patients=scope)
    assert len(first["items"]) == 50 and first["more"] is True
    rest = homecare_api.read(db, "visits", {"patient": str(row["patient_id"]), "offset": "50"}, registry=REGISTRY,
                             mcs_patients=scope)
    # The next page carries on where the first stopped: no gap, no repeat, however long the history.
    assert len(rest["items"]) == 13 and rest["more"] is False
    assert {v["visit_id"] for v in first["items"]}.isdisjoint(v["visit_id"] for v in rest["items"])
    assert homecare_api.read(db, "visits", {"patient": str(row["patient_id"]), "offset": "5000"}, registry=REGISTRY,
                             mcs_patients=scope) == {"items": [], "more": False}
    for offset in ("-1", "x", "1.5"):
        with pytest.raises(ValueError, match="bad_request"):
            homecare_api.read(db, "visits", {"patient": str(row["patient_id"]), "offset": offset}, registry=REGISTRY,
                              mcs_patients=scope)
    for path in ("nursing/visits", "core/visits", "pharmacy/unknown"):
        with pytest.raises(ValueError, match="bad_request"):
            homecare_api.read(db, path, {"patient": "1"}, registry=REGISTRY, mcs_patients=scope)


class FakeServices:
    """Stands in for the GSI address search and openrouteservice; records what would have been sent."""

    def __init__(self, places):
        self.places, self.sent = places, []

    def __call__(self, request):
        self.sent.append(request)
        if request.full_url.startswith(route.GEOCODE_URL):
            address = parse_qs(urlsplit(request.full_url).query)["q"][0]
            return [{"geometry": {"coordinates": point}} for point in self.places.get(address, [])]
        body = json.loads(request.data)
        depart = body["vehicles"][0]["time_window"][0]
        # Ten minutes between stops; a window that closes before the first possible arrival cannot be met.
        # An early arrival waits for the window to open, as openrouteservice reports with waiting_time.
        jobs = [job for job in reversed(body["jobs"]) if job.get("time_windows", [[0, 10**6]])[0][1] >= depart + 600]
        if not jobs:
            return {"routes": [], "unassigned": [{"id": job["id"]} for job in body["jobs"]]}
        return {"routes": [{"steps": [{"type": "start"}] + [
            {"type": "job", "id": job["id"], "arrival": depart + 600 * n, "duration": 300 * n,
             "waiting_time": max(0, job.get("time_windows", [[0]])[0][0] - depart - 600 * n)}
            for n, job in enumerate(jobs, 1)] + [{"type": "end"}]}]}


def test_route_sends_addresses_only_caches_locations_and_lists_unlocated_visits(db, tmp_path):
    me = first_staff(db)
    settings_path = tmp_path / "route.json"
    settings_path.write_text(json.dumps({"openrouteservice_key": "synthetic-key-0000", "origin_address": "合成市1-1",
                                         "depart_time": "09:00"}))
    settings_path.chmod(0o644)
    with pytest.raises(SystemExit, match="chmod 600"):
        route.RouteSettings(settings_path)
    settings_path.chmod(0o600)
    settings = route.RouteSettings(settings_path)
    one = patient(db, me["staff_id"], name="合成 一郎", address="合成市2-2", caution_note="秘密のメモ")
    two = patient(db, me["staff_id"], name="合成 二郎", address="合成市3-3")
    none = patient(db, me["staff_id"], name="合成 三郎")
    early = patient(db, me["staff_id"], name="合成 四郎", address="合成市4-4")
    vague = patient(db, me["staff_id"], name="合成 五郎", address="合成区")
    ids = [visits.create(db, REGISTRY, visit_values(row["patient_id"], **extra), actor=me["staff_id"])["visit_id"]
           for row, extra in ((one, {"start_time": "10:00"}), (two, {}), (none, {}), (early, {"start_time": "08:00"}),
                              (vague, {}))]
    fake = FakeServices({"合成市1-1": [[139.7, 35.6]], "合成市2-2": [[139.71, 35.61]], "合成市3-3": [[139.72, 35.62]],
                         "合成市4-4": [[139.73, 35.63]], "合成区": [[139.74, 35.64], [135.5, 34.7]]})
    with pytest.raises(ValueError, match="route_unavailable"):
        route.plan(db, None, "2026-11-24", actor=me["staff_id"], fetch=fake)
    result = route.plan(db, settings, "2026-11-24", actor=me["staff_id"], fetch=fake)
    assert [stop["visit_id"] for stop in result["stops"]] == [ids[1], ids[0]]
    assert [stop["arrival"] for stop in result["stops"]] == ["09:10", "09:45"]  # waits for the 10:00 window
    # No address, or an address matching places far apart, is never guessed; an 08:00 booking cannot be met.
    assert result["unlocated"] == [ids[2], ids[4]] and result["unscheduled"] == [ids[3]]
    optimised = json.loads([r for r in fake.sent if r.full_url == route.OPTIMIZE_URL][0].data)
    assert [job["id"] for job in optimised["jobs"]] == [1, 2, 3]  # positions, not ledger visit IDs
    assert [job.get("time_windows") for job in optimised["jobs"]] == [  # the day's order: 08:00, 10:00, untimed
        [[7 * 3600 + 45 * 60, 8 * 3600 + 15 * 60]], [[9 * 3600 + 45 * 60, 10 * 3600 + 15 * 60]], None]
    sent = " ".join(r.full_url + (r.data or b"").decode() for r in fake.sent)
    assert "合成 一郎" not in sent and "秘密のメモ" not in sent and str(one["patient_id"]) not in sent.split("q=")[0]
    assert all(r.get_header("Authorization") in (None, "synthetic-key-0000") for r in fake.sent)
    geocoded = [r for r in fake.sent if r.full_url.startswith(route.GEOCODE_URL)]
    route.plan(db, settings, "2026-11-24", actor=me["staff_id"], fetch=fake)
    again = [r for r in fake.sent if r.full_url.startswith(route.GEOCODE_URL)]
    assert len(again) - len(geocoded) == 2  # the origin and the still-ambiguous address; found places are reused
    # A day whose every visit misses its window is a plan with no stops, not a failure.
    late = FakeServices({**fake.places})
    late_settings = tmp_path / "late.json"
    late_settings.write_text(json.dumps({"openrouteservice_key": "synthetic-key-0000", "origin_address": "合成市1-1",
                                         "depart_time": "12:00"}))
    late_settings.chmod(0o600)
    visits.update(db, REGISTRY, ids[1], {"start_time": "10:00"}, expected_version=1, actor=me["staff_id"])
    nothing = route.plan(db, route.RouteSettings(late_settings), "2026-11-24", actor=me["staff_id"], fetch=late)
    assert nothing["stops"] == [] and nothing["unscheduled"] == [ids[3], ids[0], ids[1]]
    failing = lambda request: (_ for _ in ()).throw(OSError("offline"))  # noqa: E731
    with pytest.raises(ValueError, match="route_failed"):
        route.plan(db, settings, "2026-11-24", actor=me["staff_id"], fetch=failing)


def test_route_transport_refuses_other_hosts():
    with pytest.raises(ValueError, match="route_failed"):
        route._fetch(urllib.request.Request("https://example.invalid/"))


def test_route_settings_must_be_a_json_object(tmp_path):
    path = tmp_path / "route.json"
    for text in ("[]", "not json"):
        path.write_text(text)
        path.chmod(0o600)
        with pytest.raises(SystemExit, match="JSON object"):
            route.RouteSettings(path)


@pytest.mark.parametrize("reply, expected", [
    ([], None),  # no match: unlocated
    ([{"geometry": {"coordinates": [139.7, 35.6]}}], (139.7, 35.6)),
    ([{"geometry": {"coordinates": [10.0, 50.0]}}], None),  # outside Japan
    # Each result lies within 300 m of the first, but the outer two are about 400 m apart.
    ([{"geometry": {"coordinates": [139.7, 35.6]}}, {"geometry": {"coordinates": [139.7022, 35.6]}},
      {"geometry": {"coordinates": [139.6978, 35.6]}}], None),
])
def test_geocode_keeps_unmatched_and_ambiguous_addresses_unlocated(reply, expected):
    assert route.geocode("合成市1-1", fetch=lambda request: reply) == expected


@pytest.mark.parametrize("reply", [{"unexpected": 1}, [{"geometry": {}}], [{"geometry": {"coordinates": ["a", 1]}}]])
def test_geocode_reports_an_unusable_reply_as_a_failure(reply):
    with pytest.raises(ValueError, match="route_failed"):
        route.geocode("合成市1-1", fetch=lambda request: reply)


def test_route_fails_on_malformed_steps_and_keeps_the_day_bounded(db, tmp_path):
    me = first_staff(db)
    path = tmp_path / "route.json"
    path.write_text(json.dumps({"openrouteservice_key": "synthetic-key-0000", "origin_address": "合成市1-1"}))
    path.chmod(0o600)
    row = patient(db, me["staff_id"], address="合成市2-2")
    visits.create(db, REGISTRY, visit_values(row["patient_id"], start_time="23:55"), actor=me["staff_id"])
    sent = []

    def services(steps):
        def fetch(request):
            if request.full_url.startswith(route.GEOCODE_URL):
                return [{"geometry": {"coordinates": [139.7, 35.6]}}]
            sent.append(json.loads(request.data))
            return {"routes": [{"steps": steps}]}
        return fetch

    settings = route.RouteSettings(path)
    for steps in ({"type": "job"}, ["job"]):
        with pytest.raises(ValueError, match="route_failed"):
            route.plan(db, settings, "2026-11-24", actor=me["staff_id"], fetch=services(steps))
    assert sent[0]["vehicles"][0]["time_window"] == [9 * 3600, 86400]
    assert sent[0]["jobs"][0]["time_windows"] == [[23 * 3600 + 40 * 60, 86400]]
    # A failed search is not cached as a place: the next plan asks again.
    with pytest.raises(ValueError, match="route_failed"):
        route.plan(db, settings, "2026-11-24", actor=me["staff_id"],
                   fetch=lambda request: (_ for _ in ()).throw(OSError("offline")))
    assert db.execute("SELECT COUNT(*) FROM patient_locations").fetchone()[0] == 1  # the earlier success only


def test_an_existing_ledger_moves_to_resident_mode_after_its_administrator_adds_a_login(server):
    db = homecare_store.connect(server.homecare, REGISTRY.modules)
    me = first_staff(db)  # registered in ordinary mode, with no Tailscale login yet
    server.tailnet_host = TAILNET
    # Switched too soon: nobody is staff by login, and the register is not empty, so nothing answers.
    assert resident_request(server, "GET", "/", login="admin@example.invalid") == (403, chat.UNREGISTERED)
    assert resident_request(server, "POST", "/api/hc/staff.create", BODY, login="admin@example.invalid")[0] == 403
    # Recovery: back in ordinary mode the administrator adds their own login, then resident mode admits them.
    db.close()
    server.tailnet_host = None
    status, data, _mime = post(server, {"id": me["staff_id"], "expected_version": 1, "actor": me["staff_id"],
                                        "values": {"tailscale_login": "admin@example.invalid"}}, op="staff.update")
    assert status == 200 and json.loads(data)["record"]["tailscale_login"] == "admin@example.invalid"
    server.tailnet_host = TAILNET
    assert resident_request(server, "GET", "/", login="admin@example.invalid")[0] == 200
    session = json.loads(resident_request(server, "GET", "/api/session", login="admin@example.invalid")[1])
    assert session["user"] == {"staff_id": me["staff_id"], "name": me["name"]}
