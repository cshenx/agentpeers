"""Incident regressions: a reachable app is not a receiving chat."""
import os
import tempfile
import unittest
from unittest.mock import patch
import uuid

from agentpeers import native
from agentpeers.addressing import resolve_name
from agentpeers.core import Bridge
from agentpeers.setup import doctor

C = 'codex:' + str(uuid.uuid4())
A = 'claude:' + str(uuid.uuid4())


class AvailabilityJourney(unittest.TestCase):
    def missing_owner(self):
        desktop = native.Desktop()
        with patch.object(desktop, 'call', return_value={'resultType': 'error'}):
            with self.assertRaises(native.Unavailable) as failed:
                desktop.owner(C.split(':')[1])
        return failed.exception

    def test_connected_desktop_without_chat_owner_is_not_app_closed(self):
        error = self.missing_owner()
        self.assertEqual(error.diagnostics, {'code': 'desktop_owner_missing',
                         'desktop_connected': True, 'desktop_owner_found': False})
        self.assertIn('every saved chat', str(error))
        with patch.object(native, 'SharedServer') as shared, patch.object(native, 'Desktop') as desktop:
            client = shared.return_value
            client.call.return_value = {'data': []}
            desktop.return_value.__enter__.return_value.send.side_effect = error
            with self.assertRaises(native.Unavailable) as failed:
                native.send_codex({'id': C}, {'body': 'fixture'})
            self.assertIs(failed.exception.diagnostics['shared_target_loaded'], False)
            self.assertTrue(failed.exception.diagnostics['desktop_connected'])
            self.assertEqual([c.args[0] for c in client.call.call_args_list], ['thread/loaded/list'])

    def test_failed_shared_listing_is_unknown_not_not_loaded(self):
        with patch.object(native, 'SharedServer') as shared, \
             patch.object(native, 'Desktop', side_effect=self.missing_owner()):
            shared.return_value.__enter__.side_effect = native.Unavailable('fixture')
            with self.assertRaises(native.Unavailable) as failed:
                native.send_codex({'id': C}, {})
        self.assertIsNone(failed.exception.diagnostics['shared_target_loaded'])

    def test_connection_failure_does_not_claim_app_exited(self):
        old_app = {'path': '/Applications/Codex.app', 'version': '26.1013.40934'}
        with patch.object(native, 'owned_path'), patch.object(native.socket, 'socket') as sock, \
             patch.object(native, 'codex_desktop_app', return_value=old_app):
            sock.return_value.connect.side_effect = ConnectionRefusedError('fixture')
            with self.assertRaises(native.Unavailable) as failed:
                with native.Desktop():
                    self.fail('not connected')
        self.assertEqual(failed.exception.diagnostics['code'], 'desktop_connection_unavailable')
        self.assertFalse(failed.exception.diagnostics['desktop_connected'])
        self.assertIn('does not prove the app is closed', str(failed.exception))
        sock.return_value.close.assert_called_once()

    def test_discovery_timeout_preserves_connected_but_unknown_owner(self):
        desktop = native.Desktop()
        with patch.object(desktop, 'call', side_effect=TimeoutError('fixture')):
            with self.assertRaises(native.Unavailable) as failed:
                desktop.owner(C.split(':')[1])
        self.assertEqual(failed.exception.diagnostics['code'], 'desktop_owner_discovery_failed')
        self.assertTrue(failed.exception.diagnostics['desktop_connected'])
        self.assertIsNone(failed.exception.diagnostics['desktop_owner_found'])

    def test_named_failure_keeps_candidate_evidence_without_assigning_mailbox(self):
        rows = [{'id': C, 'runtime': 'codex', 'name_address': 'fixture'}]
        error = self.missing_owner()
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'AGENTPEERS_HOME': folder}), \
             patch('agentpeers.core.inventory', return_value=rows), patch.object(native, 'Desktop') as desktop:
            desktop.return_value.__enter__.return_value.owner.side_effect = error
            with Bridge() as bridge:
                failed = bridge.send(A, 'fixture', 'hello', 'test only')
                self.assertFalse(failed['mailbox_available'])
                self.assertEqual(failed['detail']['route_failure']['code'], 'name_unreachable')
                self.assertEqual(failed['detail']['route_failure']['candidates'][0]['code'], 'desktop_owner_missing')
                self.assertIsNone(failed['read_at'])
                self.assertIn('nothing was saved for delivery', failed['destination_note'])
                desktop.return_value.__enter__.return_value.send.assert_not_called()

    def test_saved_failure_has_destination_evidence_and_separate_sender_route(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'AGENTPEERS_HOME': folder}), \
             patch.object(native, 'receive_route', return_value={'session_id': A, 'status': 'available'}), \
             patch.object(native, 'send_codex', side_effect=self.missing_owner()), \
             patch.object(Bridge, 'unavailable_hint', return_value={}):
            with Bridge() as bridge:
                failed = bridge.send(A, C, 'hello', 'test only')
                self.assertEqual(failed['transport_status'], 'unavailable')
                self.assertTrue(failed['mailbox_available'])
                self.assertEqual(failed['return_route']['session_id'], A)
                self.assertIn('sender', failed['return_route_note'])
                self.assertEqual(failed['detail']['route_failure']['code'], 'desktop_owner_missing')
                self.assertIsNone(failed['read_at'])
                self.assertIn('not automatically arrive', failed['destination_note'])
                self.assertLess(list(failed).index('destination_note'), list(failed).index('return_route'))
                saved_id = failed['id']
            with Bridge() as bridge:
                self.assertEqual(bridge.get(saved_id)['detail']['route_failure'], failed['detail']['route_failure'])

    def test_setup_distinguishes_connected_app_from_missing_owner(self):
        error = self.missing_owner()
        with patch.object(native, 'shared_threads', return_value=set()), patch.object(native, 'Desktop') as desktop, \
             patch.object(native, 'claude_sessions', return_value=[{'id': A}]), \
             patch('agentpeers.setup.registrations', return_value={}):
            desktop.return_value.__enter__.return_value.owner.side_effect = error
            result = doctor(actor=C)
        checks = {c['name']: c for c in result['checks']}
        self.assertEqual(checks['desktop_socket']['status'], 'pass')
        self.assertEqual(checks['desktop_owner']['status'], 'warning')
        self.assertEqual(checks['codex_receive_route']['status'], 'fail')
        self.assertFalse(result['ready_for_test'])


if __name__ == '__main__':
    unittest.main()
