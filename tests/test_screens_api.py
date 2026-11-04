import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ala.cli import main
from ala.config import Config
from ala.service import Operations
from ala.system import Command, Paths, atomic_json


@pytest.fixture
def operations(tmp_path):
    paths = Paths(tmp_path)
    paths.config.parent.mkdir(parents=True)
    atomic_json(paths.config, Config(user='user', url='https://example.com/display').to_dict(), 0o644)
    return Operations(paths=paths)


def screen(**extra):
    return {'id': 'default', 'url': 'https://example.com/new', 'output': 'HDMI-1',
            'layout': 'fullscreen', 'autostart': True,
            'frontend': {'enabled': False, 'manifest_url': None}, **extra}


def test_explicit_screen_save_migrates_schema_but_preserves_other_settings(operations):
    expected = operations.config().to_dict()
    result = operations.dispatch('screens-save', {'target': screen(), 'expected_config': expected})
    value = json.loads(operations.paths.config.read_text())
    assert result['saved'] and result['state'] == 'pending'
    assert value['schema_version'] == 2 and value['screens'][0]['output'] == 'HDMI-1'
    assert value['timezone'] == expected['timezone']
    assert not list(operations.paths.root.rglob('*.service'))


def test_stale_editor_cannot_overwrite_a_newer_configuration(operations):
    before = operations.paths.config.read_bytes()
    expected = operations.config().to_dict()
    expected['timezone'] = 'UTC'
    with pytest.raises(RuntimeError, match='구성이 변경'):
        operations.dispatch('screens-save', {'target': screen(), 'expected_config': expected})
    assert operations.paths.config.read_bytes() == before


@pytest.mark.parametrize('value', [screen(id='../escape'), screen(url='file:///etc/passwd'),
                                 screen(output='HDMI-1; false'), screen(layout='command'),
                                 screen(frontend={'enabled': True, 'manifest_url': 'https://example.com/?token=fake'})])
def test_invalid_screen_data_never_changes_configuration(operations, value):
    before = operations.paths.config.read_bytes()
    with pytest.raises((ValueError, TypeError)):
        operations.dispatch('screens-save', {'target': value, 'expected_config': operations.config().to_dict()})
    assert operations.paths.config.read_bytes() == before


class CliService:
    def __init__(self):
        self.calls = []

    def authenticate(self):
        self.calls.append(('authenticate', None))

    def request(self, action, payload=None):
        self.calls.append((action, payload))
        return {'exit_code': 0, 'targets': [], 'state': 'ok'}


def test_readonly_display_cli_uses_shared_inventory_and_clean_json(capsys):
    svc = CliService()
    assert main(['display', 'list', '--json'], service=svc) == 0
    assert json.loads(capsys.readouterr().out)['targets'] == []
    assert svc.calls == [('authenticate', None), ('screens-inspect', None)]


def test_targeted_reload_does_not_become_a_shell_command(capsys):
    svc = CliService()
    assert main(['chrome', 'reload', '--target', 'display-2', '--json'], service=svc) == 0
    assert svc.calls[-1] == ('chrome', {'action': 'reload', 'target': 'display-2'})
    json.loads(capsys.readouterr().out)


def test_old_default_chrome_cli_payload_remains_compatible(capsys):
    svc = CliService()
    assert main(['chrome', 'status', '--json'], service=svc) == 0
    assert svc.calls[-1] == ('chrome', {'action': 'status'})


class RuntimeHost:
    """Never execute host commands while exercising real persisted selections."""
    def __init__(self):
        self.calls = []

    def run(self, args, **kwargs):
        self.calls.append(list(args))
        return Command(1)


@pytest.fixture
def runtime_operations(tmp_path, monkeypatch):
    from ala import runtime, service

    paths = Paths(tmp_path)
    boot = paths.at('/proc/sys/kernel/random/boot_id')
    boot.parent.mkdir(parents=True)
    boot.write_text('synthetic-boot')
    home = tmp_path / 'home/user'
    home.mkdir(parents=True)
    monkeypatch.setattr(service.pwd, 'getpwnam', lambda name: SimpleNamespace(
        pw_uid=1000 if name == 'user' else 1001, pw_dir=str(home)))
    monkeypatch.setenv('SUDO_USER', 'user')
    monkeypatch.setattr(runtime.RuntimeStore, '_session', lambda *_: 'synthetic-session')
    releases = []
    for version in ('1.0.0', '2.0.0'):
        release = paths.at('/opt/ala/releases') / version
        (release / '.venv/bin').mkdir(parents=True)
        (release / 'VERSION').write_text(version)
        (release / 'entry.py').write_text('# synthetic release\n')
        (release / '.venv/bin/python').write_text('# synthetic interpreter\n')
        (release / 'COMPATIBILITY.json').write_text(json.dumps({
            'schema_version': 1, 'runtime_protocol': 1, 'config_schemas': [1, 2]}))
        releases.append(release)
    host = RuntimeHost()
    operations = Operations(paths=paths, host=host, release=releases[1])
    return operations, runtime.RuntimeStore(paths, host), releases


