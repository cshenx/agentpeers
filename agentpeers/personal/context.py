"""Isolated factual-context prototype. No HTTP, OAuth, event or live agent wiring.

Principals and grants are supplied by a trusted host adapter, never tool arguments
or MCP metadata. Only fixture adapters exist at this stage.
"""
from dataclasses import dataclass
from contextlib import contextmanager
import hashlib
import json
import math
import sqlite3
import threading
import time

from . import AUTHORITY
from .config import Rejected, identifier
from .protocol import tool, STRING


@dataclass(frozen=True)
class Requester:
    owner: str
    subject: str
    session: str


@dataclass(frozen=True)
class Receiver:
    owner: str
    peer: str


@dataclass(frozen=True)
class Grant:
    owner: str
    subject: str
    destination: str
    receiver: str
    topic: str
    scope: str


CONTEXT_TOOLS = [
    tool("request_context", "Request one minimal factual answer on an approved topic. Use one stable request_id. Pending means saved, not delivered or read; retrieve explicitly with context_result.",
         {k: STRING for k in ("request_id", "destination", "topic", "question")},
         ["request_id", "destination", "topic", "question"]),
    tool("context_result", "Retrieve only this authenticated requesting conversation's context answer or pending/terminal state. No background chat wake is promised.",
         {"request_id": STRING}, ["request_id"], readonly=True),
]


