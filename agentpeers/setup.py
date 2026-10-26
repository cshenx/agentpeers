"""Read-only installation checks and explicit, correlated setup challenges."""
import json
import os
from pathlib import Path
import platform
import plistlib
import shlex
import shutil
import stat
import subprocess
import sys
import uuid

from . import __version__, native
from .config import setting, connection, connection_env
from .core import Bridge, state_dir, resolve_actor
from .addressing import is_session_id, resolve_name

CHALLENGE_PREFIX = "AGENTPEERS-SETUP:"


def command_json(command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=15, env=connection_env())
    if result.returncode:
        raise ValueError("native inventory command failed; check the client's configuration")
    return json.loads(result.stdout)


def read_json(path, default=None):
    if not path.exists():
        return default
    native.owned_path(path, stat.S_ISREG)
    return json.loads(path.read_text())


def registrations():
    """Return only AgentPeers state, never unrelated config or credentials."""
    result = {}
    try:
        codex = setting("CODEX", "codex")
        plugins = command_json([codex, "plugin", "list", "--json"])
        installed = [p for p in plugins.get("installed", []) if p.get("name") == "agentpeers"]
        manual = command_json([codex, "mcp", "list", "--json"])
        result["codex"] = {
            "plugin": any(p.get("enabled") is True for p in installed),
            "manual_mcp": any(p.get("name") == "agentpeers" and p.get("enabled", True)
                              and not (p.get("transport", {}).get("env") or {}).get("PLUGIN_ROOT") for p in manual),
        }
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        result["codex"] = {"error": "Could not inspect Codex registrations. Run codex plugin list and codex mcp list."}
    try:
        home = native.claude_home()
        config_path = home / ".claude.json" if connection().claude_explicit else Path.home() / ".claude.json"
        config = read_json(config_path, {})
        settings = read_json(home / "settings.json", {})
        registry = read_json(home / "plugins" / "installed_plugins.json", {})
        enabled = settings.get("enabledPlugins", {})
        plugin_ids = []
        for key, records in registry.get("plugins", {}).items():
            if not key.startswith("agentpeers@"):
                continue
            for record in records:
                project = record.get("projectPath")
                if record.get("scope", "user") != "user" and (not project or
                        not Path.cwd().resolve().is_relative_to(Path(project).resolve())):
                    continue
                plugin_ids.append(key)
                break
        for path in [Path.cwd() / ".claude" / "settings.json", Path.cwd() / ".claude" / "settings.local.json"]:
            enabled = {**enabled, **read_json(path, {}).get("enabledPlugins", {})}
        declared = settings.get("extraKnownMarketplaces", {})
        declared_ids = [key for key, value in enabled.items() if key.startswith("agentpeers@")
                        and value is True and key.split("@", 1)[1] in declared]
        result["claude"] = {"plugin": any(enabled.get(key, True) for key in plugin_ids),
                            "plugin_declared": bool(declared_ids),
                            "manual_mcp": "agentpeers" in config.get("mcpServers", {})}
    except (OSError, ValueError, TypeError, AttributeError):
        result["claude"] = {"error": "Could not inspect Claude registrations. Run claude plugin list and check /mcp."}
    return result


