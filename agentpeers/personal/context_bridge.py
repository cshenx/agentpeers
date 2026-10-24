"""Bounded one-requester/Dot trial on the existing OAuth and message transport.

Disabled by default. OAuth authenticates the service principal; OpenAI metadata
is a required, owner-pinned correlation label, not an authentication assertion.
No TLS identity claim, new credential scheme or ordinary-chat notification.
"""
import json
import math
from urllib.parse import urlsplit

from . import AUTHORITY
from .config import Rejected, digest, identifier
from .context import CONTEXT_TOOLS, bounded, fingerprint


ORIGIN = "context-requester-1"
RESOURCE_PATH = "/mcp/context-requester"
SCOPE = "agentpeers:context"
QUESTION = "What color is the synthetic AgentPeers trial card?"
SOURCE = "fixture:agentpeers-trial-card-v1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS ap_context_control(id INTEGER PRIMARY KEY CHECK(id=1),
 enabled INTEGER, client TEXT, subject TEXT, session TEXT,
 candidate_subject TEXT, candidate_session TEXT, subscription TEXT, callback_digest TEXT);
CREATE TABLE IF NOT EXISTS ap_context_requests(id TEXT PRIMARY KEY, request_id TEXT,
 client TEXT, subject TEXT, session TEXT, message_id TEXT UNIQUE,
 created REAL, expires REAL, reply_id TEXT UNIQUE, answer TEXT, completed REAL);
