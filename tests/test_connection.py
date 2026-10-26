"""Private profiles must survive a host-filtered MCP environment without discovery."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch, Mock

from agentpeers import config, native
from agentpeers.core import state_dir
from agentpeers.setup import doctor, command_json
from agentpeers.unixrpc import SharedServer
from agentpeers.cli import main


class ConnectionProfiles(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentpeers-profile-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.data = self.root / "plugin-data"
        self.paths = {key: str(self.root / key) for key in ("codex_home", "claude_home", "mailbox_home")}
        self.env = patch.dict(os.environ, {"HOME": str(self.home), "PATH": os.defpath}, clear=True)
        self.env.start()
        self.home_patch = patch("pathlib.Path.home", return_value=self.home)
        self.home_patch.start()

    def tearDown(self):
        self.home_patch.stop()
        self.env.stop()
        self.temp.cleanup()

    def configure(self):
        result = config.configure_connection(**self.paths, data_dir=self.data)
        os.environ["PLUGIN_DATA"] = str(self.data)
        return result

    def test_absent_file_preserves_defaults_and_legacy_without_writing(self):
        self.data.mkdir()
        os.environ["PLUGIN_DATA"] = str(self.data)
        resolved = config.connection()
        self.assertEqual(resolved.codex_home, self.home / ".codex")
        self.assertEqual(resolved.claude_home, self.home / ".claude")
        self.assertEqual(resolved.mailbox_home, self.home / ".local/state/agentpeers")
        legacy = self.home / ".local/state/peerbridge"
        legacy.mkdir(parents=True)
        self.assertEqual(state_dir(), legacy)
        self.assertEqual(list(self.data.iterdir()), [])

    def test_absent_file_preserves_existing_environment_precedence(self):
        with patch.dict(os.environ, {"AGENTPEERS_HOME": str(self.root / "current"),
                                     "PEERBRIDGE_HOME": str(self.root / "old"),
                                     "CODEX_HOME": self.paths["codex_home"],
                                     "ANTHROPIC_CONFIG_DIR": self.paths["claude_home"]}):
            self.assertEqual(state_dir(), self.root / "current")
            self.assertEqual(native.claude_home(), Path(self.paths["claude_home"]))
            self.assertEqual(config.connection_env(), dict(os.environ))

    def test_filtered_host_environment_resolves_both_client_anchors(self):
        self.configure()
        for anchor in ("PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"):
            with self.subTest(anchor=anchor), patch.dict(os.environ, {anchor: str(self.data)}, clear=True):
                resolved = config.connection()
                self.assertEqual(resolved.describe()["source"], "connection_file")
                self.assertEqual(state_dir(), Path(self.paths["mailbox_home"]))
                self.assertEqual(native.claude_home(), Path(self.paths["claude_home"]))
                self.assertEqual(resolved.codex_home, Path(self.paths["codex_home"]))
                child = config.connection_env()
                self.assertEqual(child["CODEX_HOME"], self.paths["codex_home"])
                self.assertEqual(child["CLAUDE_CONFIG_DIR"], self.paths["claude_home"])
                self.assertEqual(child["ANTHROPIC_CONFIG_DIR"], self.paths["claude_home"])
                self.assertEqual(child["AGENTPEERS_HOME"], self.paths["mailbox_home"])
                self.assertFalse((self.root / "mailbox_home").exists())

    def test_all_explicit_environment_aliases_must_agree(self):
        self.configure()
        fields = {"CODEX_HOME": "codex_home", "CLAUDE_CONFIG_DIR": "claude_home",
                  "ANTHROPIC_CONFIG_DIR": "claude_home", "AGENTPEERS_HOME": "mailbox_home",
                  "PEERBRIDGE_HOME": "mailbox_home"}
        for key, field in fields.items():
            with self.subTest(key=key):
                with patch.dict(os.environ, {key: self.paths[field]}):
                    config.connection()
                with patch.dict(os.environ, {key: str(self.root / "other")}):
                    with self.assertRaisesRegex(ValueError, "conflicts"):
                        config.connection()
        with patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": str(self.root / "other-data")}):
            with self.assertRaisesRegex(ValueError, "Conflicting native"):
                config.connection()

    def test_unknown_fields_versions_and_relative_paths_are_rejected(self):
        base = {"version": 1, **self.paths}
        cases = [{**base, "version": True}, {**base, "version": 2},
                 {**base, "env": {"COMMAND": "forbidden"}},
                 {**base, "codex_home": "relative"}, {**base, "claude_home": "/tmp/../other"},
                 {**base, "mailbox_home": ""}, {k: v for k, v in base.items() if k != "claude_home"}]
        self.configure()
        for document in cases:
            with self.subTest(document=document):
                (self.data / config.FILE_NAME).write_text(json.dumps(document))
                with self.assertRaises(ValueError):
                    config.connection()

    def test_malformed_duplicate_and_oversized_json_never_falls_back(self):
        self.configure()
        target = self.data / config.FILE_NAME
        for raw in (b"{", b"\xff", b'{"version":1,"version":1}', b" " * (config.MAX_CONFIG_BYTES + 1)):
            target.write_bytes(raw)
            with self.assertRaises(ValueError):
                state_dir()

    def test_config_must_be_private_owned_regular_file_without_symlink(self):
        self.configure()
        target = self.data / config.FILE_NAME
        target.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private regular"):
            config.connection()
        target.chmod(0o600)
        info = target.stat()
        wrong_owner = Mock(st_mode=info.st_mode, st_uid=os.getuid() + 1)
        with patch("agentpeers.config.os.fstat", return_value=wrong_owner):
            with self.assertRaisesRegex(ValueError, "private regular"):
                config.connection()
        saved = self.root / "saved.json"
        target.rename(saved)
        target.symlink_to(saved)
        with self.assertRaises(ValueError):
            config.connection()
        target.unlink()
        os.mkfifo(target, 0o600)
        with self.assertRaisesRegex(ValueError, "private regular"):
            config.connection()

    def test_data_directory_cannot_be_symlink_or_writable_by_others(self):
        self.configure()
        self.data.chmod(0o777)
        with self.assertRaisesRegex(ValueError, "Plugin data directory"):
            config.connection()
        self.data.chmod(0o700)
        other = self.root / "data-alias"
        other.symlink_to(self.data)
        with patch.dict(os.environ, {"PLUGIN_DATA": str(other)}):
            with self.assertRaisesRegex(ValueError, "Plugin data directory"):
                config.connection()

    def test_configured_roots_cannot_be_foreign_symlinks_or_public_mailbox(self):
        self.configure()
        for key in self.paths:
            path = Path(self.paths[key])
            path.symlink_to(self.home)
            with self.assertRaisesRegex(ValueError, "not a symlink"):
                config.connection()
            path.unlink()
        mailbox = Path(self.paths["mailbox_home"])
        mailbox.mkdir(mode=0o755)
        mailbox.chmod(0o755)  # Exercise the unsafe mode regardless of inherited umask.
        with self.assertRaisesRegex(ValueError, "mailbox_home must be private"):
            config.connection()

    def test_atomic_writer_is_private_idempotent_and_preserves_existing_work(self):
        created = self.configure()
        target = self.data / config.FILE_NAME
        original = target.read_bytes()
        self.assertTrue(created["created"])
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertFalse(config.configure_connection(**self.paths, data_dir=self.data)["created"])
        with self.assertRaisesRegex(ValueError, "already exists"):
            config.configure_connection(**{**self.paths, "mailbox_home": str(self.root / "other")}, data_dir=self.data)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(self.data.iterdir()), [target])

    def test_atomic_writer_does_not_replace_a_concurrent_file(self):
        self.data.mkdir(mode=0o700)
        target = self.data / config.FILE_NAME
        real_link = os.link
        def competing_writer(source, destination):
            target.write_text("concurrent work")
            return real_link(source, destination)
        with patch("agentpeers.config.os.link", side_effect=competing_writer):
            with self.assertRaises(FileExistsError):
                config.configure_connection(**self.paths, data_dir=self.data)
        self.assertEqual(target.read_text(), "concurrent work")
        self.assertEqual(list(self.data.iterdir()), [target])

    def test_invalid_file_stops_cli_and_setup_before_native_discovery(self):
        self.configure()
        (self.data / config.FILE_NAME).write_text("{}")
        with patch.object(native, "shared_threads") as threads, patch.object(native, "claude_sessions") as sessions:
            with self.assertRaises(ValueError):
                doctor()
            with patch("sys.argv", ["agentpeers", "doctor"]), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main()
            self.assertEqual(error.exception.code, 1)
            threads.assert_not_called()
            sessions.assert_not_called()

    def test_registration_subprocess_uses_resolved_native_home(self):
        self.configure()
        with patch("agentpeers.setup.subprocess.run", return_value=Mock(returncode=0, stdout="{}")) as run:
            command_json(["codex", "plugin", "list", "--json"])
        self.assertEqual(run.call_args.kwargs["env"]["CODEX_HOME"], self.paths["codex_home"])

    def test_native_inventory_child_receives_the_same_profile(self):
        self.configure()
        with patch("agentpeers.native.subprocess.Popen", side_effect=RuntimeError("fixture before process launch")) as start:
            with self.assertRaisesRegex(RuntimeError, "fixture before"):
                native.AppServer().__enter__()
        self.assertEqual(start.call_args.kwargs["env"]["CODEX_HOME"], self.paths["codex_home"])

    def test_connection_show_reports_paths_without_native_discovery(self):
        self.configure()
        output = io.StringIO()
        with patch("sys.argv", ["agentpeers", "connection", "show"]), \
             patch.object(native, "codex_sessions") as codex, \
             patch.object(native, "claude_sessions") as claude, contextlib.redirect_stdout(output):
            main()
        self.assertEqual(json.loads(output.getvalue())["mailbox_home"], self.paths["mailbox_home"])
        codex.assert_not_called()
        claude.assert_not_called()

    def test_native_registration_uses_profile_local_claude_config(self):
        self.configure()
        from agentpeers.setup import registrations
        seen = []
        def read(path, default=None):
            seen.append(path)
            return default
        with patch("agentpeers.setup.command_json", side_effect=[{}, []]), \
             patch("agentpeers.setup.read_json", side_effect=read):
            registrations()
        self.assertIn(Path(self.paths["claude_home"]) / ".claude.json", seen)
        self.assertNotIn(self.home / ".claude.json", seen)

    def registration(self):
        from agentpeers.setup import registrations
        with patch("agentpeers.setup.command_json", side_effect=[{}, []]):
            return registrations()["claude"]

    def test_mailbox_only_configuration_preserves_default_global_registration(self):
        global_config = self.home / ".claude.json"
        original = json.dumps({"mcpServers": {"agentpeers": {"command": "fixture-only"}}})
        global_config.write_text(original)
        config.configure_connection(self.home / ".codex", self.home / ".claude", self.paths["mailbox_home"], self.data)
        os.environ["PLUGIN_DATA"] = str(self.data)
        self.assertTrue(self.registration()["manual_mcp"])
        self.assertFalse(config.connection().claude_explicit)
        child = config.connection_env()
        self.assertNotIn("CLAUDE_CONFIG_DIR", child)
        self.assertNotIn("ANTHROPIC_CONFIG_DIR", child)
        self.assertEqual(global_config.read_text(), original)

    def test_nondefault_claude_home_preserves_profile_local_registration(self):
        self.configure()
        home = Path(self.paths["claude_home"])
        home.mkdir(mode=0o700)
        (home / ".claude.json").write_text(json.dumps({"mcpServers": {"agentpeers": {"command": "fixture-only"}}}))
        (self.home / ".claude.json").write_text('{"mcpServers": {}}')
        self.assertTrue(self.registration()["manual_mcp"])
        self.assertTrue(config.connection().claude_explicit)
        self.assertEqual(config.connection_env()["CLAUDE_CONFIG_DIR"], str(home))

    def test_explicit_default_home_keeps_native_profile_local_semantics(self):
        home = self.home / ".claude"
        home.mkdir(mode=0o700)
        (home / ".claude.json").write_text(json.dumps({"mcpServers": {"agentpeers": {"command": "fixture-only"}}}))
        (self.home / ".claude.json").write_text('{"mcpServers": {}}')
        config.configure_connection(self.home / ".codex", home, self.paths["mailbox_home"], self.data)
        os.environ["PLUGIN_DATA"] = str(self.data)
        for alias in ("CLAUDE_CONFIG_DIR", "ANTHROPIC_CONFIG_DIR"):
            with self.subTest(alias=alias), patch.dict(os.environ, {alias: str(home)}):
                self.assertTrue(self.registration()["manual_mcp"])
                self.assertTrue(config.connection().claude_explicit)
                self.assertEqual(config.connection_env()["CLAUDE_CONFIG_DIR"], str(home))

    def test_unreadable_plugin_data_is_an_error_not_an_absent_profile(self):
        self.configure()
        original = Path.lstat
        def inaccessible(path):
            if path == self.data:
                raise PermissionError("fixture inaccessible directory")
            return original(path)
        with patch.object(Path, "lstat", inaccessible):
            with self.assertRaisesRegex(ValueError, "Cannot inspect native plugin data"):
                config.connection()

    def test_both_codex_socket_consumers_use_the_same_resolved_home(self):
        self.configure()
        home = Path(self.paths["codex_home"])
        for relative, factory in (("ipc/ipc.sock", native.Desktop),
                                  ("app-server-control/app-server-control.sock", SharedServer)):
            path = home / relative
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            path.touch()  # Path selection only; no real transport or native server.
            with patch.object(native, "owned_path", side_effect=lambda path, kind: Path(path)), \
                 patch("socket.socket") as constructor:
                constructor.return_value.connect.side_effect = OSError("fixture stop before handshake")
                with self.assertRaises(native.Unavailable):
                    factory().__enter__()
                constructor.return_value.connect.assert_called_once()
                self.assertEqual(Path(constructor.return_value.connect.call_args.args[0]).resolve(), path.resolve())


if __name__ == "__main__":
    unittest.main()
