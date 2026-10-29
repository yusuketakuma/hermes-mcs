"""Export a content-free system status snapshot whose fields are listed in status_fields.json."""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urljoin

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _mcs_path  # noqa: F401

import mcs_util
import local_llm
from bounded_http import loopback_endpoint_allowed
from safe_io import atomic_write

SCHEMA = "mcs-status/1"
FIELDS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "status_fields.json")
MAX_SOURCE_BYTES = 1024 * 1024
MAX_DIR_FILES = 64
MAX_ITEMS = 64
# Only machine codes and numbers leave: a free-text value (a patient name, a
# message body, an exception text) can never match and is dropped instead.
CODE = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
TYPES = ("int", "number", "bool", "code", "codes", "counts")


def load_fields(path=FIELDS_PATH) -> dict:
    """The reviewed field list; adding a status item is one entry here, no code."""
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)
    sources, fields = spec.get("sources"), spec.get("fields")
    if spec.get("schema") != "mcs-status-fields/1" or not isinstance(sources, dict) \
            or not isinstance(fields, list):
        raise ValueError("status_fields_invalid")
    names = set()
    for source in sources.values():
        if isinstance(source, dict) and source.get("kind") == "host" and "path" not in source:
            continue
        rel = source.get("path") if isinstance(source, dict) else None
        if (not isinstance(rel, str) or os.path.isabs(rel) or ".." in rel.split("/")
                or source.get("kind") not in ("json", "json_dir")):
            raise ValueError("status_fields_invalid")
    for field in fields:
        if (not isinstance(field, dict) or not CODE.fullmatch(str(field.get("name")))
                or field["name"] in names or field.get("type") not in TYPES
                or (field.get("source") not in sources and field.get("source") != "build")):
            raise ValueError("status_fields_invalid")
        names.add(field["name"])
    return spec


