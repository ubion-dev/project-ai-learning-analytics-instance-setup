"""Synthetic GNOME waiting-window ownership and placement; not panel acceptance."""
import hashlib
import json
import pytest
from test_gnome_placement import run_extension


WAIT_SETUP = """
const output='DP-1';
const digest=DIGEST;
GLib.ChecksumType={SHA256:1};
GLib.compute_checksum_for_string=(kind,text,length) => {
    assert.equal(kind,1); assert.equal(text,output); assert.equal(length,-1); return digest;
};
const policy={protocol:1,enabled:true,user:'user'};
const syncPolicy=() => files.set('/var/lib/ala-runtime/screen-coverage.json',JSON.stringify(policy));
syncPolicy();
const req=request('second'); req.target='ala-wait-'+digest.slice(0,20);
addProcess(req.supervisor_pid,1,'10',['/usr/bin/python3','-I','/opt/ala/releases/0.1.0rc33/entry.py','_screen-wait',output]);
addProcess(req.browser_pid,req.supervisor_pid,'20',['/opt/google/chrome/chrome',`--user-data-dir=/home/user/.config/ala/chrome-wait/${req.target}`]);
""".replace('DIGEST',json.dumps(hashlib.sha256(b'DP-1').hexdigest()))


@pytest.mark.parametrize('version',['47.4','49.0'])
def test_virtual_waiting_window_is_placed_on_its_output_without_changing_a_site_window(version):
    run_extension(WAIT_SETUP+"""
const extension=new AlaDisplay(); extension.enable();
const waiting=fakeWindow('second'),site=fakeWindow('first');
assert.equal((await invoke(extension,'Place',req)).state,'ok');
assert.deepEqual(waiting.rect,req.rect);
assert.deepEqual(site.calls,[]);
assert.equal(config.screens.length,2);
extension.disable();
""",version)


@pytest.mark.parametrize('corrupt',[
    'policy.enabled=false; syncPolicy()',
    "policy.user='other'; syncPolicy()",
    "req.target='ala-wait-'+ '0'.repeat(20)",
    "req.layout='left'",
    "req.output='DP-2'",
    'config.screens.push({id:req.target})',
    "files.set(`/proc/${req.supervisor_pid}/cmdline`,['/usr/bin/python3','-I','/opt/ala/current/entry.py','_screen-wait','DP-2'].join('\0')+'\0')",
    "files.set(`/proc/${req.browser_pid}/cmdline`,['/opt/google/chrome/chrome','--user-data-dir=/home/user/.config/ala/chrome-display'].join('\0')+'\0')",
])
def test_waiting_window_rejects_unapproved_policy_collision_identity_or_profile(corrupt):
    run_extension(WAIT_SETUP+"""
const extension=new AlaDisplay(); extension.enable();
const waiting=fakeWindow('second'),site=fakeWindow('first');
"""+corrupt+""";
assert.notEqual((await invoke(extension,'Place',req)).state,'ok');
assert.deepEqual(waiting.calls,[]); assert.deepEqual(site.calls,[]);
extension.disable();
""")
