"""Small owner-only setup for factual topics and per-chat access."""
from datetime import datetime, timezone
from html import escape
import hmac
import hashlib
from http.cookies import SimpleCookie
import json
import secrets

from .config import Rejected
from .factual import context_reference


COOKIE = "ap_context_owner"
POLICY = "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; style-src 'unsafe-inline'"


def when(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class ContextUI:
    def __init__(self, factual):
        self.factual, self.store = factual, factual.store

    def token(self, headers):
        try:
            cookie = SimpleCookie(headers.get("Cookie", ""))
            return cookie[COOKIE].value if COOKIE in cookie else ""
        except Exception:
            return ""

    def authenticated(self, headers):
        token = self.token(headers)
        with self.store.lock:
            return bool(token and self.store.db.execute("SELECT 1 FROM ap_facts_owner_sessions WHERE hash=? AND expires>?", (self.session_hash(token), self.store.clock())).fetchone())

    def session_hash(self, token):
        return hmac.new(self.store.config["owner_key"].encode(), token.encode(), hashlib.sha256).hexdigest()

    def login(self, password):
        if not isinstance(password, str) or not hmac.compare_digest(password, self.store.config["owner_key"]):
            raise Rejected("AgentPeers password did not match")
        token = secrets.token_urlsafe(32)
        with self.store.lock:
            self.store.db.execute("DELETE FROM ap_facts_owner_sessions WHERE expires<=?", (self.store.clock(),))
            if self.store.db.execute("SELECT count(*) FROM ap_facts_owner_sessions").fetchone()[0] >= 32:
                raise Rejected("Too many owner sign-ins; wait for the existing 15-minute sessions to expire")
            self.store.db.execute("INSERT INTO ap_facts_owner_sessions VALUES(?,?)", (self.session_hash(token), self.store.clock() + 900))
        return f"{COOKIE}={token}; Max-Age=900; Path=/; Secure; HttpOnly; SameSite=Strict"

    def logout(self, headers):
        with self.store.lock:
            self.store.db.execute("DELETE FROM ap_facts_owner_sessions WHERE hash=?", (self.session_hash(self.token(headers)),))
        return f"{COOKIE}=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Strict"

    def pending(self, state):
        if not state["candidates"]:
            return ''
        content = '<h2 id="pending">Review chat access</h2><p>Match the pairing code with the intended ChatGPT chat. Review the facts it may request before allowing access.</p>'
        for c in state["candidates"][:64]:
            policy = next((p for p in state["policies"] if p["topic"] == c["topic"] and p["revision"] == c["revision"] and p["active"]), None)
            content += '<section><h3>Pairing code <code>' + escape(c["id"][:12].upper()) + '</code></h3><p>Topic: ' + escape(c["topic"]) + '</p><blockquote>' + escape(c["question"]) + '</blockquote>'
            if policy:
                refs = ['Dot’s existing context for this topic' if r == context_reference(policy['topic']) else r for r in json.loads(policy['refs'])]
                content += '<p><strong>Allowed facts:</strong> ' + escape(policy['scope']) + '</p><p><strong>Sources:</strong> ' + escape('; '.join(refs)) + '</p><p>Pairing expires ' + when(c["expires"]) + '.</p><form method="post" action="/context/actions"><input type="hidden" name="action" value="allow_chat"><input type="hidden" name="candidate" value="' + escape(c["id"], quote=True) + '"><label>Allow this chat for how many days?<input name="days" type="number" min="1" max="30" value="30" required></label><button>Allow this chat</button></form>'
            else:
                content += '<p>The topic changed. Prepare a new request in the intended chat before approving it.</p>'
            content += '</section>'
        return content

    def page(self, headers, error="", message=""):
        content = '<h1>AgentPeers context access</h1><p>Let an ordinary ChatGPT chat ask Dot factual questions on topics you approve. This does not grant access to Dot’s inbox or permission for business actions.</p>'
        if error:
            content += '<p role="alert">' + escape(error) + '</p>'
        if message and self.authenticated(headers):
            content += '<p role="status">' + escape(message) + '</p>'
        if not self.authenticated(headers):
            content += '<form method="post" action="/context/login"><label>Your AgentPeers password<input name="owner_key" type="password" required autocomplete="current-password"></label><button>Open context settings</button></form><p>Use the existing password for this service. It stays here and is not given to ChatGPT or Dot. Owner sign-in lasts 15 minutes.</p>'
        else:
            with self.store.lock:
                state = self.factual.status()
            content += '<form method="post" action="/context/logout"><button>Sign out</button></form>'
            content += '<nav><a href="#topics">Allowed topics</a> · <a href="#allowed-chats">Allowed chats</a></nav>'
            content += self.pending(state)
            content += '<h2 id="topics">1. Choose what Dot may share</h2><p>Use your existing Dot receiver. Dot also needs your permission in its own chat to answer from these sources. Changing a topic revokes earlier chat access for that topic.</p>'
            if not state["receivers"]:
                content += '<p>No active Dot receiver was found. Restore the existing Dot receiving connection first; nothing will be sent to another chat.</p>'
            else:
                if state['policies']:
                    content += '<details><summary>Add or change an allowed topic</summary>'
                options = ''.join('<option value="%s">Dot — %s (expires %s)</option>' % (escape(r["id"], quote=True), escape(r["host"] or "unknown host"), when(r["expires"])) for r in state["receivers"])
                content += '<form method="post" action="/context/actions"><input type="hidden" name="action" value="save_policy"><label>Topic name<input name="topic" placeholder="My project" required maxlength="100"></label><label>Facts Dot may share<textarea name="scope" required maxlength="2000" placeholder="Project status and technical decisions only. Exclude credentials and unrelated private facts."></textarea></label><label><input type="checkbox" name="dot_context" value="yes"> Allow Dot to use its existing context for this topic</label><label>Other approved links or record references, one per line (optional)<textarea name="refs" placeholder="https://example.com/my-project/"></textarea></label><p>Select Dot’s context, provide an approved source, or both. Use up to five sources. A link or reference also permits its child paths; wildcards are not accepted. These are Dot’s source reports, not independent fact checks by AgentPeers.</p><label>Existing Dot receiver<select name="subscription">' + options + '</select></label><button>Save allowed topic</button></form>'
                if state['policies']:
                    content += '</details>'
            for p in state["policies"]:
                content += '<section><h3>' + escape(p["topic"]) + (' — enabled' if p["active"] else ' — disabled') + '</h3><p>' + escape(p["scope"]) + '</p><ul>' + ''.join('<li><code>' + escape("Dot’s existing context for this topic" if ref == context_reference(p["topic"]) else ref) + '</code></li>' for ref in json.loads(p["refs"])) + '</ul>'
                if p["active"]:
                    content += self.action('disable_topic', 'Stop sharing this topic', topic=p["topic"])
                content += '</section>'
            address = self.store.config["public_url"] + '/mcp/context'
            content += '<h2>2. Connect ChatGPT</h2><p>Add a custom MCP app named <strong>AgentPeers Context</strong>, using OAuth at:</p><p><code>' + escape(address) + '</code></p><p>Keep the normal AgentPeers app for Dot and Grokbot. In the ordinary chat, select AgentPeers Context, discover allowed topics and ask Dot your factual question. It will show a pairing code before sending anything.</p>'
            content += '<h2>3. Allow the intended chat</h2><p>Only approve a pairing code that you just received in the ChatGPT chat you intend to connect. Access covers that chat and topic for the selected duration. A question alone cannot authorize actions.</p>'
            content += '<p>New pairing requests appear at the top of this page. After approval, return to the intended chat and tell it to continue its existing request.</p>'
            content += '<h2 id="allowed-chats">Allowed chats</h2>'
            for g in state["grants"]:
                content += '<section><p>Code <code>' + escape(g["id"][6:18].upper()) + '</code> · ' + escape(g["topic"]) + ' · ' + ('active until ' if g["active"] and g["expires"] > self.store.clock() else 'inactive; recorded expiry ') + when(g["expires"]) + '</p>'
                if g["active"]:
                    content += self.action('revoke_chat', 'Revoke this chat', grant=g["id"])
                content += '</section>'
            content += '<h2>Disconnect context access</h2><p>This revokes context-requester access only. It preserves normal Dot–Grokbot connections, messages and historical records. It cannot interrupt an agent already running.</p>' + self.action('disconnect', 'Disconnect context access')
        return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>AgentPeers context access</title><style>body{font:17px/1.5 system-ui;max-width:760px;margin:40px auto;padding:0 20px;color:#172333}h1{line-height:1.15}h2{margin-top:2em}label{display:block;margin:14px 0}input,textarea,select,button{font:inherit;padding:10px;box-sizing:border-box}input:not([type=hidden]):not([type=checkbox]),textarea,select{display:block;width:100%}textarea{min-height:90px}button{cursor:pointer;margin:8px 0}section{border:1px solid #ccd5df;padding:16px;margin:16px 0;border-radius:8px}code{overflow-wrap:anywhere}blockquote{margin-left:0;padding-left:16px;border-left:3px solid #ccd5df}[role=alert]{padding:12px;background:#fff0ec}</style><main>' + content + '</main></html>'

    @staticmethod
    def action(action, label, **fields):
        hidden = ''.join('<input type="hidden" name="%s" value="%s">' % (escape(k, quote=True), escape(v, quote=True)) for k, v in {'action':action, **fields}.items())
        return '<form method="post" action="/context/actions">' + hidden + '<button>' + escape(label) + '</button></form>'
