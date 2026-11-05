from dataclasses import replace
from pathlib import Path
import json
import os
import time
import urllib.error
import pytest

from ala.config import Config
from ala.system import Command, Paths
from ala.model import Report
from ala import display


class FakeHost:
    def __init__(self, paths, config):
        self.paths, self.config = paths, config
        self.calls=[]
        self.sessions=1
        self.runtime=True
        self.manager='WAYLAND_DISPLAY=wayland-0\nXDG_CURRENT_DESKTOP=GNOME\n'
        self.states={display.UNIT: {'ActiveState':'inactive','MainPID':'0','Result':'success','LoadState':'loaded'},
                     display.LEGACY: {'ActiveState':'inactive','MainPID':'0','Result':'success','LoadState':'not-found'}}
        self.settings={schema+'.'+key:expected for schema,key,expected in display.SETTINGS}
        self.settings.update({'org.gnome.shell.disable-user-extensions':'false','org.gnome.shell.enabled-extensions':"['ala-display@example.com']"})
        self.script_result={'exit_code':2, 'state':'pending', 'reason':'Chrome 미실행'}
        self.get_default='graphical.target'
        self.gdm_enabled='enabled'
        self.locked='no'
        self.gdm_status=Command(0,'LoadState=loaded\nActiveState=active\nSubState=running\nResult=success\n')
        self.gdm_logs=Command(0,'')
        self.real_user_check=False
        self.process_on_start=None
        self.show_failures={}
        self.overview_available=False
    def run(self, argv, **kwargs):
        self.calls.append((argv,kwargs))
        if argv[:2]==['getent','passwd']:
            return Command(0,f'user:x:{os.getuid()}:{os.getgid()}::/home/user:/bin/bash\n')
        if argv[:2]==['loginctl','list-sessions']:
            return Command(0,''.join(f'{i+1} {os.getuid()} user seat0\n' for i in range(self.sessions)))
        if argv[:2]==['loginctl','show-session']:
            return Command(0,f'User={os.getuid()}\nType=wayland\nClass=user\nRemote=no\nActive=yes\nState=active\nLockedHint={self.locked}\n')
        if argv[:2]==['loginctl','show-user']:
            return Command(0,'/run/user/test\n') if self.runtime else Command(1,'','no runtime')
        if argv[0]=='runuser':
            if argv[-1]=='prepare':
                display._prepare_user_files(self.config,self.paths.at('/home/user'),self.paths)
                return Command(0,json.dumps({'exit_code':0,'state':'ok'}))
            if argv[-1]=='settings':
                return Command(0,json.dumps({'exit_code':0,'settings':self.settings}))
            if argv[-1]=='ensure-start' or (argv[-1]=='check' and self.real_user_check):
                result=display.user_action(argv[-1],config=self.config,home=self.paths.at('/home/user'),host=self,paths=self.paths)
                return Command(result.get('exit_code',0),json.dumps(result))
            return Command(0,json.dumps(self.script_result))
        if argv==['systemctl','is-enabled','gdm.service']:
            return Command(0,self.gdm_enabled+'\n')
        if argv[:3]==['systemctl','show','gdm.service']:
            return self.gdm_status
        if argv==['systemctl','get-default']:
            return Command(0,self.get_default+'\n')
        if argv[:3]==['systemctl','--user','show-environment']:
            return Command(0,self.manager)
        if argv[:3]==['systemctl','--user','show']:
            if argv[3] in self.show_failures:
                return self.show_failures[argv[3]]
            return Command(0,'\n'.join(f'{key}={value}' for key,value in self.states[argv[3]].items()))
        if argv[:3]==['systemctl','--user','stop']:
            self.states[argv[-1]]['ActiveState']='inactive'
            return Command(0)
        if argv[:3]==['systemctl','--user','start']:
            self.states[display.UNIT]['ActiveState']='active'
            if self.process_on_start:
                self.process_on_start()
            return Command(0)
        if argv[:2]==['gsettings','get']:
            name=argv[2]+'.'+argv[3]
            return Command(0,self.settings[name]+'\n')
        if argv[0]=='journalctl' and 'gdm.service' in argv:
            return self.gdm_logs
        if argv[0]=='journalctl':
            return Command(0,'only ala logs\n')
        if argv[:2]==['systemctl','--user']:
            return Command(0)
        if argv[:2]==['gdbus','wait']:
            return Command(0 if self.overview_available else 1)
        if argv[-1:] == ['org.ala.Display1.DismissStartupOverview']:
            return Command(0, repr((json.dumps({'protocol': 1, 'state': 'ok', 'overview': 'scheduled'}),)))
        raise AssertionError(f'Unexpected command: {argv}')
    def execute(self, argv, **kwargs):
        self.calls.append((argv,kwargs))
        if argv==['systemctl','enable','gdm.service']:
            self.gdm_enabled='enabled'
        elif argv==['systemctl','set-default','graphical.target']:
            self.get_default='graphical.target'
        else:
            raise AssertionError(f'Unexpected mutation: {argv}')
        return 0


@pytest.fixture
def setup(tmp_path):
    paths=Paths(tmp_path)
    paths.at('/etc/gdm').mkdir(parents=True)
    paths.at('/home/user').mkdir(parents=True)
    paths.at('/run/user/test').mkdir(parents=True)
    config=Config(user='user',url='https://example.com/?x=$literal&y=${literal}')
    return config,paths,FakeHost(paths,config)


def user_fixture(setup):
    config,paths,host=setup
    return config,paths,host,paths.at('/home/user')


def _registered_default(config, paths, host):
    from ala.runtime import RuntimeStore
    release = paths.at('/opt/ala/releases/0.1.0rc21')
    (release / '.venv/bin').mkdir(parents=True)
    (release / '.venv/bin/python').touch()
    (release / 'entry.py').touch()
    (release / 'VERSION').write_text('0.1.0rc21\n')
    (release / 'COMPATIBILITY.json').write_text(json.dumps({
        'schema_version': 1, 'runtime_protocol': 1, 'config_schemas': [1, 2],
    }))
    boot = paths.at('/proc/sys/kernel/random/boot_id')
    boot.parent.mkdir(parents=True)
    boot.write_text('test-boot\n')
    store = RuntimeStore(paths, host)
    store.bootstrap('default', release, config)
    return store


class ReadinessClock:
    def __init__(self):self.value=0.0
    def __call__(self):return self.value
    def sleep(self,seconds):self.value+=seconds


def _ready_run(config,paths,host,home,clock,*,sleeper=None,stop_requested=None):
    launches=[]
    class Supervisor:
        def __init__(self,actual_config,actual_home,argv,**kwargs):
            launches.append((actual_config,actual_home,argv))
        def run(self):return 0
    result=display.run_chrome(config=config,home=home,host=host,paths=paths,clock=clock,
                              sleeper=sleeper or clock.sleep,stop_requested=stop_requested,
                              supervisor_factory=Supervisor)
    return result,launches


def test_gdm_preserves_unrelated_lines_and_is_idempotent():
    config=Config(user='user')
    original='# keep\n[daemon]\nWaylandEnable=true\nAutomaticLoginEnable=False\n\n[security]\nDisallowTCP=true\n'
    candidate=display.gdm_candidate(original,config)
    assert 'WaylandEnable=true' in candidate
    assert '[security]\nDisallowTCP=true' in candidate
    assert candidate.count('AutomaticLoginEnable=')==1
    assert 'AutomaticLogin=user' in candidate
    assert display.gdm_candidate(candidate,config)==candidate
    assert 'AutomaticLoginEnable=False' in display.gdm_candidate(candidate,replace(config,autologin=False))
    assert display.gdm_candidate(original,replace(config,autologin=False))==original


@pytest.mark.parametrize('content',['[daemon]\n[daemon]\n', '[daemon]\nAutomaticLogin=a\nAutomaticLogin=b\n'])
def test_ambiguous_gdm_fails_without_guessing(content):
    with pytest.raises(ValueError):
        display.gdm_candidate(content,Config(user='user'))


def test_inspection_is_readonly_and_marks_missing_files(setup):
    config,paths,host=setup
    before=set(paths.root.rglob('*'))
    result=display.inspect_display(config,host,paths)
    assert any(r.state=='change_required' for r in result)
    assert set(paths.root.rglob('*'))==before
    assert all(argv[0] not in {'bash','dnf'} for argv,_ in host.calls)


def test_apply_uses_target_user_files_and_preserves_existing_profiles(setup):
    config,paths,host=setup
    old=paths.at('/home/user/.config/chrome-did/marker')
    old.parent.mkdir(parents=True);old.write_text('keep-login-data')
    gdm=paths.at('/etc/gdm/custom.conf')
    gdm.write_text('[daemon]\nWaylandEnable=true\n')
    display.apply_display(config,host,paths)
    assert old.read_text()=='keep-login-data'
    assert 'AutomaticLogin=user' in gdm.read_text()
    assert 'WaylandEnable=true' in gdm.read_text()
    desktop=paths.at('/home/user/.config/autostart/ala-display.desktop')
    assert desktop.read_text()==display.desktop_text()
    assert any(argv[0]=='runuser' and argv[-1]=='prepare' for argv,_ in host.calls)
    assert all('-i' in argv for argv,_ in host.calls if argv[0]=='runuser')
    assert not any('restart' in argv or 'reboot' in argv for argv,_ in host.calls)
    files=[gdm,desktop,paths.at('/etc/systemd/user/ala-chrome.service')]
    before=[(p.read_bytes(),p.stat().st_mtime_ns) for p in files]
    backups=list(paths.backups.rglob('*'))
    display.apply_display(config,host,paths)
    assert [(p.read_bytes(),p.stat().st_mtime_ns) for p in files]==before
    assert list(paths.backups.rglob('*'))==backups


def test_prepare_can_run_before_first_graphical_login(setup):
    config,paths,host=setup
    host.runtime=False;host.sessions=0
    result=display.apply_display(config,host,paths)
    assert paths.at('/home/user/.config/autostart/ala-display.desktop').is_file()
    assert Report(result).exit_code==0
    assert not any(r.id in {'display.session','chrome.runtime','chrome.health'} for r in result)


