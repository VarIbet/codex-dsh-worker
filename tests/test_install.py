"""Installer integration tests with a synthetic CLI; no npm network or API calls."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]

@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('node'), 'macOS and Node required')
class InstallTests(unittest.TestCase):
    def test_paths_with_spaces_rules_preserved_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / 'worker with spaces'
            bindir = base / 'bin with spaces'
            cli = root / 'runtime/node_modules/@deepseek-ai/dsh/lib/bin.js'
            cli.parent.mkdir(parents=True)
            cli.write_text("console.log('0.2.2-rc.1')\n")
            codex = base / 'codex home'; codex.mkdir()
            (codex / 'AGENTS.md').write_text('Existing user rules\n')
            env = dict(os.environ, CODEX_HOME=str(codex), PYTHONDONTWRITEBYTECODE='1')
            cmd = [sys.executable, str(SOURCE / 'install.py'), '--root', str(root), '--bin-dir', str(bindir), '--skip-runtime', '--with-codex-rules']
            for _ in range(2):
                result = subprocess.run(cmd, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            rules = (codex / 'AGENTS.md').read_text()
            self.assertTrue(rules.startswith('Existing user rules\n'))
            self.assertEqual(rules.count('<!-- BEGIN CODEX-DSH-WORKER -->'), 1)
            self.assertIn(str(root), rules)
            self.assertEqual(len(list((root / 'config').glob('AGENTS.backup-*.md'))), 1)
            status = subprocess.check_output([str(bindir / 'dsh-worker'), 'status'], env=env, text=True)
            self.assertEqual(json.loads(status)['root'], str(root))
            self.assertTrue(json.loads(status)['enabled'])
            result = subprocess.run([str(bindir / 'dsh-worker'), 'test'], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / 'config/api-key').exists())
            self.assertEqual(list((root / 'tasks').iterdir()), [])

    def test_existing_command_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp); bindir=base/'bin'; bindir.mkdir()
            command=bindir/'dsh-worker'; command.write_text('unrelated command')
            r=subprocess.run([sys.executable,str(SOURCE/'install.py'),'--root',str(base/'worker'),'--bin-dir',str(bindir)],capture_output=True,text=True)
            self.assertNotEqual(r.returncode,0)
            self.assertEqual(command.read_text(),'unrelated command')
            self.assertFalse((base/'worker').exists())
