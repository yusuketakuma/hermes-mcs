"""Validate the ``backup`` config block without loading setup or the ledger."""
import os
import re


def validate_backup_config(value):
    """Return an error string for an invalid ``backup`` setting, else None."""
    keys = {"enabled", "policy", "snapshot", "snapshot_dir", "schedule",
            "verify_interval_s", "drill_interval_s"}
    if (not isinstance(value, dict) or set(value) - keys
            or type(value.get("enabled")) is not bool):
        return "must contain a boolean enabled and only supported backup keys"
    if not value["enabled"]:
        return None
    sources = [key for key in ("snapshot", "snapshot_dir") if key in value]
    if len(sources) != 1:
        return "exactly one of snapshot or snapshot_dir must be explicit"
    for key in ("policy", *sources):
        if not isinstance(value.get(key), str) or not os.path.isabs(value[key]):
            return f"{key} must be an explicit absolute path"
    for key in ("verify_interval_s", "drill_interval_s"):
        if type(value.get(key)) is not int or value[key] < 1:
            return f"{key} must be an explicit positive integer"
    schedule = value.get("schedule")
    match = re.fullmatch(
        r"([0-5]?[0-9])\s+((?:[01]?[0-9]|2[0-3])(?:,(?:[01]?[0-9]|2[0-3])){0,23})"
        r"\s+\*\s+\*\s+\*", schedule) if isinstance(schedule, str) else None
    if match is None:
        return "schedule must have one explicit minute and 1–24 distinct explicit hours"
    hours = [int(hour) for hour in match[2].split(",")]
    if len(hours) != len(set(hours)):
        return "schedule hours must be distinct"
    return None
