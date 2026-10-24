"""SDC診断を固定の非機密項目へ整形し、許可した受動記録だけを確認します。"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import uuid

SCHEMA_VERSION = "sdc.support.diagnostic-result.v1"
SOURCE_SCHEMA = "mcs.support-diagnostics.v1"
DIAGNOSTICS = ("runtime.status.v1", "notification.health.v1", "connectivity.status.v1")
MAX_SAFE_INTEGER = 2**53 - 1
MAX_RESULT_BYTES = 16384


def _bounded_source(value):
    # Inspect bounded plain JSON only; never serialize or echo the source.
    remaining = [4096, 65536]

    def visit(item, depth):
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 8:
            return False
        if type(item) is str:
            remaining[1] -= len(item)
            return remaining[1] >= 0
        if item is None or type(item) is bool:
            return True
        if type(item) is int:
            return -MAX_SAFE_INTEGER <= item <= MAX_SAFE_INTEGER
        if type(item) is float:
            return math.isfinite(item)
        if type(item) is list:
            return len(item) <= remaining[0] and all(visit(child, depth + 1) for child in item)
        if type(item) is dict:
            return len(item) <= remaining[0] and all(
                type(key) is str and visit(key, depth + 1) and visit(child, depth + 1)
                for key, child in item.items())
        return False

    return visit(value, 0)


def project_diagnostic(diagnostic_id, shared, *, generated_at_ms):
    """Project an existing shared DTO without I/O; generated_at_ms is assembly time.

    The required caller timestamp is not a source observation or freshness witness. In particular,
    collection.observed_at cannot establish when software facts were observed.
    Invalid calls raise fixed codes; malformed sources produce a closed failed DTO.
    """
    if type(diagnostic_id) is not str or diagnostic_id not in DIAGNOSTICS:
        raise ValueError("SDC_DIAGNOSTIC_INVALID")
    if type(generated_at_ms) is not int or not 0 <= generated_at_ms <= MAX_SAFE_INTEGER:
        raise ValueError("SDC_DIAGNOSTIC_TIME_INVALID")
    if diagnostic_id == "runtime.status.v1":
        data = {"mode": "unknown", "process": "unknown", "installed_commit": None,
                "loaded_commit": None, "installed_version": None, "loaded_version": None}
    elif diagnostic_id == "notification.health.v1":
        data = {"service": "unknown", "credential_configured": "unknown",
                "authentication": "unknown", "retry": "unknown"}
    else:
        data = {"target_id": "sdc-central", "connection": "unknown", "duration_ms": None}
    result = {"schema_version": SCHEMA_VERSION, "diagnostic_id": diagnostic_id,
              "generated_at": generated_at_ms, "observed_at": None, "status": "unknown", "duration_ms": None,
              "error_code": "SOURCE_UNKNOWN", "data": data}
    if shared is None:
        result.update(status="failed", error_code="SOURCE_UNAVAILABLE")
    elif (type(shared) is not dict or not _bounded_source(shared)
          or shared.get("schema_version") != SOURCE_SCHEMA
          or type(shared.get("software")) is not dict):
        result.update(status="failed", error_code="DIAGNOSTIC_FAILED")
    elif diagnostic_id == "runtime.status.v1":
        software = shared["software"]
        mode, commit = software.get("mode"), software.get("disk_commit")
        if type(mode) is str and mode in ("hermes", "standalone"):
            data["mode"] = mode
        if type(commit) is str and re.fullmatch(r"[0-9a-f]{40}", commit):
            data["installed_commit"] = commit
        if data["mode"] != "unknown" or data["installed_commit"] is not None:
            result["status"] = "partial"
    # This fixed DTO contains ASCII enums, validated SHA and a safe integer only.
    _encode(result)
    return result


def _encode(result):
    raw = json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    if len(raw) > MAX_RESULT_BYTES:
        raise ValueError("SDC_DIAGNOSTIC_TOO_LARGE")
    return raw


def diagnostic_json(diagnostic_id, shared, *, generated_at_ms):
    """Return bounded canonical ASCII JSON from the same pure projection."""
    return _encode(project_diagnostic(diagnostic_id, shared, generated_at_ms=generated_at_ms))


def _source_time(value):
    return (type(value) in (int, float) and math.isfinite(value)
            and 0 <= value <= MAX_SAFE_INTEGER / 1000)


def collect_diagnostic(diagnostic_id, *, generated_at_ms, root=None, mode=None):
    """Read only a registered product root's standalone owner witness, never doctor or secrets.

    root and mode come from the trusted local caller, not remote diagnostic params.
    PID existence and a fresh heartbeat prove neither loaded code nor authentication.
    """
    safe_mode = mode if type(mode) is str and mode in ("hermes", "standalone") else "unknown"
    result = project_diagnostic(diagnostic_id, {"schema_version": SOURCE_SCHEMA,
                                "software": {"mode": safe_mode}}, generated_at_ms=generated_at_ms)
    if diagnostic_id == "connectivity.status.v1" or safe_mode != "standalone":
        return result
    if not isinstance(root, (str, Path)) or not str(root) or not Path(root).is_absolute():
        return result
    from mcs_standalone import service

    now = generated_at_ms / 1000
    try:
        witness = service.status(root, now=now)
        at, started = witness.get("updated_at"), witness.get("started_at")
        generation = witness.get("generation")
        if not (_source_time(at) and _source_time(started) and started <= at <= now
                and now - at <= service.STATUS_TTL and type(generation) is str
                and re.fullmatch(r"[0-9a-f]{32}", generation)):
            return result
        parsed = uuid.UUID(hex=generation)
        if parsed.version != 4 or parsed.variant != uuid.RFC_4122:
            return result
        observed = int(at * 1000)
        if observed > generated_at_ms:
            return result
        if diagnostic_id == "runtime.status.v1":
            result["data"]["process"] = "running"
        else:
            connector = witness.get("connector")
            if type(connector) is not dict:
                return result
            if connector.get("state") == "running":
                result["data"]["service"] = "running"
            elif connector.get("state") == "waiting":
                retry = connector.get("retry_at")
                if _source_time(retry) and retry > now:
                    result["data"]["retry"] = "backoff"
            if result["data"]["service"] == "unknown" and result["data"]["retry"] == "unknown":
                return result
        result.update(observed_at=observed, status="partial", error_code="SOURCE_UNKNOWN")
    except TimeoutError:
        raise
    except Exception:
        # Unavailable or malformed witness never exposes its values or exception text.
        return result
    _encode(result)
    return result