"""


class ContextBridge:
    def __init__(self, store):
        self.store = store
        store.db.executescript(SCHEMA)

    @property
    def resource(self):
        return self.store.config["public_url"] + RESOURCE_PATH

    def control(self):
        with self.store.lock:
            row = self.store.db.execute("SELECT * FROM ap_context_control WHERE id=1").fetchone()
            return dict(row) if row else None

    def active(self):
        row = self.control()
        return bool(row and row["enabled"])

    def connected(self, actor, target):
        return self.active() and {actor, target} == {ORIGIN, "dot"}

    def authorize_client(self, client):
        row = self.control()
        if not row or not row["enabled"] or row["client"] not in (None, client):
            raise Rejected("Context trial disabled or assigned to another OAuth client")
        self.store.db.execute("UPDATE ap_context_control SET client=? WHERE id=1", (client,))

    def token_client_allowed(self, client):
        row = self.control()
        return bool(row and row["enabled"] and row["client"] == client)

    def _subscription(self):
        row = self.control()
        sub = self.store.db.execute("SELECT * FROM subscriptions WHERE id=? AND peer='dot' AND active=1 AND expires>?",
                                    (row["subscription"] if row else "", self.store.clock())).fetchone()
        if not sub or digest(sub["url"]) != row["callback_digest"]:
            raise Rejected("Pinned Dot subscription unavailable or changed; no fallback receiver")
        return sub

    def admin(self, args):
        """Called only behind the existing owner secret and same-origin check."""
        action = args.get("action")
        s = self.store
        with s.lock:
            def operation():
                row = self.control()
                if action == "enable":
                    if row and row["enabled"]:
                        return self.status()
                    if row:  # One trial lifecycle; do not erase or renew old evidence.
                        raise Rejected("Trial already closed; preserve evidence and review a new trial separately")
                    if ORIGIN in s.config["peers"] or "context-requester" in s.config["peers"]:
                        raise Rejected("Reserved trial address conflicts with an existing peer; preserve its route")
                    peer = s.config["peers"].get("dot", {})
                    if peer.get("platform") != "dot":
                        raise Rejected("Trial requires the existing configured dot receiver")
                    sub = s.db.execute("SELECT id,url FROM subscriptions WHERE id=? AND peer='dot' AND active=1 AND expires>?",
                                       (args.get("subscription_id", ""), s.clock())).fetchone()
                    if not sub:
                        raise Rejected("Select the exact existing active Dot subscription")
                    s.db.execute("INSERT INTO ap_context_control VALUES(1,1,NULL,NULL,NULL,NULL,NULL,?,?)", (sub["id"], digest(sub["url"])))
                elif action == "bind":
                    if not row or not row["enabled"] or not row["client"]:
                        raise Rejected("Approve requester OAuth and a no-dispatch handshake first")
                    pair = (args.get("subject"), args.get("session"))
                    if pair != (row["candidate_subject"], row["candidate_session"]) or not all(pair):
                        raise Rejected("Bind exactly the displayed candidate correlation pair")
                    if row["subject"] and pair != (row["subject"], row["session"]):
                        raise Rejected("A bound trial cannot move to another chat")
                    self._subscription()
                    s.db.execute("UPDATE ap_context_control SET subject=?,session=? WHERE id=1", pair)
                elif action == "disable":
                    s.db.execute("UPDATE ap_context_control SET enabled=0 WHERE id=1")
                    s.db.execute("DELETE FROM oauth_tokens WHERE resource=?", (self.resource,))
                    s.db.execute("DELETE FROM oauth_codes WHERE resource=?", (self.resource,))
                    s.db.execute("UPDATE conversations SET stopped=1 WHERE id IN (SELECT message_id FROM ap_context_requests)")
                    s.db.execute("UPDATE messages SET state='stopped',detail='Context trial disabled' WHERE id IN (SELECT message_id FROM ap_context_requests) AND state IN ('queued','waiting')")
                elif action != "status":
                    raise Rejected("Use status, enable, bind or disable")
                return self.status()
            return s.transaction(operation)

    def status(self):
        row = self.control()
        # This result is available only through authenticated owner controls.
        # Expose selection metadata, never callback URLs or signing credentials.
        receivers = self.store.db.execute(
            "SELECT id,url,expires FROM subscriptions WHERE peer='dot' AND active=1 AND expires>? ORDER BY id",
            (self.store.clock(),)).fetchall()
        return {"enabled": bool(row and row["enabled"]), "oauth_approved": bool(row and row["client"]),
                "bound": bool(row and row["subject"]),
                "candidate_subject": row["candidate_subject"] if row else None,
                "candidate_session": row["candidate_session"] if row else None,
                "request_count": self.store.db.execute("SELECT count(*) FROM ap_context_requests").fetchone()[0],
                "limit": 10, "receiver": "dot", "topic": "integration-check",
                "available_receivers": [{"subscription_id": sub["id"], "recipient": "dot",
                                         "callback_host": urlsplit(sub["url"]).hostname,
                                         "expires_at": sub["expires"]} for sub in receivers]}

    def _identity(self, metadata):
        row = self.control()
        if not row or not row["enabled"] or not row["client"]:
            raise Rejected("Context trial is not active")
        if not isinstance(metadata, dict):
            raise Rejected("Missing conversation correlation metadata")
        subject = bounded(metadata.get("openai/subject"), "subject correlation")
        session = bounded(metadata.get("openai/session"), "session correlation")
        if row["subject"]:
            if (subject, session) != (row["subject"], row["session"]):
                raise Rejected("Context trial belongs to another bound conversation")
            return row["client"], subject, session
        if row["candidate_subject"] and (subject, session) != (row["candidate_subject"], row["candidate_session"]):
            raise Rejected("Another candidate is awaiting owner review; no automatic selection")
        self.store.db.execute("UPDATE ap_context_control SET candidate_subject=?,candidate_session=? WHERE id=1", (subject, session))
        return None

    def call(self, name, args, metadata):
        definition = next((t for t in CONTEXT_TOOLS if t["name"] == name), None)
        if not definition or not isinstance(args, dict) or set(args) != set(definition["inputSchema"]["required"]) or any(not isinstance(v, str) for v in args.values()):
            raise Rejected("Only the exact requester context tools and arguments are permitted")
        identifier(args["request_id"])
        if name == "request_context" and (args["destination"], args["topic"], args["question"]) != ("Dot", "integration-check", QUESTION):
            raise Rejected("This trial permits only the approved synthetic card question to Dot")
        s = self.store
        with s.lock:
            def operation():
                identity = self._identity(metadata)
                if identity is None:
                    return {"state": "binding_required", "request_id": args["request_id"], "submitted": False,
                            "note": "Owner must approve the candidate correlation pair before any message is created."}
                key = fingerprint([*identity, args["request_id"]])
                old = s.db.execute("SELECT * FROM ap_context_requests WHERE id=?", (key,)).fetchone()
                if old:
                    return self._result(old, duplicate=name == "request_context")
                if name == "context_result":
                    raise Rejected("Unknown request for this authenticated requester and bound conversation")
                self._subscription()
                if s.db.execute("SELECT count(*) FROM ap_context_requests").fetchone()[0] >= 10:
                    raise Rejected("Ten-request trial limit reached; existing evidence preserved")
                if s.db.execute("SELECT count(*) FROM messages").fetchone()[0] >= 5000:
                    raise Rejected("Existing mailbox capacity reached")
                now, mid = s.clock(), "ctx-" + key
                body = json.dumps({"schema": "agentpeers.context.v1", **args, "expires": now + 300, "required_source": SOURCE}, sort_keys=True)
                scope = "Answer only the synthetic AgentPeers trial card color from the owner-approved fixture. Return request_id, answer and sources with observation times. No business facts or actions."
                s.db.execute("INSERT INTO conversations(id,a,b,scope,expires) VALUES(?,?,?,?,?)", (mid, ORIGIN, "dot", scope, now + 300))
                s.db.execute("INSERT INTO messages(id,conversation_id,sender,recipient,body,expects,created,state) VALUES(?,?,?,?,?,'reply',?,'queued')", (mid, mid, ORIGIN, "dot", body, now))
                s.event(mid, "saved")
                s.db.execute("INSERT INTO ap_context_requests VALUES(?,?,?,?,?,?,?,?,NULL,NULL,NULL)", (key, args["request_id"], *identity, mid, now, now + 300))
                return self._result(s.db.execute("SELECT * FROM ap_context_requests WHERE id=?", (key,)).fetchone())
            return s.transaction(operation)

    def _result(self, row, duplicate=False):
        control = self.control()
        message = self.store.db.execute("SELECT state,claimed_at,read_at FROM messages WHERE id=?", (row["message_id"],)).fetchone()
        conversation = self.store.db.execute("SELECT stopped FROM conversations WHERE id=?", (row["message_id"],)).fetchone()
        if not control or not control["enabled"] or conversation["stopped"]:
            state = "stopped"
        elif row["answer"]:
            state = "completed"
        elif self.store.clock() >= row["expires"]:
            state = "expired"
        elif message["state"] in ("blocked", "unknown", "stopped"):
            state = message["state"]
        else:
            state = "processing" if message["claimed_at"] is not None else "pending"
        result = {"request_id": row["request_id"], "state": state, "duplicate": duplicate,
                  "destination": "Dot", "expires": row["expires"], "authority": AUTHORITY,
                  "delivery_state": message["state"], "read_at": message["read_at"]}
        if state == "completed":
            result.update(json.loads(row["answer"]))
            result.update(responding_agent="dot", completed_at=row["completed"])
        return result

    def handles(self, message_id):
        with self.store.lock:
            return bool(self.store.db.execute("SELECT 1 FROM ap_context_requests WHERE message_id=?", (message_id,)).fetchone())

    def answer(self, actor, args):
        s = self.store
        with s.lock:
            def operation():
                if not self.active() or actor != "dot":
                    raise Rejected("Only the pinned active Dot receiver may answer a context request")
                self._subscription()
                row = s.db.execute("SELECT * FROM ap_context_requests WHERE message_id=?", (args["message_id"],)).fetchone()
                identifier(args["reply_id"])
                if args.get("expects", "none") != "none":
                    raise Rejected("Context replies cannot initiate follow-ups")
                try:
                    payload = json.loads(args["message"])
                except (ValueError, TypeError):
                    raise Rejected("Context reply must contain a structured factual answer") from None
                if not isinstance(payload, dict) or set(payload) != {"request_id", "answer", "sources"} or payload["request_id"] != row["request_id"]:
                    raise Rejected("Context reply must match the original request_id, answer and sources")
                bounded(payload["answer"], "minimal answer", 8000)
                sources = payload["sources"]
                if not isinstance(sources, list) or len(sources) != 1 or not isinstance(sources[0], dict) or set(sources[0]) != {"reference", "observed_at"}:
                    raise Rejected("Use the single approved fixture source and observation time")
                source = sources[0]
                observed = source["observed_at"]
                if source["reference"] != SOURCE or type(observed) not in (int, float) or not math.isfinite(observed) or not 0 <= observed <= s.clock():
                    raise Rejected("Invalid fixture source or observation time")
                canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
                if row["answer"]:
                    if row["reply_id"] == args["reply_id"] and row["answer"] == canonical:
                        return self._result(row, duplicate=True)
                    raise Rejected("Context request already has a different answer; inspect its result")
                message = s._message(args["message_id"])
                stopped = s.db.execute("SELECT stopped FROM conversations WHERE id=?", (row["message_id"],)).fetchone()[0]
                if stopped or row["expires"] <= s.clock() or message["claimed_at"] is None or message["read_at"] is None:
                    raise Rejected("Context request must be unexpired, claimed and acknowledged before answering")
                if s.db.execute("SELECT 1 FROM messages WHERE id=?", (args["reply_id"],)).fetchone() or s.db.execute("SELECT 1 FROM ap_context_requests WHERE reply_id=?", (args["reply_id"],)).fetchone():
                    raise Rejected("Reply ID already belongs to another message")
                s.db.execute("UPDATE ap_context_requests SET answer=?,reply_id=?,completed=? WHERE id=?", (canonical, args["reply_id"], s.clock(), row["id"]))
                s.event(args["message_id"], "context_answered")
                return self._result(s.db.execute("SELECT * FROM ap_context_requests WHERE id=?", (row["id"],)).fetchone())
            return s.transaction(operation)

    def rpc(self, request):
        mid, method, params = request.get("id"), request.get("method"), request.get("params", {})
        if mid is None:
            return None
        try:
            if not isinstance(params, dict):
                raise Rejected("params must be an object")
            if method == "initialize":
                value = {"protocolVersion": params.get("protocolVersion", "2026-07-28"), "capabilities": {"tools": {}},
                         "serverInfo": {"name": "AgentPeers context trial", "version": "0.7.0"},
                         "instructions": AUTHORITY + " Only the synthetic card trial is permitted. Retrieve results explicitly; no chat wake is promised."}
            elif method == "tools/list":
                value = {"tools": CONTEXT_TOOLS}
            elif method == "server/discover":
                value = {"resultType": "complete", "supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}}
            elif method == "ping":
                value = {}
            elif method == "tools/call":
                output = self.call(params.get("name"), params.get("arguments"), params.get("_meta"))
                value = {"content": [{"type": "text", "text": json.dumps(output)}], "structuredContent": output}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not available to a context requester"}}
        except Rejected as exc:
            value = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
        return {"jsonrpc": "2.0", "id": mid, "result": value}
