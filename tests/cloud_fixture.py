"""Fixture transport only. This file must never appear in a production Worker bundle."""
import json
from urllib.parse import urlsplit
from workers import Response
from worker import Mailbox as RealMailbox, Default


class Mailbox(RealMailbox):
    async def post(self, url, body, headers):
        self.db.execute('CREATE TABLE IF NOT EXISTS fixture_deliveries(seq INTEGER PRIMARY KEY,payload TEXT,headers TEXT)')
        payload = json.loads(body)
        self.db.execute('INSERT INTO fixture_deliveries(payload,headers) VALUES(?,?)', (json.dumps(payload), json.dumps(headers)))
        return 200, json.dumps({'challenge': payload.get('challenge')}).encode()

    async def fetch(self, request):
        path = urlsplit(request.url).path
        if path == '/test/reset':
            await self.ctx.storage.deleteAlarm()
            self.db.execute('CREATE TABLE IF NOT EXISTS fixture_deliveries(seq INTEGER PRIMARY KEY,payload TEXT,headers TEXT)')
            for table in ('local_conversations','ap_facts_owner_sessions','ap_facts_requests','ap_facts_grants','ap_facts_candidates','ap_facts_clients','ap_facts_policies','ap_context_control','ap_context_requests','messages','conversations','subscriptions','audit','oauth_clients','oauth_codes','oauth_tokens','ap_limits','ap_subscription_status','ap_settings','fixture_deliveries'):
                if self.db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                    self.db.execute('DELETE FROM ' + table)
            self.app = None
            return Response('{}', headers={'Content-Type':'application/json'})
        if path == '/test/deliveries':
            rows = self.db.execute('SELECT payload,headers FROM fixture_deliveries ORDER BY seq').fetchall()
            return Response(json.dumps([{'payload':json.loads(r['payload']),'headers':json.loads(r['headers'])} for r in rows]), headers={'Content-Type':'application/json'})
        if path == '/test/alarm':
            alarm = await self.ctx.storage.getAlarm()
            return Response(json.dumps({'alarm':alarm}),headers={'Content-Type':'application/json'})
        if path == '/test/interrupted':
            # A deterministic crash boundary: the native request started but its
            # acceptance response was lost. Production exposes no such endpoint.
            self.db.execute("UPDATE messages SET state='dispatching',lease='interrupted',attempts=1 WHERE recipient='grokbot' AND state IN ('queued','accepted')")
            return Response('{}',headers={'Content-Type':'application/json'})
        if path == '/test/rollback':
            def fail():
                self.db.execute("INSERT INTO conversations VALUES('must-rollback','grokbot','dot','fixture',0,0)")
                raise RuntimeError('fixture transaction failure')
            try:
                self.app.store.transaction(fail)
            except RuntimeError:
                pass
            exists = self.db.execute("SELECT 1 FROM conversations WHERE id='must-rollback'").fetchone()
            return Response(json.dumps({'rolled_back':exists is None}),headers={'Content-Type':'application/json'})
        return await super().fetch(request)
