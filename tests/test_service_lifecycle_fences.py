"""Public default commands share the same stop/generation fence as the TUI."""
from contextlib import contextmanager
from types import SimpleNamespace
import pytest

from ala import service, runtime, screens, display
from ala.config import Config
from ala.system import Paths


@pytest.mark.parametrize('action',['start','stop','restart'])
@pytest.mark.parametrize('explicit_target',[False,True])
def test_public_default_lifecycle_always_cancels_the_previous_generation(tmp_path,monkeypatch,action,explicit_target):
    calls=[];lock_depth=0
    @contextmanager
    def lock(_):
        nonlocal lock_depth
        lock_depth+=1
        assert lock_depth==1
        try:yield
        finally:lock_depth-=1
    class Store:
        def __init__(self,*a,**kw):pass
        def load(self,target):assert target=='default';return {'generation':1}
        def cancel(self,target,*,stopped):
            assert lock_depth==1
            calls.append(('cancel',target,stopped))
    monkeypatch.setattr(service,'change_lock',lock)
    monkeypatch.setattr(service,'JobStore',lambda *a:SimpleNamespace(list=lambda:[]))
    monkeypatch.setattr(runtime,'RuntimeStore',Store)
    monkeypatch.setattr(display,'chrome_action',lambda *a,**kw:pytest.fail('public lifecycle must use shared fenced API'))
    def action_call(name,config,*,target,**kw):
        assert lock_depth==1
        calls.append(('action',name,target))
        return {'state':'pending','exit_code':2}
    monkeypatch.setattr(screens,'screen_action',action_call)
    operations=service.Operations(paths=Paths(tmp_path))
    monkeypatch.setattr(operations,'config',lambda:Config(user='user',url='https://example.com/'))
    payload={'action':action}
    if explicit_target:payload['target']='default'
    assert operations.dispatch('chrome',payload)['exit_code']==2
    assert calls==[('cancel','default',action=='stop'),('action',action,'default')]


@pytest.mark.parametrize('action',['status','check','logs'])
def test_default_readonly_commands_do_not_cancel_or_write_selection(tmp_path,monkeypatch,action):
    monkeypatch.setattr(service,'change_lock',lambda *_:pytest.fail('read-only does not lock mutations'))
    class ReadStore:
        def __init__(self,*a,**kw):pass
        def load(self,target):return None
        def cancel(self,*a,**kw):pytest.fail('read-only must not alter a generation')
        def _write(self,*a,**kw):pytest.fail('read-only must not save a selection')
    monkeypatch.setattr(runtime,'RuntimeStore',ReadStore)
    calls=[]
    monkeypatch.setattr(display,'chrome_action',lambda name,*a,**kw:calls.append(name) or {'exit_code':0})
    operations=service.Operations(paths=Paths(tmp_path))
    monkeypatch.setattr(operations,'config',lambda:Config(user='user'))
    assert operations.dispatch('chrome',{'action':action})['exit_code']==0
    assert calls==[action]
