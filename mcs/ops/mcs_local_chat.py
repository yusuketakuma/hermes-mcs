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
from mcs_view import View
import local_chat_view
import homecare_api
import homecare_catalog
import homecare_route
import homecare_store

ASSETS = Path(__file__).resolve().parents[1] / "views" / "local_chat"
MAX_BODY = 64 * 1024
FILES = {"": ("index.html", "text/html; charset=utf-8"),
         "app.js": ("app.js", "text/javascript; charset=utf-8"),
         "homecare.js": ("homecare.js", "text/javascript; charset=utf-8"),
         "pharmacy.js": ("pharmacy.js", "text/javascript; charset=utf-8"),
         "style.css": ("style.css", "text/css; charset=utf-8")}


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
    if params or route != "panel" and (section is not None or page or role):
        raise ValueError("bad_arguments")
    if route == "sections":
        return local_chat_view.sections()
    if route != "patients":
        project = int(project or "")
        local_chat_view.require_project(project, allowed)
    view = View(snapshot)
    try:
        if route == "patients":
            return local_chat_view.patients(view, allowed, query=text, cursor=cursor)
        if route == "panel":
            if text or cursor:
                raise ValueError("bad_arguments")
            return local_chat_view.panel(view, allowed, project, section, page=page, role=role,
                                         message_id=int(message) if message else None)
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


class ChatServer(ThreadingHTTPServer):
    def __init__(self, snapshot, allowed, port=0, homecare=None, modules=("pharmacy",), route_settings=None):
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
        """The route after the capability prefix, or None once a refusal was sent."""
        host = f"127.0.0.1:{self.server.server_port}"
        origin = self.headers.get("Origin")
        if self.headers.get_all("Host") != [host] or origin not in (None, "http://" + host) \
                or self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none") \
                or write and (origin is None or self.headers.get("Sec-Fetch-Site") not in (None, "same-origin")):
            self._send(403)
            return None
        if len(self.path) > 8192:
            self._send(414)
            return None
        parts = urlsplit(self.path)
        prefix = "/" + self.server.token + "/"
        if not hmac.compare_digest(parts.path[:len(prefix)].encode(), prefix.encode()):
            self._send(404)
            return None
        return parts.path[len(prefix):], parts.query

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
                             "routes": self.server.route_settings is not None})
        elif route.startswith("api/hc/"):
            self._homecare("GET", route[7:], query)
        elif route.startswith("api/"):
            try:
                result = response(self.server.snapshot, self.server.allowed, route[4:], query)
            except ValueError as exc:
                code = str(exc)
                # Only fixed, non-clinical codes reach the browser.
                code = code if code in {"cursor_scope_or_generation_changed", "project_not_allowed"} else "bad_request"
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
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
    args = parser.parse_args(argv)
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
                            modules=args.homecare_module or ("pharmacy",), route_settings=routes)
    except (OSError, sqlite3.Error, RuntimeError):
        parser.exit(2, "Home-care database unavailable; no server started.\n")
    with server:
        print("Local patient chat (keep this private URL on this computer):", flush=True)
        print(server.url, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
