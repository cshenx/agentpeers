"""Native transports. Never open or edit an agent's conversation database."""
import hashlib
from datetime import datetime
import json
import os
import platform
from pathlib import Path
import queue
import socket
import stat
import struct
import subprocess
import threading
import time
import uuid

from . import __version__
from .config import setting
from .unixrpc import SharedServer
from .desktop_wake import enabled as desktop_wake_enabled

MAX_BYTES = 256_000
DISCOVERY_BUDGETS = (12, 6)


class Unavailable(Exception):
    """Known not submitted: safe to retry after the destination recovers."""

    def __init__(self, message, **diagnostics):
        super().__init__(message)
        self.diagnostics = diagnostics


class Unconfirmed(Exception):
    """Might have reached the destination. Do not automatically retry."""


def owned_path(path, kind):
    path = Path(path)
    info = path.lstat()
    if info.st_uid != os.getuid() or stat.S_ISLNK(info.st_mode):
        raise ValueError("destination must belong to this user and not be a symlink")
    if not kind(info.st_mode):
        raise ValueError("unexpected destination file type")
    return path


def claude_home():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or
                os.environ.get("ANTHROPIC_CONFIG_DIR") or Path.home() / ".claude")


def claude_sessions():
    found = []
    for path in (claude_home() / "sessions").glob("*.json"):
        try:
            owned_path(path, stat.S_ISREG)
            if path.stat().st_size > MAX_BYTES:
                continue
            data = json.loads(path.read_text())
            pid = data["pid"]
            if type(pid) is not int or pid < 2 or path.stem != str(pid):
                continue
            try:
                os.kill(pid, 0)
            except PermissionError as exc:
                raise Unavailable("cannot verify Claude processes from this sandbox; use host access") from exc
            # A stale registry entry must not address a recycled PID.
            if data.get("procStart"):
                start = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                                       capture_output=True, text=True, timeout=3,
                                       env={**os.environ, "TZ": "UTC", "LC_ALL": "C"})
                if start.returncode != 0:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        continue
                    raise Unavailable("cannot verify Claude process start time; use host access")
                try:
                    actual = datetime.strptime(" ".join(start.stdout.split()), "%a %b %d %H:%M:%S %Y")
                    recorded = datetime.strptime(" ".join(data["procStart"].split()), "%a %b %d %H:%M:%S %Y")
                except ValueError as exc:
                    raise Unavailable("Claude process identity format is unsupported; no message submitted") from exc
                if actual != recorded:
                    continue
            sock = owned_path(data["messagingSocketPath"], stat.S_ISSOCK)
            found.append({"id": "claude:" + data["sessionId"], "runtime": "claude",
                          "name": data.get("name") or str(pid), "name_address": data.get("name"), "cwd": data.get("cwd"),
                          "status": data.get("status", "unknown"), "pid": pid,
                          "socket": str(sock), "version": data.get("version")})
        except PermissionError as exc:
            raise Unavailable("cannot read Claude's advertised registry; use host access") from exc
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            continue
    return found


