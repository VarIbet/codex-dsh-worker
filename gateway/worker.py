#!/usr/bin/env python3
"""On-demand, task-scoped DSH supervisor. Standard library only."""
import argparse
import contextlib
import fcntl
import hashlib
import http.server
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from vault import configure, get as get_key

ROOT = Path(os.environ.get('DSH_WORKER_ROOT', str(Path.home() / '.local/share/codex-dsh-worker'))).resolve()
INSTALL_CONFIG = json.loads((ROOT / 'config/install.json').read_text()) if (ROOT / 'config/install.json').exists() else {}
NODE = os.environ.get('DSH_WORKER_NODE') or INSTALL_CONFIG.get('node') or shutil.which('node') or '/usr/bin/node'
PYTHON = INSTALL_CONFIG.get('python', sys.executable)
ACTIVE = {'running', 'starting'}
TERMINAL = {'completed', 'failed', 'cancelled', 'timeout', 'interrupted'}

def save(path, obj):
    tmp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')
    tmp.chmod(0o600)
    tmp.replace(path)

def read(path):
    return json.loads(path.read_text())

def init():
    os.umask(0o077)
    for d in ('config', 'gateway', 'tasks', 'logs'):
        (ROOT / d).mkdir(parents=True, exist_ok=True)

def task(tid):
    if not tid or any(c not in '0123456789abcdef-' for c in tid):
        raise ValueError('Invalid task ID')
    p = ROOT / 'tasks' / tid
    if p.is_symlink() or not p.is_dir():
        raise ValueError('Unknown task')
    return p

@contextlib.contextmanager
def task_lock(p):
    with (p / '.lock').open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield

def digest(p):
    h = hashlib.sha256()
    h.update(p.name.encode() + b'\0')
    for f in [p / 'request.md', p / 'spec.json'] + sorted((p / 'input').rglob('*')) + sorted((p / 'workspace').rglob('*')):
        if f.is_symlink():
            raise ValueError('Symlinks forbidden in approved inputs')
        if f.is_file():
            h.update(str(f.relative_to(p)).encode() + b'\0' + f.read_bytes() + b'\0')
    return h.hexdigest()

def create(prompt, source=None, timeout=300, requests=12, tokens=4096):
    if not 1 <= timeout <= 3600 or not 1 <= requests <= 50 or not 64 <= tokens <= 16384:
        raise ValueError('Budget out of range')
    tid = str(uuid.uuid4())
    p = ROOT / 'tasks' / tid
    for name in ('input', 'workspace', 'output', 'logs', 'home', 'tmp'):
        (p / name).mkdir(parents=True, mode=0o700)
    (p / 'request.md').write_text(prompt)
    if source:
        source = Path(source).resolve()
        if not source.is_dir():
            raise ValueError('Input must be a directory')
        # Reject links, sockets, and oversized snapshots; no automatic private-file copying.
        total = 0
        for f in source.rglob('*'):
            if f.is_symlink() or not (f.is_dir() or f.is_file()):
                raise ValueError('Input contains special files or symlinks')
            if f.is_file():
                if f.name in ('.env', '.credentials.yaml', 'dshapi.txt') or f.suffix in ('.pem', '.key'):
                    raise ValueError('Possible credential in input; prepare a sanitized snapshot')
                total += f.stat().st_size
                if total > 20 * 1024 * 1024:
                    raise ValueError('Input snapshot exceeds 20 MiB')
        shutil.copytree(source, p / 'input', dirs_exist_ok=True)
    for f in (p / 'input').rglob('*'):
        f.chmod(0o500 if f.is_dir() else 0o400)
    save(p / 'spec.json', {'model': 'deepseek-flash', 'timeout_seconds': timeout,
         'max_requests': requests, 'max_output_tokens_per_request': tokens,
         'max_request_bytes': 2 * 1024 * 1024, 'gateway_retries': 0, 'provider_max_retries': 1,
         'input_policy': 'sanitized snapshot; original never mounted'})
    save(p / 'status.json', {'id': tid, 'state': 'pending', 'created': time.time(), 'approval': None})
    return tid

