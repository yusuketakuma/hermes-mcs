"""Recover MCS browser sessions with owned CDP peers and bounded credential retry state."""
from __future__ import annotations

from contextlib import suppress
import hashlib
import json
import os
import re
import time
import urllib.parse

from mcs_contract import BASE, BootstrapError, MCSError
from mcs_util import HOME, env_value, load_config
from safe_io import atomic_write

HUNG_PROBE_S = 5            # a renderer silent this long on `1` is hung


LOGIN_BACKOFF_BASE_S = 3600


LOGIN_BACKOFF_MAX_S = 86400


# CDP page targets on the MCS host (derived from BASE, never a second literal)
_MCS_HOST = urllib.parse.urlparse(BASE).hostname


def _is_mcs_page(t) -> bool:
    """A CDP target that is a page on the MCS host."""
    return t.get("type") == "page" and urllib.parse.urlparse(t.get("url", "")).hostname == _MCS_HOST


class KeychainLocked(Exception):
    """The Keychain entry exists but its secret cannot be read — the
    login keychain is locked (or this context may not interact).
    Distinct from 'missing': recovery is an unlock, not
    re-provisioning."""


def _login_accounts() -> list:
    import pwd
    return pwd.getpwall()


def _other_login_accounts() -> int:
    """Count other local accounts that can log in (they could drive the CDP port)."""
    import sys
    floor = 501 if sys.platform == "darwin" else 1000
    me = os.getuid()
    # no uid ceiling: directory accounts can have large uids; nobody-style
    # service accounts are excluded by their nologin/false shell
    return sum(1 for p in _login_accounts()
               if p.pw_uid >= floor and p.pw_uid != me
               and not p.pw_shell.endswith(("nologin", "false")))


