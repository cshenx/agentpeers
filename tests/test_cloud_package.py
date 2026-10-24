"""Production upload admission: only shared runtime modules and the pinned SDK."""
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('prepare_cloud', ROOT / 'scripts/prepare_cloud.py')
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class CloudPackage(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agentpeers-package-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT / 'cloud', self.root / 'cloud')
        shutil.copytree(ROOT / 'agentpeers', self.root / 'agentpeers', ignore=shutil.ignore_patterns('__pycache__'))

    def test_only_runtime_allowlist_is_staged(self):
        prepare.prepare_runtime(self.root)
        paths = {p.relative_to(self.root / '.cloud-build/runtime').as_posix()
                 for p in (self.root / '.cloud-build/runtime').rglob('*') if p.is_file()}
        self.assertEqual(len(paths), 12)
        self.assertTrue({'worker.py','backend.py','agentpeers/personal/store.py'} <= paths)
        self.assertFalse(any('fixture' in p or p.endswith(('server.py','config.json','cli.py')) for p in paths))

    def test_unexpected_generated_file_stops_upload(self):
        prepare.prepare_runtime(self.root)
        (self.root / '.cloud-build/runtime/private.json').write_text('private data')
        with self.assertRaises(SystemExit):prepare.prepare_runtime(self.root)
        self.assertEqual((self.root / '.cloud-build/runtime/private.json').read_text(), 'private data')

    def test_symlink_cannot_redirect_staging(self):
        destination = self.root / 'unrelated';destination.mkdir()
        (self.root / '.cloud-build').symlink_to(destination, target_is_directory=True)
        with self.assertRaises(SystemExit):prepare.prepare_runtime(self.root)
        self.assertEqual(list(destination.iterdir()), [])

    def test_unrelated_sdk_is_preserved_and_refused(self):
        sdk=self.root/'python_modules';sdk.mkdir();(sdk/'private.txt').write_text('preserve me')
        with self.assertRaises(SystemExit):prepare.prepare_sdk(self.root)
        self.assertEqual((sdk/'private.txt').read_text(),'preserve me')
