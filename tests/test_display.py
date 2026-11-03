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
        self.script_result={'exit_code':2, 'state':'pending', 'reason':'Chrome 미실행'}
        self.get_default='graphical.target'
        self.gdm_enabled='enabled'
        self.locked='no'
        self.gdm_status=Command(0,'LoadState=loaded\nActiveState=active\nSubState=running\nResult=success\n')
        self.gdm_logs=Command(0,'')
        self.real_user_check=False
        self.process_on_start=None
        self.show_failures={}
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
                display._prepare_user_files(self.config,self.paths.at('/home/user'))
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
    assert any(r.id=='display.session' and r.state=='pending' for r in result)


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


def test_missing_url_is_input_pending_not_placeholder(setup):
    config,paths,host=setup
    empty=replace(config,url=None)
    assert display.chrome_action('start',empty,host,paths)['exit_code']==2
    assert any(r.id=='chrome.url' and r.state=='pending' for r in display.inspect_display(empty,host,paths))


def test_unit_uses_graphical_session_and_bounded_crash_recovery():
    unit=display.unit_text()
    assert 'PartOf=graphical-session.target' in unit
    assert 'Restart=always' in unit
    assert 'RestartSec=5' in unit
    assert 'StartLimitIntervalSec=300' in unit
    assert 'StartLimitBurst=5' in unit
    assert '_chrome-run' in unit


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
    assert states['display.session']=='pending'
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
    assert states['display.session']=='ok'
    assert states['chrome.runtime']=='not_applicable'
    assert states['display.visual_acceptance']=='not_applicable'
    assert not any(argv[0]=='runuser' and argv[-1]=='check' for argv,_ in host.calls)
    assert any(argv[0]=='runuser' and argv[-1]=='settings' for argv,_ in host.calls)


def test_missing_url_prepares_desktop_but_keeps_only_browser_input_pending(setup):
    config,paths,host=setup
    config=replace(config,url=None);host.config=config
    states={r.id:r.state for r in display.apply_display(config,host,paths)}
    assert states['display.configuration']=='ok'
    assert states['display.session']=='ok'
    assert states['chrome.url']=='pending'
    assert states['chrome.runtime']=='not_applicable'
    assert not any(argv[0]=='runuser' and argv[-1]=='check' for argv,_ in host.calls)


@pytest.mark.parametrize('locked,state',[('yes','pending'),('no','ok'),('','unknown')])
def test_actual_locked_session_is_separate_from_policy(setup,locked,state):
    config,paths,host=setup
    host.locked=locked
    results={r.id:r for r in display.apply_display(config,host,paths)}
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
        (process/'status').write_text(f'Uid:\t{os.getuid()}\n')
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


def test_supervisor_cannot_claim_unrelated_child_cgroup_is_healthy(setup):
    config,paths,host,home=user_fixture(setup)
    _seed_supervised_display(config,paths,host)
    paths.at('/proc/322/cgroup').write_text('0::/other.service\n')
    result=display.user_action('check',config=config,home=home,host=host,paths=paths)
    assert result['exit_code']==1
    assert any(r['id']=='chrome.process' and r['state']=='unknown' for r in result['results'])


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
    assert '"/usr/local/bin/ala" "_chrome-run"' in first[0]
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


def test_apply_starts_inactive_chrome_once_and_rechecks_health(setup):
    config,paths,host=setup
    host.real_user_check=True
    host.process_on_start=lambda:_seed_supervised_display(config,paths,host)
    result=display.apply_display(config,host,paths)
    assert _started_commands(host)==[['systemctl','--user','start','--no-block',display.UNIT]]
    assert Report(result).exit_code==0
    assert any(r.id=='chrome.option.start-fullscreen' and r.state=='ok' for r in result)
    before={str(p):(p.read_bytes(),p.stat().st_mtime_ns) for p in paths.root.rglob('*') if p.is_file()}
    display.apply_display(config,host,paths)
    assert len(_started_commands(host))==1
    assert before=={str(p):(p.read_bytes(),p.stat().st_mtime_ns) for p in paths.root.rglob('*') if p.is_file()}
    assert not any('reset-failed' in argv or argv[:3]==['systemctl','--user','stop'] for argv,_ in host.calls)


def test_apply_does_not_claim_async_start_is_running(setup):
    config,paths,host=setup
    host.real_user_check=True
    # systemctl accepted the request but has no Chrome PID yet.
    result=display.apply_display(config,host,paths)
    assert len(_started_commands(host))==1
    assert Report(result).exit_code==2
    assert any(r.id=='chrome.runtime' and r.state=='pending' for r in result)


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
    if condition=='no_bus':
        assert result['chrome.runtime'].state=='pending'
        assert 'GNOME' in result['chrome.runtime'].next_action
    if condition=='no_url':assert result['chrome.url'].state=='pending'
    if condition=='no_session':assert result['display.session'].state=='pending'
    if condition=='locked':assert result['display.session_lock'].state=='pending'
    if condition=='multiple_sessions':assert result['display.session'].state=='blocked'