def test_autostart_off_removes_only_owned_registration(setup):
    config,paths,host=setup
    display.apply_display(config,host,paths)
    off=replace(config,autostart=False)
    host.config=off
    display.apply_display(off,host,paths)
    assert not paths.at('/home/user/.config/autostart/ala-display.desktop').exists()
    assert paths.at('/etc/systemd/user/ala-chrome.service').exists()


def test_unmanaged_autostart_and_symlinks_not_overwritten(setup):
    config,paths,host=setup
    desktop=paths.at('/home/user/.config/autostart/ala-display.desktop')
    desktop.parent.mkdir(parents=True);desktop.write_text('unmanaged')
    result=display.apply_display(config,host,paths)
    assert any(r.state=='blocked' for r in result)
    assert desktop.read_text()=='unmanaged'
    gdm=paths.at('/etc/gdm/custom.conf')
    target=paths.root/'other';target.write_text('do not edit')
    gdm.unlink(missing_ok=True);gdm.symlink_to(target)
    result=display.apply_display(config,host,paths)
    assert any(r.state=='blocked' for r in result)
    assert target.read_text()=='do not edit'


def test_live_setting_differences_are_pending(setup):
    config,paths,host=setup
    host.settings['org.gnome.desktop.session.idle-delay']='uint32 300'
    result=display.inspect_display(config,host,paths)
    assert any(r.id=='display.live.idle-delay' and r.state=='pending' for r in result)


def test_duplicate_sessions_block_actions_and_no_runtime_is_pending(setup):
    config,paths,host=setup
    host.sessions=2
    result=display.chrome_action('start',config,host,paths)
    assert result['state']=='blocked'
    assert not any(argv[0]=='runuser' for argv,_ in host.calls)
    host.sessions=1;host.runtime=False
    assert display.chrome_action('start',config,host,paths)['exit_code']==2


def test_missing_url_allows_explicit_start_and_selects_internal_setup_mode(setup):
    config,paths,host=setup
    empty=replace(config,url=None)
    host.config=empty
    host.script_result={'exit_code':0,'state':'starting'}
    assert display.chrome_action('start',empty,host,paths)['state']=='starting'
    assert any(argv[0]=='runuser' and argv[-1]=='start' for argv,_ in host.calls)
    check=next(r for r in display.inspect_display(empty,host,paths) if r.id=='chrome.url')
    assert check.state=='ok' and check.actual=='설정 안내 모드'


def test_unit_uses_graphical_session_and_bounded_crash_recovery():
    unit=display.unit_text()
    assert 'PartOf=graphical-session.target' in unit
    assert 'Restart=always' in unit
    assert 'RestartSec=5' in unit
    assert 'SuccessExitStatus=78' in unit
    assert 'RestartPreventExitStatus=78' in unit
    assert 'StartLimitIntervalSec=300' in unit
    assert 'StartLimitBurst=5' in unit
    assert '_runtime-dispatch' in unit


def test_user_start_does_not_replace_legacy_or_active_chrome(setup):
    config,paths,host,home=user_fixture(setup)
    host.states[display.LEGACY]['ActiveState']='active'
    result=display.user_action('start',config=config,home=home,host=host,paths=paths)
    assert result['exit_code']==2
    assert not any(argv[:3]==['systemctl','--user','stop'] for argv,_ in host.calls)
    host.states[display.LEGACY]['ActiveState']='inactive'
    host.states[display.UNIT]['ActiveState']='active'
    assert display.user_action('start',config=config,home=home,host=host,paths=paths)['state']=='kept'


def test_restart_stops_only_managed_units_and_resets_limit(setup):
    config,paths,host,home=user_fixture(setup)
    host.states[display.LEGACY]['ActiveState']='active'
    host.states[display.UNIT]['ActiveState']='failed'
    result=display.user_action('restart',config=config,home=home,host=host,paths=paths)
    assert result['exit_code']==0
    commands=[argv for argv,_ in host.calls]
    assert ['systemctl','--user','stop',display.LEGACY] in commands
    assert ['systemctl','--user','stop',display.UNIT] in commands
    assert ['systemctl','--user','reset-failed',display.UNIT] in commands
    assert commands.index(['systemctl','--user','stop',display.LEGACY]) < commands.index(['systemctl','--user','start','--no-block',display.UNIT])
    assert all(argv[0]!='pkill' for argv in commands)


def test_stop_does_not_disable_future_autostart(setup):
    config,paths,host,home=user_fixture(setup)
    host.states[display.UNIT]['ActiveState']='active'
    assert display.user_action('stop',config=config,home=home,host=host,paths=paths)['state']=='stopped'
    assert not any('disable' in argv for argv,_ in host.calls)


@pytest.mark.parametrize('autostart',[False,True])
def test_stop_describes_next_login_only_when_autostart_is_configured(setup,autostart):
    config,paths,host,home=user_fixture(setup)
    config=replace(config,autostart=autostart)
    host.states[display.UNIT]['ActiveState']='active'
    value=display.user_action('stop',config=config,home=home,host=host,paths=paths)
    assert value['state']=='stopped'
    assert ('다음 그래픽 로그인' in value['reason']) is autostart


@pytest.mark.parametrize('action,unit',[('stop',display.UNIT),('start',display.UNIT),('start',display.LEGACY),
                                      ('restart',display.UNIT),('restart',display.LEGACY)])
@pytest.mark.parametrize('response',[Command(124,'','synthetic timeout'),Command(0),
                                     Command(1,'ActiveState=active\nMainPID=321\n','synthetic denied'),
                                     Command(0,'ActiveState=unexpected\n')])
def test_mutation_requires_confirmed_service_state_before_any_lifecycle_command(setup,action,unit,response):
    config,paths,host,home=user_fixture(setup)
    host.states[display.UNIT]['ActiveState']='active'
    host.show_failures[unit]=response
    value=display.user_action(action,config=config,home=home,host=host,paths=paths)
    assert value['state']=='unknown' and value['exit_code']==1
    assert not any(argv[:2]==['systemctl','--user'] and argv[2] in {'start','stop','reset-failed','daemon-reload'}
                   for argv,_ in host.calls)


@pytest.mark.parametrize('action,unit',[('stop',display.UNIT),('restart',display.UNIT),('restart',display.LEGACY)])
@pytest.mark.parametrize('response',[Command(124,'','synthetic timeout'),
                                     Command(1,'ActiveState=active\nMainPID=321\n','synthetic denied')])
def test_stop_and_restart_do_not_infer_termination_from_failed_post_query(setup,action,unit,response):
    config,paths,host,home=user_fixture(setup)
    host.states[unit]['ActiveState']='active'
    original=host.run
    stopped=False
    def run(argv,**kwargs):
        nonlocal stopped
        if argv[:3]==['systemctl','--user','stop']:
            result=original(argv,**kwargs)
            stopped=True
            return result
        if stopped and argv[:4]==['systemctl','--user','show',unit]:
            return response
        return original(argv,**kwargs)
    host.run=run
    value=display.user_action(action,config=config,home=home,host=host,paths=paths)
    assert value['state']=='unknown' and value['exit_code']==1
    assert stopped
    assert not _started_commands(host)
    assert not any('reset-failed' in argv for argv,_ in host.calls)


def test_stop_does_not_claim_success_while_service_is_still_active(setup):
    config,paths,host,home=user_fixture(setup)
    host.states[display.UNIT]['ActiveState']='active'
    original=host.run
    def run(argv,**kwargs):
        if argv[:3]==['systemctl','--user','stop']:
            host.calls.append((argv,kwargs));return Command(0)
        return original(argv,**kwargs)
    host.run=run
    value=display.user_action('stop',config=config,home=home,host=host,paths=paths)
    assert value['state']=='blocked' and value['exit_code']==1


def test_foreign_profile_process_blocks_without_removing_lock(setup):
    config,paths,host,home=user_fixture(setup)
    process=paths.at('/proc/999');process.mkdir(parents=True)
    (process/'status').write_text(f'Uid:\t{os.getuid()}\t{os.getuid()}\n')
    (process/'cmdline').write_bytes(('chrome\0--user-data-dir='+str(display._profile(home))+'\0').encode())
    profile=display._profile(home);profile.mkdir(parents=True)
    lock=profile/'SingletonLock';lock.symlink_to('host-999')
    result=display.user_action('start',config=config,home=home,host=host,paths=paths)
    assert result['state']=='blocked'
    assert lock.is_symlink()


def test_actual_session_environment_import_is_allowlisted(setup):
    config,paths,host,home=user_fixture(setup)
    environment={'WAYLAND_DISPLAY':'wayland-0','XDG_CURRENT_DESKTOP':'GNOME','SECRET':'never-import'}
    assert display.session_start(config=config,home=home,host=host,paths=paths,environment=environment)==0
    imported=[(argv,kwargs) for argv,kwargs in host.calls if 'import-environment' in argv][0]
    assert 'SECRET' not in imported[0]
    assert 'WAYLAND_DISPLAY' in imported[0]
    assert 'DISPLAY' not in imported[0]
    assert imported[1]['env']['WAYLAND_DISPLAY']=='wayland-0'
    assert _started_commands(host)==[['systemctl','--user','start','--no-block',display.UNIT]]


@pytest.mark.parametrize('extension_ready', [True, False])
def test_login_requests_overview_dismissal_after_queuing_chrome(setup, capsys, extension_ready):
    config, paths, host, home = user_fixture(setup)
    host.overview_available = extension_ready
    assert display.session_start(config=config, home=home, host=host, paths=paths, environment={}) == 0
    commands = [argv for argv, _ in host.calls]
    start = commands.index(['systemctl','--user','start','--no-block',display.UNIT])
    wait = commands.index(['gdbus','wait','--session','--timeout','5','org.ala.Display1'])
    assert start < wait
    calls = [argv for argv in commands if argv[-1:] == ['org.ala.Display1.DismissStartupOverview']]
    assert len(calls) == int(extension_ready)
    if extension_ready:
        assert commands.index(calls[0]) > wait
    outcome = 'scheduled' if extension_ready else 'unavailable'
    assert f'phase=overview outcome={outcome}' in capsys.readouterr().out


