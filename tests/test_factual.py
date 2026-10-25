"""Real owner/OAuth/MCP/receiver journeys with isolated fixtures, no live accounts."""
from concurrent.futures import ThreadPoolExecutor
import html
import json
import re
import unittest

import test_context_bridge as legacy
from agentpeers.personal.config import digest
from agentpeers.personal.endpoint import Endpoint
from agentpeers.personal.factual import RESOURCE_PATH, SCOPE
from agentpeers.personal.transport import run


class FactualTests(unittest.TestCase):
    connect = legacy.ContextBridgeTests.connect
    post = legacy.ContextBridgeTests.post
    dot_tool = legacy.ContextBridgeTests.dot_tool
    tearDown = legacy.ContextBridgeTests.tearDown

    def http(self, method, path, body=None, token=None, origin=None, cookie=None):
        raw = b'' if body is None else json.dumps(body).encode()
        headers = {'Content-Length':str(len(raw)), 'Content-Type':'application/json'}
        if token: headers['Authorization'] = 'Bearer ' + token
        if origin: headers['Origin'] = origin
        if cookie: headers['Cookie'] = cookie
        return run(Endpoint(self.app, method, path, headers, raw).steps(), self.post)

    def setUp(self):
        legacy.ContextBridgeTests.setUp(self)
        self.question = dict(request_id='facts-1', destination='Dot', topic='project', question='Which part of the project is ready?')
        self.owner = None

    def login(self):
        response = self.http('POST','/context/login',{'owner_key':self.config['owner_key']}, origin=self.config['public_url'])
        self.assertEqual(response[0],303)
        cookie = response[2]['Set-Cookie']
        for flag in ('Secure','HttpOnly','SameSite=Strict','Max-Age=900'):
            self.assertIn(flag,cookie)
        self.owner = cookie.split(';')[0]

    def owner_action(self, action, **fields):
        return self.http('POST','/context/actions',{'action':action,**fields},origin=self.config['public_url'],cookie=self.owner)

    def prepare(self, bind=True):
        self.login()
        self.policy = dict(topic='project',scope='Project status and design only. Exclude credentials and unrelated private facts.',refs='dot:project-context\nhttps://github.com/example/project',subscription=self.sid)
        self.assertEqual(self.owner_action('save_policy',**self.policy)[0],303)
        self.requester = self.connect(RESOURCE_PATH,SCOPE)
        if bind:
            pending = self.value('request_context',self.question)
            self.assertEqual(pending['state'],'approval_required')
            self.assertFalse(pending['submitted'])
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM messages').fetchone()[0],0)
            page = self.http('GET','/context',cookie=self.owner)[1]
            self.assertIn(pending['pairing_code'],page)
            self.candidate = re.search('name="candidate" value="([^"]+)"',page)[1]
            self.assertEqual(self.owner_action('allow_chat',candidate=self.candidate,days='30')[0],303)

    def request(self, name, args=None, meta=None, token=None):
        return self.http('POST',RESOURCE_PATH,{'id':1,'method':'tools/call','params':{'name':name,'arguments':args or {},'_meta':self.meta if meta is None else meta}},token=token or self.requester)

    def value(self, name, args=None, meta=None):
        response = self.request(name,args,meta)
        self.assertEqual(response[0],200,response)
        self.assertNotIn('isError',response[1]['result'],response)
        return response[1]['result']['structuredContent']

    def error(self, name, args=None, meta=None):
        response=self.request(name,args,meta)
        self.assertEqual(response[0],200)
        self.assertTrue(response[1]['result']['isError'])
        return response[1]['result']['content'][0]['text']

    def consume(self):
        self.value('request_context',self.question)
        self.mid=self.store.db.execute('SELECT message_id FROM ap_facts_requests').fetchone()[0]
        claimed=self.dot_tool('claim_message',message_id=self.mid)
        self.assertTrue(claimed['claimed'])
        self.assertEqual(claimed['sender'],'chatgpt-context')
        body=json.loads(claimed['body'])
        self.assertEqual(body['schema'],'agentpeers.factual.v1')
        self.assertEqual(body['question'],self.question['question'])
        self.assertEqual(body['owner_scope'],self.policy['scope'])
        self.dot_tool('read_message',message_id=self.mid)
        self.dot_tool('acknowledge',message_id=self.mid)

    def answer(self, **changes):
        payload=dict(request_id='facts-1',status='answered',answer='The API is ready for review.',sources=[dict(reference='dot:project-context',observed_at=999)])
        payload.update(changes)
        return self.http('POST','/mcp/dot',{'id':1,'method':'tools/call','params':{'name':'reply','arguments':dict(message_id=self.mid,reply_id='fact-answer',message=json.dumps(payload),expects='none')}},token=self.dot)[1]['result']

    def test_default_denial_and_normal_owner_setup_without_manual_correlation_fields(self):
        self.assertFalse(self.app.factual.active())
        self.assertEqual(self.http('POST',RESOURCE_PATH,{'id':1,'method':'tools/list'})[0],401)
        page=self.http('GET','/context')[1]
        self.assertIn('Your AgentPeers password',page)
        self.assertNotIn(self.config['owner_key'],page)
        self.prepare()
        page=self.http('GET','/context',cookie=self.owner)[1]
        self.assertIn('Allowed chats',page)
        self.assertNotIn('name="subject"',page)
        self.assertNotIn('name="session"',page)
        self.assertNotIn('https://callbacks.example/context-fixture',page)
        self.assertNotIn(self.config['owner_key'],page)
        destinations=self.value('context_destinations')['destinations']
        self.assertEqual(destinations[0]['destination'],'Dot')
        self.assertEqual(destinations[0]['topic'],'project')

    def test_arbitrary_authorized_question_sourced_answer_explicit_retrieval_and_no_sender_wake(self):
        self.prepare();self.value('request_context',self.question)
        self.assertTrue(self.app.dispatcher.once())
        self.assertEqual(len(self.deliveries),1)
        self.consume()
        result=self.answer()
        self.assertNotIn('isError',result,result)
        answer=self.value('context_result',{'request_id':'facts-1'})
        self.assertEqual(answer['answer'],'The API is ready for review.')
        self.assertEqual(answer['status'],'answered')
        self.assertEqual(answer['delivery_state'],'read')
        self.assertEqual(answer['sources'][0]['observed_at'],999)
        self.assertFalse(self.app.dispatcher.once())

    def test_duplicate_is_atomic_and_changed_question_rejected(self):
        self.prepare()
        with ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(lambda _:self.value('request_context',self.question),range(12)))
        self.assertEqual(sum(not r['duplicate'] for r in results),1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM messages').fetchone()[0],1)
        self.assertIn('another question',self.error('request_context',{**self.question,'question':'Different content'}))

    def test_missing_metadata_and_wrong_chat_cannot_retrieve_or_dispatch(self):
        self.prepare();self.consume();self.answer()
        self.error('context_result',{'request_id':'facts-1'},meta={})
        other={**self.meta,'openai/session':'another-chat'}
        self.error('context_result',{'request_id':'facts-1'},meta=other)
        response=self.value('request_context',self.question,meta=other)
        self.assertEqual(response['state'],'approval_required')
        self.assertFalse(response['submitted'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM messages').fetchone()[0],1)

    def test_only_context_tools_and_exact_resource_access(self):
        self.prepare()
        tools=self.http('POST',RESOURCE_PATH,{'id':1,'method':'tools/list'},token=self.requester)[1]['result']['tools']
        self.assertEqual({t['name'] for t in tools},{'context_destinations','request_context','context_result'})
        for name in ('inbox','send_message','reply','claim_message'):
            self.error(name)
        self.assertEqual(self.http('POST','/mcp/dot',{'id':1,'method':'tools/list'},token=self.requester)[0],401)
        self.assertEqual(self.http('POST',RESOURCE_PATH,{'id':1,'method':'tools/list'},token=self.dot)[0],401)
        self.assertEqual(self.http('POST','/v1/call',{'tool':'inbox'},token=self.requester)[0],401)
        self.error('request_context',{**self.question,'destination':'Codex'})
        self.error('request_context',{**self.question,'topic':'unapproved'})

    def test_explicit_unavailable_and_out_of_scope_are_terminal_without_sources(self):
        for status in ('unavailable','out_of_scope'):
            with self.subTest(status=status):
                self.prepare();self.consume()
                self.assertNotIn('isError',self.answer(status=status,answer='No authorized evidence for this question.',sources=[]))
                result=self.value('context_result',{'request_id':'facts-1'})
                self.assertEqual(result['state'],'completed')
                self.assertEqual(result['status'],status)
                self.assertEqual(result['sources'],[])
            if status=='unavailable':
                self.tearDown();self.setUp()

    def test_unapproved_sources_future_times_and_unread_answers_are_refused(self):
        self.prepare();self.consume()
        for reference in ('dot:unrelated-private','dot:project-context-evil','https://github.com/example/project-evil'):
            self.assertTrue(self.answer(sources=[dict(reference=reference,observed_at=999)])['isError'])
        for observed in (True,1001,float('nan')):
            self.assertTrue(self.answer(sources=[dict(reference='dot:project-context',observed_at=observed)])['isError'])
        self.assertTrue(self.answer(sources=[])['isError'])
        self.store.db.execute('UPDATE messages SET read_at=NULL WHERE id=?',(self.mid,))
        self.assertTrue(self.answer()['isError'])

    def test_receiver_source_child_boundaries_and_immutable_answer(self):
        self.prepare();self.consume()
        result=self.answer(sources=[dict(reference='https://github.com/example/project/blob/main/README.md',observed_at=999)])
        self.assertNotIn('isError',result)
        same=self.answer(sources=[dict(reference='https://github.com/example/project/blob/main/README.md',observed_at=999)])
        self.assertTrue(same['structuredContent']['duplicate'])
        self.assertTrue(self.answer(answer='A different answer')['isError'])

    def test_expiry_does_not_requeue_or_extend_on_duplicate(self):
        self.prepare();self.consume();self.now+=301
        self.assertTrue(self.answer()['isError'])
        expired=self.value('context_result',{'request_id':'facts-1'})
        self.assertEqual(expired['state'],'expired')
        duplicate=self.value('request_context',self.question)
        self.assertEqual(duplicate['expires'],expired['expires'])
        self.assertEqual(duplicate['state'],'expired')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM messages').fetchone()[0],1)

    def test_policy_change_revokes_old_chat_and_cannot_reassign_old_requests(self):
        self.prepare();self.consume();self.answer()
        changed={**self.policy,'scope':'A narrower revised factual scope'}
        self.assertEqual(self.owner_action('save_policy',**changed)[0],303)
        self.error('context_result',{'request_id':'facts-1'})
        pending=self.value('request_context',{**self.question,'request_id':'facts-new'})
        self.assertEqual(pending['state'],'approval_required')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM messages').fetchone()[0],1)

    def test_receiver_replacement_requires_new_owner_review(self):
        self.prepare()
        self.store.db.execute('UPDATE subscriptions SET url=? WHERE id=?',('https://callbacks.example/replacement',self.sid))
        self.assertIn('receiver unavailable or changed',self.error('request_context',self.question))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM messages').fetchone()[0],0)

    def test_revoke_one_chat_stops_work_but_preserves_normal_messaging_and_legacy_trial(self):
        self.prepare();self.value('request_context',self.question)
        grant=self.store.db.execute('SELECT id FROM ap_facts_grants').fetchone()[0]
        self.assertEqual(self.owner_action('revoke_chat',grant=grant)[0],303)
        self.error('context_result',{'request_id':'facts-1'})
        self.assertEqual(self.store.db.execute('SELECT stopped FROM conversations').fetchone()[0],1)
        self.assertFalse(self.app.context.active())
        self.assertEqual(self.dot_tool('list_peers')['self'],'dot')
        self.assertEqual(self.dot_tool('list_peers')['peers'][0]['id'],'grokbot')
        self.assertEqual(self.store.db.execute('SELECT active FROM subscriptions WHERE id=?',(self.sid,)).fetchone()[0],1)

    def test_disconnect_revokes_only_factual_tokens_and_keeps_records(self):
        self.prepare();self.consume();self.answer()
        self.assertEqual(self.owner_action('disconnect')[0],303)
        self.assertEqual(self.request('context_result',{'request_id':'facts-1'})[0],401)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM ap_facts_requests').fetchone()[0],1)
        self.assertEqual(self.dot_tool('list_peers')['self'],'dot')
        self.assertFalse(self.app.context.active())

    def test_owner_cookie_is_expiring_and_same_origin_required(self):
        self.prepare()
        self.assertEqual(self.http('POST','/context/actions',{'action':'disconnect'},origin='https://attacker.example',cookie=self.owner)[0],400)
        self.assertEqual(self.http('POST','/context/actions',{'action':'disconnect'},origin=self.config['public_url'])[0],303)
        self.assertTrue(self.app.factual.active())
        self.now+=901
        self.assertIn('type="password"',self.http('GET','/context',cookie=self.owner)[1])
        self.assertEqual(self.owner_action('disconnect')[0],303)
        self.assertTrue(self.app.factual.active())

    def test_owner_html_escapes_scope_question_and_never_displays_credentials(self):
        self.prepare(bind=False)
        self.value('request_context',{**self.question,'question':'<script>bad()</script>'})
        page=self.http('GET','/context',cookie=self.owner)[1]
        self.assertIn('&lt;script&gt;',page)
        self.assertNotIn('<script>',page)
        self.assertNotIn(self.config['owner_key'],page)
        self.assertNotIn(self.requester,page)

    def test_expired_pairing_and_changed_pairing_content_are_not_silently_renewed(self):
        self.prepare(bind=False)
        pending=self.value('request_context',self.question)
        self.error('request_context',{**self.question,'question':'Changed'})
        self.now+=601
        again=self.value('request_context',self.question)
        self.assertEqual(again['state'],'approval_expired')
        self.assertEqual(again['pairing_code'],pending['pairing_code'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM messages').fetchone()[0],0)