def test_apply_waits_for_missing_manager_environment_without_failure(setup):
    config,paths,host=setup
    host.manager='XDG_CURRENT_DESKTOP=GNOME\n'
    host.real_user_check=True
    result={r.id:r for r in display.apply_display(config,host,paths)}
    assert result['chrome.autostart'].state=='ok'
    assert result['chrome.runtime'].state=='pending'
    assert '화면 환경' in result['chrome.runtime'].actual
    assert 'GNOME' in result['chrome.runtime'].next_action
    assert not _started_commands(host)
    assert not any(r.state in {'blocked','error'} for r in result.values())


def test_apply_preserves_legacy_running_service_without_migration_restart(setup):
    config,paths,host=setup
    host.states[display.LEGACY]['ActiveState']='active'
    host.real_user_check=True
    result=display.apply_display(config,host,paths)
    assert host.states[display.LEGACY]['ActiveState']=='active'
    assert host.states[display.UNIT]['ActiveState']=='inactive'
    assert any(r.id=='chrome.legacy' and r.state=='pending' for r in result)
    assert not _started_commands(host)
    assert not any('reset-failed' in argv or argv[:3]==['systemctl','--user','stop'] for argv,_ in host.calls)


def test_apply_preserves_failed_start_limit_and_failure_reason(setup):
    config,paths,host=setup
    host.states[display.UNIT].update(ActiveState='failed',Result='start-limit-hit')
    host.real_user_check=True
    result=display.apply_display(config,host,paths)
    assert host.states[display.UNIT]['ActiveState']=='failed'
    assert any(r.id=='chrome.runtime.ensure_start' and r.state=='error' and 'start-limit-hit' in r.actual for r in result)
    assert Report(result).exit_code==1
    assert not _started_commands(host)
    assert not any('reset-failed' in argv or argv[:3]==['systemctl','--user','stop'] for argv,_ in host.calls)


@pytest.mark.parametrize('unit',[display.UNIT,display.LEGACY])
@pytest.mark.parametrize('response',[Command(124,'','timeout'),Command(0),
                                     Command(1,'ActiveState=inactive\nLoadState=loaded\n','denied')])
def test_ensure_start_does_not_infer_inactive_from_failed_service_query(setup,unit,response):
    config,paths,host=setup
    host.show_failures[unit]=response
    host.real_user_check=True
    result=display.apply_display(config,host,paths)
    assert any(r.id=='chrome.runtime.ensure_start' and r.state=='unknown' for r in result)
    assert Report(result).exit_code==1
    assert not _started_commands(host)
    assert not any('reset-failed' in argv or argv[:3]==['systemctl','--user','stop'] for argv,_ in host.calls)


def test_structured_absence_of_legacy_unit_allows_new_start(setup):
    config,paths,host=setup
    host.show_failures[display.LEGACY]=Command(1,'ActiveState=inactive\nLoadState=not-found\nMainPID=0\n','unit not found')
    result=display.apply_display(config,host,paths)
    assert len(_started_commands(host))==1
    assert not any(r.id=='chrome.runtime.ensure_start' and r.state=='unknown' for r in result)


def test_ensure_start_rejects_partial_active_response_before_kept_branch(setup):
    config,paths,host=setup
    host.show_failures[display.UNIT]=Command(1,'ActiveState=active\nLoadState=loaded\nMainPID=0\n','denied')
    value=display.user_action('ensure-start',config=config,home=paths.at('/home/user'),host=host,paths=paths)
    assert value['state']=='unknown' and value['exit_code']==1
    assert not _started_commands(host)
    assert not any('reset-failed' in argv or argv[:3]==['systemctl','--user','stop'] for argv,_ in host.calls)


def test_apply_preserves_foreign_profile_and_reports_start_blocker(setup):
    config,paths,host=setup
    _seed_display_process(config,paths,host,'999')
    host.states[display.UNIT].update(ActiveState='inactive',MainPID='0')
    profile=display._profile(paths.at('/home/user'))
    profile.mkdir(parents=True)
    lock=profile/'SingletonLock';lock.symlink_to('host-999')
    result=display.apply_display(config,host,paths)
    assert any(r.id=='chrome.runtime.ensure_start' and r.state=='blocked' and '999' in r.actual for r in result)
    assert lock.is_symlink()
    assert not _started_commands(host)
    assert not any(argv[0]=='pkill' or 'reset-failed' in argv or argv[:3]==['systemctl','--user','stop'] for argv,_ in host.calls)


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
