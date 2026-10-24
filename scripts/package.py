#!/usr/bin/env python3
"""Export a validated source ZIP from HEAD; never copy the working directory."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import zipfile


def main():
    root = Path(__file__).resolve().parent.parent
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args])
    manifest = json.loads(git('show', 'HEAD:plugin.json'))
    version = manifest['version']
    if not version or any(c not in '0123456789.-abcdefghijklmnopqrstuvwxyz' for c in version):
        raise ValueError('invalid version for archive name')
    name = 'agentpeers-' + version
    archive = git('archive', '--format=zip', '--prefix=' + name + '/', 'HEAD')
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        names = [p.removeprefix(name + '/') for p in bundle.namelist()]
        forbidden = {'proof', 'dist', '.git', '.venv', '__pycache__',
                     'node_modules', 'python_modules', '.cloud-build', '.wrangler'}
        if any(set(Path(p).parts) & forbidden or p.endswith(('.sqlite3', '.pyc', '.key', '.env'))
               or any(part.startswith(('.dev.vars', '.env')) and part not in ('.dev.vars.example', '.env.example')
                      for part in Path(p).parts) for p in names):
            raise ValueError('private or generated content found in tracked export')
        for path in ['README.md', 'LICENSE', 'SECURITY.md', 'CONTRIBUTING.md',
                     'plugin.json', 'mcp.json', 'bin/agentpeers',
                     '.claude-plugin/plugin.json', '.agents/plugins/marketplace.json',
                     '.claude-plugin/marketplace.json', 'skills/setup/SKILL.md']:
            if path not in names:
                raise ValueError('missing packaged file: ' + path)
        if '.mcp.json' in names:
            raise ValueError('Root .mcp.json would also register an unintended Claude project server')
        portable = json.loads(bundle.read(name + '/plugin.json'))
        claude = json.loads(bundle.read(name + '/.claude-plugin/plugin.json'))
        for config in ['mcp.json', '.claude-plugin/plugin.json']:
            connection = json.loads(bundle.read(name + '/' + config))['mcpServers']['agentpeers']
            if connection['env'].get('AGENTPEERS_PLUGIN_VERSION') != version:
                raise ValueError('MCP connection version must match the plugin for reloads')
        if portable['version'] != claude['version']:
            raise ValueError('plugin versions disagree')
        mode = bundle.getinfo(name + '/bin/agentpeers').external_attr >> 16
        if not mode & 0o111:
            raise ValueError('bundled launcher is not executable')
    output = root / 'dist'
    output.mkdir(exist_ok=True)
    path = output / (name + '.zip')
    path.write_bytes(archive)
    digest = hashlib.sha256(archive).hexdigest()
    path.with_suffix('.zip.sha256').write_text(digest + '  ' + path.name + '\n')
    print(json.dumps({'archive':str(path), 'sha256':digest,
                      'commit':git('rev-parse','HEAD').decode().strip(),
                      'tracked_entries':len(names)}))


if __name__ == '__main__':
    main()
