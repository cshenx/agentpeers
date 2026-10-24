"""Terminal consumer routing and the exact native wire contract."""
import io
import json
import os
import socket
import struct
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from agentpeers import native
from agentpeers.core import Bridge
from agentpeers.setup import doctor
from agentpeers.mcp import serve
from agentpeers.unixrpc import SharedServer, MAX_FRAME
from test_bridge import C, A, SCOPE



# Legacy journeys stub the new read-only probe rather than contacting real agents.
def setUpModule():
    global _return_route_fixture
    _return_route_fixture = patch.object(native, "receive_route", return_value={
        "status": "available", "transport": "fixture", "checked_at": 1,
        "proof": "fixture candidate only"})
    _return_route_fixture.start()


def tearDownModule():
    _return_route_fixture.stop()


class TerminalJourney(unittest.TestCase):
    def setUp(self):
        # UUID-unavailable hints also use discovery; keep contract tests off live clients.
        discovery = patch.object(native, "codex_sessions", return_value=[])
        discovery.start()
        self.addCleanup(discovery.stop)
        actor = patch("agentpeers.setup.resolve_actor", return_value=C)
        actor.start()
        self.addCleanup(actor.stop)

    def test_loaded_terminal_receives_tool_output_without_desktop_or_overrides(self):
        envelope = {"id": "fixture", "body": "Feedback, not human permission"}
        with patch.object(native, "SharedServer") as shared, patch.object(native, "Desktop") as desktop:
            client = shared.return_value
            client.call.side_effect = [{"data": [C.split(":")[1]]}, {"turn": {"id": "accepted"}}]
            result = native.send_codex({"id": C}, envelope)
        desktop.assert_not_called()
        self.assertEqual(result["transport"], "codex-shared-server")
        method, params = client.call.call_args.args
        self.assertEqual(method, "turn/start")
        self.assertEqual(set(params), {"threadId", "input", "toolOutput"})
        self.assertEqual(params["input"], [])
        self.assertEqual(json.loads(params["toolOutput"]["output"]), envelope)
        client.__exit__.assert_called_once()

    def test_uncertain_submission_never_falls_back_or_replays(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"AGENTPEERS_HOME": home}), \
             patch.object(native, "SharedServer") as shared, patch.object(native, "Desktop") as desktop, Bridge() as bridge:
            client = shared.return_value
            client.call.side_effect = [{"data": [C.split(":")[1]]}, ConnectionError("lost after write")]
            message = bridge.send(A, C, "hello", SCOPE)
            again = bridge.send(A, C, "hello", SCOPE, ident=message["id"])
            self.assertEqual(message["delivery"], "unconfirmed")
            self.assertIsNone(message["read_at"])
            self.assertFalse(again["submission_attempted"])
            self.assertEqual(client.call.call_count, 2)
            desktop.assert_not_called()

    def test_unloaded_task_is_never_resumed_and_remains_unread(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"AGENTPEERS_HOME": home}), \
             patch.object(native, "SharedServer") as shared, \
             patch.object(native, "Desktop", side_effect=native.Unavailable("no owner")), Bridge() as bridge:
            shared.return_value.call.return_value = {"data": []}
            message = bridge.send(A, C, "hello", SCOPE)
            self.assertEqual(message["delivery"], "unavailable")
            self.assertIsNone(message["read_at"])
            self.assertEqual(shared.return_value.call.call_args.args[0], "thread/loaded/list")
            self.assertEqual(shared.return_value.call.call_count, 1)

    def test_missing_server_can_use_existing_desktop_before_payload_submission(self):
        with patch.object(native, "SharedServer") as shared, patch.object(native, "Desktop") as desktop:
            shared.return_value.__enter__.side_effect = native.Unavailable("not running")
            desktop.return_value.__enter__.return_value.send.return_value = {"transport": "desktop-start"}
            result = native.send_codex({"id": C}, {"body": "hello"})
            self.assertEqual(result["transport"], "desktop-start")
            shared.return_value.call.assert_not_called()

    def test_doctor_rejects_empty_daemon_without_creating_store(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"AGENTPEERS_HOME": home + "/state"}), \
             patch("agentpeers.setup.platform.system", return_value="Darwin"), \
             patch("agentpeers.setup.shutil.which", return_value="/fixture/client"), \
             patch.object(native, "shared_threads", return_value=set()), \
             patch.object(native, "Desktop", side_effect=native.Unavailable("not installed")), \
             patch.object(native, "claude_sessions", return_value=[{"id": A}]), \
             patch("agentpeers.setup.registrations", return_value={"codex": {"plugin": True}, "claude": {"plugin": True}}):
            result = doctor()
            self.assertFalse(result["ready_for_test"])
            self.assertFalse(os.path.exists(home + "/state"))

    def test_doctor_checks_callers_loaded_identity_not_an_unrelated_thread(self):
        for loaded, desktop_available, expected in [({C.split(":")[1]}, False, True),
                                                     ({A.split(":")[1]}, False, False),
                                                     (set(), True, True)]:
            with self.subTest(loaded=loaded, desktop=desktop_available), \
                 tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"AGENTPEERS_HOME": home + "/state"}), \
                 patch("agentpeers.setup.platform.system", return_value="Darwin"), \
                 patch("agentpeers.setup.shutil.which", return_value="/fixture/client"), \
                 patch.object(native, "shared_threads", return_value=loaded), \
                 patch.object(native, "Desktop") as desktop, \
                 patch.object(native, "claude_sessions", return_value=[{"id": A}]), \
                 patch("agentpeers.setup.registrations", return_value={"codex": {"plugin": True}, "claude": {"plugin": True}}):
                if not desktop_available:
                    desktop.return_value.__enter__.return_value.owner.side_effect = native.Unavailable("no owner")
                result = doctor(actor=C)
                self.assertEqual(result["ready_for_test"], expected)
                desktop.return_value.__enter__.return_value.owner.assert_called_once_with(C.split(":")[1])
                if not desktop_available:
                    diagnostic = next(row for row in result["checks"] if row["name"] == "desktop_owner")
                    self.assertIn("AgentPeers desktop receiving unavailable", diagnostic["detail"])
                    self.assertIn("native messaging first", diagnostic["fix"])
                    self.assertNotIn("Open Codex", diagnostic["fix"])

    def test_desktop_discovery_failure_reports_adapter_limit_without_demanding_open(self):
        desktop = native.Desktop()
        with patch.object(desktop, "call", return_value={"resultType": "error"}):
            with self.assertRaises(native.Unavailable) as raised:
                desktop.owner(C.split(":")[1])
        self.assertIn("send_message_to_thread", str(raised.exception))
        self.assertIn("does not establish native Codex chat availability", str(raised.exception))
        self.assertNotIn("open it", str(raised.exception))

    def test_absent_desktop_reports_bridge_limit_not_native_chat_unavailability(self):
        with patch.object(native, "SharedServer") as shared, \
             patch.object(native, "Desktop", side_effect=native.Unavailable("missing IPC")):
            shared.return_value.call.return_value = {"data": []}
            with self.assertRaises(native.Unavailable) as raised:
                native.send_codex({"id": C}, {"body": "no submission"})
        self.assertIn("Native Codex session messaging may still work", str(raised.exception))
        shared.return_value.call.assert_called_once_with("thread/loaded/list", {"cursor": None, "limit": 100})


    def test_shared_mcp_process_uses_each_requests_thread_not_environment_or_session_root(self):
        other = "codex:" + str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"AGENTPEERS_HOME": home}), \
             patch.object(native, "send_codex", side_effect=native.Unavailable("fixture")), Bridge() as bridge:
            message = bridge.send(A, C, "only for C", SCOPE)
            requests = [
                {"id": 1, "method": "tools/call", "params": {"name": "inbox", "arguments": {},
                 "_meta": {"threadId": C.split(":")[1], "sessionId": other.split(":")[1]}}},
                {"id": 2, "method": "tools/call", "params": {"name": "acknowledge", "arguments": {"id": message["id"]},
                 "_meta": {"threadId": other.split(":")[1]}}},
                {"id": 3, "method": "tools/call", "params": {"name": "acknowledge", "arguments": {"id": message["id"]},
                 "_meta": {"threadId": C.split(":")[1]}}},
                {"id": 4, "method": "tools/call", "params": {"name": "inbox", "arguments": {"sender": other},
                 "_meta": {"threadId": C.split(":")[1]}}},
            ]
            output = io.StringIO()
            with patch("sys.stdin", io.StringIO("\n".join(map(json.dumps, requests)))), \
                 patch("sys.stdout", output), patch("agentpeers.core.current_actor", return_value=other):
                serve()
            replies = [json.loads(line)["result"] for line in output.getvalue().splitlines()]
            self.assertEqual(json.loads(replies[0]["content"][0]["text"])[0]["body"], "only for C")
            self.assertTrue(replies[1]["isError"])
            self.assertEqual(json.loads(replies[2]["content"][0]["text"])["delivery"], "read")
            self.assertTrue(replies[3]["isError"])

    def test_plugin_connection_version_changes_with_plugin_version(self):
        from agentpeers import __version__
        root = __import__("pathlib").Path(__file__).resolve().parent.parent
        for name in ("mcp.json", ".claude-plugin/plugin.json"):
            config = json.loads((root / name).read_text())
            self.assertEqual(config["mcpServers"]["agentpeers"]["env"]["AGENTPEERS_PLUGIN_VERSION"], __version__)