def approve(tid, evidence=None, dialog=False):
    p = task(tid)
    with task_lock(p):
        s = read(p / 'status.json')
        if s['state'] != 'pending':
            raise ValueError('Only pending tasks can be authorized')
        d = digest(p)
        if dialog:
            # Fixed script + argv: never interpolate untrusted task text into AppleScript.
            msg = 'DSH 调用申请\n任务 ID: ' + tid + '\n' + (p / 'request.md').read_text()[:2000] + '\n预算: ' + json.dumps(read(p / 'spec.json'))
            script = 'on run argv\n display dialog (item 1 of argv) buttons {"拒绝", "批准"} default button "批准" cancel button "拒绝" with title "DSH Worker 任务审批"\nend run'
            r = subprocess.run(['/usr/bin/osascript', '-e', script, msg], capture_output=True)
            if r.returncode:
                s.update(state='rejected', approval=None)
                save(p / 'status.json', s)
                raise ValueError('User rejected or closed approval')
            origin = 'macOS dialog: approved by user (same-user automation is not excluded)'
        else:
            if not evidence:
                raise ValueError('Approval requires genuine user-message evidence or --dialog')
            text = Path(evidence).read_text().strip()
            if not text:
                raise ValueError('Empty user authorization evidence')
            (p / 'authorization-evidence.md').write_text(text)
            origin = 'Codex attestation of explicit user authorization; not independently authenticated'
        s['approval'] = {'task_id': tid, 'digest': d, 'origin': origin, 'issued': time.time(),
                         'expires': time.time() + 1800, 'consumed': False}
        s['state'] = 'approved'
        save(p / 'status.json', s)

def validate(p, s):
    a = s.get('approval')
    if (s['state'] != 'approved' or not a or a.get('task_id') != p.name or a['consumed'] or a['expires'] < time.time()
            or a['digest'] != digest(p)):
        raise ValueError('Missing, expired, consumed, or changed authorization')

def sandbox_profile(p, port):
    # Default-deny Seatbelt applies to the DSH host and all descendants.
    q = lambda x: json.dumps(str(x))
    roots = ['/System', '/usr', '/bin', '/sbin', '/Library/Apple', '/Library/Developer/CommandLineTools',
             str(Path(NODE).resolve()), str(Path(PYTHON).resolve().parent.parent),
             *INSTALL_CONFIG.get('read_roots', []), str(ROOT / 'runtime/node_modules'),
             str(p / 'input'), str(p / 'workspace'), str(p / 'home'), str(p / 'tmp'),
             str(p / 'runner.patch.json')]
    for executable in (NODE, PYTHON):
        for ancestor in Path(executable).resolve().parents:
            if ancestor.name == 'Cellar':
                roots.extend(str(ancestor.parent / name) for name in ('Cellar', 'opt', 'lib'))
                break
    # Resolve aliases such as /tmp and Homebrew opt links before generating grants.
    roots = list(dict.fromkeys(str(Path(r).resolve()) for r in roots))
    lines = ['(version 1)', '(deny default)', '(allow process-fork)', '(allow process-exec)',
             '(allow sysctl-read)', '(allow file-read-metadata)', '(allow file-read-data (literal "/"))',
             '(allow mach-lookup (global-name "com.apple.system.logger"))',
             '(allow file-read* (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random"))',
             '(allow file-write* (literal "/dev/null"))']
    lines += ['(allow file-read* (subpath %s))' % q(r) for r in roots]
    lines += ['(allow file-write* (subpath %s))' % q(p / r) for r in ('workspace', 'home', 'tmp')]
    lines += ['(allow network-outbound (remote ip "localhost:%d"))' % port]
    return '\n'.join(lines)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

