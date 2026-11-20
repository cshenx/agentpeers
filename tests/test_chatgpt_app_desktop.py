"""Codex desktop moved into ChatGPT.app; build 26.1117.31558 was inspected without the desktop IPC router."""
import os
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import uuid

from agentpeers import native
from agentpeers.core import Bridge

C = 'codex:' + str(uuid.uuid4())
A = 'claude:' + str(uuid.uuid4())
ROOT = Path(__file__).resolve().parent.parent
INSPECTED = {'path': '/Applications/ChatGPT.app', 'version': '26.1117.31558'}
NEWER = {'path': '/Applications/ChatGPT.app', 'version': '26.1201.1'}
OLD = {'path': '/Applications/Codex.app', 'version': '26.1013.40934'}
REFUSED = ConnectionRefusedError(61, 'Connection refused')


def app(root, name, bundle, version):
    contents = Path(root) / 'Applications' / name / 'Contents'
    contents.mkdir(parents=True)
    with (contents / 'Info.plist').open('wb') as target:
        plistlib.dump({'CFBundleIdentifier': bundle, 'CFBundleShortVersionString': version}, target)


def stop_before_resume(test, text):
    steps = ['stop its active session in the app', 'confirm the app has released the chat',
             'no app process holds its rollout file', 'do not resume the chat in a terminal', 'codex resume']
    for step in steps:
        test.assertIn(step, text)
    positions = [text.index(step) for step in steps]
    test.assertEqual(positions, sorted(positions))
    test.assertNotIn('so the app and a terminal never drive it at once', text)


class DesktopApp(unittest.TestCase):
    def test_finds_codex_bundle_under_either_name_and_skips_chat_app(self):
        with tempfile.TemporaryDirectory() as root:
            app(root, 'ChatGPT.app', 'com.openai.chat', '1.2026.300')
            self.assertIsNone(native.codex_desktop_app([Path(root)]))
        with tempfile.TemporaryDirectory() as root:
            app(root, 'ChatGPT.app', 'com.openai.codex', '26.1117.31558')
            self.assertEqual(native.codex_desktop_app([Path(root)]),
                             {'path': str(Path(root) / 'Applications/ChatGPT.app'), 'version': '26.1117.31558'})
        with tempfile.TemporaryDirectory() as root:
            app(root, 'Codex.app', 'com.openai.codex', '26.1013.40934')
            app(root, 'ChatGPT.app', 'com.openai.codex', '26.1117.31558')
            self.assertEqual(native.codex_desktop_app([Path(root)])['version'], '26.1013.40934')

    def test_only_the_inspected_build_is_diagnosed_and_newer_builds_are_unverified(self):
        self.assertEqual(native.desktop_ipc_finding(INSPECTED), 'inspected')
        self.assertEqual(native.desktop_ipc_finding(NEWER), 'newer')
        self.assertIsNone(native.desktop_ipc_finding(OLD))
        self.assertIsNone(native.desktop_ipc_finding({'version': 'beta'}))
        self.assertIsNone(native.desktop_ipc_finding({}))


class DesktopConnect(unittest.TestCase):
    def connect(self, installed, error):
        with patch.object(native, 'owned_path'), patch.object(native.socket, 'socket') as sock, \
             patch.object(native, 'codex_desktop_app', return_value=installed):
            sock.return_value.connect.side_effect = error
            with self.assertRaises(native.Unavailable) as failed:
                with native.Desktop():
                    self.fail('not connected')
        sock.return_value.sendall.assert_not_called()
        sock.return_value.close.assert_called_once()
        return failed.exception

    def test_inspected_build_reports_observed_failure_and_no_live_attempt(self):
        error = self.connect(INSPECTED, REFUSED)
        self.assertEqual(error.diagnostics['code'], 'desktop_ipc_unsupported')
        self.assertEqual(error.diagnostics['desktop_version'], '26.1117.31558')
        self.assertEqual(error.diagnostics['desktop_ipc_inspected_absent_in'], '26.1117.31558')
        self.assertIn('Connection refused', error.diagnostics['connection_error'])
        self.assertIs(error.diagnostics['live_delivery_attempted'], False)
        self.assertTrue(str(error).startswith('Desktop IPC connection failed at '))
        self.assertIn('ipc.sock: [Errno 61] Connection refused', str(error))
        self.assertIn('inspected without a desktop IPC router', str(error))
        self.assertIn('Live delivery was not attempted', str(error))
        self.assertNotIn('No message submitted', str(error))

    def test_newer_build_is_not_assumed_to_lack_ipc(self):
        error = self.connect(NEWER, REFUSED)
        self.assertEqual(error.diagnostics['code'], 'desktop_connection_unavailable')
        self.assertIn('newer than the inspected build 26.1117.31558', str(error))
        self.assertIn('unverified', str(error))
        self.assertNotIn('cannot deliver', str(error))
        self.assertIs(error.diagnostics['live_delivery_attempted'], False)

    def test_missing_socket_on_inspected_build_is_the_same_limit(self):
        with patch.object(native, 'codex_desktop_app', return_value=INSPECTED):
            with self.assertRaises(native.Unavailable) as failed:
                with patch.object(native, 'owned_path', side_effect=FileNotFoundError(2, 'missing')):
                    with native.Desktop():
                        self.fail('not connected')
        self.assertEqual(failed.exception.diagnostics['code'], 'desktop_ipc_unsupported')

    def test_codex_app_and_other_errors_keep_unknown_state_wording(self):
        for installed, error in ((OLD, REFUSED), (INSPECTED, PermissionError(13, 'denied'))):
            failure = self.connect(installed, error)
            self.assertEqual(failure.diagnostics, {'code': 'desktop_connection_unavailable', 'desktop_connected': False})
            self.assertIn('does not prove the app is closed', str(failure))

    def test_terminal_guidance_warns_to_stop_the_app_session_first(self):
        for installed in (INSPECTED, NEWER):
            stop_before_resume(self, str(self.connect(installed, REFUSED)))
        stop_before_resume(self, native.RECEIVE_GUIDANCE)

    def test_docs_warn_to_stop_the_app_session_before_resuming(self):
        docs = (ROOT / 'docs/coding-sessions.md').read_text()
        line = next(l for l in docs.splitlines() if 'desktop_ipc_unsupported' in l)
        stop_before_resume(self, line)
        skill = (ROOT / 'skills/agentpeers/SKILL.md').read_text()
        self.assertIn('requires first stopping its active session in the app', skill)
        self.assertIn('confirming the app has released it', skill)
        self.assertIn('if the release cannot be confirmed, do not resume it in a terminal', skill)
        self.assertNotIn('26.1117 and later', skill + docs)


