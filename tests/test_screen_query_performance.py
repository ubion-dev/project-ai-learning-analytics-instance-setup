"""Read-only request batching; model latency is not a device benchmark."""
from copy import deepcopy
from threading import Barrier, Lock
import time
import pytest
from ala import screens, display
from ala.config import Config, ScreenConfig
from ala.system import Command, Paths


@pytest.fixture
def model(monkeypatch,tmp_path):
    config=Config(user='user',schema_version=2,screens=(
        ScreenConfig(id='default',output='DP-1',url='https://example.com/one'),
        ScreenConfig(id='other',output='DP-2')))
    inventory={'state':'ok','outputs':[], 'logical_outputs':[
        {'id':'DP-1','connectors':['DP-1'],'confirmed':True},
        {'id':'DP-2','connectors':['DP-2'],'confirmed':True}]}
    calls=[]
    lock=Lock()
    state={'healthy':True,'barrier':None,'latency':0}
    def record(value):
        with lock:calls.append(value)
    class Host:
        def run(self,argv,**options):
            record(('command',tuple(argv)))
            return Command(0,'ready')
        def execute(self,*a,**kw):pytest.fail('read-only inspection must not execute changes')
    host=Host()
    def topology(**kw):record(('topology',));return deepcopy(inventory)
    def check(config,home,host,paths):
        record(('check',config.target_id))
        if state['barrier']:state['barrier'].wait(timeout=3)
        host.run(['systemctl','--user','show-environment'])
        return {'state':'running','process_verified':state['healthy'],
                'service':{'ActiveState':'active'},
                'health':{'state':'healthy' if config.url else 'waiting_configuration'},'results':[]}
    def placement(config,health,**kw):
        record(('placement',config.target_id))
        if 'inventory' not in kw:topology()
        return {'state':'ok','confirmed_window_count':1,'output':config.output}
    def rpc(config,host,paths,action):
        record(('rpc',action))
        time.sleep(state['latency'])
        if action=='outputs':return topology()
        if action=='status':return check(config,tmp_path,host,paths)
        if action=='windows':
            observed=check(config,tmp_path,host,paths)
            return placement(config,observed['health'])
        assert action=='screen-observe'
        assert config.runtime_pin is None
        return screens.inspect_screens(config,host=host,paths=paths,local=True)
    monkeypatch.setattr(screens,'_applied_context',lambda c,t,h,p:c.for_screen(t))
    monkeypatch.setattr(screens,'topology',topology)
    monkeypatch.setattr(display,'_check_user',check)
    monkeypatch.setattr(display,'_user_call',rpc)
    monkeypatch.setattr(display,'chrome_action',lambda a,c,**kw:rpc(c,kw['host'],kw['paths'],a))
    monkeypatch.setattr('ala.monitors.placement',placement)
    return config,host,Paths(tmp_path),calls,state


def test_batch_preserves_results_while_reducing_process_and_ownership_queries(model):
    config,host,paths,calls,state=model
    old=screens.inspect_screens(config,host,paths)
    assert sum(c[0]=='rpc' for c in calls)==5
    assert sum(c[0]=='check' for c in calls)==4
    assert sum(c[0]=='topology' for c in calls)==3
    calls.clear()
    new=screens.inspect_screens(config,host,paths,batch=True)
    assert new==old
    assert sum(c[0]=='rpc' for c in calls)==1
    assert sum(c[0]=='check' for c in calls)==2
    assert sum(c[0]=='topology' for c in calls)==1
    assert sum(c[0]=='command' for c in calls)==1


def test_target_inspections_run_concurrently_but_keep_configuration_order(model):
    config,host,paths,calls,state=model
    state['barrier']=Barrier(2)
    report=screens.inspect_screens(config,host,paths,batch=True)
    assert [t['id'] for t in report['targets']]==['default','other']
    assert all(t['state']=='running' for t in report['targets'])


def test_query_cache_is_discarded_and_failed_ownership_is_not_healthy(model):
    config,host,paths,calls,state=model
    screens.inspect_screens(config,host,paths,batch=True)
    calls.clear();state['healthy']=False
    result=screens.inspect_screens(config,host,paths,batch=True)
    assert all(t['placement']['state']=='pending' for t in result['targets'])
    assert not any(c[0]=='placement' for c in calls)
    assert sum(c[0]=='command' for c in calls)==1


def test_failed_batch_never_replays_old_success_or_launches_a_series_of_fallback_processes(model,monkeypatch):
    config,host,paths,calls,state=model
    screens.inspect_screens(config,host,paths,batch=True)
    def failed(c,h,p,action):
        assert action=='screen-observe'
        return {'state':'unknown','reason':'synthetic unavailable'}
    monkeypatch.setattr(display,'_user_call',failed)
    calls.clear()
    result=screens.inspect_screens(config,host,paths,batch=True)
    assert result['state']=='unknown' and result['output_state']=='unknown'
    assert all(t['state']=='unknown' for t in result['targets'])
    assert calls==[]


def test_missing_user_runtime_keeps_hardware_and_target_diagnostics(model,monkeypatch):
    config,host,paths,calls,state=model
    def failed(*a,**kw):raise ValueError('synthetic missing account')
    monkeypatch.setattr(display,'_user_call',failed)
    result=screens.inspect_screens(config,host,paths,batch=True)
    assert result['state']=='unknown' and 'hardware_output_state' in result
    assert [t['id'] for t in result['targets']]==['default','other']
    assert all(t['state']=='unknown' for t in result['targets'])


def test_batch_rejects_a_selected_snapshot_for_another_user(model,monkeypatch):
    from dataclasses import replace
    config,host,paths,calls,state=model
    monkeypatch.setattr(screens,'_applied_context',lambda c,t,h,p:replace(c,user='other').for_screen(t))
    report=screens.inspect_screens(config,host,paths,batch=True)
    assert all(t['state']=='unknown' for t in report['targets'])
    assert not any(c[0] in {'check','placement'} for c in calls)


def test_service_shares_metadata_reads_only_inside_one_request(model,monkeypatch):
    from ala.service import Operations
    from ala.system import InspectionHost
    config,host,paths,calls,state=model
    operations=Operations(paths=paths,host=host)
    monkeypatch.setattr(operations,'config',lambda:config)
    def inspect(c,**kw):
        assert kw['batch'] is True and isinstance(kw['host'],InspectionHost)
        return {'targets':[{'id':s.id} for s in c.screen_targets()]}
    monkeypatch.setattr(screens,'inspect_screens',inspect)
    monkeypatch.setattr('ala.frontend.read_status',lambda **kw:{'targets':{}})
    monkeypatch.setattr('ala.display_onboarding.status',lambda **kw:{'state':'not_requested'})
    monkeypatch.setattr('ala.display_reset.status',lambda **kw:{'state':'none'})
    monkeypatch.setattr('ala.screen_swap.status',lambda **kw:{'state':'none'})
    monkeypatch.setattr('ala.screen_control.read_status',lambda *a,**kw:{'state':'none'})
    def snapshot(target,**kw):
        kw['host'].run(['systemctl','show','ala-synthetic.service'])
        return {'generation':1}
    monkeypatch.setattr('ala.runtime.snapshot',snapshot)
    monkeypatch.setattr('ala.runtime.settings_status',lambda *a,**kw:{'state':'kept','pending':False})
    for _ in range(2):
        calls.clear()
        result=operations.dispatch('screens-inspect',{})
        assert [t['id'] for t in result['targets']]==['default','other']
        assert sum(c[0]=='command' for c in calls)==1
        assert result['query_timing']['total_seconds']>=0
        assert operations.host is host
