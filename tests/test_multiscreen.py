from dataclasses import replace
import json
import os
from pathlib import Path
import pytest
from ala.config import Config,ScreenConfig
from ala import display,screens
from ala.system import Command,Paths


def fleet():
    return Config(schema_version=2,user='user',screens=(ScreenConfig(url='https://example.com/one',output='HDMI-1'),
                ScreenConfig(id='right',url='https://example.com/two',output='DP-1',layout='right')))


def test_schema_one_roundtrip_and_existing_profile_are_unchanged():
    config=Config(user='user',url='https://example.com/')
    assert 'screens' not in config.to_dict()
    assert Config.from_dict(config.to_dict())==config
    assert config.screen_targets()[0].id=='default'
    assert display._profile(Path('/home/user'))==Path('/home/user/.config/ala/chrome-display')


def test_schema_two_context_and_internal_pin_are_data_only():
    config=fleet();pin={'config_path':'/var/lib/ala/runtime/configs/test.json'}
    context=replace(config.for_screen('right'),runtime_pin=pin).for_screen('right',revision='revision')
    assert context.target_id=='right' and context.output=='DP-1' and context.layout=='right'
    assert context.config_revision=='revision' and context.runtime_pin==pin
    assert context.url=='https://example.com/two'
    assert 'runtime_pin' not in context.to_dict()
    assert Config.from_dict(config.to_dict())==config
    with pytest.raises(ValueError):Config.from_dict(config.to_dict()|{'runtime_pin':pin})


@pytest.mark.parametrize('targets',[
    [ScreenConfig(),ScreenConfig()],
    [ScreenConfig(output='HDMI-1'),ScreenConfig(id='other',output='HDMI-1')],
    [ScreenConfig(output='HDMI-1',layout='left'),ScreenConfig(id='other',output='HDMI-1',layout='left')]])
def test_duplicate_ids_and_overlapping_declared_outputs_rejected(targets):
    with pytest.raises(ValueError):Config(schema_version=2,screens=targets)


@pytest.mark.parametrize('value',[{'id':'../escape'},{'id':'bad;id'},{'id':'x','layout':[]},{'id':'x','autostart':1},
    {'id':'x','url':'file:///etc/passwd'},{'id':'x','output':'HDMI 1'},{'id':'x','frontend':{'enabled':True}}])
def test_screen_input_rejects_ambiguous_and_executable_data(value):
    with pytest.raises((ValueError,TypeError)):ScreenConfig.from_dict(value)


def test_independent_profile_unit_and_options():
    config=fleet();home=Path('/home/user')
    one=display.chrome_arguments(config.for_screen('default'),home,{'WAYLAND_DISPLAY':'wayland-0'})
    two=display.chrome_arguments(config.for_screen('right'),home,{'WAYLAND_DISPLAY':'wayland-0'})
    assert '--user-data-dir=/home/user/.config/ala/chrome-display' in one
    assert '--user-data-dir=/home/user/.config/ala/chrome-displays/right' in two
    assert '--start-fullscreen' in one and '--start-fullscreen' not in two
    assert display.unit_name()=='ala-chrome.service'
    assert display.unit_name('right')=='ala-chrome@'+'right.service'
    assert '"_runtime-dispatch" "right"' in display.unit_text('right')


def test_mirror_alias_overlap_blocks_before_lifecycle(monkeypatch):
    config=fleet();calls=[]
    inventory={'state':'ok','logical_outputs':[{'id':'DP-1+HDMI-1','connectors':['HDMI-1','DP-1'],
        'confirmed':True,'x':0,'y':0,'width':1920,'height':1080}]}
    monkeypatch.setattr(screens,'_inventory',lambda *a:inventory)
    monkeypatch.setattr(display,'chrome_action',lambda action,*a,**kw:calls.append(action) or {'service':{'ActiveState':'inactive'}})
    result=screens.screen_action('start',config)
    assert result['state']=='blocked' and calls==['status']