class Broker:
    def __init__(self, key, spec, token):
        self.key, self.spec, self.token = key, spec, token
        self.metrics = {'model': spec['model'], 'requests': 0, 'successful_requests': 0,
                        'input_tokens': 0, 'output_tokens': 0, 'cache_hit_tokens': 0,
                        'usage_reports': 0, 'actual_cost': None, 'cost_currency': None,
                        'estimated_cost': None, 'retries': None, 'gateway_retries': 0, 'http_statuses': []}
        self.lock = threading.Lock()
        self.opener = urllib.request.build_opener(NoRedirect())
        self.stopped = False

    def handler(self):
        broker = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # No headers, prompts, or credentials in ordinary logs.
            def do_POST(self):
                if self.headers.get('Authorization') != 'Bearer ' + broker.token:
                    self.send_error(403); return
                if self.path != '/v1/chat/completions':
                    self.send_error(403); return
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= broker.spec['max_request_bytes']:
                        self.send_error(413); return
                    data = json.loads(self.rfile.read(size))
                    if data.get('model') != broker.spec['model']:
                        self.send_error(403); return
                    with broker.lock:
                        if broker.stopped or broker.metrics['requests'] >= broker.spec['max_requests']:
                            self.send_error(429); return
                        broker.metrics['requests'] += 1
                    # Rebuild an allowlisted payload. Disable search, thinking, storage extensions.
                    body = {k: data[k] for k in ('model', 'messages', 'tools', 'tool_choice', 'temperature', 'stream', 'stop') if k in data}
                    body['max_tokens'] = min(int(data.get('max_tokens', broker.spec['max_output_tokens_per_request'])), broker.spec['max_output_tokens_per_request'])
                    body['thinking'] = {'type': 'disabled'}
                    if body.get('stream'):
                        body['stream_options'] = {'include_usage': True}
                    req = urllib.request.Request('https://api.deepseek.com/chat/completions',
                        data=json.dumps(body).encode(), headers={'Authorization': 'Bearer ' + broker.key, 'Content-Type': 'application/json'})
                    with broker.opener.open(req, timeout=60) as upstream:
                        status = upstream.status
                        raw = upstream.read(16 * 1024 * 1024 + 1)
                        content_type = upstream.headers.get('Content-Type', 'application/json')
                    if len(raw) > 16 * 1024 * 1024:
                        raise ValueError('Response too large')
                    with broker.lock:
                        broker.metrics['http_statuses'].append(status)
                        broker.metrics['successful_requests'] += 1
                        records = []
                        if 'event-stream' in content_type:
                            for line in raw.splitlines():
                                if line.startswith(b'data: ') and line[6:] != b'[DONE]':
                                    records.append(json.loads(line[6:]))
                        else:
                            records.append(json.loads(raw))
                        usages = [r['usage'] for r in records if r.get('usage')]
                        if usages:
                            u = usages[-1]
                            broker.metrics['usage_reports'] += 1
                            broker.metrics['input_tokens'] += u.get('prompt_tokens', 0)
                            broker.metrics['output_tokens'] += u.get('completion_tokens', 0)
                            broker.metrics['cache_hit_tokens'] += u.get('prompt_cache_hit_tokens', u.get('prompt_tokens_details', {}).get('cached_tokens', 0))
                    self.send_response(status)
                    self.send_header('Content-Type', content_type)
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers(); self.wfile.write(raw)
                except urllib.error.HTTPError as e:
                    with broker.lock:
                        broker.metrics['http_statuses'].append(e.code)
                    self.send_error(e.code, 'Upstream API request failed')
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception:
                    self.send_error(502, 'Model gateway failed; credential and upstream details redacted')
        return Handler

def patch(port, p=None):
    disabled = ['llm-deepseek', 'llm-deepseek-account', 'session-log-deepseek',
                'plugin-package-inventory-deepseek', 'subagent-spawn-in-process',
                'subagent-fork-in-process', 'tool-subagent', 'tool-subagent-fork',
                'tool-subagent-control', 'web-search-deepseek', 'web-fetch-http']
    rows = [{'id': x, 'disabled': True} for x in disabled]
    rows += [{'id': 'agent-default-model', 'config': {'provider': 'worker', 'model': 'deepseek-flash'}},
             {'id': 'llm-pi-ai', 'config': {'providers': {'worker': {
                 'api': 'openai-completions', 'apiKeyEnv': 'WORKER_GATEWAY_TOKEN',
                 'baseURL': 'http://127.0.0.1:%d/v1' % port,
                 'retryPolicy': {'mode': 'normal', 'maxRetries': 1},
                 'compat': {'supportsDeveloperRole': False, 'maxTokensField': 'max_tokens'},
                 'models': [{'id': 'deepseek-flash', 'contextWindow': 1048576, 'maxTokens': 4096}]
             }}}}]
    if p is not None:
        rows.append({'id': 'session-persistence-jsonl', 'config': {'root': str(p / 'home/.dsh/sessions'), 'compression': 'none'}})
    return rows

