"""Direct local-session -> Dot contract; no live account or agent calls."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from agentpeers import mcp, personal_client
from agentpeers.personal.app import App
from agentpeers.personal.config import Rejected
from agentpeers.personal.endpoint import Endpoint
from agentpeers.personal.local import LocalSessions
from agentpeers.personal.store import Store
from test_personal import configuration


A = 'claude:' + str(uuid.uuid4())
B = 'codex:' + str(uuid.uuid4())


class DirectDot(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = LocalSessions.connection(configuration(), 'local-test', 'Local test sessions')
        self.store = Store(Path(self.temp.name) / 'cloud.sqlite3', self.config)
        self.addCleanup(self.store.close)
        self.app = App(self.store)

    def call(self, actor, tool, **args):
        return self.app.local.call('local-test', actor, tool, args)

    def send(self, actor=A, mid='test-question'):
        return self.call(actor, 'send_message', to='Dot', message='Echo one synthetic word.',
                         scope='One informational setup check.', message_id=mid)

    def reply(self, mid='test-question', reply='test-answer', expects='none'):
        self.store.ack('dot', mid)
        return self.store.send('dot', 'local-test', 'Synthetic reply.', message_id=reply, reply_to=mid, expects=expects)

    def test_direct_reply_returned_only_to_origin_and_not_auto_acknowledged(self):
        self.send()
        answer = self.reply()
        self.assertEqual(answer['state'], 'available')
        result = self.call(A, 'message_status', message_id='test-question')
        self.assertEqual(result['reply_messages'][0]['body'], 'Synthetic reply.')
        self.assertIsNone(result['reply_messages'][0]['read_at'])
        for tool in ('message_status', 'acknowledge', 'read_message'):
            with self.assertRaisesRegex(Rejected, 'another local session'):
                self.call(B, tool, message_id='test-answer')
        receipt = self.call(A, 'acknowledge', message_id='test-answer')
        self.assertIsNotNone(receipt['read_at'])
        self.assertFalse(self.app.dispatcher.once())  # No desktop delivery or idle poll.

    def test_duplicates_are_bound_before_send_and_never_steal_a_conversation(self):
        first = self.send()
        self.assertTrue(self.send()['duplicate'])
        with self.assertRaisesRegex(Rejected, 'another local session'):
            self.send(B)
        self.assertEqual(len(self.store.inbox('dot')), 1)
        self.assertEqual(self.send()['id'], first['id'])
        self.assertEqual([r['id'] for r in self.call(A, 'inbox')['conversations']], [first['id']])
        self.assertEqual(self.call(B, 'inbox')['conversations'], [])
        with self.assertRaises(Rejected):
            self.call(A, 'send_message', to='Dot', message='', scope='test', message_id='reserved')
        with self.assertRaisesRegex(Rejected, 'another local session'):
            self.send(B, 'reserved')

    def test_legacy_conversations_and_nonlocal_keys_cannot_be_claimed(self):
        self.store.send('local-test', 'dot', 'Legacy', 'test', 'legacy')
        with self.assertRaisesRegex(Rejected, 'no local-session binding'):
            self.send(mid='legacy')
        with self.assertRaisesRegex(Rejected, 'dedicated local connector'):
            self.app.local.call('dot', A, 'list_peers', {})
        self.assertEqual(self.call(B, 'inbox')['conversations'], [])

    def test_followup_preserves_scope_and_expiry_and_revocation_apply(self):
        self.send()
        self.reply(expects='reply')
        self.call(A, 'acknowledge', message_id='test-answer')
        followup = self.call(A, 'reply', message_id='test-answer', message='One follow-up?', reply_id='followup', expects='reply')
        self.assertEqual(followup['claimed_scope'], 'One informational setup check.')
        self.assertEqual(followup['conversation_id'], 'test-question')
        self.config['connections'] = [['grokbot', 'dot']]
        with self.assertRaises(Rejected):
            self.call(A, 'message_status', message_id='test-question')

    def test_connection_preserves_existing_keys_grants_and_messages(self):
        original = configuration()
        before = deepcopy(original)
        config = LocalSessions.connection(original, 'local-new', 'New local sessions')
        self.assertEqual(original, before)
        self.assertEqual(config['peers']['dot'], original['peers']['dot'])
        self.assertEqual(config['peers']['grokbot'], original['peers']['grokbot'])
        again = LocalSessions.connection(config, 'local-new', 'New local sessions')
        self.assertEqual(config, again)
        with self.assertRaises(Rejected):
            LocalSessions.connection(config, 'local-new', 'Another name')

    def test_local_key_cannot_use_general_inbox_endpoint(self):
        key = self.config['peers']['local-test']['key']
        for path, data in [('/v1/call', {'tool': 'inbox', 'arguments': {}}),
                           ('/v1/local', {'session': A, 'tool': 'list_peers', 'arguments': {}})]:
            raw = json.dumps(data).encode()
            endpoint = Endpoint(self.app, 'POST', path, {'Authorization': 'Bearer ' + key,
                                'Content-Type': 'application/json', 'Content-Length': str(len(raw))}, raw)
            from agentpeers.personal.transport import run
            status, value, _, _ = run(endpoint.steps(), lambda *a: self.fail('No outbound call'))
            self.assertEqual(status, 200 if path == '/v1/local' else 400, value)


class LocalClient(unittest.TestCase):
    def test_setup_uses_owner_once_and_saves_only_the_separate_connector_key(self):
        data = {'version': 1, 'url': 'https://bridge.example', 'connector': 'local-test', 'key': 'x'*40}
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'AGENTPEERS_HOME': home}), \
             patch.object(personal_client, 'post', return_value=data) as post:
            personal_client.connect('https://bridge.example', 'local-test', 'Test', password='owner-test')
            saved = personal_client.config_path()
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('owner-test', saved.read_text())
            self.assertEqual(personal_client.load(), data)
            personal_client.connect('https://bridge.example', 'local-test', 'Test', password='owner-test')
            self.assertEqual(post.call_count, 1)

    def test_private_configuration_and_identity_are_required(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'AGENTPEERS_HOME': home}):
            with self.assertRaisesRegex(ValueError, 'not connected'):
                personal_client.load()
            path = Path(home) / 'personal-client.json'
            data = {'version': 1, 'url': 'https://bridge.example', 'connector': 'local-test', 'key': 'x' * 40}
            path.write_text(json.dumps(data)); path.chmod(0o600)
            self.assertEqual(personal_client.load(), data)
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'private owned'):
                personal_client.load()
            with self.assertRaises(ValueError):
                personal_client.validate({**data, 'key': 'x'*40 + '\nInjected: bad'})

    def test_http_client_identifies_itself_and_never_echoes_refused_credentials(self):
        import urllib.error
        opener = unittest.mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError('https://bridge.example/v1/local', 403, 'blocked', {}, None)
        with patch.object(personal_client.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaises(personal_client.Refused) as failed:
                personal_client.post('https://bridge.example/v1/local', {}, key='private-test-key')
        self.assertNotIn('private-test-key', str(failed.exception))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header('User-agent'), 'AgentPeers/' + mcp.__version__)

    def test_network_uncertainty_never_automatically_resends(self):
        config = {'url': 'https://bridge.example', 'key': 'not-a-real-key'}
        with patch.object(personal_client, 'load', return_value=config), \
             patch.object(personal_client, 'post', side_effect=ValueError('unknown')) as post:
            result = personal_client.call(A, 'send_message', {'message_id': 'same-id'}, 30)
            self.assertEqual(result['state'], 'unconfirmed')
            self.assertEqual(result['id'], 'same-id')
        self.assertEqual(post.call_count, 1)

    def test_mcp_retrieves_answer_in_calling_session_without_desktop_or_identity_override(self):
        with patch.object(personal_client, 'call', return_value={'reply_messages': [{'body': 'Echo'}]}) as call:
            result = mcp.dispatch('send_personal_message', {'to': 'Dot', 'message': 'test', 'scope': 'test', 'wait_seconds': 30}, actor=A)
            self.assertEqual(result['reply_messages'][0]['body'], 'Echo')
            self.assertEqual(call.call_args.args[0], A)
            with self.assertRaisesRegex(ValueError, 'bound session'):
                mcp.dispatch('personal_message_status', {'id': 'id', 'sender': B}, actor=A)
            with self.assertRaises(mcp.InvalidArguments):
                mcp.dispatch('personal_message_status', {'id': 'id', 'wait_seconds': True}, actor=A)

    def test_wait_only_retrieves_status_and_preserves_stable_message_id(self):
        with patch.object(personal_client, 'load', return_value={'url': 'https://bridge.example', 'key': 'test'}), \
             patch.object(personal_client, 'post', side_effect=[{'id': 'stable', 'state': 'queued', 'replies': []},
                         {'id': 'stable', 'state': 'read', 'replies': ['reply'], 'reply_messages': [{'body': 'Echo'}]}]) as post, \
             patch.object(personal_client.time, 'sleep'):
            result = personal_client.call(A, 'send_message', {'message_id': 'stable'}, 30)
            self.assertEqual(result['reply_messages'][0]['body'], 'Echo')
            self.assertEqual([c.args[1]['tool'] for c in post.call_args_list], ['send_message', 'message_status'])


if __name__ == '__main__':
    unittest.main()
