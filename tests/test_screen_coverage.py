from dataclasses import replace
import json
import pytest
from ala import screen_coverage as coverage, display
from ala.config import Config, ScreenConfig
from ala.system import atomic_json, Command
from test_display_onboarding import environment


def inventory(count=3):
    return {'state':'ok','logical_outputs':[
        {'id':f'DP-{n}', 'connectors':[f'DP-{n}'], 'confirmed':True, 'primary':n==1,
         'x':0 if n==3 else (n-1)*1920, 'y':1080 if n==3 else 0,
         'width':1920,'height':1080} for n in range(1,count+1)]}


def model(url=None):
    return Config(user='user',schema_version=2,screens=(ScreenConfig(id='default',url=url,output='DP-1'),))


def states(config, **flags):
    return {s.id:(coverage.bound_context(config.for_screen(s.id),inventory()),flags.get(s.id))
            for s in config.screen_targets()}


@pytest.mark.parametrize('url',[None,'https://example.com/did'])
@pytest.mark.parametrize('count',[2,3])
def test_every_uncovered_connected_output_has_a_waiting_window_with_or_without_sites(url,count):
    config=model(url)
    assert coverage.desired(config,inventory(count),states=states(config),observations={}) == {
        f'DP-{n}' for n in range(1,count+1)}
    assert config.screen_targets()[0].url == url


def test_real_window_observation_hands_off_only_that_output_and_preserves_other_waiting_pages():
    config=model('https://example.com/did')
    observations={'default':{'process_verified':True,'placement':{'state':'ok','output':'DP-1'}}}
    assert coverage.desired(config,inventory(),states=states(config),observations=observations)=={'DP-2','DP-3'}
    observations['default']['process_verified']=False
    assert 'DP-1' in coverage.desired(config,inventory(),states=states(config),observations=observations)
    observations['default'].update(process_verified=True,placement={'state':'pending','output':'DP-1'})
    assert 'DP-1' in coverage.desired(config,inventory(),states=states(config),observations=observations)


@pytest.mark.parametrize('manual',[True,False])
def test_manual_stop_is_preserved_but_disabled_site_autostart_still_gets_a_waiting_window(manual):
    config=model()
    if not manual: config=replace(config,screens=(replace(config.screens[0],autostart=False),))
    expected={'DP-2','DP-3'} if manual else {'DP-1','DP-2','DP-3'}
    assert coverage.desired(config,inventory(),states=states(config,default={'suppressed':manual}),observations={})==expected


def test_legacy_default_is_bound_to_primary_for_observation_without_saving():
    config=Config(user='user',url='https://example.com/did')
    context=coverage.bound_context(config.for_screen(),inventory())
    assert context.output=='DP-1' and config.output is None and config.schema_version==1


def enable(environment):
    paths,host,release,config,*_=environment
    metadata=release/'COMPATIBILITY.json'
    metadata.write_text(json.dumps(json.loads(metadata.read_text())|{'screen_coverage_protocol':1}))
    assert coverage.prepare(config,release,paths,host)
    return environment


def test_root_provisioning_is_not_gated_by_initialization_or_url_and_never_changes_sites(environment):
    paths,host,release,config,*_=enable(environment)
    configured=replace(config,url='https://example.com/did')
    before=paths.config.read_bytes()
    assert coverage.prepare(configured,release,paths,host)
    assert coverage.enabled(configured,paths)
    assert paths.config.read_bytes()==before
    text=paths.at('/etc/systemd/user/'+coverage.UNIT).read_text()
    assert '_screen-coverage' in text and 'KillMode=control-group' in text
    assert str(release/'entry.py') in text
    assert not coverage.enabled(replace(configured,display=False),paths)


def test_unmanaged_service_and_reserved_target_are_preserved(environment):
    paths,host,release,config,*_=enable(environment)
    path=paths.at('/etc/systemd/user/'+coverage.UNIT)
    path.write_text('external service')
    with pytest.raises(ValueError): coverage.prepare(config,release,paths,host)
    assert path.read_text()=='external service'
    config=replace(config,schema_version=2,screens=(ScreenConfig(id=coverage.identifier('DP-1')),))
    assert not coverage.authorized(config,'DP-1',paths,host)