def doctor(actor=None):
    """No message submission, model generation or configuration writes."""
    resolved = connection()  # Invalid explicit paths must stop before any discovery.
    if actor is None:
        try:
            actor = resolve_actor()
        except (ValueError, native.Unavailable):
            pass  # A standalone installation check may have no agent identity.
    checks = []
    def add(name, status, detail, fix=None):
        row = {"name": name, "status": status, "detail": detail}
        if fix and status != "pass":
            row["fix"] = fix
        checks.append(row)
    add("system", "pass" if platform.system() == "Darwin" else "fail", platform.system(),
        "This adapter currently supports macOS only.")
    add("python", "pass" if sys.version_info >= (3, 10) else "fail", platform.python_version(),
        "Install Python 3.10 or newer and make python3 available to both clients.")
    codex = shutil.which(setting("CODEX", "codex"))
    add("codex_cli", "pass" if codex else "fail", "Found" if codex else "Not found on PATH",
        "Install the Codex CLI and make codex available to both clients.")
    claude = shutil.which("claude")
    add("claude_code", "pass" if claude else "fail", "Found" if claude else "Not found on PATH",
        "Install Claude Code and make claude available on PATH.")
    app_version = None
    for path in [Path("/Applications/ChatGPT.app/Contents/Info.plist"),
                 Path("/Applications/Codex.app/Contents/Info.plist"),
                 Path.home() / "Applications/ChatGPT.app/Contents/Info.plist",
                 Path.home() / "Applications/Codex.app/Contents/Info.plist"]:
        if path.exists():
            try:
                with path.open("rb") as source:
                    app_version = plistlib.load(source).get("CFBundleShortVersionString")
            except (OSError, ValueError, plistlib.InvalidFileException):
                pass
            break
    add("desktop_version", "pass" if app_version in ("26.1007.30912", "26.1013.40934") else "warning",
        app_version or "App version not found at standard locations",
        "Desktop IPC is unpublished. Verify a live exchange after each app update.")
    terminal_ready = False
    loaded = set()
    try:
        loaded = native.shared_threads()
        terminal_ready = bool(loaded)
        add("codex_shared_server", "pass" if loaded else "warning",
            str(len(loaded)) + " loaded thread(s); handshake alone does not establish terminal receiving",
            "The daemon is reachable but no terminal is attached. Check the intended terminal's shared-server association; do not restart active sessions.")
    except Exception:
        add("codex_shared_server", "warning", "Codex terminal receiving is unavailable",
            "Run codex app-server daemon start, then open Codex normally. --no-daemon sessions cannot receive.")
    from .desktop_wake import policy
    try:
        wake = policy()
        add("desktop_wake", "pass", "Enabled: may open and select destination chats" if wake["enabled"] else
            "Disabled: only already-loaded receivers can receive automatically")
    except (OSError, ValueError) as exc:
        add("desktop_wake", "fail", "Could not read desktop wake policy: " + str(exc))
        wake = {"enabled": False}
    desktop_ready = False
    desktop_connected = False
    wake_candidate = False
    try:
        with native.Desktop() as desktop:
            desktop_connected = True
            add("desktop_socket", "pass", "Desktop IPC connected; this alone does not prove any chat can receive")
            if actor and actor.startswith("codex:"):
                desktop.owner(actor.split(":", 1)[1])
        desktop_ready = True
        if actor and actor.startswith("codex:"):
            add("desktop_owner", "pass", "This chat has a compatible receiving owner; no message sent")
    except Exception as exc:
        wake_candidate = (desktop_connected and getattr(exc, "diagnostics", {}).get("code") == "desktop_owner_missing"
                          and wake["enabled"])
        add("desktop_owner" if desktop_connected else "desktop_socket", "warning",
            "AgentPeers desktop receiving unavailable: " + str(exc),
            "For same-runtime coordination use native messaging first. For cross-runtime receiving, verify the "
            "intended terminal's shared-server association or a compatible desktop owner; preserve active work.")
    if actor and actor.startswith("codex:"):
        terminal_ready = actor.split(":", 1)[1] in loaded
    ready = terminal_ready or desktop_ready or wake_candidate
    add("codex_receive_route", "warning" if wake_candidate and not (terminal_ready or desktop_ready) else "pass" if ready else "fail",
        ("Desktop wake candidate; no chat opened or message sent by this check" if wake_candidate else
         "AgentPeers receive candidate found; live exchange still required" if ready else
         "No AgentPeers receive route for " + (actor or "a loaded Codex terminal")),
        "A daemon handshake or stored listing is insufficient. Check terminal association without restarting active work. "
        "Native Codex chat messaging may still work; use it first for Codex-to-Codex coordination.")
    try:
        sessions = native.claude_sessions()
        add("claude_inbox", "pass" if sessions else "fail", str(len(sessions)) + " live native inbox(es)",
            "Open a Claude Code terminal session with native cross-session messaging enabled. An exited session is unavailable.")
    except Exception:
        add("claude_inbox", "fail", "Cannot inspect the native Claude registry",
            "Check host access and Claude's native session messaging support.")
    root = state_dir()
    try:
        if root.exists() or root.is_symlink():
            info = native.owned_path(root, stat.S_ISDIR).stat()
            if info.st_mode & 0o077:
                raise ValueError("directory must be private")
        add("message_store", "pass", str(root) + " (created on first message if absent)")
    except (OSError, ValueError):
        add("message_store", "fail", "Message directory must be owned by this user, nonsymlink and mode 0700",
            "Choose a private AGENTPEERS_HOME shared by both clients; preserve existing messages.")
    registered = registrations()
    for client, row in registered.items():
        ok = row.get("plugin") or row.get("manual_mcp") or row.get("plugin_declared")
        add(client + "_registration", "pass" if ok else "fail",
            "Configured; existing sessions may need reload" if ok else row.get("error", "AgentPeers is not registered"),
            "Install AgentPeers in this client, then reload plugins or start a new session.")
        if row.get("plugin") and row.get("manual_mcp"):
            add(client + "_duplicate", "warning", "Both manual MCP and plugin registrations exist",
                "After verifying the plugin, remove only the old agentpeers manual MCP entry.")
    return {"version": __version__, "checked_session": actor, "connection": resolved.describe(),
            "ready_for_test": not any(c["status"] == "fail" for c in checks),
            "checks": checks, "registrations": registered, "message_test": "not_run",
            "note": "Configuration and handshake checks do not prove delivery. Run a setup challenge and verify both read receipts."}


