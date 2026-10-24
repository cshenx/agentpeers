"""Loopback HTTP service. Deploy behind an owner-managed HTTPS reverse proxy."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from .app import App
from .adapters import encode
from .config import Rejected
from .endpoint import form  # Retain the earlier import path for integrations.


def handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentPeers"
        sys_version = ""
        request_timeout = 10

        def setup(self):
            super().setup()
            # Bound idle and partial-header connections before parsing any request.
            self.connection.settimeout(self.request_timeout)

        def log_message(self, *_args):
            pass  # Callback paths, authorization codes and query strings are credentials.

        def output(self, status, value=None, headers=None, content_type="application/json"):
            body = b"" if value is None else encode(value) if content_type == "application/json" else value.encode()
            self.send_response(status)
            for key, value in {"Content-Type": content_type, "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff", "Content-Length": str(len(body)), **(headers or {})}.items():
                self.send_header(key, value)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def dispatch(self):
            from .endpoint import Endpoint
            from .transport import run
            def raw():
                length = int(self.headers.get("Content-Length", "-1"))
                if not 0 <= length <= 262144:
                    raise Rejected("Request body must be at most 256 KiB")
                self.connection.settimeout(10)
                return self.rfile.read(length)
            endpoint = Endpoint(app, self.command, self.path, self.headers, raw, self.client_address[0])
            self.output(*run(endpoint.steps(), app.events.post))

        do_GET = dispatch
        do_POST = dispatch
    return Handler


def serve(app, host="127.0.0.1", port=8766):
    if host != "127.0.0.1":
        raise Rejected("Bind only to loopback; use an HTTPS reverse proxy")
    server = ThreadingHTTPServer((host, port), handler(app))
    done = threading.Event()
    def deliver():
        while not done.is_set():
            try:
                worked = app.dispatcher.once()
                app.worker_warning = False
            except Exception:
                # Leave the durable row for inspection; don't expose a traceback with credentials.
                worked = False
                app.worker_warning = True
            done.wait(0.1 if worked else 1)
    worker = threading.Thread(target=deliver, daemon=True)
    worker.start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        done.set()
        server.server_close()
        worker.join(timeout=12)
