"""Share patient availability checks for original-source reads and clinical processing."""

def patient_scope_valid(db, project_id):
    """Check patient availability without materializing unbounded source fields."""
    row = db.execute("SELECT CASE WHEN typeof(is_archived)!='integer' OR is_archived!=0 "
                     "OR typeof(fetch_state) NOT IN ('null','text') "
                     "OR typeof(fetch_reason) NOT IN ('null','text') "
                     "OR COALESCE(octet_length(fetch_state),0)>1024 "
                     "OR COALESCE(octet_length(fetch_reason),0)>1024 THEN 0 "
                     "WHEN fetch_state='forbidden' OR fetch_reason IN "
                     "('forbidden','permission_denied','access_revoked') THEN 0 ELSE 1 END "
                     "FROM patients WHERE project_id=?", (project_id,)).fetchone()
    return row is not None and row[0] == 1
