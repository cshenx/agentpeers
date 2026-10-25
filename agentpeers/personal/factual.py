"""Scoped factual questions from ordinary chats; no inbox, events or action tools.

OAuth authenticates a requester client. Owner-approved session labels correlate
one chat; they are not cryptographic proof of native chat identity. The existing
receiver remains responsible for checking the question against its human scope.
"""
from dataclasses import dataclass
from functools import wraps
import json
import math
from urllib.parse import quote, urlsplit

from .. import __version__
from . import AUTHORITY
from .config import Rejected, digest, identifier
from .context import bounded, fingerprint
from .protocol import STRING, tool


ORIGIN = "chatgpt-context"
RESOURCE_PATH = "/mcp/context"
SCOPE = "agentpeers:facts"


def context_reference(topic):
    return "dot:context/" + quote(topic, safe="")


SCHEMA = """
CREATE TABLE IF NOT EXISTS ap_facts_policies(topic TEXT PRIMARY KEY, scope TEXT,
 refs TEXT, subscription TEXT, callback_digest TEXT, revision INTEGER, active INTEGER);
CREATE TABLE IF NOT EXISTS ap_facts_clients(client TEXT PRIMARY KEY, active INTEGER);
CREATE TABLE IF NOT EXISTS ap_facts_candidates(id TEXT PRIMARY KEY, client TEXT,
 subject TEXT, session TEXT, topic TEXT, revision INTEGER, question TEXT, expires REAL);
CREATE TABLE IF NOT EXISTS ap_facts_grants(id TEXT PRIMARY KEY, client TEXT,
 subject TEXT, session TEXT, topic TEXT, revision INTEGER, expires REAL, active INTEGER);
CREATE TABLE IF NOT EXISTS ap_facts_requests(id TEXT PRIMARY KEY, request_id TEXT,
 grant_id TEXT, message_id TEXT UNIQUE, question TEXT, created REAL, expires REAL,
 result TEXT, reply_id TEXT UNIQUE, completed REAL);
CREATE TABLE IF NOT EXISTS ap_facts_owner_sessions(hash TEXT PRIMARY KEY, expires REAL);
CREATE INDEX IF NOT EXISTS ap_facts_grant_chat ON ap_facts_grants(client,subject,session,topic);
"""
TOOLS = [
    tool("context_destinations", "List the owner-configured factual topics available for Dot. This grants no access to answers or inboxes.", readonly=True),
    tool("request_context", "Ask Dot one factual question within an owner-approved topic. Discover topics first. Use a stable request_id. If approval_required, show the pairing code to the human and stop; nothing was sent. Pending is not delivery or reading. Retrieve explicitly with context_result; never schedule polling or automatically resend uncertain work.",
         {k: STRING for k in ("request_id", "destination", "topic", "question")},
         ["request_id", "destination", "topic", "question"]),
    tool("context_result", "Retrieve this authenticated chat's existing factual answer or terminal state. The answer includes receiver-asserted sources and times, not independently verified facts. No background chat wake is promised.",
         {"request_id": STRING}, ["request_id"], readonly=True),
]
for definition in TOOLS:
    definition["securitySchemes"] = [{"type": "oauth2", "scopes": [SCOPE]}]
    definition["_meta"] = {"securitySchemes": definition["securitySchemes"]}


@dataclass(frozen=True)
class FactualCaller:
    """Constructed only by the authenticated HTTP adapter, never tool arguments."""
    client: str


def locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.store.lock:
            return method(self, *args, **kwargs)
    return call


