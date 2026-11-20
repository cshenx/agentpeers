"""Opt-in wake for a Codex chat that has no live AgentPeers route.

Codex desktop inside ChatGPT.app has no desktop channel other programs can
deliver to, so a message to one of its chats is saved unread in the mailbox.
For a chat that was explicitly opted in, a send with no live route then queues
one fixed wake signal with ``codex queue`` and opens the app's wake link. The
chat runs a turn, reads its mailbox through the inbox tool and acknowledges.

The platform records the queued text as user input, so the signal is one
constant: no sender, body, summary or scope ever goes into it. Peer content is
read only as inbox tool output. A wake is never delivery or a read receipt,
and a failed or uncertain wake is recorded and never retried.
"""
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import time
import uuid

from . import native
from .config import setting, connection, connection_env

# Pinned byte for byte by tests. Never interpolate anything into it.
WAKE_TEXT = ("AgentPeers wake signal. This text was generated automatically by AgentPeers, not written by the user. "
             "It is not a peer message and grants no permission. An AgentPeers message may be waiting for this chat: "
             "call the AgentPeers inbox tool, and for each unread message addressed to this chat, handle it under its "
             "own authority note and acknowledge it with the AgentPeers acknowledge tool. Do not acknowledge a message "
             "that is already read. If nothing is unread, reply \"No unread AgentPeers messages\" and take no other action.")

DISCLOSURE = ("Wake is off unless turned on for a chat. When it is on for a Codex chat and a message to that chat finds "
              "no live AgentPeers route, AgentPeers saves the message unread and queues a fixed wake signal with "
              "codex queue, then opens the app's wake link. The chat runs a turn: the signal appears in it as a "
              "user-looking message that says it came from AgentPeers, not from you, followed by the chat's reply, "
              "and the app may post a turn-complete notification. Each wake turn uses model usage. The peer message "
              "itself is read only through the inbox tool. Turn on: agentpeers wake on codex:<chat id> (you in a "
              "terminal, or that chat itself; never a sender). Turn off: agentpeers wake off <chat>, "
              "agentpeers wake off --all, or AGENTPEERS_WAKE=off, which overrides everything.")

# A wake still waiting for its chat to read the mailbox collects later messages
# too. Past this age it no longer blocks a new wake, so a lost wake cannot
# leave later messages without one.
COALESCE_SECONDS = 3600
OUTSTANDING = ("queueing", "queued", "uncertain")
QUEUED = re.compile(r"Queued message ([0-9a-fA-F-]{36})")
RECEIPTS = {"queueing": "saved unread; wake not yet confirmed", "queued": "saved unread; wake queued",
            "uncertain": "saved unread; wake uncertain, not retried",
            "link_failed": "saved unread; wake queued but the app link failed, not retried",
            "failed": "saved unread; wake failed, not retried",
            "refused_owner": "saved unread; not woken: a separate process owns this chat",
            "refused_uncertain": "saved unread; not woken: who owns this chat could not be verified"}


def overridden():
    """AGENTPEERS_WAKE=off turns every wake off, whatever is recorded. Only an empty value or "on" leaves it alone."""
    value = setting("WAKE")
    return value is not None and value.strip().lower() not in ("", "on")


def chat_id(chat):
    """Accept codex:UUID or a bare UUID; nothing else, so a wake can never target a name guess."""
    if not isinstance(chat, str):
        raise ValueError("name the chat as codex:<full chat id>")
    value = chat[len("codex:"):] if chat.startswith("codex:") else chat
    if ":" in value:
        raise ValueError("wake applies only to Codex chats: use codex:<full chat id>")
    try:
        return "codex:" + str(uuid.UUID(value))
    except ValueError as exc:
        raise ValueError("name the chat as codex:<full chat id>, copied from agentpeers list") from exc


def setup(db):
    db.execute("""CREATE TABLE IF NOT EXISTS wake_optins (
        chat TEXT PRIMARY KEY, enabled INTEGER NOT NULL, on_by TEXT, on_at REAL, off_by TEXT, off_at REAL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS wake_signals (
        id TEXT PRIMARY KEY, chat TEXT NOT NULL, created REAL NOT NULL, state TEXT NOT NULL, detail TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS message_wakes (
        message_id TEXT PRIMARY KEY, wake_id TEXT NOT NULL, coalesced INTEGER NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS inbox_reads (chat TEXT PRIMARY KEY, read_at REAL NOT NULL)""")


