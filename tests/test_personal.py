"""Personal-agent contracts. All transports are injected; no live account or agent calls."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import hmac
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from agentpeers.personal.adapters import Dispatcher, Events, encode, post_https
from agentpeers.personal.auth import OAuth
from agentpeers.personal.cli import initialize
from agentpeers.personal.config import Rejected, load
from agentpeers.personal.server import App, form
from agentpeers.personal.store import Store


def configuration():
    return {"public_url": "https://bridge.example", "owner_key": "owner-" + "x" * 40,
            "callback_hosts": ["callbacks.example"], "oauth_redirect_hosts": ["chatgpt.com"],
            "connections": [["grokbot", "dot"]], "max_messages": 32, "conversation_seconds": 86400,
            "peers": {"grokbot": {"name": "Grokbot", "platform": "grokbot", "key": "g" * 40, "webhook_url": "https://native.example/webhook", "webhook_key": "native-secret"},
                      "dot": {"name": "Dot", "platform": "dot", "key": "d" * 40},
                      "stranger": {"name": "Stranger", "platform": "dot", "key": "s" * 40}}}


class PersonalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = configuration()
        self.now = 1000.0
        self.path = Path(self.directory.name) / "messages.sqlite3"
        self.store = Store(self.path, self.config, lambda: self.now)
        self.calls = []
        self.outcome = 200
        def post(url, body, headers):
            self.calls.append((url, json.loads(body), headers))
            if isinstance(self.outcome, Exception):
                raise self.outcome
            payload = json.loads(body)
            return self.outcome, encode({"challenge": payload.get("challenge")})
        self.app = App(self.store, post)
        self.post = post
        self.secret = "whsec_" + base64.b64encode(b"k" * 32).decode()

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def subscribe(self, **overrides):
        params = {"name": "message.created", "arguments": {"recipient": "dot"}, "delivery": {"mode": "webhook", "url": "https://callbacks.example/receive", "secret": self.secret}}
        params.update(overrides)
        return self.app.events.subscribe("dot", params)

    def send(self, actor="dot", target="Grokbot", mid="question", expects="reply"):
        return self.store.send(actor, target, "A real question?", "Exchange test messages only", mid, expects=expects)

    def consume(self, actor, mid):
        value = self.store.claim(actor, mid)
        self.assertTrue(value["claimed"])
        self.assertIsNone(value["read_at"])
        self.store.ack(actor, mid)
        return value

    def test_bidirectional_followups_use_actual_content_and_exact_reply_route(self):
        for sender, target in [("dot", "Grokbot"), ("grokbot", "Dot")]:
            mid = sender + "-first"
            question = self.send(sender, target, mid)
            receiver = question["recipient"]
            self.consume(receiver, mid)
            answer = self.store.send(receiver, sender, "Answer and follow-up?", message_id=sender + "-second", reply_to=mid)
            self.consume(sender, answer["id"])
            final = self.store.send(sender, receiver, "Final answer", message_id=sender + "-third", reply_to=answer["id"], expects="none")
            self.consume(receiver, final["id"])
            self.assertEqual(final["claimed_scope"], question["claimed_scope"])
            self.assertEqual(self.store.get(sender, mid)["replies"], [answer["id"]])
            with self.assertRaises(Rejected):
                self.store.send(receiver, sender, "Courtesy loop", message_id=sender + "-fourth", reply_to=final["id"])

    def test_saved_or_transport_accepted_does_not_mean_read(self):
        self.send()
        self.assertEqual(self.store.get("dot", "question")["state"], "queued")
        self.app.dispatcher.once()
        row = self.store.get("dot", "question")
        self.assertEqual(row["state"], "accepted")
        self.assertIsNone(row["read_at"])
        self.store.read("grokbot", "question")
        self.assertIsNone(self.store.get("dot", "question")["read_at"])
        self.consume("grokbot", "question")
        read = self.store.get("dot", "question")
        self.assertEqual(read["state"], "read")
        self.assertEqual(read["detail"], "Agent acknowledged reading this message")

    def test_duplicate_send_and_conflicting_id(self):
        self.send()
        self.assertTrue(self.send()["duplicate"])
        with self.assertRaises(Rejected):
            self.store.send("dot", "grokbot", "Different content", "scope", "question")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)

    def test_atomic_claim_suppresses_concurrent_handlers(self):
        self.send()
        with ThreadPoolExecutor(max_workers=8) as executor:
            result = list(executor.map(lambda _: self.store.claim("grokbot", "question"), range(8)))
        self.assertEqual(sum(r["claimed"] for r in result), 1)
        self.assertIsNone(self.store.get("dot", "question")["read_at"])

    def test_unconnected_identity_cannot_discover_read_or_send(self):
        self.send()
        self.assertEqual(self.store.peers("stranger"), [])
        for call in [lambda: self.store.get("stranger", "question"), lambda: self.send("stranger", "Dot", "bad"), lambda: self.store.ack("dot", "question")]:
            with self.assertRaises(Rejected):
                call()

    def test_exact_ids_do_not_override_ambiguous_names(self):
        self.config["peers"]["stranger"]["name"] = "Grokbot"
        self.config["connections"].append(["dot", "stranger"])
        with self.assertRaises(Rejected):
            self.send()

    def test_sender_spoof_and_unknown_arguments_rejected(self):
        with self.assertRaises(Rejected):
            self.app.protocol.call("grokbot", "send_message", {"to": "Dot", "message": "hello", "scope": "test", "message_id": "x", "sender": "dot"})
        self.assertEqual(self.app.oauth.actor("g" * 40), "grokbot")
        with self.assertRaises(Rejected):
            self.app.oauth.actor("owner-" + "x" * 40)

    def test_reply_requires_read_preserves_scope_and_prevents_duplicate_work(self):
        self.send()
        with self.assertRaises(Rejected):
            self.store.send("grokbot", "dot", "answer", reply_to="question")
        self.consume("grokbot", "question")
        answer = self.store.send("grokbot", "dot", "answer", "invented permission", "answer", "question")
        self.assertEqual(answer["claimed_scope"], "Exchange test messages only")
        with self.assertRaises(Rejected):
            self.store.send("grokbot", "dot", "another answer", message_id="different", reply_to="question")

    def test_scope_and_body_limits(self):
        for body, scope in [("", "scope"), ("x" * 32001, "scope"), ("x", ""), ("x", "s" * 2001)]:
            with self.assertRaises(Rejected):
                self.store.send("dot", "grokbot", body, scope)

    def test_stop_and_expiration_preserve_readable_evidence(self):
        self.send()
        self.store.stop("dot", "question")
        self.assertFalse(self.app.dispatcher.once())
        self.assertFalse(self.store.claim("grokbot", "question")["claimed"])
        self.send(mid="expired")
        self.now += 86401
        self.app.dispatcher.once()
        self.assertEqual(self.store.get("dot", "expired")["state"], "expired")

    def test_grok_native_payload_does_not_contain_message_instructions_or_keys(self):
        self.send()
        self.app.dispatcher.once()
        url, payload, headers = self.calls[-1]
        self.assertEqual(url, self.config["peers"]["grokbot"]["webhook_url"])
        self.assertNotIn("A real question?", json.dumps(payload))
        self.assertEqual(headers["Authorization"], "Bearer native-secret")
        self.assertNotIn("native-secret", json.dumps(payload))
        self.assertIn("/callbacks/question/", payload["callback_url"])

    def test_scoped_callback_cannot_send_to_another_peer(self):
        self.send()
        self.app.dispatcher.once()
        token = self.calls[-1][1]["callback_url"].rsplit("/", 1)[1]
        self.assertTrue(self.app.callback("question", token, {"action": "claim"})["claimed"])
        with self.assertRaises(Rejected):
            self.app.callback("question", "wrong", {"action": "claim"})
        with self.assertRaises(Rejected):
            self.app.callback("question", token, {"action": "send", "to": "stranger"})
        self.app.callback("question", token, {"action": "acknowledge"})
        answer = self.app.callback("question", token, {"action": "reply", "message": "answer", "reply_id": "reply"})
        self.assertEqual(answer["recipient"], "dot")

    def test_timeout_to_grok_is_unknown_and_never_replayed(self):
        self.send()
        self.outcome = TimeoutError("https://native.example/secret?credential=do-not-log")
        self.app.dispatcher.once()
        row = self.store.get("dot", "question")
        self.assertEqual(row["state"], "unknown")
        self.assertNotIn("credential", json.dumps(row))
        self.now += 500
        self.assertFalse(self.app.dispatcher.once())
        self.assertEqual(len(self.calls), 1)

    def test_busy_429_is_deferred_then_delivered_without_duplicate_message(self):
        self.send()
        self.outcome = 429
        self.app.dispatcher.once()
        self.assertEqual(self.store.get("dot", "question")["state"], "waiting")
        self.assertFalse(self.app.dispatcher.once())
        self.now += 21
        self.outcome = 200
        self.app.dispatcher.once()
        row = self.store.get("dot", "question")
        self.assertEqual(row["state"], "accepted")
        self.assertEqual(row["attempts"], 2)
        self.assertIsNone(row["read_at"])

    def test_auth_refusal_is_blocked_without_fallback(self):
        self.send()
        self.outcome = 403
        self.app.dispatcher.once()
        self.assertEqual(self.store.get("dot", "question")["state"], "blocked")
        self.now += 300
        self.assertFalse(self.app.dispatcher.once())

    def test_dot_unavailable_keeps_message_for_subscription(self):
        self.send("grokbot", "Dot")
        self.app.dispatcher.once()
        self.assertEqual(len(self.calls), 0)
        self.assertEqual(self.store.get("grokbot", "question")["state"], "waiting")
        self.subscribe()
        self.now += 11
        self.app.dispatcher.once()
        self.assertEqual(self.store.get("grokbot", "question")["state"], "accepted")

    def test_dot_signatures_cover_exact_body_and_stable_event_id_across_retry(self):
        sid = self.subscribe()["id"]
        self.send("grokbot", "Dot")
        self.outcome = 503
        self.app.dispatcher.once()
        _, payload, headers = self.calls[-1]
        signed = ("question." + str(int(self.now)) + ".").encode() + encode(payload)
        expected = base64.b64encode(hmac.new(b"k" * 32, signed, hashlib.sha256).digest()).decode()
        self.assertEqual(headers["webhook-signature"], "v1," + expected)
        self.assertEqual(headers["X-MCP-Subscription-Id"], sid)
        self.assertEqual(payload["eventId"], "question")
        self.now += 21
        self.outcome = 200
        self.app.dispatcher.once()
        self.assertEqual(self.calls[-1][1]["eventId"], "question")

    def test_subscription_idempotent_refresh_and_unsubscribe(self):
        first = self.subscribe()
        self.now += 1
        second = self.subscribe()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM subscriptions").fetchone()[0], 1)
        self.app.events.unsubscribe("stranger", {"delivery": {"url": "https://callbacks.example/receive"}})
        self.assertEqual(self.store.db.execute("SELECT active FROM subscriptions").fetchone()[0], 1)
        self.app.events.unsubscribe("dot", {"delivery": {"url": "https://callbacks.example/receive"}})
        self.assertEqual(self.store.db.execute("SELECT active FROM subscriptions").fetchone()[0], 0)

    def test_callback_verification_failure_does_not_save_subscription(self):
        self.outcome = 500
        with self.assertRaises(Rejected):
            self.subscribe()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM subscriptions").fetchone()[0], 0)

    def test_second_receiving_conversation_is_not_silently_selected(self):
        self.subscribe()
        with self.assertRaises(Rejected):
            self.subscribe(delivery={"mode": "webhook", "url": "https://callbacks.example/other", "secret": self.secret})
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM subscriptions").fetchone()[0], 1)

    def test_subscription_rotation_keeps_both_signatures_during_refresh_window(self):
        self.subscribe()
        replacement = "whsec_" + base64.b64encode(b"r" * 32).decode()
        self.subscribe(delivery={"mode": "webhook", "url": "https://callbacks.example/receive", "secret": replacement})
        self.now += 1
        self.subscribe(delivery={"mode": "webhook", "url": "https://callbacks.example/receive", "secret": replacement})
        self.send("grokbot", "Dot")
        self.app.dispatcher.once()
        self.assertEqual(len(self.calls[-1][2]["webhook-signature"].split()), 2)

    def test_subscription_filters_key_and_destination_validation(self):
        for overrides in [{"arguments": {"recipient": "stranger"}}, {"ttlMs": True}, {"ttlMs": -1}, {"delivery": {"mode": "webhook", "url": "http://127.0.0.1/", "secret": self.secret}}, {"delivery": {"mode": "webhook", "url": "https://callbacks.example/", "secret": "bad"}}]:
            with self.assertRaises(Rejected):
                self.subscribe(**overrides)
        self.assertEqual(self.calls, [])

    def test_delayed_event_can_claim_once_without_polling(self):
        self.send("grokbot", "Dot")
        self.assertTrue(self.store.claim("dot", "")["claimed"])
        self.assertEqual(self.store.claim("dot", ""), {"claimed": False, "reason": "empty"})

    def test_service_restart_differentiates_grok_uncertainty_from_dot_notification(self):
        self.send()
        self.send("grokbot", "Dot", "dot-incoming")
        self.store.db.execute("UPDATE messages SET state='dispatching'")
        self.store.close()
        self.store = Store(self.path, self.config, lambda: self.now)
        self.assertEqual(self.store.get("dot", "question")["state"], "unknown")
        self.assertEqual(self.store.get("dot", "dot-incoming")["state"], "queued")

    def test_native_reply_arriving_before_http_return_does_not_regress_read_state(self):
        self.send()
        def fast(url, body, headers):
            self.consume("grokbot", "question")
            return 200, b"{}"
        Dispatcher(self.store, fast).once()
        self.assertEqual(self.store.get("dot", "question")["state"], "read")

    def test_oauth_pkce_resource_binding_code_replay_refresh_and_revocation(self):
        oauth = self.app.oauth
        client = oauth.register({"redirect_uris": ["https://chatgpt.com/connector/oauth/test"]})["client_id"]
        verifier = "v" * 43
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        args = {"client_id": client, "redirect_uri": "https://chatgpt.com/connector/oauth/test", "resource": "https://bridge.example/mcp/dot", "response_type": "code", "code_challenge_method": "S256", "code_challenge": challenge, "state": "nonce"}
        approved = {**args, "owner_key": self.config["owner_key"], "csrf": oauth.csrf(args)}
        query = parse_qs(urlsplit(oauth.authorize(approved)).query)
        self.assertEqual(query["state"], ["nonce"])
        self.assertEqual(query["iss"], ["https://bridge.example"])
        grant = {"grant_type": "authorization_code", "client_id": client, "redirect_uri": args["redirect_uri"], "resource": args["resource"], "code": query["code"][0], "code_verifier": verifier}
        with self.assertRaises(Rejected):
            oauth.token({**grant, "code_verifier": "wrong"})
        tokens = oauth.token(grant)
        self.assertEqual(oauth.actor(tokens["access_token"], args["resource"]), "dot")
        for call in [lambda: oauth.token(grant), lambda: oauth.actor(tokens["access_token"], "https://bridge.example/mcp/stranger")]:
            with self.assertRaises(Rejected):
                call()
        refreshed = oauth.token({"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client, "resource": args["resource"]})
        self.assertNotEqual(refreshed["refresh_token"], tokens["refresh_token"])
        oauth.revoke({"token": refreshed["refresh_token"], "client_id": client})
        with self.assertRaises(Rejected):
            oauth.actor(tokens["access_token"], args["resource"])

    def test_oauth_redirect_csrf_and_cross_resource_approvals_rejected(self):
        for uri in ["http://chatgpt.com/callback", "https://chatgpt.com.evil.example/callback", "https://chatgpt.com@evil.example/callback", "https://chatgpt.com/callback#fragment"]:
            with self.assertRaises(Rejected):
                self.app.oauth.register({"redirect_uris": [uri]})
        with self.assertRaises(Rejected):
            self.app.oauth.resource_peer("https://bridge.example/mcp/grokbot")
        self.assertNotEqual(self.app.oauth.csrf({"resource": "a"}), self.app.oauth.csrf({"resource": "b"}))

    def test_rpc_discovery_and_peer_bound_tools(self):
        for method, expected in [("server/discover", "capabilities"), ("initialize", "serverInfo"), ("events/list", "events"), ("tools/list", "tools")]:
            result = self.app.rpc("dot", {"id": 1, "method": method})
            self.assertIn(expected, result["result"])
        result = self.app.rpc("dot", {"id": 2, "method": "tools/call", "params": {"name": "list_peers"}})
        self.assertEqual(result["result"]["structuredContent"]["self"], "dot")
        self.assertIsNone(self.app.rpc("dot", {"method": "notifications/initialized"}))

    def test_ssrf_public_ip_validation_before_connect(self):
        for ip in ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"]:
            with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", (ip, 443))]):
                with self.assertRaises(Rejected):
                    post_https("https://public.example/", b"{}", {})

    def test_configuration_is_private_distinct_keys_and_separate_store(self):
        path = Path(self.directory.name) / "config.json"
        path.write_text(json.dumps(self.config))
        path.chmod(0o600)
        load(path)
        path.chmod(0o644)
        with self.assertRaises(Rejected):
            load(path)
        path.chmod(0o600)
        self.config["peers"]["dot"]["key"] = self.config["peers"]["grokbot"]["key"]
        path.write_text(json.dumps(self.config))
        with self.assertRaises(Rejected):
            load(path)

    def test_starter_refuses_overwrite_and_does_not_print_secrets(self):
        directory = Path(self.directory.name) / "new"
        result = initialize(directory, "https://bridge.example")
        self.assertFalse(result["secrets_printed"])
        self.assertEqual((directory / "config.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads((directory / "config.json").read_text())["callback_hosts"], ["connectors.api.openai.com"])
        with self.assertRaises(FileExistsError):
            initialize(directory, "https://bridge.example")

    def test_foreign_database_is_refused_without_schema_changes(self):
        path = Path(self.directory.name) / "valuable.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE unrelated(value TEXT)")
            db.execute("INSERT INTO unrelated VALUES('preserve')")
            db.commit()
        with self.assertRaises(Rejected):
            Store(path, self.config)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("SELECT value FROM unrelated").fetchone()[0], "preserve")
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("unrelated",)])

    def test_duplicate_form_fields_rejected(self):
        with self.assertRaises(Rejected):
            form(b"resource=a&resource=b")


if __name__ == "__main__":
    unittest.main()