def test_wait_worker_uses_separate_profile_and_exits_if_display_use_is_disabled(environment,monkeypatch):
    paths,host,release,config,*_=enable(environment)
    monkeypatch.setattr(display.os,'getuid',lambda:1000)
    monkeypatch.setattr(display,'_wait_for_graphical_ready',lambda *a,**kw:{'WAYLAND_DISPLAY':'wayland-0'})
    monkeypatch.setattr('ala.monitors.topology',lambda **kw:inventory())
    seen=[]
    class Supervisor:
        def __init__(self,cfg,home,argv,**options):
            assert cfg.url is None and cfg.output=='DP-2'
            assert '--user-data-dir='+str(home/'.config/ala/chrome-wait'/coverage.identifier('DP-2')) in argv
            assert 'placement_callback' in options and not options['stop_requested']()
            atomic_json(paths.config,replace(config,display=False).to_dict(),0o644)
            assert options['stop_requested']()
            seen.append(cfg)
        def run(self): return 0
    def runner(**kwargs): return display.run_chrome(**kwargs,supervisor_factory=Supervisor)
    assert coverage.run_wait('DP-2',config=config,home=paths.at('/home/user'),host=host,paths=paths,runner=runner)==0
    assert len(seen)==1


def test_configured_site_browser_starts_recovery_before_output_coordinates_are_ready(environment,monkeypatch):
    paths,host,release,config,*_=environment
    config=replace(config,schema_version=2,screens=(ScreenConfig(id='default',url='https://example.com/did',output='DP-2'),)).for_screen()
    monkeypatch.setattr(display.os,'getuid',lambda:1000)
    monkeypatch.setattr(display,'_wait_for_graphical_ready',lambda *a,**kw:{'WAYLAND_DISPLAY':'wayland-0'})
    monkeypatch.setattr('ala.monitors.topology',lambda **kw:{'state':'pending','logical_outputs':[]})
    seen=[]
    class Supervisor:
        def __init__(self,cfg,home,argv,**options):
            from ala.display_health import RECOVERY_URL
            assert argv[-1]==RECOVERY_URL and 'placement_callback' in options
            assert cfg.url=='https://example.com/did'
            seen.append(cfg)
        def run(self): return 0
    assert display.run_chrome(config=config,home=paths.at('/home/user'),host=host,paths=paths,supervisor_factory=Supervisor)==0
    assert len(seen)==1


def test_children_are_independent_do_not_restart_live_windows_and_terminate_only_owned_handles():
    now=[0]
    spawned=[]
    class Child:
        def __init__(self,output): self.output,self.result,self.stops=output,None,0
        def poll(self): return self.result
        def terminate(self): self.stops+=1
    def spawn(output):
        child=Child(output);spawned.append(child);return child
    children=coverage.Children(spawn,clock=lambda:now[0])
    children.reconcile({'DP-1','DP-2','DP-3'})
    children.reconcile({'DP-1','DP-2','DP-3'})
    assert len(spawned)==3
    children.reconcile({'DP-2','DP-3'})
    assert next(c for c in spawned if c.output=='DP-1').stops==1
    assert all(c.stops==0 for c in spawned if c.output!='DP-1')
    for _ in range(5):
        if 'DP-2' in children.running: children.running['DP-2'].result=1
        children.reconcile({'DP-2','DP-3'})
    assert sum(c.output=='DP-2' for c in spawned)==3
    now[0]=301
    children.reconcile({'DP-2','DP-3'})
    assert sum(c.output=='DP-2' for c in spawned)==4


def test_one_child_spawn_failure_does_not_abort_other_outputs():
    class Child:
        def poll(self): return None
    def spawn(output):
        if output=='DP-1': raise OSError('synthetic spawn failure')
        return Child()
    children=coverage.Children(spawn)
    children.reconcile({'DP-1','DP-2'})
    assert set(children.running)=={'DP-2'}


@pytest.mark.parametrize('has_site',[False,True])
def test_reboot_login_starts_coverage_even_when_did_autostart_is_off(environment,monkeypatch,has_site):
    paths,host,release,config,*_=enable(environment)
    if has_site: config=replace(config,url='https://example.com/did')
    monkeypatch.setattr(display.os,'getuid',lambda:1000)
    monkeypatch.setattr(display,'_session_connection',lambda host,paths:host)
    monkeypatch.setattr(display,'_workspace_policy',lambda *a,**kw:{'state':'ok','exit_code':0})
    calls=[]
    monkeypatch.setattr(coverage,'ensure',lambda *a: calls.append('coverage') or {'state':'starting','exit_code':0})
    assert display.session_start(config=config,home=paths.at('/home/user'),host=host,paths=paths,environment={})==0
    assert calls==['coverage']
    assert display._autostart_enabled(config,paths)


