"""Synthetic per-user workspace policy; never edits the host desktop."""
from dataclasses import replace

import pytest

from ala import display
from ala.config import Config
from ala.system import Command, Paths


BUS = {'DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/test/bus'}
DYNAMIC = ('org.gnome.mutter', 'dynamic-workspaces')
COUNT = ('org.gnome.desktop.wm.preferences', 'num-workspaces')


class SettingsHost:
    def __init__(self):
        self.calls = []
        self.values = {DYNAMIC:'true', COUNT:'4',
                       ('org.gnome.mutter','workspaces-only-on-primary'):'false',
                       ('org.gnome.desktop.wm.preferences','focus-mode'):"'click'",
                       ('org.gnome.shell','disable-user-extensions'):'false',
                       ('org.gnome.shell','enabled-extensions'):"['ala-display@example.com']"}
        self.replies = {}
        self.ignore_writes = False
        self.fail_read_after_write = False

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        if tuple(argv) in self.replies:
            return self.replies[tuple(argv)]
        if argv==['systemctl','--user','daemon-reload']:
            return Command(0)
        assert argv[0] == 'gsettings'
        key = tuple(argv[2:4])
        if argv[1] == 'get':
            if self.fail_read_after_write and any(call[1]=='set' for call in self.calls):
                return Command(1, '', 'synthetic unavailable')
            return Command(0, self.values[key]+'\n')
        if argv[1] == 'writable':
            return Command(0, 'true\n')
        if argv[1] == 'set':
            if not self.ignore_writes:
                self.values[key] = argv[4]
            return Command(0)
        raise AssertionError(argv)


@pytest.fixture
def policy():
    return Config(user='user'), SettingsHost()


def sets(host):
    return [call for call in host.calls if call[:2]==['gsettings','set']]


def test_readonly_policy_reports_required_changes_without_writes(policy):
    config, host = policy
    before = host.values.copy()
    value = display._workspace_policy(config, host)
    assert value['state']=='change_required' and not value['changed']
    assert host.values==before
    assert host.calls==[['gsettings','get',*DYNAMIC], ['gsettings','get',*COUNT]]
    checks = {result.id:result for result in display._workspace_checks(value)}
    assert checks['display.workspace.dynamic-workspaces'].actual=='true'
    assert checks['display.workspace.num-workspaces'].actual=='4'
    assert all(result.state=='change_required' for result in checks.values())


def test_policy_applies_only_two_user_keys_and_is_idempotent(policy):
    config, host = policy
    unrelated = {key:value for key,value in host.values.items() if key not in {DYNAMIC,COUNT}}
    first = display._workspace_policy(config, host, apply=True, environment=BUS)
    assert first['state']=='ok' and first['changed']
    assert sets(host)==[['gsettings','set',*DYNAMIC,'false'], ['gsettings','set',*COUNT,'1']]
    assert host.calls.index(['gsettings','writable',*COUNT]) < host.calls.index(sets(host)[0])
    assert {key:host.values[key] for key in unrelated}==unrelated
    host.calls.clear()
    second = display._workspace_policy(config, host, apply=True, environment=BUS)
    assert second['state']=='ok' and not second['changed']
    assert not sets(host)
    assert not any(call[1]=='writable' for call in host.calls)


def test_only_different_key_is_written(policy):
    config, host = policy
    host.values[DYNAMIC]='false'
    assert display._workspace_policy(config,host,apply=True,environment=BUS)['state']=='ok'
    assert sets(host)==[['gsettings','set',*COUNT,'1']]


def test_missing_actual_user_bus_defers_writes_until_normal_login(policy):
    config, host = policy
    value=display._workspace_policy(config,host,apply=True,environment={})
    assert value['state']=='pending' and value['exit_code']==2
    assert 'GNOME' in value['next_action']
    assert not sets(host)
    assert display._workspace_policy(config,host,apply=True,environment=BUS)['state']=='ok'


