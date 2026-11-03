"""Share dependency-light bootstrap, text, JSON and guarded-opener contracts."""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import shutil
import stat
import sys
import time
import unicodedata
import urllib.request
from contextlib import suppress

from safe_io import read_regular_nofollow


HOME = os.path.abspath(os.path.expanduser(os.environ.get("MCS_ROOT", "~/.mcs")))
# checkout root; scripts/mcs_upgrade.py runs a target tag's code from a
# temp dir and points it at the live checkout through MCS_UPDATE_REPO
REPO = os.path.abspath(os.environ.get("MCS_UPDATE_REPO") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
CONF_PATH = os.path.join(HOME, "config.json")
DB = os.path.join(HOME, "data", "ledger.db")
ATTACH_DIR = os.path.join(HOME, "data", "attachments")
# written under <HOME>/data by mcs_update while an update is applied
CACHE = os.path.join(HOME, "token_cache.json")   # outside data/ (sandbox-mounted)
CHROME_PROFILE = os.path.join(HOME, "chrome-profile")
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


CONFIG_MAX_BYTES = 1024 * 1024
DOTENV_MAX_BYTES = 64 * 1024
_weak_warned: set = set()


def _read_local(path: str, limit: int) -> bytes:
    """Bounded no-follow read of a regular local file; group/world-writable
    or foreign-owned files are read but warned once per path (warn-only
    this release so an update cannot brick an install with loose modes)."""
    raw = read_regular_nofollow(path, limit, ValueError)
    if len(raw) > limit:
        raise ValueError
    # shortcut: lstat after the read can race a swap; it only gates a warning
    with suppress(OSError):
        st = os.lstat(path)
        if (st.st_mode & 0o022 or st.st_uid != os.getuid()) \
                and path not in _weak_warned:
            _weak_warned.add(path)
            print(f"mcs: local_file_permissions_weak ({os.path.basename(path)})",
                  file=sys.stderr)
    return raw


def _provider_artifact_present(data_fd):
    """Find an existing migration remnant without reading its contents."""
    with os.scandir(data_fd) as entries:
        for index, entry in enumerate(entries):
            if index >= 100000:
                raise ValueError("provider_artifact_scan_limit")
            if entry.name.startswith((".provider-settings-backup-", ".provider-settings-stage-")):
                return True
    return False


def _provider_config_active(path):
    path = os.path.abspath(path)
    if os.path.basename(path) != "config.json":
        return False
    data = os.path.join(os.path.dirname(path), "data")
    for name in (".provider-settings-authority.json", ".provider-settings-pending.json", "provider_settings"):
        try:
            os.lstat(os.path.join(data, name))
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ProviderSettingsUnreadable("provider_settings_unreadable") from error
        return True
    def identity(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_mtime_ns, info.st_ctime_ns)
    retry_identity = None
    for attempt in range(2):
        try:
            try:
                fd = os.open(data, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            except FileNotFoundError:
                if retry_identity is not None:
                    raise ValueError("provider_settings_changed") from None
                if os.path.lexists(data):
                    raise ValueError("provider_settings_unreadable") from None
                return False
            try:
                before = os.fstat(fd)
                if retry_identity is not None and identity(before)[:4] != retry_identity:
                    raise ValueError("provider_settings_changed")
                present = _provider_artifact_present(fd)
                for name in (".provider-settings-authority.json", ".provider-settings-pending.json", "provider_settings"):
                    try:
                        os.stat(name, dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    present = True
                    break
                after = os.fstat(fd)
                current = os.stat(data, follow_symlinks=False)
                if identity(before) != identity(after) or identity(after) != identity(current):
                    # Runtime files also change directory times; never rebaseline ownership or replacement.
                    if identity(before)[:4] != identity(after)[:4] \
                            or identity(after)[:4] != identity(current)[:4]:
                        raise ValueError("provider_settings_changed")
                    if attempt == 0:
                        retry_identity = identity(before)[:4]
                        continue
                    # device/inode/mode/owner stayed equal through the whole
                    # scan on one pinned fd: churn elsewhere in data/ moved
                    # only the directory's times, so the result is consistent.
                    return present
                return present
            finally:
                os.close(fd)
        except (OSError, ValueError) as error:
            raise ProviderSettingsUnreadable("provider_settings_unreadable") from error


class ProviderSettingsUnreadable(RuntimeError):
    """Provider settings cannot be read consistently and must not default open."""


# Read failures that can occur inside a commit's write window: bounded retry
# absorbs the blip; anything else (or a persistent failure) stays fail-closed.
_TRANSIENT_PROVIDER_READ = frozenset({
    "provider_settings_changed", "provider_settings_pending",
    "provider_file_changed", "provider_file_missing", "provider_snapshot_changed"})


def _load_config(path, *, allow_provider_unavailable=False):
    active = _provider_config_active(path)
    if active:
        try:
            import cloud_policy
            import provider_settings
            import semantic_jev

            owners = {model: name for name, model in cloud_policy.PROVIDERS.items()}
            root = os.path.dirname(os.path.abspath(path))
            reader = (provider_settings.read_with_verified_common if allow_provider_unavailable
                      else provider_settings.read)
            for attempt in range(3):
                try:
                    state = reader(root, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
                    break
                except ValueError as error:
                    if str(error) not in _TRANSIENT_PROVIDER_READ or attempt == 2:
                        raise
                    # A commit keeps readers closed for its whole write window:
                    # wait briefly and re-verify rather than fail on the blip.
                    time.sleep(0.15)
            if state["mode"] == "provider_unavailable":
                cfg = dict(state["common"])
                cloud = cfg.get("cloud_llm", {})
                semantic = cfg.get("semantic", {})
                if isinstance(cloud, dict):
                    cfg["cloud_llm"] = {**cloud, "mode": "off"}
                if isinstance(semantic, dict):
                    cfg["semantic"] = {**semantic, "daily_request_budget": 0}
                return cfg
            if state["mode"] != "split":
                raise ValueError("provider_settings_unverified")
            selected, errors = cloud_policy.resolve_settings(state["common"], root)
            if cloud_policy.blocking_errors(errors):
                raise ValueError("provider_settings_invalid")
            return provider_settings.execution_view(state["common"], state["providers"],
                selected_provider=selected["provider"], model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
        except (OSError, ValueError, TypeError, RecursionError) as error:
            raise ProviderSettingsUnreadable("provider_settings_unreadable") from error
    try:
        raw = parse_config(_read_local(path, CONFIG_MAX_BYTES))
    finally:
        if _provider_config_active(path):
            raise ProviderSettingsUnreadable("provider_settings_changed")
    return raw if isinstance(raw, dict) else None


def load_config_checked(path: str = CONF_PATH) -> dict | None:
    """Return validated composite settings, or None when a consistent read fails."""
    try:
        return _load_config(path)
    except (ProviderSettingsUnreadable, OSError, ValueError, TypeError, RecursionError):
        return None


def load_config(path: str = CONF_PATH) -> dict:
    """config.json -> dict; missing/malformed files fall back to defaults."""
    try:
        config = _load_config(path, allow_provider_unavailable=True)
        return config if config is not None else {}
    except (OSError, ValueError, TypeError, RecursionError):
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
        # an undecodable (non-UTF-8) dotenv is skipped like an unreadable one
        with suppress(OSError, ValueError):
            for line in _read_local(path, DOTENV_MAX_BYTES).decode(
                    "utf-8").splitlines(keepends=True):
                if line.startswith(key + "="):
                    v = line.split("=", 1)[1].strip()
                    # decodes lifecycle_io.write_env's quoting; the standalone
                    # credential loader (mcs_standalone.config._env_tokens)
                    # keeps a stricter copy that fails closed on bad JSON
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


def _private_env_lookup(key: str, root=None) -> tuple:
    """(value, None) or (None, fixed reason code); the code never carries file content."""
    from safe_io import fdopen_owned
    try:
        fd = os.open(os.path.join(root or HOME, ".env"),
                     os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, "env_file_missing"
    except OSError:
        return None, "env_file_unreadable"
    try:
        with fdopen_owned(fd, "rb") as stream:
            st = os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode):
                return None, "env_file_not_regular"
            if st.st_uid != os.getuid():
                return None, "env_file_owner"
            if stat.S_IMODE(st.st_mode) != 0o600:
                return None, "env_file_mode_not_0600"
            if st.st_size > DOTENV_MAX_BYTES:
                return None, "env_file_too_large"
            raw = stream.read(DOTENV_MAX_BYTES + 1)
        if len(raw) > DOTENV_MAX_BYTES:
            return None, "env_file_too_large"
        lines = raw.decode("utf-8").splitlines()
    except OSError:
        return None, "env_file_unreadable"
    except UnicodeError:
        return None, "env_file_not_utf8"
    matches = [line.split("=", 1)[1] for line in lines if line.startswith(key + "=")]
    if not matches:
        exported = any(line.startswith("export " + key + "=") for line in lines)
        return None, "key_export_prefix" if exported else "key_missing"
    if len(matches) != 1:
        return None, "key_duplicate"
    value = matches[0]
    try:
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = json.loads(value) if value[0] == '"' else value[1:-1]
    except ValueError:
        return None, "key_quote_invalid"
    if not isinstance(value, str) or not value:
        return None, "key_empty"
    if any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in value):
        return None, "key_whitespace_or_control"
    return value, None


def until_epoch(value) -> float:
    """Epoch seconds of a configured ISO 8601 end time that names its UTC offset."""
    from datetime import datetime
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("until_invalid")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("until_needs_offset")
    return parsed.timestamp()


def private_env_value(key: str, root=None) -> str | None:
    """Read a provider key from an owner-only regular MCS dotenv, never the environment."""
    return _private_env_lookup(key, root)[0]


def private_env_problem(key: str, root=None) -> str | None:
    """Why private_env_value returns None for this key, as a fixed code; None when it is usable."""
    return _private_env_lookup(key, root)[1]


# 4xx statuses that mean "try again later", not a permanent refusal
# (408 timeout, 425 too early, 429 rate limit)
RETRY_LATER_4XX = frozenset({408, 425, 429})


def hermes_exe(cfg: dict, path: str | None = None) -> str:
    """The `hermes` binary: config hermes_bin, else PATH (or `path`), else
    the standard user-local install — the launchd PATH is minimal."""
    exe = cfg.get("hermes_bin")
    if isinstance(exe, str) and exe.strip():
        return exe.strip()
    return shutil.which("hermes", os.F_OK | os.X_OK, path) \
        or os.path.expanduser("~/.local/bin/hermes")


def has_sqlite_sidecars(path) -> bool:
    """True when a -wal, -shm or -journal entry (even a dangling symlink)
    sits next to ``path`` — the file is then not a static snapshot."""
    return any(os.path.lexists(f"{path}{suffix}")
               for suffix in ("-wal", "-shm", "-journal"))


def file_sha256(path) -> str:
    """Streaming sha256 of a file — shared by operations/refstats so the
    digest contract (chunks of 64 KiB, hex digest) is defined once."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def text_sha256(text: str) -> str:
    """Hex sha256 of a string's UTF-8 bytes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical(value) -> bytes:
    """Canonical JSON bytes (sorted keys, compact, UTF-8, no NaN/inf) —
    the one serialization every stored payload hash is computed over."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def payload_hash(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def positive(value) -> bool:
    """A positive SQLite rowid-range int (bool excluded)."""
    return type(value) is int and 0 < value < 2**63


def valid_text(value, size, nullable=False):
    return (nullable and value is None) or (
        isinstance(value, str) and 0 < len(value) <= size
        and bool(value.strip()) and "\x00" not in value)



def valid_hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


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


def unique_pairs(items):
    """``object_pairs_hook`` that rejects a repeated key as ``duplicate_key``."""
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def reject_constant(_):
    """``parse_constant`` hook that rejects NaN/Infinity as ``invalid_number``."""
    raise ValueError("invalid_number")


def parse_config(raw: bytes):
    """The one strict config.json parse every reader shares: a repeated key
    or NaN/Infinity is a ValueError, never a silently kept value."""
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs,
                      parse_constant=reject_constant)


def strict_json(raw):
    """Parse JSON rejecting duplicate keys and nonfinite numbers, including overflowing floats."""
    # distinct codes from unique_pairs/reject_constant; semantic_candidate keeps its own copy
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("nonfinite_json_number")

    def number(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("nonfinite_json_number")
        return result
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)


def safe_error_code(exc) -> str:
    """Error code safe to log: a code-shaped ValueError message, else the type name."""
    # user text/SQL/paths must never be interpolated into error logs
    return str(exc) if type(exc) is ValueError and \
        str(exc).replace("_", "").isalnum() else type(exc).__name__


def html_to_text(h: str) -> str:
    h = re.sub(r"<br\s*/?>", "\n", h or "")
    h = re.sub(r"</(p|div|li)>", "\n", h)
    # An unmatched trailing '<' can never match the original tag pattern.
    # Excluding that suffix avoids retrying the regex from every '<' in it.
    last_close = h.rfind(">")
    prefix, suffix = h[:last_close + 1], h[last_close + 1:]
    return html.unescape(re.sub(r"<[^>]+>", "", prefix) + suffix).strip()


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
        start = 0
        while len(seg) - start > size:
            cut = max(seg.rfind("。", start, start + size),
                      seg.rfind("\n", start, start + size)) + 1
            if cut <= start:
                cut = start + size
            out.append(seg[start:cut])
            start = cut
        buf = seg[start:]
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
