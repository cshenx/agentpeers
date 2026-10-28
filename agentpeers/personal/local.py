"""A separately authenticated local connector; replies stay with their origin.

Local sessions share one trusted OS account and credential. Session binding
prevents accidental cross-chat collection, not a hostile process on that account.
No local desktop receiver or personal-agent credential is used.
"""
import copy
import secrets
import uuid

from .config import Rejected, identifier, validate


class LocalSessions:
    def __init__(self, store, protocol):
        self.store, self.protocol = store, protocol
        store.db.executescript("""CREATE TABLE IF NOT EXISTS local_conversations(
            conversation_id TEXT PRIMARY KEY, connector TEXT, session TEXT);""")

    @staticmethod
    def connection(config, connector, name):
        identifier(connector)
        if not connector.startswith("local-") or not isinstance(name, str) or not name.strip():
            raise Rejected("Use a local- connector ID and a display name")
        if config["peers"].get("dot", {}).get("platform") != "dot":
            raise Rejected("Connect the existing Dot receiver first")
        result = copy.deepcopy(config)
        old = result["peers"].get(connector)
        if old and (old.get("platform") != "local" or old.get("name") != name):
            raise Rejected("Connector already exists with different settings; preserve it")
        if not old:
            result["peers"][connector] = {"platform": "local", "name": name,
                                           "key": secrets.token_urlsafe(32)}
        if not any(set(p) == {connector, "dot"} for p in result["connections"]):
            result["connections"].append([connector, "dot"])
        validate(result)
        return result

    def call(self, connector, session, name, args):
        s = self.store
        if s.config["peers"].get(connector, {}).get("platform") != "local":
            raise Rejected("A dedicated local connector credential is required")
        try:
            runtime, ident = session.split(":", 1)
            if runtime not in ("claude", "codex") or str(uuid.UUID(ident)) != ident:
                raise ValueError()
        except (ValueError, AttributeError):
            raise Rejected("Use the calling local session's runtime:UUID") from None
        if not isinstance(args, dict):
            raise Rejected("Arguments must be an object")
        with s.lock:
            if name.startswith('research_'):
                return s.research.local(connector, session, name, args)
            if name == 'inbox':
                if args:
                    raise Rejected('inbox takes no arguments')
                rows = s.db.execute('SELECT l.conversation_id FROM local_conversations l JOIN messages m ON m.id=l.conversation_id WHERE l.connector=? AND l.session=? ORDER BY m.created DESC LIMIT 100', (connector, session)).fetchall()
                return {'conversations': [self.call(connector, session, 'message_status', {'message_id': row['conversation_id']}) for row in rows]}
            if name == "list_peers":
                if args:
                    raise Rejected("list_peers takes no arguments")
                return {"self": session, "connector": connector, "peers": s.peers(connector),
                        "receiving": "Replies are retrieved by their originating session; no idle-chat wake is promised."}
            if name == "send_message":
                mid = identifier(args.get("message_id"))
                old = s.db.execute("SELECT * FROM local_conversations WHERE conversation_id=?", (mid,)).fetchone()
                if old and (old["connector"], old["session"]) != (connector, session):
                    raise Rejected("Message belongs to another local session")
                if not old and s.db.execute("SELECT 1 FROM messages WHERE id=?", (mid,)).fetchone():
                    raise Rejected("Existing message has no local-session binding; preserve it")
                # Reserve first: a crash must never let another session claim the ID.
                s.db.execute("INSERT OR IGNORE INTO local_conversations VALUES(?,?,?)", (mid, connector, session))
                result = self.protocol.call(connector, name, args)
                return {**result, "origin_session": session}
            if name not in ("message_status", "read_message", "acknowledge", "reply", "stop_conversation"):
                raise Rejected("Local connectors can discover, send and follow their own conversations only")
            mid = args.get("conversation_id") if name == "stop_conversation" else args.get("message_id")
            row = s.get(connector, mid)
            binding = s.db.execute("SELECT connector,session FROM local_conversations WHERE conversation_id=?",
                                   (row["conversation_id"],)).fetchone()
            if not binding or (binding['connector'], binding['session']) != (connector, session):
                raise Rejected("Conversation belongs to another local session")
            result = self.protocol.call(connector, name, args)
            if name == "message_status":
                result["reply_messages"] = [s.read(connector, reply) for reply in result["replies"]]
            return {**result, "origin_session": session}