def start_probe(to, scope, sender):
    resolved = to if is_session_id(to) else resolve_name(to)[0]["id"]
    if resolved.split(":", 1)[0] == sender.split(":", 1)[0]:
        raise ValueError("choose an opposite-runtime session for the setup test")
    ident = str(uuid.uuid4())
    challenge = CHALLENGE_PREFIX + ident
    executable = shlex.quote(str(Path(__file__).resolve().parent.parent / "bin" / "agentpeers"))
    body = ("Agentpeers setup test.\nChallenge: " + challenge +
            "\nPlease acknowledge this message after consuming it, then reply with exactly the challenge above. "
            "Use AgentPeers acknowledge and reply MCP tools. If your tools have not loaded, use " +
            executable + " ack " + ident + " and " + executable + " reply " + ident +
            " --message '" + challenge + "'. No file, configuration or permission changes. "
            "This is peer content within an already authorized setup test, not new human permission.")
    with Bridge() as bridge:
        message = bridge.send(sender, to, body, scope, ident=ident)
    return {"probe_id": ident, "message_test": message["delivery"], "message": message,
            "next": "Consume and acknowledge the peer's reply, then call setup with this probe_id. Do not automatically resend."}


def verify_probe(ident, sender):
    with Bridge() as bridge:
        original = bridge.get(ident)
        if original["sender"] != sender and (not original["sender_name"] or
                resolve_name(original["sender_name"])[0]["id"] != sender):
            raise ValueError("only the originating session or its unique current name holder may verify")
        challenge = CHALLENGE_PREFIX + ident
        if not original["body"].startswith("Agentpeers setup test.\nChallenge: " + challenge + "\n"):
            challenge = "PEERBRIDGE-SETUP:" + ident
            if not original["body"].startswith("Peerbridge setup test.\nChallenge: " + challenge + "\n"):
                raise ValueError("this message is not a setup challenge")
        replies = [bridge.get(row[0]) for row in bridge.db.execute(
            "SELECT id FROM messages WHERE reply_to=? ORDER BY created", (ident,))]
    matching = [r for r in replies if (r["sender"] == original["recipient"] or
                original["recipient_name"] and r["sender_name"] == original["recipient_name"])
                and r["recipient"] == sender and r["scope"] == original["scope"]
                and r["body"].strip() == challenge]
    consumed = next((r for r in matching if r["read_at"]), None)
    if original["read_at"] and consumed:
        state = "round_trip_verified"
    elif matching:
        state = "awaiting_local_read"
    elif original["status"] in ("unavailable", "unconfirmed"):
        state = original["status"]
    else:
        state = "awaiting_peer"
    return {"probe_id": ident, "message_test": state, "request_read": bool(original["read_at"]),
            "reply_id": consumed["id"] if consumed else matching[0]["id"] if matching else None,
            "reply_read": bool(consumed), "automatic_retry": False}