def kill_group(proc):
    if proc.poll() is None:
        try: os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError: pass
        try: proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try: os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            proc.wait()
    # Kill descendants left in the process group after the group leader exits.
    try: os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError: pass

def run(tid):
    p = task(tid)
    if (ROOT / 'config/disabled').exists():
        raise ValueError('Worker disabled')
    with (ROOT / 'config/run.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('Concurrency limit is 1')
        with task_lock(p):
            s = read(p / 'status.json'); validate(p, s)
            spec = read(p / 'spec.json')
            key = get_key(ROOT)
            s['approval']['consumed'] = True
            s.update(state='starting', started=time.time(), supervisor_pid=os.getpid())
            save(p / 'status.json', s)
        broker, server, proc = None, None, None
        previous = {}
        try:
            broker = Broker(key, spec, secrets.token_urlsafe(32))
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), broker.handler())
            server.daemon_threads = True
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            save(p / 'runner.patch.json', patch(port, p))
            (p / 'sandbox.sb').write_text(sandbox_profile(p, port))
            # Task gets a clean home, clean configuration and no inherited credential environment.
            env = {'HOME': str(p / 'home'), 'DSH_HOME': str(p / 'home/.dsh'),
                   'TMPDIR': str(p / 'tmp'), 'PATH': str(Path(NODE).parent) + ':' + str(Path(PYTHON).parent) + ':/usr/bin:/bin:/usr/sbin:/sbin',
                   'LANG': 'en_US.UTF-8', 'OPENSSL_CONF': '/dev/null', 'WORKER_GATEWAY_TOKEN': broker.token,
                   'NODE_OPTIONS': '--max-old-space-size=2048'}
            cli = ROOT / 'runtime/node_modules/@deepseek-ai/dsh/lib/bin.js'
            cmd = ['/usr/bin/sandbox-exec', '-f', str(p / 'sandbox.sb'), NODE, str(cli),
                   '--profile', 'headless', '--patch', str(p / 'runner.patch.json'),
                   '--json', '--permission-mode', 'danger-full-access', '--approval-policy', 'never', '-']
            prompt = ('Operate only in the current workspace. Read-only input is at ' + str(p / 'input') +
                      '. Do not access private files, install software, spawn other agents, or use other services.\n\n' + (p / 'request.md').read_text())
            cancelled = threading.Event()
            def stop(sig, frame): cancelled.set()
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.signal(sig, stop)
            with (p / 'logs/events.jsonl').open('wb') as out, (p / 'logs/stderr.log').open('wb') as err:
                proc = subprocess.Popen(cmd, cwd=str(p / 'workspace'), env=env,
                                        stdin=subprocess.PIPE, stdout=out, stderr=err, start_new_session=True)
                s.update(state='running', child_pid=proc.pid); save(p / 'status.json', s)
                proc.stdin.write(prompt.encode()); proc.stdin.close()
                state = None
                deadline = time.monotonic() + spec['timeout_seconds']
                while proc.poll() is None:
                    if cancelled.is_set() or (p / 'cancel.request').exists() or (ROOT / 'config/disabled').exists():
                        state = 'cancelled'; break
                    if time.monotonic() > deadline:
                        state = 'timeout'; break
                    time.sleep(0.1)
                broker.stopped = True
                kill_group(proc)
            final = None
            for line in (p / 'logs/events.jsonl').read_text(errors='replace').splitlines():
                try:
                    event = json.loads(line)
                    if event.get('type') == 'final': final = event
                except (ValueError, TypeError): pass
            if state is None:
                state = 'completed' if proc.returncode == 0 and final and (final.get('reason') or {}).get('kind') == 'completed' else 'failed'
            report = None
            if final:
                raw_report = final.get('text', '').strip()
                if raw_report.startswith('```'):
                    raw_report = '\n'.join(raw_report.splitlines()[1:-1])
                try:
                    candidate = json.loads(raw_report)
                    if isinstance(candidate, dict): report = candidate
                except (TypeError, ValueError): pass
            save(p / 'output/summary.json', report or {'completed': state == 'completed',
                 'summary': (final or {}).get('text', '')[:8000], 'needs_codex': True,
                 'issues': ['Worker did not return a structured JSON report'], 'detailed_result_path': str(p / 'logs')})
            save(p / 'output/result.json', {'completed': state == 'completed', 'final': final,
                 'workspace': str(p / 'workspace'), 'review_required': True,
                 'acceptance_state': 'pending_codex_review', 'structured_report': report is not None,
                 'files': sorted(str(f.relative_to(p / 'workspace')) for f in (p / 'workspace').rglob('*') if f.is_file()),
                 'logs': str(p / 'logs')})
            s.update(state=state, exit_code=proc.returncode, ended=time.time())
        except Exception as e:
            if proc: kill_group(proc)
            s.update(state='failed', ended=time.time(), error=type(e).__name__ + ': supervisor failed; inspect local configuration')
        finally:
            if broker: broker.stopped = True
            if server: server.shutdown(); server.server_close()
            for sig, handler in previous.items(): signal.signal(sig, handler)
            if broker:
                broker.metrics['duration_seconds'] = round(time.time() - s['started'], 3)
                session_root = p / 'home/.dsh/sessions'
                session_files = list(session_root.rglob('*.jsonl'))
                if session_files:
                    count = 0
                    for f in session_files:
                        for line in f.open(errors='replace'):
                            try:
                                event = json.loads(line)
                                if event.get('type') == 'llm/retry-started': count += 1
                            except (ValueError, TypeError): pass
                    broker.metrics['retries'] = count
                broker.metrics['estimated_cost'] = estimate_cost(broker.metrics)
                broker.metrics['unaccounted_requests'] = broker.metrics['requests'] - broker.metrics['usage_reports']
                if broker.metrics['usage_reports'] != broker.metrics['requests']:
                    broker.metrics['usage_complete'] = False
                else: broker.metrics['usage_complete'] = True
                save(p / 'output/metrics.json', broker.metrics)
            s['approval']['expires'] = 0
            save(p / 'status.json', s)
        print(json.dumps({'id': tid, 'state': s['state'], 'output': str(p / 'output')}, ensure_ascii=False))
        return 0 if s['state'] == 'completed' else 1

