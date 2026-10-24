"""Run the production Python Worker in workerd with fixture-only native transports."""
import base64
import hashlib
import html
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit

WRANGLER = os.environ.get('AGENTPEERS_WRANGLER')
SCRATCH = os.environ.get('AGENTPEERS_TEST_SCRATCH')


@unittest.skipUnless(WRANGLER and SCRATCH, 'Cloud runtime checks need Wrangler and an admitted scratch directory')
class CloudRuntime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.temp = tempfile.TemporaryDirectory(prefix='agentpeers-cloud-', dir=SCRATCH)
        cls.path = Path(cls.temp.name)
        runtime = root / '.cloud-build/runtime'
        assert (runtime / 'worker.py').exists(), 'Run scripts/prepare_cloud.py first'
        shutil.copytree(runtime, cls.path / 'runtime')
        shutil.copytree(root / 'python_modules', cls.path / 'python_modules')
        shutil.copy2(root / 'tests/cloud_fixture.py', cls.path / 'runtime/fixture.py')
        (cls.path / '.dev.vars').write_text('AGENTPEERS_OWNER_KEY="' + 'o' * 40 + '"\n')
        config = {'name':'agentpeers-cloud-fixture','main':'runtime/fixture.py','base_dir':'runtime','compatibility_date':'2026-10-22','compatibility_flags':['python_workers'],'durable_objects':{'bindings':[{'name':'MAILBOX','class_name':'Mailbox'}]},'migrations':[{'tag':'v1','new_sqlite_classes':['Mailbox']}],'observability':{'enabled':False}}
        (cls.path / 'wrangler.json').write_text(json.dumps(config))
        with socket.socket() as reserve:
            reserve.bind(('127.0.0.1',0)); cls.port = reserve.getsockname()[1]
        cls.log = (cls.path / 'runtime.log').open('w+')
        env = {k:v for k,v in os.environ.items() if not k.startswith('CLOUDFLARE_')}
        env.update(WRANGLER_SEND_METRICS='false', XDG_CONFIG_HOME=str(cls.path / 'tool-config'), CI='true')
        cls.process = subprocess.Popen([WRANGLER,'dev','--local','--ip','127.0.0.1','--port',str(cls.port),'--inspector-port','0','--log-level','error','--persist-to',str(cls.path / 'state')],cwd=cls.path,env=env,stdout=cls.log,stderr=subprocess.STDOUT)
        cls.addClassCleanup(cls.close_runtime)
        deadline=time.monotonic()+25
        while True:
            try:
                connection=http.client.HTTPConnection('127.0.0.1',cls.port,timeout=2)
                connection.request('GET','/setup');r=connection.getresponse();raw=r.read();connection.close()
                if r.status==200:break
                raise OSError('Worker returned error')
            except OSError:
                if cls.process.poll() is not None or time.monotonic()>deadline:
                    cls.log.seek(0);raise AssertionError('Cloud fixture failed to start: '+cls.log.read()[-2000:])
                time.sleep(.1)

    @classmethod
    def close_runtime(cls):
        if cls.process.poll() is None:cls.process.terminate()
        cls.process.wait(timeout=10); cls.log.close(); cls.temp.cleanup()

    def request(self,method,path,body=None,token=None,headers=None):
        h=dict(headers or {})
        if token:h['Authorization']='Bearer '+token
        if body is not None and not isinstance(body,str):body=json.dumps(body);h['Content-Type']='application/json'
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=8)
        connection.request(method,path,body=body,headers=h)
        r=connection.getresponse();status,headers,raw=r.status,r.headers,r.read();connection.close()
        return status,headers,json.loads(raw) if raw and 'application/json' in headers.get('Content-Type','') else raw.decode()

    def setUp(self):
        self.request('POST','/test/reset',{})
        status,headers,_=self.request('GET','/setup')
        self.assertEqual(status,200)
        self.assertEqual(headers['Referrer-Policy'],'strict-origin')
        self.config={'owner_key':'o'*40,'public_url':'https://bridge.example'}
        self.setup_args={'owner_key':'o'*40,'public_url':'https://bridge.example','webhook_url':'https://vendor.example/routine','webhook_key':'fixture-only','callback_hosts':'callbacks.example'}
        status,_,page=self.request('POST','/setup',urlencode(self.setup_args),headers={'Content-Type':'application/x-www-form-urlencoded','Origin':'http://127.0.0.1:'+str(self.port)})
        self.assertEqual(status,200,page)
        self.grok=re.search('<pre>([^<]+)</pre>',page)[1]

    def connect_dot(self, resource_path='/mcp/dot', scope='agentpeers:messages'):
        status,_,client=self.request('POST','/oauth/register',{'redirect_uris':['https://chatgpt.com/connector/oauth/test']});self.assertEqual(status,201)
        verifier='v'*43
        args={'client_id':client['client_id'],'redirect_uri':client['redirect_uris'][0],'resource':'https://bridge.example'+resource_path,'scope':scope,'response_type':'code','code_challenge_method':'S256','code_challenge':base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('='),'state':'return-state'}
        status,approval_headers,page=self.request('GET','/oauth/authorize?'+urlencode(args));self.assertEqual(status,200)
        self.assertEqual(approval_headers['Content-Security-Policy'], "default-src 'none'; form-action 'self' https://chatgpt.com; frame-ancestors 'none'; base-uri 'none'")
        self.assertIn('<form method="post" action="/oauth/authorize">', page)
        fields={html.unescape(k):html.unescape(v) for k,v in re.findall('type="hidden" name="([^"]+)" value="([^"]*)"',page)}
        status,headers,_=self.request('POST','/oauth/authorize',urlencode({**fields,'owner_key':'o'*40}),headers={'Content-Type':'application/x-www-form-urlencoded','Origin':'https://bridge.example'});self.assertEqual(status,303)
        code=parse_qs(urlsplit(headers['Location']).query)['code'][0]
        status,_,token=self.request('POST','/oauth/token',urlencode({'grant_type':'authorization_code','code':code,'code_verifier':verifier,'redirect_uri':args['redirect_uri'],'client_id':args['client_id'],'resource':args['resource']}),headers={'Content-Type':'application/x-www-form-urlencoded'});self.assertEqual(status,200,token)
        return token['access_token']

    def rpc(self,token,method,params=None):
        status,_,value=self.request('POST','/mcp/dot',{'jsonrpc':'2.0','id':1,'method':method,'params':params or {}},token)
        self.assertEqual(status,200,value);self.assertNotIn('error',value);return value['result']

    def tool(self,token,name,**args):
        result=self.rpc(token,'tools/call',{'name':name,'arguments':args});self.assertNotIn('isError',result);return result['structuredContent']

    def subscribe(self,token):
        return self.rpc(token,'events/subscribe',{'name':'message.created','arguments':{'recipient':'dot'},'delivery':{'mode':'webhook','url':'https://callbacks.example/event','secret':'whsec_'+base64.b64encode(b'k'*32).decode()}})

    def delivery(self,mid):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            for row in self.request('GET','/test/deliveries')[2]:
                p=row['payload']
                if p.get('message_id')==mid or p.get('eventId')==mid:return row
            time.sleep(.05)
        self.fail('Native fixture notification did not arrive for '+mid)

    def test_dot_to_grok_and_followup_are_actually_consumed(self):
        token=self.connect_dot();self.subscribe(token)
        self.tool(token,'send_message',to='Grokbot',message='Which interface wakes you?',scope='Fixture conversation',message_id='cloud-question')
        callback=urlsplit(self.delivery('cloud-question')['payload']['callback_url']).path
        self.assertTrue(self.request('POST',callback,{'action':'claim'})[2]['claimed'])
        self.request('POST',callback,{'action':'acknowledge'})
        self.assertEqual(self.request('POST',callback,{'action':'reply','message':'A native routine webhook. Should I ask a follow-up?','reply_id':'cloud-answer','expects':'reply'})[0],200)
        self.delivery('cloud-answer')
        incoming=self.tool(token,'claim_message',message_id='cloud-answer');self.assertIn('native routine',incoming['body'])
        self.tool(token,'acknowledge',message_id='cloud-answer')
        self.tool(token,'reply',message_id='cloud-answer',message='Yes; this is the final answer.',reply_id='cloud-final')
        callback=urlsplit(self.delivery('cloud-final')['payload']['callback_url']).path
        self.request('POST',callback,{'action':'claim'});self.request('POST',callback,{'action':'acknowledge'})
        self.assertEqual(self.tool(token,'message_status',message_id='cloud-final')['state'],'read')

    def test_grok_initiates_and_dot_replies(self):
        token=self.connect_dot();self.subscribe(token)
        status,_,sent=self.request('POST','/v1/call',{'tool':'send_message','arguments':{'to':'Dot','message':'How do events wake you?','scope':'Fixture conversation','message_id':'grok-first'}},self.grok);self.assertEqual(status,200,sent)
        self.delivery('grok-first')
        incoming=self.tool(token,'claim_message',message_id='grok-first');self.assertTrue(incoming['claimed'])
        self.tool(token,'acknowledge',message_id='grok-first')
        self.tool(token,'reply',message_id='grok-first',message='An MCP event wakes this existing agent.',reply_id='dot-answer')
        callback=urlsplit(self.delivery('dot-answer')['payload']['callback_url']).path
        self.request('POST',callback,{'action':'claim'});self.request('POST',callback,{'action':'acknowledge'})
        self.assertEqual(self.tool(token,'message_status',message_id='dot-answer')['state'],'read')

    def test_missing_subscription_waits_for_a_change_instead_of_polling(self):
        token=self.connect_dot()
        status,_,_=self.request('POST','/v1/call',{'tool':'send_message','arguments':{'to':'Dot','message':'Wait for a receiver','scope':'Fixture','message_id':'wait-sub'}},self.grok);self.assertEqual(status,200)
        alarm=self.request('GET','/test/alarm')[2]['alarm']
        self.assertGreater(alarm,time.time()*1000+60000)
        self.subscribe(token);self.delivery('wait-sub')
        self.assertTrue(self.tool(token,'claim_message',message_id='wait-sub')['claimed'])
        self.assertFalse(self.tool(token,'claim_message',message_id='wait-sub')['claimed'])

    def test_saved_message_and_oauth_grant_survive_runtime_restart(self):
        token=self.connect_dot()
        status,_,_=self.request('POST','/v1/call',{'tool':'send_message','arguments':{'to':'Dot','message':'Survive a cold restart','scope':'Fixture','message_id':'after-restart'}},self.grok)
        self.assertEqual(status,200)
        self.restart_runtime()
        self.assertEqual(self.tool(token,'message_status',message_id='after-restart')['body'],'Survive a cold restart')
        self.subscribe(token);self.delivery('after-restart')
        self.assertTrue(self.tool(token,'claim_message',message_id='after-restart')['claimed'])

    def restart_runtime(self):
        cls=type(self)
        cls.process.terminate();cls.process.wait(timeout=10)
        env={k:v for k,v in os.environ.items() if not k.startswith('CLOUDFLARE_')}
        env.update(WRANGLER_SEND_METRICS='false',XDG_CONFIG_HOME=str(cls.path/'tool-config'),CI='true')
        cls.process=subprocess.Popen([WRANGLER,'dev','--local','--ip','127.0.0.1','--port',str(cls.port),'--inspector-port','0','--log-level','error','--persist-to',str(cls.path/'state')],cwd=cls.path,env=env,stdout=cls.log,stderr=subprocess.STDOUT)
        deadline=time.monotonic()+20
        while True:
            try:
                status,_,_=self.request('GET','/setup')
                if status==200:break
            except OSError:
                pass
            self.assertLess(time.monotonic(),deadline)
            time.sleep(.1)

    def test_interrupted_grok_submission_is_unknown_and_is_not_replayed(self):
        token=self.connect_dot()
        # The native fixture leaves dispatching rows, then a full runtime restart
        # exercises the production recovery logic against persistent DO storage.
        self.tool(token,'send_message',to='Grokbot',message='Check uncertain native acceptance',scope='Fixture',message_id='uncertain-grok')
        self.delivery('uncertain-grok')
        # Mark the already-submitted request at the exact lost-response boundary.
        # This avoids a real external request or a timing-sensitive process kill.
        self.request('POST','/test/interrupted',{})
        self.restart_runtime()
        saved=self.tool(token,'message_status',message_id='uncertain-grok')
        self.assertEqual(saved['state'],'unknown')
        before=len(self.request('GET','/test/deliveries')[2])
        self.assertIsNone(self.request('GET','/test/alarm')[2]['alarm'])
        self.assertEqual(len(self.request('GET','/test/deliveries')[2]),before)

    def test_oversized_body_is_rejected_before_tool_execution(self):
        status,_,_=self.request('POST','/v1/call','x'*262145,self.grok,{'Content-Type':'application/json'})
        self.assertEqual(status,413)

    def test_native_transaction_rolls_back_an_interrupted_write(self):
        self.assertTrue(self.request('POST','/test/rollback',{})[2]['rolled_back'])

    def test_new_setup_uses_verified_dot_host_without_widening_saved_choices(self):
        self.request('POST','/test/reset',{})
        headers={'Content-Type':'application/x-www-form-urlencoded','Origin':'http://127.0.0.1:'+str(self.port)}
        owner=urlencode({'owner_key':'o'*40})
        initial=self.request('POST','/status',owner,headers=headers)[2]
        self.assertFalse(initial['configured'])
        self.assertEqual(initial['active_receivers'],[])
        args={**self.setup_args,'callback_hosts':''}
        status,_,page=self.request('POST','/setup',urlencode(args),headers=headers)
        self.assertEqual(status,200)
        self.assertIn('Bridge settings saved',page)
        self.assertIn('1. Connect Dot',page)
        self.assertIn('2. Give Grokbot its messaging key',page)
        saved=self.request('POST','/status',owner,headers=headers)[2]
        self.assertEqual(saved['approved_callback_hosts'],['connectors.api.openai.com'])
        # Explicit owner choices replace the starter; blank updates preserve them.
        self.assertEqual(self.request('POST','/setup',urlencode({**args,'callback_hosts':'tenant-notify.example'}),headers=headers)[0],200)
        self.assertEqual(self.request('POST','/setup',urlencode(args),headers=headers)[0],200)
        saved=self.request('POST','/status',owner,headers=headers)[2]
        self.assertEqual(saved['approved_callback_hosts'],['tenant-notify.example'])

    def test_owner_setup_cannot_be_changed_by_peer_or_foreign_origin(self):
        args={**self.setup_args,'owner_key':self.grok}
        status,_,_=self.request('POST','/setup',urlencode(args),headers={'Content-Type':'application/x-www-form-urlencoded','Origin':'http://127.0.0.1:'+str(self.port)});self.assertEqual(status,400)
        status,_,_=self.request('POST','/setup',urlencode(self.setup_args),headers={'Content-Type':'application/x-www-form-urlencoded','Origin':'https://outside.example'});self.assertEqual(status,400)
        status,_,_=self.request('POST','/setup',urlencode(self.setup_args),headers={'Content-Type':'application/x-www-form-urlencoded','Origin':'null'});self.assertEqual(status,400)
        self.assertEqual(self.request('POST','/mcp/dot',{'id':1,'method':'tools/list'},self.grok)[0],401)

    def test_owner_receiving_diagnostics_are_authenticated_bounded_and_secret_free(self):
        headers={'Content-Type':'application/x-www-form-urlencoded','Origin':'http://127.0.0.1:'+str(self.port)}
        def status(key='o'*40, override=None):
            return self.request('POST','/status',urlencode({'owner_key':key}),headers={**headers, **(override or {})})
        self.assertEqual(self.request('GET','/status')[0],400)
        self.assertEqual(status(self.grok)[0],400)
        self.assertEqual(status(override={'Origin':'https://outside.example'})[0],400)
        self.assertEqual(status(override={'Origin':'null'})[0],400)
        self.assertIsNone(status()[2]['last_subscription_attempt'])
        params={'name':'message.created','arguments':{'recipient':'dot'},'delivery':{'mode':'webhook','url':'https://outside.example/do-not-record-this-token','secret':'whsec_'+base64.b64encode(b'k'*32).decode()}}
        request={'jsonrpc':'2.0','id':1,'method':'events/subscribe','params':params}
        self.assertEqual(self.request('POST','/mcp/dot',request)[0],401)
        self.assertIsNone(status()[2]['last_subscription_attempt'])
        token=self.connect_dot()
        result=self.request('POST','/mcp/dot',request,token)[2]
        self.assertIn('observed hostname: outside.example',result['error']['message'])
        saved=status()[2]
        self.assertEqual(saved['last_subscription_attempt']['callback_host'],'outside.example')
        self.assertEqual(saved['last_subscription_attempt']['outcome'],'callback_host_not_approved')
        self.assertEqual(saved['active_receivers'],[])
        self.subscribe(token)
        code,_,saved=status()
        self.assertEqual(code,200)
        self.assertEqual(saved['last_subscription_attempt']['outcome'],'subscribed')
        self.assertEqual(saved['active_receivers'][0]['peer'],'dot')
        self.assertEqual(saved['active_receivers'][0]['callback_host'],'callbacks.example')
        serialized=json.dumps(saved)
        for secret in ('do-not-record-this-token',params['delivery']['secret'],'o'*40,self.grok,token):
            self.assertNotIn(secret,serialized)
        self.assertNotIn('https://',serialized)

    def test_owner_update_preserves_the_peers_key_and_saved_mailbox(self):
        status,_,_=self.request('POST','/v1/call',{'tool':'send_message','arguments':{'to':'Dot','message':'Keep this message','scope':'Fixture','message_id':'preserved'}},self.grok);self.assertEqual(status,200)
        status,_,page=self.request('POST','/setup',urlencode(self.setup_args),headers={'Content-Type':'application/x-www-form-urlencoded','Origin':'http://127.0.0.1:'+str(self.port)});self.assertEqual(status,200)
        self.assertEqual(re.search('<pre>([^<]+)</pre>',page)[1],self.grok)
        status,_,saved=self.request('POST','/v1/call',{'tool':'message_status','arguments':{'message_id':'preserved'}},self.grok)
        self.assertEqual(status,200);self.assertEqual(saved['body'],'Keep this message')

    def test_context_trial_oauth_dot_return_and_scoped_rollback(self):
        from agentpeers.personal.context_bridge import QUESTION, SOURCE
        dot = self.connect_dot()
        sid = self.subscribe(dot)['id']
        def admin(action, **extra):
            code, _, value = self.request('POST', '/context/admin', {'owner_key':'o'*40, 'action':action, **extra}, headers={'Origin':'https://bridge.example'})
            self.assertEqual(code, 200, value)
            return value
        self.assertTrue(admin('enable', subscription_id=sid)['enabled'])
        requester = self.connect_dot('/mcp/context-requester', 'agentpeers:context')
        metadata = {'openai/subject':'fixture-subject', 'openai/session':'fixture-session'}
        arguments = {'request_id':'cloud-card-1', 'destination':'Dot', 'topic':'integration-check', 'question':QUESTION}
        def context(name, args, meta=None):
            code, _, value = self.request('POST', '/mcp/context-requester', {'id':1, 'method':'tools/call', 'params':{'name':name, 'arguments':args, '_meta':metadata if meta is None else meta}}, requester)
            self.assertEqual(code, 200, value)
            return value['result']
        self.assertEqual(context('request_context', arguments)['structuredContent']['state'], 'binding_required')
        admin('bind', subject=metadata['openai/subject'], session=metadata['openai/session'])
        pending = context('request_context', arguments)['structuredContent']
        self.assertEqual(pending['state'], 'pending')
        self.assertTrue(context('request_context', arguments)['structuredContent']['duplicate'])
        self.assertTrue(context('context_result', {'request_id':'cloud-card-1'}, {**metadata,'openai/session':'other'})['isError'])
        deadline = time.monotonic() + 5
        mid = None
        while time.monotonic() < deadline:
            events = self.request('GET', '/test/deliveries')[2]
            matches = [event['payload'] for event in events if event['payload'].get('data', {}).get('sender') == 'context-requester-1']
            if matches:
                mid = matches[0]['data']['message_id']
                break
            time.sleep(.05)
        self.assertIsNotNone(mid, 'Context event did not reach the native fixture')
        claimed = self.tool(dot, 'claim_message', message_id=mid)
        self.assertTrue(claimed['claimed'])
        self.tool(dot, 'read_message', message_id=mid)
        self.tool(dot, 'acknowledge', message_id=mid)
        body = {'request_id':'cloud-card-1','answer':'amber','sources':[{'reference':SOURCE,'observed_at':time.time()-1}]}
        answer = self.tool(dot, 'reply', message_id=mid, reply_id='cloud-card-answer', message=json.dumps(body))
        self.assertEqual(answer['state'], 'completed')
        self.assertEqual(context('context_result', {'request_id':'cloud-card-1'})['structuredContent']['answer'], 'amber')
        self.assertFalse(admin('disable')['enabled'])
        self.assertEqual(self.request('POST','/mcp/context-requester',{'id':1,'method':'tools/list'},requester)[0],401)
        self.assertEqual(self.tool(dot,'list_peers')['self'], 'dot')