def test_desktop_only_login_does_not_request_overview_dismissal(setup):
    config, paths, host, home = user_fixture(setup)
    assert display.session_start(config=replace(config, autostart=False), home=home, host=host,
                                 paths=paths, environment={}) == 0
    assert not host.calls


@pytest.mark.parametrize(('session_count', 'locked'), [(0, 'no'), (2, 'no'), (1, 'yes')])
def test_login_skips_overview_for_unproven_desktop(setup, session_count, locked):
    config, paths, host, home = user_fixture(setup)
    host.sessions = session_count
    host.locked = locked
    assert display.session_start(config=config, home=home, host=host, paths=paths, environment={}) == 0
    assert _started_commands(host)
    assert not any(argv[0] == 'gdbus' for argv, _ in host.calls)


@pytest.mark.parametrize(('committed_changes', 'saved_changes', 'starts'), [
    ({}, {'autostart': False, 'url': None}, True),
    ({'autostart': False}, {'autostart': True}, False),
    ({'url': None}, {'url': 'https://example.com/saved'}, True),
])
def test_login_uses_committed_autostart_and_url(setup, monkeypatch, committed_changes, saved_changes, starts):
    config, paths, host, home = user_fixture(setup)
    committed = replace(config, **committed_changes)
    _registered_default(committed, paths, host)
    saved = replace(config, **saved_changes)
    resolved = []
    original = display._session_target_start

    def observe(context, *args):
        resolved.append(context.url)
        return original(context, *args)

    monkeypatch.setattr(display, '_session_target_start', observe)
    assert display.session_start(config=saved, home=home, host=host, paths=paths, environment={}) == 0
    assert bool(_started_commands(host)) is starts
    assert resolved == ([committed.url] if starts else [])


def test_login_honors_registered_manual_stop_until_next_session(setup, monkeypatch):
    config, paths, host, home = user_fixture(setup)
    store = _registered_default(config, paths, host)
    store.cancel('default')
    saved = replace(config, autostart=False)
    assert display.session_start(config=saved, home=home, host=host, paths=paths, environment={}) == 0
    assert not _started_commands(host)

    original = host.run

    def new_login(argv, **kwargs):
        if argv[:2] == ['loginctl', 'list-sessions']:
            return Command(0, f'2 {os.getuid()} user seat0\n')
        return original(argv, **kwargs)

    monkeypatch.setattr(host, 'run', new_login)
    assert display.session_start(config=saved, home=home, host=host, paths=paths, environment={}) == 0
    assert _started_commands(host) == [['systemctl', '--user', 'start', '--no-block', display.UNIT]]


def test_login_does_not_start_or_stop_services_for_unproven_selection(setup):
    config, paths, host, home = user_fixture(setup)
    _registered_default(replace(config, user='other'), paths, host)
    host.states[display.LEGACY]['ActiveState'] = 'active'
    assert display.session_start(config=config, home=home, host=host, paths=paths, environment={}) == 1
    assert not _started_commands(host)
    assert host.states[display.LEGACY]['ActiveState'] == 'active'


def test_prepare_keeps_shared_login_entry_for_committed_autostart(setup):
    config, paths, host, home = user_fixture(setup)
    _registered_default(config, paths, host)
    display._prepare_user_files(config, home, paths)
    desktop = home / '.config/autostart/ala-display.desktop'
    before = desktop.stat().st_mtime_ns
    saved = replace(config, autostart=False)
    display._prepare_user_files(saved, home, paths)
    assert desktop.read_text() == display.desktop_text()
    assert desktop.stat().st_mtime_ns == before


def test_ala31_login_queues_once_without_display_and_does_not_clear_manager(setup,capsys):
    config,paths,host,home=user_fixture(setup)
    host.sessions=0;host.manager='XDG_CURRENT_DESKTOP=GNOME\n'
    assert display.session_start(config=config,home=home,host=host,paths=paths,environment={})==0
    assert len(_started_commands(host))==1
    assert not any('unset-environment' in argv or 'import-environment' in argv or 'reset-failed' in argv for argv,_ in host.calls)
    commands = [argv[:2] for argv, _ in host.calls]
    started = [argv for argv, _ in host.calls].index(_started_commands(host)[0])
    assert ['loginctl','list-sessions'] not in commands[:started]
    assert display.session_start(config=config,home=home,host=host,paths=paths,environment={})==0
    assert len(_started_commands(host))==1
    assert 'outcome=already_running' in capsys.readouterr().out


@pytest.mark.parametrize('state,result',[('failed','exit-code'),('inactive','start-limit-hit')])
def test_ala31_login_preserves_failed_service_without_reset_or_legacy_stop(setup,state,result,capsys):
    config,paths,host,home=user_fixture(setup)
    host.states[display.UNIT].update(ActiveState=state,Result=result)
    host.states[display.LEGACY]['ActiveState']='active'
    assert display.session_start(config=config,home=home,host=host,paths=paths,environment={})==1
    assert not _started_commands(host)
    assert not any('reset-failed' in argv or argv[:3]==['systemctl','--user','stop'] for argv,_ in host.calls)
    assert 'outcome=failed_preserved' in capsys.readouterr().out


def test_ala31_login_legacy_migration_stops_only_known_old_unit_before_start(setup):
    config,paths,host,home=user_fixture(setup)
    host.states[display.LEGACY]['ActiveState']='active'
    assert display.session_start(config=config,home=home,host=host,paths=paths,environment={})==0
    commands=[argv for argv,_ in host.calls]
    stop=['systemctl','--user','stop',display.LEGACY]
    start=['systemctl','--user','start','--no-block',display.UNIT]
    assert commands.index(stop)<commands.index(start)
    assert not any('reset-failed' in argv for argv in commands)


def test_ala31_login_preserves_both_services_and_reports_legacy_conflict(setup,capsys):
    config,paths,host,home=user_fixture(setup)
    host.states[display.LEGACY]['ActiveState']='active'
    host.states[display.UNIT]['ActiveState']='active'
    assert display.session_start(config=config,home=home,host=host,paths=paths,environment={})==1
    assert host.states[display.LEGACY]['ActiveState']==host.states[display.UNIT]['ActiveState']=='active'
    assert not any('reset-failed' in argv or argv[:3] in (['systemctl','--user','stop'],['systemctl','--user','start'])
                   for argv,_ in host.calls)
    assert 'phase=unit_state outcome=legacy_conflict rc=1' in capsys.readouterr().out


def test_ala31_login_never_starts_after_unconfirmed_legacy_stop(setup,capsys):
    config,paths,host,home=user_fixture(setup)
    host.states[display.LEGACY]['ActiveState']='active'
    original=host.run;stopped=False
    def run(argv,**kwargs):
        nonlocal stopped
        if argv[:3]==['systemctl','--user','stop']:
            result=original(argv,**kwargs);stopped=True;return result
        if stopped and argv[:4]==['systemctl','--user','show',display.LEGACY]:
            return Command(1,'','private command error must not appear')
        return original(argv,**kwargs)
    host.run=run
    assert display.session_start(config=config,home=home,host=host,paths=paths,environment={})==1
    assert not _started_commands(host)
    output=capsys.readouterr().out
    assert 'phase=legacy_stop outcome=unconfirmed' in output and 'private command' not in output


def test_ala31_explicit_restart_of_normal_unit_does_not_reset_failed(setup):
    config,paths,host,home=user_fixture(setup)
    host.states[display.UNIT]['ActiveState']='active'
    assert display.user_action('restart',config=config,home=home,host=host,paths=paths)['exit_code']==0
    assert not any('reset-failed' in argv for argv,_ in host.calls)


@pytest.mark.parametrize('condition',['cleared','still_failed','unknown','not_found','unknown_result'])
def test_ala31_failed_restart_rechecks_only_exact_already_cleared_state_after_reset_failure(setup,condition):
    config,paths,host,home=user_fixture(setup)
    host.states[display.UNIT].update(ActiveState='failed',Result='exit-code')
    original=host.run
    def run(argv,**kwargs):
        if argv[:3]==['systemctl','--user','reset-failed']:
            host.calls.append((argv,kwargs))
            if condition=='cleared':host.states[display.UNIT].update(ActiveState='inactive',Result='success',LoadState='loaded')
            if condition=='still_failed':host.states[display.UNIT].update(ActiveState='failed',Result='exit-code')
            if condition=='unknown':host.show_failures[display.UNIT]=Command(124,'','synthetic timeout')
            if condition=='not_found':host.states[display.UNIT].update(ActiveState='inactive',Result='success',LoadState='not-found')
            if condition=='unknown_result':host.states[display.UNIT].update(ActiveState='inactive',Result='unknown',LoadState='loaded')
            return Command(1,'','synthetic reset failure')
        return original(argv,**kwargs)
    host.run=run
    value=display.user_action('restart',config=config,home=home,host=host,paths=paths)
    assert len(_started_commands(host))==(1 if condition=='cleared' else 0)
    if condition=='cleared':assert value['state']=='starting'
    else:assert value['state']=='error' and value['reason']=='synthetic reset failure'


def test_ala31_foreign_profile_prevents_failure_reset_even_for_explicit_restart(setup):
    config,paths,host,home=user_fixture(setup)
    host.states[display.UNIT].update(ActiveState='failed',Result='exit-code')
    process=paths.at('/proc/999');process.mkdir(parents=True)
    (process/'status').write_text(f'Uid:\t{os.getuid()}\n')
    args=['chrome','--user-data-dir='+str(display._profile(home))]
    (process/'cmdline').write_bytes(('\0'.join(args)+'\0').encode())
    value=display.user_action('restart',config=config,home=home,host=host,paths=paths)
    assert value['state']=='blocked'
    assert not any('reset-failed' in argv for argv,_ in host.calls)


