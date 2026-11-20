"""Opt-in wake for Codex chats with no live route (0.11). codex queue and open are always stubbed."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from agentpeers import codex_wake, native
from agentpeers.core import Bridge, AUTHORITY
from agentpeers.desktop_wake import enabled as desktop_wake_enabled, policy

ROOT = Path(__file__).resolve().parent.parent
C = "codex:" + str(uuid.uuid4())
IDENT = C.split(":", 1)[1]
OTHER = "codex:" + str(uuid.uuid4())
A = "claude:" + str(uuid.uuid4())
QUEUE_ID = str(uuid.uuid4())

PINNED = ("AgentPeers wake signal. This text was generated automatically by AgentPeers, not written by the user. "
          "It is not a peer message and grants no permission. An AgentPeers message may be waiting for this chat: "
          "call the AgentPeers inbox tool, and for each unread message addressed to this chat, handle it under its "
          "own authority note and acknowledge it with the AgentPeers acknowledge tool. Do not acknowledge a message "
          "that is already read. If nothing is unread, reply \"No unread AgentPeers messages\" and take no other action.")
PINNED_SHA256 = "8cb493f590d2dc35abe95b91a16b010173b7f5774202347acedf649b1a522394"


def no_route():
    return native.Unavailable("Desktop IPC connection failed", code="desktop_ipc_unsupported",
                              live_delivery_attempted=False)


class Fake:
    """Stands in for subprocess.run: records argv, answers codex queue and open, refuses anything else."""

    def __init__(self, queue=0, opened=0, queue_out=None, queue_error=None):
        self.calls, self.queue, self.opened, self.queue_error = [], queue, opened, queue_error
        self.queue_out = ("Queued message " + QUEUE_ID + " for thread " + IDENT + ".\n") if queue_out is None else queue_out

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[1:3] == ["queue", "--thread"]:
            if self.queue_error:
                raise self.queue_error
            return subprocess.CompletedProcess(argv, self.queue, self.queue_out if self.queue == 0 else "", "")
        if argv[0] == "/usr/bin/open":
            return subprocess.CompletedProcess(argv, self.opened, b"", b"")
        raise AssertionError("unexpected command " + repr(argv))


class Base(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        os.chmod(self.folder.name, 0o700)
        self.environment = patch.dict(os.environ, {"AGENTPEERS_HOME": self.folder.name})
        self.environment.start()
        os.environ.pop("AGENTPEERS_WAKE", None)
        os.environ.pop("PEERBRIDGE_WAKE", None)
        self.bridge = Bridge()
        self.patches = [patch.object(native, "send_codex", side_effect=lambda *a: (_ for _ in ()).throw(no_route())),
                        patch.object(native, "codex_sessions", return_value=[]),
                        patch.object(native, "receive_route", return_value={"session_id": A, "status": "available"}),
                        patch.object(codex_wake.platform, "system", return_value="Darwin"),
                        patch.object(codex_wake, "queue_binary", return_value="/fixture/codex")]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.bridge.db.close()
        self.environment.stop()
        self.folder.cleanup()

    def send(self, fake, text="fixture body", to=C, **options):
        with patch.object(codex_wake.subprocess, "run", side_effect=fake):
            return self.bridge.send(A, to, text, "test only", expects="ack", **options)

    def queue_argv(self):
        return ["/fixture/codex", "queue", "--thread", IDENT, "--message", PINNED]

    def open_argv(self):
        return ["/usr/bin/open", "-g", "codex://threads/wake/" + IDENT]


class WakeText(unittest.TestCase):
    def test_wake_text_is_pinned_byte_for_byte(self):
        self.assertEqual(codex_wake.WAKE_TEXT, PINNED)
        self.assertEqual(codex_wake.WAKE_TEXT.encode("utf-8"), PINNED.encode("utf-8"))
        self.assertEqual(len(codex_wake.WAKE_TEXT.encode("utf-8")), 521)
        self.assertEqual(hashlib.sha256(codex_wake.WAKE_TEXT.encode("utf-8")).hexdigest(), PINNED_SHA256)
        for placeholder in ("{", "}", "%s", "<", ">"):
            self.assertNotIn(placeholder, codex_wake.WAKE_TEXT)


class OptIn(Base):
    def test_off_by_default_and_nothing_runs(self):
        fake = Fake()
        message = self.send(fake)
        self.assertEqual(fake.calls, [])
        self.assertIsNone(message["wake"])
        self.assertNotIn("receipt", message)
        self.assertTrue(message["delivery_note"].startswith("Saved in the recipient's mailbox"))
        self.assertEqual(codex_wake.status(self.bridge.db, C)["chats"][0]["wake"], "off")

    def test_on_records_who_and_when(self):
        result = codex_wake.turn_on(self.bridge.db, C, None)["chats"][0]
        self.assertEqual((result["wake"], result["recorded"]), ("on", "on"))
        self.assertEqual(result["on_by"], "terminal user (no agent session detected)")
        self.assertIsInstance(result["on_at"], float)
        result = codex_wake.turn_on(self.bridge.db, IDENT, C)["chats"][0]
        self.assertEqual((result["chat"], result["on_by"]), (C, C))

    def test_a_sender_can_never_turn_it_on(self):
        for sender in (A, OTHER):
            with self.assertRaisesRegex(ValueError, "A sender can never turn it on"):
                codex_wake.turn_on(self.bridge.db, C, sender)
        self.assertEqual(codex_wake.status(self.bridge.db, C)["chats"][0]["wake"], "off")
        for bad in ("claude:" + IDENT, "reviewer", IDENT[:8], "codex:" + IDENT[:8]):
            with self.assertRaises(ValueError):
                codex_wake.turn_on(self.bridge.db, bad, None)

    def test_caller_comes_from_process_ancestry_not_from(self):
        parent = os.getppid()
        with patch.object(native, "claude_sessions", return_value=[{"pid": parent, "id": A}]), \
             patch.dict(os.environ, {"CODEX_THREAD_ID": IDENT, "AGENTPEERS_SESSION": C}):
            self.assertEqual(codex_wake.caller(), A)
        fake_ps = lambda argv, **k: subprocess.CompletedProcess(argv, 0, "1\n", "")
        with patch.object(native, "claude_sessions", return_value=[]), \
             patch.object(codex_wake.subprocess, "run", side_effect=fake_ps):
            with patch.dict(os.environ, {"CODEX_THREAD_ID": IDENT}):
                self.assertEqual(codex_wake.caller(), C)
            with patch.dict(os.environ, {}, clear=False):
                for name in ("CODEX_THREAD_ID", "AGENTPEERS_SESSION", "PEERBRIDGE_SESSION"):
                    os.environ.pop(name, None)
                self.assertIsNone(codex_wake.caller())
        with patch.object(native, "claude_sessions", side_effect=native.Unavailable("sandbox")):
            with self.assertRaisesRegex(ValueError, "wake not turned on"):
                codex_wake.caller()

    def test_cli_refuses_a_claude_session_and_records_the_chat_itself(self):
        from agentpeers import cli
        def run(*argv, by=None):
            out, err = io.StringIO(), io.StringIO()
            with patch.object(sys, "argv", ["agentpeers", *argv]), patch.object(codex_wake, "caller", return_value=by), \
                 patch("sys.stdout", out), patch("sys.stderr", err):
                try:
                    cli.main()
                    code = 0
                except SystemExit as exc:
                    code = exc.code
            return code, out.getvalue(), err.getvalue()
        code, _, err = run("wake", "on", C, by=A)
        self.assertEqual(code, 1)
        self.assertIn("A sender can never turn it on", err)
        code, out, _ = run("wake", "on", C, by=C)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["chats"][0]["on_by"], C)
        self.assertEqual(run("wake", "on", "--all")[0], 1)
        self.assertEqual(run("wake", "off")[0], 1)
        code, out, _ = run("wake", "off", "--all", by=A)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["chats"][0]["off_by"], A)
        code, out, _ = run("wake", "status", C)
        self.assertEqual(json.loads(out)["chats"][0]["wake"], "off")
        self.assertIn("AGENTPEERS_WAKE=off", json.loads(out)["disclosure"])

    def test_off_for_one_chat_and_for_all(self):
        codex_wake.turn_on(self.bridge.db, C, None)
        codex_wake.turn_on(self.bridge.db, OTHER, None)
        codex_wake.turn_off(self.bridge.db, C, A)
        self.assertFalse(codex_wake.enabled(self.bridge.db, C))
        self.assertTrue(codex_wake.enabled(self.bridge.db, OTHER))
        self.assertEqual(codex_wake.status(self.bridge.db, C)["chats"][0]["off_by"], A)
        codex_wake.turn_off(self.bridge.db, by=None, everything=True)
        self.assertFalse(codex_wake.enabled(self.bridge.db, OTHER))
        fake = Fake()
        self.send(fake)
        self.assertEqual(fake.calls, [])

    def test_environment_off_overrides_everything(self):
        codex_wake.turn_on(self.bridge.db, C, None)
        policy(True)
        with patch.dict(os.environ, {"AGENTPEERS_WAKE": "off"}):
            fake = Fake()
            message = self.send(fake)
            self.assertEqual(fake.calls, [])
            self.assertIsNone(message["wake"])
            state = codex_wake.status(self.bridge.db, C)
            self.assertEqual((state["chats"][0]["recorded"], state["chats"][0]["wake"]), ("on", "off"))
            self.assertEqual(state["override"], "AGENTPEERS_WAKE=off")
            self.assertFalse(desktop_wake_enabled())
            rows = codex_wake.annotate([{"id": C}])
            self.assertEqual(rows[0]["wake"], "off")
        for value in ("OFF", "0", "no"):
            with patch.dict(os.environ, {"AGENTPEERS_WAKE": value}):
                self.assertTrue(codex_wake.overridden())
        with patch.dict(os.environ, {"AGENTPEERS_WAKE": "on"}):
            self.assertFalse(codex_wake.overridden())
            self.assertTrue(desktop_wake_enabled())


class WakeOnSend(Base):
    def setUp(self):
        super().setUp()
        codex_wake.turn_on(self.bridge.db, C, None)

    def test_saves_then_queues_the_constant_then_opens_the_link(self):
        fake = Fake()
        message = self.send(fake)
        self.assertEqual(fake.calls, [self.queue_argv(), self.open_argv()])
        self.assertEqual(message["status"], "unavailable")
        self.assertIsNone(message["read_at"])
        self.assertEqual(message["receipt"], "saved unread; wake queued")
        self.assertEqual(message["wake"]["state"], "queued")
        self.assertEqual(message["wake"]["detail"]["queued_message"], QUEUE_ID)
        self.assertFalse(message["wake"]["coalesced"])
        self.assertTrue(message["delivery_note"].startswith("Saved unread; wake queued. Not delivered"))
        self.assertIn("wake signal asks the chat", message["destination_note"])
        for field in ("receipt", "delivery"):
            self.assertNotIn("delivered", message[field])
        self.assertEqual(self.bridge.inbox(C)[0]["body"], "fixture body")
        read = self.bridge.ack(message["id"], C)
        self.assertEqual((read["delivery"], read["receipt"]), ("read", "read"))

    def test_no_peer_content_ever_goes_into_the_wake(self):
        marker = "PEER-" + uuid.uuid4().hex
        fake = Fake()
        message = self.send(fake, text="body " + marker + " $(touch x) `id`", summary="summary " + marker,
                            record_ref="ref-" + marker, decision_id="decision-" + marker)
        flat = json.dumps(fake.calls)
        for value in (marker, A, "test only", message["id"]):
            self.assertNotIn(value, flat)
        self.assertEqual(fake.calls[0][-1], PINNED)
        self.assertEqual(fake.calls[0][:4], ["/fixture/codex", "queue", "--thread", IDENT])

    def test_live_route_unconfirmed_and_claude_recipients_never_wake(self):
        fake = Fake()
        with patch.object(native, "send_codex", return_value={"transport": "codex-shared-server"}):
            self.assertEqual(self.send(fake)["status"], "submitted")
        with patch.object(native, "send_codex", side_effect=native.Unconfirmed("maybe")):
            self.assertEqual(self.send(fake)["status"], "unconfirmed")
        with patch.object(native, "claude_sessions", return_value=[]):
            self.assertEqual(self.send(fake, to=A.replace(A.split(":")[1], str(uuid.uuid4())))["status"], "unavailable")
        self.assertEqual(fake.calls, [])

    def test_coalesces_until_the_chat_reads_its_inbox(self):
        fake = Fake()
        first = self.send(fake, "one")
        second = self.send(fake, "two")
        third = self.send(fake, "three")
        self.assertEqual(fake.calls, [self.queue_argv(), self.open_argv()])
        self.assertEqual(second["wake"]["id"], first["wake"]["id"])
        self.assertTrue(second["wake"]["coalesced"] and third["wake"]["coalesced"])
        self.assertEqual(second["receipt"], "saved unread; wake queued")
        self.assertIn("earlier wake", second["wake"]["note"])
        # The woken chat reads its inbox: later messages may arrive after that read, so they get a new wake.
        self.assertEqual([m["body"] for m in self.bridge.inbox(C, unread=True)], ["three", "two", "one"])
        fourth = self.send(fake, "four")
        self.assertEqual(len(fake.calls), 4)
        self.assertNotEqual(fourth["wake"]["id"], first["wake"]["id"])
        self.assertFalse(fourth["wake"]["coalesced"])

    def test_every_covered_message_read_ends_coalescing(self):
        fake = Fake()
        first = self.send(fake, "one")
        self.bridge.ack(first["id"], C)
        self.send(fake, "two")
        self.assertEqual(len(fake.calls), 4)

    def test_stale_wake_stops_coalescing(self):
        fake = Fake()
        self.send(fake, "one")
        later = codex_wake.time.time() + codex_wake.COALESCE_SECONDS + 1
        with patch.object(codex_wake.time, "time", return_value=later):
            self.send(fake, "two")
        self.assertEqual(len(fake.calls), 4)

    def test_failed_queue_is_recorded_and_never_retried(self):
        fake = Fake(queue=1)
        message = self.send(fake)
        self.assertEqual(fake.calls, [self.queue_argv()])
        self.assertEqual(message["wake"]["state"], "failed")
        self.assertEqual(message["receipt"], "saved unread; wake failed, not retried")
        self.assertTrue(message["delivery_note"].startswith("Saved unread; wake failed, not retried. Not delivered."))
        # Only a later, different message makes a new attempt; the failed one is not repeated.
        self.send(Fake(queue=1))
        self.assertEqual(len(fake.calls), 1)

    def test_uncertain_queue_never_opens_or_retries_and_still_coalesces(self):
        for fake in (Fake(queue_error=subprocess.TimeoutExpired("codex", 20)), Fake(queue_out="something else\n")):
            with self.subTest(fake=fake):
                codex_wake.turn_on(self.bridge.db, C, None)
                self.bridge.db.execute("DELETE FROM wake_signals")
                self.bridge.db.commit()
                message = self.send(fake)
                self.assertEqual(fake.calls, [self.queue_argv()])
                self.assertEqual(message["receipt"], "saved unread; wake uncertain, not retried")
                self.send(fake)
                self.assertEqual(len(fake.calls), 1)

    def test_missing_cli_and_failed_link_are_recorded(self):
        message = self.send(Fake(queue_error=FileNotFoundError("codex")))
        self.assertEqual(message["wake"]["state"], "failed")
        fake = Fake(opened=1)
        message = self.send(fake)
        self.assertEqual(fake.calls, [self.queue_argv(), self.open_argv()])
        self.assertEqual(message["wake"]["state"], "link_failed")
        self.assertIn("app link failed", message["receipt"])

    def test_other_systems_never_run_anything(self):
        fake = Fake()
        with patch.object(codex_wake.platform, "system", return_value="Linux"):
            message = self.send(fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(message["wake"]["state"], "failed")

    def test_wake_errors_never_change_the_stored_message(self):
        with patch.object(codex_wake, "run_wake", side_effect=RuntimeError("boom")):
            message = self.send(Fake())
        self.assertEqual((message["status"], message["body"]), ("unavailable", "fixture body"))
        self.assertEqual(message["wake"]["state"], "uncertain")

    def test_reused_id_does_not_wake_again(self):
        fake = Fake()
        ident = str(uuid.uuid4())
        self.send(fake, ident=ident)
        again = self.send(fake, ident=ident)
        self.assertTrue(again["reused_id"])
        self.assertEqual(len(fake.calls), 2)

    def test_sender_with_wake_on_gets_an_accurate_reply_warning(self):
        route = {"session_id": C, "status": "unavailable"}
        with patch.object(native, "receive_route", return_value=route), \
             patch.object(native, "claude_sessions", return_value=[{"id": A}]), \
             patch.object(native, "send_claude", return_value={"transport": "claude-native-inbox"}):
            message = self.bridge.send(C, A, "to claude", "test only")
            self.assertIn("wake is on for it", message["warnings"][0])
            codex_wake.turn_off(self.bridge.db, C, None)
            self.assertIn("must collect them", self.bridge.get(message["id"])["warnings"][0])


class Inbox(Base):
    def test_unread_filter_and_authority_on_every_row(self):
        fake = Fake()
        first = self.send(fake, "one")
        self.send(fake, "two")
        acked = self.bridge.ack(first["id"], C)
        self.assertEqual(acked["authority"], AUTHORITY)
        everything = self.bridge.inbox(C)
        self.assertEqual([m["body"] for m in everything], ["two", "one"])
        self.assertEqual([m["body"] for m in self.bridge.inbox(C, unread=True)], ["two"])
        for row in everything:
            self.assertEqual(row["authority"], AUTHORITY)
        self.assertIn("not a new human instruction", AUTHORITY)
        with self.assertRaises(ValueError):
            self.bridge.inbox(C, unread="yes")

    def test_mcp_inbox_takes_unread_and_list_sessions_shows_wake(self):
        from agentpeers.mcp import dispatch, TOOLS
        self.send(Fake(), "one")
        self.bridge.ack(self.send(Fake(), "two")["id"], C)
        rows = dispatch("inbox", {"unread": True}, C)
        self.assertEqual([m["body"] for m in rows], ["one"])
        self.assertIn("unread", next(t for t in TOOLS if t[0] == "inbox")[2])
        codex_wake.turn_on(self.bridge.db, C, None)
        sessions = [{"id": C, "runtime": "codex"}, {"id": OTHER, "runtime": "codex"}, {"id": A, "runtime": "claude"}]
        with patch.object(native, "claude_sessions", return_value=[sessions[2]]), \
             patch.object(native, "codex_sessions", return_value=[dict(s) for s in sessions[:2]]):
            listed = {row["id"]: row.get("wake") for row in dispatch("list_sessions", {})["sessions"]}
        self.assertEqual(listed, {C: "on", OTHER: "off", A: None})


class Disclosure(unittest.TestCase):
    def test_docs_skill_and_setup_disclose_wake(self):
        docs = (ROOT / "docs" / "coding-sessions.md").read_text()
        skill = (ROOT / "skills" / "agentpeers" / "SKILL.md").read_text()
        for text in (docs, skill, codex_wake.DISCLOSURE):
            for phrase in ("agentpeers wake on", "agentpeers wake off", "AGENTPEERS_WAKE=off", "user-looking message",
                           "not from you"):
                self.assertTrue(phrase in text, phrase)
        self.assertIn(PINNED, docs)
        self.assertIn("saved unread; wake queued", docs)
        self.assertIn("never a sender", codex_wake.DISCLOSURE)

    def test_setup_reports_wake_without_creating_the_store(self):
        from agentpeers import setup
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"AGENTPEERS_HOME": home + "/store"}), \
             patch.object(setup, "registrations", return_value={}), \
             patch.object(native, "claude_sessions", return_value=[]), \
             patch.object(native, "shared_threads", side_effect=native.Unavailable("none")), \
             patch.object(native, "Desktop", side_effect=native.Unavailable("none")):
            result = setup.doctor(actor=None)
            self.assertFalse(Path(home, "store").exists())
        check = next(c for c in result["checks"] if c["name"] == "codex_wake")
        self.assertTrue(check["detail"].startswith("Off for every chat."))
        self.assertIn(codex_wake.DISCLOSURE, check["detail"])


class Binary(unittest.TestCase):
    def test_prefers_the_cli_bundled_with_the_desktop_app(self):
        with tempfile.TemporaryDirectory() as root:
            binary = Path(root) / "ChatGPT.app" / "Contents" / "Resources" / "codex-cli" / "bin" / "codex"
            with patch.object(native, "codex_desktop_app", return_value={"path": str(Path(root) / "ChatGPT.app")}), \
                 patch.dict(os.environ, {"AGENTPEERS_CODEX": "/configured/codex"}):
                self.assertEqual(codex_wake.queue_binary(), "/configured/codex")
                binary.parent.mkdir(parents=True)
                binary.write_text("#!/bin/sh\n")
                binary.chmod(0o755)
                self.assertEqual(codex_wake.queue_binary(), str(binary))


if __name__ == "__main__":
    unittest.main()