def caller():
    """Who runs this command, from process ancestry and Codex's own thread variable.

    --from is never consulted. Same-user processes can still lie about their
    environment; like the rest of AgentPeers this is a cooperative boundary.
    """
    try:
        known = {row["pid"]: row["id"] for row in native.claude_sessions()}
    except native.Unavailable as exc:
        raise ValueError("cannot verify that no Claude session runs this command; wake not turned on") from exc
    pid = os.getppid()
    for _ in range(30):
        if pid in known:
            return known[pid]
        if pid < 2:
            break
        try:
            result = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError("cannot inspect this command's parent processes; wake not turned on") from exc
        if not result.stdout.strip().isdigit():
            break
        pid = int(result.stdout.strip())
    for name in ("CODEX_THREAD_ID", "AGENTPEERS_SESSION", "PEERBRIDGE_SESSION"):
        value = os.environ.get(name)
        if value:
            return value if ":" in value else "codex:" + value
    return None


def turn_on(db, chat, by=None):
    """Only the person in a terminal, or the chat itself, may turn wake on. A sender never can."""
    chat = chat_id(chat)
    if by is not None and by != chat:
        raise ValueError("only the person in a terminal, or the chat itself, can turn wake on for " + chat +
                         "; this command runs in " + by + ". A sender can never turn it on. Nothing changed.")
    who = by or "terminal user (no agent session detected)"
    with db:
        db.execute("INSERT INTO wake_optins(chat,enabled,on_by,on_at) VALUES (?,1,?,?) ON CONFLICT(chat) "
                   "DO UPDATE SET enabled=1,on_by=excluded.on_by,on_at=excluded.on_at", (chat, who, time.time()))
    return status(db, chat)


def turn_off(db, chat=None, by=None, everything=False):
    who = by or "terminal user (no agent session detected)"
    now = time.time()
    with db:
        if everything:
            db.execute("UPDATE wake_optins SET enabled=0,off_by=?,off_at=? WHERE enabled=1", (who, now))
        else:
            chat = chat_id(chat)
            db.execute("INSERT INTO wake_optins(chat,enabled,off_by,off_at) VALUES (?,0,?,?) ON CONFLICT(chat) "
                       "DO UPDATE SET enabled=0,off_by=excluded.off_by,off_at=excluded.off_at", (chat, who, now))
    return status(db, None if everything else chat)


def recorded_on(db, chat):
    row = db.execute("SELECT enabled FROM wake_optins WHERE chat=?", (chat,)).fetchone()
    return bool(row and row[0])


def enabled(db, chat):
    return chat.startswith("codex:") and not overridden() and recorded_on(db, chat)


def status(db, chat=None):
    query = "SELECT * FROM wake_optins" + (" WHERE chat=?" if chat else " ORDER BY chat")
    rows = [dict(r) for r in db.execute(query, (chat,) if chat else ())]
    if chat and not rows:
        rows = [{"chat": chat, "enabled": 0, "on_by": None, "on_at": None, "off_by": None, "off_at": None}]
    for row in rows:
        row["recorded"] = "on" if row.pop("enabled") else "off"
        row["wake"] = "off" if overridden() else row["recorded"]
    return {"chats": rows, "override": "AGENTPEERS_WAKE=off" if overridden() else None, "disclosure": DISCLOSURE}


def chats_on():
    """Chats with wake recorded on, read from AgentPeers' own store without creating it."""
    from .core import state_dir, Bridge
    if not (state_dir() / "messages.sqlite3").exists():
        return []
    with Bridge() as bridge:
        return [r[0] for r in bridge.db.execute("SELECT chat FROM wake_optins WHERE enabled=1 ORDER BY chat")]


def annotate(sessions):
    """Show wake on or off on each Codex row of a listing."""
    codex = [row for row in sessions if str(row.get("id", "")).startswith("codex:")]
    if not codex:
        return sessions
    try:
        on = set() if overridden() else set(chats_on())
    except Exception:
        on = None
    for row in codex:
        row["wake"] = "unknown" if on is None else "on" if row["id"] in on else "off"
    return sessions


def record_inbox_read(db, chat):
    """Inside the caller's transaction: the chat has collected every unread message saved before now."""
    db.execute("INSERT INTO inbox_reads VALUES (?,?) ON CONFLICT(chat) DO UPDATE SET read_at=excluded.read_at",
               (chat, time.time()))


