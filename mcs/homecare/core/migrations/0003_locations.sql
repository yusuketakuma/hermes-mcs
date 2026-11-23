-- Where a patient's address was found by the address search, kept so the same address is sent only once.
-- A changed address no longer matches ``address_sha256`` and is looked up again.
CREATE TABLE patient_locations (
  patient_id INTEGER PRIMARY KEY REFERENCES patients(patient_id),
  address_sha256 TEXT NOT NULL CHECK (length(address_sha256) = 64),
  lon REAL NOT NULL CHECK (lon BETWEEN 122 AND 154),
  lat REAL NOT NULL CHECK (lat BETWEEN 20 AND 46),
  located_at REAL NOT NULL
);