@pytest.mark.parametrize('condition',['session_delay','environment_delay'])
def test_ala31_service_waits_for_actual_readiness_then_launches_once(setup,condition):
    config,paths,host,home=user_fixture(setup)
    clock=ReadinessClock()
    if condition=='session_delay':host.sessions=0
    else:host.manager='XDG_CURRENT_DESKTOP=GNOME\n'
    def sleep(seconds):
        clock.sleep(seconds)
        if clock.value>=2:
            host.sessions=1;host.manager='WAYLAND_DISPLAY=wayland-test\n'
    result,launches=_ready_run(config,paths,host,home,clock,sleeper=sleep)
    assert result==0 and len(launches)==1 and clock.value==2
    assert not _started_commands(host)


def test_ala31_readiness_timeout_is_45_seconds_and_creates_no_profile(setup,capsys):
    config,paths,host,home=user_fixture(setup)
    host.sessions=0;clock=ReadinessClock()
    with pytest.raises(RuntimeError,match='45초'):
        _ready_run(config,paths,host,home,clock)
    assert clock.value==45
    assert not display._profile(home).exists()
    assert not _started_commands(host)
    assert 'phase=readiness outcome=timed_out' in capsys.readouterr().out


@pytest.mark.parametrize('condition',['stop','logout','session_changed'])
def test_ala31_readiness_cancellation_cannot_later_launch_browser(setup,condition,capsys):
    config,paths,host,home=user_fixture(setup)
    clock=ReadinessClock();host.manager='XDG_CURRENT_DESKTOP=GNOME\n'
    stopped=False;original=host.run
    def run(argv,**kwargs):
        if condition=='session_changed' and clock.value>0 and argv[:2]==['loginctl','list-sessions']:
            return Command(0,f'other-session {os.getuid()} user seat0\n')
        return original(argv,**kwargs)
    host.run=run
    def sleep(seconds):
        nonlocal stopped
        clock.sleep(seconds)
        if condition=='stop':stopped=True
        if condition=='logout':host.sessions=0
    result,launches=_ready_run(config,paths,host,home,clock,sleeper=sleep,stop_requested=lambda:stopped)
    assert result==78 and launches==[]
    host.sessions=1;host.manager='WAYLAND_DISPLAY=wayland-test\n'
    clock.sleep(60)
    assert launches==[] and not _started_commands(host)
    assert not display._profile(home).exists()
    assert 'phase=readiness outcome=canceled' in capsys.readouterr().out


@pytest.mark.parametrize('condition',['multiple_sessions','session_permission','manager_permission'])
def test_ala31_readiness_never_bypasses_ambiguous_session_or_permission_failure(setup,condition):
    config,paths,host,home=user_fixture(setup)
    clock=ReadinessClock();original=host.run
    if condition=='multiple_sessions':host.sessions=2
    def run(argv,**kwargs):
        if condition=='session_permission' and argv[:2]==['loginctl','show-session']:
            return Command(1,'','synthetic denied')
        if condition=='manager_permission' and argv[:3]==['systemctl','--user','show-environment']:
            return Command(1,'','synthetic denied')
        return original(argv,**kwargs)
    host.run=run
    with pytest.raises(RuntimeError):_ready_run(config,paths,host,home,clock)
    assert clock.value==0 and not display._profile(home).exists()


def test_ala31_login_logs_have_fixed_fields_and_never_include_values(setup,capsys):
    import re
    config,paths,host,home=user_fixture(setup)
    host.show_failures[display.UNIT]=Command(1,'','private-account https://example.com/private')
    assert display.session_start(config=config,home=home,host=host,paths=paths,environment={})==1
    lines=capsys.readouterr().out.splitlines()
    assert lines and all(re.fullmatch(r'\[ala-autostart\] phase=[a-z_]+ outcome=[a-z_]+ rc=-?\d+',line) for line in lines)
    assert not any('private-account' in line or config.url in line for line in lines)


def test_ala31_syslog_receives_identical_fixed_message_only_for_real_session_context(setup,monkeypatch,capsys):
    import sys
    from types import SimpleNamespace
    calls=[]
    module=SimpleNamespace(LOG_INFO=6,openlog=lambda name:calls.append(('open',name)),
                           syslog=lambda level,message:calls.append(('message',level,message)))
    monkeypatch.setitem(sys.modules,'syslog',module)
    display._autostart_log('entry','entered',journal=True)
    message='[ala-autostart] phase=entry outcome=entered rc=0'
    assert calls==[('open','ala-autostart'),('message',6,message)]
    assert capsys.readouterr().out.strip()==message
    config,paths,fake,home=user_fixture(setup)
    calls.clear()
    assert display.session_start(config=config,home=home,host=fake,paths=paths,environment={})==0
    assert calls==[]
    real=display.Host()
    fake.states[display.UNIT]['ActiveState']='inactive'
    monkeypatch.setattr(real,'run',fake.run)
    monkeypatch.setattr(display,'_foreign_profile_pid',lambda *args,**kwargs:None)
    assert display.session_start(config=config,home=home,host=real,paths=Paths(),environment={})==0
    records=[call[2] for call in calls if call[0]=='message']
    assert records[-2:] == [
        '[ala-autostart] phase=start outcome=requested rc=0',
        '[ala-autostart] phase=overview outcome=unavailable rc=0',
    ]


def test_ala31_syslog_import_and_connection_failures_are_fixed_nonfatal_messages(monkeypatch,capsys):
    import builtins
    from types import SimpleNamespace
    original=builtins.__import__
    for failure in (ImportError,OSError):
        def importing(name,*args,**kwargs):
            if name=='syslog':
                if failure is ImportError:raise ImportError('private import details')
                def unavailable(*args):raise OSError('private connection details')
                return SimpleNamespace(LOG_INFO=6,openlog=unavailable)
            return original(name,*args,**kwargs)
        monkeypatch.setattr(builtins,'__import__',importing)
        display._autostart_log('entry','entered',journal=True)
        output=capsys.readouterr().out
        assert output.splitlines()==['[ala-autostart] phase=entry outcome=entered rc=0',
                                     '[ala-autostart] phase=journal outcome=unavailable rc=1']
        assert 'private' not in output


@pytest.mark.parametrize('failure',[None,'unsafe_directory','non_socket','socket_symlink','wrong_owner','connection_failed'])
def test_ala31_missing_bus_context_uses_only_verified_current_user_socket(failure,monkeypatch,capsys):
    import stat
    import tempfile
    with tempfile.TemporaryDirectory(prefix='ala-bus-',dir=str(Path('/tmp').resolve())) as temporary:
        paths=Paths(Path(temporary));config=Config(user='user',url='https://example.com/display')
        home=paths.at('/home/user');home.mkdir(parents=True)
        runtime=paths.at('/run/user/test');runtime.mkdir(parents=True,mode=0o700)
        bus=runtime/'bus'
        try:
            if failure=='non_socket':bus.write_text('not a socket')
            elif failure=='socket_symlink':bus.symlink_to(runtime/'other')
            else:bus.write_text('synthetic socket inode')
            if failure=='unsafe_directory':runtime.chmod(0o777)
            # Inject kernel inode metadata; unit tests never open a bus/listener.
            original_stat=Path.lstat
            def observed_stat(path,*args,**kwargs):
                info=original_stat(path,*args,**kwargs);fields=list(info)
                if path==bus and failure not in {'non_socket','socket_symlink'}:fields[0]=stat.S_IFSOCK|0o600
                if path==runtime and failure=='wrong_owner':fields[4]=os.getuid()+1
                return os.stat_result(fields)
            monkeypatch.setattr(Path,'lstat',observed_stat)
            monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS','unverified incoming context')
            host=FakeHost(paths,config);original=host.run
            def run(argv,**kwargs):
                if argv[:3]==['systemctl','--user','show-environment']:
                    address=(kwargs.get('env') or {}).get('DBUS_SESSION_BUS_ADDRESS')
                    if failure=='connection_failed' or address!='unix:path=/run/user/test/bus':
                        host.calls.append((argv,kwargs))
                        return Command(1,'','private connection failure must not leak')
                return original(argv,**kwargs)
            host.run=run
            result=display.session_start(config=config,home=home,host=host,paths=paths,
                                         environment={'WAYLAND_DISPLAY':'wayland-test'})
            assert result==(0 if failure is None else 1)
            if failure is None:
                assert len(_started_commands(host))==1
                calls=[kwargs for argv,kwargs in host.calls if 'import-environment' in argv or argv[:3]==['systemctl','--user','start']]
                assert calls and all(item['env']['DBUS_SESSION_BUS_ADDRESS']=='unix:path=/run/user/test/bus' for item in calls)
            else:
                assert not _started_commands(host)
            output=capsys.readouterr().out
            assert 'private connection' not in output and 'unverified incoming' not in output
        finally:
            monkeypatch.setattr(Path,'lstat',original_stat)


def test_user_runtime_check_checks_real_arguments_and_failure_state(setup):
    config,paths,host,home=user_fixture(setup)
    process=paths.at('/proc/111');process.mkdir(parents=True)
    (process/'status').write_text(f'Uid:\t{os.getuid()}\t{os.getuid()}\n')
    arguments=display.chrome_arguments(config,home,{'WAYLAND_DISPLAY':'wayland-0'})
    (process/'cmdline').write_bytes(('\0'.join(arguments)+'\0').encode())
    host.states[display.UNIT].update(ActiveState='active',MainPID='111')
    result=display.user_action('check',config=config,home=home,host=host,paths=paths)
    assert result['exit_code']==2  # Old standalone Chrome is preserved; monitoring needs an explicit restart.
    assert any(r['id']=='chrome.health' and r['state']=='pending' for r in result['results'])
    assert any(r['id']=='chrome.option.password-store' and r['state']=='ok' for r in result['results'])
    arguments.remove('--password-store=basic')
    (process/'cmdline').write_bytes(('\0'.join(arguments)+'\0').encode())
    assert display.user_action('check',config=config,home=home,host=host,paths=paths)['exit_code']==1
    host.states[display.UNIT].update(ActiveState='failed',Result='start-limit-hit')
    result=display.user_action('check',config=config,home=home,host=host,paths=paths)
    assert result['state']=='error'
    assert result['service']['Result']=='start-limit-hit'


