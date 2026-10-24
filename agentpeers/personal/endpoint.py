"""Shared bounded HTTP routing for self-hosted and cloud deployments."""
import html
import hmac
import json
import re
from urllib.parse import parse_qs, urlsplit
from .config import Rejected


def form(raw):
    fields = parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=30)
    if any(len(values) != 1 for values in fields.values()):
        raise Rejected("Repeated form fields are forbidden")
    return {key: values[0] for key, values in fields.items()}



class Endpoint:
    def __init__(self, app, method, path, headers, raw=b"", source="local"):
        self.app, self.method, self.path = app, method, path
        self.headers, self.raw, self.source = headers, raw, source

    def output(self, status, value=None, headers=None, content_type="application/json"):
        return status, value, headers or {}, content_type

    def steps(self):
        if self.method == "GET":
            return self.get()
        if self.method == "POST":
            return (yield from self.post_steps())
        return self.output(405, {"error": "Use GET or POST"})

    def auth(self, resource=None):
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise Rejected("Unauthorized")
        return self.app.oauth.actor(authorization[7:], resource)

    def read_body(self):
        if self.headers.get("Transfer-Encoding"):
            raise Rejected("Transfer-Encoding is not supported")
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            raise Rejected("Content-Length required") from None
        if not 0 <= length <= 262144:
            raise Rejected("Request body must be at most 256 KiB")
        raw = self.raw() if callable(self.raw) else self.raw
        if len(raw) != length:
            raise Rejected("Incomplete request body")
        if self.headers.get("Content-Type", "").split(";")[0] == "application/x-www-form-urlencoded":
            return form(raw)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise Rejected("Request must be an object")
        return value

    def get(self):
        try:
            parsed = urlsplit(self.path)
            path = parsed.path
            base = self.app.store.config["public_url"]
            if path == "/context/admin":
                body = '<!doctype html><meta charset="utf-8"><title>AgentPeers context trial</title><h1>One-chat context trial</h1><p>Owner control only. Check status first for active Dot receiver IDs and expiry. Verify the intended Dot listener, then use its exact subscription ID when enabling. Status also displays correlation candidates; bind the exact intended pair. Disabling preserves evidence and revokes only requester grants.</p><form method="post" action="/context/admin"><label>Your AgentPeers password <input name="owner_key" type="password" required autocomplete="off"></label><label>Action <input name="action" value="status"></label><label>Existing Dot subscription ID <input name="subscription_id"></label><label>Candidate subject <input name="subject"></label><label>Candidate session <input name="session"></label><button>Apply owner action</button></form>'
                return self.output(200, body, {"Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'", "Referrer-Policy": "strict-origin"}, "text/html; charset=utf-8")
            if path == "/health":
                return self.output(503 if self.app.worker_warning else 200, {"service": "AgentPeers", "delivery_verified": False, "worker_warning": self.app.worker_warning})
            if path == "/.well-known/oauth-authorization-server":
                return self.output(200, self.app.oauth.metadata())
            prefix = "/.well-known/oauth-protected-resource/mcp/"
            if path.startswith(prefix):
                resource = base + "/mcp/" + path[len(prefix):]
                self.app.oauth.resource_peer(resource)
                return self.output(200, {"resource": resource, "authorization_servers": [base], "scopes_supported": [self.app.oauth.scope(resource)]})
            if path == "/oauth/authorize":
                self.app.limit(self.source, path)
                args = form(parsed.query.encode())
                self.app.oauth.validate_authorize(args)
                peer = self.app.oauth.resource_peer(args["resource"])
                if peer == "context-requester-1":
                    fields = {**args, "csrf": self.app.oauth.csrf(args)}
                    hidden = "".join('<input type="hidden" name="%s" value="%s">' % (html.escape(k, quote=True), html.escape(v, quote=True)) for k, v in fields.items())
                    body = '<!doctype html><meta charset="utf-8"><title>Approve context requester</title><h1>Approve one context requester</h1><p>Only the synthetic card question to Dot and explicit result retrieval. This connection cannot access Dot\'s general inbox, send general messages, subscribe or administer the service. Owner must separately bind the originating conversation.</p><form method="post" action="/oauth/authorize">%s<input name="owner_key" type="password" required autocomplete="off"><button>Allow synthetic context trial</button></form>' % hidden
                    return self.output(200, body, {"Referrer-Policy": "strict-origin", "Content-Security-Policy": "default-src 'none'; form-action 'self' https://%s; frame-ancestors 'none'; base-uri 'none'" % urlsplit(args["redirect_uri"]).hostname}, "text/html; charset=utf-8")
                # Chromium applies form-action to the 303 return as well as the
                # POST. Permit only this validated client's owner-approved host;
                # the password POST itself still goes to our same-origin route.
                return_origin = "https://" + urlsplit(args["redirect_uri"]).hostname
                policy = "default-src 'none'; form-action 'self' %s; frame-ancestors 'none'; base-uri 'none'" % return_origin
                fields = {**args, "csrf": self.app.oauth.csrf(args)}
                hidden = "".join('<input type="hidden" name="%s" value="%s">' % (html.escape(k, quote=True), html.escape(v, quote=True)) for k, v in fields.items())
                body = '<!doctype html><meta charset="utf-8"><title>Connect AgentPeers</title><h1>Connect %s to AgentPeers</h1><p>This connection can read and send %s’s messages to the peers you connected. Enter your AgentPeers password to approve it. The password stays with AgentPeers; the client receives a separate messaging credential. This connection cannot manage peers or run commands.</p><form method="post" action="/oauth/authorize">%s<label>Your AgentPeers password <input name="owner_key" type="password" autocomplete="off" required></label><button type="submit">Allow messaging</button></form>' % (html.escape(self.app.store.config["peers"][peer]["name"]), html.escape(self.app.store.config["peers"][peer]["name"]), hidden)
                return self.output(200, body, {"Referrer-Policy": "strict-origin", "Content-Security-Policy": policy}, "text/html; charset=utf-8")
            return self.output(404, {"error": "Not found"})
        except Rejected as exc:
            return self.output(400, {"error": str(exc)})
        except Exception:
            return self.output(400, {"error": "Invalid request"})

    def post_steps(self):
        path = urlsplit(self.path).path
        resource = None
        try:
            if path.startswith("/oauth/"):
                self.app.limit(self.source, path)
                args = self.read_body()
                if path == "/oauth/register":
                    return self.output(201, self.app.oauth.register(args))
                if path == "/oauth/token":
                    return self.output(200, self.app.oauth.token(args))
                if path == "/oauth/revoke":
                    return self.output(200, self.app.oauth.revoke(args))
                if path == "/oauth/authorize":
                    if self.headers.get("Origin") != self.app.store.config["public_url"]:
                        raise Rejected("Owner authorization requires the same HTTPS origin")
                    destination = self.app.oauth.authorize(args)
                    return self.output(303, headers={"Location": destination})
            if path == "/context/admin":
                self.app.limit(self.source, path)
                if self.headers.get("Origin") != self.app.store.config["public_url"]:
                    raise Rejected("Owner control requires the same HTTPS origin")
                args = self.read_body()
                if not isinstance(args.get("owner_key"), str) or not hmac.compare_digest(args["owner_key"], self.app.store.config["owner_key"]):
                    raise Rejected("Owner approval failed")
                return self.output(200, self.app.context.admin(args))
            if path.startswith("/mcp/"):
                resource = self.app.store.config["public_url"] + path
                actor = self.auth(resource)
                request = self.read_body()
                result = yield from self.app.rpc_steps(actor, request)
                return self.output(200 if result is not None else 202, result)
            if path == "/v1/call":
                actor = self.auth()
                args = self.read_body()
                return self.output(200, self.app.protocol.call(actor, args.get("tool"), args.get("arguments", {})))
            match = re.fullmatch(r"/callbacks/([A-Za-z0-9._:-]{1,100})/([A-Za-z0-9_-]{40,100})", path)
            if match:
                return self.output(200, self.app.callback(*match.groups(), self.read_body()))
            return self.output(404, {"error": "Not found"})
        except Rejected as exc:
            unauthorized = str(exc) in ("Unauthorized", "Unauthorized callback")
            headers = {}
            if unauthorized and resource:
                metadata = self.app.store.config["public_url"] + "/.well-known/oauth-protected-resource" + path
                scope = "agentpeers:context" if path == "/mcp/context-requester" else "agentpeers:messages"
                headers["WWW-Authenticate"] = 'Bearer resource_metadata="%s", scope="%s"' % (metadata, scope)
            return self.output(401 if unauthorized else 400, {"error": str(exc)}, headers)
        except Exception:
            return self.output(400, {"error": "Invalid request"})
