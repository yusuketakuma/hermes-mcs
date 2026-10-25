"""Resolve approved signal thresholds from the latest provenance-bound stored policy."""
from mcs_queries import DAY_S, latest_artifact

FOLLOWUP_DAYS = 7          # med_change_no_followup window
FOLLOWUP_MAX_AGE_D = 90    # only mentions within this horizon — older
                           # ones are historical, not prospective
CONC_WINDOW_H = 72         # comm_concentration window
CONC_MIN_POSTS = 10        # comm_concentration threshold
EXPIRY_AHEAD_DAYS = 3      # rx_period_expiry horizon
RX_LAPSED_MAX_D = 14       # rx_period_lapsed: how many days past a
                           # period end the "no renewal mention" alert
                           # stays live — past that the silence is a
                           # record style, not a lapsed prescription
REQ_AGE_DAYS = 30          # request_aging: open register items older than this
REQ_RESPONSE_DAYS = 3      # pharmacist_request_unanswered response window
FYI_MAX_AGE_D = 30         # horizon for FYI-type signals (request
                           # visibility / adherence / symptom coupling)
NOTIFY_COOLDOWN_S = 7 * DAY_S  # no second notify for the same key inside this
TRANSITION_LOOKBACK_D = 60   # 退院 mentions within the last N days
TRANSITION_MED_WINDOW_D = 14 # med change mentions within ±N days of it

# Human-approved threshold policy: ops.signal_policy writes
# signal_policy_v1 artifacts; the latest one overrides these defaults.
# Statistics viewing has no path into this — detection thresholds move
# only via an explicit human-confirmed command (receipted).
POLICY_KIND = "signal_policy_v1"
#            name                default               low  high
THRESHOLDS = {
    "followup_days":            (FOLLOWUP_DAYS,            1,  90),
    "followup_max_age_d":       (FOLLOWUP_MAX_AGE_D,       7, 365),
    "conc_window_h":            (CONC_WINDOW_H,            6, 336),
    "conc_min_posts":           (CONC_MIN_POSTS,           2, 200),
    "expiry_ahead_days":        (EXPIRY_AHEAD_DAYS,        1,  90),
    "rx_lapsed_days":           (RX_LAPSED_MAX_D,          1,  60),
    "req_age_days":             (REQ_AGE_DAYS,             7, 365),
    "transition_lookback_d":    (TRANSITION_LOOKBACK_D,    7, 365),
    "transition_med_window_d":  (TRANSITION_MED_WINDOW_D,  1,  60),
    "notify_cooldown_d":        (NOTIFY_COOLDOWN_S // DAY_S, 1, 90),
    "request_response_days":    (REQ_RESPONSE_DAYS,        1,  30),
    "fyi_max_age_d":            (FYI_MAX_AGE_D,            7, 180),
}


def approved_policy(db):
    """Latest signal_policy_v1 artifact as (artifact_id, content), or
    None when absent or lacking provenance. Provenance required: a policy
    only counts when it arrived through the human-confirmed command path
    (non-empty str command_id + actor recorded by _apply_signal_policy_tx).
    A bare artifact insert cannot move thresholds — same identity
    boundary as the command envelope."""
    artifact_id, content = latest_artifact(db, POLICY_KIND)
    # Never revive a prior approval past an unreadable latest row.
    if not (content is not None
            and isinstance(content.get("command_id"), str)
            and content["command_id"]
            and isinstance(content.get("actor"), str)
            and content["actor"]):
        return None
    return artifact_id, content


# (delay, max-age): a detector looks at posts aged between these two, so a
# max-age at or below the delay leaves an empty window that reads as "no signals"
WINDOW_PAIRS = (("followup_days", "followup_max_age_d"),
                ("request_response_days", "fyi_max_age_d"))


def resolved_policy(db):
    """(thresholds, warnings): resolved_thresholds plus fixed warning codes
    (threshold_window_clamped:<max-age key>) for combinations that were
    clamped because they would leave a detector window empty."""
    return resolve_policy(approved_policy(db))


def resolve_policy(approved):
    """resolved_policy for an approved_policy result the caller already read."""
    th = {name: default for name, (default, lo, hi)
          in THRESHOLDS.items()}
    warnings = []
    policy = approved[1].get("policy") if approved is not None else None
    if isinstance(policy, dict):
        for name, (_default, lo, hi) in THRESHOLDS.items():
            v = policy.get(name)
            if type(v) is int and lo <= v <= hi:
                th[name] = v
    for delay, max_age in WINDOW_PAIRS:
        if th[max_age] <= th[delay]:
            th[max_age] = th[delay] + 1   # keep at least one day of window
            warnings.append(f"threshold_window_clamped:{max_age}")
    return th, warnings


def resolved_thresholds(db):
    """Resolved thresholds: defaults overlaid by the latest approved
    signal_policy_v1 artifact. Malformed/out-of-range values in the
    artifact are ignored per key rather than failing the whole run;
    a max-age that would empty its detector window is clamped (see resolved_policy)."""
    return resolved_policy(db)[0]