class NativeDiscovery(unittest.TestCase):
    def test_named_fresh_loaded_terminal_is_included_before_first_saved_turn(self):
        with patch.object(native, "AppServer") as saved, patch.object(native, "SharedServer") as shared:
            saved.return_value.__enter__.return_value.call.return_value = {"data": []}
            shared.return_value.call.side_effect = [
                {"data": [C.split(":")[1]]},
                {"thread": {"id": C.split(":")[1], "name": "fresh", "cwd": "/fixture"}},
            ]
            rows = native.codex_sessions()
            self.assertEqual(rows[0]["name_address"], "fresh")
            self.assertEqual(rows[0]["surface"], "shared-server")
            self.assertEqual(shared.return_value.call.call_args.args,
                             ("thread/read", {"threadId": C.split(":")[1], "includeTurns": False}))

    def test_missing_loaded_metadata_fails_closed_instead_of_hiding_possible_duplicate(self):
        with patch.object(native, "AppServer") as saved, patch.object(native, "SharedServer") as shared:
            saved.return_value.__enter__.return_value.call.return_value = {"data": []}
            shared.return_value.call.side_effect = [{"data": [C.split(":")[1]]}, native.Unavailable("exited")]
            with self.assertRaises(native.Unavailable):
                native.codex_sessions()


