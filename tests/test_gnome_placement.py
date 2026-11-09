"""GNOME placement contract with delayed, acknowledged window geometry.

This models the asynchronous boundary in Mutter 47/49, not an actual GNOME
session or a reproduction of any particular school's monitor configuration.
"""
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


EXTENSION = Path(__file__).resolve().parents[1] / 'assets/gnome-extension/ala-display@example.com/extension.js'
HARNESS = r"""
import assert from 'node:assert/strict';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
class Extension { constructor() { this.metadata = metadata; } }
const Meta = {WindowType: {NORMAL: 0}, MaximizeFlags: {BOTH: 3}};
const config = {schema_version: 2, user: 'user', display: true,
    screens: [{id:'first'}, {id:'second'}]};
let callerUid = 1000;
let stateDelay = 0;
let serial = 1;
const signals = new Map();
let nextSignal = 0;
const timers = new Map();
let nextTimer = 0;
const Main = {
    sessionMode: {currentMode:'user', isGreeter:false, isLocked:false},
    layoutManager: {
        monitors: [{x:0,y:0,width:1920,height:1080}, {x:1920,y:-180,width:1280,height:720}],
        connect(name,callback) { signals.set(++nextSignal,{name,callback}); return nextSignal; },
        disconnect(id) { assert.ok(signals.delete(id)); },
    },
};
let logical = [[0,0,1,0,true,[['HDMI-1','v','p','s']],{}],
               [1920,-180,1.5,0,false,[['DP-1','v','p','s']],{}]];
const files = new Map();
function addProcess(pid, ppid, ticks, argv, exe='/opt/google/chrome/chrome') {
    const values = ['S',String(ppid),...Array(17).fill('0'),ticks];
    files.set(`/proc/${pid}/stat`, `${pid} (chrome) ${values.join(' ')}`);
    files.set(`/proc/${pid}/status`, 'Uid:\t1000\t1000\t1000\t1000\n');
    files.set(`/proc/${pid}/cmdline`, argv.join('\0') + '\0');
    files.set(`/proc/${pid}/exe`, exe);
}
function request(target='first') {
    const n = target === 'first' ? 0 : 100;
    return {target,layout:'fullscreen',output:'DP-1',rect:{...Main.layoutManager.monitors[1]},
        boot_id:'boot',supervisor_pid:100+n,browser_pid:101+n,
        supervisor_start_ticks:'10',browser_start_ticks:'20'};
}
for (const target of ['first','second']) {
    const req = request(target);
    addProcess(req.supervisor_pid, 1, '10', ['/usr/bin/python3','-I','/opt/ala/current/entry.py','_chrome-run','--target',target]);
    addProcess(req.browser_pid, req.supervisor_pid, '20', ['/opt/google/chrome/chrome',`--user-data-dir=/home/user/.config/ala/chrome-displays/${target}`]);
}
const Gio = {
    Credentials: class { get_unix_user() { return 1000; } },
    DBusExportedObject: {wrapJSObject() { return {export() {}, unexport() {}}; }},
    DBus: {session: {
        call(_bus,_path,_iface,method,_params,_type,_flags,_timeout,_cancel,callback) {
            if (method === 'GetConnectionUnixUser') callback(this,[callerUid]);
            else {
                assert.equal(method,'GetCurrentState');
                const result = [serial,[],structuredClone(logical),{}];
                setTimeout(() => callback(this,result),stateDelay);
            }
        },
        call_finish(result) { return result; },
    }},
    DBusCallFlags: {NONE:0}, BusNameOwnerFlags: {NONE:0},
    bus_own_name_on_connection() { return 1; }, bus_unown_name() {},
    File: {new_for_path() { return {query_info() { return {
        get_is_symlink:() => false, get_file_type:() => 1,
        get_attribute_uint32:key => key === 'unix::mode' ? 0o644 : 0,
    }; }}; }},
    FileType:{REGULAR:1}, FileQueryInfoFlags:{NOFOLLOW_SYMLINKS:0},
};
const GLib = {
    uuid_string_random:()=> '00000000-0000-0000-0000-000000000001',
    Variant: class { constructor(_type,value) { this.value=value; } deep_unpack() { return this.value; } },
    file_get_contents(path) {
        let value;
        if (path === '/etc/ala/config.json') value=JSON.stringify(config);
        else if (path === '/proc/sys/kernel/random/boot_id') value='boot';
        else { assert.ok(files.has(path),path); value=files.get(path); }
        return [true,new TextEncoder().encode(value)];
    },
    file_read_link(path) { assert.ok(files.has(path)); return files.get(path); },
    get_user_name:() => 'user', get_home_dir:() => '/home/user',
    get_monotonic_time:() => Number(process.hrtime.bigint()/1000n),
    PRIORITY_DEFAULT:0, SOURCE_REMOVE:false, SOURCE_CONTINUE:true,
    timeout_add(_priority,ms,callback) {
        const id=++nextTimer;
        const tick=() => {
            if (!timers.has(id)) return;
            if (callback()) timers.set(id,setTimeout(tick,ms));
            else timers.delete(id);
        };
        timers.set(id,setTimeout(tick,ms));
        return id;
    },
    Source:{remove(id) { clearTimeout(timers.get(id)); timers.delete(id); }},
};
const actors=[];
global.get_window_actors=() => actors.map(meta_window => ({meta_window}));
const workspaces=[{index:() => 0,activate() {throw new Error('must not activate workspace');}},
                  {index:() => 1,activate() {throw new Error('must not activate workspace');}}];
let activeWorkspace=workspaces[0];
global.workspace_manager={
    get_active_workspace:() => activeWorkspace,
    connect(name,callback) { signals.set(++nextSignal,{name,callback}); return nextSignal; },
    disconnect(id) { assert.ok(signals.delete(id)); },
};
global.display={
    connect(name,callback) { signals.set(++nextSignal,{name,callback}); return nextSignal; },
    disconnect(id) { assert.ok(signals.delete(id)); },
};
function emit(name,window) {
    for (const {name:signal,callback} of signals.values())
        if (name === signal) callback(global.display,window,1);
}
function monitorForRect(rect) {
    let best=0,area=0;
    Main.layoutManager.monitors.forEach((m,index) => {
        const overlap=Math.max(0,Math.min(rect.x+rect.width,m.x+m.width)-Math.max(rect.x,m.x)) *
            Math.max(0,Math.min(rect.y+rect.height,m.y+m.height)-Math.max(rect.y,m.y));
        if (overlap>area) {best=index; area=overlap;}
    });
    return best;
}
function fakeWindow(target='first',options={}) {
    const req=request(target);
    const window={
        id:target === 'first' ? 1 : 2, pid:req.browser_pid, monitor:0,
        rect:{x:50,y:50,width:900,height:600}, fullscreen:false,
        delay:80, calls:[], stalled:false, configuration:0,
        workspace:workspaces[0], workspaceDelay:0, workspaceStalled:false,
        get_window_type() { return 0; }, get_pid() { return this.pid; },
        get_stable_sequence() { return this.id; }, get_monitor() { return this.monitor; },
        get_frame_rect() { return {...this.rect}; }, is_fullscreen() { return this.fullscreen; },
        parent:null, attached:false, attachedChildren:false, children:[],
        get_client_type() { return 0; }, get_transient_for() { return this.parent; },
        is_attached_dialog() { return this.attached; }, has_attached_dialogs() { return this.attachedChildren; },
        foreach_transient(callback) { for (const child of this.children) if (!callback(child)) break; },
        get_workspace() { return this.workspace; },
        located_on_workspace(workspace) { return this.workspace === workspace; },
        change_workspace(workspace) {
            this.calls.push(['workspace',workspace.index()]);
            if (!this.workspaceStalled)
                setTimeout(() => {this.workspace=workspace;},this.workspaceDelay);
        },
        activate() { throw new Error('must not focus or activate managed window'); },
        focus() { throw new Error('must not focus or activate managed window'); },
        get_work_area_for_monitor(index) {
            const m=Main.layoutManager.monitors[index];
            return {...m,y:m.y+24,height:m.height-24};
        },
        unmake_fullscreen() { this.calls.push(['unfullscreen']); this.fullscreen=false; },
        unmaximize(...args) { this.calls.push(['unmaximize',...args]); },
        configure(rect) {
            const sequence=++this.configuration;
            setTimeout(() => {
                if (sequence !== this.configuration || this.stalled) return;
                this.rect={...rect};
                this.monitor=monitorForRect(rect);
            },this.delay);
        },
        move_to_monitor(index) {
            this.calls.push(['monitor',index]);
            const m=Main.layoutManager.monitors[index];
            this.configure({...this.rect,x:m.x,y:m.y+24});
        },
        move_resize_frame(user,x,y,width,height) {
            this.calls.push(['resize',user,x,y,width,height]);
            let rect={x,y,width,height};
            // Both tagged Mutter versions choose the fullscreen constraint's
            // logical monitor from the NEW rectangle, not get_monitor(). An
            // explicit X11 spanning hint overrides that monitor rectangle.
            if (this.fullscreen)
                rect=this.fullscreenBounds || Main.layoutManager.monitors[monitorForRect(rect)];
            this.configure(rect);
        },
        make_fullscreen() {
            this.calls.push(['fullscreen',this.monitor]);
            this.fullscreen=true;
            this.configure(Main.layoutManager.monitors[this.monitor]);
        },
        ...options,
    };
    actors.push(window);
    return window;
}
function invoke(extension,method,req) {
    return new Promise(resolve => extension[`${method}Async`]([JSON.stringify(req)],{
        get_sender:() => ':1.1',
        return_value:value => resolve(JSON.parse(value.deep_unpack()[0])),
    }));
}
"""


