"""Execute the actual bridge against synthetic Shell and Mutter calls."""
import json

import pytest

from test_gnome_identify import run_extension
from test_monitor_layout import current_state


def scenario(code):
    return 'displayState = ' + json.dumps(current_state(2)) + ';\n' + r"""
const extension=new AlaDisplay();extension.enable();
const applications=[];
const originalCall=Gio.DBus.session.call;
const Variant=GLib.Variant;
GLib.Variant=class extends Variant {constructor(type,value) {super(type,value);this.type=type;}};
Gio.DBus.session.call=function(...args) {
    if (args[3]==='ApplyMonitorsConfig') {
        assert.equal(args[4].type,'(uua(iiduba(ssa{sv}))a{sv})');
        applications.push(args[4].deep_unpack());
        args[9](this,[]);
    } else originalCall.apply(this,args);
};
const request={serial:7,session:extension._layoutSession,method:'verify',outputs:[
    {id:'DP-1',mode_id:'full',x:1920,y:0,scale:1,transform:0,primary:true},
    {id:'DP-2',mode_id:'full',x:0,y:0,scale:1,transform:0,primary:false},
]};
function layout(value=request) {
    return new Promise(resolve=>extension.ApplyLayoutAsync([JSON.stringify(value)],{
        get_sender:()=> ':1.2',return_value:value=>resolve(JSON.parse(value.deep_unpack()[0])),
    }));
}
""" + code


@pytest.mark.parametrize('version', ['47.4', '49.4'])
def test_verify_and_temporary_use_only_fixed_typed_methods(version):
    run_extension(scenario(r"""
assert.equal((await layout()).state,'ok');
request.method='temporary';
assert.equal((await layout()).state,'ok');
assert.deepEqual(applications.map(a=>a.slice(0,2)),[[7,0],[7,1]]);
assert.deepEqual(applications[1][2].map(l=>l.slice(0,5)),[[1920,0,1,0,true],[0,0,1,0,false]]);
assert.equal((await layout({...request,method:'persistent'})).state,'unknown');
assert.equal(applications.length,2);extension.disable();assertClean();
"""), version)


@pytest.mark.parametrize('change', [
    'request.serial=6', 'request.session="stale"', 'request.outputs[0].scale=1.25',
    'request.outputs[0].x=-1', 'request.outputs[0].primary=false',
    'request.outputs[0].transform=1', 'request.outputs[1].id="DP-1"',
    'request.outputs[0].mode_id="unknown"', 'request.outputs.pop()',
    'Main.sessionMode.isLocked=true', 'config.user="other"', 'config.display=false', 'callerUid=2000',
    'displayState[1][0][2]["rgb-range"]=2', 'displayState[1][0][2]["is-for-lease"]=true',
    'displayState[2][0][5].push(displayState[2][1][5][0])',
    'displayState[3]["global-scale-required"]=true;request.outputs[0].scale=2',
])
def test_invalid_or_unsupported_request_never_reaches_mutter_apply(change):
    run_extension(scenario(change + ";\n" + r"""
assert.equal((await layout()).state,'unknown');assert.equal(applications.length,0);
extension.disable();assertClean();
"""))


def test_unrequested_underscanning_color_and_layout_mode_are_preserved():
    run_extension(scenario(r"""
displayState[1][0][2]={'is-underscanning':true,'color-mode':1,'rgb-range':1};
displayState[3]['supports-changing-layout-mode']=true;
assert.equal((await layout()).state,'ok');
const props=applications[0][2][0][5][0][2];
assert.equal(props.underscanning.type,'b');assert.equal(props.underscanning.value,true);
assert.equal(props['color-mode'].type,'u');assert.equal(props['color-mode'].value,1);
assert.equal(applications[0][3]['layout-mode'].value,1);
extension.disable();assertClean();
"""))


def test_extension_disabling_during_read_never_sends_a_change():
    run_extension(scenario(r"""
deferred=true;const response=layout();await new Promise(resolve=>setImmediate(resolve));
assert.equal(waiting.length,1);extension.disable();waiting.shift()();
assert.equal((await response).state,'unknown');assert.equal(applications.length,0);assertClean();
"""))
