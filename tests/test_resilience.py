"""Recovery behavior uses isolated mailboxes and fixture transports only."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch
import uuid

from agentpeers import native
from agentpeers.core import Bridge
from agentpeers.mcp import InvalidArguments, dispatch, serve

A = 'claude:' + str(uuid.uuid4())
C = 'codex:' + str(uuid.uuid4())
SCOPE = 'Human authorized this fixture exchange'


def timed_out():
    return native.Unavailable('slow lookup', code='codex_discovery_timeout')


class Resilience(unittest.TestCase):
    def test_correct_invalid_call_in_same_mcp_process_without_duplicate_send(self):
        ident = str(uuid.uuid4())
        args = {'to': A, 'message': 'fixture question', 'id': ident}
        corrected = dict(args, scope=SCOPE)
        requests = [{'id': i, 'method': 'tools/call', 'params': {
            'name': 'send_message', 'arguments': value}}
            for i, value in enumerate([args, corrected, corrected], 1)]
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'AGENTPEERS_HOME': home}), \
             patch('sys.stdin', io.StringIO('\n'.join(map(json.dumps, requests)))), patch('sys.stdout', output), \
             patch.object(native, 'claude_sessions', return_value=[{'id': A}]), \
             patch.object(native, 'receive_route', return_value={'status': 'available'}), \
             patch.object(native, 'send_claude', return_value={'transport': 'fixture'}) as send:
            serve(C)
            failed, delivered, repeated = [json.loads(line)['result'] for line in output.getvalue().splitlines()]
            failure = failed['structuredContent']
            self.assertEqual(failure['code'], 'invalid_arguments')
            self.assertEqual(failure['fields'], ['scope'])
            self.assertFalse(failure['message_created'])
            self.assertFalse(failure['submission_attempted'])
            self.assertEqual(json.loads(delivered['content'][0]['text'])['delivery'], 'submitted')
            self.assertTrue(json.loads(repeated['content'][0]['text'])['reused_id'])
            with Bridge() as bridge:
                self.assertEqual(bridge.db.execute('SELECT count(*) FROM messages').fetchone()[0], 1)
            send.assert_called_once()

    def test_invalid_types_enums_and_missing_fields_never_open_mailbox(self):
        samples = [('reply', {'id': []}), ('send_message', {'to': C, 'message': 4, 'scope': SCOPE}),
                   ('send_message', {'to': C, 'message': 'hi', 'scope': SCOPE, 'expects': 'maybe'}),
                   ('send_message', {'to': C, 'message': 'hi', 'scope': SCOPE, 'allow_question': 1}),
                   ('inbox', []), ('inbox', {'surprise': True}),
                   ('message_status', {}), ('send_message', {'to': C, 'message': 'hi', 'scope': SCOPE,
                                                          'summary': 'x' * 301})]
        with patch('agentpeers.mcp.Bridge') as bridge:
            for name, args in samples:
                with self.subTest(name=name, args=args), self.assertRaises(InvalidArguments):
                    dispatch(name, args, C)
            bridge.assert_not_called()

    def test_transient_timeout_retries_fresh_read_only_discovery_once(self):
        row = {'id': C.split(':')[1], 'name': 'fixture', 'cwd': '/fixture'}
        with patch.object(native, 'stored_codex_sessions', side_effect=[timed_out(), [row]]) as stored, \
             patch.object(native, 'SharedServer') as shared, patch.object(native, 'send_codex') as send:
            shared.return_value.__enter__.side_effect = FileNotFoundError('no shared server socket')
            result = native.codex_sessions()
        self.assertEqual(result[0]['id'], C)
        self.assertEqual(stored.call_args_list, [call(12), call(6)])
        send.assert_not_called()

    def test_stalled_initialization_is_replaced_by_new_read_only_process(self):
        with patch.object(native, 'AppServer') as server, patch.object(native, 'SharedServer') as shared:
            first, second = Mock(), Mock()
            first.__enter__ = Mock(side_effect=timed_out())
            first.__exit__ = Mock()
            second.__enter__ = Mock(return_value=second)
            second.__exit__ = Mock(return_value=False)
            second.call.return_value = {'data': [{'id': C.split(':')[1], 'name': 'fixture'}]}
            server.side_effect = [first, second]
            shared.return_value.__enter__.side_effect = FileNotFoundError('no shared server socket')
            self.assertEqual(native.codex_sessions()[0]['id'], C)
            self.assertEqual(server.call_count, 2)
            second.call.assert_called_once_with('thread/list', {'limit': 100, 'archived': False})
            self.assertEqual([c.kwargs['timeout'] for c in server.call_args_list], [12, 6])

    def test_real_stalled_child_is_reaped_and_fresh_child_recovers(self):
        # Exercise actual stdio/process cleanup without launching Codex or agents.
        script = '''#!/usr/bin/env python3
import json, os, pathlib, sys, time
marker = pathlib.Path(os.environ['FIXTURE_STARTS'])
starts = json.loads(marker.read_text()) if marker.exists() else []
starts.append(os.getpid()); marker.write_text(json.dumps(starts))
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request: continue
    if len(starts) == 1: time.sleep(60)
    result = {'data': [{'id': os.environ['FIXTURE_THREAD'], 'name': 'fixture'}]} if request['method'] == 'thread/list' else {}
    print(json.dumps({'id': request['id'], 'result': result}), flush=True)
'''
        with tempfile.TemporaryDirectory(prefix='ap read retry ') as folder:
            root = Path(folder)
            program = root / 'fixture-codex'
            program.write_text(script)
            program.chmod(0o700)
            starts = root / 'starts.json'
            env = {'AGENTPEERS_CODEX': str(program), 'FIXTURE_STARTS': str(starts),
                   'FIXTURE_THREAD': C.split(':')[1], 'CODEX_HOME': str(root)}
            with patch.dict(os.environ, env), patch.object(native, 'DISCOVERY_BUDGETS', (1, 2)), \
                 patch.object(native, 'send_codex') as send, patch.object(native, 'Desktop') as desktop:
                self.assertEqual(native.codex_sessions()[0]['id'], C)
            pids = json.loads(starts.read_text())
            self.assertEqual(len(pids), 2)
            for pid in pids:
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            send.assert_not_called()
            desktop.assert_not_called()

    def test_persistent_timeout_preserves_other_runtime_and_unknown_state(self):
        with patch.object(native, 'stored_codex_sessions', side_effect=timed_out()) as stored, \
             patch.object(native, 'claude_sessions', return_value=[{'id': A}]), \
             patch.object(native, 'send_codex') as send, patch.object(native, 'Desktop') as desktop:
            result = dispatch('list_sessions', {})
        self.assertEqual(stored.call_count, 2)
        self.assertEqual(result['sessions'], [{'id': A}])
        self.assertEqual(result['discovery_status'], 'partial')
        self.assertEqual(result['runtimes'], {'claude': 'complete', 'codex': 'unknown'})
        self.assertEqual(result['errors'][0]['attempts'], 2)
        self.assertEqual(result['errors'][0]['session_state'], 'unknown')
        self.assertFalse(result['submission_attempted'])
        send.assert_not_called()
        desktop.assert_not_called()

    def test_non_timeout_failure_is_not_retried(self):
        with patch.object(native, 'stored_codex_sessions', side_effect=native.Unavailable('permission denied')) as stored:
            with self.assertRaisesRegex(native.Unavailable, 'permission denied'):
                native.codex_sessions()
            stored.assert_called_once_with(12)

    def test_known_exact_recipient_still_works_after_discovery_failure(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'AGENTPEERS_HOME': home}), \
             patch.object(native, 'codex_sessions', side_effect=timed_out()) as discover, \
             patch.object(native, 'claude_sessions', return_value=[{'id': A}]), \
             patch.object(native, 'receive_route', return_value={'status': 'available'}), \
             patch.object(native, 'send_codex', return_value={'transport': 'fixture'}) as send:
            self.assertEqual(dispatch('list_sessions', {})['runtimes']['codex'], 'unknown')
            result = dispatch('send_message', {'to': C, 'message': 'fixture question', 'scope': SCOPE}, A)
            self.assertEqual(result['recipient'], C)
            self.assertEqual(result['delivery'], 'submitted')
            discover.assert_called_once()
            send.assert_called_once()

    def test_discovery_deadline_prevents_another_request_after_budget(self):
        client = native.AppServer(timeout=12, deadline=10)
        client.proc = Mock()
        with patch.object(native.time, 'monotonic', return_value=11):
            with self.assertRaises(native.Unavailable) as raised:
                client.call('thread/list', {})
        self.assertEqual(raised.exception.diagnostics['code'], 'codex_discovery_timeout')
        client.proc.stdin.write.assert_not_called()

    def test_partial_pagination_is_discarded_before_retry(self):
        with patch.object(native, 'AppServer') as saved, patch.object(native, 'SharedServer') as shared:
            first, second = Mock(), Mock()
            for server in (first, second):
                server.__enter__ = Mock(return_value=server)
                server.__exit__ = Mock(return_value=False)
            first.call.side_effect = [{'data': [{'id': 'old-partial'}], 'nextCursor': 'page2'}, timed_out()]
            second.call.return_value = {'data': [{'id': C.split(':')[1], 'name': 'fixture'}]}
            saved.side_effect = [first, second]
            shared.return_value.__enter__.side_effect = FileNotFoundError('no shared server socket')
            result = native.codex_sessions()
        self.assertEqual([row['id'] for row in result], [C])


if __name__ == '__main__':
    unittest.main()
