#!/usr/bin/env python3
"""Build an allowlisted Worker tree; never upload a checkout or private state."""
import base64
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys


def prepare_runtime(root):
    root = root.resolve()
    runtime = root / '.cloud-build/runtime'
    for path in (runtime.parent, runtime):
        if path.is_symlink():
            raise SystemExit('Refusing a symlink in the generated runtime path')
    runtime.mkdir(parents=True, exist_ok=True)
    modules = ('__init__', 'adapters', 'app', 'auth', 'config', 'context', 'context_bridge', 'context_ui', 'factual', 'endpoint', 'protocol', 'store', 'transport')
    allowed = {'worker.py': root / 'cloud/worker.py', 'backend.py': root / 'cloud/backend.py',
               'agentpeers/__init__.py': root / 'agentpeers/__init__.py'}
    allowed.update({'agentpeers/personal/' + name + '.py': root / 'agentpeers/personal' / (name + '.py') for name in modules})
    directories = {parent.as_posix() for name in allowed for parent in Path(name).parents if parent != Path('.')}
    for existing in runtime.rglob('*'):
        relative = existing.relative_to(runtime).as_posix()
        if existing.is_symlink() or relative not in (directories if existing.is_dir() else allowed):
            raise SystemExit('Unexpected file or symlink in generated cloud runtime; inspect it before rebuilding')
    for name, source in allowed.items():
        if any(p.is_symlink() for p in (source, *source.parents) if p != root.parent):
            raise SystemExit('Cloud source paths must not contain symlinks')
        destination = runtime / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    return len(allowed)


def verify_sdk(sdk):
    distributions = list(importlib.metadata.distributions(path=[str(sdk)]))
    if len(distributions) != 1 or distributions[0].metadata['Name'] != 'workers-runtime-sdk' or distributions[0].version != '1.9.5':
        raise SystemExit('Unexpected Python SDK dependency; preserve and inspect python_modules')
    expected = {'.agentpeers-sdk'}
    for entry in distributions[0].files or []:
        path = sdk / str(entry)
        if not path.resolve().is_relative_to(sdk.resolve()) or path.is_symlink() or not path.is_file():
            raise SystemExit('Unexpected SDK path')
        expected.add(str(entry))
        if entry.hash:
            if entry.hash.mode != 'sha256':
                raise SystemExit('Unsupported SDK file hash')
            value = base64.urlsafe_b64encode(hashlib.sha256(path.read_bytes()).digest()).decode().rstrip('=')
            if value != entry.hash.value:
                raise SystemExit('Python SDK file did not match its installed record')
    for path in sdk.rglob('*'):
        if path.is_symlink() or path.is_file() and path.relative_to(sdk).as_posix() not in expected:
            raise SystemExit('Unexpected file in Python SDK; refusing to upload it')


def prepare_sdk(root):
    if sys.version_info < (3, 11):
        raise SystemExit('Cloud deployment tools require Python 3.11+; the local bridge and self-hosted service support 3.10+')
    sdk = root / 'python_modules'
    marker = sdk / '.agentpeers-sdk'
    if sdk.is_symlink() or sdk.exists() and not marker.is_file():
        raise SystemExit('Refusing an unrelated python_modules directory')
    if not sdk.exists():
        subprocess.run([sys.executable, '-m', 'pip', '--disable-pip-version-check', 'install', '--require-hashes',
                        '--only-binary=:all:', '--no-deps', '--no-compile', '--target', str(sdk),
                        '-r', str(root / 'cloud/requirements-build.txt')], check=True)
        marker.write_text('workers-runtime-sdk==1.9.5\n')
    verify_sdk(sdk)


def main():
    root = Path(__file__).resolve().parent.parent
    count = prepare_runtime(root)
    prepare_sdk(root)
    print(json.dumps({'cloud_modules': count, 'sdk': 'workers-runtime-sdk==1.9.5', 'live_agent_calls': 0}))


if __name__ == '__main__':
    main()