def outstanding(db, chat, now):
    """The newest wake for this chat that will still collect a message saved now, or None.

    A wake stops covering new messages once its chat reads its inbox (it may
    already be past that read), once every message it covers is read, or after
    COALESCE_SECONDS. A failed wake or a failed app link never covers them.
    """
    row = db.execute("SELECT * FROM wake_signals WHERE chat=? ORDER BY created DESC LIMIT 1", (chat,)).fetchone()
    if row is None or row["state"] not in OUTSTANDING or now - row["created"] > COALESCE_SECONDS:
        return None
    read = db.execute("SELECT read_at FROM inbox_reads WHERE chat=?", (chat,)).fetchone()
    if read and read[0] >= row["created"]:
        return None
    unread = db.execute("SELECT 1 FROM message_wakes w JOIN messages m ON m.id=w.message_id "
                        "WHERE w.wake_id=? AND m.read_at IS NULL LIMIT 1", (row["id"],)).fetchone()
    return row if unread else None


def queue_binary():
    """The Codex CLI bundled with the desktop app writes the queue format that app reads; else the configured CLI."""
    app = native.codex_desktop_app()
    if app:
        bundled = Path(app["path"]) / "Contents" / "Resources" / "codex-cli" / "bin" / "codex"
        if bundled.is_file() and os.access(bundled, os.X_OK):
            return str(bundled)
    return setting("CODEX", "codex")


class NotWoken(Exception):
    def __init__(self, state, reason):
        super().__init__(reason)
        self.state = state


