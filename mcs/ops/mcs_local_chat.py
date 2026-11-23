#!/usr/bin/env python3
"""Serve an explicitly scoped, read-only patient chat on this computer only."""
import argparse
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
from socketserver import TCPServer
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _mcs_path  # noqa: F401
from mcs_config import REMOTE_ACCESS_PORT, TAILNET_HOST, remote_access
from mcs_view import View
import local_chat_view
import homecare_api
import homecare_catalog
import homecare_records
import homecare_route
import homecare_store

ASSETS = Path(__file__).resolve().parents[1] / "views" / "local_chat"
MAX_BODY = 64 * 1024
# Resident mode sits behind `tailscale serve`, which strips any copy a client sends and sets this for tailnet users.
IDENTITY = "Tailscale-User-Login"
UNREGISTERED = "このTailscaleのアカウントは職員として登録されていません。管理者に職員の登録を頼んでください。".encode()
# What an unregistered login may reach while the register is empty: enough to register the first person.
FIRST_ROUTES = frozenset({"api/session", "api/hc/meta", "api/hc/staff", "api/hc/staff.create"})
FILES = {"": ("index.html", "text/html; charset=utf-8"),
         "app.js": ("app.js", "text/javascript; charset=utf-8"),
         "homecare.js": ("homecare.js", "text/javascript; charset=utf-8"),
         "pharmacy.js": ("pharmacy.js", "text/javascript; charset=utf-8"),
         "style.css": ("style.css", "text/css; charset=utf-8"),
         "mcs.css": ("mcs.css", "text/css; charset=utf-8"),
         "homecare.css": ("homecare.css", "text/css; charset=utf-8")}


def _one(params, name, default=None):
    values = params.pop(name, [default])
    if len(values) != 1:
        raise ValueError("bad_arguments")
    return values[0]


def response(snapshot, allowed, route, query):
    params = parse_qs(query, keep_blank_values=True, max_num_fields=10)
    cursor = _one(params, "cursor") or None
    text = _one(params, "q", "")
    if len(text) > 500:
        raise ValueError("bad_query")
    project = _one(params, "project")
    message = _one(params, "message")
    section = _one(params, "section")
    page = int(_one(params, "page", "0"))
    role = _one(params, "role", "")
    since = _one(params, "since")
    if params or route != "panel" and (section is not None or page or role) \
            or since is not None and (route not in ("brief", "panel") or not (since.isascii() and since.isdigit())):
        raise ValueError("bad_arguments")
    since = None if since is None else int(since)
    if route in ("sections", "drugs"):
        if project is not None or message is not None or cursor:
            raise ValueError("bad_arguments")
        return local_chat_view.sections() if route == "sections" else local_chat_view.drugs(text)
    if route != "patients":
        project = int(project or "")
        local_chat_view.require_project(project, allowed)
    view = View(snapshot)
    try:
        if route == "patients":
            return local_chat_view.patients(view, allowed, query=text, cursor=cursor)
        if route in ("counts", "registered"):
            if text or cursor or message:
                raise ValueError("bad_arguments")
            return getattr(local_chat_view, route)(view, allowed, project)
        if route == "sources":
            if message:
                raise ValueError("bad_arguments")
            return local_chat_view.source_search(view, allowed, project, text, cursor=cursor)
        if route == "brief":
            if text or cursor or message:
                raise ValueError("bad_arguments")
            return local_chat_view.brief(view, allowed, project, since=since)
        if route == "panel":
            if text or cursor:
                raise ValueError("bad_arguments")
            return local_chat_view.panel(view, allowed, project, section, page=page, role=role,
                                         message_id=int(message) if message else None, since=since)
        return local_chat_view.read(view, allowed, route, project, query=text,
                                   cursor=cursor, message_id=int(message) if message else None)
    finally:
        view.close()


def mcs_patients(snapshot, allowed):
    view = View(snapshot)
    try:
        return local_chat_view.patient_names(view, allowed)
    finally:
        view.close()