def run_extension(scenario, version='47.4'):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is required for the extension harness')
    source = re.sub(r'^import [^\n]+;\n', '', EXTENSION.read_text(encoding='utf-8'), flags=re.MULTILINE)
    source = source.replace('export default class AlaDisplay', 'class AlaDisplay', 1)
    setup = f'const metadata={EXTENSION.with_name("metadata.json").read_text()};\nconst Config={{PACKAGE_VERSION:{json.dumps(version)}}};\n'
    result = subprocess.run([node, '--input-type=module', '-'], input=setup+HARNESS+source+'\n'+scenario,
                            capture_output=True, text=True, timeout=8)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('version', ['47.4', '49.0'])
def test_waits_for_destination_before_fullscreen_and_does_not_touch_other_windows(version):
    run_extension("""
const extension=new AlaDisplay(); extension.enable();
const window=fakeWindow();
const other=fakeWindow('second');
const ordinary=fakeWindow('second',{pid:999});
const result=await invoke(extension,'Place',request());
await sleep(150);
assert.deepEqual(window.rect,request().rect);
assert.equal(window.monitor,1);
assert.equal(result.state,'ok');
assert.deepEqual(window.calls.filter(c => c[0]==='fullscreen'),[['fullscreen',1]]);
assert.deepEqual(other.calls,[]); assert.deepEqual(ordinary.calls,[]);
assert.equal((await invoke(extension,'Inspect',request())).windows.length,1);
extension.disable(); assert.equal(timers.size,0);
""", version)