class AppServer:
    """Use the read-only JSON-RPC API for stored Codex discovery."""
    def __init__(self, timeout=12, deadline=None):
        self.timeout = timeout
        self.deadline = deadline

    def __enter__(self):
        command = [setting("CODEX", "codex"), "app-server"]
        self.proc = subprocess.Popen(command,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True)
        self.lines = queue.Queue()
        def read():
            for line in self.proc.stdout:
                if len(line) <= 4_000_000:
                    self.lines.put(line)
            self.lines.put(None)
        threading.Thread(target=read, daemon=True).start()
        try:
            self.call("initialize", {"clientInfo": {"name": "agentpeers", "version": __version__},
                                     "capabilities": {"experimentalApi": True}})
            self.proc.stdin.write(json.dumps({"method": "initialized"}) + "\n")
            self.proc.stdin.flush()
        except Exception:
            self.__exit__()
            raise
        return self

    def call(self, method, params):
        deadline = time.monotonic() + self.timeout
        if self.deadline is not None:
            deadline = min(deadline, self.deadline)
        if time.monotonic() >= deadline:
            raise Unavailable("Codex discovery time budget exhausted", code="codex_discovery_timeout",
                              stage="discovery", submission_attempted=False)
        ident = uuid.uuid4().hex
        self.proc.stdin.write(json.dumps({"id": ident, "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        while time.monotonic() < deadline:
            try:
                line = self.lines.get(timeout=max(.01, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise Unavailable("Codex app-server response timed out", code="codex_discovery_timeout",
                                  stage="discovery", submission_attempted=False) from exc
            if line is None:
                raise Unavailable("Codex app-server connection exited")
            value = json.loads(line)
            if value.get("id") == ident:
                if "error" in value:
                    raise Unavailable("Codex app-server rejected request: " + str(value["error"]))
                return value["result"]
        raise Unavailable("Codex discovery timed out", code="codex_discovery_timeout",
                          stage="discovery", submission_attempted=False)

    def __exit__(self, *args):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.proc.stdin.close()
        self.proc.stdout.close()


def loaded_threads(client):
    """Only live shared-server threads; never resume or load a stored chat."""
    found, seen, cursor = set(), set(), None
    while True:
        result = client.call("thread/loaded/list", {"cursor": cursor, "limit": 100})
        found.update(result["data"])
        cursor = result.get("nextCursor")
        if not cursor:
            return found
        if cursor in seen:
            raise Unavailable("Codex loaded-thread listing repeated a cursor")
        seen.add(cursor)


def shared_threads():
    with SharedServer() as client:
        return loaded_threads(client)


def send_codex(destination, envelope):
    """Prefer a loaded shared-server thread, then the separate desktop owner.

    Fallback is allowed only before submitting any message payload. A closed
    unloaded task is never silently resumed in a new server process.
    """
    shared_target_loaded = None
    client = SharedServer()
    try:
        client.__enter__()
    except (Unavailable, OSError, ValueError, KeyError):
        client = None
    if client is not None:
        try:
            try:
                present = destination["id"].split(":", 1)[1] in loaded_threads(client)
                shared_target_loaded = present
            except (Unavailable, OSError, ValueError, KeyError):
                present = False
            if present:
                try:
                    result = client.call("turn/start", {
                        "threadId": destination["id"].split(":", 1)[1], "input": [],
                        "toolOutput": {"name": "agentpeers", "output": json.dumps(envelope, ensure_ascii=False)}
                    })
                    return {"transport": "codex-shared-server", "ack": result,
                            "note": "Native tool output accepted; explicit receiver read still required"}
                except (Unavailable, OSError, ValueError, KeyError) as exc:
                    raise Unconfirmed("shared-server submission outcome unknown; no fallback: " + str(exc)) from exc
        finally:
            client.__exit__()
    try:
        with Desktop() as desktop:
            return desktop.send(destination, envelope)
    except Unavailable as exc:
        raise Unavailable("No AgentPeers receive route: target is not verified loaded on the shared server "
                          "and the desktop adapter is unavailable. Native Codex session messaging may still work; "
                          "use it first for Codex-to-Codex within existing authorization. " + str(exc),
                          **{**exc.diagnostics, "session_id": destination["id"],
                             "shared_target_loaded": shared_target_loaded}) from exc


def stored_codex_sessions(budget):
    rows, seen = [], set()
    # Initialization and all pages share one budget; a slow server cannot add
    # a full timeout for every page. A retry uses a fresh read-only process.
    with AppServer(timeout=budget, deadline=time.monotonic() + budget) as client:
        cursor = None
        while True:
            params = {"limit": 100, "archived": False}
            if cursor:
                params["cursor"] = cursor
            result = client.call("thread/list", params)
            rows.extend(result.get("data", []))
            cursor = result.get("nextCursor")
            if not cursor:
                break
            if cursor in seen:
                raise Unavailable("Codex discovery repeated a page cursor")
            seen.add(cursor)
    return rows


def codex_sessions():
    for attempt, budget in enumerate(DISCOVERY_BUDGETS, 1):
        try:
            rows = stored_codex_sessions(budget)
            break
        except Unavailable as exc:
            if exc.diagnostics.get("code") != "codex_discovery_timeout":
                raise
            if attempt == 2:
                raise Unavailable("Codex session discovery timed out after two read-only attempts. "
                                  "Receiving state is unknown; no message was submitted. "
                                  "Use an exact previously verified session ID for authorized contact.",
                                  code="codex_discovery_timeout", stage="discovery", attempts=2,
                                  session_state="unknown", submission_attempted=False) from exc
    # A fresh terminal can be loaded and named before its first saved turn.
    # Include public metadata for loaded IDs absent from the saved-chat listing,
    # otherwise a duplicate live name could be mistaken for a unique address.
    rows = {row["id"]: row for row in rows}
    live = set()
    client = SharedServer()
    try:
        client.__enter__()
    except (Unavailable, OSError, ValueError, KeyError):
        client = None
    if client is not None:
        try:
            live = loaded_threads(client)
            for ident in live - rows.keys():
                rows[ident] = client.call("thread/read", {"threadId": ident, "includeTurns": False})["thread"]
        finally:
            client.__exit__()
    rows = list(rows.values())
    return [{"id": "codex:" + row["id"], "runtime": "codex",
             "surface": "shared-server" if row["id"] in live else "desktop-or-stored",
             "name": row.get("name") or row.get("preview") or row["id"], "name_address": row.get("name"),
             "cwd": row.get("cwd"), "status": "live shared server" if row["id"] in live else "AgentPeers reachability unverified; native Codex messaging may be available"}
            for row in rows]


class Desktop:
    """Experimental desktop IPC, observed on app 26.1007.30912.

    This is NOT a published OpenAI API. Only discover, steer and start are used.
    Explicit desktop-wake opt-in can open a chat using its native app link.
    Never take terminal ownership, interrupt, modify settings, or answer approvals.
    """
    def __init__(self, timeout=15):
        self.timeout = timeout

    def __enter__(self):
        home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        path = home / "ipc" / "ipc.sock"
        try:
            owned_path(path, stat.S_ISSOCK)
            self.sock = socket.socket(socket.AF_UNIX)
            self.sock.settimeout(self.timeout)
            self.sock.connect(str(path))
        except OSError as exc:
            if hasattr(self, "sock"):
                self.sock.close()
            raise Unavailable("Cannot connect to Codex desktop IPC; this does not prove the app is closed. " + str(exc),
                              code="desktop_connection_unavailable", desktop_connected=False) from exc
        try:
            result = self.call("initialize", {"clientType": "agentpeers"}, 0)
            self.client_id = result["result"]["clientId"]
        except (OSError, ValueError, KeyError) as exc:
            self.sock.close()
            raise Unavailable("Desktop IPC handshake failed; app state is unknown. " + str(exc),
                              code="desktop_handshake_failed", desktop_connected=None) from exc
        return self

    def exact(self, length):
        data = b""
        while len(data) < length:
            part = self.sock.recv(length - len(data))
            if not part:
                raise ConnectionError("desktop connection closed")
            data += part
        return data

    def call(self, method, params, version, target=None):
        rid = uuid.uuid4().hex
        value = {"type": "request", "requestId": rid, "method": method,
                 "params": params, "version": version}
        if hasattr(self, "client_id"):
            value["sourceClientId"] = self.client_id
        if target:
            value["targetClientId"] = target
        body = json.dumps(value).encode()
        self.sock.sendall(struct.pack("<I", len(body)) + body)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            self.sock.settimeout(max(.01, deadline - time.monotonic()))
            length = struct.unpack("<I", self.exact(4))[0]
            if length > 4_000_000:
                raise ValueError("desktop frame exceeds limit")
            response = json.loads(self.exact(length))
            if response.get("requestId") == rid:
                return response
            if response.get("type") == "client-discovery-request":
                no = {"type": "client-discovery-response", "requestId": response["requestId"],
                      "response": {"canHandle": False}}
                data = json.dumps(no).encode()
                self.sock.sendall(struct.pack("<I", len(data)) + data)
        raise TimeoutError("desktop response timed out")

    def owner(self, ident):
        try:
            result = self.call("thread-owner-discovery", {"hostId": "local", "conversationId": ident}, 1)
        except (OSError, TimeoutError, ValueError) as exc:
            # Only discovery has run: no message payload has left this process.
            raise Unavailable("Desktop IPC connected, but receiver discovery failed; chat ownership is unknown. "
                              "No message submitted: " + str(exc), code="desktop_owner_discovery_failed",
                              desktop_connected=True, desktop_owner_found=None) from exc
        if result.get("resultType") != "success":
            raise Unavailable("Desktop IPC connected, but no compatible receiving owner answered for this chat; "
                              "the app being open does not make every saved chat reachable. No message submitted. "
                              "This does not establish native Codex chat availability. For Codex-to-Codex "
                              "use the host's native session messaging first (send_message_to_thread when available), "
                              "within existing human authorization. For Claude-to-Codex verify a loaded shared-server "
                              "terminal or compatible desktop owner; never silently resume a stored chat.",
                              code="desktop_owner_missing", desktop_connected=True, desktop_owner_found=False)
        if result.get("result", {}).get("supportsUntrustedAppInput") is not True:
            raise Unavailable("desktop owner lacks untrusted input support; adapter incompatible",
                              code="desktop_owner_incompatible", desktop_connected=True, desktop_owner_found=True)
        return result["handledByClientId"]

    def wake(self, ident):
        """Open only a confirmed stored local chat, with no prompt in the URL.

        The app owns any resume and its normal admission/permissions. A launch
        return code is never delivery evidence; require a compatible receiver.
        """
        if platform.system() != "Darwin":
            raise Unavailable("Desktop wake currently requires macOS", code="desktop_wake_unsupported")
        ident = str(uuid.UUID(ident))
        try:
            with AppServer() as client:
                thread = client.call("thread/read", {"threadId": ident, "includeTurns": False})["thread"]
            if thread["id"] != ident:
                raise ValueError("stored chat ID mismatch")
            if "archived_sessions" in Path(thread.get("path") or "").parts:
                raise ValueError("archived chats are not opened automatically")
            result = subprocess.run(["/usr/bin/open", "-g", "codex://threads/" + ident],
                                    capture_output=True, timeout=5)
            if result.returncode:
                raise ValueError("native app link could not be opened")
        except (OSError, ValueError, KeyError, Unavailable, subprocess.SubprocessError) as exc:
            raise Unavailable("Could not open the destination chat; no message submitted: " + str(exc),
                              code="desktop_wake_failed") from exc
        deadline = time.monotonic() + 30
        while True:
            try:
                return self.owner(ident)
            except Unavailable as exc:
                if exc.diagnostics.get("code") != "desktop_owner_missing":
                    raise
                if time.monotonic() >= deadline:
                    raise Unavailable("Opened the chat but no compatible receiver became available; "
                                      "no message submitted. No automatic retry.", code="desktop_wake_failed",
                                      desktop_connected=True, desktop_owner_found=False) from exc
                time.sleep(.25)

    def send(self, destination, envelope):
        ident = destination["id"].split(":", 1)[1]
        woke = False
        try:
            owner = self.owner(ident)
        except Unavailable as exc:
            if exc.diagnostics.get("code") != "desktop_owner_missing" or not desktop_wake_enabled():
                raise
            owner = self.wake(ident)
            woke = True
        # The peer message is a tool result, never a human/user instruction.
        output = {"name": "agentpeers", "output": json.dumps(envelope, ensure_ascii=False)}
        restore = {"id": envelope["id"], "text": "Peer message", "createdAt": int(time.time()*1000),
                   "cwd": destination.get("cwd"),
                   "context": {"prompt": "", "workspaceRoots": [], "addedFiles": [],
                               "fileAttachments": [], "imageAttachments": [], "ideContext": None}}
        try:
            result = self.call("thread-follower-steer-turn", {
                "conversationId": ident, "input": [], "toolOutput": output,
                "restoreMessage": restore, "clientUserMessageId": envelope["id"], "attachments": []
            }, 1, owner)
            if result.get("resultType") == "success":
                return {"transport": "desktop-steer", "ack": result.get("result"), "desktop_wake": woke}
            error = str(result.get("error", ""))
            # Start only after an explicit rejection saying no turn is active.
            if not ("not being streamed" in error or "not currently running" in error
                    or "active turn" in error and "ended" in error):
                raise Unconfirmed("desktop steer rejected: " + error)
            result = self.call("thread-follower-start-turn", {
                "conversationId": ident, "turnStart": {
                    "request": {"threadId": ident, "input": [], "toolOutput": output,
                                "clientUserMessageId": envelope["id"]},
                    "context": {"restoreMessage": restore}
                }
            }, 2, owner)
            if result.get("resultType") != "success":
                raise Unconfirmed("desktop start rejected: " + str(result.get("error")))
            return {"transport": "desktop-start", "ack": result.get("result"), "desktop_wake": woke}
        except (OSError, TimeoutError, ValueError) as exc:
            raise Unconfirmed("desktop submission outcome unknown: " + str(exc)) from exc

    def __exit__(self, *args):
        self.sock.close()


def receive_route(actor):
    """Read-only return-route candidate. Never send, resume or infer delivery.

    The shorter RPC timeouts keep missing owners from delaying every send by
    the transport's full timeout. A failed probe is a warning, never a reroute.
    """
    result = {"session_id": actor, "status": "unavailable", "transport": None,
              "checked_at": time.time(), "proof": "candidate only; a live read receipt is still required"}
    if actor.startswith("claude:"):
        try:
            if any(row["id"] == actor for row in claude_sessions()):
                result.update(status="available", transport="claude-native-inbox")
        except Exception:
            result["status"] = "unknown"
    elif actor.startswith("codex:"):
        ident = actor.split(":", 1)[1]
        shared_target_loaded = None
        try:
            with SharedServer(timeout=2) as client:
                shared_target_loaded = ident in loaded_threads(client)
                if shared_target_loaded:
                    result.update(status="available", transport="codex-shared-server")
                    return result
        except Exception:
            pass
        try:
            with Desktop(timeout=2) as desktop:
                desktop.owner(ident)
            result.update(status="available", transport="desktop")
        except Unavailable as exc:
            result["diagnostics"] = {**exc.diagnostics, "shared_target_loaded": shared_target_loaded}
            if exc.diagnostics.get("code") == "desktop_owner_missing" and desktop_wake_enabled():
                result.update(status="wake_required", desktop_wake=True,
                              note="Desktop wake is enabled; an actual send can open this chat. Not delivery proof.")
                return result
            if exc.diagnostics.get("code") != "desktop_owner_missing" or shared_target_loaded is None:
                result["status"] = "unknown"
        except Exception:
            result["status"] = "unknown"
    else:
        result["status"] = "unknown"
    return result


def send_claude(destination, envelope):
    path = owned_path(destination["socket"], stat.S_ISSOCK)
    key = claude_home() / "sessions" / (
        str(destination["pid"]) + "." + hashlib.sha256(str(path).encode()).hexdigest() + ".key")
    # Claude hashes its advertised pathname, not realpath (/tmp aliases differ on macOS).
    # Read solely for native peer classification. Never log or persist this token.
    try:
        owned_path(key, stat.S_ISREG)
        if key.stat().st_size > 4096 or key.stat().st_mode & 0o077:
            raise ValueError("invalid credential file")
        token = json.loads(key.read_text())["peerToken"]
        if not isinstance(token, str) or not token or len(token) > 1024:
            raise ValueError("invalid peer token")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise Unavailable("native peer credential unavailable; no message submitted") from exc
    serialized = json.dumps(envelope, ensure_ascii=False).replace("<", "\\u003c")
    content = ("<cross-session-message from-name=\"agentpeers\">\n" +
               serialized + "\n</cross-session-message>")
    payload = {"type": "user", "message": {"role": "user", "content": content},
               "msgV": 1, "msg_id": "cc-msg-" + envelope["id"].replace("-", ""), "priority": "next"}
    conn = socket.socket(socket.AF_UNIX)
    conn.settimeout(5)
    try:
        try:
            conn.connect(str(path))
        except OSError as exc:
            raise Unavailable("Claude session cannot be reached: " + str(exc)) from exc
        try:
            data = json.dumps({"type": "auth", "token": token}) + "\n" + json.dumps(payload) + "\n"
            conn.sendall(data.encode())
            conn.shutdown(socket.SHUT_WR)
        except OSError as exc:
            raise Unconfirmed("Claude write outcome unknown: " + str(exc)) from exc
    finally:
        conn.close()
    return {"transport": "claude-native-inbox", "note": "socket write completed; authentication, inbound hold/refusal/read unknown until ACK"}