def estimate_cost(metrics):
    """Price snapshot estimate only; actual account debit is unknown."""
    f = ROOT / 'config/pricing.json'
    if not f.exists(): return None
    prices = read(f)
    if prices.get('model') != metrics['model'] or prices.get('valid_until', 0) < time.time(): return None
    hit = metrics['cache_hit_tokens']
    miss = max(0, metrics['input_tokens'] - hit)
    output = metrics['output_tokens']
    return {'currency': 'USD', 'kind': 'published-price range, not actual debit',
            'off_peak': round((hit * prices['cache_hit'] + miss * prices['cache_miss'] + output * prices['output']) / 1000000, 9),
            'peak': round((hit * prices['cache_hit'] + miss * prices['cache_miss'] + output * prices['output']) / 500000, 9),
            'source': prices['source'], 'verified_date': prices['verified_date']}

def cancel(tid):
    p = task(tid)
    with task_lock(p):
        s = read(p / 'status.json')
        if s['state'] in TERMINAL: return
        (p / 'cancel.request').touch()
        if s['state'] not in ACTIVE:
            s.update(state='cancelled', approval=None); save(p / 'status.json', s)

def status():
    return {'root': str(ROOT), 'enabled': not (ROOT / 'config/disabled').exists(),
            'mode': 'approval', 'concurrency': 1, 'model': 'deepseek-flash',
            'credential_store': 'restricted file' if (ROOT / 'config/api-key').exists() else 'macOS Keychain (not read by status)',
            'cli_installed': (ROOT / 'runtime/node_modules/.bin/dsh').exists()}

def recover():
    """Recover only after the exclusive supervisor lock is available."""
    with (ROOT / 'config/run.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('Supervisor is still active; use cancel')
        for p in (ROOT / 'tasks').iterdir():
            if p.is_symlink() or not (p / 'status.json').exists(): continue
            with task_lock(p):
                s = read(p / 'status.json')
                if s['state'] not in ACTIVE: continue
                pid = s.get('child_pid')
                if pid:
                    r = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True)
                    # Do not signal a reused PID or an unrelated process group.
                    if str(p / 'runner.patch.json') in r.stdout and os.getpgid(pid) == pid:
                        try: os.killpg(pid, signal.SIGKILL)
                        except ProcessLookupError: pass
                s.update(state='interrupted', ended=time.time(), recovery='Supervisor absent; matching surviving leader killed when found')
                if s.get('approval'):
                    s['approval'].update(consumed=True, expires=0)
                save(p / 'status.json', s)
                print(s['id'], s['state'])