@pytest.mark.parametrize('apply',[False,True])
def test_display_disabled_preserves_existing_workspace_preferences(policy,apply):
    config, host = policy
    before=host.values.copy()
    value=display._workspace_policy(replace(config,display=False),host,apply=apply,environment=BUS)
    assert value['state']=='not_applicable' and value['exit_code']==0
    assert not host.calls and host.values==before


@pytest.mark.parametrize('key',[DYNAMIC,COUNT])
def test_missing_schema_or_key_is_unknown_and_never_written(policy,key):
    config, host = policy
    host.replies[('gsettings','get',*key)]=Command(1,'','synthetic schema missing')
    value=display._workspace_policy(config,host,apply=True,environment=BUS)
    assert value['state']=='unknown' and not sets(host)
    checks=display._workspace_checks(value)
    assert next(check for check in checks if check.id.endswith(key[1])).state=='unknown'


@pytest.mark.parametrize('reply,state',[(Command(0,'false\n'),'blocked'),
                                      (Command(1,'','synthetic denied'),'unknown'),
                                      (Command(0,'unverified\n'),'unknown')])
@pytest.mark.parametrize('key',[DYNAMIC,COUNT])
def test_preflight_protects_both_keys_before_any_change(policy,key,reply,state):
    config, host = policy
    before=host.values.copy()
    host.replies[('gsettings','writable',*key)]=reply
    value=display._workspace_policy(config,host,apply=True,environment=BUS)
    assert value['state']==state and not sets(host)
    assert host.values==before


def test_partial_write_failure_reports_actual_values_for_retry(policy):
    config, host = policy
    host.replies[('gsettings','set',*COUNT,'1')]=Command(1,'','synthetic denied')
    value=display._workspace_policy(config,host,apply=True,environment=BUS)
    assert value['state']=='error' and value['changed']
    assert value['settings']=={'.'.join(DYNAMIC):'false', '.'.join(COUNT):'4'}
    host.replies.clear();host.calls.clear()
    assert display._workspace_policy(config,host,apply=True,environment=BUS)['state']=='ok'
    assert sets(host)==[['gsettings','set',*COUNT,'1']]


@pytest.mark.parametrize('condition,state',[('unconfirmed','pending'),('unreadable','unknown')])
def test_successful_command_requires_settings_readback(policy,condition,state):
    config, host = policy
    host.ignore_writes=condition=='unconfirmed'
    host.fail_read_after_write=condition=='unreadable'
    value=display._workspace_policy(config,host,apply=True,environment=BUS)
    assert value['state']==state and value['exit_code']!=0


def test_workspace_rpc_is_readonly_without_a_live_session(policy,tmp_path,monkeypatch):
    config, host = policy
    monkeypatch.setattr(display.os,'getuid',lambda:1000)
    value=display.user_action('workspace-settings',config=config,home=tmp_path,host=host,paths=Paths(tmp_path))
    assert value['state']=='change_required'
    assert not sets(host) and all(call[:2]==['gsettings','get'] for call in host.calls)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('bus',[False,True])
def test_prepare_records_workspace_result_without_coupling_chrome_registration(policy,tmp_path,monkeypatch,bus):
    config, host = policy
    paths=Paths(tmp_path)
    home=paths.at('/home/user');home.mkdir(parents=True)
    monkeypatch.setattr(display.os,'getuid',lambda:1000)
    if bus:
        monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS',BUS['DBUS_SESSION_BUS_ADDRESS'])
    else:
        monkeypatch.delenv('DBUS_SESSION_BUS_ADDRESS',raising=False)
    value=display.user_action('prepare',config=config,home=home,host=host,paths=paths)
    assert value['state']=='ok' and value['exit_code']==0
    assert value['workspace_policy']['state']==('ok' if bus else 'pending')
    assert bool(sets(host))==bus
    assert (home/'.config/gnome-initial-setup-done').is_file()
    assert all(call[0] in {'gsettings','systemctl'} for call in host.calls)
