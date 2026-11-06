"""Global login registration and local setup-page lifecycle boundaries."""
from dataclasses import replace
import json
import shutil

import pytest

from ala import display, display_health
from ala.config import Config, ScreenConfig
from ala.display_onboarding import pending_for_user, _revision
from ala.runtime import RuntimeStore
from ala.system import Command, atomic_json
from test_display import setup, user_fixture, _seed_supervised_display


@pytest.mark.parametrize('action', ['prepare', 'autostart-sync', 'identify-outputs'])
def test_global_user_actions_load_global_config_even_without_a_default_target(setup, monkeypatch, action):
    _, paths, host, home = user_fixture(setup)
    config = Config(schema_version=2, user='user', screens=(ScreenConfig(id='screen-a', output='HDMI-1'),))
    loaded = []

    def load(actual_paths, target='default'):
        assert actual_paths is paths
        loaded.append(target)
        return config, home

    monkeypatch.setattr(display, '_load_user_config', load)
    monkeypatch.setattr(display, '_context', lambda *args: pytest.fail('session-wide actions must not select a DID'))
    monkeypatch.setattr('ala.monitors.identify_outputs', lambda **kwargs: {'state': 'ok', 'duration_seconds': 10})
    result = display.user_action(action, host=host, paths=paths)
    assert result['state'] == 'ok'
    assert loaded == [None]
    assert not hasattr(config, '_target_id')
    assert not any(argv[:3] in (['systemctl', '--user', 'start'], ['systemctl', '--user', 'stop'])
                   for argv, _ in host.calls)


def delegate_sync(monkeypatch, home):
    def call(config, host, paths, action):
        assert action == 'autostart-sync'
        return display.user_action(action, config=config, home=home, host=host, paths=paths)
    monkeypatch.setattr(display, '_user_call', call)


def test_registration_sync_is_idempotent_and_does_not_provision_or_start_chrome(setup, monkeypatch):
    config, paths, host, home = user_fixture(setup)
    config = replace(config, url=None)
    delegate_sync(monkeypatch, home)
    first = display.sync_autostart_registration(config, host, paths)
    desktop = home / '.config/autostart/ala-display.desktop'
    assert first == {'state': 'ok', 'exit_code': 0, 'changed': True}
    before = desktop.stat().st_mtime_ns
    assert display.sync_autostart_registration(config, host, paths)['changed'] is False
    assert desktop.stat().st_mtime_ns == before
    assert desktop.read_text() == display.desktop_text()
    assert not (home / '.config/gnome-initial-setup-done').exists()
    assert not (home / '.config/ala').exists()
    assert not paths.at('/etc/systemd/user').exists()
    assert not paths.at('/etc/gdm/custom.conf').exists()
    assert all(argv[:2] == ['getent', 'passwd'] for argv, _ in host.calls)


@pytest.mark.parametrize('kind', ['file', 'symlink', 'directory'])
def test_registration_sync_preserves_external_entry_and_never_delegates(setup, monkeypatch, kind):
    config, paths, host, home = user_fixture(setup)
    desktop = home / '.config/autostart/ala-display.desktop'
    desktop.parent.mkdir(parents=True)
    external = home / 'external.desktop'
    external.write_text('[Desktop Entry]\nName=External\n')
    if kind == 'file':
        desktop.write_text(external.read_text())
    elif kind == 'symlink':
        desktop.symlink_to(external)
    else:
        desktop.mkdir()
    monkeypatch.setattr(display, '_user_call', lambda *args: pytest.fail('external registration must be preserved'))
    result = display.sync_autostart_registration(config, host, paths)
    assert result['state'] == 'blocked'
    assert external.read_text() == '[Desktop Entry]\nName=External\n'
    assert (desktop.is_symlink() if kind == 'symlink' else desktop.is_dir() if kind == 'directory'
            else desktop.read_text() == external.read_text())


def test_registration_sync_backs_up_managed_changes_and_only_removes_its_entry(setup, monkeypatch):
    config, paths, host, home = user_fixture(setup)
    delegate_sync(monkeypatch, home)
    desktop = home / '.config/autostart/ala-display.desktop'
    desktop.parent.mkdir(parents=True)
    previous = display.desktop_text() + '# prior managed contents\n'
    desktop.write_text(previous)
    other = desktop.parent / 'unrelated.desktop'
    other.write_text('external registration\n')
    assert display.sync_autostart_registration(config, host, paths)['state'] == 'ok'
    assert any(path.read_text() == previous for path in paths.backups.iterdir())
    assert display.sync_autostart_registration(replace(config, autostart=False), host, paths)['state'] == 'ok'
    assert not desktop.exists()
    assert other.read_text() == 'external registration\n'


@pytest.mark.parametrize('entrypoint', ['login', 'chrome'])
@pytest.mark.parametrize('contents', [None, '{', '{}', '{"schema_version":1,"pending":"yes","user":"user"}',
                                     '{"schema_version":true,"pending":false,"user":"user"}'])
