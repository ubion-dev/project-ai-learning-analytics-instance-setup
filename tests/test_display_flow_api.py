"""Public orchestration contracts, isolated from systemd and release fixtures."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ala import cli, display, display_onboarding, screen_swap, screens, service
from ala.config import Config, ScreenConfig
from ala.system import Paths, atomic_json


class ReadHost:
    def run(self, *args, **kwargs):
        pytest.fail('the API routing test must not execute a system command')


@pytest.fixture
def operations(tmp_path, monkeypatch):
    monkeypatch.setenv('SUDO_USER', 'user')
    monkeypatch.setattr(service.pwd, 'getpwnam', lambda name: SimpleNamespace(
        pw_uid=1000, pw_dir=str(tmp_path), pw_shell='/bin/bash'))
    return service.Operations(paths=Paths(tmp_path), host=ReadHost(), release=Path('/release'))


def test_initial_autostart_default_and_existing_off_are_distinct(operations):
    assert not operations.paths.config.exists()
    initial = operations.config()
    assert initial.user == 'user' and initial.autostart is True
    assert not operations.paths.config.exists()
    saved = Config(user='user', autostart=False)
    atomic_json(operations.paths.config, saved.to_dict(), 0o644)
    assert operations.config() == saved
    assert operations.config().autostart is False


@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('choice', ['missing', False, True])
def test_submit_forwards_initialization_default_and_explicit_choice(operations, monkeypatch, existing, choice):
    if existing:
        atomic_json(operations.paths.config, Config(user='user', autostart=False).to_dict(), 0o644)
    calls = []

    class Jobs:
        def __init__(self, paths, host):
            assert paths is operations.paths and host is operations.host

        def submit(self, config, release, python, **kwargs):
            calls.append((config(), release, python, kwargs))
            return {'run_id': 'synthetic-job', 'state': 'queued'}

    monkeypatch.setattr(service, 'JobStore', Jobs)
    payload = {'expected_config': operations.config().to_dict(), 'bash_assist': True}
    if choice != 'missing':
        payload['initialize_displays'] = choice
    assert operations.dispatch('submit', payload)['state'] == 'queued'
    config, release, python, options = calls[0]
    expected = not existing if choice == 'missing' else choice
    assert options['initialize_displays'] is expected
    assert options['persist_config'] is True and options['bash_assist'] is True
    assert options['expected_config'] == payload['expected_config']
    assert release == operations.release and python == release / '.venv/bin/python'
    assert config.autostart is (not existing)
    assert operations.paths.config.exists() is existing


def test_new_non_display_config_does_not_initialize_outputs(operations, monkeypatch):
    config = Config(user='user', display=False)
    monkeypatch.setattr(operations, 'config', lambda: config)
    captured = []

    class Jobs:
        def __init__(self, *args):
            pass

        def submit(self, current, *args, **kwargs):
            assert current() == config
            captured.append(kwargs)
            return {'state': 'queued'}

    monkeypatch.setattr(service, 'JobStore', Jobs)
    operations.dispatch('submit', {})
    assert captured[0]['initialize_displays'] is False


@pytest.mark.parametrize('action', ['preview', 'status'])
def test_initialization_read_routes_delegate_without_writing_config(operations, monkeypatch, action):
    calls = []
    result = {'state': 'pending', 'reason': 'synthetic observation'}

    def preview(config, **kwargs):
        calls.append(('preview', config, kwargs))
        return result

    def status(**kwargs):
        calls.append(('status', None, kwargs))
        return result

    monkeypatch.setattr(display_onboarding, 'preview', preview)
    monkeypatch.setattr(display_onboarding, 'status', status)
    assert operations.dispatch('display-initialization-' + action, {}) is result
    assert calls[0][0] == action
    assert calls[0][2] == {'paths': operations.paths, 'host': operations.host}
    if action == 'preview':
        assert calls[0][1] == operations.config()
    assert not operations.paths.config.exists()
    assert not operations.paths.at('/var/lib/ala').exists()


def test_identification_is_a_fixed_user_call_after_session_validation(operations, monkeypatch):
    calls = []
    config = operations.config()

    def session(model, host):
        assert model == config and host is operations.host
        calls.append('session')
        return 'ok'

    def identify(model, host, paths, action):
        assert model == config and host is operations.host and paths is operations.paths
        calls.append(action)
        return {'state': 'ok', 'exit_code': 0, 'outputs': []}

    monkeypatch.setattr(screens, 'inspect_session', session)
    monkeypatch.setattr(display, '_user_call', identify)
    result = operations.dispatch('screens-identify', {'target': 'other', 'duration': 9999})
    assert result['state'] == 'ok'
    assert calls == ['session', 'identify-outputs']
    assert not operations.paths.config.exists()


@pytest.mark.parametrize('session', ['pending', 'multiple', 'locked', 'lock_unknown', 'unknown', 'disabled'])
def test_identification_does_not_touch_gnome_without_one_unlocked_session(operations, monkeypatch, session):
    monkeypatch.setattr(screens, 'inspect_session', lambda *args: session)
    monkeypatch.setattr(display, '_user_call', lambda *args: pytest.fail('unsafe identification request'))
    result = operations.dispatch('screens-identify', {})
    assert result['exit_code'] == 2 and result['state'] == 'pending'
    assert not operations.paths.config.exists()


def test_swap_submit_forwards_exact_pair_and_reviewed_snapshots(operations, monkeypatch):
    config = Config(schema_version=2, user='user', screens=(
        ScreenConfig(id='default', output='HDMI-1', url='https://example.com/display'),
        ScreenConfig(id='other', output='DP-1'),
    )).to_dict()
    topology = {'output_state': 'ok', 'logical_outputs': [
        {'id': 'HDMI-1', 'confirmed': True}, {'id': 'DP-1', 'confirmed': True},
    ]}
    payload = {'source': 'default', 'destination': 'other', 'expected_config': config,
               'expected_topology': topology}
    before = deepcopy(payload)
    calls = []

    def submit(source, destination, expected_config, expected_topology, **kwargs):
        calls.append((source, destination, expected_config, expected_topology, kwargs))
        return {'state': 'queued', 'operation_id': 'a' * 32}

    monkeypatch.setattr(screen_swap, 'submit', submit)
    assert operations.dispatch('screens-swap-submit', payload)['state'] == 'queued'
    assert calls == [('default', 'other', config, topology, {
        'release': operations.release, 'paths': operations.paths, 'host': operations.host,
    })]
    assert payload == before
    assert not operations.paths.config.exists()


@pytest.mark.parametrize('action', ['status', 'retry'])
def test_swap_status_and_retry_forward_operation_identity(operations, monkeypatch, action):
    calls = []
    identifier = 'a' * 32
    monkeypatch.setattr(screen_swap, 'status', lambda **kwargs: calls.append(kwargs) or {'state': 'running'})
    monkeypatch.setattr(screen_swap, 'retry', lambda *args, **kwargs: calls.append((args, kwargs)) or {'state': 'queued'})
    operations.dispatch('screens-swap-' + action, {'operation_id': identifier, 'target': 'other'})
    common = {'paths': operations.paths, 'host': operations.host}
    if action == 'status':
        assert calls == [dict(common, operation_id=identifier, target='other')]
    else:
        assert calls == [((identifier,), dict(common, release=operations.release))]
    assert not operations.paths.config.exists()


def _private_runtime(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.os, 'geteuid', lambda: 0)
    monkeypatch.setattr('ala.system.platform_error', lambda: None)
    monkeypatch.setattr('ala.jobs.trusted_release', lambda path: calls.append(('trusted', path)) or path)
    monkeypatch.setattr(display_onboarding, 'run_pending', lambda: calls.append(('initialization',)) or {
        'state': 'pending', 'exit_code': 2})
    monkeypatch.setattr(screen_swap, 'run_worker', lambda operation: calls.append(('swap', operation)) or 3)
    monkeypatch.setattr(screen_swap, 'recover_all', lambda: calls.append(('recover',)) or 0)
    return calls


@pytest.mark.parametrize('argv,expected,dispatch', [
    (['_display-onboarding-worker'], 2, ('initialization',)),
    (['_swap-worker', 'a' * 32], 3, ('swap', 'a' * 32)),
    (['_swap-recover'], 0, ('recover',)),
])
def test_private_display_workers_require_installed_root_entry(monkeypatch, capsys, argv, expected, dispatch):
    calls = _private_runtime(monkeypatch)
    assert cli.main(argv) == expected
    assert calls == [('trusted', Path(cli.__file__).resolve().parents[1]), dispatch]
    if argv[0] == '_display-onboarding-worker':
        assert json.loads(capsys.readouterr().out)['state'] == 'pending'


@pytest.mark.parametrize('argv', [
    ['_display-onboarding-worker', 'extra'], ['_swap-worker'],
    ['_swap-worker', 'a' * 32, 'extra'], ['_swap-recover', 'extra'],
])
def test_private_worker_argument_count_never_dispatches_extra_input(monkeypatch, argv):
    calls = _private_runtime(monkeypatch)
    assert cli.main(argv) == 64
    assert calls == [('trusted', Path(cli.__file__).resolve().parents[1])]


@pytest.mark.parametrize('command', ['_display-onboarding-worker', '_swap-worker', '_swap-recover'])
@pytest.mark.parametrize('rejection', ['user', 'platform', 'release'])
def test_private_workers_reject_unprivileged_platform_or_source_checkout(monkeypatch, capsys, command, rejection):
    calls = _private_runtime(monkeypatch)
    if rejection == 'user':
        monkeypatch.setattr(cli.os, 'geteuid', lambda: 1000)
    elif rejection == 'platform':
        monkeypatch.setattr('ala.system.platform_error', lambda: 'synthetic unsupported platform')
    else:
        def reject(path):
            raise PermissionError('synthetic untrusted release')
        monkeypatch.setattr('ala.jobs.trusted_release', reject)
    argv = [command, 'a' * 32] if command == '_swap-worker' else [command]
    assert cli.main(argv) == 1
    assert 'error' in json.loads(capsys.readouterr().out)
    assert calls == []
