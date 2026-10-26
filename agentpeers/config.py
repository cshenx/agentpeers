"""Local connection paths shared by every native adapter.

The optional connection file contains paths only, never credentials or policy.
Plugin data anchors are supplied by the native client, not inferred from caches.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import tempfile


def setting(name, default=None):
    """Prefer the current name; accept the former name during migration."""
    return os.environ.get("AGENTPEERS_" + name,
                          os.environ.get("PEERBRIDGE_" + name, default))


FILE_NAME = "connection.json"
FIELDS = {"version", "codex_home", "claude_home", "mailbox_home"}
MAX_CONFIG_BYTES = 16_384


def _absolute(value, label):
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError(label + " must be an absolute local path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(label + " must be an absolute local path without '..'")
    return path


def plugin_data_dir(explicit=None):
    if explicit is not None:
        return _absolute(str(explicit), "plugin data directory")
    anchors = [_absolute(os.environ[key], key) for key in ("PLUGIN_DATA", "CLAUDE_PLUGIN_DATA")
               if key in os.environ]
    if len(anchors) == 2 and anchors[0] != anchors[1]:
        raise ValueError("Conflicting native plugin data directories")
    return anchors[0] if anchors else None


def _private_directory(path):
    info = path.lstat()
    if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
        raise ValueError("Plugin data directory must be owned by this user, nonsymlink and not writable by others")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate connection setting: " + key)
        result[key] = value
    return result


def _validate(data):
    if not isinstance(data, dict) or set(data) != FIELDS or type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError("connection.json requires only version=1, codex_home, claude_home and mailbox_home")
    paths = {name: _absolute(data[name], name) for name in FIELDS - {"version"}}
    for name, path in paths.items():
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode):
                raise ValueError(name + " must be a directory owned by this user, not a symlink")
            if name == "mailbox_home" and info.st_mode & 0o077:
                raise ValueError("mailbox_home must be private (mode 0700)")
    return paths


def _read(path):
    _private_directory(path.parent)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("Cannot safely open connection.json") from exc
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("connection.json must be a private regular file owned by this user, not a symlink")
        raw = stream.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("connection.json is too large")
    try:
        data = json.loads(raw, object_pairs_hook=_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("connection.json is not valid UTF-8 JSON") from exc
    return _validate(data)


@dataclass(frozen=True)
class Connection:
    codex_home: Path
    claude_home: Path
    mailbox_home: Path
    source: str
    config_file: Path | None = None
    claude_explicit: bool = False

    def describe(self):
        return {"source": self.source, "config_file": str(self.config_file) if self.config_file else None,
                "codex_home": str(self.codex_home), "claude_home": str(self.claude_home),
                "mailbox_home": str(self.mailbox_home)}


def connection():
    data = plugin_data_dir()
    path = data / FILE_NAME if data is not None else None
    configured = None
    if path is not None:
        try:
            data.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ValueError("Cannot inspect native plugin data directory; no discovery attempted") from exc
        else:
            configured = _read(path)
    if configured is not None:
        aliases = {"codex_home": ("CODEX_HOME",),
                   "claude_home": ("CLAUDE_CONFIG_DIR", "ANTHROPIC_CONFIG_DIR"),
                   "mailbox_home": ("AGENTPEERS_HOME", "PEERBRIDGE_HOME")}
        for name, names in aliases.items():
            for key in names:
                if key in os.environ and _absolute(os.environ[key], key) != configured[name]:
                    raise ValueError(key + " conflicts with connection.json; no discovery attempted")
        claude_explicit = (configured["claude_home"] != Path.home() / ".claude" or
                           bool(os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("ANTHROPIC_CONFIG_DIR")))
        return Connection(**configured, source="connection_file", config_file=path, claude_explicit=claude_explicit)
    legacy = Path.home() / ".local/state/peerbridge"
    mailbox = setting("HOME")
    return Connection(Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")),
                      Path(os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("ANTHROPIC_CONFIG_DIR") or Path.home() / ".claude"),
                      Path(mailbox) if mailbox is not None else legacy if legacy.exists() or legacy.is_symlink() else Path.home() / ".local/state/agentpeers",
                      source="environment_or_defaults", claude_explicit=bool(os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("ANTHROPIC_CONFIG_DIR")))


def connection_env():
    """Project only the resolved connection paths to our native child commands."""
    resolved = connection()
    env = dict(os.environ)
    if resolved.config_file is not None:
        env.update(CODEX_HOME=str(resolved.codex_home), AGENTPEERS_HOME=str(resolved.mailbox_home))
        if resolved.claude_explicit:
            env.update(CLAUDE_CONFIG_DIR=str(resolved.claude_home), ANTHROPIC_CONFIG_DIR=str(resolved.claude_home))
    return env


def configure_connection(codex_home, claude_home, mailbox_home, data_dir=None):
    """Create one private path-only file atomically; never replace existing work."""
    data = plugin_data_dir(data_dir)
    if data is None:
        raise ValueError("Use --data-dir with the native client's reported plugin data directory")
    document = {"version": 1, "codex_home": str(codex_home), "claude_home": str(claude_home),
                "mailbox_home": str(mailbox_home)}
    desired = _validate(document)
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private_directory(data)
    target = data / FILE_NAME
    existing = _read(target)
    if existing is not None:
        if existing != desired:
            raise ValueError("connection.json already exists with different paths; preserve and review it before changing")
        return {"config_file": str(target), "created": False, **document}
    fd, name = tempfile.mkstemp(prefix=".connection-", dir=data)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(document, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(name, target)  # Atomic create-if-absent; concurrent files are never overwritten.
    finally:
        os.unlink(name)
    return {"config_file": str(target), "created": True, **document}