def test_runtime_launches_user_supervisor_with_private_pipe_and_new_profile(setup):
    config,paths,host,home=user_fixture(setup)
    from ala.display_health import RECOVERY_URL
    executed=[]
    class FakeSupervisor:
        def __init__(self, actual_config, actual_home, argv, **kwargs):
            assert actual_config==config and actual_home==home and kwargs['paths']==paths
            executed.append(argv)
        def run(self): return 0
    assert display.run_chrome(config=config,home=home,host=host,paths=paths,supervisor_factory=FakeSupervisor)==0
    args=executed[0]
    assert args[-1]==RECOVERY_URL and '--remote-debugging-pipe' in args
    assert '--password-store=basic' in args
    assert '--user-data-dir='+str(home/'.config/ala/chrome-display') in args
    assert '--ozone-platform=wayland' in args
    assert '--no-sandbox' not in args and '--kiosk' not in args


def test_ready_failure_never_disables_tls():
    def opener(request,timeout):
        raise urllib.error.URLError('certificate verification failed')
    assert display.url_ready('https://example.com',opener=opener) is False


def test_root_cannot_launch_chrome_even_with_explicit_test_config(setup,monkeypatch):
    config,paths,host,home=user_fixture(setup)
    monkeypatch.setattr(display.os,'getuid',lambda:0)
    with pytest.raises(PermissionError):
        display.run_chrome(config=config,home=home,host=host,paths=paths)
    assert host.calls==[]


def test_unmanaged_unit_is_blocked_before_gdm_changes(setup):
    config,paths,host=setup
    unit=paths.at('/etc/systemd/user/ala-chrome.service')
    unit.parent.mkdir(parents=True);unit.write_text('[Service]\nExecStart=/unrelated\n')
    gdm=paths.at('/etc/gdm/custom.conf');gdm.write_text('[daemon]\nWaylandEnable=true\n')
    before=gdm.read_bytes()
    assert any(r.id=='chrome.unit' and r.state=='blocked' for r in display.inspect_display(config,host,paths))
    assert any(r.state=='blocked' for r in display.apply_display(config,host,paths))
    assert gdm.read_bytes()==before


def test_existing_unmanaged_display_profile_is_preserved(setup):
    config,paths,host,home=user_fixture(setup)
    profile=display._profile(home);profile.mkdir(parents=True)
    cookie=profile/'Cookies';cookie.write_text('do not migrate')
    with pytest.raises(RuntimeError,match='관리되지 않은'):
        display.run_chrome(config=config,home=home,host=host,paths=paths)
    assert cookie.read_text()=='do not migrate'


def test_overriding_duplicate_flags_are_not_reported_healthy(setup):
    config,paths,host,home=user_fixture(setup)
    process=paths.at('/proc/111');process.mkdir(parents=True)
    (process/'status').write_text(f'Uid:\t{os.getuid()}\n')
    args=display.chrome_arguments(config,home,{'WAYLAND_DISPLAY':'wayland-0'})
    args+=['--password-store=gnome-libsecret','--no-sandbox=false']
    (process/'cmdline').write_bytes(('\0'.join(args)+'\0').encode())
    host.states[display.UNIT].update(ActiveState='active',MainPID='111')
    value=display.user_action('check',config=config,home=home,host=host,paths=paths)
    assert value['exit_code']==1 and value['state']=='error'
    assert any(r['id']=='chrome.option.password-store' and r['state']=='error' for r in value['results'])
    assert any(r['id']=='chrome.forbidden' for r in value['results'])


def test_autostart_changes_are_backed_up_outside_user_profile(setup):
    config,paths,host=setup
    display.apply_display(config,host,paths)
    desktop=paths.at('/home/user/.config/autostart/ala-display.desktop')
    desktop.write_text(desktop.read_text()+'# changed by user\n')
    display.apply_display(config,host,paths)
    assert any(p.name.startswith('ala-display.desktop.') for p in paths.backups.iterdir())
    assert not paths.at('/home/user/.local/state/ala/backups').exists()


def test_desktop_only_first_ssh_setup_needs_no_url_or_gui_login(setup):
    config,paths,host=setup
    config=replace(config,url=None,autostart=False)
    host.config=config;host.sessions=0;host.runtime=False
    result=display.apply_display(config,host,paths)
    states={r.id:r.state for r in result}
    assert 'AutomaticLogin=user' in paths.at('/etc/gdm/custom.conf').read_text()
    assert paths.at('/home/user/.config/gnome-initial-setup-done').read_text()=='yes\n'
    assert not paths.at('/home/user/.config/autostart/ala-display.desktop').exists()
    assert states['display.configuration']=='ok'
    assert 'display.session' not in states
    assert 'chrome.url' not in states
    assert not any(argv[0]=='runuser' and argv[-1]=='check' for argv,_ in host.calls)
    before={str(p):(p.read_bytes(),p.stat().st_mtime_ns) for p in paths.root.rglob('*') if p.is_file()}
    display.apply_display(config,host,paths)
    assert before=={str(p):(p.read_bytes(),p.stat().st_mtime_ns) for p in paths.root.rglob('*') if p.is_file()}


def test_autostart_off_checks_desktop_without_requiring_chrome(setup):
    config,paths,host=setup
    config=replace(config,autostart=False);host.config=config
    result=display.apply_display(config,host,paths)
    states={r.id:r.state for r in result}
    assert states['display.configuration']=='ok'
    assert 'display.session' not in states
    assert 'chrome.runtime' not in states
    assert 'display.visual_acceptance' not in states
    assert not any(argv[0]=='runuser' and argv[-1]=='check' for argv,_ in host.calls)
    assert not any(argv[0]=='runuser' and argv[-1]=='settings' for argv,_ in host.calls)


def test_missing_url_prepares_desktop_and_internal_setup_mode_without_starting_now(setup):
    config,paths,host=setup
    config=replace(config,url=None);host.config=config
    states={r.id:r.state for r in display.apply_display(config,host,paths)}
    assert states['display.configuration']=='ok'
    assert 'display.session' not in states
    assert states['chrome.url']=='ok'
    assert 'chrome.runtime' not in states
    assert not any(argv[0]=='runuser' and argv[-1]=='check' for argv,_ in host.calls)


@pytest.mark.parametrize('locked,state',[('yes','pending'),('no','ok'),('','unknown')])
def test_actual_locked_session_is_separate_from_policy(setup,locked,state):
    config,paths,host=setup
    host.locked=locked
    display.apply_display(config,host,paths)
    results={r.id:r for r in display.inspect_display(config,host,paths)}
    assert results['display.configuration'].state=='ok'
    assert results['display.session'].state=='ok'
    assert results['display.session_lock'].state==state


def test_failed_gdm_is_not_reported_as_just_missing_login(setup):
    config,paths,host=setup
    host.sessions=0
    host.gdm_status=Command(0,'LoadState=loaded\nActiveState=failed\nResult=exit-code\n')
    host.gdm_logs=Command(0,'No screens found; private-account token=do-not-export\n')
    before=list(paths.root.rglob('*'))
    results={r.id:r for r in display.inspect_display(config,host,paths)}
    assert results['display.gdm_runtime'].state=='error'
    assert results['display.gdm_diagnostics'].actual==['그래픽 초기화 오류 단서']
    serialized=json.dumps([r.to_dict() for r in results.values()])
    assert 'private-account' not in serialized and 'do-not-export' not in serialized
    assert before==list(paths.root.rglob('*'))
    journal=[(argv,kw) for argv,kw in host.calls if argv[0]=='journalctl'][0]
    assert '-b' in journal[0] and journal[0][journal[0].index('-n')+1]=='80'
    assert journal[1]['timeout']==10


def test_gdm_query_permissions_are_unknown_not_healthy(setup):
    config,paths,host=setup
    host.sessions=0
    host.gdm_status=Command(1,'','denied')
    host.gdm_logs=Command(1,'','denied')
    result={r.id:r for r in display.inspect_display(config,host,paths)}
    assert result['display.gdm_runtime'].state=='unknown'
    assert result['display.gdm_diagnostics'].state=='unknown'


def test_current_desktop_ignores_old_gdm_errors(setup):
    config,paths,host=setup
    host.gdm_logs=Command(0,'No screens found\n')
    result={r.id:r for r in display.inspect_display(config,host,paths)}
    assert result['display.session'].state=='ok'
    assert 'display.gdm_diagnostics' not in result
    assert not any(argv[0]=='journalctl' for argv,_ in host.calls)


def _started_commands(host):
    return [argv for argv,_ in host.calls if argv[:3]==['systemctl','--user','start']]


def _seed_display_process(config,paths,host,pid='321'):
    home=paths.at('/home/user')
    process=paths.at('/proc')/pid
    process.mkdir(parents=True,exist_ok=True)
    (process/'status').write_text(f'Uid:\t{os.getuid()}\n')
    arguments=display.chrome_arguments(config,home,{'WAYLAND_DISPLAY':'wayland-0'})
    (process/'cmdline').write_bytes(('\0'.join(arguments)+'\0').encode())
    host.states[display.UNIT].update(ActiveState='active',MainPID=pid)


