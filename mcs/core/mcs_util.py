"""Shared micro-utilities for the MCS adapter — no network, no DB access.

Centralizes the small pieces several modules used to keep private copies
of: config loading, HTML-to-text, the no-redirect HTTP guard, the
single-writer run lock, shared path constants, and the text utilities
(chunking / evidence-quote location) used by the extract and semantic
layers, and the LLM-lane stability gates (circuit breaker / disk floor)
shared by extract_llm and semantic_drain. Keep this module
dependency-free so every entry point
(run_check, init_data, the extract CLIs, notify_flush, mcs_adapter)
can import it without side effects.
"""
import fcntl
import hashlib
import html
import json
import os
import re
import tempfile
import urllib.request
from contextlib import suppress

HOME = os.path.expanduser("~/.mcs")
CONF_PATH = os.path.join(HOME, "config.json")
RUN_LOCK = os.path.join(HOME, "data", "run.lock")
DB = os.path.join(HOME, "data", "ledger.db")
CACHE = os.path.join(HOME, "token_cache.json")   # outside data/ (sandbox-mounted)
CHROME_PROFILE = os.path.join(HOME, "chrome-profile")
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def load_config(path: str = CONF_PATH) -> dict:
    """config.json -> dict; a missing/malformed file means {} so every
    caller falls back to its documented per-key defaults + validation."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def env_value(key: str, paths=None, check_env: bool = True) -> str | None:
    """KEY=value lookup shared by notify_flush/semantic/semantic_jev.

    Order: process env (unless check_env=False — file-pinned lookups like
    per-profile Discord tokens pass False so a stray shell var can never
    override the profile's own file), then each dotenv file in `paths`.
    Default paths: ~/.mcs/.env then ~/.hermes/.env.
    Secrets must never reach logs/payloads — callers hold that contract."""
    if check_env and os.environ.get(key):
        return os.environ[key]
    for path in (paths if paths is not None else
                 (os.path.join(HOME, ".env"),
                  os.path.expanduser("~/.hermes/.env"))):
        with suppress(OSError), open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith(key + "="):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if v:
                        return v
                    # an emptied `KEY=` line is "not configured" — same
                    # as an empty env var, it must not shadow a real
                    # value in a later dotenv file
                    break
    return None


def atomic_write(path: str, writer, mode: int | None = None,
                 tmp_prefix: str = ".atomic.") -> None:
    """tmp -> fsync -> [chmod] -> os.replace -> dir fsync: consumers see
    the whole old file or the whole new one, and a mid-write crash never
    leaves a torn file behind (tmp is unlinked on failure).  ``writer``
    receives the open text-mode file object."""
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=tmp_prefix,
                             suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            writer(f)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
        dfd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


# ---------- LLM lane stability gates ----------
# Shared by extract_llm and semantic_drain so both lanes defer under the
# same conditions instead of each inventing a gate.  The breaker state is
# a JSON file beside the ledger (independent of the DB's own health — a
# corrupted ledger must not hide an outage); once the failure streak hits
# _CIRCUIT_TRIP the lane stays closed for a cooldown that doubles per
# streak (cap 30min), so an outage degrades to one file read per drain.
# The disk floor guards the artifact writers: a nearly-full volume turns
# every guarded INSERT into an I/O error storm — rows stay pending and
# the drain reports disk_free_mb instead of burning attempts.
_CIRCUIT_TRIP = 3
_CIRCUIT_BASE_S = 120.0
_CIRCUIT_MAX_S = 1800.0


def _db_file(ledger) -> str:
    return ledger.db.execute("PRAGMA database_list").fetchone()[2]


def circuit_state_path(ledger):
    from pathlib import Path
    return Path(_db_file(ledger)).with_name("llm_circuit.json")


def _circuit_state(ledger) -> dict:
    try:
        d = json.loads(circuit_state_path(ledger).read_text())
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return d if isinstance(d, dict) else {}


def circuit_open_s(ledger) -> float:
    """Seconds the breaker stays open; 0 when closed. An unreadable or
    corrupt state file fails closed-open — it never blocks the lane."""
    import time as _time
    until = _circuit_state(ledger).get("open_until")
    return max(0.0, until - _time.time()) \
        if type(until) in (int, float) else 0.0


def circuit_success(ledger) -> None:
    """Any answered call proves the endpoint is alive — reset state."""
    with suppress(OSError):
        circuit_state_path(ledger).unlink(missing_ok=True)


def circuit_failure(ledger) -> None:
    """Endpoint-down observation: count consecutive probes and open the
    breaker on a doubling cooldown once the streak trips."""
    import time as _time
    path = circuit_state_path(ledger)
    state = _circuit_state(ledger)
    fails = (state.get("failures") or 0) + 1
    out = {"failures": fails, "streak": state.get("streak") or 0}
    if fails >= _CIRCUIT_TRIP:
        out["streak"] += 1
        out["open_until"] = _time.time() + min(
            _CIRCUIT_MAX_S, _CIRCUIT_BASE_S * 2 ** out["streak"])
        out["failures"] = 0
    payload = json.dumps(out)
    with suppress(OSError):
        atomic_write(str(path), lambda f: f.write(payload))


def disk_free_mb(ledger) -> float | None:
    """Free space (MiB) on the volume holding the ledger; None when
    unmeasurable — an unreadable fs must not gate the lane."""
    try:
        import shutil
        return shutil.disk_usage(_db_file(ledger)).free / (1024 * 1024)
    except (OSError, TypeError, IndexError):
        return None


def disk_floor_mb() -> float:
    """MiB floor under which the LLM lane defers; 0 disables the guard."""
    try:
        return float(os.environ.get("MCS_DISK_GUARD_MB", "512"))
    except ValueError:
        return 512.0


def file_sha256(path) -> str:
    """Streaming sha256 of a file — shared by operations/refstats so the
    digest contract (chunks of 64 KiB, hex digest) is defined once."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def json_object(text: str) -> dict | None:
    """First `{...}` block in LLM output parsed as a dict — models wrap
    JSON in prose, so the JSON object is located, not assumed.
    raw_decode stops at the object's own closing brace; a greedy
    first-{/last-} match would swallow trailing prose braces and fail."""
    text = text or ""
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            d, _end = decoder.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict):
            return d
    return None