def _read_json(path):
    try:
        return json.loads(mcs_util._read_local(path, MAX_SOURCE_BYTES).decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None


def _source(root, source):
    if source["kind"] == "host":
        return host_status(root)
    path = os.path.join(root, source["path"])
    if source["kind"] == "json":
        return _read_json(path)
    if os.path.islink(path) or not os.path.isdir(path):
        return None                       # an absent directory is unknown, not "0 workers"
    try:
        files = []
        with os.scandir(path) as entries:
            for entry in entries:
                if not entry.name.startswith(".") and entry.name.endswith(".json"):
                    files.append(entry.path)
                    if len(files) > MAX_DIR_FILES:
                        return None
    except OSError:
        return None
    docs = [_read_json(path) for path in sorted(files)]
    return docs if all(isinstance(doc, dict) for doc in docs) else None


JOB_PREFIXES = ("ai.mcs.", "local.mcs", "org.mcs.", "ai.hermes.")


def _run(argv):
    """A short read-only OS query; None on any failure or timeout."""
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=3,
                             env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _sysctl(name):
    out = _run(["/usr/sbin/sysctl", "-n", name])
    return out.strip() if out else None


def _int(text):
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _llm_get(path, *, endpoint):
    """GET the loopback llama-server without proxies or redirects (64 KiB cap)."""
    if not loopback_endpoint_allowed(endpoint):
        return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        with opener.open(urljoin(endpoint, path), timeout=2) as response:
            if response.status != 200:
                return None
            body = response.read(65537)
            return json.loads(body) if len(body) <= 65536 else None
    except (OSError, ValueError, http.client.HTTPException):
        return None


def _network():
    route = _run(["/sbin/route", "-n", "get", "default"])
    if not route:
        return {"default_route": None, "interface": None}
    iface = next((line.split(":", 1)[1].strip() for line in route.splitlines()
                  if line.strip().startswith("interface:")), None)
    out = {"default_route": True if iface else None,
           "interface": iface if iface and CODE.fullmatch(iface) else None}
    for line in (_run(["/usr/sbin/netstat", "-ibn"]) or "").splitlines()[1:]:
        cols = line.split()
        # the <Link#n> row carries the interface's own counters
        if iface and len(cols) >= 11 and cols[0] == iface and cols[2].startswith("<Link#"):
            out.update(rx_packets=_int(cols[-7]), rx_errors=_int(cols[-6]), rx_bytes=_int(cols[-5]),
                       tx_packets=_int(cols[-4]), tx_errors=_int(cols[-3]), tx_bytes=_int(cols[-2]))
            break
    return out


def _jobs():
    running, nonzero = 0, []
    output = _run(["/bin/launchctl", "list"])
    lines = output.splitlines() if output else []
    if not lines or lines[0].split() != ["PID", "Status", "Label"]:
        return None
    for line in lines[1:]:
        cols = line.split("\t")
        if len(cols) != 3:
            return None
        if not cols[2].startswith(JOB_PREFIXES):
            continue
        if (not CODE.fullmatch(cols[2]) or (cols[0] != "-" and not cols[0].isdigit())
                or (cols[1] != "-" and not re.fullmatch(r"-?[0-9]+", cols[1]))):
            return None
        if cols[0] != "-":
            running += 1
        elif cols[1] not in ("0", "-"):
            # not running and its last exit was non-zero; a periodic job may
            # exit non-zero on purpose (run_check exits 3 when the run lock is held)
            nonzero.append(cols[2])
    return {"running": running, "nonzero_exit": sorted(nonzero)}


def host_status(root) -> dict:
    """This Mac's resources: numbers and machine codes only (no host name or address)."""
    load = os.getloadavg()
    boot = re.search(r"sec = (\d+)", _sysctl("kern.boottime") or "")
    swap = re.findall(r"(total|used) = ([0-9.]+)M", _sysctl("vm.swapusage") or "")
    usage = shutil.disk_usage(root) if os.path.isdir(root) else None
    endpoint, _model = local_llm.resolve(mcs_util.load_config())
    slots = _llm_get("/slots", endpoint=endpoint)
    health = _llm_get("/health", endpoint=endpoint)
    valid_slots = isinstance(slots, list) and all(isinstance(s, dict) for s in slots)
    valid_busy = valid_slots and all(type(s.get("is_processing")) is bool for s in slots)
    return {
        "cpu": {"count": os.cpu_count(), "load1": load[0], "load5": load[1], "load15": load[2]},
        "memory": {"total_bytes": _int(_sysctl("hw.memsize")),
                   "free_pct": _int(_sysctl("kern.memorystatus_level")),
                   **{f"swap_{k}_mb": float(v) for k, v in swap}},
        "disk": {"free_mb": usage.free // 2**20 if usage else None,
                 "total_mb": usage.total // 2**20 if usage else None},
        "uptime_s": int(time.time()) - int(boot[1]) if boot else None,
        "llm": {"health": health.get("status") if isinstance(health, dict) else None,
                "slots_total": len(slots) if valid_slots else None,
                "slots_busy": sum(1 for s in slots if s["is_processing"]) if valid_busy else None},
        "network": _network(),
        "jobs": _jobs(),
    }


def _dig(value, path):
    for key in path.split(".") if path else []:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _present(value, path):
    """True when the dotted key exists (its value may be null)."""
    *parents, last = path.split(".")
    holder = _dig(value, ".".join(parents))
    return isinstance(holder, dict) and last in holder


def _typed(kind, value):
    """The value when it is exactly the declared type, else None (dropped)."""
    if kind == "bool":
        return value if type(value) is bool else None
    if kind == "int":
        return value if type(value) is int else None
    if kind == "number":
        return value if type(value) in (int, float) and value == value \
            and abs(value) != float("inf") else None
    if kind == "code":
        return value if isinstance(value, str) and CODE.fullmatch(value) else None
    if kind == "codes":
        if not isinstance(value, list) or len(value) > MAX_ITEMS:
            return None
        return value if all(isinstance(v, str) and CODE.fullmatch(v) for v in value) else None
    if not isinstance(value, dict) or len(value) > MAX_ITEMS:     # counts
        return None
    return value if all(CODE.fullmatch(str(k)) and type(v) is int
                        for k, v in value.items()) else None


def _build_value(name):
    if name == "commit":
        try:
            out = subprocess.run(["git", "-C", mcs_util.REPO, "rev-parse", "HEAD"],
                                 capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        sha = out.stdout.strip()
        return sha if out.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else None
    return None


def snapshot(root=None, spec=None, now=None) -> dict:
    """{schema, generated_at, fields, unavailable}: names absent or of the wrong type
    are listed in unavailable (never their values), so a reader can tell 0 from unknown."""
    root = root or os.path.join(mcs_util.HOME, "data")
    spec = spec or load_fields()
    loaded, fields, unavailable = {}, {}, []
    for field in spec["fields"]:
        src = field["source"]
        if src == "build":
            raw = _build_value(field.get("path"))
        else:
            if src not in loaded:
                loaded[src] = _source(root, spec["sources"][src])
            data = loaded[src]
            if field.get("count_by"):
                raw = None
                if isinstance(data, list):
                    raw = {}
                    for doc in data:
                        key = doc.get(field["count_by"])
                        if not isinstance(key, str) or not CODE.fullmatch(key):
                            raw = None
                            break
                        raw[key] = raw.get(key, 0) + 1
            else:
                raw = _dig(data, field.get("path", ""))
        if raw is None and field.get("nullable") is True and src != "build" \
                and _present(loaded[src], field.get("path", "")):
            fields[field["name"]] = None          # explicitly empty, e.g. "not down"
            continue
        value = _typed(field["type"], raw)
        if value is None:
            unavailable.append(field["name"])
        else:
            fields[field["name"]] = value
    return {"schema": SCHEMA, "generated_at": time.time() if now is None else now,
            "fields": fields, "unavailable": unavailable}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="MCSの稼働状況（数値と状態コードのみ）をJSONで出力します")
    p.add_argument("--out", help="書き出し先（所有者のみ読み書き可。省略時は標準出力）")
    args = p.parse_args(argv)
    try:
        body = json.dumps(snapshot(), ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (OSError, ValueError) as exc:
        print(f"status_snapshot_failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    if args.out:
        atomic_write(args.out, lambda f: f.write(body + "\n"), 0o600)
    else:
        print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