def _seed_supervised_display(config,paths,host,pid='321',browser='322'):
    from ala import display_health as health
    home=paths.at('/home/user')
    for process_id,args in ((pid,['/opt/ala/current/.venv/bin/python','-I','/opt/ala/current/entry.py','_chrome-run']),
                            (browser,display.chrome_arguments(config,home,{'WAYLAND_DISPLAY':'wayland-0'},supervised=True))):
        process=paths.at('/proc')/process_id
        process.mkdir(parents=True,exist_ok=True)
        (process/'status').write_text(f'Uid:\t{os.getuid()}\nPPid:\t{pid if process_id == browser else 1}\n')
        (process/'stat').write_text(f'{process_id} (chrome process) S '+'0 '*18+'123')
        (process/'cgroup').write_text('0::/user.slice/ala-chrome.service\n')
        (process/'cmdline').write_bytes(('\0'.join(args)+'\0').encode())
    boot=paths.at('/proc/sys/kernel/random/boot_id');boot.parent.mkdir(parents=True,exist_ok=True);boot.write_text('test-boot')
    value={'state':'healthy','supervisor_pid':int(pid),'browser_pid':int(browser),
           'supervisor_start_ticks':'123','browser_start_ticks':'123','boot_id':'test-boot',
           'updated_at':health.stamp(time.time()),'config_fingerprint':health.fingerprint(config.url),
           'target_matches_config':True,'reason':'주 문서 확인','next_action':'실제 화면 별도 확인'}
    state=health.state_path(home);state.parent.mkdir(parents=True,exist_ok=True)
    state.write_text(json.dumps(value));state.chmod(0o600)
    host.states[display.UNIT].update(ActiveState='active',MainPID=pid)


def test_supervisor_record_and_actual_child_flags_are_both_required(setup):
    config,paths,host,home=user_fixture(setup)
    _seed_supervised_display(config,paths,host)
    result=display.user_action('check',config=config,home=home,host=host,paths=paths)
    assert result['exit_code']==0
    assert any(r['id']=='chrome.health' and r['state']=='ok' for r in result['results'])
    process=paths.at('/proc/322/cmdline')
    process.write_bytes(process.read_bytes().replace(b'--remote-debugging-pipe\0',b''))
    assert display.user_action('check',config=config,home=home,host=host,paths=paths)['exit_code']==1


def test_supervisor_without_record_is_pending_without_claiming_site_is_down(setup):
    from ala.display_health import state_path
    config,paths,host,home=user_fixture(setup)
    _seed_supervised_display(config,paths,host)
    state_path(home).unlink()
    before={str(p) for p in paths.root.rglob('*')}
    result=display.user_action('check',config=config,home=home,host=host,paths=paths)
    assert result['exit_code']==2
    actual=next(r['actual'] for r in result['results'] if r['id']=='chrome.health')
    assert '감시 기록을 아직' in actual['reason']
    assert before=={str(p) for p in paths.root.rglob('*')}


def test_supervisor_allows_verified_direct_child_in_a_different_cgroup(setup):
    config,paths,host,home=user_fixture(setup)
    _seed_supervised_display(config,paths,host)
    paths.at('/proc/322/cgroup').write_text('0::/other.service\n')
    result=display.user_action('check',config=config,home=home,host=host,paths=paths)
    assert result['exit_code']==0
    assert any(r['id']=='chrome.health' and r['state']=='ok' for r in result['results'])


@pytest.mark.parametrize('failure',['unrelated_parent','missing_parent','wrong_uid','pid_reuse',
                                    'supervisor_reused','missing_process','wrong_profile'])
def test_supervisor_rejects_unrelated_or_unverified_process_without_healthy_row(setup,failure):
    config,paths,host,home=user_fixture(setup)
    _seed_supervised_display(config,paths,host)
    process=paths.at('/proc/322')
    if failure=='unrelated_parent': (process/'status').write_text(f'Uid:\t{os.getuid()}\nPPid:\t999\n')
    if failure=='missing_parent': (process/'status').write_text(f'Uid:\t{os.getuid()}\n')
    if failure=='wrong_uid': (process/'status').write_text(f'Uid:\t{os.getuid()+1}\nPPid:\t321\n')
    if failure=='pid_reuse': (process/'stat').write_text('322 (chrome) S '+'0 '*18+'456')
    if failure=='supervisor_reused': paths.at('/proc/321/stat').write_text('321 (python) S '+'0 '*18+'456')
    if failure=='missing_process': (process/'status').unlink()
    if failure=='wrong_profile':
        arguments=display.chrome_arguments(config,home,{'WAYLAND_DISPLAY':'wayland-0'},supervised=True)
        arguments=[arg if not arg.startswith('--user-data-dir=') else '--user-data-dir=/unmanaged-profile' for arg in arguments]
        (process/'cmdline').write_bytes(('\0'.join(arguments)+'\0').encode())
    result=display.user_action('check',config=config,home=home,host=host,paths=paths)
    assert result['exit_code']==1
    assert result['health']['state']=='unknown'
    assert any(r['id']=='chrome.health' and r['state']=='unknown' for r in result['results'])
    assert not any(r['id']=='chrome.health' and r['state']=='ok' for r in result['results'])


@pytest.mark.parametrize('unit',[display.UNIT,display.LEGACY])
@pytest.mark.parametrize('action',['check','status'])
def test_partial_service_query_failure_never_claims_healthy_running(setup,unit,action):
    config,paths,host,home=user_fixture(setup)
    _seed_supervised_display(config,paths,host)
    host.show_failures[unit]=Command(1,'ActiveState=active\nLoadState=loaded\nMainPID=321\n','synthetic denied')
    result=display.user_action(action,config=config,home=home,host=host,paths=paths)
    assert result['state']=='unknown'
    assert (result['check_exit_code'] if action=='status' else result['exit_code'])==1
    assert any(r['id']=='chrome.runtime' and r['state']=='unknown' for r in result['results'])
    assert any(r['id']=='chrome.health' and r['state']=='unknown' for r in result['results'])
    assert not any(argv[:3] in (['systemctl','--user','start'],['systemctl','--user','stop']) for argv,_ in host.calls)


@pytest.mark.parametrize('supervised',[False,True])
@pytest.mark.parametrize('flag',['--remote-debugging-port=9222','--remote-debugging-port=0','--remote-debugging-address=localhost'])
def test_network_debugging_flags_are_forbidden_in_every_display_mode(setup,supervised,flag):
    config,paths,host,home=user_fixture(setup)
    if supervised:
        _seed_supervised_display(config,paths,host)
        process=paths.at('/proc/322/cmdline')
    else:
        _seed_display_process(config,paths,host)
        process=paths.at('/proc/321/cmdline')
    process.write_bytes(process.read_bytes()+flag.encode()+b'\0')
    result=display.user_action('check',config=config,home=home,host=host,paths=paths)
    assert result['exit_code']==1
    assert any(r['id']=='chrome.forbidden' and r['state']=='error' for r in result['results'])


def test_debugging_pipe_requires_valid_supervisor_evidence(setup):
    config,paths,host,home=user_fixture(setup)
    _seed_display_process(config,paths,host)
    process=paths.at('/proc/321/cmdline')
    process.write_bytes(process.read_bytes()+b'--remote-debugging-pipe\0')
    result=display.user_action('check',config=config,home=home,host=host,paths=paths)
    assert result['exit_code']==1
    assert any(r['id']=='chrome.forbidden' and r['actual']=='--remote-debugging-pipe' for r in result['results'])


def test_registration_uses_stable_launcher_across_tool_releases(monkeypatch):
    monkeypatch.setattr(display,'RELEASE',Path('/opt/ala/releases/0.1.0rc14'))
    first=(display.unit_text(),display.desktop_text())
    monkeypatch.setattr(display,'RELEASE',Path('/opt/ala/releases/0.1.0rc15'))
    assert (display.unit_text(),display.desktop_text())==first
    assert '"/usr/local/bin/ala" "_runtime-dispatch" "default"' in first[0]
    assert '"/usr/local/bin/ala" "_session"' in first[1]
    assert '/opt/ala/releases/' not in ''.join(first)


def test_apply_migrates_only_managed_versioned_launchers_once(setup,monkeypatch):
    config,paths,host=setup
    display.apply_display(config,host,paths)
    unit=paths.at('/etc/systemd/user/ala-chrome.service')
    desktop=paths.at('/home/user/.config/autostart/ala-display.desktop')
    legacy_entry='"/opt/ala/releases/0.1.0rc14/.venv/bin/python" "-I" "/opt/ala/releases/0.1.0rc14/entry.py"'
    old_unit=display.unit_text().replace('"/usr/local/bin/ala"',legacy_entry)
    old_desktop=display.desktop_text().replace('"/usr/local/bin/ala"',legacy_entry)
    unit.write_text(old_unit);desktop.write_text(old_desktop)
    assert {'chrome.unit','chrome.autostart'} <= {r.id for r in display.inspect_display(config,host,paths) if r.state=='change_required'}
    display.apply_display(config,host,paths)
    backups=[p.read_text() for p in paths.backups.iterdir() if p.is_file()]
    assert old_unit in backups and old_desktop in backups
    before={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in (unit,desktop)}
    backup_names={p.name for p in paths.backups.iterdir()}
    monkeypatch.setattr(display,'RELEASE',Path('/opt/ala/releases/0.1.0rc15'))
    result={r.id:r.state for r in display.inspect_display(config,host,paths)}
    assert result['chrome.unit']==result['chrome.autostart']=='ok'
    display.apply_display(config,host,paths)
    assert {p:(p.read_bytes(),p.stat().st_mtime_ns) for p in (unit,desktop)}==before
    assert {p.name for p in paths.backups.iterdir()}==backup_names
    # Only display registrations use the stable launcher. User RPC stays pinned
    # to the executing release, as do the install jobs managed elsewhere.
    assert any('/opt/ala/releases/0.1.0rc15/entry.py' in argv for argv,_ in host.calls if argv[0]=='runuser')


def test_apply_only_provisions_registration_without_starting_current_chrome(setup):
    config,paths,host=setup
    host.real_user_check=True
    host.process_on_start=lambda:_seed_supervised_display(config,paths,host)
    result=display.apply_display(config,host,paths)
    assert _started_commands(host)==[]
    assert Report(result).exit_code==0
    assert any(r.id=='chrome.autostart' and r.state=='ok' for r in result)
    assert not any(r.id.startswith(('chrome.health','chrome.runtime','display.session')) for r in result)
    before={str(p):(p.read_bytes(),p.stat().st_mtime_ns) for p in paths.root.rglob('*') if p.is_file()}
    display.apply_display(config,host,paths)
    assert len(_started_commands(host))==0
    assert before=={str(p):(p.read_bytes(),p.stat().st_mtime_ns) for p in paths.root.rglob('*') if p.is_file()}
    assert not any('reset-failed' in argv or argv[:3]==['systemctl','--user','stop'] for argv,_ in host.calls)


