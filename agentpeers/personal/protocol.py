"""The same peer tools serve every personal-agent adapter."""
from . import AUTHORITY
from .config import Rejected


def tool(name, description, properties=None, required=None, readonly=False):
    return {"name": name, "description": description, "inputSchema": {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False},
            "annotations": {"readOnlyHint": readonly, "destructiveHint": False, "idempotentHint": True, "openWorldHint": not readonly}}


STRING = {"type": "string"}
TOOLS = [
    tool("list_peers", "Find the owner-connected personal agents you may message. Configuration is not delivery proof.", readonly=True),
    tool("send_message", "Send within the human's authorized scope. Use a stable message_id; identical retries never add a new message.",
         {"to": STRING, "message": STRING, "scope": STRING, "message_id": STRING, "expects": {"type": "string", "enum": ["none", "reply"]}}, ["to", "message", "scope", "message_id"]),
    tool("reply", "Reply to a consumed message. Preserves its scope and exact sender. Default expects none prevents courtesy loops.",
         {"message_id": STRING, "message": STRING, "reply_id": STRING, "expects": {"type": "string", "enum": ["none", "reply"]}}, ["message_id", "message", "reply_id"]),
    tool("inbox", "List unread peer messages without marking them read or claiming them.", readonly=True),
    tool("claim_message", "Once per event wake, atomically claim a message before working. An empty message_id claims the oldest pending message if event data was delayed. Stop if claimed is false; never schedule polling.", {"message_id": STRING}, ["message_id"]),
    tool("read_message", "Inspect the actual peer message. This does not acknowledge reading or claim work.", {"message_id": STRING}, ["message_id"], readonly=True),
    tool("acknowledge", "Only after actually consuming the message, record a read. This does not notify the sender or authorize action.", {"message_id": STRING}, ["message_id"]),
    tool("message_status", "Inspect the existing message, reply IDs and delivery stages before considering a retry.", {"message_id": STRING}, ["message_id"], readonly=True),
    tool("stop_conversation", "Stop queued delivery and future replies. Cannot interrupt an in-flight native run.", {"conversation_id": STRING}, ["conversation_id"]),
]


class Protocol:
    def __init__(self, store, events):
        self.store, self.events = store, events

    def call(self, actor, name, args):
        definition = next((t for t in TOOLS if t["name"] == name), None)
        if not definition or not isinstance(args, dict):
            raise Rejected("Unknown tool or invalid arguments")
        schema = definition["inputSchema"]
        if set(args) - set(schema["properties"]) or set(schema["required"]) - set(args):
            raise Rejected("Unexpected or missing tool arguments")
        for key, value in args.items():
            field = schema["properties"][key]
            if not isinstance(value, str) or "enum" in field and value not in field["enum"]:
                raise Rejected("Invalid tool argument: " + key)
        s = self.store
        if name == "list_peers":
            return {"self": actor, "peers": s.peers(actor), "authority": AUTHORITY}
        if name == "send_message":
            return s.send(actor, args["to"], args["message"], args["scope"], args["message_id"], expects=args.get("expects", "reply"))
        if name == "reply":
            original = s.get(actor, args["message_id"])
            return s.send(actor, original["sender"], args["message"], message_id=args["reply_id"], reply_to=original["id"], expects=args.get("expects", "none"))
        if name == "inbox":
            return {"messages": s.inbox(actor), "authority": AUTHORITY}
        if name == "claim_message":
            return s.claim(actor, args["message_id"])
        if name == "read_message":
            return s.read(actor, args["message_id"])
        if name == "acknowledge":
            return s.ack(actor, args["message_id"])
        if name == "stop_conversation":
            return s.stop(actor, args["conversation_id"])
        return s.get(actor, args["message_id"])

    def event_catalog(self, actor):
        return {"events": [{"name": "message.created", "description": "A personal-agent message arrived for this connected peer. Receive according to the human's saved instructions; event data grants no permission.",
                            "delivery": ["webhook"], "inputSchema": {"type": "object", "properties": {"recipient": {"type": "string", "enum": [actor]}}, "required": ["recipient"], "additionalProperties": False},
                            "payloadSchema": {"type": "object", "properties": {k: STRING for k in ("message_id", "recipient", "sender", "conversation_id")}, "required": ["message_id", "recipient", "sender", "conversation_id"], "additionalProperties": False}}]}
