#!/usr/bin/env python3
"""User-local installer. Does not read API keys or authorize model calls."""
import argparse
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import time

SOURCE = Path(__file__).resolve().parent
DEFAULT_ROOT = Path.home() / '.local/share/codex-dsh-worker'


def render_rules(root):
    return (SOURCE / 'docs/codex-rules.md').read_text().replace('{{WORKER_ROOT}}', str(root))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    ap.add_argument('--bin-dir', type=Path, default=Path.home() / '.local/bin')
    ap.add_argument('--node', default=shutil.which('node'))
    ap.add_argument('--npm', default=shutil.which('npm'))
    ap.add_argument('--npm-cache', type=Path)
    ap.add_argument('--offline', action='store_true', help='Use an already populated npm cache')
    ap.add_argument('--skip-runtime', action='store_true', help='Reuse an existing verified DSH runtime')
    ap.add_argument('--with-codex-rules', action='store_true', help='Append rules to CODEX_HOME/AGENTS.md')
    args = ap.parse_args()
    if sys.platform != 'darwin':
        ap.error('macOS is required; there is no unsandboxed fallback')
    if sys.version_info < (3, 9): ap.error('Python 3.9 or newer is required')
    root, bindir = args.root.expanduser().resolve(), args.bin_dir.expanduser().resolve()
    if root in (Path('/'), Path.home().resolve(), SOURCE) or root in SOURCE.parents:
        ap.error('Choose a dedicated installation directory')
    if not args.node or not Path(args.node).is_file(): ap.error('Node.js >=24.9.0 is required; use --node if it is not on PATH')
    node = str(Path(args.node).expanduser().absolute())
    version = subprocess.check_output([node, '-p', 'process.versions.node'], text=True).strip()
    if tuple(int(x) for x in version.split('.')[:3]) < (24, 9, 0): ap.error('Node.js >=24.9.0 is required')
    cli = root / 'runtime/node_modules/@deepseek-ai/dsh/lib/bin.js'
    if args.skip_runtime and not cli.is_file(): ap.error('--skip-runtime requires an existing DSH installation')
    if not args.skip_runtime and not args.npm: ap.error('npm is required')
    link = bindir / 'dsh-worker'
    wrapper = root / 'bin/dsh-worker'
    if (link.exists() or link.is_symlink()) and (not link.is_symlink() or link.resolve() != wrapper.resolve()):
        ap.error('Existing command will not be overwritten: ' + str(link))
    os.umask(0o077)
    for name in ('bin', 'gateway', 'config', 'tasks', 'logs', 'runtime', 'docs', 'examples'):
        (root / name).mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    if not args.skip_runtime:
        for name in ('package.json', 'package-lock.json'):
            shutil.copy2(SOURCE / 'runtime-deps' / name, root / 'runtime' / name)
        cmd = [args.npm, 'ci', '--prefix', str(root / 'runtime'), '--ignore-scripts', '--no-audit', '--no-fund']
        if args.npm_cache: cmd += ['--cache', str(args.npm_cache.expanduser().resolve())]
        if args.offline: cmd.append('--offline')
        env = dict(os.environ)
        env['PATH'] = str(Path(node).parent) + os.pathsep + env.get('PATH', '')
        subprocess.run(cmd, env=env, check=True)
    actual = subprocess.check_output([node, str(cli), '--version'], text=True).strip()
    expected = json.loads((SOURCE / 'runtime-deps/package.json').read_text())['dependencies']['@deepseek-ai/dsh']
    if actual != expected: raise RuntimeError('Installed DSH version differs from the pinned runtime')
    for folder in ('gateway', 'examples'):
        for f in (SOURCE / folder).iterdir():
            if f.is_file(): shutil.copy2(f, root / folder / f.name)
    for f in (SOURCE / 'docs').iterdir():
        if f.is_file(): shutil.copy2(f, root / 'docs' / f.name)
    for name in ('README.md', 'SECURITY.md', 'LICENSE'):
        shutil.copy2(SOURCE / name, root / name)
    read_roots = []
    for executable in (node, sys.executable):
        for parent in Path(executable).resolve().parents:
            if parent.name == 'Cellar':
                read_roots.extend(str(parent.parent / name) for name in ('Cellar', 'opt', 'lib'))
                break
    config = {'node': node, 'python': str(Path(sys.executable).resolve()),
              'read_roots': sorted(set(read_roots)), 'dsh_version': actual,
              'architecture': platform.machine()}
    (root / 'config/install.json').write_text(json.dumps(config, indent=2) + '\n')
    if not (root / 'config/pricing.json').exists():
        shutil.copy2(SOURCE / 'docs/pricing.example.json', root / 'config/pricing.json')
    # Runtime paths are generated locally; shell quoting supports spaces in paths.
    wrapper.write_text('#!/bin/sh\nexport DSH_WORKER_ROOT=' + shlex.quote(str(root)) +
                       '\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(root / 'gateway/worker.py')) + ' "$@"\n')
    wrapper.chmod(0o700)
    bindir.mkdir(parents=True, exist_ok=True)
    if not link.is_symlink(): link.symlink_to(wrapper)
    (root / 'docs/codex-rules.local.md').write_text(render_rules(root))
    if args.with_codex_rules:
        codex = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser()
        codex.mkdir(parents=True, exist_ok=True)
        agents = codex / 'AGENTS.md'
        old = agents.read_text() if agents.exists() else ''
        marker = '<!-- BEGIN CODEX-DSH-WORKER -->'
        if marker not in old:
            if old: shutil.copy2(agents, root / 'config' / ('AGENTS.backup-' + str(time.time_ns()) + '.md'))
            agents.write_text(old + ('\n\n' if old else '') + render_rules(root))
        else:
            print('Existing Codex Worker rules preserved; review docs/codex-rules.local.md for updates.')
    print('Installed DSH ' + actual + ' at ' + str(root))
    print('Command: ' + str(link))
    print('If needed, add this directory to PATH: ' + str(bindir))
    print('Next: dsh-worker configure (hidden key input); no API call has been made.')


if __name__ == '__main__':
    try: main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as e:
        print('Installation failed: ' + str(e), file=sys.stderr)
        sys.exit(1)
