"""Exercise the local requester entry point and fixture receiver, without agents."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest

from agentpeers.personal.app import App
from agentpeers.personal.config import Rejected
from agentpeers.personal.context import ContextProtocol, ContextRequests, Grant, Receiver, Requester
from agentpeers.personal.store import Store


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "context.sqlite3"
        self.now = 1000.0
        self.requester = Requester("owner-a", "subject-a", "chat-a")
        self.receiver = Receiver("owner-a", "dot-fixture")
        self.grants = (
            Grant("owner-a", "subject-a", "Dot", "dot-fixture", "project-status", "Answer factual project status only"),
            Grant("owner-a", "subject-a", "Marketing", "codex:exact-fixture-session", "project-status", "Answer factual project status only"),
            Grant("owner-a", "subject-b", "Dot", "dot-fixture", "project-status", "Answer factual project status only"),
            Grant("owner-b", "subject-a", "Dot", "dot-fixture", "project-status", "Answer factual project status only"),
        )
        self.requests = self.open()
        self.protocol = ContextProtocol(self.requests)
        self.sources = [{"reference": "fixture:project-status@revision-7", "observed_at": 990}]

    def open(self, **kwargs):
        return ContextRequests(self.path, self.grants, clock=lambda: self.now, **kwargs)

    def tearDown(self):
        self.requests.close()
        self.directory.cleanup()

    def submit(self, requester=None, **changes):
        arguments = {"request_id": "question-1", "destination": "Dot", "topic": "project-status", "question": "What is the verified status?"}
        arguments.update(changes)
        return self.protocol.call(requester or self.requester, "request_context", arguments)

    def result(self, requester=None, request_id="question-1"):
        return self.protocol.call(requester or self.requester, "context_result", {"request_id": request_id})

    def claim(self, receiver=None):
        receiver = receiver or self.receiver
        delivery_id = self.requests.pending(receiver)[0]
        return delivery_id, self.requests.claim(receiver, delivery_id)

    def test_requester_to_dot_fixture_returns_one_sourced_answer(self):
        submitted = self.submit()
        self.assertEqual(submitted["state"], "pending")
        self.assertNotIn("answer", submitted)
        delivery_id, envelope = self.claim()
        self.assertEqual(envelope["requester"], {"role": "chatgpt_requester", "owner": "owner-a", "subject": "subject-a", "session": "chat-a"})
        self.assertEqual(envelope["question"], "What is the verified status?")
        self.assertEqual(envelope["claimed_scope"], "Answer factual project status only")
        self.assertEqual(self.result()["state"], "processing")
        self.requests.answer(self.receiver, delivery_id, "The fixture project passed its review.", self.sources)
        result = self.result()
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["answer"], "The fixture project passed its review.")
        self.assertEqual(result["sources"], self.sources)
        self.assertEqual(result["responding_agent"], "dot-fixture")
        self.assertEqual(result["completed_at"], 1000)
        for field in ("owner", "subject", "session", "question", "delivery_id"):
            self.assertNotIn(field, result)

    def test_named_codex_destination_is_pinned_to_exact_receiver(self):
        self.submit(destination="Marketing")
        codex = Receiver("owner-a", "codex:exact-fixture-session")
        self.assertEqual(self.requests.pending(self.receiver), [])
        delivery_id, envelope = self.claim(codex)
        self.assertEqual(envelope["destination"], "Marketing")
        with self.assertRaises(Rejected):
            self.requests.answer(self.receiver, delivery_id, "Wrong receiver", self.sources)
        self.requests.answer(codex, delivery_id, "Fixture launch date is recorded.", self.sources)
        self.assertEqual(self.result()["responding_agent"], codex.peer)

    def test_owner_subject_and_session_isolation_even_with_known_id(self):
        self.submit()
        for identity in (Requester("owner-b", "subject-a", "chat-a"), Requester("owner-a", "subject-b", "chat-a"), Requester("owner-a", "subject-a", "chat-b")):
            with self.assertRaises(Rejected):
                self.result(identity)
            self.submit(identity)
            self.assertEqual(self.result(identity)["state"], "pending")
        self.assertEqual(self.requests.db.execute("SELECT count(*) FROM context_requests").fetchone()[0], 4)

    def test_missing_identity_and_receiver_impersonation_are_rejected(self):
        for principal in (None, "dot", self.receiver, Requester("", "subject-a", "chat-a"), Requester("owner-a", "", "chat-a"), Requester("owner-a", "subject-a", "")):
            with self.assertRaises(Rejected):
                self.protocol.call(principal, "request_context", {"request_id": "missing", "destination": "Dot", "topic": "project-status", "question": "Status?"})
        for field in ("owner", "subject", "session", "sender", "receiver", "_meta"):
            with self.assertRaises(Rejected):
                self.submit(**{field: "dot"})

    def test_requester_cannot_call_inbox_answer_or_receiver_methods(self):
        for name in ("inbox", "reply", "answer", "claim", "pending", "send_message"):
            with self.assertRaises(Rejected):
                self.protocol.call(self.requester, name, {})

    def test_exact_destination_and_topic_grants_are_required(self):
        for changes in ({"destination": "Grokbot"}, {"destination": "dot"}, {"topic": "secrets"}, {"destination": "https://arbitrary.example"}):
            with self.assertRaises(Rejected):
                self.submit(**changes)
        empty = ContextRequests(Path(self.directory.name) / "unapproved.sqlite3")
        try:
            with self.assertRaises(Rejected):
                ContextProtocol(empty).call(self.requester, "request_context", {"request_id": "x", "destination": "Dot", "topic": "project-status", "question": "status?"})
        finally:
            empty.close()

    def test_duplicate_request_never_creates_or_reassigns_work(self):
        self.submit()
        self.assertTrue(self.submit()["duplicate"])
        for changes in ({"question": "A changed question"}, {"destination": "Marketing"}):
            with self.assertRaises(Rejected):
                self.submit(**changes)
        delivery_id, _ = self.claim()
        self.assertEqual(self.submit()["state"], "processing")
        self.assertFalse(self.requests.claim(self.receiver, delivery_id)["claimed"])
        self.assertEqual(self.requests.db.execute("SELECT count(*) FROM context_requests").fetchone()[0], 1)

    def test_concurrent_duplicate_submission_and_claim(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            responses = list(executor.map(lambda _: self.submit(), range(8)))
        self.assertEqual(sum(not r["duplicate"] for r in responses), 1)
        delivery_id = self.requests.pending(self.receiver)[0]
        with ThreadPoolExecutor(max_workers=8) as executor:
            claimed = list(executor.map(lambda _: self.requests.claim(self.receiver, delivery_id), range(8)))
        self.assertEqual(sum(r["claimed"] for r in claimed), 1)

    def test_two_store_instances_suppress_duplicate_work(self):
        second = self.open()
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                calls = [lambda: self.submit(), lambda: ContextProtocol(second).call(self.requester, "request_context", {"request_id": "question-1", "destination": "Dot", "topic": "project-status", "question": "What is the verified status?"})]
                responses = list(executor.map(lambda call: call(), calls))
            self.assertEqual(sum(not r["duplicate"] for r in responses), 1)
            delivery_id = self.requests.pending(self.receiver)[0]
            with ThreadPoolExecutor(max_workers=2) as executor:
                claimed = list(executor.map(lambda store: store.claim(self.receiver, delivery_id), [self.requests, second]))
            self.assertEqual(sum(r["claimed"] for r in claimed), 1)
        finally:
            second.close()

    def test_timeout_does_not_extend_on_retry_or_reclaim(self):
        original = self.submit()
        self.now = original["expires"]
        self.assertEqual(self.result()["state"], "expired")
        self.assertEqual(self.submit()["expires"], original["expires"])
        self.assertEqual(self.submit()["state"], "expired")
        self.assertEqual(self.requests.pending(self.receiver), [])

    def test_processing_timeout_rejects_late_answer(self):
        self.submit()
        delivery_id, _ = self.claim()
        self.now += 300
        with self.assertRaises(Rejected):
            self.requests.answer(self.receiver, delivery_id, "Late", self.sources)
        self.assertEqual(self.result()["state"], "expired")
        self.assertFalse(self.requests.claim(self.receiver, delivery_id)["claimed"])

    def test_answer_requires_claim_and_sources(self):
        self.submit()
        delivery_id = self.requests.pending(self.receiver)[0]
        with self.assertRaises(Rejected):
            self.requests.answer(self.receiver, delivery_id, "Unclaimed", self.sources)
        self.claim()
        for sources in ([], [{"reference": "fixture:x"}], [{"reference": "fixture:x", "observed_at": float("nan")}], [{"reference": "fixture:x", "observed_at": True}], [{"reference": "fixture:x", "observed_at": 1001}], [{"reference": "", "observed_at": 999}]):
            with self.assertRaises(Rejected):
                self.requests.answer(self.receiver, delivery_id, "Answer", sources)
        self.assertEqual(self.result()["state"], "processing")

    def test_answer_retry_is_identical_and_cannot_overwrite(self):
        self.submit()
        delivery_id, _ = self.claim()
        self.requests.answer(self.receiver, delivery_id, "Answer", self.sources)
        self.assertTrue(self.requests.answer(self.receiver, delivery_id, "Answer", self.sources)["duplicate"])
        with self.assertRaises(Rejected):
            self.requests.answer(self.receiver, delivery_id, "Different answer", self.sources)
        self.now += 1000
        self.assertEqual(self.result()["answer"], "Answer")

    def test_two_connections_cannot_publish_conflicting_answers(self):
        self.submit()
        delivery_id, _ = self.claim()
        second = self.open()
        def answer(pair):
            store, body = pair
            try:
                store.answer(self.receiver, delivery_id, body, self.sources)
                return body
            except Rejected:
                return None
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                answers = list(executor.map(answer, [(self.requests, "First answer"), (second, "Second answer")]))
            published = [body for body in answers if body]
            self.assertEqual(len(published), 1)
            self.assertEqual(self.result()["answer"], published[0])
        finally:
            second.close()

    def test_wrong_owner_or_replacement_receiver_cannot_claim(self):
        self.submit()
        delivery_id = self.requests.pending(self.receiver)[0]
        for receiver in (Receiver("owner-b", "dot-fixture"), Receiver("owner-a", "replacement"), self.requester):
            with self.assertRaises(Rejected):
                self.requests.claim(receiver, delivery_id)

    def test_failure_is_terminal_and_cannot_be_retried_as_new_work(self):
        self.submit()
        delivery_id, _ = self.claim()
        self.requests.fail(self.receiver, delivery_id, "blocked")
        self.assertEqual(self.result()["reason"], "blocked")
        self.assertEqual(self.submit()["state"], "failed")
        self.assertTrue(self.requests.fail(self.receiver, delivery_id, "blocked")["duplicate"])
        with self.assertRaises(Rejected):
            self.requests.answer(self.receiver, delivery_id, "Bypass failure", self.sources)

    def test_restart_preserves_processing_and_does_not_replay(self):
        self.submit()
        delivery_id, _ = self.claim()
        self.requests.close()
        self.requests = self.open()
        self.protocol = ContextProtocol(self.requests)
        self.assertEqual(self.result()["state"], "processing")
        self.assertEqual(self.requests.pending(self.receiver), [])
        self.assertFalse(self.requests.claim(self.receiver, delivery_id)["claimed"])
        self.requests.answer(self.receiver, delivery_id, "After restart", self.sources)
        self.assertEqual(self.result()["answer"], "After restart")

    def test_changed_grant_does_not_reassign_existing_request(self):
        self.submit()
        self.requests.close()
        changed = (Grant("owner-a", "subject-a", "Dot", "replacement", "project-status", "Answer factual project status only"),)
        self.requests = ContextRequests(self.path, changed, clock=lambda: self.now)
        self.protocol = ContextProtocol(self.requests)
        with self.assertRaises(Rejected):
            self.result()
        with self.assertRaises(Rejected):
            self.submit()

    def test_capacity_preserves_requests_and_allows_exact_retry(self):
        self.requests.close()
        self.requests = self.open(capacity=1)
        self.protocol = ContextProtocol(self.requests)
        self.submit()
        self.assertTrue(self.submit()["duplicate"])
        with self.assertRaises(Rejected):
            self.submit(request_id="question-2")
        self.assertEqual(self.result()["state"], "pending")

    def test_ambiguous_grant_refused(self):
        with self.assertRaises(Rejected):
            ContextRequests(self.path, (self.grants[0], self.grants[0]))

    def test_foreign_database_refused_without_changes(self):
        foreign = Path(self.directory.name) / "foreign.sqlite3"
        db = sqlite3.connect(foreign)
        db.execute("CREATE TABLE valuable(value TEXT)")
        db.execute("INSERT INTO valuable VALUES('preserve')")
        db.commit()
        db.close()
        before = foreign.read_bytes()
        with self.assertRaises(Rejected):
            ContextRequests(foreign, self.grants)
        self.assertEqual(foreign.read_bytes(), before)

    def test_existing_personal_mailbox_is_refused_and_preserved(self):
        path = Path(self.directory.name) / "personal.sqlite3"
        store = Store(path, {"peers": {}, "connections": []})
        store.close()
        before = path.read_bytes()
        with self.assertRaises(Rejected):
            ContextRequests(path, self.grants)
        self.assertEqual(path.read_bytes(), before)

    def test_live_personal_app_does_not_advertise_or_accept_context_tools(self):
        store = Store(Path(self.directory.name) / "mailbox.sqlite3", {"peers": {}, "connections": []})
        try:
            app = App(store, post=lambda *args: self.fail("Unexpected network call"))
            listed = app.rpc("dot", {"id": 1, "method": "tools/list"})
            self.assertNotIn("request_context", [t["name"] for t in listed["result"]["tools"]])
            rejected = app.rpc("dot", {"id": 2, "method": "tools/call", "params": {"name": "request_context", "arguments": {}, "_meta": {"openai/session": "chat-a", "openai/subject": "subject-a"}}})
            self.assertTrue(rejected["result"]["isError"])
            self.assertEqual(store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