@pytest.mark.parametrize('version', ['47.4', '49.0'])
def test_fullscreen_move_preserves_fullscreen_and_correct_placement_is_idempotent(version):
    run_extension("""
const extension=new AlaDisplay(); extension.enable();
const window=fakeWindow('first',{fullscreen:true,rect:{...Main.layoutManager.monitors[0]}});
assert.equal((await invoke(extension,'Place',request())).state,'ok');
assert.deepEqual(window.rect,request().rect);
assert.deepEqual(window.calls,[['resize',false,1920,-180,1280,720]]);
window.calls=[];
assert.equal((await invoke(extension,'Place',request())).state,'ok');
assert.deepEqual(window.calls,[]);
extension.disable();
""", version)


@pytest.mark.parametrize('version', ['47.4', '49.0'])
def test_spanning_fullscreen_constraint_is_never_reported_as_one_output_success(version):
    run_extension("""
const extension=new AlaDisplay(); extension.enable();
const window=fakeWindow('first',{fullscreen:true,rect:{...Main.layoutManager.monitors[0]},
    fullscreenBounds:{x:0,y:-180,width:3200,height:1260}});
const other=fakeWindow('second');
assert.equal((await invoke(extension,'Place',request())).state,'pending');
assert.deepEqual(window.rect,window.fullscreenBounds);
assert.deepEqual(window.calls,[['resize',false,1920,-180,1280,720]]);
assert.deepEqual(other.calls,[]);
extension.disable(); assert.equal(timers.size,0);
""", version)


