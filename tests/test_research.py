"""Paired research -> exact local launcher; no live accounts."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agentpeers.personal.app import App
from agentpeers.personal.config import Rejected
from agentpeers.personal.local import LocalSessions
from agentpeers.personal.store import Store
from agentpeers import research_client, native
from test_personal import configuration

A = 'codex:11111111-1111-4111-8111-111111111111'
B = 'codex:22222222-2222-4222-8222-222222222222'
META = {'openai/subject': 'test-user', 'openai/session': 'research-chat'}


class ResearchJourney(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000
        self.config = LocalSessions.connection(configuration(), 'local-test', 'Test sessions')
        self.store = Store(Path(self.temp.name)/'messages.sqlite3', self.config, clock=lambda:self.now)
        self.addCleanup(self.store.close)
        self.app = App(self.store)
        self.store.db.execute("INSERT INTO ap_facts_clients VALUES('test-client',1)")
        self.local('register', id='handoff-one', scope='Only synthetic facts', title='Launcher')

    def local(self, name, actor=A, **args):
        return self.app.local.call('local-test', actor, 'research_'+name, args)

    def ask(self, request='question-one', question='Echo synthetic fact', meta=META):
        return self.app.factual.call('test-client','request_context',{'request_id':request,'destination':'Codex','topic':'handoff-one','question':question},meta)

    def pair(self):
        result = self.ask()
        self.assertEqual(result['state'], 'approval_required')
        self.assertFalse(result['submitted'])
        self.assertEqual(self.local('pending',id='handoff-one')['requests'], [])
        self.local('pair',id='handoff-one',pairing_code=result['pairing_code'])
        return self.local('pending',id='handoff-one')['requests'][0]['id']

    def test_question_read_answer_and_origin_retrieval(self):
        key=self.pair()
        self.assertTrue(self.local('claim',id='handoff-one',request=key)['claimed'])
        self.local('delivery',id='handoff-one',request=key,state='submitted')
        self.assertIsNone(self.ask()['read_at'])
        self.local('read',id='handoff-one',request=key)
        result=self.local('answer',id='handoff-one',request=key,answer='synthetic-fact',sources=['fixture:source'],reply_id='answer-one',status='answered')
        self.assertEqual(result['state'],'completed')
        returned=self.app.factual.call('test-client','context_result',{'request_id':'question-one'},META)
        self.assertEqual(returned['answer'],'synthetic-fact')
        self.assertEqual(returned['responding_agent'],A)
        with self.assertRaises(Rejected):self.app.factual.call('test-client','context_result',{'request_id':'question-one'},{**META,'openai/session':'another-chat'})

    def test_other_launcher_cannot_pair_read_deliver_or_answer(self):
        pending=self.ask()
        for action,args in [('pair',{'pairing_code':pending['pairing_code']}),('pending',{}),('close',{})]:
            with self.assertRaisesRegex(Rejected,'another local session'):
                self.local(action,actor=B,id='handoff-one',**args)
        self.local('pair',id='handoff-one',pairing_code=pending['pairing_code'])
        with self.assertRaisesRegex(Rejected,'different chat'):self.ask(request='other',meta={**META,'openai/session':'another-chat'})

    def test_no_read_means_no_answer_and_duplicate_requests_do_not_dispatch_twice(self):
        key=self.pair()
        self.assertTrue(self.local('claim',id='handoff-one',request=key)['claimed'])
        self.assertFalse(self.local('claim',id='handoff-one',request=key)['claimed'])
        with self.assertRaisesRegex(Rejected,'acknowledge'):
            self.local('answer',id='handoff-one',request=key,answer='x',sources=['fixture:x'],reply_id='answer',status='answered')
        self.assertTrue(self.ask()['duplicate'])
        with self.assertRaisesRegex(Rejected,'different content'):self.ask(question='changed')
        self.assertEqual(self.local('pending',id='handoff-one')['requests'],[])

    def test_expiry_close_and_malformed_pairing_fail_without_delivery(self):
        pending=self.ask()
        with self.assertRaises(Rejected):self.local('pair',id='handoff-one',pairing_code='WRONG')
        self.now+=601
        self.assertEqual(self.ask()['state'],'expired')
        with self.assertRaises(Rejected):self.local('pair',id='handoff-one',pairing_code=pending['pairing_code'])
        self.local('close',id='handoff-one')
        self.assertEqual(self.ask()['state'],'stopped')

    def test_generic_machine_send_rejected_and_existing_local_reply_preserved(self):
        with self.assertRaisesRegex(Rejected,'individual session'):
            self.store.send('dot','local-test','orphan','test','orphan')
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM messages WHERE id='orphan'").fetchone())
        question=self.app.local.call('local-test',A,'send_message',{'to':'dot','message':'hello','scope':'test','message_id':'local-question'})
        self.store.ack('dot',question['id'])
        reply=self.store.send('dot','local-test','answer',reply_to=question['id'],message_id='local-answer',expects='none')
        self.assertEqual(reply['state'],'available')

    def test_pairing_notice_is_once_and_distinct_from_question_submission(self):
        self.ask()
        def control(actor,name,args):return self.local(name,actor=actor,**args)
        with patch.object(research_client,'call',side_effect=control),patch.object(native,'send_codex',return_value={'transport':'fixture'}) as send:
            research_client.deliver_once(A,'handoff-one')
            research_client.deliver_once(A,'handoff-one')
            self.assertEqual(send.call_count,1)
            first=send.call_args.args[1]['id']
            self.local('pair',id='handoff-one',pairing_code=self.ask()['pairing_code'])
            research_client.deliver_once(A,'handoff-one')
            self.assertEqual(send.call_count,2)
            self.assertNotEqual(first,send.call_args.args[1]['id'])
            self.assertIn('request',send.call_args.args[1])

    def test_revoked_requester_stops_local_delivery_without_retargeting(self):
        self.pair()
        self.store.factual.revoke_client('test-client')
        with self.assertRaisesRegex(Rejected,'revoked'):
            self.local('pending',id='handoff-one')

    def test_native_submit_is_claimed_once_and_uncertainty_is_not_retried(self):
        self.pair()
        def control(actor,name,args):return self.local(name,actor=actor,**args)
        with patch.object(research_client,'call',side_effect=control),patch.object(native,'send_codex',side_effect=native.Unconfirmed('lost result')) as send:
            result=research_client.deliver_once(A,'handoff-one')
            self.assertEqual(result['deliveries'][0]['state'],'unconfirmed')
            research_client.deliver_once(A,'handoff-one')
        self.assertEqual(send.call_count,1)
        self.assertEqual(self.ask()['state'],'unconfirmed')
