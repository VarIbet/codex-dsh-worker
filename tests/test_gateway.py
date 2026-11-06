import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import worker as w


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dsh-tests-')
        self.old = w.ROOT
        w.ROOT = Path(self.tmp.name).resolve()
        w.init()
        self.evidence = w.ROOT / 'user-message.md'
        self.evidence.write_text('TEST FIXTURE ONLY: approve the specified synthetic local test; no real API calls')

    def tearDown(self):
        w.ROOT = self.old
        self.tmp.cleanup()

    def approved(self, timeout=5):
        tid = w.create('Synthetic test task', timeout=timeout, requests=2)
        w.approve(tid, str(self.evidence))
        return tid, w.task(tid)

    def test_unauthorized_rejected_before_key_or_model(self):
        tid = w.create('not authorized')
        with patch.object(w, 'get_key', side_effect=AssertionError('key must not be read')):
            with self.assertRaises(ValueError): w.run(tid)

    def test_missing_evidence_rejected(self):
        tid = w.create('pending')
        with self.assertRaises(ValueError): w.approve(tid)

    def test_expired_and_changed_scope_rejected(self):
        tid, p = self.approved()
        s = w.read(p / 'status.json')
        w.validate(p, s)
        (p / 'request.md').write_text('changed')
        with self.assertRaises(ValueError): w.validate(p, s)
        (p / 'request.md').write_text('Synthetic test task')
        s['approval']['expires'] = 0
        with self.assertRaises(ValueError): w.validate(p, s)

    def test_authorization_not_cross_task(self):
        tid, p = self.approved()
        other = w.task(w.create('different work'))
        s = w.read(p / 'status.json')
        with self.assertRaises(ValueError): w.validate(other, s)
        # Identical prompts and budgets still have different task identities.
        other = w.task(w.create('Synthetic test task', timeout=5, requests=2))
        with self.assertRaises(ValueError): w.validate(other, s)

    def test_dialog_approval_and_rejection_adapter(self):
        tid = w.create('dialog fixture')
        with patch.object(w.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaises(ValueError): w.approve(tid, dialog=True)
        self.assertEqual(w.read(w.task(tid) / 'status.json')['state'], 'rejected')
        tid = w.create('dialog fixture')
        with patch.object(w.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
            w.approve(tid, dialog=True)
        p = w.task(tid)
        w.validate(p, w.read(p / 'status.json'))

    def test_cancel_and_reject_invalidate_approval(self):
        tid, p = self.approved()
        w.cancel(tid)
        s = w.read(p / 'status.json')
        self.assertEqual(s['state'], 'cancelled')
        with self.assertRaises(ValueError): w.validate(p, s)
        tid, p = self.approved()
        s = w.read(p / 'status.json'); s.update(state='rejected', approval=None); w.save(p / 'status.json', s)
        with self.assertRaises(ValueError): w.run(tid)

    def test_disable_blocks_approved_task(self):
        tid, p = self.approved()
        (w.ROOT / 'config/disabled').touch()
        with self.assertRaises(ValueError): w.run(tid)

    def test_input_snapshot_readonly_and_symlinks_refused(self):
        src = w.ROOT / 'source'; src.mkdir(); (src / 'hello').write_text('original')
        tid = w.create('snapshot', src)
        p = w.task(tid)
        self.assertEqual((p / 'input/hello').stat().st_mode & 0o777, 0o400)
        (p / 'workspace/hello').write_text('changed')
        self.assertEqual((src / 'hello').read_text(), 'original')
        (src / 'link').symlink_to(src / 'hello')
        with self.assertRaises(ValueError): w.create('bad snapshot', src)

    def run_fixture(self, code, timeout=5, cancel=False):
        tid, p = self.approved(timeout)
        real = subprocess.Popen
        def fake_cli(cmd, **kwargs):
            return real([sys.executable, '-c', code], **kwargs)
        if cancel:
            threading.Thread(target=lambda: (time.sleep(.3), (p / 'cancel.request').touch()), daemon=True).start()
        with patch.object(w, 'get_key', return_value='synthetic-noncredential'), patch.object(w.subprocess, 'Popen', side_effect=fake_cli), contextlib.redirect_stdout(io.StringIO()):
            rc = w.run(tid)
        return rc, w.read(p / 'status.json'), p

    def test_process_success_and_single_use(self):
        rc, s, p = self.run_fixture('import json;print(json.dumps({"type":"final","reason":{"kind":"completed"},"text":"ok"}))')
        self.assertEqual((rc, s['state']), (0, 'completed'))
        self.assertTrue(s['approval']['consumed'])
        with self.assertRaises(ValueError): w.run(s['id'])

    def test_malformed_or_failed_output_is_failure(self):
        for code in ['print("not json")', 'raise SystemExit(7)', 'print(\'{"type":"final","reason":{"kind":"error"}}\')']:
            rc, s, p = self.run_fixture(code)
            self.assertEqual((rc, s['state']), (1, 'failed'))

    def test_timeout_terminates_process(self):
        rc, s, p = self.run_fixture('import time;time.sleep(20)', timeout=1)
        self.assertEqual(s['state'], 'timeout')
        with self.assertRaises(ProcessLookupError): __import__('os').kill(s['child_pid'], 0)

    def test_cancel_terminates_process(self):
        rc, s, p = self.run_fixture('import time;time.sleep(20)', cancel=True)
        self.assertEqual(s['state'], 'cancelled')
        with self.assertRaises(ProcessLookupError): __import__('os').kill(s['child_pid'], 0)

    def test_concurrency_limit(self):
        tid, p = self.approved()
        with (w.ROOT / 'config/run.lock').open('a') as f:
            w.fcntl.flock(f, w.fcntl.LOCK_EX)
            with self.assertRaises(ValueError): w.run(tid)

    def test_recover_consumes_orphan_authorization(self):
        tid, p = self.approved()
        s = w.read(p / 'status.json'); s.update(state='running'); w.save(p / 'status.json', s)
        with contextlib.redirect_stdout(io.StringIO()): w.recover()
        s = w.read(p / 'status.json')
        self.assertEqual(s['state'], 'interrupted')
        self.assertEqual(s['approval']['expires'], 0)

    def test_broker_auth_model_budget_usage_and_error(self):
        spec = {'model': 'deepseek-flash', 'max_requests': 2, 'max_request_bytes': 1024, 'max_output_tokens_per_request': 128}
        broker = w.Broker('synthetic-upstream-key', spec, 'synthetic-task-token')
        server = w.http.server.ThreadingHTTPServer(('127.0.0.1', 0), broker.handler())
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = 'http://127.0.0.1:%d' % server.server_address[1]
        local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        calls = []
        class Response:
            status = 200
            headers = {'Content-Type': 'application/json'}
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self, n): return json.dumps({'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'prompt_cache_hit_tokens': 3}}).encode()
        def upstream(req, **kw):
            calls.append(req)
            return Response()
        def request(model='deepseek-flash', token='synthetic-task-token', path='/v1/chat/completions'):
            req = urllib.request.Request(url + path, data=json.dumps({'model': model, 'messages': [], 'max_tokens': 10000, 'web_search': True, 'dsh_session_log': 'private'}).encode(), headers={'Authorization': 'Bearer ' + token})
            return local.open(req, timeout=2)
        def denied(code, **kw):
            with self.assertRaises(urllib.error.HTTPError) as err: request(**kw)
            self.assertEqual(err.exception.code, code)
        try:
            with patch.object(broker.opener, 'open', side_effect=upstream):
                denied(403, token='wrong')
                denied(403, model='deepseek-v4-pro')
                denied(403, path='/v1/files')
                request().close()
                body = json.loads(calls[0].data)
                self.assertEqual(body['max_tokens'], 128)
                self.assertNotIn('web_search', body)
                self.assertNotIn('dsh_session_log', body)
                self.assertEqual(broker.metrics['input_tokens'], 10)
                self.assertEqual(broker.metrics['cache_hit_tokens'], 3)
            with patch.object(broker.opener, 'open', side_effect=urllib.error.HTTPError('https://api.deepseek.com', 401, 'unauthorized', {}, None)):
                denied(401)
            denied(429)
            self.assertEqual(broker.metrics['requests'], 2)
        finally:
            server.shutdown(); server.server_close()


if __name__ == '__main__': unittest.main(verbosity=2)
