"""The local web server (spec v2.2 §2 W1-W6, §5). Standard library only.

- Binds 127.0.0.1 only. GET and HEAD only: every other method is 405.
- Host header must be 127.0.0.1:<port> or localhost:<port> (blocks DNS rebinding): otherwise 421.
- No CORS headers. A strict CSP (scripts, styles and fetches from this origin only; no inline code).
- Static files come from a fixed list in web/static (no path joins on user input): anything else is 404.
- It never places orders: there is no write endpoint of any kind.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore
from urllib.parse import parse_qsl, urlsplit

from .api import STATIC, Api, BadRequest

log = logging.getLogger(__name__)

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "form-action 'none'; base-uri 'none'; frame-ancestors 'none'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY", "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin", "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml"}
STATIC_NAME = re.compile(r"^[a-z0-9_\-]{1,40}\.(js|css|json|svg)$")
PAGES = [re.compile(p) for p in (r"^/$", r"^/theme/[a-z0-9_\-]{1,32}$", r"^/ticker/[A-Z][A-Z0-9.\-]{0,9}$",
                                 r"^/scan/[A-Z][A-Z0-9.\-]{0,9}$", r"^/scan/theme/[a-z0-9_\-]{1,32}$",
                                 r"^/builder$", r"^/about$")]
MAX_URL = 8192


def static_files() -> dict[str, Path]:
    """The whitelist: every servable file, by name. Built once from the package directory."""
    return {p.name: p for p in STATIC.iterdir() if p.is_file() and STATIC_NAME.match(p.name)}


class Handler(BaseHTTPRequestHandler):
    server_version = "gexscan"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # set by make_server
    api: Api
    allowed_hosts: frozenset = frozenset()
    files: dict = {}
    shell: bytes = b""
    sem: BoundedSemaphore

    def log_message(self, fmt, *args):          # path only, to the debug log
        log.debug("%s %s", self.command, urlsplit(self.path).path)

    # ---- response helpers ---------------------------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None, head: bool = False):
        self.send_response(code)
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _json(self, code: int, obj, head: bool = False):
        body = json.dumps(obj, separators=(",", ":"), allow_nan=False, default=str).encode()
        self._send(code, body, TYPES[".json"], {"Cache-Control": "no-store"}, head)

    def _text(self, code: int, msg: str, head: bool = False, extra: dict | None = None):
        self._send(code, msg.encode(), "text/plain; charset=utf-8", {"Cache-Control": "no-store", **(extra or {})}, head)

    def _not_allowed(self):
        self.close_connection = True            # the body (if any) is never read
        self._text(405, "Method not allowed. This server is read-only (GET only).", extra={"Allow": "GET, HEAD"})

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _not_allowed

    def do_GET(self):
        self._route(head=False)

    def do_HEAD(self):
        self._route(head=True)

    # ---- routing ------------------------------------------------------------------------------------
    def _route(self, head: bool):
        if (self.headers.get("Host") or "").lower() not in self.allowed_hosts:
            return self._text(421, "Misdirected request: use http://127.0.0.1 or http://localhost.", head)
        if len(self.path) > MAX_URL:
            return self._text(414, "URL too long.", head)
        u = urlsplit(self.path)
        path = u.path
        if path.startswith("/api/"):
            return self._api(path, u.query, head)
        if path.startswith("/static/"):
            f = self.files.get(path[len("/static/"):])
            if f is None:
                return self._text(404, "Not found.", head)
            return self._send(200, f.read_bytes(), TYPES[f.suffix], {"Cache-Control": "no-cache"}, head)
        if any(p.match(path) for p in PAGES):
            return self._send(200, self.shell, TYPES[".html"], {"Cache-Control": "no-cache"}, head)
        return self._text(404, "Not found.", head)

    def _api(self, path: str, query: str, head: bool):
        q = {}
        for k, v in parse_qsl(query, keep_blank_values=True):
            q.setdefault(k, v)
        if path not in self.api.routes:
            return self._json(404, {"error": "unknown endpoint"}, head)
        with self.sem:
            try:
                out = self.api.handle(path, q)
            except BadRequest as e:
                return self._json(400, {"error": str(e)}, head)
            except Exception:  # never leak internals (or anything from the environment) to the page
                log.exception("api %s", path)
                return self._json(500, {"error": "internal error; see the server log"}, head)
        return self._json(200, out, head)


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        """A browser that drops a socket mid-response (a tab closed, a fetch superseded) is routine, not a crash."""
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            log.debug("client %s went away", client_address[0])
            return
        super().handle_error(request, client_address)


def make_server(api: Api, port: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    if host != "127.0.0.1":
        raise ValueError("the web app binds 127.0.0.1 only")
    # the page shell is served for the page routes only, never as /static/app.html
    attrs = {"api": api, "files": static_files(), "shell": (STATIC / "app.html").read_bytes(), "sem": BoundedSemaphore(4),
             "allowed_hosts": frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})}
    handler = type("GexHandler", (Handler,), attrs)
    srv = Server((host, port), handler)
    srv.daemon_threads = True
    if port == 0:  # tests: pick a free port, then allow its Host
        real = srv.server_address[1]
        handler.allowed_hosts = frozenset({f"127.0.0.1:{real}", f"localhost:{real}"})
    return srv


def serve(cfg: dict, port: int, fixtures: Path | None, today: dt.date | None, state: Path | None,
          use_news: bool = True) -> None:
    api = Api(cfg, fixtures, today, state, use_news)
    srv = make_server(api, port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


__all__ = ["CSP", "Handler", "PAGES", "SECURITY_HEADERS", "make_server", "serve", "static_files"]