def selection_config(selection):
    return json.loads(Path(selection['config_path']).read_text())


def save_initial_config(operations, config):
    operations.paths.config.parent.mkdir(parents=True)
    atomic_json(operations.paths.config, config.to_dict(), 0o644)


def test_screen_save_does_not_stage_unapplied_settings_for_nightly(runtime_operations, monkeypatch):
    from ala import runtime

    operations, store, (old, _) = runtime_operations
    before = Config(user='user', url='https://example.com/display?view=previous')
    save_initial_config(operations, before)
    original = store.bootstrap('default', old, before)
    saved = screen(url='https://example.com/display?view=updated', output=None,
                   frontend={'enabled': True, 'manifest_url': 'https://example.com/manifest.json'})

    operations.dispatch('screens-save', {'target': saved, 'expected_config': before.to_dict()})

    state = store.load('default')
    assert state['committed'] == original['committed']
    assert state['desired'] == original['desired']
    assert runtime.apply_target('default', paths=operations.paths, host=operations.host)['state'] == 'kept'
    assert not operations.host.calls

    monkeypatch.setattr('ala.screens.inspect_screens', lambda *_args, **_kwargs: {'targets': [dict(saved)]})
    target = operations.dispatch('screens-inspect', {})['targets'][0]
    assert target['url'] == saved['url'] and target['applied_url'] == before.url
    assert target['frontend']['enabled'] and not target['applied_frontend']['enabled']
    assert target['settings']['pending'] and target['settings']['state'] == 'pending'


def test_screen_save_preserves_global_settings_if_runtime_write_fails(runtime_operations, monkeypatch):
    from ala.runtime import RuntimeStore

    operations, store, (old, _) = runtime_operations
    before = Config(user='user', url='https://example.com/previous')
    save_initial_config(operations, before)
    store.bootstrap('default', old, before)
    before_bytes = operations.paths.config.read_bytes()

    def fail(*_args):
        raise OSError('fixture storage failure')

    monkeypatch.setattr(RuntimeStore, 'settings_saved', fail)
    with pytest.raises(OSError):
        operations.dispatch('screens-save', {'target': screen(), 'expected_config': before.to_dict()})
    assert operations.paths.config.read_bytes() == before_bytes


def test_config_import_stages_new_configuration_without_moving_committed_runtime(runtime_operations):
    operations, store, (old, new) = runtime_operations
    before = Config(user='user', url='https://example.com/previous')
    after = Config(user='user', url='https://example.com/current')
    save_initial_config(operations, before)
    original = store.bootstrap('default', old, before)['committed']

    assert operations.dispatch('config-save', after.to_dict())['saved']

    state = store.load('default')
    assert state['committed'] == original
    assert selection_config(state['committed']) == before.to_dict()
    assert selection_config(state['desired']) == after.to_dict()
    assert state['desired']['release'] == str(new)
    assert not operations.host.calls  # Saving configuration does not start a window.


def test_first_config_save_selects_the_new_url_instead_of_an_unsaved_empty_default(runtime_operations):
    operations, store, (_, current) = runtime_operations
    assert not operations.paths.config.exists()
    assert operations.config().url is None
    after = Config(user='user', url='https://example.com/first-display')

    operations.dispatch('config-save', after.to_dict())

    state = store.load('default')
    assert state is not None
    assert state['committed'] == state['desired']
    assert selection_config(state['committed']) == after.to_dict()
    assert state['committed']['release'] == str(current)
    assert not operations.host.calls


def test_explicit_save_enrolls_an_unchanged_first_configuration_without_rewriting_it(runtime_operations):
    operations, store, (_, current) = runtime_operations
    config = Config(user='user', url='https://example.com/first-display')
    save_initial_config(operations, config)
    config_info = operations.paths.config.stat()

    operations.dispatch('config-save', config.to_dict())

    state = store.load('default')
    assert state is not None
    assert state['committed'] == state['desired']
    assert selection_config(state['committed']) == config.to_dict()
    assert state['committed']['release'] == str(current)
    assert operations.paths.config.stat().st_mtime_ns == config_info.st_mtime_ns
    assert not operations.host.calls


