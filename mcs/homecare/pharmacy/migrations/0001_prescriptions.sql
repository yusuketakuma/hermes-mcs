-- The pharmacy module's prescription history. A line keeps the drug name as entered next to an optional
-- master code, so an old prescription still reads the same after the drug master changes.
CREATE TABLE pharmacy_prescriptions (
  prescription_id INTEGER PRIMARY KEY,
  patient_id INTEGER NOT NULL REFERENCES patients(patient_id),
  issued_on TEXT NOT NULL CHECK (issued_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  institution TEXT NOT NULL DEFAULT '' CHECK (length(institution) <= 100),
  prescriber TEXT NOT NULL DEFAULT '' CHECK (length(prescriber) <= 60),
  note TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 500),
  version INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX pharmacy_prescriptions_patient ON pharmacy_prescriptions(patient_id, issued_on);

CREATE TABLE pharmacy_prescription_lines (
  prescription_id INTEGER NOT NULL REFERENCES pharmacy_prescriptions(prescription_id),
  position INTEGER NOT NULL CHECK (position BETWEEN 1 AND 30),
  drug_name TEXT NOT NULL CHECK (length(trim(drug_name)) BETWEEN 1 AND 100),
  code_system TEXT CHECK (code_system IS NULL OR length(code_system) BETWEEN 1 AND 20),
  drug_code TEXT CHECK (drug_code IS NULL OR length(drug_code) BETWEEN 1 AND 20),
  dose TEXT NOT NULL DEFAULT '' CHECK (length(dose) <= 60),
  usage TEXT NOT NULL DEFAULT '' CHECK (length(usage) <= 100),
  days INTEGER CHECK (days IS NULL OR days BETWEEN 1 AND 365),
  PRIMARY KEY (prescription_id, position),
  CHECK ((code_system IS NULL) = (drug_code IS NULL))
);
