"""Local sessions talk directly to their owner's personal-agent service.

Only a dedicated connector credential is kept on disk. No personal agent's key
or owner password is used for messaging. Requests never follow redirects or
retry sends; a bounded wait retrieves one existing conversation's status.
"""
import getpass
import json
import os
from pathlib import Path
import re
import socket
import stat
import time
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .config import connection
from .native import owned_path
from . import __version__


MAX_RESPONSE = 262144


class Refused(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def origin(value):
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        raise ValueError('Use your AgentPeers HTTPS origin without a path or credentials')
    return value.rstrip('/')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post(url, payload, key=None, form=False):
    body = urllib.parse.urlencode(payload).encode() if form else json.dumps(payload).encode()
    parsed = urllib.parse.urlsplit(url)
    headers = {'Content-Type': 'application/x-www-form-urlencoded' if form else 'application/json',
               'Origin': origin(parsed.scheme + '://' + parsed.netloc) if form else 'null',
               'User-Agent': 'AgentPeers/' + __version__, 'Accept': 'application/json'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    request = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(request, timeout=15) as result:
            raw = result.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        # Never include response bodies or URLs in transport exceptions.
        if exc.code == 403:
            raise Refused(403, 'Cloudflare or the service refused this HTTP client (403); no receiving-state conclusion or automatic retry. Check the client and service admission policy') from None
        raise Refused(exc.code, 'AgentPeers refused the request (HTTP %d); inspect the existing message before retrying' % exc.code) from None
    except (OSError, urllib.error.URLError, socket.timeout):
        raise ValueError('AgentPeers connection failed; send outcome may be unknown. Inspect the same message ID; do not resend automatically') from None
    if len(raw) > MAX_RESPONSE:
        raise ValueError('AgentPeers response exceeds the size limit')
    return json.loads(raw)


def config_path():
    return connection().mailbox_home / 'personal-client.json'


def validate(config):
    if not isinstance(config, dict) or set(config) != {'version', 'url', 'connector', 'key'} or config.get('version') != 1:
        raise ValueError('Invalid personal-agent connector configuration')
    origin(config['url'])
    if (not isinstance(config['connector'], str) or not re.fullmatch(r'local-[A-Za-z0-9._:-]{1,94}', config['connector'])
            or not isinstance(config['key'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', config['key'])):
        raise ValueError('Invalid local connector identity or credential')
    return config


def load():
    path = config_path()
    owned_path(path.parent, stat.S_ISDIR)
    if path.parent.stat().st_mode & 0o077:
        raise ValueError('Personal connector directory must be private')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise ValueError('Direct personal-agent messaging is not connected. Run agentpeers connect-dot once; do not route Dot messages through a helper Codex chat') from None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Personal connector must be a private owned regular file')
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValueError('Personal connector configuration exceeds the size limit')
    return validate(json.loads(raw))


def connect(url, connector, name, password=None):
    url = origin(url)
    path = config_path()
    if path.exists() or path.is_symlink():
        old = load()
        if (old['url'], old['connector']) != (url, connector):
            raise ValueError('A different personal connection exists; preserve it before changing connections')
        return {'connected': True, 'created': False, 'config_file': str(path)}
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    owned_path(path.parent, stat.S_ISDIR)
    if path.parent.stat().st_mode & 0o077:
        raise ValueError('Personal connector directory must be private')
    # Identity is never taken from an incoming peer message.
    config = validate(post(url + '/local/connect', {'owner_key': password if password is not None else getpass.getpass('Your AgentPeers password: '),
                                                  'connector': connector, 'name': name}, form=True))
    if (config['url'], config['connector']) != (url, connector):
        raise ValueError('Connection response does not match the requested service and connector')
    fd, temporary = tempfile.mkstemp(prefix='.personal-client-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(config, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # Complete create-if-absent; never overwrite active work.
    finally:
        os.unlink(temporary)
    return {'connected': True, 'created': True, 'config_file': str(path), 'credential_printed': False}


def call(session, tool, args, wait_seconds=0):
    runtime, ident = session.split(':', 1)
    if runtime not in ('claude', 'codex') or str(uuid.UUID(ident)) != ident:
        raise ValueError('Use your own local session identity')
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= 30:
        raise ValueError('wait_seconds must be 0–30')
    config = load()
    def request(name, fields):
        return post(config['url'] + '/v1/local', {'session': session, 'tool': name, 'arguments': fields}, config['key'])
    if tool == 'send_message':
        args = {**args, 'message_id': args.get('message_id') or str(uuid.uuid4())}
    try:
        result = request(tool, args)
    except ValueError as exc:
        if tool != 'send_message':
            raise
        return {'id': args['message_id'], 'state': 'unavailable' if isinstance(exc, Refused) and 400 <= exc.code < 500 else 'unconfirmed',
                'origin_session': session, 'error': str(exc), 'automatic_retry': False,
                'next_action': 'Inspect this same ID or personal-inbox from this same session; do not create another send.'}
    if wait_seconds and tool in ('send_message', 'message_status'):
        deadline = time.monotonic() + wait_seconds
        while not result.get('replies') and result.get('state') not in ('blocked', 'expired', 'unknown', 'stopped') and time.monotonic() < deadline:
            time.sleep(min(2, max(0, deadline - time.monotonic())))
            result = request('message_status', {'message_id': result['id']})
    result['route'] = 'direct-personal-agent'
    result['receipt_note'] = 'Reply content is retrieved directly into this originating session. Only acknowledge after consuming it. No desktop helper chat is involved.'
    return result