class FactualContext:
    origin, scope = ORIGIN, SCOPE

    def __init__(self, store):
        self.store = store
        store.db.executescript(SCHEMA)

    @property
    def resource(self):
        return self.store.config["public_url"] + RESOURCE_PATH

    def available(self):
        return ("context" not in self.store.config["peers"] and ORIGIN not in self.store.config["peers"]
                and self.store.config["peers"].get("dot", {}).get("platform") == "dot")

    @locked
    def active(self):
        return self.available() and bool(self.store.db.execute("SELECT 1 FROM ap_facts_policies WHERE active=1").fetchone())

    def connected(self, actor, target):
        return self.available() and {actor, target} == {ORIGIN, "dot"}

    @locked
    def authorize_client(self, client):
        if not self.active():
            raise Rejected("Save an allowed factual topic at /context first")
        self.store.db.execute("INSERT INTO ap_facts_clients VALUES(?,1) ON CONFLICT(client) DO UPDATE SET active=1", (client,))

    @locked
    def token_client_allowed(self, client):
        return self.available() and bool(self.store.db.execute("SELECT 1 FROM ap_facts_clients WHERE client=? AND active=1", (client,)).fetchone())

    @locked
    def receivers(self):
        return [{"id": r["id"], "host": urlsplit(r["url"]).hostname,
                 "expires": r["expires"]} for r in self.store.db.execute(
                     "SELECT id,url,expires FROM subscriptions WHERE peer='dot' AND active=1 AND expires>?", (self.store.clock(),))]

    def _subscription(self, policy):
        sub = self.store.db.execute("SELECT * FROM subscriptions WHERE id=? AND peer='dot' AND active=1 AND expires>?",
                                    (policy["subscription"], self.store.clock())).fetchone()
        if not sub or digest(sub["url"]) != policy["callback_digest"]:
            raise Rejected("Approved Dot receiver unavailable or changed; no fallback destination")
        return sub

    @locked
    def policy(self, topic):
        row = self.store.db.execute("SELECT * FROM ap_facts_policies WHERE topic=? AND active=1", (topic,)).fetchone()
        if not self.available() or not row:
            raise Rejected("Destination and topic need an owner-approved factual policy")
        return row

    @locked
    def status(self):
        s = self.store
        return {"policies": [dict(r) for r in s.db.execute("SELECT * FROM ap_facts_policies ORDER BY topic")],
                "candidates": [dict(r) for r in s.db.execute("SELECT * FROM ap_facts_candidates WHERE expires>? ORDER BY expires", (s.clock(),))],
                "grants": [dict(r) for r in s.db.execute("SELECT id,topic,expires,active FROM ap_facts_grants ORDER BY expires DESC LIMIT 100")],
                "receivers": self.receivers(), "request_count": s.db.execute("SELECT count(*) FROM ap_facts_requests").fetchone()[0]}

    def _stop_grant(self, grant_id):
        s = self.store
        s.db.execute("UPDATE ap_facts_grants SET active=0 WHERE id=?", (grant_id,))
        s.db.execute("UPDATE conversations SET stopped=1 WHERE id IN (SELECT message_id FROM ap_facts_requests WHERE grant_id=?)", (grant_id,))
        s.db.execute("UPDATE messages SET state='stopped',lease=NULL WHERE id IN (SELECT message_id FROM ap_facts_requests WHERE grant_id=?) AND state IN ('queued','waiting')", (grant_id,))

    def revoke_client(self, client):
        """Called inside the OAuth revocation transaction, for this resource only."""
        self.store.db.execute("UPDATE ap_facts_clients SET active=0 WHERE client=?", (client,))
        self.store.db.execute("UPDATE ap_facts_candidates SET expires=0 WHERE client=?", (client,))
        for row in self.store.db.execute("SELECT id FROM ap_facts_grants WHERE client=? AND active=1", (client,)).fetchall():
            self._stop_grant(row["id"])

    def admin(self, args):
        """Only the owner-authenticated, same-origin UI may call this method."""
        s = self.store
        with s.lock:
            def operation():
                if not self.available():
                    raise Rejected("Context addresses conflict with an existing peer, or Dot is not configured; existing routes preserved")
                action = args.get("action")
                if action == "save_policy":
                    topic = bounded(args.get("topic", ""), "topic name", 100).strip()
                    scope = bounded(args.get("scope"), "allowed factual scope", 2000)
                    refs = [v.strip() for v in args.get("refs", "").splitlines() if v.strip()]
                    if args.get("dot_context") == "yes":
                        refs.insert(0, context_reference(topic))
                    refs = list(dict.fromkeys(refs))
                    if not 1 <= len(refs) <= 5 or any(not isinstance(v, str) or len(v.encode()) > 500 or "*" in v for v in refs):
                        raise Rejected("Give 1–5 exact source references or source roots, without wildcards")
                    sub = s.db.execute("SELECT id,url FROM subscriptions WHERE id=? AND peer='dot' AND active=1 AND expires>?",
                                       (args.get("subscription"), s.clock())).fetchone()
                    if not sub:
                        raise Rejected("Choose the current existing Dot receiver")
                    old = s.db.execute("SELECT * FROM ap_facts_policies WHERE topic=?", (topic,)).fetchone()
                    if not old and s.db.execute("SELECT count(*) FROM ap_facts_policies").fetchone()[0] >= 32:
                        raise Rejected("Allowed-topic capacity reached")
                    values = (scope, json.dumps(refs), sub["id"], digest(sub["url"]))
                    if old and old["active"] and tuple(old[k] for k in ("scope", "refs", "subscription", "callback_digest")) == values:
                        return self.status()
                    revision = old["revision"] + 1 if old else 1
                    for grant in s.db.execute("SELECT id FROM ap_facts_grants WHERE topic=? AND active=1", (topic,)).fetchall():
                        self._stop_grant(grant["id"])
                    s.db.execute("INSERT INTO ap_facts_policies VALUES(?,?,?,?,?,?,1) ON CONFLICT(topic) DO UPDATE SET scope=excluded.scope,refs=excluded.refs,subscription=excluded.subscription,callback_digest=excluded.callback_digest,revision=excluded.revision,active=1",
                                 (topic, *values, revision))
                elif action == "allow_chat":
                    candidate = s.db.execute("SELECT * FROM ap_facts_candidates WHERE id=?", (args.get("candidate"),)).fetchone()
                    if not candidate:
                        raise Rejected("Pairing expired or unknown; use the intended chat to prepare a new request")
                    policy = self.policy(candidate["topic"])
                    if policy["revision"] != candidate["revision"] or not self.token_client_allowed(candidate["client"]):
                        raise Rejected("Policy or requester changed; prepare a new request for review")
                    if candidate["expires"] <= s.clock():
                        approved = s.db.execute("SELECT 1 FROM ap_facts_grants WHERE id=? AND active=1 AND expires>?", ("grant_" + candidate["id"], s.clock())).fetchone()
                        if approved:
                            return self.status()  # Lost-response/double-click retry never extends access.
                        raise Rejected("Pairing expired or revoked; prepare a new request in the intended chat")
                    self._subscription(policy)
                    try:
                        days = int(args.get("days", "30"))
                    except (ValueError, TypeError):
                        raise Rejected("Choose 1–30 days") from None
                    if not 1 <= days <= 30:
                        raise Rejected("Choose 1–30 days")
                    if s.db.execute("SELECT count(*) FROM ap_facts_grants").fetchone()[0] >= 1000:
                        raise Rejected("Grant capacity reached; preserve existing records")
                    for grant in s.db.execute("SELECT id FROM ap_facts_grants WHERE client=? AND subject=? AND session=? AND topic=? AND active=1",
                                               tuple(candidate[k] for k in ("client", "subject", "session", "topic"))).fetchall():
                        self._stop_grant(grant["id"])
                    s.db.execute("INSERT INTO ap_facts_grants VALUES(?,?,?,?,?,?,?,1)",
                                 ("grant_" + candidate["id"], *(candidate[k] for k in ("client", "subject", "session", "topic", "revision")), s.clock() + days * 86400))
                    s.db.execute("UPDATE ap_facts_candidates SET expires=0 WHERE id=?", (candidate["id"],))
                elif action == "revoke_chat":
                    self._stop_grant(identifier(args.get("grant", "")))
                elif action in ("disable_topic", "disconnect"):
                    topic = bounded(args.get("topic"), "topic name", 100).strip() if action == "disable_topic" else None
                    rows = s.db.execute("SELECT id FROM ap_facts_grants WHERE active=1" + (" AND topic=?" if topic else ""), (topic,) if topic else ()).fetchall()
                    for grant in rows:
                        self._stop_grant(grant["id"])
                    if topic:
                        s.db.execute("UPDATE ap_facts_policies SET active=0 WHERE topic=?", (topic,))
                    else:
                        s.db.execute("UPDATE ap_facts_policies SET active=0")
                        s.db.execute("UPDATE ap_facts_clients SET active=0")
                        s.db.execute("UPDATE ap_facts_candidates SET expires=0")
                        s.db.execute("DELETE FROM oauth_tokens WHERE resource=?", (self.resource,))
                        s.db.execute("DELETE FROM oauth_codes WHERE resource=?", (self.resource,))
                else:
                    raise Rejected("Unknown context owner action")
                return self.status()
            return s.transaction(operation)

    def _identity(self, client, metadata):
        if not self.token_client_allowed(client) or not isinstance(metadata, dict):
            raise Rejected("Authenticated requester and conversation correlation are required")
        return client, bounded(metadata.get("openai/subject"), "subject correlation"), bounded(metadata.get("openai/session"), "session correlation")

    def _grant(self, identity, topic=None, grant_id=None):
        sql = "SELECT g.* FROM ap_facts_grants g JOIN ap_facts_policies p ON p.topic=g.topic AND p.revision=g.revision AND p.active=1 WHERE g.client=? AND g.subject=? AND g.session=? AND g.active=1 AND g.expires>?"
        values = (*identity, self.store.clock())
        if grant_id is not None:
            sql += " AND g.id=?"; values += (grant_id,)
        else:
            sql += " AND g.topic=?"; values += (topic,)
        rows = self.store.db.execute(sql, values).fetchall()
        return rows[0] if len(rows) == 1 else None

    def call(self, client, name, args, metadata):
        definition = next((t for t in TOOLS if t["name"] == name), None)
        if not definition or not isinstance(args, dict) or set(args) != set(definition["inputSchema"]["required"]) or any(not isinstance(v, str) for v in args.values()):
            raise Rejected("Only the exact factual requester tools and arguments are available")
        s = self.store
        with s.lock:
            def operation():
                if not self.token_client_allowed(client):
                    raise Rejected("Requester connection is inactive")
                if name == "context_destinations":
                    return {"destinations": [{"destination": "Dot", "topic": p["topic"], "scope": p["scope"], "sources": json.loads(p["refs"])} for p in s.db.execute("SELECT topic,scope,refs FROM ap_facts_policies WHERE active=1 ORDER BY topic")], "authority": AUTHORITY}
                identity = self._identity(client, metadata)
                identifier(args["request_id"])
                key = fingerprint([*identity, args["request_id"]])
                old = s.db.execute("SELECT * FROM ap_facts_requests WHERE id=?", (key,)).fetchone()
                if name == "context_result":
                    if not old or not self._grant(identity, grant_id=old["grant_id"]):
                        raise Rejected("No active permission for that request in this chat")
                    return self._result(old)
                if args["destination"] != "Dot":
                    raise Rejected("Only the configured Dot context receiver is implemented")
                question = bounded(args["question"], "factual question", 4000)
                policy = self.policy(args["topic"])
                grant = self._grant(identity, topic=args["topic"])
                if not grant:
                    cid = fingerprint([*identity, args["topic"], policy["revision"], args["request_id"]])
                    candidate = s.db.execute("SELECT * FROM ap_facts_candidates WHERE id=?", (cid,)).fetchone()
                    if not candidate:
                        if s.db.execute("SELECT count(*) FROM ap_facts_candidates").fetchone()[0] >= 5000:
                            raise Rejected("Pairing capacity reached; existing evidence preserved")
                        s.db.execute("INSERT INTO ap_facts_candidates VALUES(?,?,?,?,?,?,?,?)", (cid, *identity, args["topic"], policy["revision"], question, s.clock() + 600))
                        candidate = s.db.execute("SELECT * FROM ap_facts_candidates WHERE id=?", (cid,)).fetchone()
                    if candidate["question"] != question:
                        raise Rejected("Pairing request ID already belongs to different content")
                    return {"request_id": args["request_id"], "state": "approval_required" if candidate["expires"] > s.clock() else "approval_expired",
                            "submitted": False, "pairing_code": cid[:12].upper(), "owner_page": s.config["public_url"] + "/context",
                            "note": "The human must match this code in Context access and allow this chat. Nothing was sent. Do not retry until approved."}
                if old:
                    if old["grant_id"] != grant["id"] or old["question"] != question:
                        raise Rejected("Request ID belongs to another question or earlier grant; do not replay it")
                    return self._result(old, duplicate=True)
                self._subscription(policy)
                if s.db.execute("SELECT count(*) FROM messages").fetchone()[0] >= 5000 or s.db.execute("SELECT count(*) FROM ap_facts_requests").fetchone()[0] >= 5000:
                    raise Rejected("Mailbox capacity reached; existing records preserved")
                now, mid = s.clock(), "fact-" + key
                body = json.dumps({"schema": "agentpeers.factual.v1", **args, "expires": now + 300,
                                   "grant_id": grant["id"], "grant_expires": grant["expires"], "policy_revision": policy["revision"],
                                   "owner_scope": policy["scope"], "allowed_sources": json.loads(policy["refs"]),
                                   "context_reference": context_reference(policy["topic"]) if context_reference(policy["topic"]) in json.loads(policy["refs"]) else None,
                                   "reply_format": {"request_id": args["request_id"], "status": "answered | unavailable | out_of_scope", "answer": "short factual answer or reason", "sources": [{"reference": "actual approved source reference", "observed_at": "Unix seconds when actually observed"}]}}, sort_keys=True)
                s.db.execute("INSERT INTO conversations(id,a,b,scope,expires) VALUES(?,?,?,?,?)", (mid, ORIGIN, "dot", policy["scope"], now + 300))
                s.db.execute("INSERT INTO messages(id,conversation_id,sender,recipient,body,expects,created,state) VALUES(?,?,?,?,?,'reply',?,'queued')", (mid, mid, ORIGIN, "dot", body, now))
                s.event(mid, "saved")
                s.db.execute("INSERT INTO ap_facts_requests VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL)", (key, args["request_id"], grant["id"], mid, question, now, now + 300))
                return self._result(s.db.execute("SELECT * FROM ap_facts_requests WHERE id=?", (key,)).fetchone())
            return s.transaction(operation)

    def _result(self, row, duplicate=False):
        s = self.store
        message = s.db.execute("SELECT state,read_at,claimed_at FROM messages WHERE id=?", (row["message_id"],)).fetchone()
        conv = s.db.execute("SELECT stopped FROM conversations WHERE id=?", (row["message_id"],)).fetchone()
        state = "stopped" if conv["stopped"] else "completed" if row["result"] else "expired" if s.clock() >= row["expires"] else message["state"] if message["state"] in ("blocked", "unknown", "stopped") else "processing" if message["claimed_at"] is not None else "pending"
        value = {"request_id": row["request_id"], "state": state, "duplicate": duplicate, "destination": "Dot", "expires": row["expires"], "delivery_state": message["state"], "read_at": message["read_at"], "authority": AUTHORITY}
        if state == "completed":
            value.update(json.loads(row["result"]), responding_agent="dot", completed_at=row["completed"])
        return value

    @locked
    def handles(self, message_id):
        return bool(self.store.db.execute("SELECT 1 FROM ap_facts_requests WHERE message_id=?", (message_id,)).fetchone())

    def answer(self, actor, args):
        s = self.store
        with s.lock:
            def operation():
                row = s.db.execute("SELECT * FROM ap_facts_requests WHERE message_id=?", (args["message_id"],)).fetchone()
                grant = s.db.execute("SELECT * FROM ap_facts_grants WHERE id=?", (row["grant_id"],)).fetchone() if row else None
                if actor != "dot" or not grant or not self.token_client_allowed(grant["client"]) or not self._grant(tuple(grant[k] for k in ("client", "subject", "session")), grant_id=grant["id"]):
                    raise Rejected("Only the approved Dot receiver may answer an active context request")
                policy = self.policy(grant["topic"])
                self._subscription(policy)
                identifier(args["reply_id"])
                if args.get("expects", "none") != "none":
                    raise Rejected("Factual results cannot start follow-ups")
                try:
                    payload = json.loads(args["message"])
                except (ValueError, TypeError):
                    raise Rejected("Reply must contain the structured factual result") from None
                if not isinstance(payload, dict) or set(payload) != {"request_id", "status", "answer", "sources"} or payload["request_id"] != row["request_id"] or payload["status"] not in ("answered", "unavailable", "out_of_scope"):
                    raise Rejected("Return the matching request_id, status, answer and sources")
                bounded(payload["answer"], "short answer or reason", 8000)
                sources = payload["sources"]
                if not isinstance(sources, list) or not (1 if payload["status"] == "answered" else 0) <= len(sources) <= 5:
                    raise Rejected("An answer needs 1–5 actual approved sources; a refusal may use none")
                for source in sources:
                    if not isinstance(source, dict) or set(source) != {"reference", "observed_at"}:
                        raise Rejected("Each source needs reference and observed_at")
                    reference = bounded(source["reference"], "source reference", 2000)
                    if not any(reference == root or reference.startswith(root.rstrip('/') + '/') or reference.startswith(root + '#') for root in json.loads(policy["refs"])):
                        raise Rejected("Source is outside the owner's approved references")
                    observed = source["observed_at"]
                    if type(observed) not in (int, float) or not math.isfinite(observed) or not 0 <= observed <= s.clock():
                        raise Rejected("Source observation time must be actual finite Unix seconds, not in the future")
                canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
                if row["result"]:
                    if row["reply_id"] == args["reply_id"] and row["result"] == canonical:
                        return self._result(row, duplicate=True)
                    raise Rejected("A different result is already saved; inspect the existing request")
                message = s._message(row["message_id"])
                conv = s.db.execute("SELECT stopped FROM conversations WHERE id=?", (row["message_id"],)).fetchone()
                if conv["stopped"] or row["expires"] <= s.clock() or message["claimed_at"] is None or message["read_at"] is None:
                    raise Rejected("Request must be unexpired, claimed and acknowledged before answering")
                if s.db.execute("SELECT 1 FROM messages WHERE id=?", (args["reply_id"],)).fetchone() or s.db.execute("SELECT 1 FROM ap_facts_requests WHERE reply_id=?", (args["reply_id"],)).fetchone():
                    raise Rejected("Reply ID belongs to another message")
                s.db.execute("UPDATE ap_facts_requests SET result=?,reply_id=?,completed=? WHERE id=?", (canonical, args["reply_id"], s.clock(), row["id"]))
                s.event(row["message_id"], "factual_answered")
                return self._result(s.db.execute("SELECT * FROM ap_facts_requests WHERE id=?", (row["id"],)).fetchone())
            return s.transaction(operation)

    def rpc(self, client, request):
        mid, method, params = request.get("id"), request.get("method"), request.get("params", {})
        if mid is None:
            return None
        try:
            if not isinstance(params, dict):
                raise Rejected("params must be an object")
            if method == "initialize":
                value = {"protocolVersion": params.get("protocolVersion", "2026-07-28"), "capabilities": {"tools": {}},
                         "serverInfo": {"name": "AgentPeers Context", "version": __version__},
                         "instructions": AUTHORITY + " Discover factual topics. Show approval_required pairing codes to the human; do not retry until approved. Retrieve a pending answer explicitly. No scheduled polling or chat wake, inbox, general messaging or business actions."}
            elif method == "tools/list":
                value = {"tools": TOOLS}
            elif method == "server/discover":
                value = {"resultType": "complete", "supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}}
            elif method == "ping":
                value = {}
            elif method == "tools/call":
                result = self.call(client, params.get("name"), params.get("arguments"), params.get("_meta"))
                value = {"content": [{"type": "text", "text": json.dumps(result)}], "structuredContent": result}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Not available to a factual requester"}}
        except Rejected as exc:
            value = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
        return {"jsonrpc": "2.0", "id": mid, "result": value}
