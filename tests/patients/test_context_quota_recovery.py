"""A damaged synthetic generation record is counted but does not stop the overview lane."""
import patient_context_quota as quota
import patient_context_refresh_readiness as readiness
from test_patient_context_generation_limits import _ready_sources
from test_patient_context_refresh import CFG, runtime as runtime, seed, step


def test_unreadable_quota_record_does_not_stop_lane_a(runtime):
    led, clock, _ = runtime
    seed(led)
    _ready_sources(runtime)
    led.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                   "VALUES(?,1,NULL,'x','','{}',?)", (quota.KIND, clock.wall - 2 * 86400))
    led.db.commit()
    kept = led.db.execute("SELECT * FROM artifacts WHERE kind=? ORDER BY artifact_id", (quota.KIND,)).fetchall()
    outcome = step(runtime)
    assert outcome['status'] == 'progressed' and outcome['lane'] == 'A', outcome
    assert readiness.context_view_input(led.db, 1, cfg=CFG) is not None
    after = led.db.execute("SELECT * FROM artifacts WHERE kind=? ORDER BY artifact_id", (quota.KIND,)).fetchall()
    assert after[:1] == kept and len(after) == 2
