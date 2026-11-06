#!/usr/bin/env python3
"""Scan publishable files; emits file names only, never matched secrets."""
from pathlib import Path
import re
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
r = subprocess.run(['git', '-C', str(root), 'ls-files', '-z'], capture_output=True)
files = [root / p.decode() for p in r.stdout.split(b'\0') if p] if r.returncode == 0 else [p for p in root.rglob('*') if p.is_file() and '.git' not in p.parts]
patterns = [re.compile(rb'sk-[A-Za-z0-9]{16,}'),
            re.compile(rb'/(?:Users|home)/[A-Za-z0-9_.-]+/'),
            re.compile(rb'[A-Za-z0-9._%+-]+@(?:gmail|outlook|hotmail|qq|icloud)\.com', re.I)]
forbidden_dirs = {'tasks', 'logs', 'config', 'runtime', 'approval-test', 'node_modules', '__pycache__'}
failures = []
for p in files:
    relative = p.relative_to(root)
    if p.is_symlink() or forbidden_dirs.intersection(relative.parts):
        failures.append(str(relative)); continue
    data = p.read_bytes()
    if any(pattern.search(data) for pattern in patterns): failures.append(str(relative))
if failures:
    print('Publication scan failed in: ' + ', '.join(sorted(set(failures))))
    sys.exit(1)
print('Publication scan passed: %d files; no personal home paths, common personal emails or API-key patterns.' % len(files))