def homecare_response(server, method, route, query=None, body=None):
    """The pharmacy records under api/hc/; a fresh connection per request keeps threads apart."""
    if server.homecare is None:
        raise ValueError("bad_request")
    registry = server.homecare_registry
    db = homecare_store.connect(server.homecare, registry.modules)
    try:
        if method == "GET":
            params = parse_qs(query, keep_blank_values=True, max_num_fields=5)
            params = {key: _one(params, key) for key in list(params)}
            return homecare_api.read(db, route, params, registry=registry,
                                     mcs_patients=lambda: mcs_patients(server.snapshot, server.allowed))
        return homecare_api.write(db, route, body, registry=registry, allowed=server.allowed,
                                  mcs_patients=lambda: mcs_patients(server.snapshot, server.allowed),
                                  route_settings=server.route_settings)
    finally:
        db.close()


def resident_user(server, login):
    """``(staff, empty)`` for a resident request: the active staff member of this login, and whether the
    ledger has no staff yet (then only the first registration may proceed)."""
    db = homecare_store.connect(server.homecare, server.homecare_registry.modules)
    try:
        return homecare_records.staff_for_login(db, login), not homecare_records.any_staff(db)
    finally:
        db.close()


class ChatServer(ThreadingHTTPServer):
    """Serves one launch: on this computer behind a per-launch capability URL, or resident behind
    ``tailscale serve`` (``tailnet_host``), where each request is a registered staff member's Tailscale login."""

    def __init__(self, snapshot, allowed, port=0, homecare=None, modules=("pharmacy",), route_settings=None,
                 tailnet_host=None):
        if tailnet_host is not None and (homecare is None or not port or not TAILNET_HOST.match(tailnet_host)):
            raise ValueError("resident mode needs a home-care ledger, a fixed port and a *.ts.net name")
        self.tailnet_host = tailnet_host
        self.snapshot = Path(snapshot)
        self.allowed = None if allowed is None else frozenset(allowed)
        self.token = secrets.token_urlsafe(32)
        # Writes carry this per-launch value in a header that a cross-site page cannot read or set.
        self.csrf = secrets.token_urlsafe(32)
        self.homecare = None if homecare is None else Path(homecare)
        self.homecare_registry = homecare_catalog.registry(modules)
        self.route_settings = route_settings
        if self.homecare is not None:
            homecare_store.connect(self.homecare, self.homecare_registry.modules).close()
        super().__init__(("127.0.0.1", port), ChatHandler)

    def server_bind(self):
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address

    @property
    def url(self):
        if self.tailnet_host:
            return f"https://{self.tailnet_host}/"
        return f"http://127.0.0.1:{self.server_port}/{self.token}/"

    def handle_error(self, request, client_address):
        # No request paths, patient data or exceptions in console logs.
        pass


class ChatHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(10)
        super().setup()

    # URLs contain a per-launch capability; never write them or clinical queries to logs.
    def log_message(self, *_args):
        pass

    def _send(self, status, data=b"", mime="text/plain; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; "
                         "style-src 'self'; connect-src 'self'; img-src 'self'; "
                         "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _route(self, *, write=False):
        """The route after the capability prefix, or None once a refusal was sent.

        Resident mode has no capability: the page is the tailnet name's root, and every request must carry one
        Tailscale login that belongs to active staff (``self.user``), except the very first registration."""
        local, resident = f"127.0.0.1:{self.server.server_port}", self.server.tailnet_host
        hosts, page_origin = ([resident, local], "https://" + resident) if resident else ([local], "http://" + local)
        origin = self.headers.get("Origin")
        host = self.headers.get_all("Host") or []
        if len(host) != 1 or host[0] not in hosts or origin not in (None, page_origin) \
                or self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none") \
                or write and (origin is None or self.headers.get("Sec-Fetch-Site") not in (None, "same-origin")):
            self._send(403)
            return None
        if len(self.path) > 8192:
            self._send(414)
            return None
        parts = urlsplit(self.path)
        prefix = "/" if resident else "/" + self.server.token + "/"
        if not hmac.compare_digest(parts.path[:len(prefix)].encode(), prefix.encode()):
            self._send(404)
            return None
        route = parts.path[len(prefix):]
        self.user, self.login, self.first = None, None, False
        if resident:
            logins = self.headers.get_all(IDENTITY) or []
            if len(logins) != 1 or not logins[0].strip():
                self._send(403)
                return None
            self.login = logins[0].strip().lower()
            try:
                self.user, self.first = resident_user(self.server, self.login)
            except (OSError, sqlite3.Error, RuntimeError):
                self._send(503)
                return None
            # Before anyone is registered, only the page and the ledger's own routes answer, so the first person
            # can register; no MCS record is served to a login that is not staff.
            if self.user is None and not (self.first and (route in FILES or route in FIRST_ROUTES)):
                self._send(403, UNREGISTERED)
                return None
        return route, parts.query

    def _json(self, status, data):
        self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json")

    def do_GET(self):
        found = self._route()
        if found is None:
            return
        route, query = found
        if route in FILES:
            name, mime = FILES[route]
            self._send(200, (ASSETS / name).read_bytes(), mime)
        elif route == "api/session":
            self._json(200, {"csrf": self.server.csrf, "homecare": self.server.homecare is not None,
                             "routes": self.server.route_settings is not None,
                             "resident": bool(self.server.tailnet_host), "login": self.login,
                             "user": self.user and {"staff_id": self.user["staff_id"], "name": self.user["name"]}})
        elif route == "api/hc/staff" and self.server.tailnet_host and self.user is None:
            # The first registration sees an empty register; staff records are read only by registered staff.
            self._json(200, {"items": []})
        elif route.startswith("api/hc/"):
            self._homecare("GET", route[7:], query)
        elif route.startswith("api/"):
            try:
                result = response(self.server.snapshot, self.server.allowed, route[4:], query)
            except ValueError as exc:
                code = str(exc)
                # Only fixed, non-clinical codes reach the browser.
                code = code if code in {"cursor_scope_or_generation_changed", "project_not_allowed",
                                        "bad_search_filter", "search_cursor_invalid"} else "bad_request"
                self._json(400, {"error": code})
            except (OSError, sqlite3.Error):
                self._send(503, b'{"error":"snapshot_unavailable"}', "application/json")
            else:
                self._json(200, result)
        else:
            self._send(404)

    def do_POST(self):
        found = self._route(write=True)
        if found is None:
            return
        route, _query = found
        length = self.headers.get("Content-Length", "")
        if not route.startswith("api/hc/") or not length.isdigit():
            self._send(404 if not route.startswith("api/hc/") else 411)
            return
        if int(length) > MAX_BODY:
            self._send(413)
            return
        if self.headers.get("Content-Type") != "application/json" or not hmac.compare_digest(
                self.headers.get("X-MCS-CSRF", "").encode(), self.server.csrf.encode()):
            self._send(403)
            return
        try:
            body = json.loads(self.rfile.read(int(length)) or b"null")
        except (ValueError, RecursionError):
            self._json(400, {"error": "bad_request"})
            return
        if self.server.tailnet_host and isinstance(body, dict):
            # Who writes is the Tailscale login, never what the page says.
            body = {**body, "actor": self.user and self.user["staff_id"]}
            if self.first and route == "api/hc/staff.create" and isinstance(body.get("values"), dict):
                body["values"] = {**body["values"], "tailscale_login": self.login}
        self._homecare("POST", route[7:], body=body)

    def _homecare(self, method, route, query=None, body=None):
        try:
            result = homecare_response(self.server, method, route, query, body)
        except homecare_store.Conflict as exc:
            self._json(409, homecare_api.error_body(exc))
        except ValueError as exc:
            self._json(400, homecare_api.error_body(exc))
        except (OSError, sqlite3.Error):
            self._send(503, b'{"error":"storage_unavailable"}', "application/json")
        else:
            self._json(200, result)


def resident_argv(cfg, root):
    """The arguments of the resident workspace that `mcs setup remote` saved under ``remote_access``:
    this Mac's published snapshot, all its patients and its ledger, behind its tailnet name."""
    remote = remote_access(cfg)
    if remote is None:
        raise ValueError("remote_access_not_configured")
    argv = ["--snapshot", str(Path(root) / "data" / "snapshots" / "ledger-snapshot.db"), "--all-patients",
            "--homecare-db", str(homecare_store.default_path(root)),
            "--port", str(remote.get("port", REMOTE_ACCESS_PORT)), "--tailnet-host", remote["tailnet_host"]]
    return argv + (["--route-settings", remote["route_settings"]] if "route_settings" in remote else [])


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv == ["--resident"]:
        # The LaunchAgent form: every setting comes from config.json, so the agent's plist stays fixed.
        import mcs_util
        cfg = mcs_util.load_config_checked()
        try:
            argv = resident_argv(cfg or {}, mcs_util.HOME)
        except ValueError:
            sys.exit("remote_access is not configured; run `mcs setup remote` first. No server started.")
    parser = argparse.ArgumentParser(description=__doc__, epilog="--resident alone starts the resident workspace "
                                     "saved by `mcs setup remote`.")
    parser.add_argument("--snapshot", type=Path, required=True,
                        help="Published ledger-snapshot.db only; never the live ledger")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--project", type=int, action="append", help="Explicitly authorized patient project ID; repeatable")
    scope.add_argument("--all-patients", action="store_true", help="Explicitly authorize all patients in this local snapshot")
    parser.add_argument("--port", type=int, default=0, help="Loopback port (default: choose an available port)")
    parser.add_argument("--homecare-db", type=Path,
                        help="Home-care records database to read and edit (created on first use)")
    parser.add_argument("--homecare-module", action="append", choices=sorted(homecare_catalog.AVAILABLE),
                        help="Occupation module to enable with --homecare-db; repeatable (default: pharmacy)")
    parser.add_argument("--route-settings", type=Path,
                        help="Private JSON (chmod 600) enabling visit routes; sends addresses only, when asked")
    parser.add_argument("--tailnet-host", metavar="NAME.ts.net",
                        help="Resident mode behind `tailscale serve` on this name: staff sign in with their Tailscale "
                             "login (needs --homecare-db and a fixed --port)")
    args = parser.parse_args(argv)
    if args.tailnet_host and (not args.homecare_db or not args.port or not TAILNET_HOST.match(args.tailnet_host)):
        parser.error("--tailnet-host needs --homecare-db, a fixed --port and a *.ts.net name")
    if args.project and any(pid <= 0 for pid in args.project) or not 0 <= args.port <= 65535:
        parser.error("invalid project or port")
    try:
        view = View(args.snapshot)
        view.close()
    except (ValueError, OSError, sqlite3.Error):
        parser.exit(2, "Published snapshot unavailable; no server started.\n")
    if args.route_settings and not args.homecare_db:
        parser.error("--route-settings needs --homecare-db")
    try:
        routes = None if args.route_settings is None else homecare_route.RouteSettings(args.route_settings)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Route settings unavailable ({type(exc).__name__}); no server started.\n")
    try:
        server = ChatServer(args.snapshot, args.project, args.port, homecare=args.homecare_db,
                            modules=args.homecare_module or ("pharmacy",), route_settings=routes,
                            tailnet_host=args.tailnet_host)
    except (OSError, sqlite3.Error, RuntimeError):
        parser.exit(2, "Home-care database unavailable; no server started.\n")
    with server:
        if server.tailnet_host:
            print("Resident patient workspace. Publish it to your tailnet only (never Funnel) with:", flush=True)
            print(f"  tailscale serve --bg http://127.0.0.1:{server.server_port}", flush=True)
            print(f"Staff open {server.url} and are known by their Tailscale login.", flush=True)
        else:
            print("Local patient chat (keep this private URL on this computer):", flush=True)
            print(server.url, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
