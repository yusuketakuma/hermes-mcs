-- Home-care core shared by every occupation module: staff and qualifications, patients, care team,
-- MCS patient links and the audit trail. Module tables live in their own migrations and reference only these.
CREATE TABLE staff (
  staff_id INTEGER PRIMARY KEY,
  name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 60),
  kana TEXT NOT NULL DEFAULT '' CHECK (length(kana) <= 60),
  tailscale_login TEXT UNIQUE CHECK (tailscale_login IS NULL OR length(tailscale_login) BETWEEN 3 AND 254),
  admin INTEGER NOT NULL DEFAULT 0 CHECK (admin IN (0, 1)),
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  version INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

-- Codes are '<module>.<name>' declared by the core or a module, e.g. 'pharmacy.pharmacist'.
CREATE TABLE staff_qualifications (
  staff_id INTEGER NOT NULL REFERENCES staff(staff_id),
  code TEXT NOT NULL CHECK (code GLOB '[a-z]*.[a-z]*'),
  PRIMARY KEY (staff_id, code)
);

CREATE TABLE patients (
  patient_id INTEGER PRIMARY KEY,
  name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 60),
  kana TEXT NOT NULL DEFAULT '' CHECK (length(kana) <= 60),
  birth_date TEXT CHECK (birth_date IS NULL OR birth_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  postal_code TEXT NOT NULL DEFAULT '' CHECK (length(postal_code) <= 8),
  address TEXT NOT NULL DEFAULT '' CHECK (length(address) <= 200),
  residence TEXT NOT NULL DEFAULT '' CHECK (length(residence) <= 100),
  phone TEXT NOT NULL DEFAULT '' CHECK (length(phone) <= 20),
  keyperson_name TEXT NOT NULL DEFAULT '' CHECK (length(keyperson_name) <= 60),
  keyperson_relation TEXT NOT NULL DEFAULT '' CHECK (length(keyperson_relation) <= 20),
  keyperson_phone TEXT NOT NULL DEFAULT '' CHECK (length(keyperson_phone) <= 20),
  preferred_contact TEXT NOT NULL DEFAULT 'patient'
    CHECK (preferred_contact IN ('patient', 'keyperson', 'facility', 'other')),
  contact_phone TEXT NOT NULL DEFAULT '' CHECK (length(contact_phone) <= 20),
  contact_note TEXT NOT NULL DEFAULT '' CHECK (length(contact_note) <= 200),
  access_note TEXT NOT NULL DEFAULT '' CHECK (length(access_note) <= 500),
  caution_note TEXT NOT NULL DEFAULT '' CHECK (length(caution_note) <= 500),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'ended')),
  version INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

-- Who on this organization's staff holds which role for a patient; a role code is '<module>.<name>'.
CREATE TABLE care_team (
  assignment_id INTEGER PRIMARY KEY,
  patient_id INTEGER NOT NULL REFERENCES patients(patient_id),
  staff_id INTEGER NOT NULL REFERENCES staff(staff_id),
  role TEXT NOT NULL CHECK (role GLOB '[a-z]*.[a-z]*'),
  started_at REAL NOT NULL,
  started_by INTEGER NOT NULL REFERENCES staff(staff_id),
  ended_at REAL,
  ended_by INTEGER REFERENCES staff(staff_id),
  CHECK ((ended_at IS NULL) = (ended_by IS NULL))
);
CREATE INDEX care_team_patient ON care_team(patient_id, role) WHERE ended_at IS NULL;

-- Human-confirmed links between a pharmacy patient and an MCS patient room; rows are never changed.
CREATE TABLE patient_mcs_links (
  link_id INTEGER PRIMARY KEY,
  patient_id INTEGER NOT NULL REFERENCES patients(patient_id),
  project_id INTEGER NOT NULL CHECK (project_id > 0),
  action TEXT NOT NULL CHECK (action IN ('link', 'unlink')),
  reason TEXT NOT NULL DEFAULT '' CHECK (length(reason) <= 200),
  actor_staff_id INTEGER NOT NULL REFERENCES staff(staff_id),
  at REAL NOT NULL
);
CREATE INDEX patient_mcs_links_patient ON patient_mcs_links(patient_id, link_id);
CREATE INDEX patient_mcs_links_project ON patient_mcs_links(project_id, link_id);

-- Who changed what, with the record before and after; rows are never changed.
CREATE TABLE audit_events (
  event_id INTEGER PRIMARY KEY,
  at REAL NOT NULL,
  actor_staff_id INTEGER REFERENCES staff(staff_id),
  kind TEXT NOT NULL,
  entity_id INTEGER NOT NULL,
  before_json TEXT,
  after_json TEXT
);

CREATE TRIGGER patient_mcs_links_append_only_update BEFORE UPDATE ON patient_mcs_links
BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER patient_mcs_links_append_only_delete BEFORE DELETE ON patient_mcs_links
BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER audit_events_append_only_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER audit_events_append_only_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'append_only'); END;