def main():
    init()
    ap = argparse.ArgumentParser(description='Codex × DSH task gateway')
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('status', 'test', 'tasks', 'enable', 'disable', 'recover'):
        sub.add_parser(name)
    c = sub.add_parser('configure'); c.add_argument('--key-file'); c.add_argument('--allow-file-fallback', action='store_true')
    c = sub.add_parser('create'); c.add_argument('--request', required=True); c.add_argument('--input')
    c.add_argument('--timeout', type=int, default=300); c.add_argument('--max-requests', type=int, default=12)
    c.add_argument('--max-output-tokens', type=int, default=4096)
    a = sub.add_parser('authorize'); a.add_argument('id'); g = a.add_mutually_exclusive_group(required=True)
    g.add_argument('--user-message'); g.add_argument('--dialog', action='store_true')
    for name in ('run', 'logs', 'cancel', 'reject'):
        sub.add_parser(name).add_argument('id')
    c = sub.add_parser('clean'); c.add_argument('--older-than-days', type=int, default=7); c.add_argument('--execute', action='store_true')
    args = ap.parse_args()
    if args.cmd == 'status': print(json.dumps(status(), ensure_ascii=False, indent=2))
    elif args.cmd == 'configure': print('Credential configured: ' + configure(ROOT, args.key_file, args.allow_file_fallback))
    elif args.cmd == 'recover': recover()
    elif args.cmd == 'test':
        # Deliberately local and free. Does not read Keychain.
        print(json.dumps(status(), ensure_ascii=False))
        cli = ROOT / 'runtime/node_modules/.bin/dsh'
        return subprocess.run([NODE, str(ROOT / 'runtime/node_modules/@deepseek-ai/dsh/lib/bin.js'), '--version'], env={'PATH': str(Path(NODE).parent) + ':/usr/bin:/bin'}).returncode
    elif args.cmd == 'tasks':
        for p in sorted((ROOT / 'tasks').iterdir()):
            if (p / 'status.json').is_file():
                s = read(p / 'status.json'); print(s['id'], s['state'])
    elif args.cmd == 'create':
        print(create(Path(args.request).read_text(), args.input, args.timeout, args.max_requests, args.max_output_tokens))
    elif args.cmd == 'authorize': approve(args.id, args.user_message, args.dialog)
    elif args.cmd == 'run': return run(args.id)
    elif args.cmd == 'cancel': cancel(args.id)
    elif args.cmd == 'reject':
        p = task(args.id)
        with task_lock(p):
            s = read(p / 'status.json')
            if s['state'] not in ('pending', 'approved'): raise ValueError('Cannot reject an active or finished task')
            s.update(state='rejected', approval=None); save(p / 'status.json', s)
    elif args.cmd == 'logs':
        p = task(args.id)
        print('Detailed logs (may contain task data): ' + str(p / 'logs'))
        if (p / 'output/result.json').exists(): print((p / 'output/result.json').read_text()[:12000])
    elif args.cmd == 'disable': (ROOT / 'config/disabled').touch(); print('Disabled; active task will be cancelled')
    elif args.cmd == 'enable': (ROOT / 'config/disabled').unlink(missing_ok=True); print('Enabled: approval mode')
    elif args.cmd == 'clean':
        if args.older_than_days < 1: raise ValueError('Retention must be at least 1 day')
        cutoff = time.time() - args.older_than_days * 86400
        for p in (ROOT / 'tasks').iterdir():
            if p.is_symlink() or not (p / 'status.json').is_file(): continue
            with task_lock(p):
                s = read(p / 'status.json')
                if s['state'] in TERMINAL | {'rejected'} and s.get('ended', s['created']) < cutoff:
                    print(('Cleaning temporary data: ' if args.execute else 'Would clean temporary data: ') + p.name)
                    if args.execute:
                        for d in ('tmp', 'home/.cache'):
                            shutil.rmtree(p / d, ignore_errors=True)
    return 0

if __name__ == '__main__':
    try: sys.exit(main())
    except (ValueError, RuntimeError, OSError) as e:
        print('dsh-worker: ' + str(e), file=sys.stderr)
        sys.exit(2)
