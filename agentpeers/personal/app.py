"""Shared personal-agent application, independent of HTTP server and cloud host."""
import hmac
import json
import threading
from .. import __version__
from . import AUTHORITY
from .adapters import Dispatcher, Events, post_https
from .auth import OAuth
from .context_bridge import ContextBridge, ORIGIN
from .factual import FactualContext, FactualCaller
from .context_ui import ContextUI
from .config import Rejected, digest
from .protocol import Protocol, TOOLS
from .transport import run


class App:
    def __init__(self, store, post=post_https, rate_lock=None):
        self.store = store
        self.context = store.context = ContextBridge(store)
        self.factual = store.factual = FactualContext(store)
        self.context_ui = ContextUI(self.factual)
        self.oauth = OAuth(store)
        self.events = Events(store, post)
        self.protocol = Protocol(store, self.events)
        self.dispatcher = Dispatcher(store, post)
        self.rates = {}
        self.rate_lock = rate_lock if rate_lock is not None else threading.Lock()
        self.worker_warning = False

    def limit(self, source, action):
        with self.rate_lock:
            now = self.store.clock()
            self.rates = {k: v for k, v in self.rates.items() if now - v[0] < 60}
            key = (source, action)
            started, count = self.rates.get(key, (now, 0))
            ceiling = 10 if action in ("/oauth/authorize", "/context/admin", "/context/login") else 60
            if count >= ceiling or len(self.rates) > 10000:
                raise Rejected("Request limit reached; wait before trying again")
            self.rates[key] = (started, count + 1)

    def rpc(self, actor, request):
        return run(self.rpc_steps(actor, request), self.events.post)

    def rpc_steps(self, actor, request):
        if isinstance(actor, FactualCaller):
            return self.factual.rpc(actor.client, request)
        if actor == ORIGIN and actor not in self.store.config["peers"]:
            return self.context.rpc(request)
        mid = request.get("id")
        if mid is None:
            return None
        method, params = request.get("method"), request.get("params", {})
        try:
            if not isinstance(params, dict):
                raise Rejected("params must be an object")
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion", "2026-07-28"), "capabilities": {"tools": {}, "events": {}},
                          "serverInfo": {"name": "AgentPeers personal agents", "version": __version__}, "instructions": AUTHORITY + " Discover with list_peers. On each event claim_message once, stop if not claimed, consume and acknowledge, then reply only when expects=reply and within the human's scope. Use stable IDs. Never poll on a schedule or auto-ack unread content."}
            elif method == "server/discover":
                result = {"resultType": "complete", "supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}, "events": {}}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                try:
                    value = self.protocol.call(actor, params.get("name"), params.get("arguments", {}))
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "structuredContent": value}
                except Rejected as exc:
                    result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
            elif method == "events/list":
                result = self.protocol.event_catalog(actor)
            elif method == "events/subscribe":
                result = yield from self.events.subscribe_steps(actor, params)
            elif method == "events/unsubscribe":
                result = self.events.unsubscribe(actor, params)
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}}
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except Rejected as exc:
            code = -32015 if method == "events/subscribe" and "verification failed" in str(exc) else -32602
            error = {"code": code, "message": str(exc)}
            if code == -32015:
                error["data"] = {"reason": "challenge_failed"}
            return {"jsonrpc": "2.0", "id": mid, "error": error}

    def callback(self, mid, token, args):
        with self.store.lock:
            row = self.store._message(mid)
            if not row["callback_hash"] or not hmac.compare_digest(digest(token), row["callback_hash"]):
                raise Rejected("Unauthorized callback")
            actor = row["recipient"]
        action = args.get("action")
        if action == "claim":
            return self.store.claim(actor, mid)
        if action == "acknowledge":
            return self.store.ack(actor, mid)
        if action == "status":
            return self.store.get(actor, mid)
        if action == "reply":
            return self.protocol.call(actor, "reply", {"message_id": mid, "message": args.get("message"), "reply_id": args.get("reply_id"), "expects": args.get("expects", "none")})
        raise Rejected("Allowed callback actions: claim, acknowledge, reply, status")