def test_start_acceptance_remains_pending_and_never_cancels_runtime(monkeypatch):
    calls=[]
    monkeypatch.setattr(display,'chrome_action',lambda action,*a,**kw:calls.append(action) or {'state':'starting','exit_code':0})
    result=screens.screen_action('start',Config(user='user',url='https://example.com/'))
    assert result['state']=='pending' and result['exit_code']==2 and calls==['start']


def test_readonly_inventory_never_places_or_starts(monkeypatch):
    calls=[]
    monkeypatch.setattr(display,'_user_call',lambda c,h,p,a:calls.append(a) or {'state':'ok','outputs':[],'logical_outputs':[]})
    monkeypatch.setattr(display,'chrome_action',lambda a,*args,**kw:calls.append(a) or {'state':'stopped','service':{'ActiveState':'inactive'}})
    result=screens.inspect_screens(fleet())
    assert len(result['targets'])==2 and all(t['actual_url'] is None for t in result['targets'])
    assert calls==['outputs','status','status']


def test_connected_hdmi_is_visible_when_gnome_cannot_confirm_placement(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    connector = paths.at('/sys/class/drm/card0-HDMI-A-1')
    connector.mkdir(parents=True)
    status = connector / 'status'
    status.write_text('connected\n')
    unchanged = status.stat().st_mtime_ns
    failure = {'state': 'pending', 'reason': 'GNOME 화면 확장에 연결되지 않았습니다.',
               'next_action': 'GNOME 재로그인을 확인하세요.'}
    calls = []
    monkeypatch.setattr(display, '_user_call', lambda c,h,p,a: calls.append(a) or failure)
    monkeypatch.setattr(display, 'chrome_action', lambda a,*args,**kw: calls.append(a) or {
        'state': 'stopped', 'service': {'ActiveState': 'inactive'}})
    report = screens.inspect_screens(fleet(), paths=paths)
    assert report['hardware_outputs'] == [
        {'id': 'card0-HDMI-A-1', 'name': 'HDMI-A-1', 'status': 'connected'}]
    assert report['hardware_output_state'] == 'ok'
    assert report['output_state'] == 'pending' and report['logical_outputs'] == []
    assert report['reason'] == failure['reason']
    assert report['output_next_action'] == failure['next_action']
    assert report['targets'][0]['placement']['state'] == 'pending'
    assert screens.screen_action('start', fleet(), paths=paths)['state'] == 'pending'
    assert calls == ['outputs', 'status', 'status', 'status', 'outputs']
    assert status.stat().st_mtime_ns == unchanged


def test_budget_caps_each_subprocess_to_remaining(monkeypatch):
    ticks=iter([10,12,14,16])
    monkeypatch.setattr(display.time,'monotonic',lambda:next(ticks))
    calls=[]
    class Host:
        def run(self,argv,**kw):calls.append(kw['timeout']);return Command(0)
    host=display.BudgetHost(Host(),5)
    assert host.run(['one'],timeout=30).returncode==0
    assert host.run(['two'],timeout=30).returncode==0
    assert host.run(['three'],timeout=30).returncode==124
    assert calls==[3,1]


def test_named_target_stop_does_not_touch_default_or_legacy(tmp_path):
    config=fleet().for_screen('right');calls=[]
    state={'ActiveState':'active','MainPID':'0','LoadState':'loaded','Result':'success'}
    class Host:
        def run(self,argv,**kwargs):
            calls.append(argv)
            if argv[2]=='show':return Command(0,'\n'.join(f'{k}={v}' for k,v in state.items()))
            if argv[2]=='stop':state['ActiveState']='inactive';return Command(0)
            raise AssertionError(argv)
    result=display.user_action('stop',target='right',config=config,home=tmp_path,host=Host(),paths=Paths(tmp_path))
    assert result['state']=='stopped'
    assert all(display.UNIT not in args and display.LEGACY not in args for args in calls)
    assert ['systemctl','--user','stop','ala-chrome@'+'right.service'] in calls


def test_multi_login_queues_each_target_once_and_retains_existing(tmp_path):
    config=fleet();calls=[]
    states={display.UNIT:'active',display.LEGACY:'inactive','ala-chrome@'+'right.service':'inactive'}
    class Host:
        def run(self,argv,**kwargs):
            calls.append(argv)
            if argv[2]=='show-environment':return Command(0,'WAYLAND_DISPLAY=wayland-0\n')
            if argv[2]=='show':return Command(0,'ActiveState='+states[argv[3]]+'\nResult=success\nLoadState=loaded\nMainPID=0\n')
            if argv[2]=='start':states[argv[-1]]='active';return Command(0)
            if argv[2]=='daemon-reload':return Command(0)
            raise AssertionError(argv)
    host=Host()
    assert display.session_start(config=config,home=tmp_path,host=host,paths=Paths(tmp_path),environment={})==0
    assert display.session_start(config=config,home=tmp_path,host=host,paths=Paths(tmp_path),environment={})==0
    assert [args for args in calls if args[2]=='start']==[['systemctl','--user','start','--no-block','ala-chrome@'+'right.service']]
    assert not any(args[2] in {'stop','restart','reset-failed'} for args in calls)


def test_runtime_pin_is_forwarded_as_data_to_target_user_rpc(tmp_path):
    paths=Paths(tmp_path);paths.at('/run/user/test').mkdir(parents=True)
    config=replace(fleet().for_screen('right'),runtime_pin={'config_path':'/var/lib/ala/runtime/configs/test.json'}).for_screen('right')
    calls=[]
    class Host:
        def run(self,argv,**kwargs):
            calls.append(argv)
            if argv[0]=='getent':return Command(0,f'user:x:{os.getuid()}:{os.getgid()}::/home/user:/bin/bash\n')
            if argv[0]=='loginctl':return Command(0,'/run/user/test\n')
            return Command(0,json.dumps({'exit_code':0,'state':'ok'}))
    display._user_call(config,Host(),paths,'status')
    command=calls[-1]
    assert 'ALA_RUNTIME_CONFIG=/var/lib/ala/runtime/configs/test.json' in command
    assert 'ALA_RUNTIME_TARGET=right' in command
    assert command[-3:]==['status','--target','right']


def test_extension_install_is_idempotent_and_external_symlink_refused(tmp_path):
    paths=Paths(tmp_path)
    display._install_extension(paths)
    files=list(paths.at('/usr/share/gnome-shell/extensions').rglob('*'))
    original={str(p):p.stat().st_mtime_ns for p in files if p.is_file()}
    display._install_extension(paths)
    assert original=={str(p):p.stat().st_mtime_ns for p in files if p.is_file()}
    target=paths.at('/usr/share/gnome-shell/extensions/ala-display@example.com/extension.js')
    target.unlink();target.symlink_to(tmp_path/'outside')
    with pytest.raises(ValueError):display._install_extension(paths)
    assert not (tmp_path/'outside').exists()


def test_extension_activation_preserves_other_extensions():
    values={('org.gnome.shell','disable-user-extensions'):'false',('org.gnome.shell','enabled-extensions'):"['external@example.com']"}
    calls=[]
    class Host:
        def run(self,argv,**kwargs):
            calls.append(argv)
            if argv[1]=='get':return Command(0,values[tuple(argv[2:])])
            assert argv[1]=='set'
            values[tuple(argv[2:4])]=argv[-1];return Command(0)
    host=Host()
    assert display._extension_activation(host)['state']=='change_required'
    assert not any(args[1]=='set' for args in calls)
    assert display._extension_activation(host,apply=True)['state']=='ok'
    assert 'external@example.com' in values[('org.gnome.shell','enabled-extensions')]
    count=len([a for a in calls if a[1]=='set'])
    assert display._extension_activation(host,apply=True)['state']=='ok'
    assert len([a for a in calls if a[1]=='set'])==count


def test_named_runtime_passes_target_revision_and_placement_to_own_supervisor(tmp_path,monkeypatch):
    from ala import monitors
    context=fleet().for_screen('right',revision='test-revision')
    captured={}
    monkeypatch.setattr(display,'_wait_for_graphical_ready',lambda *a,**kw:{'WAYLAND_DISPLAY':'wayland-0'})
    monkeypatch.setattr(monitors,'topology',lambda **kw:{'state':'ok','logical_outputs':[{'id':'DP-1','connectors':['DP-1'],
        'confirmed':True,'x':1920,'y':0,'width':1920,'height':1080}]})
    class Supervisor:
        def __init__(self,config,home,args,**kwargs):captured.update(config=config,home=home,args=args,options=kwargs)
        def run(self):return 0
    assert display.run_chrome(target='right',config=context,home=tmp_path,paths=Paths(tmp_path),
                              supervisor_factory=Supervisor)==0
    assert captured['options']['target_id']=='right' and captured['options']['config_revision']=='test-revision'
    assert callable(captured['options']['placement_callback'])
    assert captured['config'].frontend==context.frontend
    assert not (tmp_path/'.config/ala/chrome-display').exists()
    assert (tmp_path/'.config/ala/chrome-displays/right/.ala-display-profile').exists()


def test_named_runtime_without_output_does_not_create_browser_or_profile(tmp_path,monkeypatch):
    from ala import monitors
    monkeypatch.setattr(display,'_wait_for_graphical_ready',lambda *a,**kw:{'WAYLAND_DISPLAY':'wayland-0'})
    monkeypatch.setattr(monitors,'topology',lambda **kw:{'state':'ok','logical_outputs':[]})
    def forbidden(*a,**kw):pytest.fail('unresolved output must not spawn Chrome')
    assert display.run_chrome(target='right',config=fleet().for_screen('right'),home=tmp_path,paths=Paths(tmp_path),
                              supervisor_factory=forbidden)==78
    assert not list(tmp_path.iterdir())


def test_placement_saved_connector_prevents_primary_guess_after_hotplug(monkeypatch):
    from ala import monitors
    context=Config(schema_version=2,user='user',screens=[{'id':'default'}]).for_screen()
    health={'supervisor_pid':10,'browser_pid':20,'supervisor_start_ticks':'100','browser_start_ticks':'200',
            'boot_id':'test-boot','placement':{'output':'HDMI-1'}}
    inventory={'state':'ok','logical_outputs':[{'id':'DP-1','connectors':['DP-1'],'confirmed':True,'x':0,'y':0,'width':1920,'height':1080}]}
    monkeypatch.setattr(monitors,'bridge',lambda *a,**kw:pytest.fail('disconnected pinned connector must not move another window'))
    assert monitors.placement(context,health,inventory=inventory,apply=True)['state']=='pending'


def test_named_health_record_and_supervisor_suffix_are_isolated(tmp_path):
    from ala import display_health as health
    import time
    paths=Paths(tmp_path);home=tmp_path/'home';home.mkdir()
    config=fleet().for_screen('right')
    for pid,args,ppid in [(321,['/opt/ala/current/.venv/bin/python','-I','/opt/ala/current/entry.py','_chrome-run','--target','right'],1),
                          (322,display.chrome_arguments(config,home,{'WAYLAND_DISPLAY':'wayland-0'},supervised=True),321)]:
        process=paths.at('/proc')/str(pid);process.mkdir(parents=True)
        (process/'status').write_text(f'Uid:\t{os.getuid()}\nPPid:\t{ppid}\n')
        (process/'stat').write_text(f'{pid} (chrome process) S '+'0 '*18+'123')
        (process/'cmdline').write_bytes(('\0'.join(args)+'\0').encode())
    boot=paths.at('/proc/sys/kernel/random/boot_id');boot.parent.mkdir(parents=True);boot.write_text('test-boot')
    record={'state':'healthy','supervisor_pid':321,'browser_pid':322,'supervisor_start_ticks':'123',
            'browser_start_ticks':'123','boot_id':'test-boot','updated_at':health.stamp(time.time()),
            'config_fingerprint':health.fingerprint(config.url),'target_id':'right','config_revision':'test-revision',
            'target_matches_config':True}
    path=health.state_path(home,'right');path.parent.mkdir(parents=True);path.write_text(json.dumps(record));path.chmod(0o600)
    calls=[]
    class Host:
        def run(self,argv,**kwargs):
            calls.append(argv)
            if argv[2]=='show-environment':return Command(0,'WAYLAND_DISPLAY=wayland-0\n')
            assert argv[2]=='show' and argv[3]=='ala-chrome@'+'right.service'
            return Command(0,'ActiveState=active\nMainPID=321\nLoadState=loaded\nResult=success\n')
    result=display.user_action('check',target='right',config=config,home=home,host=Host(),paths=paths)
    assert result['state']=='running' and result['health']['target_id']=='right'
    assert not any(display.UNIT in args or display.LEGACY in args for args in calls)
    record['target_id']='default';path.write_text(json.dumps(record))
    assert display.user_action('check',target='right',config=config,home=home,host=Host(),paths=paths)['exit_code']==1


@pytest.mark.parametrize('target',['default','right'])
@pytest.mark.parametrize('after',['alive','gone','window_remains','bridge_unknown','different_uid','reused'])
def test_stop_requires_owned_browser_and_observed_window_to_disappear(tmp_path,monkeypatch,target,after):
    from ala import monitors
    context=fleet().for_screen(target);paths=Paths(tmp_path)
    browser=paths.at('/proc/20');browser.mkdir(parents=True)
    (browser/'status').write_text(f'Uid:\t{os.getuid()}\n')
    (browser/'stat').write_text('20 (chrome) S '+'0 '*18+'200')
    unit=display.unit_name(target);state={'ActiveState':'active','MainPID':'10','LoadState':'loaded','Result':'success'}
    health={'state':'healthy','supervisor_pid':10,'browser_pid':20,'supervisor_start_ticks':'100',
            'browser_start_ticks':'200','boot_id':'test-boot'}
    monkeypatch.setattr(display,'_check_user',lambda *a:{'process_verified':True,'state':'running','health':health})
    bridge_calls=[]
    def bridge(method,request=None,**kwargs):
        bridge_calls.append(method)
        if method=='Inspect':return {'state':'ok','windows':[{'id':123}]}
        if after=='bridge_unknown':return {'state':'unknown'}
        return {'state':'ok','remaining_window_count':1 if after=='window_remains' else 0}
    monkeypatch.setattr(monitors,'bridge',bridge)
    class Host:
        def run(self,argv,**kwargs):
            assert argv[3]==unit
            if argv[2]=='show':return Command(0,'\n'.join(f'{k}={v}' for k,v in state.items()))
            assert argv[2]=='stop';state['ActiveState']='inactive'
            if after in {'gone','window_remains','bridge_unknown'}:
                (browser/'status').unlink();(browser/'stat').unlink();browser.rmdir()
            elif after=='different_uid':(browser/'status').write_text(f'Uid:\t{os.getuid()+1}\n')
            elif after=='reused':(browser/'stat').write_text('20 (other) S '+'0 '*18+'201')
            return Command(0)
    value=display.user_action('stop',config=context,home=tmp_path,host=Host(),paths=paths)
    assert value['state']=={'alive':'pending','gone':'stopped','window_remains':'pending','bridge_unknown':'unknown',
                            'different_uid':'unknown','reused':'stopped'}[after]
    assert value['exit_code']==(0 if after in {'gone','reused'} else 2 if after in {'alive','window_remains'} else 1)
    assert bridge_calls==(['Inspect','Closed'] if after in {'gone','window_remains','bridge_unknown','reused'} else ['Inspect'])


def test_stop_does_not_claim_completion_from_unverified_ownership(tmp_path,monkeypatch):
    monkeypatch.setattr(display,'_check_user',lambda *a:{'state':'unknown'})
    state={'ActiveState':'active','MainPID':'10','LoadState':'loaded','Result':'success'}
    class Host:
        def run(self,argv,**kwargs):
            if argv[2]=='show':return Command(0,'\n'.join(f'{k}={v}' for k,v in state.items()))
            assert argv[2]=='stop';state['ActiveState']='inactive';return Command(0)
    result=display.user_action('stop',config=Config(user='user'),home=tmp_path,host=Host(),paths=Paths(tmp_path))
    assert result['state']=='unknown' and result['exit_code']==1 and result['service_stopped']


def test_frontend_timer_registration_and_reapply_are_idempotent(tmp_path):
    from ala.frontend import FrontendConfig
    paths=Paths(tmp_path);calls=[];timer={'enabled':False,'active':False}
    config=Config(schema_version=2,user='user',screens=(ScreenConfig(frontend=FrontendConfig(True,'https://example.com/build.json')),))
    class Host:
        def run(self,argv,**kwargs):
            calls.append(argv)
            if argv[1]=='daemon-reload':return Command(0)
            assert argv[-1]=='ala-frontend.timer'
            if argv[1]=='is-enabled':return Command(0 if timer['enabled'] else 1,'enabled' if timer['enabled'] else 'disabled')
            if argv[1]=='is-active':return Command(0 if timer['active'] else 3,'active' if timer['active'] else 'inactive')
            assert argv[1] in {'enable','disable'}
            timer.update(enabled=argv[1]=='enable',active=argv[1]=='enable');return Command(0)
    host=Host()
    assert display._frontend_schedule(config,host,paths,apply=True).state=='ok'
    files=list(paths.at('/etc/systemd/system').iterdir());before={p:p.stat().st_mtime_ns for p in files}
    assert display._frontend_schedule(config,host,paths,apply=True).state=='ok'
    assert before=={p:p.stat().st_mtime_ns for p in files}
    assert len([a for a in calls if a[1]=='enable'])==1
    assert display._frontend_schedule(Config(user='user'),host,paths,apply=True).state=='ok'
    assert len([a for a in calls if a[1]=='disable'])==1
    assert not any(a[1]=='stop' or 'ala-frontend.service' in a for a in calls)


def test_disabled_frontend_does_not_register_or_disable_an_external_timer(tmp_path):
    paths=Paths(tmp_path);path=paths.at('/etc/systemd/system/ala-frontend.timer');path.parent.mkdir(parents=True)
    path.write_text('[Timer]\nOnBootSec=1\n')
    class Host:
        def run(self,*a,**kw):pytest.fail('external timer must not be modified')
    assert display._frontend_schedule(Config(user='user'),Host(),paths,apply=True).state=='blocked'


def test_repeat_stop_cannot_hide_a_surviving_profile_process(tmp_path,monkeypatch):
    monkeypatch.setattr(display,'_foreign_profile_pid',lambda *a,**kw:20)
    state={'ActiveState':'inactive','MainPID':'0','LoadState':'loaded','Result':'success'}
    class Host:
        def run(self,argv,**kwargs):
            assert argv[2]=='show';return Command(0,'\n'.join(f'{k}={v}' for k,v in state.items()))
    result=display.user_action('stop',config=Config(user='user'),home=tmp_path,host=Host(),paths=Paths(tmp_path))
    assert result['state']=='unknown' and result['exit_code']==1


def test_repeat_stop_uses_previous_confirmed_window_ids_without_reviving_a_process(tmp_path,monkeypatch):
    from ala import monitors,display_health as health
    paths=Paths(tmp_path);record=health.state_path(tmp_path)
    record.parent.mkdir(parents=True);record.write_text(json.dumps({'boot_id':'test-boot','target_id':'default',
                'placement':{'windows':[{'id':123}]}}));record.chmod(0o600)
    boot=paths.at('/proc/sys/kernel/random/boot_id');boot.parent.mkdir(parents=True);boot.write_text('test-boot')
    monkeypatch.setattr(monitors,'bridge',lambda method,*args,**kwargs:{'state':'ok','remaining_window_count':1})
    state={'ActiveState':'inactive','MainPID':'0','LoadState':'loaded','Result':'success'}
    class Host:
        def run(self,argv,**kwargs):
            assert argv[2]=='show';return Command(0,'\n'.join(f'{k}={v}' for k,v in state.items()))
    result=display.user_action('stop',config=Config(user='user'),home=tmp_path,host=Host(),paths=paths)
    assert result['state']=='pending' and result['exit_code']==2


class PreflightHost:
    def __init__(self,count=1,active=True):self.count=count;self.active=active;self.calls=[]
    def run(self,argv,**kwargs):
        self.calls.append(argv)
        if argv[:2]==['getent','passwd']:return Command(0,f'user:x:{os.getuid()}:{os.getgid()}::/home/user:/bin/bash\n')
        if argv[:2]==['loginctl','list-sessions']:return Command(0,''.join(f'{i+1} user\n' for i in range(self.count)))
        if argv[:2]==['loginctl','show-session']:
            return Command(0,f'User={os.getuid()}\nType=wayland\nClass=user\nRemote=no\nActive={"yes" if self.active else "no"}\nState=active\nLockedHint=no\n')
        raise AssertionError('preflight must not mutate or launch a command: '+repr(argv))


def preflight_unit(paths,target='default'):
    path=paths.at('/etc/systemd/user/'+display.unit_name(target));path.parent.mkdir(parents=True)
    path.write_text(display.unit_text(target));path.chmod(0o644)
    return path


def preflight_inventory():
    return {'state':'ok','logical_outputs':[
        {'id':c,'connectors':[c],'confirmed':True,'x':i*1920,'y':0,'width':1920,'height':1080}
        for i,c in enumerate(('HDMI-1','DP-1'))]}


def test_candidate_preflight_is_readonly_and_does_not_load_candidate_as_current(tmp_path,monkeypatch):
    config=replace(fleet(),runtime_pin={'config_path':'/var/lib/ala-runtime/configs/candidate.json'}).for_screen('right')
    paths=Paths(tmp_path);preflight_unit(paths,'right');calls=[]
    before={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in tmp_path.rglob('*') if p.is_file()}
    def observed(context,host,paths,action):
        calls.append(action)
        assert context.runtime_pin is None
        if action=='environment':
            assert context.target_id=='right'
        if action=='environment':return {'state':'ready','exit_code':0}
        assert action=='outputs';return preflight_inventory()
    monkeypatch.setattr(display,'_user_call',observed)
    monkeypatch.setattr(display,'_check_user',lambda *a:pytest.fail('site/PID drift is not a startup precondition'))
    result=screens.screen_action('preflight',config,target='right',host=PreflightHost(),paths=paths)
    assert result['state']=='ready' and result['exit_code']==0 and calls==['environment','outputs']
    assert before=={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in tmp_path.rglob('*') if p.is_file()}


@pytest.mark.parametrize('count,active',[(0,True),(2,True),(1,False)])
def test_no_single_active_session_preflight_preserves_current_display(tmp_path,monkeypatch,count,active):
    monkeypatch.setattr(display,'_user_call',lambda *a:pytest.fail('invalid graphical session must be rejected before user RPC'))
    host=PreflightHost(count,active)
    result=screens.screen_action('preflight',fleet(),host=host,paths=Paths(tmp_path))
    assert result['exit_code']!=0 and result['state'] in {'pending','blocked'}
    assert not any('stop' in args or 'start' in args or 'Place' in args for args in host.calls)


@pytest.mark.parametrize('registration',['missing','legacy','external','symlink'])
def test_unprepared_or_foreign_unit_preflight_never_stops_current_display(tmp_path,monkeypatch,registration):
    paths=Paths(tmp_path)
    if registration!='missing':
        path=preflight_unit(paths)
        if registration=='legacy':path.write_text(display.unit_text().replace('_runtime-dispatch','_chrome-run'))
        elif registration=='external':path.write_text('[Service]\nExecStart=/unmanaged\n')
        else:path.unlink();path.symlink_to(tmp_path/'outside')
    monkeypatch.setattr(display,'_user_call',lambda *a:pytest.fail('bad registration must be rejected before user RPC'))
    result=screens.screen_action('preflight',fleet(),host=PreflightHost(),paths=paths)
    assert result['exit_code']!=0 and result['state'] in {'pending','blocked','unknown'}


@pytest.mark.parametrize('problem',['manager','output_missing','extension_missing','unsupported','mirror_overlap'])
def test_nonready_environment_or_output_preflight_never_requests_lifecycle(tmp_path,monkeypatch,problem):
    paths=Paths(tmp_path);preflight_unit(paths)
    calls=[]
    def observed(context,host,paths,action):
        calls.append(action)
        if action=='environment':
            return {'state':'pending','exit_code':2} if problem=='manager' else {'state':'ready','exit_code':0}
        assert action=='outputs'
        if problem=='extension_missing':return {'state':'pending','reason':'확장 연결 대기'}
        if problem=='unsupported':return {'state':'pending','reason':'지원 GNOME 버전 아님'}
        value=preflight_inventory()
        if problem=='output_missing':value['logical_outputs']=[]
        if problem=='mirror_overlap':
            value['logical_outputs']=[{'id':'DP-1+HDMI-1','connectors':['HDMI-1','DP-1'],
                'confirmed':True,'x':0,'y':0,'width':1920,'height':1080}]
        return value
    monkeypatch.setattr(display,'_user_call',observed)
    result=screens.screen_action('preflight',fleet(),host=PreflightHost(),paths=paths)
    assert result['exit_code']!=0
    assert set(calls)<={'environment','outputs'}


@pytest.mark.parametrize('property_change,expected',[
    ({},'ready'),({'FragmentPath':'/home/user/.config/systemd/user/ala-chrome@'+'right.service'},'blocked'),
    ({'NeedDaemonReload':'yes'},'pending'),({'LoadState':'not-found'},'pending')])
def test_user_preflight_observes_exact_loaded_unit_without_changing_manager(tmp_path,property_change,expected):
    calls=[]
    class Host:
        def run(self,argv,**kwargs):
            calls.append(argv)
            if argv[2]=='show-environment':return Command(0,'WAYLAND_DISPLAY=wayland-0\n')
            assert argv[2]=='show' and argv[3]=='ala-chrome@'+'right.service'
            values={'LoadState':'loaded','FragmentPath':'/etc/systemd/user/ala-chrome@'+'right.service','NeedDaemonReload':'no'}|property_change
            return Command(0,'\n'.join(f'{key}={value}' for key,value in values.items()))
    result=display.user_action('environment',target='right',config=fleet(),home=tmp_path,host=Host(),paths=Paths(tmp_path))
    assert result['state']==expected
    assert len(calls)==2 and not any('start' in args or 'stop' in args or 'daemon-reload' in args for args in calls)


def test_nonfirst_candidate_pin_is_not_forwarded_to_session_inventory_rpc(monkeypatch):
    pin={'config_path':'/var/lib/ala-runtime/configs/second-candidate.json','revision':'a'*64,'version':'0.1.0rc21'}
    config=replace(fleet(),runtime_pin=pin).for_screen('right',revision=pin['revision'])
    calls=[]
    def observed(context,host,paths,action):
        calls.append((context.target_id,context.runtime_pin,action))
        assert context.runtime_pin is None
        return preflight_inventory()
    monkeypatch.setattr(display,'_user_call',observed)
    result=screens._inventory(config,object(),Paths())
    assert result['state']=='ok' and calls==[('default',None,'outputs')]
    assert config.target_id=='right' and config.runtime_pin==pin