def test_pending_or_malformed_initialization_fence_never_launches(setup, entrypoint, contents):
    config, paths, host, home = user_fixture(setup)
    config = replace(config, url=None)
    paths.config.parent.mkdir(parents=True)
    atomic_json(paths.config, config.to_dict(), 0o644)
    fence = paths.at('/var/lib/ala-runtime/display-onboarding.json')
    RuntimeStore(paths)._write(fence, {'schema_version': 1, 'pending': True, 'user': 'user',
                                      'expected_revision': _revision(config.to_dict())})
    if contents is not None:
        try:
            malformed = json.loads(contents)
        except ValueError:
            pass
        else:
            malformed['expected_revision'] = _revision(config.to_dict())
            contents = json.dumps(malformed)
        fence.write_text(contents)
    invoke = (lambda: display.session_start(config=config, home=home, host=host, paths=paths, environment={})) if entrypoint == 'login' else (
        lambda: display.run_chrome(config=config, home=home, host=host, paths=paths,
                                  supervisor_factory=lambda *args, **kwargs: pytest.fail('fenced launch')))
    if contents is None:
        assert invoke() == 78
    else:
        with pytest.raises(ValueError):
            invoke()
    assert [argv for argv, _ in host.calls] == (
        [['systemctl', '--user', 'show-environment'],
         ['gsettings', 'get', 'org.gnome.mutter', 'dynamic-workspaces'],
         ['gsettings', 'get', 'org.gnome.desktop.wm.preferences', 'num-workspaces']]
        if entrypoint == 'login' and contents is None else [])
    assert not (home / '.config/ala').exists()


@pytest.mark.parametrize('autostart', [False, True])
@pytest.mark.parametrize('environment', [
    {'DISPLAY': ':0', 'XDG_SESSION_TYPE': 'x11', 'XDG_CURRENT_DESKTOP': 'GNOME'},
    {'WAYLAND_DISPLAY': 'wayland-0', 'XDG_SESSION_TYPE': 'wayland', 'XDG_CURRENT_DESKTOP': 'GNOME'},
])
def test_pending_first_login_imports_graphical_environment_without_starting_chrome(setup, autostart, environment):
    config, paths, host, home = user_fixture(setup)
    config = replace(config, url=None, autostart=autostart)
    paths.config.parent.mkdir(parents=True)
    atomic_json(paths.config, config.to_dict(), 0o644)
    RuntimeStore(paths)._write(paths.at('/var/lib/ala-runtime/display-onboarding.json'), {
        'schema_version': 1, 'pending': True, 'user': 'user', 'expected_revision': _revision(config.to_dict()),
    })
    host.manager = ''
    result = display.session_start(config=config, home=home, host=host, paths=paths, environment=environment)
    assert result == 78
    commands = [argv for argv, _ in host.calls]
    assert commands[0] == ['systemctl', '--user', 'show-environment']
    cleared = ['systemctl', '--user', 'unset-environment', 'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY']
    assert cleared in commands
    imports = [(argv, options) for argv, options in host.calls if 'import-environment' in argv]
    assert len(imports) == 1
    arguments, options = imports[0]
    assert set(arguments[3:]) == set(environment)
    assert all(options['env'][key] == value for key, value in environment.items())
    assert commands.index(cleared) < commands.index(arguments)
    assert not any(argv[:3] in (['systemctl', '--user', 'start'], ['systemctl', '--user', 'stop'])
                   or argv[0] == 'gdbus' for argv in commands)
    assert not (home / '.config/ala').exists()


@pytest.mark.parametrize('target', ['default', 'screen-a'])
def test_startup_status_reads_only_units_without_a_manager_graphical_environment(setup, target):
    config, paths, host, home = user_fixture(setup)
    if target != 'default':
        config = Config(schema_version=2, user='user', screens=(ScreenConfig(id=target, output='HDMI-1'),))
        host.states[display.unit_name(target)] = dict(host.states[display.UNIT])
    host.manager = ''
    before = set(paths.root.rglob('*'))
    result = display.user_action('startup-status', target=target, config=config, home=home, host=host, paths=paths)
    assert result['state'] == 'ok' and result['exit_code'] == 0
    assert result['service']['ActiveState'] == 'inactive'
    assert result['legacy_service']['ActiveState'] == 'inactive'
    commands = [argv for argv, _ in host.calls]
    assert all(argv[:3] == ['systemctl', '--user', 'show'] for argv in commands)
    assert [argv[3] for argv in commands] == [display.unit_name(target)] + ([display.LEGACY] if target == 'default' else [])
    assert set(paths.root.rglob('*')) == before


