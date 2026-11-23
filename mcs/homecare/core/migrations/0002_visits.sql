-- Visits to a patient by this organization's staff; ``kind`` is a module-declared code such as
-- 'pharmacy.home_visit'. A visit is planned, then done or cancelled; rows are edited with a version check.
CREATE TABLE visits (
  visit_id INTEGER PRIMARY KEY,
  patient_id INTEGER NOT NULL REFERENCES patients(patient_id),
  staff_id INTEGER REFERENCES staff(staff_id),
  kind TEXT NOT NULL CHECK (kind GLOB '[a-z]*.[a-z]*'),
  visit_date TEXT NOT NULL CHECK (visit_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  start_time TEXT CHECK (start_time IS NULL OR start_time GLOB '[0-2][0-9]:[0-5][0-9]'),
  status TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned', 'done', 'cancelled')),
  note TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 500),
  version INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  CHECK (status != 'done' OR staff_id IS NOT NULL)
);
CREATE INDEX visits_patient ON visits(patient_id, visit_date);
CREATE INDEX visits_date ON visits(visit_date, start_time);