def bounded(value, name, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > maximum:
        raise Rejected("Invalid " + name)
    return value


def fingerprint(values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class ContextRequests:
    """Durable local contract with an explicit, separately supplied allowlist.

    The path must be a dedicated prototype database, never the live mailbox.
    Grants approve exact owner/subject/destination/topic combinations and pin the
    receiver identity. A named Codex receiver uses its exact native session ID.
    """
    def __init__(self, path, grants=(), clock=time.time, ttl=300, capacity=1000):
        if type(ttl) is not int or not 1 <= ttl <= 3600 or type(capacity) is not int or not 1 <= capacity <= 5000:
            raise Rejected("Invalid prototype bounds")
        self.grants, self.clock, self.ttl, self.capacity = tuple(grants), clock, ttl, capacity
        keys = set()
        for grant in self.grants:
            if type(grant) is not Grant:
                raise Rejected("Expected a host-approved grant")
            for field in ("owner", "subject", "destination", "receiver", "topic", "scope"):
                bounded(getattr(grant, field), field, 2000 if field == "scope" else 200)
            key = (grant.owner, grant.subject, grant.destination, grant.topic)
            if key in keys:
                raise Rejected("Ambiguous context destination/topic")
            keys.add(key)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        application = self.db.execute("PRAGMA application_id").fetchone()[0]
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        populated = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
        if application not in (0, 0x41504358) or version not in (0, 1) or populated and application != 0x41504358:
            self.db.close()
            raise Rejected("Use a separate context prototype database; preserve existing data")
        self.db.execute("PRAGMA application_id=1095779160")  # APCX
        self.db.execute("PRAGMA user_version=1")
        self.db.execute("""CREATE TABLE IF NOT EXISTS context_requests(
            delivery_id TEXT PRIMARY KEY, request_id TEXT, owner TEXT, subject TEXT,
            session TEXT, destination TEXT, receiver TEXT, topic TEXT, grant_hash TEXT,
            question TEXT, created REAL, expires REAL, state TEXT, answer TEXT,
            completed REAL, failure TEXT)""")

    def close(self):
        with self.lock:
            self.db.close()

    @contextmanager
    def _transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def _requester(self, principal):
        if type(principal) is not Requester:
            raise Rejected("A separately authenticated ChatGPT requester is required")
        return tuple(bounded(getattr(principal, field), field) for field in ("owner", "subject", "session"))

    def _grant(self, owner, subject, destination, topic):
        matches = [g for g in self.grants if (g.owner, g.subject, g.destination, g.topic) == (owner, subject, destination, topic)]
        if len(matches) != 1:
            raise Rejected("Destination and topic require an exact owner-approved grant")
        return matches[0]

    def _grant_hash(self, grant):
        return fingerprint([grant.owner, grant.subject, grant.destination, grant.receiver, grant.topic, grant.scope])

    def _row(self, delivery_id):
        row = self.db.execute("SELECT * FROM context_requests WHERE delivery_id=?", (delivery_id,)).fetchone()
        if row is None:
            raise Rejected("Unknown context request for this identity")
        grant = self._grant(row["owner"], row["subject"], row["destination"], row["topic"])
        if self._grant_hash(grant) != row["grant_hash"]:
            raise Rejected("Context route changed; previous request cannot be reassigned")
        return row

    def _state(self, row):
        if row["state"] in ("pending", "processing") and self.clock() >= row["expires"]:
            return "expired"
        return row["state"]

    def _public(self, row, duplicate=False):
        result = {k: row[k] for k in ("request_id", "destination", "topic", "created", "expires")}
        result.update(state=self._state(row), duplicate=duplicate, authority=AUTHORITY)
        if result["state"] == "completed":
            result.update(json.loads(row["answer"]))
            result["completed_at"] = row["completed"]
        if result["state"] == "failed":
            result["reason"] = row["failure"]
        return result

    def request_context(self, principal, request_id, destination, topic, question):
        owner, subject, session = self._requester(principal)
        identifier(request_id)
        bounded(question, "factual question", 4000)
        grant = self._grant(owner, subject, destination, topic)
        delivery_id = fingerprint([owner, subject, session, request_id])
        with self._transaction():
            old = self.db.execute("SELECT * FROM context_requests WHERE delivery_id=?", (delivery_id,)).fetchone()
            if old:
                row = self._row(delivery_id)
                if (row["destination"], row["topic"], row["question"]) != (destination, topic, question):
                    raise Rejected("Request ID already belongs to different content")
                return self._public(row, duplicate=True)
            if self.db.execute("SELECT count(*) FROM context_requests").fetchone()[0] >= self.capacity:
                raise Rejected("Context prototype capacity reached; preserve existing requests")
            now = self.clock()
            self.db.execute("INSERT INTO context_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (delivery_id, request_id, owner, subject, session, destination, grant.receiver,
                             topic, self._grant_hash(grant), question, now, now + self.ttl, "pending", None, None, None))
            return self._public(self._row(delivery_id))

    def context_result(self, principal, request_id):
        identity = self._requester(principal)
        identifier(request_id)
        with self.lock:
            return self._public(self._row(fingerprint([*identity, request_id])))

    def _receiver_row(self, receiver, delivery_id):
        if type(receiver) is not Receiver:
            raise Rejected("A separately authenticated receiver is required")
        bounded(receiver.owner, "receiver owner")
        bounded(receiver.peer, "receiver identity")
        row = self._row(delivery_id)
        if (receiver.owner, receiver.peer) != (row["owner"], row["receiver"]):
            raise Rejected("Request belongs to another approved receiver")
        return row

    def pending(self, receiver):
        """Fixture receiver discovery; no notification or scheduled polling."""
        if type(receiver) is not Receiver:
            raise Rejected("A separately authenticated receiver is required")
        with self.lock:
            candidates = self.db.execute("SELECT delivery_id FROM context_requests WHERE owner=? AND receiver=? AND state='pending' AND expires>? ORDER BY created",
                                         (receiver.owner, receiver.peer, self.clock())).fetchall()
            result = []
            for row in candidates:
                try:
                    self._receiver_row(receiver, row["delivery_id"])
                except Rejected:
                    continue
                result.append(row["delivery_id"])
            return result

    def claim(self, receiver, delivery_id):
        with self._transaction():
            row = self._receiver_row(receiver, delivery_id)
            if self._state(row) != "pending":
                return {"claimed": False, "state": self._state(row)}
            changed = self.db.execute("UPDATE context_requests SET state='processing' WHERE delivery_id=? AND state='pending' AND expires>?",
                                      (delivery_id, self.clock())).rowcount
            if not changed:
                return {"claimed": False}
            grant = self._grant(row["owner"], row["subject"], row["destination"], row["topic"])
            return {"claimed": True, "delivery_id": delivery_id, "request_id": row["request_id"],
                    "question": row["question"], "topic": row["topic"], "expires": row["expires"],
                    "requester": {"role": "chatgpt_requester", **{k: row[k] for k in ("owner", "subject", "session")}},
                    "destination": row["destination"], "claimed_scope": grant.scope, "authority": AUTHORITY}

    def answer(self, receiver, delivery_id, answer, sources):
        bounded(answer, "minimal factual answer", 8000)
        if not isinstance(sources, list) or not 1 <= len(sources) <= 5:
            raise Rejected("Supply 1–5 source references with observation times")
        for source in sources:
            if not isinstance(source, dict) or set(source) != {"reference", "observed_at"}:
                raise Rejected("Each source requires reference and observed_at")
            bounded(source["reference"], "source reference", 1000)
            observed = source["observed_at"]
            if type(observed) not in (int, float) or not math.isfinite(observed) or not 0 <= observed <= self.clock():
                raise Rejected("Source observation time must be a finite past Unix timestamp")
        with self._transaction():
            row = self._receiver_row(receiver, delivery_id)
            payload = json.dumps({"answer": answer, "sources": sources, "responding_agent": row["receiver"]}, sort_keys=True, ensure_ascii=False)
            if row["state"] == "completed" and row["answer"] == payload:
                return self._public(row, duplicate=True)
            if self._state(row) != "processing":
                raise Rejected("Only a claimed, unexpired request may receive one answer")
            self.db.execute("UPDATE context_requests SET state='completed',answer=?,completed=? WHERE delivery_id=?",
                            (payload, self.clock(), delivery_id))
            return self._public(self._row(delivery_id))

    def fail(self, receiver, delivery_id, reason):
        if reason not in ("unavailable", "blocked", "unsupported"):
            raise Rejected("Use unavailable, blocked or unsupported")
        with self._transaction():
            row = self._receiver_row(receiver, delivery_id)
            if row["state"] == "failed" and row["failure"] == reason:
                return self._public(row, duplicate=True)
            if self._state(row) != "processing":
                raise Rejected("Only a claimed, unexpired request may fail")
            self.db.execute("UPDATE context_requests SET state='failed',failure=? WHERE delivery_id=?", (reason, delivery_id))
            return self._public(self._row(delivery_id))


class ContextProtocol:
    """Local requester consumer. Deliberately not registered in the live App."""
    def __init__(self, requests):
        self.requests = requests

    def call(self, authenticated_requester, name, arguments):
        self.requests._requester(authenticated_requester)
        definition = next((t for t in CONTEXT_TOOLS if t["name"] == name), None)
        if not definition or not isinstance(arguments, dict):
            raise Rejected("Unknown context tool or invalid arguments")
        schema = definition["inputSchema"]
        if set(arguments) != set(schema["required"]) or any(not isinstance(value, str) for value in arguments.values()):
            raise Rejected("Missing or unexpected context tool arguments")
        return getattr(self.requests, name)(authenticated_requester, **arguments)
