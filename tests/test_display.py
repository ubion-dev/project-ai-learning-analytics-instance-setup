from dataclasses import replace
from pathlib import Path
import json
import os
import urllib.error
import pytest

from ala.config import Config
from ala.system import Command, Paths
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
    def run(self, argv, **kwargs):
        self.calls.append((argv,kwargs))
        if argv[:2]==['getent','passwd']:
            return Command(0,f'user:x:{os.getuid()}:{os.getgid()}::/home/user:/bin/bash\n')
        if argv[:2]==['loginctl','list-sessions']:
            return Command(0,''.join(f'{i+1} {os.getuid()} user seat0\n' for i in range(self.sessions)))
        if argv[:2]==['loginctl','show-session']:
            return Command(0,f'User={os.getuid()}\nType=wayland\nClass=user\nRemote=no\nActive=yes\nState=active\n')
        if argv[:2]==['loginctl','show-user']:
            return Command(0,'/run/user/test\n') if self.runtime else Command(1,'','no runtime')
        if argv[0]=='runuser':
            if argv[-1]=='prepare':
                display._prepare_user_files(self.config,self.paths.at('/home/user'))
                return Command(0,json.dumps({'exit_code':0,'state':'ok'}))
            if argv[-1]=='settings':
                return Command(0,json.dumps({'exit_code':0,'settings':self.settings}))
            return Command(0,json.dumps(self.script_result))
        if argv==['systemctl','is-enabled','gdm.service']:
            return Command(0,self.gdm_enabled+'\n')
        if argv==['systemctl','get-default']:
            return Command(0,self.get_default+'\n')
        if argv[:3]==['systemctl','--user','show-environment']:
            return Command(0,self.manager)
        if argv[:3]==['systemctl','--user','show']:
            return Command(0,'\n'.join(f'{key}={value}' for key,value in self.states[argv[3]].items()))
        if argv[:3]==['systemctl','--user','stop']:
            self.states[argv[-1]]['ActiveState']='inactive'
            return Command(0)
        if argv[:3]==['systemctl','--user','start']:
            self.states[display.UNIT]['ActiveState']='active'
            return Command(0)
        if argv[:2]==['gsettings','get']:
            name=argv[2]+'.'+argv[3]
            return Command(0,self.settings[name]+'\n')
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
    assert result['exit_code']==0
    assert any(r['id']=='chrome.option.password-store' and r['state']=='ok' for r in result['results'])
    arguments.remove('--password-store=basic')
    (process/'cmdline').write_bytes(('\0'.join(arguments)+'\0').encode())
    assert display.user_action('check',config=config,home=home,host=host,paths=paths)['exit_code']==1
    host.states[display.UNIT].update(ActiveState='failed',Result='start-limit-hit')
    result=display.user_action('check',config=config,home=home,host=host,paths=paths)
    assert result['state']=='error'
    assert result['service']['Result']=='start-limit-hit'


def test_readiness_retries_then_executes_exact_url_and_new_profile(setup):
    config,paths,host,home=user_fixture(setup)
    calls=[];sleep=[];executed=[]
    class Response:
        status=200
        def __enter__(self):return self
        def __exit__(self,*args):pass
    def opener(request,timeout):
        calls.append((request.full_url,timeout))
        if len(calls)<3:raise urllib.error.URLError('not ready')
        return Response()
    assert display.run_chrome(config=config,home=home,host=host,paths=paths,sleeper=sleep.append,opener=opener,execute=executed.append)==0
    assert sleep==[10,10]
    assert all(timeout==5 for _,timeout in calls)
    args=executed[0]
    assert args[-1]==config.url
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
        display.run_chrome(config=config,home=home,host=host,paths=paths,execute=lambda args:None)
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