@pytest.mark.parametrize('layout', ['left', 'right'])
@pytest.mark.parametrize('version', ['47.4', '49.0'])
def test_negative_origin_and_odd_split_width_use_stage_coordinates_once(layout, version):
    run_extension(f"""
const extension=new AlaDisplay(); extension.enable();
Object.assign(Main.layoutManager.monitors[1],{{x:-1537,y:-80,width:1537,height:864}});
logical[1][0]=-1537; logical[1][1]=-80; logical[1][2]=1.25;
const window=fakeWindow('first',{{fullscreen:true}}),req=request();
req.layout='{layout}';
assert.equal((await invoke(extension,'Place',req)).state,'ok');
assert.deepEqual(window.rect,{{x:{'-1537' if layout == 'left' else '-769'},y:-80,width:{768 if layout == 'left' else 769},height:864}});
assert.equal(window.fullscreen,false);
assert.equal(window.calls.filter(c => c[0]==='fullscreen').length,0);
assert.deepEqual(window.calls.find(c => c[0]==='unmaximize').slice(1),{'[3]' if version.startswith('47') else '[]'});
extension.disable();
""", version)


@pytest.mark.parametrize('selector', ['DP-1', 'DP-1+HDMI-2'])
def test_mirrored_connectors_resolve_to_one_logical_output(selector):
    run_extension(f"""
const extension=new AlaDisplay(); extension.enable();
logical[1][5].push(['HDMI-2','v','p','s']);
const window=fakeWindow(),req=request(); req.output='{selector}';
assert.equal((await invoke(extension,'Place',req)).state,'ok');
assert.equal(window.monitor,1);
assert.deepEqual(window.rect,req.rect);
extension.disable();
""")


@pytest.mark.parametrize('change', [
    'Main.layoutManager.monitors[1].width=1270',
    'Main.layoutManager.monitors.reverse()',
    'logical[1][5]=[["HDMI-2","v","p","s"]]; emit("monitors-changed",window)',
    'config.screens.push({id:"third"})',
    'Main.sessionMode.isLocked=true',
    'activeWorkspace=workspaces[1]',
    'files.set("/proc/101/stat",files.get("/proc/101/stat").replace(/20$/,"21"))',
    'actors.length=0',
    'emit("grab-op-begin",window)',
    'extension.disable()',
])
def test_changes_while_reading_display_state_do_not_mutate_windows(change):
    run_extension(f"""
const extension=new AlaDisplay(); extension.enable(); stateDelay=60;
const window=fakeWindow(),req=request();
const pending=invoke(extension,'Place',req);
await sleep(20);
{change};
const result=await pending;
assert.notEqual(result.state,'ok'); assert.deepEqual(window.calls,[]);
extension.disable(); assert.equal(timers.size,0);
""")


def test_duplicate_requests_do_not_race_and_inspection_remains_read_only():
    run_extension("""
const extension=new AlaDisplay(); extension.enable(); stateDelay=80;
const window=fakeWindow(),req=request();
const first=invoke(extension,'Place',req);
await sleep(20);
const observed=await invoke(extension,'Inspect',req);
assert.equal(observed.state,'ok'); assert.deepEqual(window.calls,[]);
assert.equal((await invoke(extension,'Place',req)).state,'pending');
assert.equal((await first).state,'ok');
assert.equal(window.calls.filter(c => c[0]==='resize').length,1);
assert.equal(window.calls.filter(c => c[0]==='fullscreen').length,1);
extension.disable(); assert.equal(timers.size,0);
""")


