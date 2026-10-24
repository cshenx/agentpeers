"""Explicit opt-in to opening a Codex desktop chat before peer delivery."""
import json
import os
import stat
import tempfile


def policy(enabled=None):
    # Use the same external home as messages, including the legacy home. Neither
    # application config nor any private conversation database is touched.
    from .core import state_dir
    from .native import owned_path
    root = state_dir()
    path = root / "desktop-wake.json"
    if root.exists() or root.is_symlink():
        owned_path(root, stat.S_ISDIR)
        if root.stat().st_mode & 0o077:
            raise ValueError("AgentPeers state directory must be private (0700)")
    value = False
    if path.exists() or path.is_symlink():
        owned_path(path, stat.S_ISREG)
        if path.stat().st_mode & 0o077 or path.stat().st_size > 4096:
            raise ValueError("desktop wake policy must be private (0600) and bounded")
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or type(data.get("enabled")) is not bool:
            raise ValueError("invalid desktop wake policy")
        value = data["enabled"]
    if enabled is not None:
        if type(enabled) is not bool:
            raise ValueError("desktop wake must be enabled or disabled explicitly")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".desktop-wake-", dir=root)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump({"enabled": enabled}, output)
                output.write("\n")
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        value = enabled
    return {"enabled": value, "path": str(path),
            "effect": "Open an unloaded destination in Codex desktop before sending peer tool output; "
                      "this can change the app's selected chat. No terminal takeover or background service."}


def enabled():
    return policy()["enabled"]
