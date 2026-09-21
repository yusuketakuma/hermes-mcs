"""Shared micro-utilities for the MCS adapter — no network, no DB access.

Centralizes the small pieces several modules used to keep private copies
of: config loading, HTML-to-text, the no-redirect HTTP guard, and the
single-writer run lock. Keep this module dependency-free so every entry
point (run_check, init_data, the extract CLIs, notifier, mcs_adapter)
can import it without side effects.
"""
import fcntl
import html
import json
import os
import re
import urllib.request

HOME = os.path.expanduser("~/.mcs")
CONF_PATH = os.path.join(HOME, "config.json")
RUN_LOCK = os.path.join(HOME, "data", "run.lock")


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
    """KEY=value lookup shared by notifier/semantic/semantic_jev.

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
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
    return None


def html_to_text(h: str) -> str:
    h = re.sub(r"<br\s*/?>", "\n", h or "")
    h = re.sub(r"</(p|div|li)>", "\n", h)
    return html.unescape(re.sub(r"<[^>]+>", "", h)).strip()


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
