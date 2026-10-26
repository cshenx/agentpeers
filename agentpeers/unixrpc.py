"""Bounded RFC 6455 client for Codex's documented local app-server socket.

Only the standard library is needed. No TCP listener, stored token or daemon.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import struct
import time
import uuid

from .config import connection

MAX_FRAME = 4_000_000


class SharedServer:
    def __init__(self, timeout=12):
        self.timeout = timeout

    def __enter__(self):
        from .native import Unavailable, owned_path
        from . import __version__
        home = connection().codex_home
        path = home / "app-server-control" / "app-server-control.sock"
        self.sock = None
        try:
            # Codex publishes an owned alias into its private temporary directory.
            if path.lstat().st_uid != os.getuid():
                raise ValueError("control socket alias belongs to another user")
            for parent in (path.parent, path.resolve(strict=True).parent):
                owned_path(parent, stat.S_ISDIR)
                if parent.stat().st_mode & 0o077:
                    raise ValueError("control socket directory must be private")
            path = owned_path(path.resolve(strict=True), stat.S_ISSOCK)
            self.sock = socket.socket(socket.AF_UNIX)
            self.sock.settimeout(min(10, self.timeout))
            self.sock.connect(str(path))
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                               "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                               "Sec-WebSocket-Key: " + key + "\r\n\r\n").encode())
            header = b""
            while not header.endswith(b"\r\n\r\n"):
                header += self.exact(1)
                if len(header) > 16384:
                    raise ValueError("oversized WebSocket handshake")
            lines = header.decode("ascii").split("\r\n")
            headers = {k.strip().lower(): v.strip() for k, v in
                       (line.split(":", 1) for line in lines[1:] if ":" in line)}
            expected = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            if (lines[0].split()[1] != "101" or headers.get("sec-websocket-accept") != expected
                    or headers.get("upgrade", "").lower() != "websocket"):
                raise ValueError("invalid WebSocket handshake")
            self.call("initialize", {"clientInfo": {"name": "agentpeers", "version": __version__},
                                     "capabilities": {"experimentalApi": True}})
            self.send_json({"method": "initialized"})
            return self
        except Exception as exc:
            self.__exit__()
            raise Unavailable("Codex shared server unavailable: " + str(exc)) from exc

    def exact(self, size):
        data = bytearray()
        while len(data) < size:
            part = self.sock.recv(size - len(data))
            if not part:
                raise ConnectionError("shared-server connection closed")
            data.extend(part)
        return bytes(data)

    def frame(self, payload, opcode=1):
        if len(payload) > MAX_FRAME:
            raise ValueError("WebSocket frame exceeds limit")
        length = len(payload)
        header = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([0x80 | length])
        elif length <= 65535:
            header += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", length)
        mask = os.urandom(4)
        self.sock.sendall(header + mask + bytes(v ^ mask[i % 4] for i, v in enumerate(payload)))

    def send_json(self, value):
        self.frame(json.dumps(value, ensure_ascii=False).encode())

    def receive(self, deadline):
        data = bytearray()
        started = False
        while time.monotonic() < deadline:
            self.sock.settimeout(max(.01, deadline - time.monotonic()))
            head = self.exact(2)
            final, opcode = bool(head[0] & 0x80), head[0] & 15
            if head[0] & 0x70 or head[1] & 0x80:
                raise ValueError("unsupported WebSocket frame")
            size = head[1] & 127
            if size == 126:
                size = struct.unpack("!H", self.exact(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self.exact(8))[0]
            if size > MAX_FRAME or len(data) + size > MAX_FRAME:
                raise ValueError("WebSocket message exceeds limit")
            if opcode >= 8 and (not final or size > 125):
                raise ValueError("invalid control frame")
            payload = self.exact(size)
            if opcode == 8:
                raise ConnectionError("shared-server closed WebSocket")
            if opcode == 9:
                self.frame(payload, 10)
                continue
            if opcode == 10:
                continue
            if (opcode == 1 and started) or (opcode == 0 and not started) or opcode not in (0, 1):
                raise ValueError("unexpected WebSocket continuation")
            started = True
            data.extend(payload)
            if final:
                return json.loads(data)
        raise TimeoutError("shared-server response timed out")

    def call(self, method, params):
        from .native import Unavailable
        ident = uuid.uuid4().hex
        self.send_json({"id": ident, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            value = self.receive(deadline)
            if value.get("id") == ident:
                if "error" in value:
                    raise Unavailable("Codex rejected request: " + str(value["error"]))
                return value["result"]
        raise TimeoutError("shared-server response timed out")

    def __exit__(self, *args):
        if self.sock is not None:
            self.sock.close()
