"""Named route contracts; fixtures never contact live agent sessions."""
import os
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from unittest.mock import patch

from agentpeers import native
from agentpeers.core import Bridge
from agentpeers.mcp import dispatch
from agentpeers.setup import start_probe

C = 'codex:' + str(uuid.uuid4())
C2 = 'codex:' + str(uuid.uuid4())
A = 'claude:' + str(uuid.uuid4())
A2 = 'claude:' + str(uuid.uuid4())
SCOPE = 'Human authorized a bounded name-addressing test'


def session(ident, name, surface='shared-server'):
    return dict(id=ident, runtime=ident.split(':')[0], name=name,
                name_address=name, surface=surface, cwd='/fixture')



# Legacy journeys stub the new read-only probe rather than contacting real agents.
def setUpModule():
    global _return_route_fixture
    _return_route_fixture = patch.object(native, "receive_route", return_value={
        "status": "available", "transport": "fixture", "checked_at": 1,
        "proof": "fixture candidate only"})
    _return_route_fixture.start()


def tearDownModule():
    _return_route_fixture.stop()


class NamedJourney(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pb-names-')
        self.env = patch.dict(os.environ, {'AGENTPEERS_HOME': self.temp.name})
        self.env.start()
        self.codex = [session(C, 'builder')]
        self.claude = [session(A, 'reviewer')]
        self.patches = [patch.object(native, 'codex_sessions', side_effect=lambda: self.codex),
                        patch.object(native, 'claude_sessions', side_effect=lambda: self.claude),
                        patch.object(native, 'send_codex', return_value={'transport':'fixture'}),
                        patch.object(native, 'send_claude', return_value={'transport':'fixture'})]
        self.mocks = [p.start() for p in self.patches]
        self.bridge = Bridge()

    def tearDown(self):
        self.bridge.db.close()
        for p in reversed(self.patches): p.stop()
        self.env.stop()
        self.temp.cleanup()

    @contextmanager
    def desktop_connect_error(self, error, app=None):
        with patch.object(native, 'owned_path'), patch.object(native.socket, 'socket') as socket, \
             patch.object(native, 'codex_desktop_app', return_value=app):
            socket.return_value.connect.side_effect = error
            yield socket.return_value
            socket.return_value.sendall.assert_not_called()

    def test_unverified_unique_name_offers_exact_id_for_new_send(self):
        self.codex[:] = [session(C, 'builder', 'desktop-or-stored')]
        with self.desktop_connect_error(ConnectionRefusedError('fixture')):
            row = dispatch('send_message', {'to': 'builder', 'message': 'check', 'scope': SCOPE}, actor=A)
        self.assertEqual(row['transport_status'], 'unavailable')
        self.assertIsNone(row['recipient'])
        self.assertFalse(row['mailbox_available'])
        self.assertFalse(row['detail']['queued'])
        self.assertIn('No recipient mailbox assigned; nothing was saved for delivery', row['destination_note'])
        self.assertIn('For a new send, you may explicitly choose the full session ID ' + C, row['destination_note'])
        self.assertEqual(self.bridge.inbox(C), [])
        self.mocks[2].assert_not_called()

    def test_unverified_duplicate_name_offers_no_unique_id(self):
        self.codex[:] = [session(C, 'builder', 'desktop-or-stored'), session(C2, 'builder')]
        with self.desktop_connect_error(ConnectionRefusedError('fixture')):
            row = self.bridge.send(A, 'builder', 'check', SCOPE)
        self.assertFalse(row['mailbox_available'])
        self.assertIn('nothing was saved for delivery', row['destination_note'])
        self.assertNotIn('full session ID', row['destination_note'])
        self.assertNotIn('exact_candidate_id', row['detail']['route_failure'])
        self.mocks[2].assert_not_called()

    def test_named_reply_is_saved_and_pinned_during_desktop_connection_outage(self):
        from agentpeers import codex_wake
        codex_wake.turn_on(self.bridge.db, C)
        inspected = {'version': '26.1117.31558', 'path': '/fixture/ChatGPT.app'}
        for error, app, code in ((ConnectionRefusedError('fixture'), None, 'desktop_connection_unavailable'),
                                 (FileNotFoundError('fixture'), inspected, 'desktop_ipc_unsupported')):
            with self.subTest(code=code):
                self.codex[:] = [session(C, 'builder')]
                request = self.bridge.send(C, 'reviewer', 'check', SCOPE, decision_id='fixture decision')
                self.codex[:] = [session(C, 'builder', 'desktop-or-stored')]
                with self.desktop_connect_error(error, app) as socket, \
                     patch.object(codex_wake, 'run_wake', return_value=('queued', {})) as wake:
                    reply = dispatch('reply', {'id': request['id'], 'message': 'done'}, actor=A)
                    socket.connect.assert_called_once()
                    wake.assert_called_once_with(C)

                self.assertEqual(reply['requested_to'], 'builder')
                self.assertEqual(reply['recipient'], C)
                self.assertIsNone(reply['recipient_name'])
                self.assertEqual(reply['reply_to'], request['id'])
                self.assertEqual(reply['scope'], SCOPE)
                self.assertEqual(reply['decision_id'], 'fixture decision')
                self.assertEqual(reply['replacement'], {})
                self.assertEqual(reply['delivery'], 'unavailable')
                self.assertEqual(reply['transport_status'], 'unavailable')
                self.assertTrue(reply['mailbox_available'])
                self.assertEqual(reply['detail']['route_failure']['code'], code)
                self.assertEqual(reply['wake']['state'], 'queued')
                self.assertEqual(reply['receipt'], 'saved unread; wake queued')
                parent = self.bridge.get(request['id'])
                self.assertEqual(parent['read_by'], A)
                self.assertEqual(parent['replies'][0]['id'], reply['id'])

                self.codex[:] = [session(C2, 'builder')]
                self.assertEqual(self.bridge.inbox(C2), [])
                with self.assertRaises(ValueError):
                    self.bridge.ack(reply['id'], C2)
                self.assertEqual(self.bridge.inbox(C, unread=True)[0]['id'], reply['id'])
                read = self.bridge.ack(reply['id'], C)
                self.assertEqual(read['delivery'], 'read')
                self.assertEqual(read['transport_status'], 'unavailable')
                self.assertIn('Read from the mailbox', read['delivery_note'])
        self.mocks[2].assert_not_called()

    def test_named_reply_outage_does_not_override_duplicates_or_replacements(self):
        request = self.bridge.send(C, 'reviewer', 'check', SCOPE)
        original = session(C, 'builder', 'desktop-or-stored')
        replacement = session(C2, 'builder', 'desktop-or-stored')
        for rows in ([original, replacement], [replacement]):
            with self.subTest(rows=rows), self.desktop_connect_error(ConnectionRefusedError('fixture')):
                self.codex[:] = rows
                reply = self.bridge.reply(request['id'], A, 'done')
                self.assertFalse(reply['mailbox_available'])
                self.assertIsNone(reply['recipient'])
        self.assertEqual(self.bridge.inbox(C), [])
        self.mocks[2].assert_not_called()

    def test_named_reply_requires_complete_inventory(self):
        request = self.bridge.send(C, 'reviewer', 'check', SCOPE)
        self.mocks[0].side_effect = native.Unavailable('fixture discovery timed out', code='codex_discovery_timeout')
        with patch.object(native, 'Desktop') as desktop:
            send = self.bridge.send(A, 'reviewer', 'check', SCOPE)
            reply = self.bridge.reply(request['id'], A, 'done')
            desktop.assert_not_called()
        for row in (send, reply):
            self.assertFalse(row['mailbox_available'])
            self.assertNotIn('full session ID', row['destination_note'])
        self.mocks[2].assert_not_called()

    def test_named_reply_does_not_recover_from_other_desktop_failures(self):
        request = self.bridge.send(C, 'reviewer', 'check', SCOPE)
        self.codex[:] = [session(C, 'builder', 'desktop-or-stored')]
        for error in (TimeoutError('connect timed out'), PermissionError('connect denied'), OSError('generic')):
            with self.subTest(error=error), self.desktop_connect_error(error):
                self.assertFalse(self.bridge.reply(request['id'], A, 'done')['mailbox_available'])
        for error in (ValueError('wrong socket owner'), native.Unavailable('no owner', code='desktop_owner_missing'),
                      native.Unavailable('owner timed out', code='desktop_owner_discovery_failed')):
            with self.subTest(error=error), patch.object(native, 'Desktop') as desktop, \
                 patch.object(native, 'desktop_wake_enabled', return_value=False):
                desktop.return_value.__enter__.side_effect = error
                self.assertFalse(self.bridge.reply(request['id'], A, 'done')['mailbox_available'])
        self.mocks[2].assert_not_called()

    def test_unique_name_records_logical_and_actual_addresses_in_mcp(self):
        row = dispatch('send_message', {'to':'reviewer', 'message':'check', 'scope':SCOPE}, actor=C)
        self.assertEqual(row['requested_to'], 'reviewer')
        self.assertEqual(row['recipient'], A)
        self.assertEqual(row['sender_name'], 'builder')
        self.assertEqual(row['delivery'], 'submitted')
        target, envelope = self.mocks[3].call_args.args
        self.assertEqual(target['id'], A)
        self.assertEqual(envelope['to_name'], 'reviewer')
        self.assertEqual(envelope['from_name'], 'builder')
        self.assertIsNone(row['read_by'])

    def test_reply_follows_sender_name_across_runtime_replacement(self):
        row = self.bridge.send(C, 'reviewer', 'check', SCOPE)
        # The logical sender moves from Codex to Claude; old UUID is gone.
        self.codex[:] = []
        self.claude.append(session(A2, 'builder'))
        reply = self.bridge.reply(row['id'], A, 'done')
        self.assertEqual(reply['recipient'], A2)
        self.assertEqual(reply['requested_to'], 'builder')
        self.assertEqual(reply['scope'], SCOPE)
        self.assertEqual(reply['replacement']['recipient']['previous_session'], C)
        self.assertEqual(reply['replacement']['recipient']['current_session'], A2)
        self.assertEqual(self.bridge.get(row['id'])['recipient'], A)
        self.assertEqual(self.bridge.get(row['id'])['read_by'], A)
        self.assertEqual(self.mocks[3].call_args.args[0]['id'], A2)

    def test_replacement_recipient_can_consume_named_external_mailbox(self):
        row = self.bridge.send(C, 'reviewer', 'check', SCOPE)
        self.claude[:] = [session(A2, 'reviewer')]
        self.assertEqual(self.bridge.inbox(A2)[0]['id'], row['id'])
        self.assertIsNone(self.bridge.get(row['id'])['read_at'])
        read = self.bridge.ack(row['id'], A2)
        self.assertEqual(read['recipient'], A)
        self.assertEqual(read['read_by'], A2)
        self.assertEqual(read['replacement']['reader']['previous_session'], A)
        reply = self.bridge.reply(row['id'], A2, 'new session consumed')
        self.assertEqual(reply['sender'], A2)
        self.assertEqual(reply['replacement']['sender']['previous_session'], A)
        self.assertEqual(reply['recipient'], C)
        # First consumption remains auditable even after another ack.
        self.assertEqual(self.bridge.ack(row['id'], A2)['read_by'], A2)

    def test_duplicate_across_runtimes_refuses_without_payload(self):
        self.codex.append(session(C2, 'reviewer'))
        row = self.bridge.send(C, 'reviewer', 'check', SCOPE)
        self.assertEqual(row['status'], 'unavailable')
        self.assertIsNone(row['recipient'])
        self.assertIn(A, row['detail']['reason'])
        self.assertIn(C2, row['detail']['reason'])
        self.mocks[2].assert_not_called()
        self.mocks[3].assert_not_called()

    def test_ambiguous_replacement_cannot_ack_or_read_alias_mailbox(self):
        row = self.bridge.send(C, 'reviewer', 'check', SCOPE)
        self.claude[:] = [session(A2, 'reviewer')]
        self.codex.append(session(C2, 'reviewer'))
        self.assertEqual(self.bridge.inbox(A2), [])
        with self.assertRaises(native.Unavailable): self.bridge.ack(row['id'], A2)
        self.assertIsNone(self.bridge.get(row['id'])['read_at'])

    def test_offline_name_is_not_queued_or_retargeted_on_id_reuse(self):
        self.claude[:] = []
        ident = str(uuid.uuid4())
        row = self.bridge.send(C, 'reviewer', 'check', SCOPE, ident=ident)
        self.assertEqual(row['status'], 'unavailable')
        self.assertFalse(row['detail']['queued'])
        self.claude.append(session(A2, 'reviewer'))
        self.assertEqual(self.bridge.inbox(A2), [])
        with self.assertRaises(ValueError): self.bridge.ack(row['id'], A2)
        reused = self.bridge.send(C, 'reviewer', 'check', SCOPE, ident=ident)
        self.assertTrue(reused['reused_id'])
        self.assertFalse(reused['submission_attempted'])
        self.assertIsNone(reused['recipient'])
        fresh = self.bridge.send(C, 'reviewer', 'new request', SCOPE)
        self.assertEqual(fresh['recipient'], A2)
        self.assertEqual(self.mocks[3].call_count, 1)

    def test_explicit_uuid_conversation_stays_pinned(self):
        row = self.bridge.send(C, A, 'check', SCOPE)
        self.codex[:] = [session(C2, 'builder')]
        reply = self.bridge.reply(row['id'], A, 'done')
        self.assertEqual(reply['recipient'], C)
        self.assertIsNone(row['sender_name'])
        self.assertEqual(self.mocks[2].call_args.args[0]['id'], C)
        with self.assertRaises(ValueError): self.bridge.reply(row['id'], A2, 'not addressed')

    def test_discovery_failure_does_not_assume_other_runtime_has_no_duplicates(self):
        self.mocks[0].side_effect = native.Unavailable('offline discovery')
        row = self.bridge.send(C, 'reviewer', 'check', SCOPE)
        self.assertEqual(row['status'], 'unavailable')
        self.assertIn('discovery', row['detail']['reason'])
        self.mocks[3].assert_not_called()

    def test_stored_codex_duplicate_without_owner_does_not_shadow_live_claude(self):
        self.codex.append(session(C2, 'reviewer', 'desktop-or-stored'))
        with patch.object(native.Desktop, '__enter__', return_value=native.Desktop()), \
             patch.object(native.Desktop, '__exit__', return_value=None), \
             patch.object(native.Desktop, 'owner', side_effect=native.Unavailable('no owner', code='desktop_owner_missing')):
            row = self.bridge.send(C, 'reviewer', 'check', SCOPE)
        self.assertEqual(row['recipient'], A)

    def test_live_desktop_duplicate_is_counted(self):
        self.codex.append(session(C2, 'reviewer', 'desktop-or-stored'))
        with patch.object(native.Desktop, '__enter__', return_value=native.Desktop()), \
             patch.object(native.Desktop, '__exit__', return_value=None), \
             patch.object(native.Desktop, 'owner', return_value='fixture-owner'):
            row = self.bridge.send(C, 'reviewer', 'check', SCOPE)
        self.assertEqual(row['status'], 'unavailable')
        self.mocks[3].assert_not_called()

    def test_name_is_exact_and_display_preview_is_not_a_name(self):
        row = self.bridge.send(C, 'Reviewer', 'case differs', SCOPE)
        self.assertEqual(row['status'], 'unavailable')
        self.claude[0]['name_address'] = None
        row = self.bridge.send(C, 'reviewer', 'display fallback only', SCOPE)
        self.assertEqual(row['status'], 'unavailable')

    def test_named_metadata_survives_reopen_and_old_message_table_stays_compatible(self):
        row = self.bridge.send(C, 'reviewer', 'check', SCOPE)
        with Bridge() as other:
            self.assertEqual(other.get(row['id'])['sender_name'], 'builder')
            self.assertEqual(len(other.db.execute('PRAGMA table_info(messages)').fetchall()), 10)
            legacy = str(uuid.uuid4())
            other.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,NULL,NULL)',
                             (legacy,C,A,'old process',SCOPE,None,1,'submitted'))
            other.db.commit()
            old = other.get(legacy)
            self.assertIsNone(old['recipient_name'])
            self.assertEqual(old['requested_to'], A)
            self.assertEqual(other.ack(legacy,A)['read_by'], A)

    def test_uncertain_named_send_is_not_replayed_to_replacement(self):
        ident = str(uuid.uuid4())
        self.mocks[3].side_effect = native.Unconfirmed('lost response')
        first = self.bridge.send(C, 'reviewer', 'check', SCOPE, ident=ident)
        self.claude[:] = [session(A2,'reviewer')]
        reused = self.bridge.send(C, 'reviewer', 'check', SCOPE, ident=ident)
        self.assertEqual(first['status'], 'unconfirmed')
        self.assertEqual(reused['recipient'], A)
        self.assertEqual(self.mocks[3].call_count, 1)

    def test_stale_uuid_reports_last_observed_name_and_replacement_without_forwarding(self):
        self.bridge.send(C, 'reviewer', 'establish observed native name', SCOPE)
        self.claude[:] = [session(A2, 'reviewer')]
        self.mocks[3].reset_mock()
        row = self.bridge.send(C, A, 'explicit old ID', SCOPE)
        self.assertEqual(row['status'], 'unavailable')
        self.assertEqual(row['recipient'], A)
        self.assertEqual(row['detail']['last_known_name'], 'reviewer')
        self.assertEqual(row['detail']['current_name_holder'], A2)
        self.assertFalse(row['detail']['automatically_forwarded'])
        self.assertIsNone(row['read_at'])
        self.mocks[3].assert_not_called()

    def test_reply_chain_lookup_is_bounded(self):
        row = self.bridge.send(C, 'reviewer', 'start', SCOPE)
        for i in range(40):
            actor = A if i % 2 == 0 else C
            row = self.bridge.reply(row['id'], actor, 'turn ' + str(i))
        self.assertEqual(row['body'], 'turn 39')

    def test_reply_scope_and_route_cannot_be_overridden(self):
        row = self.bridge.send(C, 'reviewer', 'check', SCOPE)
        with self.assertRaises(ValueError): self.bridge.send(A, 'builder', 'reply', 'broader scope', reply_to=row['id'])
        with self.assertRaises(ValueError): self.bridge.send(A, A2, 'reply', SCOPE, reply_to=row['id'])

    def test_setup_rejects_same_runtime_name_before_sending(self):
        with self.assertRaises(ValueError): start_probe('builder', SCOPE, C)
        self.assertEqual(self.bridge.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)


if __name__ == '__main__': unittest.main()
