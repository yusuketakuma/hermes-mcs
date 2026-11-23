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

ASSETS = Path(__file__).resolve().parents[1] / "views" / "local_chat"
FILES = {"": ("index.html", "text/html; charset=utf-8"),
         "app.js": ("app.js", "text/javascript; charset=utf-8"),
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


class ChatServer(ThreadingHTTPServer):
    def __init__(self, snapshot, allowed, port=0):
        self.snapshot = Path(snapshot)
        self.allowed = None if allowed is None else frozenset(allowed)
        self.token = secrets.token_urlsafe(32)
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

    def do_GET(self):
        host = f"127.0.0.1:{self.server.server_port}"
        origin = self.headers.get("Origin")
        if self.headers.get_all("Host") != [host] or origin not in (None, "http://" + host) \
                or self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            self._send(403)
            return
        if len(self.path) > 8192:
            self._send(414)
            return
        parts = urlsplit(self.path)
        prefix = "/" + self.server.token + "/"
        if not hmac.compare_digest(parts.path[:len(prefix)].encode(), prefix.encode()):
            self._send(404)
            return
        route = parts.path[len(prefix):]
        if route in FILES:
            name, mime = FILES[route]
            self._send(200, (ASSETS / name).read_bytes(), mime)
        elif route.startswith("api/"):
            try:
                result = response(self.server.snapshot, self.server.allowed, route[4:], parts.query)
            except ValueError as exc:
                code = str(exc)
                # Only fixed, non-clinical codes reach the browser.
                code = code if code in {"cursor_scope_or_generation_changed", "project_not_allowed"} else "bad_request"
                self._send(400, json.dumps({"error": code}).encode(), "application/json")
            except (OSError, sqlite3.Error):
                self._send(503, b'{"error":"snapshot_unavailable"}', "application/json")
            else:
                self._send(200, json.dumps(result, ensure_ascii=False).encode(), "application/json")
        else:
            self._send(404)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True,
                        help="Published ledger-snapshot.db only; never the live ledger")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--project", type=int, action="append", help="Explicitly authorized patient project ID; repeatable")
    scope.add_argument("--all-patients", action="store_true", help="Explicitly authorize all patients in this local snapshot")
    parser.add_argument("--port", type=int, default=0, help="Loopback port (default: choose an available port)")
    args = parser.parse_args(argv)
    if args.project and any(pid <= 0 for pid in args.project) or not 0 <= args.port <= 65535:
        parser.error("invalid project or port")
    try:
        view = View(args.snapshot)
        view.close()
    except (ValueError, OSError, sqlite3.Error):
        parser.exit(2, "Published snapshot unavailable; no server started.\n")
    with ChatServer(args.snapshot, args.project, args.port) as server:
        print("Local patient chat (keep this private URL on this computer):", flush=True)
        print(server.url, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