def test_apply_registration_is_ready_while_full_inspection_reports_stopped(setup):
    config,paths,host=setup
    host.real_user_check=True
    result=display.apply_display(config,host,paths)
    assert len(_started_commands(host))==0
    assert Report(result).exit_code==0
    assert any(r.id=='chrome.runtime' and r.state=='pending' for r in display.inspect_display(config,host,paths))


@pytest.mark.parametrize('condition',['autostart_off','no_url','no_session','no_bus','locked','multiple_sessions'])
def test_apply_prepares_registration_without_unsafe_current_start(setup,condition):
    config,paths,host=setup
    if condition=='autostart_off':config=replace(config,autostart=False)
    if condition=='no_url':config=replace(config,url=None)
    host.config=config
    if condition=='no_session':host.sessions=0
    if condition=='no_bus':host.runtime=False
    if condition=='locked':host.locked='yes'
    if condition=='multiple_sessions':host.sessions=2
    result={r.id:r for r in display.apply_display(config,host,paths)}
    assert result['display.configuration'].state=='ok'
    assert result['chrome.unit'].state=='ok'
    assert result['chrome.autostart'].state=='ok'
    assert not _started_commands(host)
    assert not any('reset-failed' in argv or argv[:3]==['systemctl','--user','stop'] for argv,_ in host.calls)
    if condition=='no_url':assert result['chrome.url'].state=='ok'
    assert not any(id in result for id in ('chrome.runtime','chrome.health','display.session','display.session_lock'))
    assert not any(argv[0]=='runuser' and argv[-1] in {'ensure-start','check','settings'} for argv,_ in host.calls)


def test_apply_does_not_require_current_manager_environment(setup):
    config,paths,host=setup
    host.manager='XDG_CURRENT_DESKTOP=GNOME\n'
    host.real_user_check=True
    result={r.id:r for r in display.apply_display(config,host,paths)}
    assert result['chrome.autostart'].state=='ok'
    assert 'chrome.runtime' not in result
    assert Report(list(result.values())).exit_code==0
    assert not _started_commands(host)
    assert not any(r.state in {'blocked','error'} for r in result.values())


def test_apply_preserves_legacy_running_service_without_migration_restart(setup):
    config,paths,host=setup
    host.states[display.LEGACY]['ActiveState']='active'
    host.real_user_check=True
    result=display.apply_display(config,host,paths)
    assert host.states[display.LEGACY]['ActiveState']=='active'
    assert host.states[display.UNIT]['ActiveState']=='inactive'
    assert not any(r.id in {'chrome.runtime','chrome.legacy','chrome.health'} for r in result)
    assert Report(result).exit_code==0
    assert not _started_commands(host)
    assert not any('reset-failed' in argv or argv[:3]==['systemctl','--user','stop'] for argv,_ in host.calls)


def test_apply_preserves_failed_start_limit_and_failure_reason(setup):
    config,paths,host=setup
    host.states[display.UNIT].update(ActiveState='failed',Result='start-limit-hit')
    host.real_user_check=True
    result=display.apply_display(config,host,paths)
    assert host.states[display.UNIT]['ActiveState']=='failed'
    assert not any(r.id.startswith(('chrome.runtime','chrome.health')) for r in result)
    assert Report(result).exit_code==0
    assert not _started_commands(host)
    assert not any('reset-failed' in argv or argv[:3]==['systemctl','--user','stop'] for argv,_ in host.calls)


@pytest.mark.parametrize('unit',[display.UNIT,display.LEGACY])
@pytest.mark.parametrize('response',[Command(124,'','timeout'),Command(0),
                                     Command(1,'ActiveState=inactive\nLoadState=loaded\n','denied')])
def test_ensure_start_does_not_infer_inactive_from_failed_service_query(setup,unit,response):
    config,paths,host=setup
    host.show_failures[unit]=response
    host.real_user_check=True
    result=display.user_action('ensure-start',config=config,home=paths.at('/home/user'),host=host,paths=paths)
    assert result['state']=='unknown' and result['exit_code']==1
    assert not _started_commands(host)
    assert not any('reset-failed' in argv or argv[:3]==['systemctl','--user','stop'] for argv,_ in host.calls)


def test_structured_absence_of_legacy_unit_allows_new_start(setup):
    config,paths,host=setup
    host.show_failures[display.LEGACY]=Command(1,'ActiveState=inactive\nLoadState=not-found\nMainPID=0\n','unit not found')
    result=display.user_action('ensure-start',config=config,home=paths.at('/home/user'),host=host,paths=paths)
    assert len(_started_commands(host))==1
    assert result['state']=='starting'


def test_ensure_start_rejects_partial_active_response_before_kept_branch(setup):
    config,paths,host=setup
    host.show_failures[display.UNIT]=Command(1,'ActiveState=active\nLoadState=loaded\nMainPID=0\n','denied')
    value=display.user_action('ensure-start',config=config,home=paths.at('/home/user'),host=host,paths=paths)
    assert value['state']=='unknown' and value['exit_code']==1
    assert not _started_commands(host)
    assert not any('reset-failed' in argv or argv[:3]==['systemctl','--user','stop'] for argv,_ in host.calls)


def test_apply_preserves_foreign_profile_without_attempting_to_start_it(setup):
    config,paths,host=setup
    _seed_display_process(config,paths,host,'999')
    host.states[display.UNIT].update(ActiveState='inactive',MainPID='0')
    profile=display._profile(paths.at('/home/user'))
    profile.mkdir(parents=True)
    lock=profile/'SingletonLock';lock.symlink_to('host-999')
    result=display.apply_display(config,host,paths)
    assert Report(result).exit_code==0
    assert lock.is_symlink()
    assert not _started_commands(host)
    assert not any(argv[0]=='pkill' or 'reset-failed' in argv or argv[:3]==['systemctl','--user','stop'] for argv,_ in host.calls)


def test_configuration_only_inspection_is_readonly_without_session_or_runtime_queries(setup):
    config,paths,host=setup
    display.apply_display(config,host,paths)
    host.calls=[];host.sessions=0;host.runtime=False
    before={str(p):(p.read_bytes(),p.stat().st_mtime_ns) for p in paths.root.rglob('*') if p.is_file()}
    result=display.inspect_display(config,host,paths,configuration_only=True)
    assert Report(result).exit_code==0
    assert not any(r.id.startswith(('chrome.runtime','chrome.health','display.session','display.live.')) for r in result)
    assert not any(argv[0] in {'loginctl','runuser','journalctl'} for argv,_ in host.calls)
    assert before=={str(p):(p.read_bytes(),p.stat().st_mtime_ns) for p in paths.root.rglob('*') if p.is_file()}
    full=display.inspect_display(config,host,paths)
    assert any(r.id=='display.session' and r.state=='pending' for r in full)


def test_display_parts_keep_runtime_exception_out_of_configuration_results(setup):
    config,paths,host=setup
    display.apply_display(config,host,paths)
    original=host.run
    def run(argv,**kwargs):
        if argv[:2]==['loginctl','list-sessions']:raise OSError('synthetic session query unavailable')
        return original(argv,**kwargs)
    host.run=run
    configuration,runtime=display.inspect_display_parts(config,host,paths)
    assert Report(configuration).exit_code==0
    assert Report(runtime).exit_code==1
    assert any(r.id=='display.inspect' and r.state=='unknown' for r in runtime)
    assert not any(r.id=='display.inspect' for r in configuration)


def test_display_parts_keep_static_exception_in_configuration_and_skip_runtime(setup):
    config,paths,host=setup
    paths.at('/etc/gdm/custom.conf').write_text('[daemon]\n[daemon]\n')
    configuration,runtime=display.inspect_display_parts(config,host,paths)
    assert any(r.id=='display.inspect' and r.state=='blocked' for r in configuration)
    assert runtime==[]
    assert not any(argv[0] in {'loginctl','runuser','journalctl'} for argv,_ in host.calls)


def test_display_parts_inspect_one_pass_and_keep_url_input_in_static_part_only(setup):
    config,paths,host=setup
    config=replace(config,url=None);host.config=config
    display.apply_display(config,host,paths);host.calls=[]
    configuration,runtime=display.inspect_display_parts(config,host,paths)
    assert sum(r.id=='chrome.url' for r in configuration)==1
    assert not any(r.id=='chrome.url' for r in runtime)
    assert sum(argv==['systemctl','get-default'] for argv,_ in host.calls)==1
    assert sum(argv[:2]==['loginctl','list-sessions'] for argv,_ in host.calls)==1


def test_configuration_only_accepts_setup_mode_when_autostart_selected(setup):
    config,paths,host=setup
    config=replace(config,url=None);host.config=config
    display.apply_display(config,host,paths)
    assert any(r.id=='chrome.url' and r.state=='ok' for r in display.inspect_display(config,host,paths,configuration_only=True))
    config=replace(config,autostart=False);host.config=config
    result=display.apply_display(config,host,paths)
    assert Report(result).exit_code==0
    assert not any(r.id=='chrome.url' for r in result)


def test_pid_reuse_during_argument_inspection_does_not_leave_healthy_row(setup,monkeypatch):
    from ala import display_health as health
    config,paths,host,home=user_fixture(setup)
    _seed_supervised_display(config,paths,host)
    counts={}
    original=health.process_identity
    def identity(paths,pid):
        counts[pid]=counts.get(pid,0)+1
        if pid==322 and counts[pid]>1:return '456'
        return original(paths,pid)
    monkeypatch.setattr(health,'process_identity',identity)
    result=display.user_action('check',config=config,home=home,host=host,paths=paths)
    assert result['exit_code']==1
    assert result['health']['state']=='unknown'
    assert not any(r['id']=='chrome.health' and r['state']=='ok' for r in result['results'])