def test_config_import_keeps_manual_stop_intent_while_staging_the_next_configuration(runtime_operations):
    operations, store, (old, _) = runtime_operations
    before = Config(user='user', url='https://example.com/previous')
    save_initial_config(operations, before)
    original = store.bootstrap('default', old, before)['committed']
    stopped = store.cancel('default', stopped=True)
    after = Config(user='user', url='https://example.com/current')

    operations.dispatch('config-save', after.to_dict())

    state = store.load('default')
    assert state['committed'] == original
    assert selection_config(state['desired']) == after.to_dict()
    assert state['stopped'] is True and store.is_stopped(state)
    assert state['stop_boot_id'] == stopped['stop_boot_id']
    assert state['stop_session_id'] == stopped['stop_session_id']
    assert not operations.host.calls


def test_config_import_prepares_a_new_target_but_preserves_existing_committed_choice(runtime_operations):
    operations, store, (old, current) = runtime_operations
    before = Config(user='user', url='https://example.com/previous')
    save_initial_config(operations, before)
    original = store.bootstrap('default', old, before)['committed']
    after = Config.from_dict({**before.to_dict(), 'schema_version': 2,
                              'screens': [screen(), screen(id='display-2', url='https://example.com/second', output='DP-1')]})

    operations.dispatch('config-save', after.to_dict())

    existing, added = store.load('default'), store.load('display-2')
    assert existing['committed'] == original
    assert selection_config(existing['desired']) == after.to_dict()
    assert added is not None and added['committed'] == added['desired']
    assert selection_config(added['committed']) == after.to_dict()
    assert added['committed']['release'] == str(current)
    assert not operations.host.calls


def test_config_save_conflicting_with_reserved_work_preserves_the_saved_configuration(runtime_operations):
    operations, store, (old, _) = runtime_operations
    before = Config(user='user', url='https://example.com/previous')
    save_initial_config(operations, before)
    store.bootstrap('default', old, before)
    token = store.reserve('default', 'frontend')
    config_bytes = operations.paths.config.read_bytes()
    state_bytes = store.state_path('default').read_bytes()

    with pytest.raises(RuntimeError):
        operations.dispatch('config-save', Config(user='user', url='https://example.com/current').to_dict())

    assert operations.paths.config.read_bytes() == config_bytes
    assert store.state_path('default').read_bytes() == state_bytes
    assert store.valid(token)


def test_config_import_cannot_change_the_account_of_a_selected_runtime(runtime_operations):
    operations, store, (old, _) = runtime_operations
    before = Config(user='user', url='https://example.com/previous')
    save_initial_config(operations, before)
    store.bootstrap('default', old, before)
    config_bytes = operations.paths.config.read_bytes()
    state_bytes = store.state_path('default').read_bytes()

    with pytest.raises((ValueError, RuntimeError)):
        operations.dispatch('config-save', Config(user='other_user', url=before.url).to_dict())

    assert operations.paths.config.read_bytes() == config_bytes
    assert store.state_path('default').read_bytes() == state_bytes
    assert not operations.host.calls


def test_config_import_cannot_remove_a_selected_target_from_the_control_inventory(runtime_operations):
    operations, store, (old, _) = runtime_operations
    before = Config.from_dict({'schema_version': 2, 'user': 'user',
                               'screens': [screen(), screen(id='display-2', output='DP-1')]})
    save_initial_config(operations, before)
    for target in before.screen_targets():
        store.bootstrap(target.id, old, before)
    config_bytes = operations.paths.config.read_bytes()
    states = {target.id: store.state_path(target.id).read_bytes() for target in before.screen_targets()}
    after = Config.from_dict({**before.to_dict(), 'screens': [screen()]})

    with pytest.raises((ValueError, RuntimeError)):
        operations.dispatch('config-save', after.to_dict())

    assert operations.paths.config.read_bytes() == config_bytes
    assert {target: store.state_path(target).read_bytes() for target in states} == states
    assert not operations.host.calls



def test_quick_login_log_reads_current_boot_and_keeps_only_categories(operations, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[0] == 'systemctl':
            return Command(0, stdout='LoadState=loaded\nActiveState=active\n')
        return Command(0, stdout='authentication failure with private metadata')

    monkeypatch.setattr(operations.host, 'run', run)
    value = operations.dispatch('screens-login-logs', {'target': 'default'})
    assert calls[1][:4] == ['journalctl', '-b', '-u', 'gdm.service']
    assert '로그인·세션 시작 오류 단서' in value['log']
    assert 'private metadata' not in value['log']
