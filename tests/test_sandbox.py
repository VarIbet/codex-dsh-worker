"""Real macOS Seatbelt probes using nonsecret canaries; no API requests."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import worker as w

@unittest.skipUnless(sys.platform == 'darwin' and Path(w.NODE).is_file(), 'Requires macOS and Node.js')
class SandboxTests(unittest.TestCase):
    def test_real_files_network_and_credential_boundary(self):
        with tempfile.TemporaryDirectory(prefix='dsh-seatbelt-') as tmp:
            base = Path(tmp).resolve(); p = base / 'task'; p.mkdir()
            for d in ('workspace', 'input', 'home', 'tmp'): (p / d).mkdir()
            (base / 'gateway-canary/gateway').mkdir(parents=True); (base / 'gateway-canary/gateway/worker.py').write_text('nonsecret gateway canary')
            outside = base / 'private-canary'; outside.write_text('nonsecret unrelated data')
            (p / 'input/readme').write_text('authorized input')
            with socket.socket() as allowed, socket.socket() as denied:
                allowed.bind(('127.0.0.1', 0)); allowed.listen()
                denied.bind(('127.0.0.1', 0)); denied.listen()
                good, bad = allowed.getsockname()[1], denied.getsockname()[1]
                (p / 'sandbox.sb').write_text(w.sandbox_profile(p, good))
                code = r'''
const fs=require('fs'),net=require('net'),cp=require('child_process');
const [p,out,good,bad]=process.argv.slice(1);let r={};
function check(name,fn){try{fn();r[name]=true}catch(e){r[name]=e.code||'denied'}}
check('allowed_read',()=>fs.readFileSync(p+'/input/readme'));
check('allowed_write',()=>fs.writeFileSync(p+'/workspace/result','ok'));
check('outside_read',()=>fs.readFileSync(out));
check('outside_write',()=>fs.writeFileSync(out,'changed'));
check('input_write',()=>fs.writeFileSync(p+'/input/readme','changed'));
check('symlink_escape',()=>{fs.symlinkSync(out,p+'/workspace/link');fs.readFileSync(p+'/workspace/link')});
check('gateway_config_read',()=>fs.readFileSync(process.env.GATEWAY_ROOT+'/gateway/worker.py'));
check('private_canary_read',()=>fs.openSync(process.env.PRIVATE_CANARY,'r'));
let sec=cp.spawnSync('/usr/bin/security',['find-generic-password','-s','Codex-DSH-Worker'],{encoding:'utf8',timeout:3000});
r.keychain_query_exit=sec.status;r.keychain_query_blocked=sec.status!==0;
function connect(port){return new Promise(resolve=>{let s=net.connect({host:'127.0.0.1',port:+port});let end=v=>{s.destroy();resolve(v)};s.on('connect',()=>end('connected'));s.on('error',e=>end(e.code));s.setTimeout(1000,()=>end('timeout'))})}
(async()=>{r.allowed_network=await connect(good);r.other_local_network=await connect(bad);console.log(JSON.stringify(r))})()
'''
                env = {'PATH': '/usr/bin:/bin', 'HOME': str(p / 'home'), 'OPENSSL_CONF': '/dev/null', 'GATEWAY_ROOT': str(base / 'gateway-canary'), 'PRIVATE_CANARY': str(outside)}
                cmd = ['/usr/bin/sandbox-exec', '-f', str(p / 'sandbox.sb'), w.NODE, '-e', code, str(p), str(outside), str(good), str(bad)]
                r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(r.returncode, 0, r.stderr)
                report = json.loads(r.stdout)
                for name in ('allowed_read','allowed_write','keychain_query_blocked'): self.assertTrue(report[name], report)
                for name in ('outside_read','outside_write','input_write','symlink_escape','gateway_config_read','private_canary_read'):
                    self.assertIn(report[name], ('EPERM','EACCES'), report)
                self.assertEqual(report['allowed_network'], 'connected', report)
                self.assertNotEqual(report['other_local_network'], 'connected', report)
                self.assertEqual(outside.read_text(), 'nonsecret unrelated data')
                print('\nSeatbelt evidence: ' + json.dumps(report))

if __name__ == '__main__': unittest.main(verbosity=2)