@pytest.mark.parametrize('change', [
    'Main.layoutManager.monitors[1].height=710',
    'emit("monitors-changed",window)',
    'config.screens[0].output="HDMI-1"',
    'Main.sessionMode.isLocked=true',
    'emit("grab-op-begin",window)',
    'actors.push(fakeWindow("first",{id:3}))',
    'extension.disable()',
])
def test_invalidated_pending_resize_never_enters_fullscreen(change):
    run_extension(f"""
const extension=new AlaDisplay(); extension.enable();
const window=fakeWindow('first',{{delay:200}}),req=request();
const pending=invoke(extension,'Place',req);
await sleep(30);
assert.equal(window.calls.filter(c => c[0]==='resize').length,1);
{change};
assert.notEqual((await pending).state,'ok');
await sleep(220);
assert.equal(window.calls.filter(c => c[0]==='fullscreen').length,0);
extension.disable(); assert.equal(timers.size,0);
""")


def test_interactive_drag_is_not_interrupted_and_other_target_remains_independent():
    run_extension("""
const extension=new AlaDisplay(); extension.enable();
const window=fakeWindow(),other=fakeWindow('second');
emit('grab-op-begin',window);
assert.equal((await invoke(extension,'Place',request())).state,'pending');
assert.deepEqual(window.calls,[]);
assert.equal((await invoke(extension,'Place',request('second'))).state,'ok');
assert.deepEqual(window.calls,[]);
emit('grab-op-end',window);
assert.equal((await invoke(extension,'Place',request())).state,'ok');
assert.equal(window.monitor,1);
extension.disable(); assert.equal(signals.size,0);
""")


def test_unacknowledged_resize_is_bounded_pending_and_releases_operation():
    run_extension("""
const extension=new AlaDisplay(); extension.enable();
const window=fakeWindow('first',{stalled:true}),req=request(),start=Date.now();
assert.equal((await invoke(extension,'Place',req)).state,'pending');
assert.ok(Date.now()-start<2500);
assert.equal(window.calls.filter(c => c[0]==='fullscreen').length,0);
assert.equal(timers.size,0); assert.equal(extension._placements.size,0);
window.stalled=false;
assert.equal((await invoke(extension,'Place',req)).state,'ok');
extension.disable();
""")


def test_slow_topology_reply_cannot_start_mutations_after_the_phase_deadline():
    run_extension("""
const extension=new AlaDisplay(); extension.enable(); stateDelay=1950;
const window=fakeWindow(),start=Date.now();
assert.equal((await invoke(extension,'Place',request())).state,'pending');
assert.ok(Date.now()-start<2500); assert.deepEqual(window.calls,[]);
extension.disable(); assert.equal(timers.size,0);
""")


@pytest.mark.parametrize('change', [
    'callerUid=1001',
    'req.browser_start_ticks="30"',
    'req.output="HDMI-2"',
    'fakeWindow("first",{id:3})',
])
def test_unconfirmed_window_or_output_never_changes_any_window(change):
    run_extension(f"""
const extension=new AlaDisplay(); extension.enable();
const window=fakeWindow(),other=fakeWindow('second'),req=request();
{change};
assert.notEqual((await invoke(extension,'Place',req)).state,'ok');
assert.deepEqual(window.calls,[]); assert.deepEqual(other.calls,[]);
extension.disable();
""")


@pytest.mark.parametrize('relationship', [
    'window.parent=other',
    'window.attached=true',
    'window.attachedChildren=true',
    'window.children=[other]',
])
@pytest.mark.parametrize('version', ['47.4', '49.0'])
def test_related_normal_window_is_blocked_without_moving_either_window(relationship, version):
    run_extension(f"""
const extension=new AlaDisplay(); extension.enable();
const window=fakeWindow(),other=fakeWindow('second'),req=request();
{relationship};
const inspected=await invoke(extension,'Inspect',req);
assert.equal(inspected.state,'ok');
assert.equal(inspected.windows[0].client_type,0);
assert.equal(inspected.windows[0].transient_for,window.parent ? other.id : null);
const result=await invoke(extension,'Place',req);
assert.equal(result.state,'blocked');
assert.equal(result.reason,'다른 창과 연결된 관리 창은 함께 이동할 수 있어 배치를 보류했습니다.');
assert.deepEqual(window.calls,[]); assert.deepEqual(other.calls,[]);
extension.disable();
""", version)


