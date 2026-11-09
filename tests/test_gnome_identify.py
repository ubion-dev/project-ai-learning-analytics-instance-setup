"""Run the real extension identify method against bounded Shell/GJS fakes."""
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


EXTENSION = Path(__file__).resolve().parents[1] / 'assets/gnome-extension/ala-display@example.com/extension.js'
HARNESS = r"""
import assert from 'node:assert/strict';
class Extension { constructor() { this.metadata = metadata; } }
const Meta = {};
const config = {user:'user', display:true};
let configMode = 0o644, callerUid = 1000, nextId = 1;
let deferred = false, failAdd = false;
const waiting = [], chrome = new Set(), timers = new Map();
const monitorSignals = new Map(), sessionSignals = new Map();
let displayState = [1,
    [[['HDMI-1','Vendor','Model',''],[],{}],[['DP-1','Vendor','Model',''],[],{}]],
    [[0,0,1,0,true,[['HDMI-1','Vendor','Model','']],{}],
     [1920,0,1,0,false,[['DP-1','Vendor','Model','']],{}]],{}];
const geometry = [{x:0,y:0,width:1920,height:1080},{x:1920,y:0,width:1920,height:1080}];
class Actor {
    constructor(properties) { Object.assign(this,properties); this.children=[]; this.destroyed=false; }
    add_child(child) { this.children.push(child); }
    destroy() {
        assert.equal(this.destroyed,false);
        this.destroyed=true;
        chrome.delete(this);
        for (const child of this.children) child.destroy();
    }
}
const St = {Widget:class extends Actor {}, Label:class extends Actor {}};
const Clutter = {BinLayout:class {}, ActorAlign:{CENTER:2}};
const Main = {
    sessionMode: {
        currentMode:'user',isGreeter:false,isLocked:false,
        connect(signal,callback) { assert.equal(signal,'updated'); const id=nextId++;sessionSignals.set(id,callback);return id; },
        disconnect(id) { assert.ok(sessionSignals.delete(id)); },
    },
    layoutManager: {
        monitors:geometry,
        connect(signal,callback) { assert.equal(signal,'monitors-changed');const id=nextId++;monitorSignals.set(id,callback);return id; },
        disconnect(id) { assert.ok(monitorSignals.delete(id)); },
        addTopChrome(actor,options) {
            assert.deepEqual(options,{trackFullscreen:false,affectsStruts:false,affectsInputRegion:false});
            chrome.add(actor);
            if (failAdd && chrome.size===2) throw new Error('partial actor failure');
        },
    },
};
const Gio = {
    Credentials:class {get_unix_user() {return 1000;}},
    DBusExportedObject:{wrapJSObject(xml) {
        assert.match(xml,/<method name="IdentifyOutputs"><arg type="s" direction="out"\/><\/method>/);
        return {export() {},unexport() {}};
    }},
    DBus:{session:{
        call(_bus,_path,_iface,method,_params,_type,_flags,_timeout,_cancel,callback) {
            if (method==='GetConnectionUnixUser') callback(this,[callerUid]);
            else {
                assert.equal(method,'GetCurrentState');
                if (deferred) waiting.push(() => callback(this,displayState));
                else callback(this,displayState);
            }
        },
        call_finish(result) {return result;},
    }},
    DBusCallFlags:{NONE:0}, BusNameOwnerFlags:{NONE:0},
    bus_own_name_on_connection() {return 1;}, bus_unown_name() {},
    File:{new_for_path(path) {
        assert.equal(path,'/etc/ala/config.json');
        return {query_info() {return {
            get_is_symlink:()=>false,get_file_type:()=>1,
            get_attribute_uint32:key=>key==='unix::mode'?configMode:0,
        };}};
    }},
    FileQueryInfoFlags:{NOFOLLOW_SYMLINKS:0},FileType:{REGULAR:1},
};
const GLib = {
    uuid_string_random:()=> '00000000-0000-0000-0000-000000000001',
    Variant:class {constructor(_type,value) {this.value=value;}deep_unpack() {return this.value;}},
    get_user_name:()=> 'user',
    file_get_contents(path) {
        assert.equal(path,'/etc/ala/config.json');
        return [true,new TextEncoder().encode(JSON.stringify(config))];
    },
    PRIORITY_DEFAULT:0,SOURCE_REMOVE:false,
    timeout_add(priority,duration,callback) {
        assert.equal(duration,10000);assert.equal(priority,0);
        const id=nextId++;timers.set(id,callback);return id;
    },
    Source:{remove(id) {assert.ok(timers.delete(id));}},
};
global.get_window_actors = () => {throw new Error('identify must not inspect or alter Chrome windows');};
function invoke(extension,method='IdentifyOutputs') {
    return new Promise(resolve => extension[method+'Async']([],{
        get_sender:()=> ':1.2',
        return_value:value=>resolve(JSON.parse(value.deep_unpack()[0])),
    }));
}
function expire() {
    for (const [id,callback] of [...timers]) {
        timers.delete(id);
        assert.equal(callback(),GLib.SOURCE_REMOVE);
    }
}
function assertClean() {
    assert.equal(chrome.size,0);assert.equal(timers.size,0);
    assert.equal(monitorSignals.size,0);assert.equal(sessionSignals.size,0);
}
"""


