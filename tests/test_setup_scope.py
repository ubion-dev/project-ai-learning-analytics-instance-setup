"""Setup completion is independent of the currently running display."""
from ala.config import Config
from ala.engine import Engine
from ala.model import CheckResult, Report
from ala.substeps import child_rows
from ala.system import Paths


def engine_for(monkeypatch, tmp_path, *, gpu_state='ok'):
    monkeypatch.setattr('ala.engine.platform_error', lambda _:None)
    class FakeHost:
        def run(self,*_,**__):
            raise AssertionError('unexpected host query')
        def execute(self,*_,**__):
            raise AssertionError('unexpected host mutation')
    engine = Engine(Config(user='user',url='https://example.com/display'),host=FakeHost(),paths=Paths(tmp_path))
    monkeypatch.setattr(engine,'_account',lambda _: (None,CheckResult('user','ok')))
    for name in ('base','updates','hardware','network','shell','ssh','docker','gpu','dev_tools','display_policy'):
        state = gpu_state if name=='gpu' else 'ok'
        monkeypatch.setattr(engine,'_'+name,lambda name=name,state=state:[CheckResult(name+'.configuration',state)])
    return engine


def test_full_and_configuration_results_share_one_display_pass_and_restore_scope(monkeypatch,tmp_path):
    engine=engine_for(monkeypatch,tmp_path)
    calls=[]
    def parts(*_,include_runtime=True):
        calls.append(include_runtime)
        return [CheckResult('chrome.autostart','ok')], ([CheckResult('chrome.process','unknown',actual='runtime query failed')] if include_runtime else [])
    monkeypatch.setattr('ala.display.inspect_display_parts',parts)
    full=engine.inspect(workers=4)
    assert full.exit_code==1 and engine.configuration_report.exit_code==0
    assert calls==[True]
    assert 'chrome.process' not in {r.id for r in engine.configuration_report.results}
    plan=engine.plan(workers=4)
    assert calls==[True,False] and plan.exit_code==0 and plan.to_dict()['scope']=='configuration'
    assert engine.inspect().exit_code==1 and calls==[True,False,True]
    assert not list(tmp_path.iterdir())


def test_apply_finishes_configuration_without_display_lifecycle_or_runtime_query(monkeypatch,tmp_path):
    engine=engine_for(monkeypatch,tmp_path)
    inspected=[]
    actions=[]
    def parts(*_,include_runtime=True):
        inspected.append(include_runtime)
        return [CheckResult('chrome.autostart','ok')], ([CheckResult('chrome.runtime','error')] if include_runtime else [])
    monkeypatch.setattr('ala.display.inspect_display_parts',parts)
    monkeypatch.setattr('ala.display.apply_display',lambda *_:actions.append('configure') or [CheckResult('chrome.autostart','ok')])
    report=engine.apply()
    assert report.exit_code==0 and report.scope=='configuration'
    assert actions==['configure'] and inspected==[False]
    assert engine._configuration_only is False
    assert not list(tmp_path.iterdir())


def test_configuration_scope_keeps_gpu_reboot_wait_and_url_input_wait(monkeypatch,tmp_path):
    engine=engine_for(monkeypatch,tmp_path,gpu_state='pending')
    monkeypatch.setattr('ala.display.inspect_display_parts',lambda *_,**kw:([CheckResult('chrome.url','pending',actual='입력 대기')],[]))
    report=engine.plan()
    assert report.exit_code==2
    assert {'gpu.configuration','chrome.url'} <= {r.id for r in report.results if r.state=='pending'}


def test_configuration_children_hide_runtime_but_full_view_keeps_it():
    report=Report([CheckResult('chrome.autostart','ok')],scope='configuration').to_dict()
    rows,_=child_rows('chrome',report=report)
    assert {r['id'] for r in rows}=={'chrome.autologin','chrome.profile','chrome.service'}
    report.pop('scope')
    rows,_=child_rows('chrome',report=report)
    assert {'chrome.session','chrome.health','chrome.policy'} <= {r['id'] for r in rows}
