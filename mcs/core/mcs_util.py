"""Shared micro-utilities for the MCS adapter — no network, no DB access.

Centralizes the small pieces several modules used to keep private copies
of: config loading, HTML-to-text, the no-redirect HTTP guard, the
single-writer run lock, shared path constants, and the text utilities
(chunking / evidence-quote location) used by the extract and semantic
layers, and the LLM-lane stability gates (circuit breaker / disk floor)
shared by extract_llm and semantic_drain, and the verified launchd
bootstrap shared by mcs_setup and mcs_update. Keep this module
dependency-free so every entry point
(run_check, init_data, the extract CLIs, notify_flush, mcs_adapter)
can import it without side effects.
"""
import fcntl
import hashlib
import html
import json
import math
import os
import re
import sqlite3
import tempfile
import time
import unicodedata
import urllib.request
from contextlib import contextmanager, suppress

HOME = os.path.abspath(os.path.expanduser(os.environ.get("MCS_ROOT", "~/.mcs")))
# checkout root; scripts/mcs_upgrade.py runs a target tag's code from a
# temp dir and points it at the live checkout through MCS_UPDATE_REPO
REPO = os.path.abspath(os.environ.get("MCS_UPDATE_REPO") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
CONF_PATH = os.path.join(HOME, "config.json")
RUN_LOCK = os.path.join(HOME, "data", "run.lock")
DB = os.path.join(HOME, "data", "ledger.db")
# written under <HOME>/data by mcs_update while an update is applied
UPDATE_MARKER_NAME = "update_in_progress.marker"
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
    except (OSError, ValueError, RecursionError):
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
                    v = line.split("=", 1)[1].strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                        if v[0] == '"':
                            try:
                                v = json.loads(v)
                            except ValueError:
                                v = v[1:-1]
                        else:
                            v = v[1:-1]
                    if v:
                        return v
                    # an emptied `KEY=` line is "not configured" — same
                    # as an empty env var, it must not shadow a real
                    # value in a later dotenv file
                    break
    return None


def publish_tmp(tmp: str, dest: str, mode: int | None = None) -> None:
    """[chmod] -> fsync tmp -> os.replace -> dir fsync for a finished tmp
    file: readers see the whole old ``dest`` or the whole new one."""
    if mode is not None:
        os.chmod(tmp, mode)
    fd = os.open(tmp, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, dest)
    dfd = os.open(os.path.dirname(dest) or ".", os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def atomic_write(path: str, writer, mode: int | None = None,
                 tmp_prefix: str = ".atomic.") -> None:
    """tmp -> fsync -> [chmod] -> os.replace -> dir fsync: consumers see
    the whole old file or the whole new one, and a mid-write crash never
    leaves a torn file behind (tmp is unlinked on failure).  ``writer``
    receives the open text-mode file object."""
    parent = os.path.dirname(path) or "."
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
_CIRCUIT_MAX_STREAK = math.ceil(math.log2(_CIRCUIT_MAX_S / _CIRCUIT_BASE_S))


def _db_file(ledger) -> str:
    return ledger.db.execute("PRAGMA database_list").fetchone()[2]


def circuit_state_path(ledger):
    from pathlib import Path
    return Path(_db_file(ledger)).with_name("llm_circuit.json")


def _circuit_state(ledger) -> dict:
    return load_config(circuit_state_path(ledger))


def circuit_open_s(ledger) -> float:
    """Seconds the breaker stays open; 0 when closed. An unreadable or
    corrupt state file fails closed-open — it never blocks the lane."""
    import time as _time
    until = _circuit_state(ledger).get("open_until")
    if type(until) not in (int, float):
        return 0.0
    try:
        until = float(until)
    except OverflowError:
        return 0.0
    return max(0.0, until - _time.time()) if math.isfinite(until) else 0.0


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
    fails, streak = state.get("failures"), state.get("streak")
    fails = min(fails, _CIRCUIT_TRIP - 1) \
        if type(fails) is int and fails >= 0 else 0
    streak = min(streak, _CIRCUIT_MAX_STREAK) \
        if type(streak) is int and streak >= 0 else 0
    fails += 1
    out = {"failures": fails, "streak": streak}
    if fails >= _CIRCUIT_TRIP:
        out["streak"] = min(streak + 1, _CIRCUIT_MAX_STREAK)
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
        value = float(os.environ.get("MCS_DISK_GUARD_MB", "512"))
        return value if math.isfinite(value) and value >= 0 else 512.0
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


def loads_dict(raw) -> dict | None:
    """``json.loads(raw)`` when it yields a dict, else None — for stored
    JSON TEXT columns (not LLM output; that is ``json_object``)."""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


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
        except (ValueError, RecursionError):
            continue
        if isinstance(d, dict):
            return d
    return None


def html_to_text(h: str) -> str:
    h = re.sub(r"<br\s*/?>", "\n", h or "")
    h = re.sub(r"</(p|div|li)>", "\n", h)
    return html.unescape(re.sub(r"<[^>]+>", "", h)).strip()


def _combines(ch: str) -> bool:
    norm = unicodedata.normalize("NFKC", ch)
    return bool(norm) and all(unicodedata.combining(c) for c in norm)


def fold_map(text: str, casefold: bool = False) -> tuple[str, list, list]:
    """``text`` NFKC-normalized (optionally casefolded) with whitespace
    removed, plus each output char's source span as parallel
    ``starts``/``ends`` code-point lists. A base char and its following
    combining marks (halfwidth ﾞﾟ included) normalize together, so
    ﾊﾞ folds to バ; NFKC may expand one source into several chars, all
    mapped to that source."""
    out, starts, ends = [], [], []
    i, n = 0, len(text)
    while i < n:
        j = i + 1
        while j < n and _combines(text[j]):
            j += 1
        norm = unicodedata.normalize("NFKC", text[i:j])
        for c in norm.casefold() if casefold else norm:
            if not c.isspace():
                out.append(c)
                starts.append(i)
                ends.append(j)
        i = j
    return "".join(out), starts, ends


def fold_find(text: str, term: str, casefold: bool = False) -> tuple[int, int] | None:
    """The UNIQUE original span of ``term`` in ``text`` compared after
    ``fold_map``, or None when absent, ambiguous, or starting/ending
    inside one source's expansion (never a partial char)."""
    flat, starts, ends = fold_map(text, casefold)
    needle = fold_map(term, casefold)[0]
    s = flat.find(needle) if needle else -1
    if s < 0 or flat.find(needle, s + 1) >= 0:
        return None
    e = s + len(needle)
    if (s and starts[s - 1] == starts[s]) \
            or (e < len(starts) and starts[e] == starts[e - 1]):
        return None
    return (starts[s], ends[e - 1])


def locate_quote_span(body: str, quote: str) -> tuple[int, int] | None:
    """Find quote's UNIQUE codepoint span in body. Ambiguous or absent
    quotes get no span — never a guessed one (INV-07, AT-029).

    Exact match first; if absent, retry after NFKC normalization with
    all whitespace removed — the rule extract_llm's grounded() checks —
    and map the span back to original codepoints. Models routinely emit
    quotes with inserted/altered whitespace or width variants — the
    located span is still unique and the caller stores body[s:e]
    verbatim, so span equality holds."""
    if not body or not quote or not quote.strip():
        return None
    first = body.find(quote)
    if first >= 0 and body.find(quote, first + 1) < 0:
        return (first, first + len(quote))
    if first >= 0:
        return None
    return fold_find(body, quote)


def search_fold(text):
    """Search key: NFKC + casefold + whitespace removed (None stays None)."""
    if not isinstance(text, str):
        return None
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def register_search_fold(db) -> None:
    """Make ``mcs_fold(text)`` (``search_fold``) callable in SQL on this
    connection — read-only connections included; registered once."""
    try:
        db.execute("SELECT mcs_fold('')").fetchone()
    except sqlite3.OperationalError:
        db.create_function("mcs_fold", 1, search_fold, deterministic=True)


def text_chunks(text: str, size: int = 3000) -> list:
    """Split into <=size chunks at line/sentence boundaries, hard-
    splitting only as a last resort. The concatenation of all chunks is
    the original text — full coverage, never head-only processing
    (§12.3, AT-017)."""
    if type(size) is not int or size < 1:
        raise ValueError("chunk_size_must_be_positive_integer")
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
    except BaseException:
        os.close(fd)
        raise
    return fd


_MCS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def code_stamp() -> int:
    """Newest mtime of the mcs/ sources — changes when an update merges."""
    newest = 0
    for base, _dirs, files in os.walk(_MCS_ROOT):
        for name in files:
            if name.endswith(".py"):
                with suppress(OSError):
                    newest = max(newest, os.stat(
                        os.path.join(base, name)).st_mtime_ns)
    return newest


class RunLockLost(BaseException):
    """The run lock could not be safely retaken after unlocked transport.

    ``held`` tells whether the fd holds the lock again (an updater ran in
    the window: old code must not keep writing) or not (bounded wait
    expired: no DB write is allowed at all). BaseException on purpose:
    the generic ``except Exception`` retry paths must not charge an
    attempt for it — the drain catches it explicitly."""

    def __init__(self, held: bool):
        super().__init__("run_lock_held" if held else "run_lock_timeout")
        self.held = held


RUN_LOCK_REACQUIRE_S = 150.0
RUN_LOCK_POLL_S = 0.2


def _updater_state() -> tuple:
    return (os.path.exists(os.path.join(HOME, "data", UPDATE_MARKER_NAME)),
            code_stamp())


@contextmanager
def unlocked_transport(ledger, lock_fd, wait_s: float | None = None):
    """Release the run lock during transport and reacquire before DB work.

    The reacquire is bounded (``wait_s``, default RUN_LOCK_REACQUIRE_S)
    and refuses a lock won right after an updater — same rule as
    run_check._wait_run_lock: an update marker that appeared or a code
    stamp that moved during the window raises RunLockLost(held=True);
    an expired wait raises RunLockLost(held=False)."""
    if lock_fd is None:
        yield
        return
    if ledger.db.in_transaction:
        raise RuntimeError("transport_inside_db_transaction")
    before = _updater_state()
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    try:
        yield
    finally:
        until = time.monotonic() + (
            RUN_LOCK_REACQUIRE_S if wait_s is None else wait_s)
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= until:
                    raise RunLockLost(held=False) from None
                time.sleep(RUN_LOCK_POLL_S)
        if _updater_state() != before:
            raise RunLockLost(held=True)


def launchd_bootstrap(label: str, plist: str, run) -> str | None:
    """Bootstrap a LaunchAgent and verify it is loaded — the canonical
    success criterion for mcs_setup/mcs_update (mcs_recover.py and
    install.sh keep standalone copies of the same semantics).

    launchd may still be tearing down a just-booted-out job and answer
    any nonzero code (typically "5: Input/output error") — any failure
    is retried up to 3 times, 1s apart. An exit 0 is not proof: success
    is only a `launchctl print` of the label answering 0 afterwards (a
    late load after failed attempts also counts). `run(argv)` returns an
    object with .returncode/.stderr (text). Returns None on success, else a
    failure detail."""
    uid = os.getuid()
    err = ""
    for _ in range(3):
        r = run(["launchctl", "bootstrap", f"gui/{uid}", plist])
        if r.returncode == 0:
            err = ""
            break
        err = (r.stderr or "").strip()
        time.sleep(1)
    if run(["launchctl", "print", f"gui/{uid}/{label}"]).returncode == 0:
        return None
    return err or "bootstrap exited 0 but the label is not loaded"