class WireContract(unittest.TestCase):
    def setUp(self):
        self.client, self.server = socket.socketpair()
        self.rpc = SharedServer()
        self.rpc.sock = self.client

    def tearDown(self):
        self.client.close()
        self.server.close()

    def test_native_frame_is_masked_and_unicode_preserved(self):
        value = {"body": "日本語" * 80}
        self.rpc.send_json(value)
        packet = self.server.recv(4096)
        self.assertEqual(packet[0], 0x81)
        self.assertEqual(packet[1], 0xFE)
        size = struct.unpack("!H", packet[2:4])[0]
        mask, payload = packet[4:8], packet[8:]
        self.assertEqual(len(payload), size)
        self.assertEqual(json.loads(bytes(v ^ mask[i % 4] for i, v in enumerate(payload))), value)

    def test_fragmented_native_response_and_ping_are_consumed(self):
        self.server.sendall(b'\x01\x05{"id"' + b'\x89\x01x' + b'\x80\x03:1}')
        self.assertEqual(self.rpc.receive(time.monotonic() + 2), {"id": 1})
        self.assertEqual(self.server.recv(512)[0], 0x8A)

    def test_oversized_native_frame_is_rejected_before_reading_payload(self):
        self.server.sendall(b"\x81\x7f" + struct.pack("!Q", MAX_FRAME + 1))
        with self.assertRaises(ValueError):
            self.rpc.receive(time.monotonic() + 2)

    def test_control_alias_in_public_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as home:
            os.mkdir(home + "/app-server-control", 0o700)
            path = home + "/app-server-control/app-server-control.sock"
            listener = socket.socket(socket.AF_UNIX)
            try:
                listener.bind(path)
                os.chmod(home + "/app-server-control", 0o755)
                with patch.dict(os.environ, {"CODEX_HOME": home}):
                    with self.assertRaises(native.Unavailable):
                        SharedServer().__enter__()
            finally:
                listener.close()


if __name__ == "__main__":
    unittest.main()