class SavedMessage(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pb-')
        self.env = patch.dict(os.environ, {'AGENTPEERS_HOME': self.temp.name})
        self.env.start()
        self.route = patch.object(native, 'receive_route', return_value={
            'status': 'available', 'transport': 'fixture', 'checked_at': 1, 'proof': 'fixture'})
        self.route.start()
        self.bridge = Bridge()

    def tearDown(self):
        self.bridge.db.close()
        self.route.stop()
        self.env.stop()
        self.temp.cleanup()

    def failure(self):
        with patch.object(native, 'codex_desktop_app', return_value=INSPECTED):
            return native.desktop_ipc_unsupported(REFUSED, '/fixture/ipc/ipc.sock')

    def test_send_says_saved_unread_and_later_read(self):
        with patch.object(native, 'send_codex', side_effect=self.failure()), \
             patch.object(native, 'codex_sessions', return_value=[]):
            message = self.bridge.send(A, C, 'fixture body', 'test only', expects='none')
        self.assertEqual(message['status'], 'unavailable')
        self.assertTrue(message['detail']['reason'].startswith(
            "Live delivery was not attempted; the message is saved unread in the recipient's mailbox. "))
        self.assertEqual(message['detail']['route_failure']['code'], 'desktop_ipc_unsupported')
        self.assertIn('not yet read', message['delivery_note'])
        self.assertIn('Live delivery was not attempted', message['destination_note'])
        self.assertIn('saved for the recipient', message['destination_note'])
        self.assertEqual(self.bridge.inbox(C)[0]['body'], 'fixture body')
        self.bridge.ack(message['id'], C)
        read = self.bridge.get(message['id'])
        self.assertEqual(read['delivery'], 'read')
        self.assertIn('has since read', read['destination_note'])

    def test_other_failures_keep_the_retained_wording(self):
        with patch.object(native, 'send_codex', side_effect=native.Unavailable('fixture', code='desktop_owner_missing')), \
             patch.object(native, 'codex_sessions', return_value=[]):
            message = self.bridge.send(A, C, 'fixture body', 'test only', expects='none')
        self.assertTrue(message['detail']['reason'].startswith('Automatic delivery unavailable; message retained'))

    def test_send_reports_the_limit_with_shared_server_state(self):
        with patch.object(native, 'SharedServer') as shared, patch.object(native, 'owned_path'), \
             patch.object(native.socket, 'socket') as sock, \
             patch.object(native, 'codex_desktop_app', return_value=INSPECTED):
            shared.return_value.call.return_value = {'data': []}
            sock.return_value.connect.side_effect = REFUSED
            with self.assertRaises(native.Unavailable) as failed:
                native.send_codex({'id': C}, {'body': 'fixture'})
        self.assertEqual(failed.exception.diagnostics['code'], 'desktop_ipc_unsupported')
        self.assertIs(failed.exception.diagnostics['shared_target_loaded'], False)
        sock.return_value.sendall.assert_not_called()


class Listing(unittest.TestCase):
    def note(self, installed, connect_error):
        with patch.object(native, 'codex_desktop_app', return_value=installed), \
             patch.object(native.socket, 'socket') as sock:
            sock.return_value.connect.side_effect = connect_error
            result = native.desktop_unreachable_note()
        return result, sock

    def test_listing_qualifies_by_build_and_needs_an_observed_failure(self):
        result, sock = self.note(INSPECTED, REFUSED)
        self.assertIn('not reachable by AgentPeers: desktop IPC connection failed', result)
        sock.return_value.sendall.assert_not_called()
        sock.return_value.close.assert_called_once()
        result, _ = self.note(NEWER, REFUSED)
        self.assertIn('reachability unverified', result)
        self.assertIsNone(self.note(INSPECTED, None)[0])
        result, sock = self.note(OLD, REFUSED)
        self.assertIsNone(result)
        sock.return_value.connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
