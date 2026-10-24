"""Small single-owner OAuth service. Grants bind a client to one registered personal peer."""
import base64
import hashlib
import hmac
import json
import re
import secrets
from urllib.parse import urlencode, urlsplit

from .config import Rejected, digest


class OAuth:
    def __init__(self, store):
        self.store = store

    def resource_peer(self, resource):
        from .context_bridge import ORIGIN
        context = getattr(self.store, "context", None)
        if context and resource == context.resource and "context-requester" not in self.store.config["peers"]:
            return ORIGIN
        base = self.store.config["public_url"] + "/mcp/"
        if not isinstance(resource, str) or not resource.startswith(base):
            raise Rejected("Unknown OAuth resource")
        peer = resource[len(base):]
        if self.store.config["peers"].get(peer, {}).get("platform") != "dot":
            raise Rejected("OAuth resource must select a configured Dot peer")
        return peer

    def scope(self, resource):
        from .context_bridge import SCOPE
        self.resource_peer(resource)
        context = getattr(self.store, "context", None)
        return SCOPE if context and resource == context.resource and "context-requester" not in self.store.config["peers"] else "agentpeers:messages"

    def metadata(self):
        base = self.store.config["public_url"]
        return {"issuer": base, "authorization_endpoint": base + "/oauth/authorize", "token_endpoint": base + "/oauth/token",
                "registration_endpoint": base + "/oauth/register", "revocation_endpoint": base + "/oauth/revoke",
                "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": ["none"], "code_challenge_methods_supported": ["S256"],
                "scopes_supported": ["agentpeers:messages"] + (["agentpeers:context"] if getattr(self.store, "context", None) and self.store.context.active() else []), "authorization_response_iss_parameter_supported": True}

    def register(self, args):
        redirects = args.get("redirect_uris", [])
        if not isinstance(redirects, list) or not 1 <= len(redirects) <= 5 or any(not isinstance(v, str) or len(v) > 2000 for v in redirects):
            raise Rejected("Register 1–5 official HTTPS redirect URIs")
        for url in redirects:
            u = urlsplit(url)
            if u.scheme != "https" or u.hostname not in self.store.config["oauth_redirect_hosts"] or u.username or u.password or u.fragment or u.port not in (None, 443):
                raise Rejected("Redirect hostname is not owner-approved")
        if args.get("token_endpoint_auth_method", "none") != "none":
            raise Rejected("Use a public OAuth client with S256 PKCE")
        client = "client_" + secrets.token_urlsafe(24)
        with self.store.lock:
            if self.store.db.execute("SELECT count(*) FROM oauth_clients").fetchone()[0] >= 128:
                raise Rejected("Client registration limit reached; owner maintenance required")
            self.store.db.execute("INSERT INTO oauth_clients VALUES(?,?)", (client, json.dumps(redirects)))
        return {"client_id": client, "redirect_uris": redirects, "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}

    def validate_authorize(self, args):
        with self.store.lock:
            client = self.store.db.execute("SELECT * FROM oauth_clients WHERE id=?", (args.get("client_id"),)).fetchone()
        if not client or args.get("redirect_uri") not in json.loads(client["redirects"]):
            raise Rejected("Unknown OAuth client or exact redirect URI")
        self.resource_peer(args.get("resource"))
        if args.get("response_type") != "code" or args.get("code_challenge_method") != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", args.get("code_challenge", "")):
            raise Rejected("Authorization code with S256 PKCE is required")
        if args.get("scope", "agentpeers:messages") != self.scope(args["resource"]):
            raise Rejected("Scope must match the exact protected resource")
        if self.scope(args["resource"]) == "agentpeers:context" and not self.store.context.active():
            raise Rejected("Context trial is disabled")
        if len(args.get("state", "")) > 2000:
            raise Rejected("State too large")

    def csrf(self, args, issued=None):
        issued = int(self.store.clock()) if issued is None else issued
        fields = {k: v for k, v in args.items() if k not in ("owner_key", "csrf")}
        signed = (str(issued) + "." + json.dumps(fields, sort_keys=True, separators=(",", ":"))).encode()
        mac = hmac.new(self.store.config["owner_key"].encode(), signed, hashlib.sha256).hexdigest()
        return str(issued) + "." + mac

    def authorize(self, args):
        self.validate_authorize(args)
        try:
            issued = int(args.get("csrf", "").split(".")[0])
            valid = 0 <= self.store.clock() - issued <= 600 and hmac.compare_digest(args["csrf"], self.csrf(args, issued))
        except (ValueError, KeyError):
            valid = False
        if not valid or not hmac.compare_digest(args.get("owner_key", ""), self.store.config["owner_key"]):
            raise Rejected("Owner approval failed")
        code = secrets.token_urlsafe(32)
        with self.store.lock:
            self.store.db.execute("DELETE FROM oauth_codes WHERE expires<=?", (self.store.clock(),))
            def issue():
                if self.scope(args["resource"]) == "agentpeers:context":
                    self.store.context.authorize_client(args["client_id"])
                self.store.db.execute("INSERT INTO oauth_codes VALUES(?,?,?,?,?,?)", (digest(code), args["client_id"], args["redirect_uri"], args["code_challenge"], args["resource"], self.store.clock() + 120))
            self.store.transaction(issue)
        return args["redirect_uri"] + ("&" if "?" in args["redirect_uri"] else "?") + urlencode({"code": code, "state": args.get("state", ""), "iss": self.store.config["public_url"]})

    def token(self, args):
        client, resource = args.get("client_id"), args.get("resource")
        self.resource_peer(resource)
        now = self.store.clock()
        with self.store.lock:
            self.store.db.execute("DELETE FROM oauth_tokens WHERE expires<=?", (now,))
            if args.get("grant_type") == "authorization_code":
                row = self.store.db.execute("SELECT * FROM oauth_codes WHERE hash=?", (digest(args.get("code", "")),)).fetchone()
                verifier = args.get("code_verifier", "")
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                if not row or row["client"] != client or row["resource"] != resource or row["redirect"] != args.get("redirect_uri") or row["expires"] <= now or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier) or not hmac.compare_digest(row["challenge"], challenge):
                    raise Rejected("invalid_grant")
                table, token_hash = "oauth_codes", row["hash"]
            elif args.get("grant_type") == "refresh_token":
                row = self.store.db.execute("SELECT * FROM oauth_tokens WHERE hash=? AND kind='refresh'", (digest(args.get("refresh_token", "")),)).fetchone()
                if not row or row["client"] != client or row["resource"] != resource or row["expires"] <= now:
                    raise Rejected("invalid_grant")
                table, token_hash = "oauth_tokens", row["hash"]
            else:
                raise Rejected("unsupported_grant_type")
            access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            def rotate():
                self.store.db.execute("DELETE FROM " + table + " WHERE hash=?", (token_hash,))
                self.store.db.execute("INSERT INTO oauth_tokens VALUES(?,?,?,?,?)", (digest(access), client, resource, "access", now + 3600))
                self.store.db.execute("INSERT INTO oauth_tokens VALUES(?,?,?,?,?)", (digest(refresh), client, resource, "refresh", now + 30 * 86400))
            self.store.transaction(rotate)
        return {"access_token": access, "token_type": "Bearer", "expires_in": 3600, "refresh_token": refresh, "scope": self.scope(resource)}

    def actor(self, token, resource=None):
        # Direct bearer keys are distinct per peer. MCP OAuth tokens cannot call another peer's endpoint.
        if not resource:
            for peer_id, peer in self.store.config["peers"].items():
                if hmac.compare_digest(token, peer["key"]):
                    return peer_id
            raise Rejected("Unauthorized")
        peer_id = self.resource_peer(resource)
        with self.store.lock:
            row = self.store.db.execute("SELECT * FROM oauth_tokens WHERE hash=? AND kind='access' AND expires>?", (digest(token), self.store.clock())).fetchone()
        if not row or row["resource"] != resource:
            raise Rejected("Unauthorized")
        if self.scope(resource) == "agentpeers:context" and not self.store.context.token_client_allowed(row["client"]):
            raise Rejected("Unauthorized")
        return peer_id

    def revoke(self, args):
        with self.store.lock:
            row = self.store.db.execute("SELECT client FROM oauth_tokens WHERE hash=?", (digest(args.get("token", "")),)).fetchone()
            if row and row[0] == args.get("client_id"):
                self.store.db.execute("DELETE FROM oauth_tokens WHERE client=?", (row[0],))
        return {}
