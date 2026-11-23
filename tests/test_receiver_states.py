"""Timeouts must not become offline claims or select another named peer."""
import os
import tempfile
import unittest
from unittest.mock import patch
import uuid

from agentpeers import native
from agentpeers.addressing import resolve_name
from agentpeers.core import Bridge

C = 'codex:' + str(uuid.uuid4())
A = 'claude:' + str(uuid.uuid4())
ROWS = [{'id': C, 'runtime': 'codex', 'name_address': 'fixture', 'surface': 'desktop-or-stored'}]


def timeout():
    return native.Unavailable('receiver discovery timed out', code='desktop_owner_discovery_failed',
                              desktop_connected=True, desktop_owner_found=None)


class ReceiverStates(unittest.TestCase):
    def test_name_timeout_reports_unknown_not_nobody_listening(self):
        with patch.object(native, 'Desktop') as desktop:
            desktop.return_value.__enter__.return_value.owner.side_effect = timeout()
            with self.assertRaises(native.Unavailable) as failed:
                resolve_name('fixture', ROWS)
            self.assertEqual(failed.exception.diagnostics['code'], 'name_resolution_unverified')
            self.assertIn('unknown', str(failed.exception))
            self.assertNotIn('Nobody is listening', str(failed.exception))
            desktop.return_value.__enter__.return_value.send.assert_not_called()

    def test_unverified_duplicate_cannot_silently_select_live_peer(self):
        rows = ROWS + [{'id': A, 'runtime': 'claude', 'name_address': 'fixture'}]
        with patch.object(native, 'Desktop') as desktop:
            desktop.return_value.__enter__.return_value.owner.side_effect = timeout()
            with self.assertRaises(native.Unavailable) as failed:
                resolve_name('fixture', rows)
        self.assertEqual(failed.exception.diagnostics['verified_candidates'], [A])
        self.assertEqual(failed.exception.diagnostics['candidates'][0]['session_id'], C)

    def test_name_timeout_remains_unsubmitted_unassigned_and_persisted(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'AGENTPEERS_HOME': folder}), \
             patch('agentpeers.core.inventory', return_value=ROWS), patch.object(native, 'Desktop') as desktop:
            desktop.return_value.__enter__.return_value.owner.side_effect = timeout()
            with Bridge() as bridge:
                row = bridge.send(A, 'fixture', 'hello', 'test only')
                self.assertEqual(row['transport_status'], 'unavailable')
                self.assertIsNone(row['recipient'])
                self.assertIn('state is unknown', row['destination_note'])
                self.assertIn('nothing was saved for delivery', row['destination_note'])
                self.assertEqual(bridge.get(row['id'])['detail'], row['detail'])
                desktop.return_value.__enter__.return_value.send.assert_not_called()

    def test_return_probe_preserves_unknown_despite_healthy_desktop_socket(self):
        with patch.object(native, 'SharedServer') as shared, patch.object(native, 'Desktop') as desktop:
            shared.return_value.__enter__.return_value.call.return_value = {'data': []}
            desktop.return_value.__enter__.return_value.owner.side_effect = timeout()
            route = native.receive_route(C)
        self.assertEqual(route['status'], 'unknown')
        self.assertTrue(route['diagnostics']['desktop_connected'])
        self.assertIsNone(route['diagnostics']['desktop_owner_found'])
        self.assertIs(route['diagnostics']['shared_target_loaded'], False)

    def test_missing_desktop_owner_with_unverified_shared_server_is_unknown(self):
        with patch.object(native, 'SharedServer') as shared, patch.object(native, 'Desktop') as desktop:
            shared.return_value.__enter__.side_effect = native.Unavailable('cannot check')
            desktop.return_value.__enter__.return_value.owner.side_effect = native.Unavailable(
                'no desktop owner', code='desktop_owner_missing')
            route = native.receive_route(C)
        self.assertEqual(route['status'], 'unknown')
        self.assertIsNone(route['diagnostics']['shared_target_loaded'])

    def test_read_receipt_does_not_keep_telling_recipient_to_collect_it(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'AGENTPEERS_HOME': folder}), \
             patch.object(native, 'receive_route', return_value={'status': 'available'}), \
             patch.object(native, 'send_codex', side_effect=timeout()), \
             patch.object(Bridge, 'unavailable_hint', return_value={}):
            with Bridge() as bridge:
                row = bridge.send(A, C, 'hello', 'test only')
                original_detail = row['detail']
                read = bridge.ack(row['id'], C)
                self.assertEqual(read['delivery'], 'read')
                self.assertEqual(read['transport_status'], 'unavailable')
                self.assertEqual(read['detail'], original_detail)
                self.assertIn('has since read', read['destination_note'])
                self.assertNotIn('collect from its mailbox', read['destination_note'])


if __name__ == '__main__':
    unittest.main()