@pytest.mark.parametrize('unit', [display.UNIT, display.LEGACY])
@pytest.mark.parametrize('observation', [Command(1, '', 'unavailable'), Command(0, ''), Command(0, 'ActiveState=unrecognized\n')])
def test_startup_status_rejects_unknown_unit_observation_without_mutations(setup, unit, observation):
    config, paths, host, home = user_fixture(setup)
    host.manager = ''
    host.show_failures[unit] = observation
    result = display.user_action('startup-status', config=config, home=home, host=host, paths=paths)
    assert result['state'] == 'unknown' and result['exit_code'] == 1
    assert all(argv[:3] == ['systemctl', '--user', 'show'] for argv, _ in host.calls)


def test_initialization_fence_read_is_readonly_and_scoped_to_its_user(setup):
    config, paths, _, _ = user_fixture(setup)
    assert pending_for_user(config, paths) is False
    assert not paths.at('/var/lib/ala-runtime').exists()
    fence = paths.at('/var/lib/ala-runtime/display-onboarding.json')
    RuntimeStore(paths)._write(fence, {'schema_version': 1, 'pending': True, 'user': 'another',
                                      'expected_revision': _revision(config.to_dict())})
    before = fence.read_bytes(), fence.stat().st_mtime_ns
    assert pending_for_user(config, paths) is False
    assert (fence.read_bytes(), fence.stat().st_mtime_ns) == before


def seed_local_setup(config, paths, host, home, *, ready=True):
    _seed_supervised_display(config, paths, host)
    path = display_health.state_path(home)
    record = json.loads(path.read_text())
    record.update(state='waiting_configuration', local_page_ready=ready, target_matches_config=False,
                  reason='설정 안내 표시 중', placement={'windows': [{'id': 7}]})
    path.write_text(json.dumps(record))
    return path


@pytest.mark.parametrize('ready', [False, True])
def test_setup_health_keeps_process_proof_without_claiming_site_health(setup, ready):
    config, paths, host, home = user_fixture(setup)
    config = replace(config, url=None)
    seed_local_setup(config, paths, host, home, ready=ready)
    result = display.user_action('check', config=config, home=home, host=host, paths=paths)
    assert result['exit_code'] == 2 and result['process_verified'] is True
    assert result['health']['state'] == 'waiting_configuration'
    assert result['health']['local_page_ready'] is ready
    assert result['health']['target_matches_config'] is False
    assert not any(row['id'] == 'chrome.health' and row['state'] == 'ok' for row in result['results'])


@pytest.mark.parametrize('invalid', ['missing-local-proof', 'non-bool-local-proof', 'site-healthy', 'healthy-without-match', 'matched-guide'])
def test_none_url_health_never_accepts_invalid_or_site_health_proof(setup, invalid):
    config, paths, host, home = user_fixture(setup)
    config = replace(config, url=None)
    path = seed_local_setup(config, paths, host, home)
    record = json.loads(path.read_text())
    if invalid == 'missing-local-proof':
        record.pop('local_page_ready')
    elif invalid == 'non-bool-local-proof':
        record['local_page_ready'] = 'yes'
    elif invalid == 'site-healthy':
        record.update(state='healthy', target_matches_config=True)
    elif invalid == 'healthy-without-match':
        record['state'] = 'healthy'
    else:
        record['target_matches_config'] = True
    path.write_text(json.dumps(record))
    assert display_health.read_health(home, paths, 321, None)['state'] == 'unknown'


@pytest.mark.parametrize('outcome', ['alive', 'gone', 'unobserved-windows'])
def test_setup_stop_still_verifies_child_and_window_disappearance(setup, monkeypatch, outcome):
    config, paths, host, home = user_fixture(setup)
    config = replace(config, url=None)
    seed_local_setup(config, paths, host, home)
    original = host.run
    bridge_calls = []

    def bridge(method, request=None, **kwargs):
        bridge_calls.append(method)
        if outcome == 'unobserved-windows':
            return {'state': 'unknown'}
        return {'state': 'ok', 'windows': [{'id': 7}], 'remaining_window_count': 0}

    def run(argv, **kwargs):
        result = original(argv, **kwargs)
        if argv[:3] == ['systemctl', '--user', 'stop'] and outcome != 'alive':
            shutil.rmtree(paths.at('/proc/322'))
        return result

    monkeypatch.setattr('ala.monitors.bridge', bridge)
    monkeypatch.setattr(host, 'run', run)
    result = display.user_action('stop', config=config, home=home, host=host, paths=paths)
    assert result['state'] == {'alive': 'pending', 'gone': 'stopped', 'unobserved-windows': 'unknown'}[outcome]
    assert bridge_calls == (['Inspect', 'Closed'] if outcome == 'gone' else ['Inspect'])
    assert [argv for argv, _ in host.calls if argv[:3] == ['systemctl', '--user', 'stop']] == [
        ['systemctl', '--user', 'stop', display.UNIT]]