def test_initialization_fence_does_not_block_output_waiting_controller_on_login(environment,monkeypatch):
    from ala import display_onboarding as onboarding
    paths,host,release,config,*_=enable(environment)
    onboarding.reserve(config,release,paths=paths,host=host,run_id='synthetic-run')
    monkeypatch.setattr(display.os,'getuid',lambda:1000)
    monkeypatch.setattr(display,'_session_connection',lambda host,paths:host)
    monkeypatch.setattr(display,'_workspace_policy',lambda *a,**kw:{'state':'ok','exit_code':0})
    calls=[]
    monkeypatch.setattr(coverage,'ensure',lambda *a: calls.append('coverage') or {'state':'starting','exit_code':0})
    assert display.session_start(config=config,home=paths.at('/home/user'),host=host,paths=paths,environment={})==78
    assert calls==['coverage']


def test_controller_handles_three_outputs_independently_and_hands_off_only_ready_site(environment,monkeypatch):
    paths,host,release,config,*_=enable(environment)
    config=model('https://example.com/did')
    atomic_json(paths.config,config.to_dict(),0o644)
    monkeypatch.setattr(display.os,'getuid',lambda:1000)
    monkeypatch.setattr(display,'_load_user_config',lambda *a,**kw:(Config.load(paths.config),paths.at('/home/user')))
    monkeypatch.setattr(display,'RELEASE',release)
    spawned=[]
    class Child:
        def __init__(self,argv,**options):
            self.argv,self.result,self.stops=argv,None,0
            assert options['close_fds'] is True
            assert 'ALA_RUNTIME_CONFIG' not in options['env']
            spawned.append(self)
        def poll(self):return self.result
        def terminate(self):self.stops+=1;self.result=0
        def wait(self,timeout):return self.result
    monkeypatch.setattr(coverage.subprocess,'Popen',Child)
    frames=[{'DP-1','DP-2','DP-3'},{'DP-2','DP-3'}]
    def frame(*a):return frames.pop(0)
    monkeypatch.setattr(coverage,'frame',frame)
    ticks=[0]
    def pause(_):
        ticks[0]+=1
        if ticks[0]==10: atomic_json(paths.config,replace(config,display=False).to_dict(),0o644)
    assert coverage.run(host=host,paths=paths,sleeper=pause)==0
    assert len(spawned)==3
    assert {c.argv[-1] for c in spawned}=={'DP-1','DP-2','DP-3'}
    assert all(c.argv[:3]==[str(release/'.venv/bin/python'),'-I',str(release/'entry.py')] for c in spawned)
    assert all(c.stops==1 for c in spawned)


@pytest.mark.parametrize('running',[True,False])
@pytest.mark.parametrize('issue',[None,'old-extension','shadow'])
def test_start_requires_owned_service_and_reports_extension_relogin(environment,monkeypatch,running,issue):
    paths,host,release,config,*_=enable(environment)
    calls=[]
    class UserHost:
        def run(self,argv,**options):
            calls.append(argv)
            if '--property=FragmentPath' in argv:
                path='/home/user/external.service' if issue=='shadow' else '/etc/systemd/user/'+coverage.UNIT
                return Command(0,'LoadState=loaded\nFragmentPath='+path+'\n')
            if argv[:3]==['systemctl','--user','show']:
                return Command(0,'LoadState=loaded\nActiveState='+('active' if running else 'inactive')+'\nResult=success\n')
            return Command(0)
    monkeypatch.setattr('ala.monitors.bridge',lambda *a,**kw:{'state':'ok','capabilities':[] if issue=='old-extension' else ['output-waiting-windows']})
    result=coverage.ensure(config,paths.at('/home/user'),UserHost(),paths)
    starts=[a for a in calls if a[:3]==['systemctl','--user','start']]
    assert bool(starts) is (not running and issue!='shadow')
    assert result['exit_code']==(0 if issue is None else 2)
    if issue=='old-extension': assert '로그아웃·로그인' in result['reason']
    assert not any('restart' in a or 'reset-failed' in a or 'stop' in a for a in calls)
