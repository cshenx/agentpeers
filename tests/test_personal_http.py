"""Real loopback HTTP journeys with fixture vendor transports, not live-agent acceptance."""
import base64
import hashlib
import html
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import re
import socket
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit

from agentpeers.personal.server import App, handler
from agentpeers.personal.store import Store
from test_personal import configuration


class PersonalHTTP(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = configuration()
        self.store = Store(Path(self.temp.name) / "personal.sqlite3", self.config)
        self.deliveries = []
        def vendor(url, body, headers):
            payload = json.loads(body)
            self.deliveries.append((url, payload, headers))
            return 200, json.dumps({"challenge": payload.get("challenge")}).encode()
        self.app = App(self.store, vendor)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.store.close()
        self.temp.cleanup()

    def request(self, method, path, body=None, token=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        h = dict(headers or {})
        if token:
            h["Authorization"] = "Bearer " + token
        if body is not None and not isinstance(body, str):
            body = json.dumps(body)
            h["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=h)
        response = connection.getresponse()
        status, response_headers, raw = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return status, response_headers, json.loads(raw) if raw and "application/json" in response_headers.get("Content-Type", "") else raw.decode()

    def connect_dot(self):
        status, _, client = self.request("POST", "/oauth/register", {"redirect_uris": ["https://chatgpt.com/connector/oauth/test"]})
        self.assertEqual(status, 201)
        verifier = "v" * 43
        args = {"client_id": client["client_id"], "redirect_uri": client["redirect_uris"][0], "resource": "https://bridge.example/mcp/dot", "response_type": "code", "code_challenge_method": "S256", "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("="), "state": "return-state"}
        status, approval_headers, page = self.request("GET", "/oauth/authorize?" + urlencode(args))
        self.assertEqual(status, 200)
        self.assertEqual(approval_headers['Referrer-Policy'], 'strict-origin')
        self.assertEqual(approval_headers['Content-Security-Policy'], "default-src 'none'; form-action 'self' https://chatgpt.com; frame-ancestors 'none'; base-uri 'none'")
        self.assertIn('<form method="post" action="/oauth/authorize">', page)
        fields = {html.unescape(k): html.unescape(v) for k, v in re.findall('type="hidden" name="([^"]+)" value="([^"]*)"', page)}
        status, headers, _ = self.request("POST", "/oauth/authorize", urlencode({**fields, "owner_key": self.config["owner_key"]}), headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": self.config["public_url"]})
        self.assertEqual(status, 303)
        code = parse_qs(urlsplit(headers["Location"]).query)["code"][0]
        status, _, token = self.request("POST", "/oauth/token", urlencode({"grant_type": "authorization_code", "code": code, "code_verifier": verifier, "redirect_uri": args["redirect_uri"], "client_id": client["client_id"], "resource": args["resource"]}), headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 200)
        return token["access_token"]

    def rpc(self, token, method, params=None):
        status, _, response = self.request("POST", "/mcp/dot", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, token)
        self.assertEqual(status, 200)
        self.assertNotIn("error", response)
        return response["result"]

    def tool(self, token, name, **args):
        result = self.rpc(token, "tools/call", {"name": name, "arguments": args})
        self.assertNotIn("isError", result)
        return result["structuredContent"]

    def test_existing_dot_plugin_and_grok_callback_round_trip(self):
        status, headers, _ = self.request("POST", "/mcp/dot", {"id": 1, "method": "tools/list"})
        self.assertEqual(status, 401)
        metadata_path = "/.well-known/oauth-protected-resource/mcp/dot"
        self.assertIn(metadata_path, headers["WWW-Authenticate"])
        self.assertEqual(self.request("GET", metadata_path)[2]["resource"], "https://bridge.example/mcp/dot")
        token = self.connect_dot()
        self.rpc(token, "initialize", {"protocolVersion": "2026-07-28"})
        secret = "whsec_" + base64.b64encode(b"k" * 32).decode()
        self.rpc(token, "events/subscribe", {"name": "message.created", "arguments": {"recipient": "dot"}, "delivery": {"mode": "webhook", "url": "https://callbacks.example/event", "secret": secret}})
        peers = self.tool(token, "list_peers")
        self.assertEqual(peers["peers"][0]["id"], "grokbot")
        question = self.tool(token, "send_message", to="Grokbot", message="Which transport is native?", scope="One fixture conversation", message_id="http-question")
        self.assertIsNone(question["read_at"])
        self.app.dispatcher.once()
        callback = urlsplit(self.deliveries[-1][1]["callback_url"]).path
        status, _, incoming = self.request("POST", callback, {"action": "claim"})
        self.assertEqual(status, 200)
        self.assertTrue(incoming["claimed"])
        self.assertEqual(incoming["body"], "Which transport is native?")
        self.request("POST", callback, {"action": "acknowledge"})
        status, _, answer = self.request("POST", callback, {"action": "reply", "message": "Routine webhook. Should replies use events?", "reply_id": "http-answer", "expects": "reply"})
        self.assertEqual(status, 200)
        self.app.dispatcher.once()
        self.assertEqual(self.deliveries[-1][1]["eventId"], "http-answer")
        incoming = self.tool(token, "claim_message", message_id="http-answer")
        self.assertIn("Should replies", incoming["body"])
        self.tool(token, "acknowledge", message_id="http-answer")
        final = self.tool(token, "reply", message_id="http-answer", message="Yes, MCP Events for Dot.", reply_id="http-final")
        self.assertEqual(final["expects"], "none")
        self.app.dispatcher.once()
        callback = urlsplit(self.deliveries[-1][1]["callback_url"]).path
        self.request("POST", callback, {"action": "claim"})
        self.request("POST", callback, {"action": "acknowledge"})
        self.assertEqual(self.tool(token, "message_status", message_id="http-final")["state"], "read")

    def test_grok_can_initiate_through_authenticated_common_tools(self):
        token = self.connect_dot()
        status, _, sent = self.request("POST", "/v1/call", {"tool": "send_message", "arguments": {"to": "Dot", "message": "An unsolicited finding", "scope": "Fixture test", "message_id": "grok-start"}}, "g" * 40)
        self.assertEqual(status, 200)
        self.assertEqual(sent["sender"], "grokbot")
        incoming = self.tool(token, "claim_message", message_id="grok-start")
        self.assertTrue(incoming["claimed"])
        self.assertEqual(incoming["sender"], "grokbot")

    def test_credential_is_peer_bound_and_unauthenticated_api_is_closed(self):
        self.assertEqual(self.request("POST", "/v1/call", {"tool": "list_peers"})[0], 401)
        self.assertEqual(self.request("POST", "/mcp/dot", {"id": 1, "method": "tools/list"}, "g" * 40)[0], 401)
        self.assertEqual(self.request("GET", "/health")[2], {"service": "AgentPeers", "delivery_verified": False, "worker_warning": False})

    def test_callback_error_diagnostics_do_not_echo_supplied_secret_or_body(self):
        status, _, value = self.request("POST", "/callbacks/unknown/" + "s" * 43, {"action": "reply", "message": "private-body"})
        self.assertEqual(status, 400)
        self.assertNotIn("private-body", json.dumps(value))
        self.assertNotIn("s" * 43, json.dumps(value))

    def test_partial_request_headers_cannot_hold_a_worker_indefinitely(self):
        self.server.RequestHandlerClass.request_timeout = 0.1
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2) as client:
            client.sendall(b"POST /v1/call HTTP/1.1\r\nHost: localhost\r\n")
            self.assertEqual(client.recv(1), b"")


if __name__ == "__main__":
    unittest.main()
