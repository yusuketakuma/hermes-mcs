"""共有する診断から設定値・患者件数・任意の文字列を除く。"""
from __future__ import annotations

import re

from collection_diagnostics import error_counts, validated

SCHEMA_VERSION = "mcs.support-diagnostics.v1"
CHECKS = ("configuration", "interpreter", "runtime", "recovery_runtime", "llm",
          "services", "credentials", "data", "desired_recovery_runtime",
          "recovery_selection", "card_worker", "restore_delivery")
STATUSES = frozenset({"healthy", "warning", "blocked", "not_checked", "unknown", "stale"})


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _count(value):
    return value if type(value) is int and 0 <= value <= 1_000_000_000 else None


def _status(value):
    return value if isinstance(value, str) and value in STATUSES else "unknown"


def _formatted(value, pattern):
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else None


def shared_report(report):
    """Project local doctor facts only; perform no collection, probe or transmission."""
    report = _mapping(report)
    checks = _mapping(report.get("checks"))
    clean_checks = {}
    for scope in CHECKS:
        if scope not in checks:
            continue
        check = _mapping(checks[scope])
        errors, warnings = _count(check.get("errors")), _count(check.get("warnings"))
        clean_checks[scope] = {
            "status": _status(check.get("status")) if errors is not None and warnings is not None else "unknown",
            "errors": errors, "warnings": warnings,
        }
    source = _mapping(report.get("source"))
    disk = _mapping(source.get("disk"))
    gateway = _mapping(source.get("gateway"))
    collection = _mapping(report.get("collection"))
    counts = validated(collection.get("counts"))
    errors = counts["errors"] if counts is not None else error_counts(None)
    if errors["total"] is not None and _count(errors["total"]) is None:
        errors = error_counts(None)
    collection_status = _status(collection.get("status"))
    if collection_status == "healthy":
        if counts is None or errors["state"] != "known" or counts["incomplete"]["state"] != "known":
            collection_status = "unknown"
        elif errors["total"] or counts["incomplete"]["projects"]:
            collection_status = "warning"
    hold = _mapping(report.get("card_worker")).get("restore_delivery_held")
    mode = report.get("mode")
    return {
        "schema_version": SCHEMA_VERSION,
        "software": {
            "mode": mode if mode in ("hermes", "standalone", "invalid") else "unknown",
            "python": _formatted(report.get("python"), r"[0-9]{1,3}(?:\.[0-9]{1,3}){1,2}"),
            "sqlite": _formatted(report.get("sqlite"), r"[0-9]{1,3}(?:\.[0-9]{1,3}){1,2}"),
            "disk_commit": _formatted(disk.get("revision"), r"[0-9a-f]{40}"),
            "loaded_commit": _formatted(gateway.get("source_revision"), r"[0-9a-f]{40}"),
            "working_tree_dirty": disk.get("dirty") if type(disk.get("dirty")) is bool else None,
        },
        "checks": clean_checks,
        "collection": {"status": collection_status,
                       "observed_at": counts["observed_at"] if counts is not None else None,
                       "errors": errors},
        "restore_delivery_held": hold if type(hold) is bool else None,
    }