def test_inspection_never_runs_ensure_start_or_changes_registration(setup):
    config,paths,host=setup
    display.apply_display(config,host,paths)
    host.calls=[]
    host.states[display.UNIT]['ActiveState']='inactive'
    before={str(p):(p.read_bytes(),p.stat().st_mtime_ns) for p in paths.root.rglob('*') if p.is_file()}
    display.inspect_display(config,host,paths)
    assert before=={str(p):(p.read_bytes(),p.stat().st_mtime_ns) for p in paths.root.rglob('*') if p.is_file()}
    assert not any(argv[0]=='runuser' and argv[-1] in {'prepare','ensure-start'} for argv,_ in host.calls)
    assert not _started_commands(host)
    assert not any(argv[0]=='journalctl' for argv,_ in host.calls)


def test_greeter_session_is_not_a_logged_in_desktop(setup):
    config,paths,host=setup
    original=host.run
    def run(argv,**kwargs):
        if argv[:2]==['loginctl','show-session']:
            return Command(0,'User=999\nType=wayland\nClass=greeter\nRemote=no\nActive=yes\nState=active\n')
        return original(argv,**kwargs)
    host.run=run
    results={r.id:r for r in display.inspect_display(config,host,paths)}
    assert results['display.session'].state=='pending'
    assert results['display.login_screen'].state=='pending'
    assert 'greeter' in results['display.login_screen'].actual
    assert not any(argv[0]=='runuser' and argv[-1]=='check' for argv,_ in host.calls)


# ALA-29: process titles are observations, not recovered shell argv.
def _ala29_flat_process(paths, pid, *, arguments=None, suffix=b'\0', executable='/opt/google/chrome/chrome'):
    process=paths.at('/proc')/str(pid)
    if arguments is None:
        arguments=(process/'cmdline').read_bytes().decode().rstrip('\0').split('\0')
    (process/'cmdline').write_bytes((' '.join(['/opt/google/chrome/chrome',*arguments[1:]])).encode()+suffix)
    link=process/'exe'
    if link.is_symlink():link.unlink()
    link.symlink_to(executable)
    return process


@pytest.mark.parametrize('suffix',[b'',b'\0',b'\0\0\0'])
def test_ala29_flat_supervised_chrome_keeps_real_options_and_health(setup,suffix):
    config,paths,host,home=user_fixture(setup)
    _seed_supervised_display(config,paths,host)
    _ala29_flat_process(paths,322,suffix=suffix)
    before={str(path):(path.read_bytes(),path.stat().st_mtime_ns)
            for path in paths.root.rglob('*') if path.is_file() and not path.is_symlink()}
    result=display.user_action('check',config=config,home=home,host=host,paths=paths)
    assert result['exit_code']==0
    assert result['health']['state']=='healthy'
    assert any(row['id']=='chrome.health' and row['state']=='ok' for row in result['results'])
    assert all(row['state']=='ok' for row in result['results'] if row['id'].startswith('chrome.option.'))
    assert before=={str(path):(path.read_bytes(),path.stat().st_mtime_ns)
                   for path in paths.root.rglob('*') if path.is_file() and not path.is_symlink()}
    assert not _started_commands(host)


@pytest.mark.parametrize('condition',['duplicate','wrong','forbidden'])
def test_ala29_flat_title_does_not_hide_definite_bad_switches(setup,condition):
    config,paths,host,home=user_fixture(setup)
    _seed_supervised_display(config,paths,host)
    process=paths.at('/proc/322')
    arguments=(process/'cmdline').read_bytes().decode().rstrip('\0').split('\0')
    if condition=='duplicate':arguments.insert(-1,'--password-store=basic')
    elif condition=='wrong':arguments=[arg if arg!='--password-store=basic' else '--password-store=gnome' for arg in arguments]
    else:arguments.insert(-1,'--no-sandbox')
    _ala29_flat_process(paths,322,arguments=arguments)
    result=display.user_action('check',config=config,home=home,host=host,paths=paths)
    assert result['exit_code']==1 and result['health']['state']=='unknown'
    identifier='chrome.forbidden' if condition=='forbidden' else 'chrome.option.password-store'
    assert any(row['id']==identifier and row['state']=='error' for row in result['results'])
    assert not any(row['id']=='chrome.health' and row['state']=='ok' for row in result['results'])


@pytest.mark.parametrize('condition',['exe_mismatch','exe_unreadable','malformed_utf8','quoted_profile','residual_after_nul'])
def test_ala29_ambiguous_title_is_one_process_unknown_without_option_error_flood(setup,condition,monkeypatch):
    config,paths,host,home=user_fixture(setup)
    _seed_supervised_display(config,paths,host)
    process=_ala29_flat_process(paths,322)
    if condition=='exe_mismatch':
        (process/'exe').unlink();(process/'exe').symlink_to('/usr/bin/python3')
    elif condition=='exe_unreadable':
        original=os.readlink
        def denied(path,*args,**kwargs):
            if Path(path)==process/'exe':raise PermissionError('synthetic denied')
            return original(path,*args,**kwargs)
        monkeypatch.setattr(os,'readlink',denied)
    elif condition=='malformed_utf8':(process/'cmdline').write_bytes(b'\xff\0')
    elif condition=='quoted_profile':
        value=(process/'cmdline').read_bytes()
        original=('--user-data-dir='+str(display._profile(home))).encode()
        quoted=('--user-data-dir="'+str(display._profile(home))+'"').encode()
        (process/'cmdline').write_bytes(value.replace(original,quoted))
    else:(process/'cmdline').write_bytes((process/'cmdline').read_bytes()+b'--no-sandbox\0')
    result=display.user_action('check',config=config,home=home,host=host,paths=paths)
    assert result['exit_code']==1 and result['health']['state']=='unknown'
    assert sum(row['id']=='chrome.process' and row['state']=='unknown' for row in result['results'])==1
    assert not any(row['id'].startswith('chrome.option.') for row in result['results'])
    assert not any(row['id']=='chrome.url' for row in result['results'])
    assert not any(row['id']=='chrome.health' and row['state']=='ok' for row in result['results'])


def test_ala29_flat_success_still_rechecks_process_identity_after_arguments(setup,monkeypatch):
    from ala import display_health as health
    config,paths,host,home=user_fixture(setup)
    _seed_supervised_display(config,paths,host)
    _ala29_flat_process(paths,322)
    counts={}
    original=health.process_identity
    def changed(paths,pid):
        counts[pid]=counts.get(pid,0)+1
        return '456' if pid==322 and counts[pid]>1 else original(paths,pid)
    monkeypatch.setattr(health,'process_identity',changed)
    result=display.user_action('check',config=config,home=home,host=host,paths=paths)
    assert result['exit_code']==1 and counts[322]>=2
    assert result['health']['state']=='unknown'
    assert not any(row['id']=='chrome.health' and row['state']=='ok' for row in result['results'])


def test_ala29_foreign_flat_profile_is_blocked_without_touching_other_process_or_lock(setup):
    config,paths,host,home=user_fixture(setup)
    process=paths.at('/proc/999');process.mkdir(parents=True)
    (process/'status').write_text(f'Uid:\t{os.getuid()}\n')
    arguments=['/usr/bin/google-chrome-stable','--user-data-dir='+str(display._profile(home)),'--start-fullscreen']
    _ala29_flat_process(paths,999,arguments=arguments)
    profile=display._profile(home);profile.mkdir(parents=True)
    lock=profile/'SingletonLock';lock.symlink_to('host-999')
    result=display.user_action('start',config=config,home=home,host=host,paths=paths)
    assert result['exit_code']==1 and result['state']=='blocked'
    assert lock.is_symlink() and (process/'cmdline').exists()
    assert not _started_commands(host)
    assert not any(argv[0]=='pkill' or 'reset-failed' in argv for argv,_ in host.calls)


def test_ala29_unrelated_ambiguous_title_is_not_claimed_as_display_profile_owner(setup):
    config,paths,host,home=user_fixture(setup)
    process=paths.at('/proc/999');process.mkdir(parents=True)
    (process/'status').write_text(f'Uid:\t{os.getuid()}\n')
    (process/'cmdline').write_bytes(b'not-chrome unrelated-title-with-spaces\0')
    (process/'exe').symlink_to('/usr/bin/python3')
    assert display._foreign_profile_pid(paths,display._profile(home)) is None


@pytest.mark.parametrize('condition',['different_profile','profile_text_inside_url'])
def test_ala29_foreign_detection_requires_an_exact_profile_switch_boundary(setup,condition):
    config,paths,host,home=user_fixture(setup)
    process=paths.at('/proc/999');process.mkdir(parents=True)
    (process/'status').write_text(f'Uid:\t{os.getuid()}\n')
    target=str(display._profile(home))
    arguments=['/usr/bin/google-chrome-stable','--user-data-dir='+target+'-other']
    if condition=='profile_text_inside_url':
        arguments.append('https://example.com/?value=--user-data-dir='+target)
    _ala29_flat_process(paths,999,arguments=arguments)
    assert display._foreign_profile_pid(paths,display._profile(home)) is None


def test_ala29_unsupported_foreign_title_with_display_profile_evidence_blocks_launch(setup):
    config,paths,host,home=user_fixture(setup)
    process=paths.at('/proc/999');process.mkdir(parents=True)
    (process/'status').write_text(f'Uid:\t{os.getuid()}\n')
    arguments=['/usr/bin/google-chrome-stable','--user-data-dir='+str(display._profile(home)),'--start-fullscreen']
    _ala29_flat_process(paths,999,arguments=arguments)
    (process/'cmdline').write_bytes((process/'cmdline').read_bytes()+b'unrecognized-residual\0')
    try:
        result=display.user_action('start',config=config,home=home,host=host,paths=paths)
    except ValueError:
        # A read-only ambiguity is safe if the privileged RPC rejects the action.
        pass
    else:
        assert result['exit_code']==1 and result['state'] in {'unknown','blocked'}
    assert not _started_commands(host)
    assert not any(argv[0]=='pkill' or 'reset-failed' in argv for argv,_ in host.calls)