def html_to_text(h: str) -> str:
    h = re.sub(r"<br\s*/?>", "\n", h or "")
    h = re.sub(r"</(p|div|li)>", "\n", h)
    return html.unescape(re.sub(r"<[^>]+>", "", h)).strip()


def locate_quote_span(body: str, quote: str) -> tuple[int, int] | None:
    """Find quote's UNIQUE codepoint span in body. Ambiguous or absent
    quotes get no span — never a guessed one (INV-07, AT-029).

    Exact match first; if absent, retry with all whitespace removed and
    map the span back to original codepoints. Models routinely emit
    quotes with inserted/altered whitespace — the located span is still
    unique and the caller stores body[s:e] verbatim, so span equality
    holds."""
    if not body or not quote:
        return None
    first = body.find(quote)
    if first >= 0 and body.find(quote, first + 1) < 0:
        return (first, first + len(quote))
    if first >= 0:
        return None
    nbody = []
    nidx = []
    for i, ch in enumerate(body):
        if not ch.isspace():
            nbody.append(ch)
            nidx.append(i)
    nbody = "".join(nbody)
    nquote = "".join(quote.split())
    s = nbody.find(nquote)
    if s < 0 or nbody.find(nquote, s + 1) >= 0:
        return None
    return (nidx[s], nidx[s + len(nquote) - 1] + 1)


def text_chunks(text: str, size: int = 3000) -> list:
    """Split into <=size chunks at line/sentence boundaries, hard-
    splitting only as a last resort. The concatenation of all chunks is
    the original text — full coverage, never head-only processing
    (§12.3, AT-017)."""
    if not text:
        return []
    if len(text) <= size:
        return [text]
    out, buf = [], ""
    for seg in re.split(r"(?<=\n)", text):
        if len(buf) + len(seg) <= size:
            buf += seg
            continue
        if buf:
            out.append(buf)
            buf = ""
        while len(seg) > size:
            cut = max(seg.rfind("。", 0, size), seg.rfind("\n", 0, size))
            if cut <= 0:
                cut = size
            out.append(seg[:cut])
            seg = seg[cut:]
        buf = seg
    if buf:
        out.append(buf)
    return out


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect — credentials (bearer/bot token) must never
    ride to a host we did not address."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def no_proxy_opener(*handlers):
    """Opener with environment proxies disabled — LLM/Discord/MCS calls
    must not silently route through a configured HTTP proxy."""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), *handlers)


def acquire_run_lock(path: str | None = None) -> int | None:
    """Non-blocking exclusive flock on the run lockfile.

    Returns the open fd — the lock lives as long as the fd stays open
    (hold it in main() scope; process exit releases it). Returns None
    when another writer holds it. Every entry point that writes the
    ledger must hold this lock so a manual CLI can never interleave
    writes with a scheduled tick."""
    path = path or RUN_LOCK
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd
