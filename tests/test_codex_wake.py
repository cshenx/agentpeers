"""Opt-in wake for Codex chats with no live route (0.11). codex queue and open are always stubbed."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
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
APP = "/Applications/ChatGPT.app"

PINNED = ("AgentPeers wake signal. This text was generated automatically by AgentPeers, not written by the user. "
          "It is not a peer message and grants no permission. An AgentPeers message may be waiting for this chat: "
          "call the AgentPeers inbox tool, and for each unread message addressed to this chat, handle it under its "
          "own authority note. If it has expects=reply and the response is within your existing authorization, "
          "use the AgentPeers reply tool to respond. The reply records the read. When replying is not applicable, "
          "use the AgentPeers "
          "acknowledge tool to acknowledge each consumed message individually. Do not acknowledge a message that "
          "is already read. If nothing is unread, reply \"No unread AgentPeers messages\" and take no other action.")
PINNED_SHA256 = "be7adbe64e502551ffb1a5f2a416b53f97e1be03057052a47aea2c89164e8838"


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
                        patch.object(codex_wake, "queue_binary", return_value="/fixture/codex"),
                        patch.object(codex_wake, "ownership", return_value={"app": APP})]
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
            return self.bridge.send(A, to, text, "test only", **({"expects": "ack", **options}))

    def queue_argv(self):
        return ["/fixture/codex", "queue", "--thread", IDENT, "--message", PINNED]

    def open_argv(self):
        return ["/usr/bin/open", "-g", "codex://threads/wake/" + IDENT]


class WakeText(unittest.TestCase):
    def test_wake_text_is_pinned_byte_for_byte(self):
        self.assertEqual(codex_wake.WAKE_TEXT, PINNED)
        self.assertEqual(codex_wake.WAKE_TEXT.encode("utf-8"), PINNED.encode("utf-8"))
        self.assertEqual(len(codex_wake.WAKE_TEXT.encode("utf-8")), 735)
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

    def test_no_reply_is_saved_without_wake(self):
        fake = Fake()
        message = self.send(fake, expects="none")
        self.assertEqual(fake.calls, [])
        self.assertEqual(message["receipt"], "saved unread; not woken: no reply expected")
        self.assertNotIn("delivered", message["receipt"])
        self.assertIsNone(message["read_at"])
        self.assertEqual(self.bridge.get(message["id"])["receipt"], message["receipt"])
        self.assertEqual(self.bridge.inbox(C, unread=True)[0]["id"], message["id"])

    def test_reply_expected_still_wakes(self):
        fake = Fake()
        message = self.send(fake, expects="reply")
        self.assertEqual(fake.calls, [self.queue_argv(), self.open_argv()])
        self.assertEqual(message["receipt"], "saved unread; wake queued")

    def test_no_reply_then_reply_wakes_and_collects_both(self):
        fake = Fake()
        first = self.send(fake, "held", expects="none")
        second = self.send(fake, "request", expects="reply")
        self.assertEqual(fake.calls, [self.queue_argv(), self.open_argv()])
        self.assertFalse(second["wake"]["coalesced"])
        self.assertEqual({m["id"] for m in self.bridge.inbox(C, unread=True)},
                         {first["id"], second["id"]})

    def test_default_reply_to_chat_queues_wake(self):
        with patch.object(native, "claude_sessions", return_value=[{"id": A}]), \
             patch.object(native, "send_claude", return_value={"transport": "claude-native-inbox"}):
            question = self.bridge.send(C, A, "question", "test only", expects="reply")
        fake = Fake()
        with patch.object(codex_wake.subprocess, "run", side_effect=fake):
            reply = self.bridge.reply(question["id"], A, "answer")
        self.assertEqual(reply["expects"], "none")
        self.assertEqual(fake.calls, [self.queue_argv(), self.open_argv()])
        self.assertEqual(reply["receipt"], "saved unread; wake queued")

    def test_held_note_then_default_reply_wakes_and_collects_both(self):
        with patch.object(native, "claude_sessions", return_value=[{"id": A}]), \
             patch.object(native, "send_claude", return_value={"transport": "claude-native-inbox"}):
            question = self.bridge.send(C, A, "question", "test only", expects="reply")
        fake = Fake()
        note = self.send(fake, "held", expects="none")
        with patch.object(codex_wake.subprocess, "run", side_effect=fake):
            reply = self.bridge.reply(question["id"], A, "answer")
        self.assertEqual(fake.calls, [self.queue_argv(), self.open_argv()])
        self.assertEqual(reply["receipt"], "saved unread; wake queued")
        self.assertEqual({m["id"] for m in self.bridge.inbox(C, unread=True)},
                         {note["id"], reply["id"]})

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


class Ownership(Base):
    """Only the desktop app (or nothing) may hold the chat; anything else or any doubt refuses."""

    def setUp(self):
        super().setUp()
        self.patches[-1].stop()
        self.patches.pop()
        self.codex = tempfile.TemporaryDirectory()
        self.rollout = Path(self.codex.name, "sessions", "2026", "11", "20", "rollout-2026-11-20T00-00-00-" + IDENT + ".jsonl")
        self.rollout.parent.mkdir(parents=True)
        self.rollout.write_text("{}\n")
        self.more = [patch.dict(os.environ, {"CODEX_HOME": self.codex.name}),
                     patch.object(native, "codex_desktop_app", return_value={"path": APP, "version": "26.1117.31558"})]
        for item in self.more:
            item.start()
        # pid -> (parent, executable). 300-302 is the app's own chain, as observed on WH.
        self.table = {300: (301, APP + "/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"),
                      301: (302, APP + "/Contents/Frameworks/Codex Framework.framework/Helpers/Codex (Aperitif).app/Contents/MacOS/Codex (Aperitif)"),
                      302: (1, APP + "/Contents/MacOS/ChatGPT"),
                      400: (401, "/opt/homebrew/bin/codex"), 401: (402, "/bin/zsh"), 402: (1, "/Applications/Ghostty.app/Contents/MacOS/ghostty"),
                      500: (401, APP + "/Contents/Resources/codex-cli/bin/codex")}
        self.holders, self.lsof, self.listing = ["300"], None, ["1 /sbin/launchd"]

    def tearDown(self):
        for item in self.more:
            item.stop()
        self.codex.cleanup()
        super().tearDown()

    def run_fake(self, argv, **kwargs):
        if argv[0] == "/usr/sbin/lsof":
            self.assertEqual(argv, ["/usr/sbin/lsof", "-t", "--", str(self.rollout)])
            if isinstance(self.lsof, Exception):
                raise self.lsof
            code, out, err = self.lsof or ((0, "\n".join(self.holders) + "\n", "") if self.holders else (1, "", ""))
            return subprocess.CompletedProcess(argv, code, out, err)
        if argv[:2] == ["/bin/ps", "-axo"]:
            return subprocess.CompletedProcess(argv, 0, "\n".join(self.listing) + "\n", "")
        if argv[:3] == ["/bin/ps", "-o", "ppid=,comm="]:
            row = self.table.get(int(argv[-1]))
            return subprocess.CompletedProcess(argv, 0 if row else 1, "%d %s\n" % row if row else "", "")
        raise AssertionError("unexpected command " + repr(argv))

    def check(self):
        with patch.object(codex_wake.subprocess, "run", side_effect=self.run_fake):
            return codex_wake.ownership(IDENT)

    def refused(self, state, text):
        with self.assertRaises(codex_wake.NotWoken) as caught:
            self.check()
        self.assertEqual(caught.exception.state, state)
        self.assertIn(text, str(caught.exception))

    def test_the_apps_own_server_or_no_holder_is_allowed(self):
        self.assertEqual(self.check()["rollout_held_by_app"], [300])
        self.holders = []
        self.assertEqual(self.check()["rollout_held_by_app"], [])

    def test_a_terminal_or_embedded_server_is_refused(self):
        self.holders = ["300", "400"]
        self.refused("refused_owner", "/opt/homebrew/bin/codex")
        self.holders = ["500"]  # The app's bundled CLI, started from a terminal.
        self.refused("refused_owner", "outside the desktop app")
        self.holders = []
        self.listing = ["1 /sbin/launchd", "400 codex resume " + IDENT]  # Named but not yet holding the file.
        self.refused("refused_owner", "process 400")

    def test_doubt_refuses(self):
        self.holders = ["999"]  # Cannot be traced.
        self.refused("refused_uncertain", "could not trace")
        self.holders, self.lsof = ["300"], (1, "", "lsof: WARNING: can't stat()")
        self.refused("refused_uncertain", "no clear answer")
        self.lsof = subprocess.TimeoutExpired("lsof", 15)
        self.refused("refused_uncertain", "could not list")
        self.lsof = None
        self.table[302] = (77, APP + "/Contents/MacOS/ChatGPT")  # App main not started by launchd.
        self.refused("refused_owner", "outside the desktop app")

    def test_missing_duplicate_or_archived_chat_refuses(self):
        self.rollout.unlink()
        self.refused("refused_uncertain", "found 0 rollout files")
        self.rollout.write_text("{}\n")
        twin = self.rollout.parent / ("rollout-2026-11-20T00-00-01-" + IDENT + ".jsonl")
        twin.write_text("{}\n")
        self.refused("refused_uncertain", "found 2 rollout files")
        twin.unlink()
        archived = Path(self.codex.name, "archived_sessions", "rollout-x-" + IDENT + ".jsonl")
        archived.parent.mkdir()
        archived.write_text("{}\n")
        self.refused("refused_uncertain", "archived")

    def test_every_refusal_comes_before_the_queue(self):
        codex_wake.turn_on(self.bridge.db, C, None)
        for state, receipt in (("refused_owner", "not woken: a separate process owns this chat"),
                               ("refused_uncertain", "not woken: who owns this chat could not be verified")):
            fake = Fake()
            with patch.object(codex_wake, "ownership", side_effect=codex_wake.NotWoken(state, "fixture")):
                message = self.send(fake)
            self.assertEqual(fake.calls, [])  # Nothing queued, no link.
            self.assertEqual(message["receipt"], "saved unread; " + receipt)
            self.assertFalse(message["wake"]["detail"]["queue_attempted"])
            self.assertNotIn("wake queued", message["delivery_note"])

    def test_no_check_after_the_queue_can_claim_to_protect_the_chat(self):
        """The queued signal may run at once (the app consumes it before any link), so ownership is checked
        exactly once, before the queue, and nothing sits between the queue and the link."""
        codex_wake.turn_on(self.bridge.db, C, None)
        events, fake = [], Fake()

        def owner(ident):
            events.append("ownership")
            if len(events) > 1:  # A separate owner appears once the signal is queued.
                raise codex_wake.NotWoken("refused_owner", "late terminal")
            return {"app": APP}

        def run(argv, **kwargs):
            events.append(argv[1] if argv[0] == "/fixture/codex" else argv[0])
            return fake(argv, **kwargs)

        with patch.object(codex_wake, "ownership", side_effect=owner), \
             patch.object(codex_wake.subprocess, "run", side_effect=run):
            message = self.bridge.send(A, C, "fixture body", "test only", expects="ack")
        self.assertEqual(events, ["ownership", "queue", "/usr/bin/open"])
        self.assertEqual(fake.calls, [self.queue_argv(), self.open_argv()])
        self.assertEqual(message["wake"]["state"], "queued")
        detail = message["wake"]["detail"]
        self.assertLessEqual(detail["owner_checked_at"], detail["queue_returned_at"])
        self.assertNotIn("owner_before_link", detail)
        self.assertNotIn("blocked_after_queue", codex_wake.RECEIPTS)
        source = Path(codex_wake.__file__).read_text()
        body = source[source.index("def run_wake"):source.index("def after_unavailable")]
        self.assertEqual(body.count("ownership("), 1)
        self.assertLess(body.index("ownership("), body.index('"queue", "--thread"'))


class Concurrency(Base):
    def setUp(self):
        super().setUp()
        codex_wake.turn_on(self.bridge.db, C, None)

    def test_concurrent_sends_queue_one_wake_and_pending_is_not_reported_queued(self):
        release, calls, lock = threading.Event(), [], threading.Lock()
        def slow(argv, **kwargs):
            with lock:
                calls.append(list(argv))
            if argv[1:2] == ["queue"]:
                release.wait(10)
            return Fake()(argv)
        results, errors = {}, []
        def sender(n):
            try:
                with Bridge() as bridge:
                    results[n] = bridge.send(A, C, "concurrent " + str(n), "test only", expects="ack")
            except Exception as exc:
                errors.append(exc)
        with patch.object(codex_wake.subprocess, "run", side_effect=slow):
            first = threading.Thread(target=sender, args=(0,))
            first.start()
            for _ in range(500):
                if calls:
                    break
                time.sleep(.01)
            others = [threading.Thread(target=sender, args=(n,)) for n in range(1, 8)]
            for thread in others:
                thread.start()
            for thread in others:
                thread.join(20)
            # While the first wake is still being queued, others are not told it is queued.
            for n in range(1, 8):
                self.assertEqual(results[n]["receipt"], "saved unread; wake not yet confirmed")
                self.assertNotIn("wake queued", results[n]["delivery_note"])
            release.set()
            first.join(20)
        self.assertEqual(errors, [])
        self.assertEqual([c[0] if c[0] == "/usr/bin/open" else c[1] for c in calls], ["queue", "/usr/bin/open"])
        wakes = {results[n]["wake"]["id"] for n in results}
        self.assertEqual(len(wakes), 1)
        for n in range(8):
            self.assertEqual(self.bridge.get(results[n]["id"])["receipt"], "saved unread; wake queued")

    def test_no_message_joins_a_wake_whose_chat_already_read_past_it(self):
        reads, errors, stop = [], [], threading.Event()
        def sender(n):
            try:
                with Bridge() as bridge:
                    for i in range(6):
                        bridge.send(A, C, "m%d-%d" % (n, i), "test only", expects="ack")
            except Exception as exc:
                errors.append(exc)
        def reader():
            try:
                with Bridge() as bridge:
                    while not stop.is_set():
                        before = bridge.db.execute("SELECT read_at FROM inbox_reads WHERE chat=?", (C,)).fetchone()
                        ids = {row["id"] for row in bridge.inbox(C)}
                        after = bridge.db.execute("SELECT read_at FROM inbox_reads WHERE chat=?", (C,)).fetchone()
                        if after and after != before:
                            reads.append((after[0], ids))
                        time.sleep(.01)
            except Exception as exc:
                errors.append(exc)
        with patch.object(codex_wake.subprocess, "run", side_effect=Fake()):
            watcher = threading.Thread(target=reader)
            watcher.start()
            senders = [threading.Thread(target=sender, args=(n,)) for n in range(4)]
            for thread in senders:
                thread.start()
            for thread in senders:
                thread.join(60)
            stop.set()
            watcher.join(20)
        self.assertEqual(errors, [])
        self.assertTrue(reads)
        links = self.bridge.db.execute("SELECT w.message_id,w.coalesced,s.created FROM message_wakes w "
                                       "JOIN wake_signals s ON s.id=w.wake_id").fetchall()
        self.assertEqual(len(links), 24)
        self.assertTrue(any(row[1] for row in links) and not all(row[1] for row in links))
        # A message joined an earlier wake only if every inbox read recorded after that wake saw it.
        for message_id, coalesced, created in links:
            if coalesced:
                for read_at, ids in reads:
                    if read_at >= created:
                        self.assertIn(message_id, ids)


class Inbox(Base):
    def test_one_read_returns_every_unread_message_with_no_cap(self):
        with patch.object(codex_wake.subprocess, "run", side_effect=Fake()):
            for n in range(130):
                self.bridge.send(A, C, "bulk %d" % n, "test only", expects="none")
        rows = self.bridge.inbox(C)
        self.assertEqual(len(rows), 130)  # Not cut at 100 while any is unread.
        self.assertIsNotNone(self.bridge.db.execute("SELECT 1 FROM inbox_reads WHERE chat=?", (C,)).fetchone())

    def test_more_than_500_unread_drain_in_one_read_without_a_new_message(self):
        codex_wake.turn_on(self.bridge.db, C, None)
        fake = Fake()
        first = self.send(fake, "first")
        with patch.object(codex_wake.subprocess, "run", side_effect=fake):
            for n in range(700):
                self.bridge.send(A, C, "bulk %d" % n, "test only", expects="none")
        self.assertEqual(fake.calls, [self.queue_argv(), self.open_argv()])  # One wake covers all 701.
        rows = self.bridge.inbox(C, unread=True)
        self.assertEqual(len(rows), 701)
        self.assertEqual({row["body"] for row in rows}, {"first"} | {"bulk %d" % n for n in range(700)})
        for row in rows:
            self.bridge.ack(row["id"], C)
        self.assertEqual(self.bridge.inbox(C, unread=True), [])  # Drained; nothing waits for another message.
        self.assertEqual(len(fake.calls), 2)
        self.assertNotEqual(self.send(fake, "next")["wake"]["id"], first["wake"]["id"])  # The read ended the wake.

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
