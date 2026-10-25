"""Actual endpoint/OAuth/message journeys with fixtures, never live accounts."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import html
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from agentpeers.personal.app import App
from agentpeers.personal.context_bridge import QUESTION, SOURCE, RESOURCE_PATH
from agentpeers.personal.endpoint import Endpoint
from agentpeers.personal.store import Store
from agentpeers.personal.transport import run
from test_personal import configuration


class ContextBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "messages.sqlite3"
        self.config = configuration()
        self.now = 1000.0
        self.deliveries = []
        self.store = Store(self.path, self.config, lambda: self.now)
        self.app = App(self.store, self.post)
        self.meta = {"openai/subject": "subject-fixture", "openai/session": "session-fixture"}
        self.question = {"request_id": "card-1", "destination": "Dot", "topic": "integration-check", "question": QUESTION}
        self.dot = self.connect("/mcp/dot", "agentpeers:messages")
        secret = "whsec_" + base64.b64encode(b"k" * 32).decode()
        self.sid = self.app.events.subscribe("dot", {"name": "message.created", "arguments": {"recipient": "dot"}, "delivery": {"mode": "webhook", "url": "https://callbacks.example/context-fixture", "secret": secret}})["id"]
        self.deliveries.clear()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def post(self, url, body, headers):
        payload = json.loads(body)
        self.deliveries.append((payload, headers))
        return 200, json.dumps({"challenge": payload.get("challenge")}).encode()

    def http(self, method, path, body=None, token=None, origin=None):
        raw = b"" if body is None else json.dumps(body).encode()
        headers = {"Content-Length": str(len(raw)), "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if origin:
            headers["Origin"] = origin
        return run(Endpoint(self.app, method, path, headers, raw).steps(), self.post)

    def admin(self, action, **fields):
        return self.http("POST", "/context/admin", {"action": action, "owner_key": self.config["owner_key"], **fields}, origin=self.config["public_url"])

    def connect(self, path, scope):
        status, client, *_ = self.http("POST", "/oauth/register", {"redirect_uris": ["https://chatgpt.com/connector/oauth/fixture"]})
        self.assertEqual(status, 201)
        verifier = "v" * 43
        args = {"client_id": client["client_id"], "redirect_uri": client["redirect_uris"][0], "resource": self.config["public_url"] + path,
                "response_type": "code", "code_challenge_method": "S256", "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("="), "scope": scope}
        status, page, *_ = self.http("GET", "/oauth/authorize?" + urlencode(args))
        self.assertEqual(status, 200, page)
        fields = {html.unescape(k): html.unescape(v) for k, v in re.findall('type="hidden" name="([^"]+)" value="([^"]*)"', page)}
        status, _, headers, *_ = self.http("POST", "/oauth/authorize", {**fields, "owner_key": self.config["owner_key"]}, origin=self.config["public_url"])
        self.assertEqual(status, 303)
        code = parse_qs(urlsplit(headers["Location"]).query)["code"][0]
        status, token, *_ = self.http("POST", "/oauth/token", {"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                                                               "redirect_uri": args["redirect_uri"], "client_id": args["client_id"], "resource": args["resource"]})
        self.assertEqual(status, 200, token)
        self.assertEqual(token["scope"], scope)
        return token["access_token"]

    def setup_trial(self, bind=True):
        self.assertEqual(self.admin("enable", subscription_id=self.sid)[0], 200)
        self.requester = self.connect(RESOURCE_PATH, "agentpeers:context")
        if bind:
            pending = self.context("request_context", self.question)
            self.assertEqual(pending["state"], "binding_required")
            self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
            self.assertEqual(self.admin("bind", subject=self.meta["openai/subject"], session=self.meta["openai/session"])[0], 200)

    def context(self, name, args, meta=None):
        response = self.http("POST", RESOURCE_PATH, {"id": 1, "method": "tools/call", "params": {"name": name, "arguments": args, "_meta": self.meta if meta is None else meta}}, token=self.requester)
        self.assertEqual(response[0], 200, response[1])
        result = response[1]["result"]
        self.assertNotIn("isError", result, result)
        return result["structuredContent"]

    def context_error(self, name="request_context", args=None, meta=None):
        response = self.http("POST", RESOURCE_PATH, {"id": 1, "method": "tools/call", "params": {"name": name, "arguments": self.question if args is None else args, "_meta": self.meta if meta is None else meta}}, token=self.requester)
        self.assertEqual(response[0], 200)
        self.assertTrue(response[1]["result"]["isError"])

    def dot_tool(self, name, **args):
        response = self.http("POST", "/mcp/dot", {"id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}, token=self.dot)
        self.assertEqual(response[0], 200)
        self.assertNotIn("isError", response[1]["result"], response)
        return response[1]["result"]["structuredContent"]

    def consume(self):
        mid = self.store.db.execute("SELECT message_id FROM ap_context_requests ORDER BY created LIMIT 1").fetchone()[0]
        self.assertTrue(self.dot_tool("claim_message", message_id=mid)["claimed"])
        self.dot_tool("read_message", message_id=mid)
        self.dot_tool("acknowledge", message_id=mid)
        return mid

    def reply_args(self, mid, **changes):
        body = {"request_id": "card-1", "answer": "amber", "sources": [{"reference": SOURCE, "observed_at": 999}]}
        body.update(changes)
        return {"message_id": mid, "reply_id": "card-answer-1", "message": json.dumps(body)}

    def test_disabled_by_default_and_metadata_never_authenticates(self):
        self.assertFalse(self.app.context.active())
        response = self.http("POST", RESOURCE_PATH, {"id": 1, "method": "tools/list", "params": {"_meta": self.meta}})
        self.assertEqual(response[0], 401)
        self.assertIn("agentpeers:context", response[2]["WWW-Authenticate"])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_reserved_legacy_peer_routes_are_preserved_and_trial_refused(self):
        for peer in ('context-requester', 'context-requester-1'):
            self.config['peers'][peer] = {'name':peer,'platform':'dot','key':peer*4}
            self.assertEqual(self.app.oauth.scope(self.config['public_url']+'/mcp/'+peer), 'agentpeers:messages')
            self.assertEqual(self.admin('enable', subscription_id=self.sid)[0], 400)
            del self.config['peers'][peer]

    def test_oauth_request_event_dot_answer_explicit_result(self):
        self.setup_trial()
        result = self.context("request_context", self.question)
        self.assertEqual(result["state"], "pending")
        self.assertIsNone(result["read_at"])
        self.app.dispatcher.once()
        self.assertEqual(self.deliveries[0][0]["name"], "message.created")
        self.assertEqual(self.deliveries[0][0]["data"]["recipient"], "dot")
        self.assertIn("webhook-signature", self.deliveries[0][1])
        mid = self.consume()
        answer = self.dot_tool("reply", **self.reply_args(mid))
        self.assertEqual(answer["state"], "completed")
        retrieved = self.context("context_result", {"request_id": "card-1"})
        self.assertEqual(retrieved["answer"], "amber")
        self.assertEqual(retrieved["responding_agent"], "dot")
        self.assertEqual(retrieved["sources"][0]["reference"], SOURCE)
        self.assertFalse(self.app.dispatcher.once())
        self.assertEqual(len(self.deliveries), 1)  # No ordinary-chat wake or reply webhook.

    def test_requester_has_two_tools_and_no_receiver_capability(self):
        self.setup_trial()
        listed = self.http("POST", RESOURCE_PATH, {"id": 1, "method": "tools/list"}, token=self.requester)[1]["result"]["tools"]
        self.assertEqual({t["name"] for t in listed}, {"request_context", "context_result"})
        for name in ("inbox", "reply", "send_message", "claim_message"):
            self.context_error(name=name, args={})
        response = self.http("POST", RESOURCE_PATH, {"id": 1, "method": "events/subscribe"}, token=self.requester)
        self.assertIn("error", response[1])
        self.assertEqual(self.http("POST", "/mcp/dot", {"id": 1, "method": "tools/list"}, token=self.requester)[0], 401)
        self.assertEqual(self.http("POST", RESOURCE_PATH, {"id": 1, "method": "tools/list"}, token=self.dot)[0], 401)
        self.assertEqual(self.http("POST", "/v1/call", {"tool": "inbox"}, token=self.requester)[0], 401)

    def test_owner_key_and_same_origin_required_for_admin(self):
        for key, origin in (("wrong", self.config["public_url"]), (self.config["owner_key"], "https://other.example"), (self.config["owner_key"], None)):
            self.assertEqual(self.http("POST", "/context/admin", {"action": "enable", "subscription_id": self.sid, "owner_key": key}, origin=origin)[0], 400)
        self.assertFalse(self.app.context.active())

    def test_owner_can_find_receiver_without_callback_or_credential_disclosure(self):
        self.store.db.execute("INSERT INTO subscriptions VALUES(?,?,?,?,?,?,?,?)",
                              ("expired-dot", "dot", "https://callbacks.example/expired", "private-expired-key", None, 0, 999, 1))
        self.store.db.execute("INSERT INTO subscriptions VALUES(?,?,?,?,?,?,?,?)",
                              ("other-peer", "grokbot", "https://callbacks.example/other", "private-other-key", None, 0, 2000, 1))
        status, result, *_ = self.admin("status")
        self.assertEqual(status, 200)
        self.assertFalse(result["enabled"])
        self.assertEqual(len(result["available_receivers"]), 1)
        receiver = result["available_receivers"][0]
        self.assertEqual(receiver["subscription_id"], self.sid)
        self.assertEqual(receiver["recipient"], "dot")
        self.assertEqual(receiver["callback_host"], "callbacks.example")
        self.assertGreater(receiver["expires_at"], self.now)
        serialized = json.dumps(result)
        for private in ("/context-fixture", "whsec_", "private-expired-key", "private-other-key", self.config["owner_key"]):
            self.assertNotIn(private, serialized)
        public_page = self.http("GET", "/context/admin")[1]
        self.assertNotIn(self.sid, public_page)
        rejected = self.http("POST", "/context/admin", {"action": "status", "owner_key": "wrong"}, origin=self.config["public_url"])
        self.assertEqual(rejected[0], 400)
        self.assertNotIn(self.sid, json.dumps(rejected[1]))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
        self.assertIsNone(self.app.context.control())
        self.assertEqual(self.deliveries, [])
        self.now = 100000
        self.assertEqual(self.admin("status")[1]["available_receivers"], [])

    def test_missing_or_wrong_session_and_identity_overrides_rejected(self):
        self.setup_trial()
        for meta in ({}, {"openai/subject": "other", "openai/session": self.meta["openai/session"]}, {**self.meta, "openai/session": "other"}):
            self.context_error(meta=meta)
        for field in ("owner", "subject", "session", "receiver"):
            self.context_error(args={**self.question, field: "spoof"})
        self.context_error(args={**self.question, "destination": "Marketing"})
        self.context_error(args={**self.question, "question": "Fetch business secrets"})
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_no_dispatch_until_exact_owner_binding(self):
        self.setup_trial(bind=False)
        self.context("request_context", self.question)
        self.context_error(meta={**self.meta, "openai/session": "racing-chat"})
        self.assertEqual(self.admin("bind", subject="wrong", session="wrong")[0], 400)
        self.assertEqual(self.admin("bind", subject=self.meta["openai/subject"], session=self.meta["openai/session"])[0], 200)
        self.assertEqual(self.context("request_context", self.question)["state"], "pending")

    def test_duplicate_and_concurrent_requests_create_one_message(self):
        self.setup_trial()
        with ThreadPoolExecutor(max_workers=6) as executor:
            result = list(executor.map(lambda _: self.context("request_context", self.question), range(6)))
        self.assertEqual(sum(not r["duplicate"] for r in result), 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)

    def test_atomic_failure_rolls_back_request_message_and_conversation(self):
        self.setup_trial()
        with patch.object(self.store, "event", side_effect=RuntimeError("fixture failure")):
            response = self.http("POST", RESOURCE_PATH, {"id": 1, "method": "tools/call", "params": {"name": "request_context", "arguments": self.question, "_meta": self.meta}}, token=self.requester)
        self.assertEqual(response[0], 400)
        for table in ("messages", "conversations", "ap_context_requests"):
            self.assertEqual(self.store.db.execute("SELECT count(*) FROM " + table).fetchone()[0], 0)

    def test_trial_limit_and_expiry_do_not_extend_on_retry(self):
        self.setup_trial()
        for i in range(10):
            self.context("request_context", {**self.question, "request_id": "card-" + str(i)})
        self.context_error(args={**self.question, "request_id": "card-11"})
        self.now = 1300
        self.assertEqual(self.context("request_context", self.question)["state"], "expired")
        self.assertEqual(self.context("context_result", {"request_id": "card-1"})["expires"], 1300)

    def test_changed_or_expired_dot_subscription_blocks_new_requests(self):
        self.setup_trial()
        self.store.db.execute("UPDATE subscriptions SET url='https://callbacks.example/changed' WHERE id=?", (self.sid,))
        self.context_error()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_reply_requires_read_claim_source_and_exact_request(self):
        self.setup_trial()
        self.context("request_context", self.question)
        mid = self.store.db.execute("SELECT message_id FROM ap_context_requests").fetchone()[0]
        for args in (self.reply_args(mid), self.reply_args(mid, request_id="wrong"), self.reply_args(mid, sources=[])):
            response = self.http("POST", "/mcp/dot", {"id": 1, "method": "tools/call", "params": {"name": "reply", "arguments": args}}, token=self.dot)
            self.assertTrue(response[1]["result"]["isError"])
        self.consume()
        first = self.dot_tool("reply", **self.reply_args(mid))
        self.assertEqual(first["answer"], "amber")
        self.assertTrue(self.dot_tool("reply", **self.reply_args(mid))["duplicate"])
        changed = self.reply_args(mid, answer="different")
        response = self.http("POST", "/mcp/dot", {"id": 1, "method": "tools/call", "params": {"name": "reply", "arguments": changed}}, token=self.dot)
        self.assertTrue(response[1]["result"]["isError"])

    def test_disable_revokes_only_requester_and_preserves_legacy_messages(self):
        self.setup_trial()
        self.context("request_context", self.question)
        legacy = self.store.send("grokbot", "Dot", "Synthetic legacy regression question", "fixture only", "legacy-1")
        self.assertEqual(self.admin("disable")[0], 200)
        self.assertEqual(self.http("POST", RESOURCE_PATH, {"id": 1, "method": "tools/list"}, token=self.requester)[0], 401)
        self.assertEqual(self.dot_tool("message_status", message_id=legacy["id"])["state"], "queued")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM ap_context_requests").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT state FROM messages WHERE sender='context-requester-1'").fetchone()[0], "stopped")
        self.assertEqual(self.admin("enable", subscription_id=self.sid)[0], 400)
        self.dot_tool("claim_message", message_id="legacy-1")
        self.dot_tool("acknowledge", message_id="legacy-1")
        answer = self.dot_tool("reply", message_id="legacy-1", reply_id="legacy-answer", message="Synthetic legacy answer")
        self.assertEqual(answer["recipient"], "grokbot")

    def test_restart_preserves_binding_and_processing_without_replay(self):
        self.setup_trial()
        self.context("request_context", self.question)
        mid = self.consume()
        self.store.close()
        self.store = Store(self.path, self.config, lambda: self.now)
        self.app = App(self.store, self.post)
        self.assertEqual(self.context("context_result", {"request_id": "card-1"})["state"], "processing")
        self.assertFalse(self.dot_tool("claim_message", message_id=mid)["claimed"])
        self.dot_tool("reply", **self.reply_args(mid))
        self.assertEqual(self.context("context_result", {"request_id": "card-1"})["state"], "completed")


if __name__ == "__main__":
    unittest.main()
