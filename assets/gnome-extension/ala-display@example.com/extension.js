/* Protocol 1. No titles, arbitrary commands, display reconfiguration or URL data. */
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Clutter from 'gi://Clutter';
import St from 'gi://St';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as Config from 'resource:///org/gnome/shell/misc/config.js';

const SHELL_MAJOR = Config.PACKAGE_VERSION.split('.')[0];
const IFACE = `<node><interface name="org.ala.Display1">
<method name="GetState"><arg type="s" direction="out"/></method>
<method name="IdentifyOutputs"><arg type="s" direction="out"/></method>
<method name="DismissStartupOverview"><arg type="s" direction="out"/></method>
<method name="Inspect"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
<method name="Place"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
<method name="Closed"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
</interface></node>`;
const decoder = new TextDecoder('utf-8', {fatal: true});
function read(path, limit = 1048576) {
    const [ok, data] = GLib.file_get_contents(path);
    if (!ok || data.length > limit) throw new Error('unreadable');
    return decoder.decode(data);
}
function unpack(value) {
    if (value instanceof GLib.Variant) return unpack(value.deep_unpack());
    if (Array.isArray(value)) return value.map(unpack);
    if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k,v]) => [k,unpack(v)]));
    return value;
}
function call(connection, bus, path, iface, method, parameters) {
    return new Promise((resolve,reject) => connection.call(bus,path,iface,method,parameters,null,
        Gio.DBusCallFlags.NONE,4000,null,(source,result) => {
            try { resolve(unpack(source.call_finish(result))); } catch (e) { reject(e); }
        }));
}
function processInfo(pid) {
    if (!Number.isSafeInteger(pid) || pid < 1) throw new Error('process');
    const text = read(`/proc/${pid}/stat`,8192);
    const values = text.substring(text.lastIndexOf(')')+2).trim().split(/\s+/);
    const uid = read(`/proc/${pid}/status`,65536).match(/^Uid:\s+(\d+)/m);
    if (!uid) throw new Error('uid');
    return {uid:Number(uid[1]),ppid:Number(values[1]),ticks:values[19]};
}
function protectedConfig() {
    const file = Gio.File.new_for_path('/etc/ala/config.json');
    const info = file.query_info('standard::type,standard::is-symlink,unix::uid,unix::mode',Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS,null);
    if (info.get_is_symlink() || info.get_file_type() !== Gio.FileType.REGULAR ||
        info.get_attribute_uint32('unix::uid') !== 0 || (info.get_attribute_uint32('unix::mode') & 0o022)) throw new Error('config');
    return JSON.parse(read('/etc/ala/config.json'));
}
function targetProfile(id) {
    return GLib.get_home_dir() + (id === 'default' ? '/.config/ala/chrome-display' : `/.config/ala/chrome-displays/${id}`);
}
function validate(request, uid) {
    if (!request || !/^[a-z][a-z0-9_-]{0,31}$/.test(request.target) || !['fullscreen','left','right'].includes(request.layout)) throw new Error('request');
    const config = protectedConfig();
    if (config.user !== GLib.get_user_name() || !config.display) throw new Error('account');
    const targets = config.schema_version === 1 ? [{id:'default'}] : config.screens;
    if (!Array.isArray(targets) || !targets.some(t => t.id === request.target)) throw new Error('target');
    if (read('/proc/sys/kernel/random/boot_id',128).trim() !== request.boot_id) throw new Error('boot');
    const supervisor = processInfo(request.supervisor_pid), browser = processInfo(request.browser_pid);
    if (supervisor.uid !== uid || browser.uid !== uid || browser.ppid !== request.supervisor_pid ||
        supervisor.ticks !== request.supervisor_start_ticks || browser.ticks !== request.browser_start_ticks) throw new Error('identity');
    const args = read(`/proc/${request.supervisor_pid}/cmdline`,65536).split('\0').filter(Boolean);
    const suffix = request.target === 'default' ? ['_chrome-run'] : ['_chrome-run','--target',request.target];
    const expectedSuffixes = request.target === 'default' ? [suffix,['_chrome-run','--target','default']] : [suffix];
    if (args[1] !== '-I' || !/^\/opt\/ala\/(?:current|releases\/[^/]+)\/entry\.py$/.test(args[2] || '') ||
        !expectedSuffixes.some(s => JSON.stringify(args.slice(3)) === JSON.stringify(s))) throw new Error('supervisor');
    let exe = GLib.file_read_link(`/proc/${request.browser_pid}/exe`).replace(/ \(deleted\)$/,'');
    if (exe !== '/opt/google/chrome/chrome') throw new Error('executable');
    const raw = read(`/proc/${request.browser_pid}/cmdline`,1048576);
    const profile = targetProfile(request.target), token = `--user-data-dir=${profile}`;
    const parts = raw.split('\0').filter(Boolean);
    const exact = parts.filter(p => p === token).length === 1;
    // Chromium may flatten argv without quoting. Only a whitespace-free exact
    // profile boundary can establish ownership; ambiguous paths are rejected.
    const flat = parts.length === 1 && !/[\s'"\\]/.test(profile) &&
        parts[0].startsWith(exe+' ') && parts[0].split(' ').filter(p => p === token).length === 1;
    if (!exact && !flat) throw new Error('profile');
    return browser;
}
function descendant(pid,browser,uid,browserTicks) {
    for (let depth=0;depth<16;depth++) {
        const value = processInfo(pid);
        if (value.uid !== uid || BigInt(value.ticks) < BigInt(browserTicks)) return false;
        if (pid === browser) return true;
        if (value.ppid <= 1 || value.ppid === pid) return false;
        pid = value.ppid;
    }
    return false;
}
function windows(request,uid,browserTicks) {
    return global.get_window_actors().map(a => a.meta_window).filter(w => {
        try { return w.get_window_type() === Meta.WindowType.NORMAL && descendant(w.get_pid(),request.browser_pid,uid,browserTicks); }
        catch (_) { return false; }
    });
}
function windowData(window) {
    const rect = window.get_frame_rect();
    return {id:window.get_stable_sequence(),monitor:window.get_monitor(),
        rect:{x:rect.x,y:rect.y,width:rect.width,height:rect.height},fullscreen:window.is_fullscreen()};
}
function unmaximize(window) {
    // Mutter 49 unmaximizes both directions without a flags argument.
    if (SHELL_MAJOR === '47')
        window.unmaximize(Meta.MaximizeFlags.BOTH);
    else
        window.unmaximize();
}
export default class AlaDisplay extends Extension {
    enable() {
        this._uid = new Gio.Credentials().get_unix_user();
        this._object = Gio.DBusExportedObject.wrapJSObject(IFACE,this);
        this._object.export(Gio.DBus.session,'/org/ala/Display1');
        this._owner = Gio.bus_own_name_on_connection(Gio.DBus.session,'org.ala.Display1',Gio.BusNameOwnerFlags.NONE,null,null);
    }
    disable() {
        this._clearOutputLabels();
        this._disconnectOverviewStartup();
        if (this._owner) Gio.bus_unown_name(this._owner);
        this._owner = null;
        this._object?.unexport(); this._object = null;
    }
    async _handle(invocation,fn,failureReason='관리 창의 사용자·프로세스·출력 증거를 확인하지 못했습니다.') {
        try {
            const [uid] = await call(Gio.DBus.session,'org.freedesktop.DBus','/org/freedesktop/DBus','org.freedesktop.DBus','GetConnectionUnixUser',new GLib.Variant('(s)',[invocation.get_sender()]));
            if (uid !== this._uid || !this.metadata['shell-version'].includes(SHELL_MAJOR))
                throw new Error('unsupported');
            const result = await fn(uid);
            invocation.return_value(new GLib.Variant('(s)',[JSON.stringify({protocol:1,...result})]));
        } catch (_) {
            invocation.return_value(new GLib.Variant('(s)',[JSON.stringify({protocol:1,state:'unknown',reason:failureReason})]));
        }
    }
    GetStateAsync(_args,invocation) {
        this._handle(invocation,async () => ({state:'ok',gnome_version:Config.PACKAGE_VERSION,
            capabilities:['identify-outputs'],
            display_config:await call(Gio.DBus.session,'org.gnome.Mutter.DisplayConfig','/org/gnome/Mutter/DisplayConfig','org.gnome.Mutter.DisplayConfig','GetCurrentState',null),
            compositor_monitors:Main.layoutManager.monitors.map(m => ({x:m.x,y:m.y,width:m.width,height:m.height}))}));
    }
    _clearOutputLabels() {
        if (this._identifyTimer) GLib.Source.remove(this._identifyTimer);
        this._identifyTimer = 0;
        if (this._identifyMonitorSignal) Main.layoutManager.disconnect(this._identifyMonitorSignal);
        this._identifyMonitorSignal = 0;
        if (this._identifySessionSignal) Main.sessionMode.disconnect(this._identifySessionSignal);
        this._identifySessionSignal = 0;
        for (const actor of this._outputLabels || []) {
            // LayoutManager untracks chrome actors on their destroy signal.
            actor.destroy();
        }
        this._outputLabels = [];
    }
    _identifySessionReady() {
        if (Main.sessionMode.isGreeter || Main.sessionMode.isLocked || Main.sessionMode.currentMode !== 'user')
            return false;
        const config = protectedConfig();
        return config.user === GLib.get_user_name();
    }
    IdentifyOutputsAsync(_args,invocation) {
        const object = this._object;
        const request = this._identifyRequest = (this._identifyRequest || 0) + 1;
        this._handle(invocation, async () => {
            if (!object || object !== this._object) throw new Error('extension inactive');
            if (!this._identifySessionReady())
                return {state:'blocked',reason:'지정한 화면 계정의 잠금 해제된 GNOME 사용자 세션에서 식별하세요.'};
            const monitors = [...Main.layoutManager.monitors];
            const rectangles = monitors.map(m => ({x:m.x,y:m.y,width:m.width,height:m.height}));
            const state = await call(Gio.DBus.session,'org.gnome.Mutter.DisplayConfig','/org/gnome/Mutter/DisplayConfig','org.gnome.Mutter.DisplayConfig','GetCurrentState',null);
            if (object !== this._object || request !== this._identifyRequest || !this._identifySessionReady())
                return {state:'pending',reason:'화면 세션 또는 식별 요청이 바뀌어 표시하지 않았습니다.'};
            if (monitors.length !== Main.layoutManager.monitors.length ||
                monitors.some((m,i) => m !== Main.layoutManager.monitors[i] ||
                    ['x','y','width','height'].some(k => m[k] !== rectangles[i][k])))
                return {state:'pending',reason:'출력 연결이 변경되었습니다. 다시 조회하고 식별하세요.'};
            if (!Array.isArray(state) || state.length !== 4 || !Number.isSafeInteger(state[0]) ||
                !Array.isArray(state[1]) || state[1].length > 64 || !Array.isArray(state[2]) || state[2].length > 64)
                throw new Error('display state');
            if (!state[2].length) {
                this._clearOutputLabels();
                return {state:'pending',reason:'식별할 활성 GNOME 출력이 없습니다.'};
            }
            const physical = new Set(state[1].map(m => m[0][0]));
            const used = new Set();
            const outputs = state[2].map(group => {
                const [x,y] = group;
                const connectors = group[5].map(spec => spec[0]).sort();
                if (!connectors.length || new Set(connectors).size !== connectors.length || connectors.some(c => typeof c !== 'string' ||
                    !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(c) || !physical.has(c) || used.has(c)))
                    throw new Error('connector');
                connectors.forEach(c => used.add(c));
                const matches = rectangles.filter(m => m.x === x && m.y === y);
                if (matches.length !== 1 || ['x','y','width','height'].some(k =>
                    !Number.isSafeInteger(matches[0][k]) || Math.abs(matches[0][k]) > 1000000) ||
                    matches[0].width <= 0 || matches[0].height <= 0)
                    throw new Error('unconfirmed output');
                return {id:connectors.join('+'),connectors,...matches[0],confirmed:true,mirrored:connectors.length > 1};
            }).sort((a,b) => a.id < b.id ? -1 : a.id > b.id ? 1 : 0)
                .map((output,index) => ({...output,display_number:index + 1}));
            if (outputs.length !== rectangles.length || new Set(outputs.map(o => `${o.x},${o.y}`)).size !== outputs.length)
                throw new Error('ambiguous output');
            this._clearOutputLabels();
            try {
                for (const output of outputs) {
                    const actor = new St.Widget({x:output.x,y:output.y,width:output.width,height:output.height,
                        reactive:false,can_focus:false,clip_to_allocation:true,layout_manager:new Clutter.BinLayout()});
                    this._outputLabels.push(actor);
                    actor.add_child(new St.Label({text:`${output.display_number}\n${output.connectors.join(' + ')}`,
                        reactive:false,can_focus:false,x_align:Clutter.ActorAlign.CENTER,y_align:Clutter.ActorAlign.CENTER,
                        style:'font-size: 48px; font-weight: bold; color: #e9f0f7; background-color: #2a2634; padding: 20px 32px; border-radius: 16px;'}));
                    Main.layoutManager.addTopChrome(actor,{trackFullscreen:false,affectsStruts:false,affectsInputRegion:false});
                }
                this._identifyMonitorSignal = Main.layoutManager.connect('monitors-changed', () => this._clearOutputLabels());
                this._identifySessionSignal = Main.sessionMode.connect('updated', () => {
                    try {
                        if (!this._identifySessionReady()) this._clearOutputLabels();
                    } catch (_) { this._clearOutputLabels(); }
                });
                this._identifyTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT,10000,() => {
                    this._identifyTimer = 0;
                    this._clearOutputLabels();
                    return GLib.SOURCE_REMOVE;
                });
            } catch (error) {
                this._clearOutputLabels();
                throw error;
            }
            return {state:'ok',serial:state[0],duration_seconds:10,logical_outputs:outputs,
                reason:'모니터 식별 표시를 요청했습니다. 실제 패널에서 번호를 확인하세요.'};
        }, '모니터 식별을 위한 화면 계정·세션·출력을 확인하지 못했습니다.');
    }
    _disconnectOverviewStartup() {
        if (this._overviewStartupSignal) {
            Main.layoutManager.disconnect(this._overviewStartupSignal);
            this._overviewStartupSignal = 0;
        }
    }
    _hideStartupOverview() {
        if (Main.sessionMode.isGreeter || Main.sessionMode.isLocked)
            return 'kept';
        if (!Main.overview.visibleTarget)
            return 'already_hidden';
        Main.overview.hide();
        return 'dismissed';
    }
    DismissStartupOverviewAsync(_args,invocation) {
        const object = this._object;
        this._handle(invocation, () => {
            if (!object || object !== this._object) throw new Error('extension inactive');
            const config = protectedConfig();
            if (config.user !== GLib.get_user_name() || config.display !== true ||
                Main.sessionMode.isGreeter || Main.sessionMode.isLocked)
                throw new Error('overview session');
            if (this._startupOverviewHandled)
                return {state:'ok',overview:'kept'};
            this._startupOverviewHandled = true;
            // GNOME Shell can open the overview during the startup animation.
            // Let it finish first so it cannot undo this one-shot dismissal.
            if (Main.layoutManager._startingUp) {
                this._overviewStartupSignal = Main.layoutManager.connect('startup-complete', () => {
                    this._disconnectOverviewStartup();
                    this._hideStartupOverview();
                });
                return {state:'ok',overview:'scheduled'};
            }
            return {state:'ok',overview:this._hideStartupOverview()};
        });
    }
    InspectAsync([text],invocation) {
        this._handle(invocation,async uid => {
            if (text.length>8192) throw new Error('request');
            const request=JSON.parse(text), browser=validate(request,uid);
            return {state:'ok',windows:windows(request,uid,browser.ticks).map(windowData)};
        });
    }
    ClosedAsync([text],invocation) {
        this._handle(invocation,async () => {
            if (text.length>8192) throw new Error('request');
            const request=JSON.parse(text);
            if (!/^[a-z][a-z0-9_-]{0,31}$/.test(request.target) || !Array.isArray(request.windows) || request.windows.length>32 ||
                request.windows.some(id => !Number.isSafeInteger(id) || id<1)) throw new Error('request');
            const config=protectedConfig(),targets=config.schema_version === 1 ? [{id:'default'}] : config.screens;
            if (config.user !== GLib.get_user_name() || !Array.isArray(targets) || !targets.some(t => t.id === request.target)) throw new Error('target');
            const ids=new Set(global.get_window_actors().map(a => a.meta_window.get_stable_sequence()));
            return {state:'ok',remaining_window_count:request.windows.filter(id => ids.has(id)).length};
        });
    }
    PlaceAsync([text],invocation) {
        this._handle(invocation,async uid => {
            if (text.length>8192) throw new Error('request');
            const request=JSON.parse(text), browser=validate(request,uid), found=windows(request,uid,browser.ticks);
            if (found.length !== 1) return {state:found.length ? 'blocked' : 'pending',reason:'관리 창 하나의 식별을 기다립니다.'};
            const rect=request.rect;
            const matches=Main.layoutManager.monitors.filter(m => ['x','y','width','height'].every(k => Number.isSafeInteger(rect?.[k]) && rect[k] === m[k]));
            if (matches.length !== 1) return {state:'pending',reason:'출력 연결이 변경되어 배치를 보류했습니다.'};
            const monitor=matches[0],windowId=found[0].get_stable_sequence(),index=Main.layoutManager.monitors.indexOf(monitor);
            // An output selector must still name the same logical group, not
            // merely a stale numeric monitor index at the requested coordinates.
            const state=await call(Gio.DBus.session,'org.gnome.Mutter.DisplayConfig','/org/gnome/Mutter/DisplayConfig','org.gnome.Mutter.DisplayConfig','GetCurrentState',null);
            const group=state[2].filter(l => l[0] === rect.x && l[1] === rect.y);
            if (group.length !== 1) throw new Error('output');
            const connectors=group[0][5].map(s => s[0]).sort();
            if (!connectors.includes(request.output) && connectors.join('+') !== request.output) throw new Error('output');
            const current=validate(request,uid),rechecked=windows(request,uid,current.ticks);
            if (rechecked.length !== 1 || rechecked[0].get_stable_sequence() !== windowId || Main.layoutManager.monitors[index] !== monitor) throw new Error('changed');
            const window=rechecked[0];
            window.unmake_fullscreen(); unmaximize(window); window.move_to_monitor(index);
            let x=rect.x,width=rect.width;
            if (request.layout === 'left') width=Math.floor(width/2);
            if (request.layout === 'right') {x+=Math.floor(width/2);width-=Math.floor(width/2);}
            window.move_resize_frame(true,x,rect.y,width,rect.height);
            if (request.layout === 'fullscreen') window.make_fullscreen();
            return {state:'pending',reason:'배치 요청 후 실제 창을 재확인하세요.'};
        });
    }
}