@pytest.mark.parametrize('phase', ['query', 'resize'])
def test_new_window_relationship_cancels_later_phases(phase):
    run_extension(f"""
const extension=new AlaDisplay(); extension.enable();
const window=fakeWindow('first',{{delay:200}}),other=fakeWindow('second'),req=request();
stateDelay={'100' if phase == 'query' else '0'};
const pending=invoke(extension,'Place',req);
await sleep(30);
window.children=[other];
assert.equal((await pending).state,'blocked');
assert.equal(window.calls.filter(c => c[0]==='fullscreen').length,0);
{'assert.deepEqual(window.calls,[]);' if phase == 'query' else ''}
assert.deepEqual(other.calls,[]);
extension.disable(); assert.equal(timers.size,0);
""")


@pytest.mark.parametrize('phase', ['query', 'resize'])
def test_unreadable_window_relationship_is_unknown_without_further_mutation(phase):
    run_extension(f"""
const extension=new AlaDisplay(); extension.enable();
const window=fakeWindow('first',{{delay:200}}),req=request();
stateDelay={'100' if phase == 'query' else '0'};
const pending=invoke(extension,'Place',req);
await sleep(30);
window.get_transient_for=() => {{throw new Error('unmanaged');}};
assert.equal((await pending).state,'unknown');
assert.equal(window.calls.filter(c => c[0]==='fullscreen').length,0);
{'assert.deepEqual(window.calls,[]);' if phase == 'query' else ''}
extension.disable(); assert.equal(timers.size,0);
""")


@pytest.mark.parametrize('version', ['47.4', '49.0'])
def test_hidden_workspace_window_moves_only_itself_without_geometry_or_global_switch(version):
    run_extension("""
const extension=new AlaDisplay(); extension.enable();
const window=fakeWindow('first',{workspace:workspaces[1],monitor:1,
    fullscreen:true,rect:{...Main.layoutManager.monitors[1]}});
const other=fakeWindow('second',{workspace:workspaces[1]});
const before=await invoke(extension,'Inspect',request());
assert.equal(before.windows[0].workspace_index,1);
assert.equal(before.windows[0].on_active_workspace,false);
assert.equal((await invoke(extension,'Place',request())).state,'ok');
assert.deepEqual(window.calls,[['workspace',0]]);
assert.equal(window.workspace,workspaces[0]); assert.equal(activeWorkspace,workspaces[0]);
assert.equal(other.workspace,workspaces[1]); assert.deepEqual(other.calls,[]);
const after=await invoke(extension,'Inspect',request());
assert.equal(after.windows[0].workspace_index,0);
assert.equal(after.windows[0].on_active_workspace,true);
extension.disable();
""", version)


def test_workspace_change_is_observed_before_any_geometry_control():
    run_extension("""
const extension=new AlaDisplay(); extension.enable();
const window=fakeWindow('first',{workspace:workspaces[1],workspaceDelay:120});
const pending=invoke(extension,'Place',request());
await sleep(40); assert.deepEqual(window.calls,[['workspace',0]]);
assert.equal(window.workspace,workspaces[1]);
assert.equal((await pending).state,'ok');
assert.equal(window.workspace,workspaces[0]); assert.equal(window.monitor,1);
assert.equal(activeWorkspace,workspaces[0]);
extension.disable(); assert.equal(timers.size,0);
""")


def test_active_workspace_switch_cancels_waiting_workspace_change_without_later_control():
    run_extension("""
const extension=new AlaDisplay(); extension.enable();
const window=fakeWindow('first',{workspace:workspaces[1],workspaceDelay:200});
const other=fakeWindow('second');
const pending=invoke(extension,'Place',request());
await sleep(30); assert.deepEqual(window.calls,[['workspace',0]]);
activeWorkspace=workspaces[1];
assert.equal((await pending).state,'pending');
await sleep(220);
assert.deepEqual(window.calls,[['workspace',0]]);
assert.deepEqual(other.calls,[]); assert.equal(activeWorkspace,workspaces[1]);
extension.disable(); assert.equal(timers.size,0);
""")


