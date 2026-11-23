"""Synthetic-only home-care core: modules, migrations, validation, versions, care team, links, audit and writes."""
from email.message import Message
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

import homecare_api
import homecare_catalog
import homecare_links as links
import homecare_modules
import homecare_records as records
import homecare_store
import homecare_team as team
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
        imported = {line.split()[1].split(".")[0] for line in path.read_text().splitlines()
                    if line.startswith(("import ", "from ")) and line.split()[1].split(".")[0] in modules}
        owner = path.parent.name
        allowed = core if owner == "core" else core | {p.stem for p in path.parent.glob("*.py")}
        assert imported <= allowed, (path.name, imported - allowed)


def test_database_is_private_migrated_per_module_and_reopenable(tmp_path):
    path = tmp_path / "private" / "homecare.db"
    homecare_store.connect(path, REGISTRY.modules).close()
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    again = homecare_store.connect(path, REGISTRY.modules)
    assert [tuple(row) for row in again.execute("SELECT module, number FROM schema_migrations")] == [("core", 1)]
    again.close()


def test_applied_migration_must_not_change_and_newer_database_is_refused(tmp_path):
    path = tmp_path / "homecare.db"
    homecare_store.connect(path, REGISTRY.modules).close()
    edited = tmp_path / "edited"
    edited.mkdir()
    original = homecare_store.migrations(homecare_modules.CORE.migrations)[0]
    (edited / original[1]).write_text(original[2] + "\n-- edited\n")
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
    ({"preferred_contact": "keyperson"}, ["keyperson_name", "keyperson_phone", "keyperson_relation"]),
    ({"preferred_contact": "facility"}, ["contact_phone"]),
    ({"phone": "", "preferred_contact": "patient"}, ["phone"]),
    ({"status": "deleted"}, ["status"]),
    ({"primary_pharmacist_id": 1}, ["primary_pharmacist_id"]),
    ({"address": "あ" * 201}, ["address"])])
def test_patient_fields_are_validated_by_name(db, values, fields):
    me = first_staff(db)
    with pytest.raises(records.InvalidFields) as error:
        patient(db, me["staff_id"], **values)
    assert error.value.fields == fields
    assert db.execute("SELECT count(*) FROM patients").fetchone()[0] == 0


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


@pytest.mark.parametrize("change", [{"active": False}, {"admin": False}])
def test_staff_cannot_lock_themselves_out_and_logins_are_unique(db, change):
    me = first_staff(db, tailscale_login="one@example.invalid")
    with pytest.raises(records.InvalidFields) as error:
        records.update(db, "staff", me["staff_id"], change, expected_version=1, actor=me["staff_id"], registry=REGISTRY)
    assert error.value.fields == list(change)
    with pytest.raises(records.InvalidFields) as error:
        staff(db, me["staff_id"], tailscale_login="ONE@example.invalid")
    assert error.value.fields == ["tailscale_login"]


def test_care_team_roles_need_the_declared_qualification_and_hand_over(db):
    me = first_staff(db)
    other = staff(db, me["staff_id"], name="合成 薬剤師二", codes=("pharmacy.pharmacist",))
    clerk = staff(db, me["staff_id"])
    row = patient(db, me["staff_id"])
    role = "pharmacy.primary_pharmacist"
    with pytest.raises(ValueError, match="not_qualified"):
        team.assign(db, REGISTRY, row["patient_id"], clerk["staff_id"], role, actor=me["staff_id"])
    with pytest.raises(ValueError, match="role_not_allowed"):
        team.assign(db, REGISTRY, row["patient_id"], me["staff_id"], "nursing.primary_nurse", actor=me["staff_id"])
    team.assign(db, REGISTRY, row["patient_id"], me["staff_id"], role, actor=me["staff_id"])
    with pytest.raises(homecare_store.Conflict, match="already_assigned"):
        team.assign(db, REGISTRY, row["patient_id"], me["staff_id"], role, actor=me["staff_id"])
    team.assign(db, REGISTRY, row["patient_id"], other["staff_id"], role, actor=clerk["staff_id"])
    assert [member["staff_id"] for member in team.current(db, row["patient_id"])] == [other["staff_id"]]
    ended = db.execute("SELECT ended_by FROM care_team WHERE staff_id=?", (me["staff_id"],)).fetchone()
    assert ended["ended_by"] == clerk["staff_id"]
    team.end(db, REGISTRY, team.current(db, row["patient_id"])[0]["assignment_id"], actor=me["staff_id"])
    assert team.current(db, row["patient_id"]) == []
    kinds = [r["kind"] for r in db.execute("SELECT kind FROM audit_events WHERE kind LIKE 'care_team.%'")]
    assert kinds == ["care_team.assign", "care_team.end", "care_team.assign", "care_team.end"]


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
    with pytest.raises(ValueError, match="project_not_allowed"):
        homecare_api.write(db, "link", {"patient": row["patient_id"], "project": 99, "actor": me["staff_id"]},
                           registry=REGISTRY, allowed={11})
    homecare_api.write(db, "link", {"patient": row["patient_id"], "project": 11, "actor": me["staff_id"]},
                       registry=REGISTRY, allowed={11})
    homecare_api.write(db, "team.assign", {"patient": row["patient_id"], "staff": me["staff_id"],
                                           "role": "pharmacy.primary_pharmacist", "actor": me["staff_id"]},
                       registry=REGISTRY, allowed={11})
    scope = lambda: [{"project_id": 11, "patient_name": "合成 花子"}]  # noqa: E731
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
    for bad in ("0", "-1", "1.5", True, None):
        with pytest.raises(ValueError, match="bad_request"):
            homecare_api.read(db, "patient", {"patient": bad}, registry=REGISTRY, mcs_patients=scope)


def post(server, body, headers=None):
    obj = chat.ChatHandler.__new__(chat.ChatHandler)
    obj.server = server
    obj.path = "/synthetic-capability/api/hc/staff.create"
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
                           allowed={1}, homecare=path, homecare_registry=REGISTRY)


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


def test_register_script_is_served_and_never_builds_html():
    assert chat.FILES["homecare.js"][0] == "homecare.js"
    js = (chat.ASSETS / "homecare.js").read_text()
    assert "innerHTML" not in js and "localStorage" not in js and "sessionStorage" not in js
    assert '"X-MCS-CSRF"' in js and 'credentials: "omit"' in js
    assert "pharmacist" not in js, "the core screen must read occupation codes from api/hc/meta"
