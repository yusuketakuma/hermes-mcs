"""Define shared scheduled jobs, calendar expansion and bounded job execution."""
from __future__ import annotations

import re

from backup_config import validate_backup_config as _backup_config

CRON_JOBS = [
    ("MCS unread check", "*/10 * * * *", "mcs_check.sh"),
    ("MCS health watch", "*/5 * * * *", "mcs_health.sh"),
    ("MCS durable drain", "10,40 * * * *", "mcs_deep.sh"),
    ("MCS retry maintenance", "0 */6 * * *", "mcs_llm_catchup.sh"),
    ("llamacpp daily restart", "0 4 * * *", "llamacpp_restart_if_idle.sh"),
    ("MCS update check", "10 5 * * *", "mcs_update.sh"),
]


def configured_cron_jobs(cfg: dict) -> list[tuple[str, str, str]]:
    """Keep default jobs and append only an explicitly configured offsite schedule."""
    jobs = list(CRON_JOBS)
    backup = cfg.get("backup", {"enabled": False})
    if _backup_config(backup):
        raise ValueError("backup_config_invalid")
    if backup["enabled"]:
        jobs.append(("MCS offsite backup", backup["schedule"], "mcs_offsite.sh"))
    return jobs


def calendar_entries(schedule: str) -> list[dict]:
    """Expand a CRON_JOBS schedule into calendar entries for the standalone scheduler.

    Each entry is a {"Hour", "Minute"} dict (the launchd StartCalendarInterval
    shape); `*/n` and comma lists are expanded, other fields must be `*`."""
    minute, hour, *rest = schedule.split()
    if rest != ["*", "*", "*"]:
        raise ValueError(f"unsupported schedule {schedule!r}")

    def expand(field, size):
        if field == "*":
            return [None]
        if field.startswith("*/"):
            return list(range(0, size, int(field[2:])))
        return [int(v) for v in field.split(",")]
    return [{k: v for k, v in (("Hour", h), ("Minute", m)) if v is not None}
            for h in expand(hour, 24) for m in expand(minute, 60)]


# The standalone host's scheduler (mcs_standalone.runtime) wraps each job
# in the same cap as Hermes cron. Otherwise, refusing to start a job
# that is still running would stop it for good after one wedged tick.
# The wrapper kills the job's process group.
CRON_TIMEOUT_S = 3600      # = hermes cron's script timeout
TIMEOUT_PERL = ("my $t = shift; my $p = 0;"
               # launchd's SIGTERM (bootout, logout) must reach the job too
               " $SIG{$_} = sub { kill 'TERM', -$p if $p; exit 143 } for qw(TERM INT HUP);"
               " $p = fork; die unless defined $p;"
               " if (!$p) { setpgrp(0, 0); exec @ARGV; exit 127 }"
               " $SIG{ALRM} = sub { kill 'TERM', -$p; sleep 5; kill 'KILL', -$p; exit 124 };"
               " alarm $t; waitpid($p, 0); my $s = $?;"
               " exit(($s & 127) ? 128 + ($s & 127) : ($s >> 8))")


def render_template(text: str, subs: dict) -> str:
    return re.sub(r"__([A-Z_]+)__",
                  lambda match: subs.get(match.group(1), match.group(0)), text)




def latest_calendar_slots(schedules, minute, catchup_window_min, localtime):
    """job -> start of its newest calendar slot within the catch-up window."""
    due, pending = {}, dict(schedules)
    for back in range(catchup_window_min + 1):
        if not pending:
            break
        stamp = (minute - back) * 60
        local = localtime(stamp)
        for job, entries in list(pending.items()):
            if any(entry.get("Hour", local.tm_hour) == local.tm_hour
                   and entry.get("Minute", local.tm_min) == local.tm_min for entry in entries):
                due[job] = stamp
                del pending[job]
    return due

