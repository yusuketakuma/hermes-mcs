"""Shared micro-utilities for the MCS adapter — no network, no DB access.

Centralizes the small pieces several modules used to keep private copies
of: config loading, HTML-to-text, the no-redirect HTTP guard, the
single-writer run lock, shared path constants, and the text utilities
(chunking / evidence-quote location) used by the extract and semantic
layers. Keep this module dependency-free so every entry point
(run_check, init_data, the extract CLIs, notify_flush, mcs_adapter)
can import it without side effects.
"""
import fcntl
import hashlib
import html
import json
import os
import re
import urllib.request

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
        try:
            for line in open(path, encoding="utf-8"):
                if line.startswith(key + "="):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if v:
                        return v
                    # an emptied `KEY=` line is "not configured" — same
                    # as an empty env var, it must not shadow a real
                    # value in a later dotenv file
                    break
        except OSError:
            pass
    return None


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
