"""Factual research handoffs to one locally registered launcher.

OAuth requester identity is kept separate from Dot. Local credentials may grant
only their own session, and pairing requires the code from the intended chat.
"""
import json
from . import AUTHORITY
from .config import Rejected, identifier
from .context import bounded, fingerprint

SCHEMA = '''
CREATE TABLE IF NOT EXISTS ap_research_jobs(
 id TEXT PRIMARY KEY, connector TEXT, session TEXT, scope TEXT, title TEXT,
 expires REAL, requester TEXT, stopped INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS ap_research_requests(
 id TEXT PRIMARY KEY, request_id TEXT, job TEXT, identity TEXT, question TEXT,
 expires REAL, state TEXT, read_at REAL, result TEXT, reply_id TEXT, completed REAL);
CREATE TABLE IF NOT EXISTS ap_research_notifications(request TEXT PRIMARY KEY);
'''


class Research:
    def __init__(self, store):
        self.store = store
        store.db.executescript(SCHEMA)
        # A process may have died after native submission. Preserve uncertainty;
        # never return these rows to the pending queue on restart.
        store.db.execute("UPDATE ap_research_requests SET state='unconfirmed' WHERE state='dispatching'")

    def identity(self, client, metadata):
        return json.dumps(self.store.factual._identity(client, metadata), separators=(',', ':'))

    def key(self, identity, request_id):
        identifier(request_id)
        return fingerprint([identity, request_id])

    def has_request(self, client, metadata, request_id):
        identity = self.identity(client, metadata)
        return bool(self.store.db.execute('SELECT 1 FROM ap_research_requests WHERE id=?',
                                          (self.key(identity, request_id),)).fetchone())

    def job(self, ident):
        row = self.store.db.execute('SELECT * FROM ap_research_jobs WHERE id=?', (ident,)).fetchone()
        if not row:
            raise Rejected('Unknown research handoff; obtain it from the launching session')
        return row

    def live(self, job):
        return not job['stopped'] and job['expires'] > self.store.clock()

    def result(self, row):
        job = self.job(row['job'])
        state = ('stopped' if job['stopped'] else 'expired' if not self.live(job) or row['expires'] <= self.store.clock()
                 else row['state'])
        value = {'request_id': row['request_id'], 'state': state, 'destination': 'Codex',
                 'launcher': job['title'], 'expires': min(job['expires'], row['expires']),
                 'read_at': row['read_at'], 'authority': AUTHORITY}
        if state == 'approval_required':
            value.update(pairing_code=row['id'][:12].upper(), submitted=False,
                         note='Give this code to the launching Codex session. It must approve this exact research chat before delivery. Do not use Dot or a generic machine peer.')
        elif state == 'completed':
            value.update(json.loads(row['result']), responding_agent=job['session'], completed_at=row['completed'])
        return value

    def call(self, client, name, args, metadata):
        s = self.store
        identity = self.identity(client, metadata)
        key = self.key(identity, args['request_id'])
        with s.lock:
            def operation():
                old = s.db.execute('SELECT * FROM ap_research_requests WHERE id=?', (key,)).fetchone()
                if name == 'context_result':
                    if not old:
                        raise Rejected('No research request belongs to this chat and ID')
                    return self.result(old)
                if args['destination'] != 'Codex':
                    raise Rejected('Use the exact Codex research handoff destination')
                job = self.job(args['topic'])
                question = bounded(args['question'], 'factual research question', 4000)
                if old:
                    if old['job'] != job['id'] or old['question'] != question:
                        raise Rejected('Request ID belongs to different content; no resend')
                    return {**self.result(old), 'duplicate': True}
                if not self.live(job):
                    raise Rejected('Research handoff stopped or expired')
                if job['requester'] and not s.factual.token_client_allowed(json.loads(job['requester'])[0]):
                    raise Rejected('Paired research requester access was revoked')
                if job['requester'] not in (None, identity):
                    raise Rejected('Research handoff is paired with a different chat')
                if s.db.execute('SELECT count(*) FROM ap_research_requests WHERE job=?', (job['id'],)).fetchone()[0] >= 20:
                    raise Rejected('Research handoff question limit reached')
                if s.db.execute('SELECT count(*) FROM ap_research_requests').fetchone()[0] >= 5000:
                    raise Rejected('Research capacity reached; preserve existing evidence')
                state = 'pending' if job['requester'] == identity else 'approval_required'
                s.db.execute('INSERT INTO ap_research_requests VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL,NULL)',
                             (key, args['request_id'], job['id'], identity, question,
                              min(job['expires'], s.clock() + 600), state))
                return self.result(s.db.execute('SELECT * FROM ap_research_requests WHERE id=?', (key,)).fetchone())
            return s.transaction(operation)

    def local(self, connector, session, name, args):
        s = self.store
        with s.lock:
            def operation():
                if name == 'research_register':
                    if set(args) != {'id', 'scope', 'title'} or not session.startswith('codex:'):
                        raise Rejected('Register an exact local Codex launcher with id, scope and title')
                    ident = identifier(args['id'])
                    scope = bounded(args['scope'], 'human-authorized factual scope', 2000)
                    title = bounded(args['title'], 'launcher display name', 200)
                    old = s.db.execute('SELECT * FROM ap_research_jobs WHERE id=?', (ident,)).fetchone()
                    if old:
                        if tuple(old[k] for k in ('connector', 'session', 'scope', 'title')) != (connector, session, scope, title):
                            raise Rejected('Handoff ID belongs to a different launcher or scope')
                        job = old
                    else:
                        if s.db.execute('SELECT count(*) FROM ap_research_jobs').fetchone()[0] >= 1000:
                            raise Rejected('Handoff capacity reached; preserve existing records')
                        s.db.execute('INSERT INTO ap_research_jobs VALUES(?,?,?,?,?,?,NULL,0)',
                                     (ident, connector, session, scope, title, s.clock() + 14400))
                        job = self.job(ident)
                    return {'id': job['id'], 'destination': 'Codex', 'topic': job['id'], 'launcher': session,
                            'expires': job['expires'], 'scope': scope, 'paired': job['requester'] is not None,
                            'requester_app': 'AgentPeers Context', 'note': 'Only factual questions in this scope; pairing grants no publishing, spending, access changes or implementation.'}
                if 'id' not in args:
                    raise Rejected('Exact handoff id is required')
                job = self.job(args['id'])
                if (job['connector'], job['session']) != (connector, session):
                    raise Rejected('Handoff belongs to another local session')
                if name == 'research_close' and set(args) == {'id'}:
                    s.db.execute('UPDATE ap_research_jobs SET stopped=1 WHERE id=?', (job['id'],))
                    return {'id': job['id'], 'state': 'stopped', 'evidence_preserved': True}
                if not self.live(job):
                    raise Rejected('Research handoff stopped or expired')
                if job['requester'] and not s.factual.token_client_allowed(json.loads(job['requester'])[0]):
                    raise Rejected('Paired research requester access was revoked')
                if name == 'research_pair' and set(args) == {'id', 'pairing_code'}:
                    code = bounded(args['pairing_code'], 'pairing code', 12).upper()
                    rows = s.db.execute("SELECT * FROM ap_research_requests WHERE job=? AND substr(upper(id),1,12)=? AND expires>?",
                                        (job['id'], code, s.clock())).fetchall()
                    if len(rows) != 1:
                        raise Rejected('No unique active pairing matches this handoff and code')
                    row = rows[0]
                    if not s.factual.token_client_allowed(json.loads(row['identity'])[0]):
                        raise Rejected('Research requester access was revoked')
                    if job['requester'] not in (None, row['identity']):
                        raise Rejected('Handoff already paired; never replace its requester')
                    s.db.execute('UPDATE ap_research_jobs SET requester=? WHERE id=?', (row['identity'], job['id']))
                    s.db.execute("UPDATE ap_research_requests SET state='pending' WHERE id=? AND state='approval_required'", (row['id'],))
                    return {'id': job['id'], 'paired': True, 'request': row['id'], 'launcher': session}
                if name == 'research_pending' and set(args) == {'id'}:
                    rows = s.db.execute("SELECT * FROM ap_research_requests WHERE job=? AND state='pending' AND expires>? ORDER BY rowid LIMIT 20", (job['id'], s.clock())).fetchall()
                    pairs = s.db.execute("SELECT id,question FROM ap_research_requests WHERE job=? AND state='approval_required' AND expires>? AND id NOT IN (SELECT request FROM ap_research_notifications) ORDER BY rowid LIMIT 20", (job['id'], s.clock())).fetchall()
                    return {'requests': [dict(r) for r in rows], 'pairing': [{'request':r['id'], 'pairing_code':r['id'][:12].upper(), 'question':r['question']} for r in pairs], 'launcher': session, 'scope': job['scope'], 'expires': job['expires']}
                if name == 'research_pair_notice' and set(args) == {'id', 'request'}:
                    row = s.db.execute("SELECT id FROM ap_research_requests WHERE id=? AND job=? AND state='approval_required' AND expires>?", (args['request'], job['id'], s.clock())).fetchone()
                    old = s.db.execute('SELECT 1 FROM ap_research_notifications WHERE request=?', (args['request'],)).fetchone()
                    claimed = bool(row and not old)
                    if claimed: s.db.execute('INSERT INTO ap_research_notifications VALUES(?)', (row['id'],))
                    return {'claimed': claimed}
                if name in ('research_claim', 'research_delivery', 'research_read', 'research_answer'):
                    row = s.db.execute('SELECT * FROM ap_research_requests WHERE id=? AND job=?', (args.get('request'), job['id'])).fetchone()
                    if not row or row['expires'] <= s.clock() or row['identity'] != job['requester']:
                        raise Rejected('No active request belongs to this paired handoff')
                    if name == 'research_claim' and set(args) == {'id', 'request'}:
                        claimed = row['state'] == 'pending'
                        if claimed:
                            s.db.execute("UPDATE ap_research_requests SET state='dispatching' WHERE id=?", (row['id'],))
                        return {'claimed': claimed, 'request': row['id']}
                    if name == 'research_delivery' and set(args) == {'id', 'request', 'state'}:
                        if args['state'] not in ('submitted', 'unavailable', 'unconfirmed'):
                            raise Rejected('Invalid native delivery state')
                        if row['state'] == 'dispatching':
                            s.db.execute('UPDATE ap_research_requests SET state=? WHERE id=?', (args['state'], row['id']))
                        return self.result(s.db.execute('SELECT * FROM ap_research_requests WHERE id=?', (row['id'],)).fetchone())
                    if name == 'research_read' and set(args) == {'id', 'request'}:
                        if row['state'] not in ('dispatching', 'submitted', 'unconfirmed', 'processing', 'completed'):
                            raise Rejected('No submitted research question to acknowledge')
                        s.db.execute("UPDATE ap_research_requests SET read_at=coalesce(read_at,?),state=CASE WHEN state='completed' THEN state ELSE 'processing' END WHERE id=?", (s.clock(), row['id']))
                        return {'read': True, 'request': row['id']}
                    if name == 'research_answer' and set(args) == {'id', 'request', 'answer', 'sources', 'reply_id', 'status'}:
                        if row['read_at'] is None:
                            raise Rejected('Consume and acknowledge the actual question before answering')
                        if args['status'] not in ('answered', 'unavailable', 'out_of_scope'):
                            raise Rejected('Invalid factual result status')
                        answer = bounded(args['answer'], 'factual answer or refusal', 8000)
                        sources = args['sources']
                        if not isinstance(sources, list) or len(sources) > 5 or (args['status'] == 'answered' and not sources):
                            raise Rejected('An answer needs 1–5 source references; a refusal may use none')
                        for ref in sources:
                            bounded(ref, 'source reference', 2000)
                        reply = identifier(args['reply_id'])
                        result = json.dumps({'status': args['status'], 'answer': answer, 'sources': sources}, sort_keys=True)
                        if row['result'] is not None:
                            if (row['result'], row['reply_id']) != (result, reply):
                                raise Rejected('A different answer already exists; do not replace it')
                        else:
                            s.db.execute("UPDATE ap_research_requests SET state='completed',result=?,reply_id=?,completed=? WHERE id=?", (result, reply, s.clock(), row['id']))
                        return self.result(s.db.execute('SELECT * FROM ap_research_requests WHERE id=?', (row['id'],)).fetchone())
                raise Rejected('Unexpected research tool or arguments')
            return s.transaction(operation)