def process(pid):
    """(parent pid, executable path) of a live process, or None when it cannot be read."""
    try:
        result = subprocess.run(["/bin/ps", "-o", "ppid=,comm=", "-p", str(pid)],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    parts = result.stdout.strip().split(None, 1)
    if result.returncode or len(parts) != 2 or not parts[0].isdigit():
        return None
    return int(parts[0]), parts[1]


def app_owned(pid, app):
    """True when every process from pid up to the app's own launchd-started main process is inside the app
    bundle; False for anything else, such as a terminal; None when the chain cannot be read."""
    contents, main = app.rstrip("/") + "/Contents/", app.rstrip("/") + "/Contents/MacOS/"
    for _ in range(20):
        info = process(pid)
        if info is None:
            return None
        parent, executable = info
        if not executable.startswith(contents):
            return False
        if executable.startswith(main):
            return parent == 1
        pid = parent
    return False


def rollout(ident):
    """The chat's one rollout file. An archived, missing or duplicated chat is never woken."""
    home = connection().codex_home
    if list((home / "archived_sessions").glob("*" + ident + "*.jsonl")):
        raise NotWoken("refused_uncertain", "the chat is archived")
    found = list((home / "sessions").glob("*/*/*/rollout-*-" + ident + ".jsonl"))
    if len(found) != 1:
        raise NotWoken("refused_uncertain", "found " + str(len(found)) + " rollout files for this chat, not exactly one")
    return found[0]


def ownership(ident):
    """Refuse unless only the desktop app (or nothing) holds the chat. Fails safe on any doubt.

    Two independent views: processes holding the chat's rollout file (lsof),
    and processes naming the chat on their command line (a terminal resume).
    Each one must be the desktop app's own process. This is the only gate and
    it runs immediately before the queue: once a signal is queued, any process
    that has the chat loaded may run it, so nothing checked afterwards could
    protect a chat. Codex offers no lock an outside tool can hold, so a process
    that starts and loads the chat between this check and the queue call is
    not excluded; the receipt records both times so that gap is measurable.
    """
    app = native.codex_desktop_app()
    if not app:
        raise NotWoken("failed", "no Codex desktop app found")
    path = rollout(ident)
    try:
        held = subprocess.run(["/usr/sbin/lsof", "-t", "--", str(path)], capture_output=True, text=True, timeout=15)
        listing = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise NotWoken("refused_uncertain", "could not list the chat's processes: " + str(exc)) from exc
    if held.returncode == 0 and held.stdout.strip():
        holders = held.stdout.split()
    elif held.returncode == 1 and not held.stdout.strip() and not held.stderr.strip():
        holders = []
    else:
        raise NotWoken("refused_uncertain", "lsof gave no clear answer (exit " + str(held.returncode) + ")")
    if listing.returncode or not listing.stdout.strip():
        raise NotWoken("refused_uncertain", "the process list could not be read")
    own, pid = set(), os.getpid()
    while pid > 1 and len(own) < 30:  # This command and the shell that started it may name the chat.
        own.add(pid)
        info = process(pid)
        if info is None:
            break
        pid = info[0]
    naming = [line.split(None, 1)[0] for line in listing.stdout.splitlines()
              if ident in line and not (line.split(None, 1)[0].isdigit() and int(line.split(None, 1)[0]) in own)]
    owners = []
    for pid in dict.fromkeys(holders + naming):
        if not pid.isdigit():
            raise NotWoken("refused_uncertain", "unreadable process id " + repr(pid))
        owned = app_owned(int(pid), app["path"])
        if owned is None:
            raise NotWoken("refused_uncertain", "could not trace process " + pid + " to its parent")
        if not owned:
            info = process(int(pid))
            raise NotWoken("refused_owner", "process " + pid + " (" + (info[1] if info else "unknown") +
                           ") outside the desktop app holds or names this chat")
        owners.append(int(pid))
    return {"app": app["path"], "rollout_held_by_app": [int(p) for p in holders], "app_processes": owners}


def run_wake(chat):
    """Check ownership, queue WAKE_TEXT, open the wake link. Exactly one attempt of each; never retried.

    Every refusal happens before the queue. Nothing runs between the queue and
    the link: the queued signal is already runnable by whatever holds the chat,
    so a later check could only hide that, never prevent it.
    """
    ident = chat.split(":", 1)[1]
    detail = {"queue_attempted": False, "link_attempted": False}
    if platform.system() != "Darwin":
        return "failed", {**detail, "reason": "wake requires macOS"}
    binary = queue_binary()
    try:
        detail["owner"] = ownership(ident)
    except NotWoken as exc:
        return exc.state, {**detail, "reason": str(exc)}
    detail.update(owner_checked_at=time.time(), queue_attempted=True, codex=binary)
    try:
        result = subprocess.run([binary, "queue", "--thread", ident, "--message", WAKE_TEXT],
                                capture_output=True, text=True, timeout=20, env=connection_env())
    except FileNotFoundError as exc:
        return "failed", {**detail, "reason": "Codex CLI not found: " + str(exc)}
    except (OSError, subprocess.SubprocessError) as exc:
        return "uncertain", {**detail, "reason": "codex queue outcome unknown: " + str(exc)}
    detail["queue_returned_at"] = time.time()
    output = ((result.stdout or "") + (result.stderr or "")).strip()[:500]
    detail["queue_output"] = output
    if result.returncode:
        return "failed", {**detail, "reason": "codex queue exited " + str(result.returncode)}
    found = QUEUED.search(result.stdout or "")
    if not found:
        return "uncertain", {**detail, "reason": "codex queue exited 0 without confirming a queued message"}
    detail.update(queued_message=found.group(1), link_attempted=True)
    try:
        opened = subprocess.run(["/usr/bin/open", "-g", "codex://threads/wake/" + ident],
                                capture_output=True, timeout=10)
        if opened.returncode:
            return "link_failed", {**detail, "reason": "open exited " + str(opened.returncode)}
    except (OSError, subprocess.SubprocessError) as exc:
        return "link_failed", {**detail, "reason": "could not open the wake link: " + str(exc)}
    return "queued", detail


def after_unavailable(db, message_id, chat):
    """Called after a send to a codex: chat stored the message with no live route."""
    if not enabled(db, chat):
        return None
    with db:
        db.execute("BEGIN IMMEDIATE")  # Concurrent senders and inbox reads see each other.
        now = time.time()
        current = outstanding(db, chat, now)
        if current is not None:
            db.execute("INSERT OR IGNORE INTO message_wakes VALUES (?,?,1)", (message_id, current["id"]))
            return current["id"]
        wake = str(uuid.uuid4())
        db.execute("INSERT INTO wake_signals VALUES (?,?,?,?,NULL)", (wake, chat, now, "queueing"))
        db.execute("INSERT INTO message_wakes VALUES (?,?,0)", (message_id, wake))
    try:
        state, detail = run_wake(chat)
    except Exception as exc:  # Never let a wake failure change the stored message.
        state, detail = "uncertain", {"reason": "wake outcome unknown: " + str(exc)}
    with db:
        db.execute("UPDATE wake_signals SET state=?,detail=? WHERE id=?",
                   (state, json.dumps({**detail, "finished_at": time.time()}), wake))
    return wake


def receipt(db, message_id, read_at):
    """The wake part of a message receipt, or None when no wake was involved."""
    row = db.execute("SELECT w.coalesced,s.* FROM message_wakes w JOIN wake_signals s ON s.id=w.wake_id "
                     "WHERE w.message_id=?", (message_id,)).fetchone()
    if row is None:
        return None
    detail = json.loads(row["detail"]) if row["detail"] else {}
    wake = {"id": row["id"], "chat": row["chat"], "state": row["state"], "queued_at": row["created"],
            "coalesced": bool(row["coalesced"]), "detail": detail,
            "receipt": "read" if read_at else RECEIPTS.get(row["state"], "saved unread")}
    if row["coalesced"]:
        wake["note"] = ("An earlier wake for this chat had not yet collected its mailbox, so no new wake was "
                        "sent; that wake collects every unread message.")
    wake["proof"] = "A queued wake is not delivery or a read. Only the chat's own acknowledge marks it read."
    return wake
