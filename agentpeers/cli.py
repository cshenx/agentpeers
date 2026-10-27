import argparse
import json
from pathlib import Path
import sys

from .core import Bridge, resolve_actor
from .addressing import name_for
from . import native
from .config import connection, configure_connection
from . import personal_client


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "personal":
        from .personal.cli import main as personal_main
        return personal_main(sys.argv[2:])
    parser = argparse.ArgumentParser(prog="agentpeers", description="Peer messaging between your existing AI agents across platforms.",
                                     epilog="Local session commands connect Claude Code and Codex on one Mac. Use agentpeers personal --help for the separate Grokbot and Dot connection service.")
    parser.add_argument("--from", dest="actor", help="own session address; normally inferred")
    sub = parser.add_subparsers(dest="command", required=True)
    direct = sub.add_parser('connect-dot', help='connect this OS account directly to Dot through your existing AgentPeers service')
    direct.add_argument('--url', required=True)
    direct.add_argument('--connector', required=True, help='unique local- ID for this machine/profile')
    direct.add_argument('--name', required=True, help='display name shown to Dot')
    sub.add_parser('personal-peers', help='discover directly connected personal agents')
    sub.add_parser('personal-inbox', help="recover this session's existing personal-agent conversations and replies")
    direct_send = sub.add_parser('personal-send', help='send directly to a connected personal agent')
    direct_send.add_argument('--to', required=True)
    direct_send.add_argument('--scope', required=True)
    direct_send.add_argument('--id')
    direct_send.add_argument('--expects', choices=['none', 'reply'], default='reply')
    direct_send.add_argument('--wait', type=int, default=30)
    direct_reply = sub.add_parser('personal-reply', help='reply to a personal-agent message in this session')
    direct_reply.add_argument('id')
    direct_reply.add_argument('--reply-id', required=True)
    direct_reply.add_argument('--expects', choices=['none', 'reply'], default='none')
    for command in (direct_send, direct_reply):
        source = command.add_mutually_exclusive_group(required=True)
        source.add_argument('--message')
        source.add_argument('--message-file')
    direct_status = sub.add_parser('personal-status', help="retrieve this session's existing personal-agent reply")
    direct_status.add_argument('id')
    direct_status.add_argument('--wait', type=int, default=0)
    sub.add_parser('personal-ack', help='acknowledge a personal reply after reading it').add_argument('id')
    listing = sub.add_parser("list", help="discover local Claude Code and Codex sessions")
    listing.add_argument("--runtime", choices=["codex", "claude", "all"], default="all")
    sub.add_parser("whoami", help="show this local session identity")
    paths = sub.add_parser("connection", help="inspect or configure private local profile paths; never connects or sends")
    actions = paths.add_subparsers(dest="connection_action", required=True)
    actions.add_parser("show", help="show resolved nonsecret profile and mailbox paths")
    configure = actions.add_parser("configure", help="create a private path-only connection file in native plugin data")
    configure.add_argument("--data-dir", help="exact plugin data directory reported by the native client")
    for field in ("codex-home", "claude-home", "mailbox-home"):
        configure.add_argument("--" + field, required=True)
    waking = sub.add_parser("desktop-wake", help="opt in to opening unloaded destination chats in Codex desktop")
    waking.add_argument("mode", choices=["enable", "disable", "status"], default="status", nargs="?")
    checking = sub.add_parser("doctor", help="check local plugin dependencies and registrations without sending")
    checking.add_argument("--probe", help="verify an earlier setup challenge after reading its reply")
    sending = sub.add_parser("send", help="send a message to a local peer session")
    sending.add_argument("--to", required=True, help="exact native session name or runtime:UUID")
    sending.add_argument("--scope", required=True, help="scope authorized by the human; not peer content")
    sending.add_argument("--id", help="stable UUID for an idempotent submission")
    replying = sub.add_parser("reply", help="reply to a local session message")
    replying.add_argument("id")
    for command, default in [(sending, "reply"), (replying, "none")]:
        source = command.add_mutually_exclusive_group(required=True)
        source.add_argument("--message", help="text; use - to read UTF-8 stdin")
        source.add_argument("--message-file", help="read UTF-8 text from a file; - means stdin")
        command.add_argument("--expects", choices=["none", "ack", "reply"], default=default,
                             help="requested response; acknowledgment alone never wakes sender")
        command.add_argument("--summary", help="one-line summary, up to 300 characters")
        command.add_argument("--record-ref", help="inert path, URL or commit reference; never fetched automatically")
    sending.add_argument("--allow-question", action="store_true", help="allow this exact worker one question to its dispatcher")
    sending.add_argument("--decision-id", help="correlate a human decision question; does not grant permission")
    sub.add_parser("decision-status", help="inspect local peer reports about a decision").add_argument("decision_id")
    asking = sub.add_parser("ask-dispatcher", help="one question about an explicitly opted-in assignment")
    asking.add_argument("assignment_id")
    question_source = asking.add_mutually_exclusive_group(required=True)
    question_source.add_argument("--message")
    question_source.add_argument("--message-file")
    sub.add_parser("inbox", help="inspect this local session mailbox")
    for command, help_text in [("ack", "acknowledge consuming a local message"),
                               ("status", "inspect an existing local message")]:
        sub.add_parser(command, help=help_text).add_argument("id")
    sub.add_parser("mcp", help="run the local Claude Code and Codex MCP server")
    sub.add_parser("personal", help="configure or serve the separate Grokbot and Dot connection; see personal --help")
    args = parser.parse_args()
    if args.command == "personal":
        parser.error("Place personal immediately after agentpeers; --from applies only to local sessions.")
    try:
        if args.command == "connection":
            output = (connection().describe() if args.connection_action == "show" else
                      configure_connection(args.codex_home, args.claude_home, args.mailbox_home, args.data_dir))
        else:
            connection()  # Validate explicit connection settings before any native action.
        if args.command == "connection":
            pass
        elif args.command == 'connect-dot':
            output = personal_client.connect(args.url, args.connector, args.name)
        elif args.command.startswith('personal-'):
            actor = resolve_actor(args.actor)
            fields = {}
            if args.command in ('personal-send', 'personal-reply'):
                source = args.message_file or args.message
                text = sys.stdin.read(32001) if source == '-' else (Path(source).read_text(encoding='utf-8') if args.message_file else source)
                fields = {'message': text, 'expects': args.expects}
                if args.command == 'personal-send':
                    fields.update(to=args.to, scope=args.scope)
                    if args.id: fields['message_id'] = args.id
                else:
                    fields.update(message_id=args.id, reply_id=args.reply_id)
            elif args.command in ('personal-status', 'personal-ack'):
                fields['message_id'] = args.id
            tool = {'personal-peers': 'list_peers', 'personal-inbox': 'inbox', 'personal-send': 'send_message',
                    'personal-reply': 'reply', 'personal-status': 'message_status', 'personal-ack': 'acknowledge'}[args.command]
            output = personal_client.call(actor, tool, fields, getattr(args, 'wait', 0))
        elif args.command == "desktop-wake":
            from .desktop_wake import policy
            output = policy(None if args.mode == "status" else args.mode == "enable")
        elif args.command == "list":
            result, errors = [], []
            for runtime, discover in [("claude", native.claude_sessions), ("codex", native.codex_sessions)]:
                if args.runtime in (runtime, "all"):
                    try:
                        result.extend(discover())
                    except Exception as exc:
                        errors.append({"runtime": runtime, "error": str(exc)})
            output = {"sessions": result, "errors": errors}
        elif args.command == "whoami":
            actor = resolve_actor(args.actor)
            output = {"session": actor, "name": name_for(actor)}
        elif args.command == "doctor":
            from .setup import doctor, verify_probe
            output = (verify_probe(args.probe, resolve_actor(args.actor)) if args.probe else
                      doctor(actor=resolve_actor(args.actor)) if args.actor else doctor())
        elif args.command == "mcp":
            from .mcp import serve
            serve(args.actor)
            return
        else:
            bridge = Bridge()
            actor = None if args.command in ("status", "decision-status") else resolve_actor(args.actor)
            if args.command in ("send", "reply", "ask-dispatcher"):
                if args.message_file is not None or args.message == "-":
                    if args.message_file in (None, "-"):
                        if sys.stdin.isatty():
                            raise ValueError("stdin is a terminal; pipe text or use --message-file PATH")
                        raw = sys.stdin.buffer.read(32_001)
                    else:
                        with Path(args.message_file).open("rb") as stream:
                            raw = stream.read(32_001)
                    if len(raw) > 32_000:
                        raise ValueError("message must contain 1–32000 UTF-8 bytes")
                    args.message = raw.decode("utf-8")
                if args.command != "ask-dispatcher":
                    options = dict(expects=args.expects, summary=args.summary, record_ref=args.record_ref)
            if args.command == "send":
                output = bridge.send(actor, args.to, args.message, args.scope, ident=args.id, decision_id=args.decision_id, allow_question=args.allow_question, **options)
            elif args.command == "reply":
                output = bridge.reply(args.id, actor, args.message, **options)
            elif args.command == "inbox":
                output = bridge.inbox(actor)
            elif args.command == "ack":
                output = bridge.ack(args.id, actor)
            elif args.command == "ask-dispatcher":
                output = bridge.ask_dispatcher(args.assignment_id, actor, args.message)
            elif args.command == "decision-status":
                output = bridge.decision_status(args.decision_id)
            else:
                output = bridge.get(args.id)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        if args.command == "doctor" and (output.get("ready_for_test") is False or
                "message_test" in output and output["message_test"] not in ("not_run", "round_trip_verified")):
            sys.exit(2)
        if isinstance(output, dict) and (output.get("status") in ("unavailable", "unconfirmed") or output.get('state') in ('unavailable', 'unconfirmed')):
            sys.exit(2)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(1)
