"""Optional owner-operated service commands; never used as peer tools."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
import sys

from .config import DEFAULT_DOT_CALLBACK_HOSTS, Rejected, load


def initialize(directory, public_url):
    directory = Path(directory).expanduser()
    directory.mkdir(mode=0o700, parents=False, exist_ok=True)
    s = directory.lstat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077 or stat.S_ISLNK(s.st_mode):
        raise Rejected("Choose a private directory owned by your OS user (mode 0700)")
    config = {"public_url": public_url, "owner_key": secrets.token_urlsafe(32), "callback_hosts": list(DEFAULT_DOT_CALLBACK_HOSTS), "oauth_redirect_hosts": ["chatgpt.com"],
              "max_messages": 32, "conversation_seconds": 86400,
              "connections": [["grokbot", "dot"]], "peers": {
                  "grokbot": {"name": "Grokbot", "platform": "grokbot", "key": secrets.token_urlsafe(32), "webhook_url": "", "webhook_key": ""},
                  "dot": {"name": "Dot", "platform": "dot", "key": secrets.token_urlsafe(32)}}}
    path = directory / "config.json"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(config, stream, indent=2)
        stream.write("\n")
    return {"config": str(path), "next": "Set the native Grokbot routine URL/key and review the default Dot callback host in this private file. No agent was contacted and no service was started.", "secrets_printed": False}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agentpeers personal", description="Manage the AgentPeers Grokbot and Dot connection service (optional HTTPS service).")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="write a private starter configuration; does not deploy or contact agents")
    init.add_argument("--directory", required=True)
    init.add_argument("--public-url", required=True)
    for name, help_text in (("doctor", "check private service configuration without contacting agents"),
                            ("serve", "run the connection service behind your HTTPS endpoint"),
                            ("revoke", "remove one peer's OAuth grants and subscription")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--config", required=True)
        if name == "serve":
            p.add_argument("--port", type=int, default=8766)
        if name == "revoke":
            p.add_argument("--peer", required=True, help="stop the service first; remove this peer's OAuth grants and subscriptions")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            print(json.dumps(initialize(args.directory, args.public_url), indent=2))
            return
        path = Path(args.config).expanduser()
        config = load(path)
        if args.command == "doctor":
            print(json.dumps({"configuration_valid": True, "live_delivery_verified": False, "public_url": config["public_url"],
                              "peers": [{"id": p, "name": c["name"], "platform": c["platform"]} for p, c in config["peers"].items()],
                              "callback_hosts_configured": bool(config["callback_hosts"]), "note": "No network requests or agent calls were made."}, indent=2))
            return
        # Lock before opening the store or recovering interrupted deliveries. Never run two workers.
        os.umask(0o077)
        lock = os.open(str(path.parent / "service.lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        if os.fstat(lock).st_uid != os.getuid() or os.fstat(lock).st_mode & 0o077:
            raise Rejected("Service lock must be owned and private")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Rejected("Personal-agent service is already running; stop it before maintenance") from None
        database = path.parent / "personal.sqlite3"
        if database.exists() or database.is_symlink():
            d = database.lstat()
            if not stat.S_ISREG(d.st_mode) or d.st_uid != os.getuid() or d.st_mode & 0o077:
                raise Rejected("Personal database must be an owned private regular file")
        from .store import Store
        store = Store(database, config)
        try:
            if args.command == "revoke":
                if args.peer not in config["peers"]:
                    raise Rejected("Unknown peer")
                resource = config["public_url"] + "/mcp/" + args.peer
                store.db.execute("DELETE FROM oauth_tokens WHERE resource=?", (resource,))
                store.db.execute("DELETE FROM oauth_codes WHERE resource=?", (resource,))
                store.db.execute("UPDATE subscriptions SET active=0 WHERE peer=?", (args.peer,))
                print(json.dumps({"peer": args.peer, "oauth_revoked": True, "note": "To disconnect all messaging, also remove its connections and rotate its direct peer key in private config before restarting."}))
            else:
                from .server import App, serve
                print(json.dumps({"service": "AgentPeers", "listening": "127.0.0.1:%d" % args.port, "live_delivery_verified": False}), flush=True)
                try:
                    serve(App(store), port=args.port)
                except KeyboardInterrupt:
                    pass
        finally:
            store.close()
            os.close(lock)
    except Rejected as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        raise SystemExit(1)
    except (OSError, ValueError, KeyError):
        # Do not expose a configuration parse error that might include a secret's source line.
        print(json.dumps({"error": "Personal-agent configuration or operation failed. Check private file permissions, required fields, service lock and command arguments."}), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
