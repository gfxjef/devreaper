"""Local HTTP server: serves the panel and a small JSON API. Bound to 127.0.0.1 only."""
import json
import secrets
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HOST, PORT = "127.0.0.1", 7777
MAX_BODY = 1 << 20
STATIC = Path(__file__).with_name("static")
INDEX = STATIC / "index.html"
ASSETS = {"/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8")}
SECURITY_HEADERS = {
    # No framing (clickjacking on the two-click kill), no external loads, no inline scripts.
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "img-src data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                               "form-action 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class LocalServer(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second socket bind an in-use port; we want the bind to fail instead.
    allow_reuse_address = False
    daemon_threads = True


def make_server(app, port=PORT):
    token = secrets.token_urlsafe(24)
    allowed_hosts = set()  # filled once the socket is bound (port 0 picks a free port in tests)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for header, value in SECURITY_HEADERS.items():
                self.send_header(header, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj).encode())

        def do_GET(self):
            if self.headers.get("Host") not in allowed_hosts:
                return self._json(403, {"error": "bad host"})
            url = urlsplit(self.path)
            if url.path in ("/", "/index.html"):
                html = INDEX.read_text(encoding="utf-8").replace("__TOKEN__", token)
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            if url.path in ASSETS:
                name, ctype = ASSETS[url.path]
                return self._send(200, (STATIC / name).read_bytes(), ctype)
            if url.path == "/api/state":
                return self._json(200, app.state())
            if url.path == "/api/live":
                return self._json(200, app.live())
            if url.path == "/api/ram":
                try:
                    hours = min(max(float(parse_qs(url.query).get("hours", ["168"])[0]), 1), 168)
                except ValueError:
                    hours = 168
                return self._json(200, {"hours": hours, "series": app.ram_series(hours)})
            if self.path == "/api/ping":
                return self._json(200, {"app": "devreaper"})
            self._json(404, {"error": "not found"})

        def do_POST(self):
            # Read the body before any reply: on Windows, closing a socket with unread data sends a reset and the
            # client sees ConnectionAbortedError instead of the 403.
            try:
                length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length else b""
            origin = self.headers.get("Origin")
            if (self.headers.get("Host") not in allowed_hosts
                    or self.headers.get("X-DevReaper-Token") != token
                    or (origin is not None and origin.removeprefix("http://") not in allowed_hosts)):
                return self._json(403, {"error": "forbidden"})
            try:
                body = json.loads(raw or b"{}")
                result = app.action(self.path.removeprefix("/api/"), body, origin="panel")
            except (ValueError, KeyError) as e:
                return self._json(400, {"error": str(e)})
            except (OSError, subprocess.SubprocessError) as e:
                return self._json(500, {"error": f"No se pudo completar: {e}"})
            self._json(200, {"result": result, "state": app.state()})

    server = LocalServer((HOST, port), Handler)
    bound = server.server_address[1]
    allowed_hosts.update({f"127.0.0.1:{bound}", f"localhost:{bound}"})
    return server