class MCSAuthenticationMixin:
    """Own browser and credential recovery while the adapter retains bounded worker I/O."""

    def _cdp_up(self) -> bool:
        try:
            self._cdp_json("/json/version", timeout=3)
            return True
        except (OSError, MCSError):
            return False

    def _ensure_chrome(self, profile_dir: str, chrome_bin: str):
        if self._cdp_up():
            # the browser process answers /json/version even when an MCS
            # tab's renderer is hung — such a tab makes every token read
            # and form fill time out, so heal it before using the browser
            self._heal_hung_pages()
            return
        import subprocess
        self._remaining_timeout(1)
        # CDP is down: a hung browser, or this profile opened without the
        # debugging port, would absorb the relaunch (same-profile hand-off)
        # and CDP would never come up — stop that instance first
        self._stop_profile_chrome(profile_dir, chrome_bin)
        subprocess.Popen([
            chrome_bin,
            f"--remote-debugging-port={urllib.parse.urlparse(self.cdp_url).port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run", "--no-default-browser-check",
            # the profile must not pull the ~4 GB on-device AI model
            "--disable-features=OptimizationGuideModelDownloading,"
            "OptimizationHintsFetching,OptimizationGuideOnDeviceModel",
            f"{BASE}/authentication/login"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            self._sleep_bounded(1)
            self._remaining_timeout(3)
            if self._cdp_up():
                return
        raise BootstrapError("chrome launch timed out")

    @staticmethod
    def _profile_chrome_pids(profile_dir: str, chrome_bin: str) -> list[int]:
        from mcs_cdp import profile_chrome_pids
        return profile_chrome_pids(profile_dir, chrome_bin)

    def _cdp_peer_owned(self, ws_url: str, profile_dir: str,
                        chrome_bin: str) -> bool:
        """True only when ws_url is the CDP endpoint and its listener is this profile's Chrome run by this OS user.

        A loopback address does not identify the peer: another local
        account could hold the CDP port while the profile's Chrome is
        down and answer every probe itself. Fails closed when the socket
        listing is unavailable (no lsof, timeout, unparseable output)."""
        import subprocess
        try:
            cdp = urllib.parse.urlparse(self.cdp_url)
            ws = urllib.parse.urlparse(ws_url)
            if (cdp.port is None or ws.scheme != "ws"
                    or (ws.hostname, ws.port) != (cdp.hostname, cdp.port)):
                return False
        except ValueError:
            return False
        pids = self._profile_chrome_pids(profile_dir, chrome_bin)
        lsof = next((p for p in ("/usr/sbin/lsof", "/usr/bin/lsof")
                     if os.access(p, os.X_OK)), None)
        if not pids or lsof is None:
            return False
        host = f"[{cdp.hostname}]" if ":" in cdp.hostname else cdp.hostname
        try:
            listing = subprocess.run(
                [lsof, "-nP", "-a", f"-iTCP@{host}:{cdp.port}",
                 "-sTCP:LISTEN", "-Fpu"], capture_output=True, text=True,
                timeout=self._remaining_timeout(10))
        except (OSError, subprocess.SubprocessError):
            return False
        if listing.returncode != 0:
            return False
        listeners, pid = set(), None
        for line in listing.stdout.splitlines():
            if line[:1] == "p" and line[1:].isdigit():
                pid = int(line[1:])
            elif line[:1] == "u" and line[1:].isdigit() and pid is not None:
                listeners.add((pid, int(line[1:])))
        return bool(listeners) and all(
            pid in pids and uid == os.getuid() for pid, uid in listeners)

    def _stop_profile_chrome(self, profile_dir: str, chrome_bin: str) -> bool:
        """Stop Chrome main processes of exactly this dedicated profile
        (helpers exit with them): SIGTERM, bounded wait, then SIGKILL.
        Nothing else is touched; an unparseable listing stops nothing."""
        import signal
        pids = self._profile_chrome_pids(profile_dir, chrome_bin)
        if not pids:
            return False

        def alive():
            out = []
            for pid in pids:
                try:
                    os.kill(pid, 0)
                    out.append(pid)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    out.append(pid)
            return out

        for sig, wait_s in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
            for pid in alive():
                with suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, sig)
            for _ in range(wait_s * 2):
                if not alive():
                    break
                self._sleep_bounded(0.5)
            if not alive():
                break
        self.chrome_restarted = True
        return True

    def _mcs_pages(self) -> list:
        return [t for t in self._cdp_json("/json/list")
                # id first: a malformed URL is only parsed for a well-formed page id, as before
                if isinstance(t.get("id"), str) and re.fullmatch(r"[0-9A-Fa-f]{16,64}", t["id"])
                and _is_mcs_page(t)]

    def _heal_hung_pages(self) -> int:
        """Close MCS tabs whose renderer no longer answers a trivial
        evaluate (browser side still up). Only www.medical-care.net tabs
        of this dedicated profile; a run deadline is not a hang. Returns
        the number of tabs verified gone — callers reopen a fresh tab."""
        self.healed_tabs = 0
        try:
            hung = []
            for page in self._mcs_pages():
                try:
                    self._cdp_eval(page["webSocketDebuggerUrl"], "1", HUNG_PROBE_S)
                except MCSError as error:
                    if error.kind == "network_error":
                        hung.append(page["id"])
            if not hung:
                return 0
            for target in hung:
                # /json/close answers plain text, which the JSON worker
                # rejects — the result is verified by re-listing instead
                with suppress(MCSError, OSError):
                    self._cdp_json(f"/json/close/{target}")
            alive = {t["id"] for t in self._mcs_pages()}
        except (MCSError, OSError, KeyError, TypeError):
            return 0
        self.healed_tabs = sum(target not in alive for target in hung)
        return self.healed_tabs

    def _cdp_eval(self, ws_url: str, expr: str, timeout: int = 15):
        return self._io("cdp_eval", timeout, url=ws_url, expression=expr)["value"]

    def _cdp_eval_owned(self, ws_url: str, profile_dir: str,
                        chrome_bin: str, expr: str):
        return self._io("cdp_eval", 15, url=ws_url, expression=expr,
                        peer_profile={"profile_dir": profile_dir,
                                      "chrome_bin": chrome_bin})["value"]

    def _cdp_json(self, path: str, method: str = "GET", timeout: float = 5):
        return self._io("cdp_json", timeout, url=self.cdp_url + path,
                        method=method)["value"]

    def _login_page(self):
        """Pick the login tab by STRICT origin+path — never fill credentials
        into a lookalike path on another host (Oracle B14)."""
        targets = self._cdp_json("/json/list")
        for t in targets:
            if t.get("type") != "page":
                continue
            u = urllib.parse.urlparse(t.get("url", ""))
            if (u.scheme == "https"
                    and u.hostname == _MCS_HOST
                    and "authentication/login" in u.path):
                return t
        # reuse an existing mcs tab or open one
        for t in targets:
            if _is_mcs_page(t):
                self._cdp_eval(t["webSocketDebuggerUrl"],
                               f"location.href='{BASE}/authentication/login'")
                self._sleep_bounded(4)
                return t
        page = self._cdp_json(f"/json/new?{BASE}/authentication/login", method="PUT")
        self._sleep_bounded(4)
        return page

    def _keychain_password(self, service: str = "mcs-adapter") -> str | None:
        """Fetch MCS password from macOS Keychain. First call may show an ACL
        prompt — 'Always Allow' makes subsequent reads silent. Returns None if
        the entry doesn't exist or access is denied. Raises KeychainLocked
        when the item is unreadable because the keychain is locked (or UI
        interaction is unavailable) — a recoverable operational state that
        must not be conflated with a missing credential."""
        import subprocess
        try:
            r = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-w"],
                capture_output=True, text=True, timeout=self._remaining_timeout(15))
        except subprocess.TimeoutExpired:
            self._remaining_timeout(1)
            # A stalled ACL prompt is temporarily unreadable. Preserve the
            # existing .env fallback only while the run still has time.
            raise KeychainLocked(service) from None
        if r.returncode == 0:
            return r.stdout.strip() or None
        err = (r.stderr or "").lower()
        if (r.returncode == 36 or "interaction is not allowed" in err
                or "keychain is locked" in err):
            raise KeychainLocked(service)
        return None

    def _login_password(self) -> tuple[str | None, bool]:
        """MCS password for form fill — (password, keychain_was_locked).
        Keychain is primary; when it is locked or lacks the entry,
        MCS_PASSWORD in ~/.mcs/.env is the fallback so a rebooted Mac
        (login keychain locked until first unlock) can still re-login.
        Plaintext-at-rest is the accepted tradeoff — FileVault or
        physical security is assumed; the keychain remains preferred
        whenever it is readable."""
        try:
            pw = self._keychain_password()
            locked = False
        except KeychainLocked:
            pw, locked = None, True
        self._pw_source = "keychain" if pw else None
        if not pw:
            pw = env_value("MCS_PASSWORD")
            self._pw_source = "env" if pw else None
        return pw, locked

    def _config(self) -> dict:
        return load_config()

    def _recover_session(self) -> bool:
        """A still-valid browser session recovers a run with zero form
        interaction — API token expiry does not imply logout; cookies
        can keep the app session alive (or a manual/concurrent login
        may have landed). A fresh localStorage token that passes
        check_session is enough."""
        try:
            tok = self._token_via_cdp()
        except Exception:
            return False
        self._token = tok
        with suppress(Exception):
            if self.check_session():
                self._write_cache(tok)
                return True
        return False

    def _read_login_state(self) -> dict:
        if not self._login_state:
            return {}
        try:
            with open(self._login_state, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError, RecursionError):
            return {}
        return state if isinstance(state, dict) else {}

    def _write_login_state(self, state: dict | None) -> None:
        """Persist (or clear, with None) the login outcome; best effort."""
        if not self._login_state:
            return
        with suppress(OSError):
            if state is None:
                os.unlink(self._login_state)
                return
            text = json.dumps(state)
            atomic_write(self._login_state, lambda f: f.write(text), mode=0o600)

    def _keychain_revision(self, service: str = "mcs-adapter") -> str | None:
        """The Keychain item's modification date ("mdat"), read without
        the secret (no -w): it moves when the stored password is
        replaced. None when the item or the tool is unavailable."""
        import subprocess
        try:
            r = subprocess.run(
                ["security", "find-generic-password", "-s", service],
                capture_output=True, text=True,
                timeout=self._remaining_timeout(15))
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode != 0:
            return None
        for line in (r.stdout or "").splitlines():
            if '"mdat"' in line:
                return line.strip()[:120]
        return None

    @staticmethod
    def _env_revision() -> str:
        """Where MCS_PASSWORD comes from and when those files last changed.

        A value only the process environment holds has no file to stamp;
        it can change only when the supervisor that passes it restarts,
        so the parent process identifies that revision."""
        env = os.environ.get("MCS_PASSWORD")
        if not env:
            stamps = ["-"]
        elif env == env_value("MCS_PASSWORD", check_env=False):
            stamps = ["environ"]           # mirrored from a stamped file
        else:
            stamps = [f"environ:{os.getppid()}"]
        for path in (os.path.join(HOME, ".env"),
                     os.path.expanduser("~/.hermes/.env")):
            try:
                stamps.append(str(os.stat(path).st_mtime_ns))
            except OSError:
                stamps.append("-")
        return ":".join(stamps)

    def _credential_fp(self, login_id, source: str | None) -> str:
        """Identifies the credential by where it is stored and when that
        store last changed — never by the password, so the record gives
        nothing to test a guessed password against."""
        if source == "keychain":
            revision = self._keychain_revision() or "unknown"
        elif source == "env":
            revision = self._env_revision()
        else:
            revision = "browser"   # the browser filled the password itself
        msg = json.dumps([login_id, source, revision], ensure_ascii=False)
        return hashlib.sha256(msg.encode()).hexdigest()[:16]

    def _login_blocked(self, login_id, source: str | None) -> tuple[bool, dict]:
        """(blocked, state) — blocked while the last submit of THIS
        credential was rejected less than the backoff ago."""
        state = self._read_login_state()
        fp = self._credential_fp(login_id, source)
        failures, at = state.get("failures"), state.get("last_failed_at")
        if (state.get("credential") != fp or type(failures) is not int
                or failures < 1 or type(at) not in (int, float)):
            return False, {"credential": fp}
        backoff = min(LOGIN_BACKOFF_MAX_S,
                      LOGIN_BACKOFF_BASE_S * 2 ** min(failures - 1, 16))
        return 0 <= time.time() - at < backoff, state

    def _login_rejected(self, state: dict) -> None:
        """Record one submitted login that never produced a session."""
        failures = state.get("failures")
        state = dict(state, failures=(failures + 1 if type(failures) is int
                                      and failures >= 0 else 1),
                     last_failed_at=time.time())
        self._write_login_state(state)

    def auto_login(self, profile_dir: str = "", chrome_bin: str = "",
                   wait_s: int = 45) -> str:
        """Recover the session, filling the login form only when needed.

        Same states as _auto_login, plus 'manual_required:backoff' when
        the form was not sent because this credential was rejected less
        than the backoff ago (the manual-login alert still goes out). A
        successful login of any kind clears the rejection record."""
        state = self._auto_login(profile_dir, chrome_bin, wait_s)
        if state == "ok":
            # any working session ends a rejected-login episode
            self._write_login_state(None)
        return state

    def _auto_login(self, profile_dir: str = "", chrome_bin: str = "",
                    wait_s: int = 45) -> str:
        """Re-login by filling the form from macOS Keychain and clicking
        submit. Native Chrome autofill can't be driven via DOM, so the
        credential lives in Keychain (service 'mcs-adapter'); the adapter
        reads it at runtime and injects via input events. loginId is
        persisted by the app itself; if empty we fill from config
        'mcs_login_id'. Password is never logged or stored by us.

        Returns 'ok' | 'manual_required' | 'keychain_locked' | 'failed'.
        'manual_required:no_form' distinguishes a missing login form
        (page never rendered / app redirected) from a submitted login
        that never validated; 'manual_required:multi_user_host' means
        another local account can log in, so the CDP Chrome is not used. Credentials are filled only into a CDP
        listener verified as this profile's Chrome (_cdp_peer_owned);
        any other peer ends the attempt as 'failed'."""
        # per-attempt journal facts (run_check records them)
        self.healed_tabs, self.chrome_restarted = 0, False
        if _other_login_accounts():
            # the CDP port is reachable by every local account: never open
            # it or type the password while another account can log in
            return "manual_required:multi_user_host"
        try:
            self._ensure_chrome(profile_dir, chrome_bin)
        except (MCSError, OSError):  # incl. deadline_exceeded
            return "failed"
        # cheapest recovery first — before touching the login form at
        # all, a live session (fresh token + valid API check) ends it
        if self._recover_session():
            return "ok"
        try:
            page = self._login_page()
            ws = page["webSocketDebuggerUrl"]
            self._sleep_bounded(3)  # Angular render
            state = self._cdp_eval(ws, """(() => {
              if (location.origin !== 'https://www.medical-care.net')
                return 'bad_origin';
              const id = document.querySelector('input[name=loginId]');
              const pw = document.querySelector('input[type=password]');
              if (!pw) return 'no_form';
              if (pw.value) return 'ready';
              return (id && id.value) ? 'need_pw' : 'need_both';
            })()""")
            if state == "bad_origin":
                return "failed"
            if state == "no_form":
                # a logged-in app redirects /authentication/login back
                # home — no form then MEANS a session, verify before
                # escalating to a manual alert
                if self._recover_session():
                    return "ok"
                return "manual_required:no_form"
            login_id = self._config().get("mcs_login_id", "")
            if state == "ready":
                # browser-filled password: only the login id is known
                blocked, login = self._login_blocked(login_id, "browser")
                if blocked:
                    return "manual_required:backoff"
            else:
                pw, locked = self._login_password()
                if not pw:
                    return "keychain_locked" if locked else \
                        "manual_required"
                blocked, login = self._login_blocked(
                    login_id, getattr(self, "_pw_source", None))
                if blocked:
                    return "manual_required:backoff"
                if not self._cdp_peer_owned(ws, profile_dir, chrome_bin):
                    return "failed"
                fill = self._cdp_eval_owned(ws, profile_dir, chrome_bin, f"""(() => {{
                  if (location.origin !== 'https://www.medical-care.net')
                    return 'bad_origin';
                  const idEl = document.querySelector('input[name=loginId]');
                  const pwEl = document.querySelector('input[type=password]');
                  if (idEl && !idEl.value) {{
                    idEl.value = {json.dumps(login_id)};
                    idEl.dispatchEvent(new Event('input', {{bubbles:true}}));
                  }}
                  pwEl.value = {json.dumps(pw)};
                  pwEl.dispatchEvent(new Event('input', {{bubbles:true}}));
                  return 'filled';
                }})()""")
                if fill != "filled":
                    return "failed"
            clicked = self._cdp_eval(ws, """(() => {
              if (location.origin !== 'https://www.medical-care.net')
                return 'bad_origin';
              const btn = document.querySelector(
                'button[type=submit],input[type=submit],' +
                'form button:not([type=button])');
              if (!btn) return 'no_button';
              btn.click();
              return 'clicked';
            })()""")
            if clicked != "clicked":
                return "failed"
        except Exception:
            return "failed"
        # poll for a fresh session token
        deadline = time.monotonic() + wait_s
        if self._deadline is not None:
            deadline = min(deadline, self._deadline)
        while time.monotonic() < deadline:
            self._sleep_bounded(min(2, max(0, deadline - time.monotonic())))
            try:
                tok = self._token_via_cdp()
            except Exception:
                continue
            if tok:
                self._token = tok
                try:
                    ok = self.check_session()
                except MCSError:
                    return "failed"
                if ok:
                    self._write_cache(tok)
                    return "ok"
                # token present but not yet valid, or MFA page — keep waiting
        # submitted and never validated: this credential waits out the
        # backoff before the form is sent again (H5)
        self._login_rejected(login)
        return "manual_required"
