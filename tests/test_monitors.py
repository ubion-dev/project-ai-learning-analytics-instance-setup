from copy import deepcopy
import json
from pathlib import Path
import pytest

from ala.config import Config,ScreenConfig
from ala import monitors
from ala.system import Command


class BridgeHost:
    def __init__(self,value):self.value=value;self.calls=[]
    def run(self,argv,**kwargs):
        self.calls.append((argv,kwargs))
        return Command(0,repr((json.dumps(self.value),)))


def state(mirror=False):
    specs=[['HDMI-1','Synthetic','Model',''],['DP-1','Synthetic','Model','']]
    monitors_=[[s,[['mode',1920,1080,60.0,1.0,[1.0],{'is-current':True}]],{}] for s in specs]
    logical=[[0,0,1.0,0,True,specs if mirror else [specs[0]],{}]]
    rects=[{'x':0,'y':0,'width':1920,'height':1080}]
    if not mirror:
        logical.append([1920,0,1.0,0,False,[specs[1]],{}])
        rects.append({'x':1920,'y':0,'width':1920,'height':1080})
    return {'protocol':1,'state':'ok','gnome_version':'47.4','display_config':[1,monitors_,logical,{}],
            'compositor_monitors':rects}


@pytest.mark.parametrize('mirror',[False,True])
def test_mutter_inventory_keeps_physical_logical_and_mirrors_separate(mirror):
    host=BridgeHost(state(mirror))
    value=monitors.topology(host=host)
    assert value['state']=='ok' and len(value['outputs'])==2
    assert len(value['logical_outputs'])==(1 if mirror else 2)
    assert value['logical_outputs'][0]['mirrored'] is mirror
    assert 'serial' not in value['outputs'][0] and 'vendor' not in value['outputs'][0]
    assert all('Place' not in args[-1] for args,_ in host.calls)


def test_connector_alias_resolves_same_mirrored_logical_output():
    value=monitors.topology(host=BridgeHost(state(True)))
    assert monitors.resolve_output(value,'DP-1')['output']==monitors.resolve_output(value,'HDMI-1')['output']
    assert monitors.resolve_output(value,None,auto=True)['selector']=='DP-1+HDMI-1'


def test_ambiguous_or_missing_output_never_defaults_to_primary():
    value=monitors.topology(host=BridgeHost(state()))
    assert monitors.resolve_output(value,None,auto=True)['state']=='pending'
    assert monitors.resolve_output(value,'unknown')['state']=='pending'


@pytest.mark.parametrize('payload',[None,{},['bad'],{'protocol':2,'state':'ok'}])
def test_malformed_bridge_response_is_unknown(payload):
    assert monitors.bridge('GetState',host=BridgeHost(payload))['state']=='unknown'


def test_unsupported_gnome_and_uncorroborated_rect_remain_pending():
    value=state();value['gnome_version']='99.1'
    assert monitors.topology(host=BridgeHost(value))['state']=='pending'
    value=state();value['compositor_monitors']=[]
    result=monitors.topology(host=BridgeHost(value))
    assert not result['logical_outputs'][0]['confirmed']
    assert monitors.resolve_output(result,'HDMI-1')['state']=='pending'


def test_fractional_scale_uses_compositor_rect_not_physical_guess():
    value=state(True);value['display_config'][2][0][2]=1.5
    value['compositor_monitors'][0].update(width=1280,height=720)
    result=monitors.topology(host=BridgeHost(value))
    assert result['logical_outputs'][0]['width']==1280


def test_split_rect_preserves_odd_width_without_overlap():
    output={'x':-100,'y':20,'width':1919,'height':1080}
    left=monitors.desired_rect(output,'left');right=monitors.desired_rect(output,'right')
    assert left['x']+left['width']==right['x']
    assert left['width']+right['width']==1919


def test_placement_readonly_does_not_send_place(monkeypatch):
    config=Config(schema_version=2,user='user',screens=(ScreenConfig(output='HDMI-1'),)).for_screen()
    health={'supervisor_pid':10,'browser_pid':20,'supervisor_start_ticks':'100','browser_start_ticks':'200','boot_id':'test-boot'}
    calls=[]
    def bridge(method,request=None,**kwargs):
        calls.append(method)
        return {'state':'ok','windows':[{'rect':{'x':0,'y':0,'width':100,'height':100},'fullscreen':False}]}
    monkeypatch.setattr(monitors,'bridge',bridge)
    inventory={'state':'ok','logical_outputs':[{'id':'HDMI-1','connectors':['HDMI-1'],'confirmed':True,'x':0,'y':0,'width':1920,'height':1080}]}
    assert monitors.placement(config,health,inventory=inventory)['state']=='pending'
    assert calls==['Inspect']
    assert monitors.placement(config,health,inventory=inventory,apply=True)['state']=='pending'
    assert calls==['Inspect','Inspect','Place','Inspect']


def test_placement_requires_single_window_identity(monkeypatch):
    config=Config(schema_version=2,user='user',screens=(ScreenConfig(output='HDMI-1'),)).for_screen()
    assert monitors.placement(config,{})['state']=='pending'
    health={'supervisor_pid':10,'browser_pid':20,'supervisor_start_ticks':'100','browser_start_ticks':'200','boot_id':'test-boot'}
    monkeypatch.setattr(monitors,'bridge',lambda *a,**kw:{'state':'ok','windows':[{},{}]})
    inventory={'state':'ok','logical_outputs':[{'id':'HDMI-1','connectors':['HDMI-1'],'confirmed':True,'x':0,'y':0,'width':1920,'height':1080}]}
    assert monitors.placement(config,health,inventory=inventory,apply=True)['state']=='blocked'


def test_legacy_default_does_not_require_new_bridge():
    assert monitors.placement(Config(user='user'),{})['state']=='not_applicable'