def test_even_a_workspace_switch_back_to_the_original_cancels_the_snapshot():
    run_extension("""
const extension=new AlaDisplay(); extension.enable(); stateDelay=100;
const window=fakeWindow();
const pending=invoke(extension,'Place',request());
await sleep(20);
activeWorkspace=workspaces[1]; emit('active-workspace-changed',window);
activeWorkspace=workspaces[0]; emit('active-workspace-changed',window);
assert.equal((await pending).state,'pending'); assert.deepEqual(window.calls,[]);
extension.disable(); assert.equal(signals.size,0);
""")


@pytest.mark.parametrize('change', [
    'activeWorkspace=workspaces[1]',
    'window.workspace=workspaces[1]',
])
def test_workspace_change_during_geometry_wait_does_not_enter_fullscreen(change):
    run_extension(f"""
const extension=new AlaDisplay(); extension.enable();
const window=fakeWindow('first',{{delay:200}});
const pending=invoke(extension,'Place',request());
await sleep(30); assert.equal(window.calls.filter(c => c[0]==='resize').length,1);
{change};
assert.equal((await pending).state,'pending');
assert.equal(window.calls.filter(c => c[0]==='fullscreen').length,0);
assert.equal(window.calls.filter(c => c[0]==='workspace').length,0);
extension.disable(); assert.equal(timers.size,0);
""")


def test_related_hidden_workspace_window_is_blocked_before_workspace_mutation():
    run_extension("""
const extension=new AlaDisplay(); extension.enable();
const window=fakeWindow('first',{workspace:workspaces[1]}),other=fakeWindow('second');
window.children=[other];
assert.equal((await invoke(extension,'Place',request())).state,'blocked');
assert.deepEqual(window.calls,[]); assert.deepEqual(other.calls,[]);
assert.equal(window.workspace,workspaces[1]); assert.equal(activeWorkspace,workspaces[0]);
extension.disable();
""")


def test_unconfirmed_workspace_change_is_pending_and_bounded():
    run_extension("""
const extension=new AlaDisplay(); extension.enable();
const window=fakeWindow('first',{workspace:workspaces[1],workspaceStalled:true});
const start=process.hrtime.bigint();
assert.equal((await invoke(extension,'Place',request())).state,'pending');
assert.ok(Number(process.hrtime.bigint()-start)/1e6<2500);
assert.deepEqual(window.calls,[['workspace',0]]);
assert.equal(window.workspace,workspaces[1]); assert.equal(activeWorkspace,workspaces[0]);
extension.disable(); assert.equal(timers.size,0);
""")


@pytest.mark.parametrize('offset', [-86400000, 86400000])
def test_wallclock_jump_does_not_abort_healthy_placement(offset):
    run_extension(f"""
const extension=new AlaDisplay(); extension.enable();
const window=fakeWindow(),wallClock=Date.now;
setTimeout(() => {{Date.now=() => wallClock()+({offset});}},25);
assert.equal((await invoke(extension,'Place',request())).state,'ok');
assert.deepEqual(window.rect,request().rect);
extension.disable(); assert.equal(timers.size,0);
""")


def test_backward_wallclock_jump_cannot_extend_unacknowledged_placement():
    run_extension("""
const extension=new AlaDisplay(); extension.enable();
const window=fakeWindow('first',{stalled:true}),wallClock=Date.now,start=process.hrtime.bigint();
setTimeout(() => {Date.now=() => wallClock()-86400000;},25);
assert.equal((await invoke(extension,'Place',request())).state,'pending');
assert.ok(Number(process.hrtime.bigint()-start)/1e6<2500);
assert.equal(window.calls.filter(c => c[0]==='fullscreen').length,0);
extension.disable(); assert.equal(timers.size,0);
""")