def run_extension(scenario, version='47.4'):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is required for the extension harness')
    source = re.sub(r'^import [^\n]+;\n', '', EXTENSION.read_text(encoding='utf-8'), flags=re.MULTILINE)
    source = source.replace('export default class AlaDisplay', 'class AlaDisplay', 1)
    metadata = EXTENSION.with_name('metadata.json').read_text(encoding='utf-8')
    setup = f'const metadata={metadata.strip()};\nconst Config={{PACKAGE_VERSION:{json.dumps(version)}}};\n'
    result = subprocess.run([node, '--input-type=module', '-'],
                            input=setup + HARNESS + source + '\n' + scenario,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('version', ['47.4', '49.4'])
def test_state_query_only_advertises_capability_without_showing_labels(version):
    run_extension("""
const extension=new AlaDisplay();extension.enable();
const result=await invoke(extension,'GetState');
assert.deepEqual(result.capabilities,['identify-outputs','workspace-placement','monitor-layout-trial']);
assert.equal(result.protocol,1);assertClean();
""", version)


@pytest.mark.parametrize('version', ['47.4', '49.4'])
@pytest.mark.parametrize('mirrored', [False, True])
def test_labels_use_logical_id_order_and_never_take_input_or_focus(version, mirrored):
    run_extension(f"""
if ({str(mirrored).lower()}) {{
    displayState[2]=[[0,0,1.5,0,true,displayState[1].map(m=>m[0]),{{}}]];
    Main.layoutManager.monitors=[{{x:0,y:0,width:1280,height:720}}];
}}
const extension=new AlaDisplay();extension.enable();
const result=await invoke(extension);
assert.equal(result.state,'ok');assert.equal(result.duration_seconds,10);
assert.equal(result.logical_outputs.length,{1 if mirrored else 2});
assert.deepEqual(result.logical_outputs.map(o=>o.display_number),{[1] if mirrored else [1, 2]});
assert.deepEqual(result.logical_outputs.map(o=>o.id),{json.dumps(['DP-1+HDMI-1'] if mirrored else ['DP-1', 'HDMI-1'])});
assert.equal(chrome.size,{1 if mirrored else 2});
for (const actor of chrome) {{
    assert.equal(actor.reactive,false);assert.equal(actor.can_focus,false);
    assert.equal(actor.clip_to_allocation,true);
    assert.equal(actor.children[0].reactive,false);assert.equal(actor.children[0].can_focus,false);
    assert.match(actor.children[0].text,/^[12]\\n(?:DP|HDMI)-1/);
}}
expire();assertClean();
""", version)


@pytest.mark.parametrize('version', ['47.4', '49.4'])
def test_repeat_replaces_labels_and_resets_one_fixed_timer(version):
    run_extension("""
const extension=new AlaDisplay();extension.enable();
assert.equal((await invoke(extension)).state,'ok');
const previous=[...chrome];
assert.equal((await invoke(extension)).state,'ok');
assert.ok(previous.every(actor=>actor.destroyed));
assert.equal(chrome.size,2);assert.equal(timers.size,1);
assert.equal(monitorSignals.size,1);assert.equal(sessionSignals.size,1);
expire();assertClean();
""", version)


@pytest.mark.parametrize('cause', ['hotplug', 'lock', 'disable'])
@pytest.mark.parametrize('version', ['47.4', '49.4'])
def test_context_end_cleans_actors_timer_and_signals(cause, version):
    run_extension(f"""
const extension=new AlaDisplay();extension.enable();
assert.equal((await invoke(extension)).state,'ok');
const previous=[...chrome];
if ('{cause}'==='hotplug') for (const callback of [...monitorSignals.values()]) callback();
if ('{cause}'==='lock') {{Main.sessionMode.isLocked=true;for (const callback of [...sessionSignals.values()]) callback();}}
if ('{cause}'==='disable') extension.disable();
assert.ok(previous.every(actor=>actor.destroyed));assertClean();
""", version)


@pytest.mark.parametrize('change', ['disable', 'lock', 'hotplug'])
@pytest.mark.parametrize('version', ['47.4', '49.4'])
def test_inflight_query_cannot_display_after_context_changed(change, version):
    run_extension(f"""
const extension=new AlaDisplay();extension.enable();deferred=true;
const request=invoke(extension);await Promise.resolve();
assert.equal(waiting.length,1);
if ('{change}'==='disable') extension.disable();
if ('{change}'==='lock') Main.sessionMode.isLocked=true;
if ('{change}'==='hotplug') Main.layoutManager.monitors=[...Main.layoutManager.monitors.map(m=>({{...m}}))];
waiting[0]();assert.notEqual((await request).state,'ok');assertClean();
""", version)


@pytest.mark.parametrize('change', [
    'callerUid=1001', "config.user='other'", 'configMode=0o666',
    'Main.sessionMode.isGreeter=true', 'Main.sessionMode.isLocked=true',
    "Main.sessionMode.currentMode='unlock-dialog'",
    'Main.layoutManager.monitors=[]',
    "displayState[2][0][5].push(displayState[2][0][5][0])",
])
def test_invalid_caller_session_or_topology_never_displays(change):
    run_extension(f"""
const extension=new AlaDisplay();extension.enable();{change};
assert.notEqual((await invoke(extension)).state,'ok');assertClean();
""")


def test_unsupported_shell_never_displays():
    run_extension("""
const extension=new AlaDisplay();extension.enable();
assert.notEqual((await invoke(extension)).state,'ok');assertClean();
""", '48.1')


def test_partial_actor_failure_cleans_every_created_overlay():
    run_extension("""
const extension=new AlaDisplay();extension.enable();failAdd=true;
assert.notEqual((await invoke(extension)).state,'ok');assertClean();
""")


def test_older_async_request_cannot_replace_newer_labels():
    run_extension("""
const extension=new AlaDisplay();extension.enable();deferred=true;
const first=invoke(extension);await Promise.resolve();
const second=invoke(extension);await Promise.resolve();
assert.equal(waiting.length,2);
waiting[1]();assert.equal((await second).state,'ok');const current=[...chrome];
waiting[0]();assert.notEqual((await first).state,'ok');
assert.ok(current.every(actor=>!actor.destroyed));assert.equal(timers.size,1);
expire();assertClean();
""")
