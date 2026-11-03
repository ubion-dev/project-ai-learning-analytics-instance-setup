import json
import hashlib
import os
from pathlib import Path
import shutil
import stat

import pytest

from ala import runtime
from ala.config import Config
from ala.jobs import change_lock
from ala.system import Command, Paths


class Clock:
    def __init__(self): self.value = 0
    def __call__(self): return self.value
    def sleep(self, value): self.value += value


class Host:
    def __init__(self): self.calls = []
    def run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[-1:] == ['--version']:
            return Command(0, Path(args[-2]).parent.name + '\n')
        if args[0] == 'systemd-run': return Command(0)
        return Command(1)


def release(paths, version, *, legacy=False):
    directory = paths.at('/opt/ala/releases') / version
    (directory / '.venv/bin').mkdir(parents=True)
    (directory / 'VERSION').write_text(version)
    (directory / 'entry.py').write_text('# fixture')
    (directory / '.venv/bin/python').write_text('# fixture')
    if not legacy:
        (directory / 'COMPATIBILITY.json').write_text(json.dumps(
            {'schema_version': 1, 'runtime_protocol': 1, 'config_schemas': [1, 2]}))
    return directory


@pytest.fixture
def fixture(tmp_path):
    paths, host, clock = Paths(tmp_path), Host(), Clock()
    boot = paths.at('/proc/sys/kernel/random/boot_id')
    boot.parent.mkdir(parents=True)
    boot.write_text('boot-one')
    old, new = release(paths, '1.0.0'), release(paths, '2.0.0')
    config = Config(user='user', url='https://example.com/')
    store = runtime.RuntimeStore(paths, host, monotonic=clock)
    store.bootstrap('default', old, config)
    store.stage('default', new, config)
    return paths, host, clock, store, old, new, config


def ready(version, *, health='healthy'):
    return {'state': 'running', 'service': {'ActiveState': 'active'},
            'health': {'state': health, 'supervisor_version': version, 'browser_pid': 123},
            'placement': {'state': 'ok'}, 'results': [], 'exit_code': 0}


def controller(paths, *, fail=False, pending_stop=False, cancel_on_start=False):
    calls = []
    active = True
    stop_count = 0
    def control(action, selection, **kwargs):
        nonlocal active, stop_count
        # No long observation or process control holds the common root lock.
        with change_lock(paths): pass
        calls.append((action, selection['version']))
        if action == 'preflight': return {'state': 'ready', 'exit_code': 0}
        if action == 'stop':
            stop_count += 1
            if pending_stop or stop_count == 1:
                active = False
                return {'state': 'pending', 'exit_code': 2, 'service_stopped': True}
            active = False
            return {'state': 'stopped', 'exit_code': 0}
        if action == 'start':
            active = True
            if cancel_on_start:
                runtime.cancel('default', paths=paths, host=Host())
            return {'state': 'pending', 'exit_code': 2}
        if action == 'status':
            return {'service': {'ActiveState': 'active' if active else 'inactive'}}
        result = ready('0.0.0' if fail and selection['version'] == '2.0.0' else selection['version'], health='recovering')
        result['health']['config_revision'] = selection['revision']
        return result
    return control, calls


def test_current_pointer_is_not_committed_runtime_and_boot_discards_trial(fixture):
    paths, host, clock, store, old, new, config = fixture
    paths.at('/opt/ala/current').symlink_to(new)
    token = runtime.RuntimeStore(paths, host).reserve('default', 'runtime', ttl=300)
    store.phase(token, 'trial')
    assert runtime.load_user_selection(paths=paths, host=host)['version'] == '2.0.0'
    paths.at('/proc/sys/kernel/random/boot_id').write_text('boot-two')
    assert runtime.load_user_selection(paths=paths, host=host)['version'] == '1.0.0'
    assert store.load('default')['operation'] is not None  # read never repairs state


def test_manual_stop_cancels_token_until_new_boot_or_new_session(fixture, monkeypatch):
    paths, host, clock, store, *_ = fixture
    monkeypatch.setattr(runtime.RuntimeStore, '_session', lambda *_: 'one')
    token = store.reserve('default', 'frontend')
    store.cancel('default')
    assert not store.valid(token)
    assert store.is_stopped(store.load('default'))
    monkeypatch.setattr(runtime.RuntimeStore, '_session', lambda *_: 'two')
    assert not store.is_stopped(store.load('default'))
    with pytest.raises(RuntimeError): store.finish(token)


def test_strict_pin_and_cas_reject_stale_or_untrusted_data(fixture, monkeypatch):
    paths, host, clock, store, old, new, config = fixture
    with pytest.raises(RuntimeError): store.stage('default', new, config, expected_generation=0)
    monkeypatch.setenv('ALA_RUNTIME_CONFIG', '/outside/config.json')
    with pytest.raises(PermissionError): runtime.load_user_selection(paths=paths, host=host)
    with pytest.raises(ValueError): store.state_path('../default')
    with pytest.raises(PermissionError): store.release(paths.root)


def test_worker_umask_never_hides_nonsecret_pin_tree(fixture):
    paths, host, clock, store, old, new, config = fixture
    shutil.rmtree(store.base)
    previous = os.umask(0o077)
    try:
        store.bootstrap('screen_two', new, Config(user='user', url='https://example.com/second'))
    finally:
        os.umask(previous)
    for relative in ('', 'targets', 'targets/screen_two', 'configs'):
        directory = store.base / relative
        assert stat.S_IMODE(directory.stat().st_mode) == 0o755
    assert stat.S_IMODE(store.state_path('screen_two').stat().st_mode) == 0o644
    assert stat.S_IMODE(Path(store.load('screen_two')['committed']['config_path']).stat().st_mode) == 0o644


def test_pending_start_is_observed_and_site_outage_not_runtime_failure(fixture):
    paths, host, clock, store, *_ = fixture
    control, calls = controller(paths)
    result = runtime.apply_target('default', paths=paths, host=host, control=control,
                                  monotonic=clock, sleep=clock.sleep)
    assert result['state'] == 'completed'
    assert store.load('default')['committed']['version'] == '2.0.0'
    assert calls[:4] == [('status', '1.0.0'), ('preflight', '2.0.0'), ('stop', '1.0.0'), ('stop', '1.0.0')]
    assert clock.value >= 60


def test_stop_pending_with_inactive_unit_never_starts_candidate(fixture):
    paths, host, clock, store, *_ = fixture
    control, calls = controller(paths, pending_stop=True)
    result = runtime.apply_target('default', paths=paths, host=host, control=control,
                                  monotonic=clock, sleep=clock.sleep)
    assert result['state'] == 'failed' and result['restored'] is False
    assert not any(action == 'start' for action, _ in calls)
    assert store.load('default')['committed']['version'] == '1.0.0'


def test_failed_candidate_restores_old_selection_without_cli_switch(fixture):
    paths, host, clock, store, *_ = fixture
    control, calls = controller(paths, fail=True)
    result = runtime.apply_target('default', paths=paths, host=host, control=control,
                                  monotonic=clock, sleep=clock.sleep)
    assert result['state'] == 'failed' and result['restored'] is True
    assert ('start', '1.0.0') in calls
    assert store.load('default')['committed']['version'] == '1.0.0'


def test_stop_generation_wins_over_restore(fixture):
    paths, host, clock, store, *_ = fixture
    control, calls = controller(paths, cancel_on_start=True)
    result = runtime.apply_target('default', paths=paths, host=host, control=control,
                                  monotonic=clock, sleep=clock.sleep)
    assert result['state'] == 'cancelled'
    assert ('start', '1.0.0') not in calls


def test_inactive_url_missing_migrates_code_without_start_or_visual_claim(fixture):
    paths, host, clock, store, old, new, config = fixture
    without_url = Config(user='user')
    store.stage('default', new, without_url)
    calls = []
    def control(action, *_args, **kwargs):
        calls.append(action)
        return {'service': {'ActiveState': 'inactive'}}
    result = runtime.apply_target('default', paths=paths, host=host, control=control)
    assert result['state'] == 'selected_unverified' and result['exit_code'] == 2
    assert calls == ['status']
    assert store.load('default')['committed']['version'] == '2.0.0'
    assert store.load('default')['last_result']['state'] == 'selected_unverified'


def test_legacy_configuration_guard_preserves_current_schema_and_url(fixture):
    paths, host, clock, store, old, new, config = fixture
    (old / 'COMPATIBILITY.json').unlink()
    runtime.guard_config_change(config, config, paths=paths, host=host)
    with pytest.raises(ValueError):
        runtime.guard_config_change(config, Config(user='user', url='https://example.com/new'), paths=paths, host=host)


def test_explicit_apply_pins_detached_worker(fixture):
    paths, host, clock, store, old, new, config = fixture
    value = runtime.submit_apply('default', release=new, paths=paths, host=host)
    assert value['exit_code'] == 2
    command = host.calls[-1][0]
    assert command[-2:] == ['_runtime-worker', 'default']
    assert str(new / 'entry.py') in command


def test_failed_preflight_never_stops_existing_window(fixture):
    paths, host, clock, store, *_ = fixture
    calls = []
    def control(action, *_args, **kwargs):
        calls.append(action)
        return {'service': {'ActiveState': 'active'}} if action == 'status' else {'exit_code': 1, 'state': 'blocked'}
    result = runtime.apply_target('default', paths=paths, host=host, control=control)
    assert result['state'] == 'pending' and calls == ['status', 'preflight']
    assert store.load('default')['committed']['version'] == '1.0.0'


def test_incompatible_shared_protocol_blocked_before_staging(fixture):
    paths, host, clock, store, old, new, config = fixture
    (new / 'COMPATIBILITY.json').write_text(json.dumps(
        {'schema_version': 1, 'runtime_protocol': 1, 'extension_protocol': 2, 'config_schemas': [1, 2]}))
    with pytest.raises(ValueError): store.stage('default', new, config)


def test_dead_trial_worker_never_selects_candidate_after_crash(fixture):
    paths, host, clock, store, *_ = fixture
    token = runtime.RuntimeStore(paths, host).reserve('default', 'runtime', ttl=300, unit='ala-runtime-nightly.service')
    store.phase(token, 'trial')
    assert runtime.load_user_selection(paths=paths, host=host)['version'] == '1.0.0'


@pytest.mark.parametrize('reported,through_current,expected', [
    ('1.0.0', False, 'completed'), ('2.0.0', False, 'pending'),
    ('1.0.0', True, 'pending'), ('2.0.0', True, 'pending')])
def test_enrollment_checks_reported_version_against_actual_supervisor_entry(fixture, reported, through_current, expected):
    paths, host, clock, store, old, new, config = fixture
    shutil.rmtree(store.base)
    process = paths.at('/proc/123')
    process.mkdir()
    process.joinpath('status').write_text('Uid:\t1000\t1000\t1000\t1000\n')
    process.joinpath('stat').write_text('123 (fixture supervisor) ' + ' '.join(['0'] * 19 + ['456']))
    entry = old / 'entry.py'
    if through_current:
        paths.at('/opt/ala/current').symlink_to(new)
        entry = paths.at('/opt/ala/current/entry.py')
    process.joinpath('cmdline').write_bytes(('python\0-I\0' + str(entry) + '\0_chrome-run\0').encode())
    original = host.run
    def run(args, **kwargs):
        if args[:2] == ['getent', 'passwd']:
            return Command(0, 'user:x:1000:1000::/home/user:/bin/bash')
        return original(args, **kwargs)
    host.run = run
    report = {'service': {'ActiveState': 'active', 'MainPID': '123'}, 'results': [],
              'health': {'state': 'healthy', 'supervisor_version': reported, 'supervisor_pid': 123,
                         'supervisor_start_ticks': '456',
                         'config_fingerprint': hashlib.sha256(config.url.encode()).hexdigest()}}
    result = runtime.bootstrap_from_observation(config, {'default': report}, paths=paths, host=host)
    assert result['state'] == expected
    if expected == 'completed': assert store.load('default')['committed']['version'] == '1.0.0'
    else: assert store.load('default') is None


def test_first_save_can_skip_inactive_enrollment(fixture):
    paths, host, clock, store, old, new, config = fixture
    shutil.rmtree(store.base)
    result = runtime.bootstrap_from_observation(config, {'default': {'service': {'ActiveState': 'inactive'}}},
                                                paths=paths, host=host, release=new, skip_inactive=True)
    assert result['results'][0]['state'] == 'not_applicable'
    assert store.load('default') is None


def test_setup_queued_before_reservation_blocks_display_operation(fixture, monkeypatch):
    paths, host, clock, store, *_ = fixture
    monkeypatch.setattr('ala.jobs.JobStore.list', lambda *_: [{'state': 'queued'}])
    with pytest.raises(RuntimeError): store.reserve('default', 'frontend')
    assert store.load('default')['operation'] is None


def test_default_controller_preflights_candidate_not_committed_snapshot(fixture, monkeypatch):
    from ala import screens
    paths, host, clock, store, old, new, config = fixture
    candidate = Config(user='user', url='https://example.com/new-display')
    store.stage('default', new, candidate)
    observed = []
    def chrome_action(action, context, **kwargs):
        assert action == 'status', 'candidate was not ready; existing Chrome must never be stopped'
        assert context.url == config.url
        return {'state': 'running', 'service': {'ActiveState': 'active'}}
    def preflight(context, **kwargs):
        observed.append((context.url, context.runtime_pin['version'], context.runtime_pin['revision']))
        return {'state': 'pending', 'exit_code': 2}
    monkeypatch.setattr(screens.display, 'chrome_action', chrome_action)
    monkeypatch.setattr(screens, 'preflight', preflight)
    result = runtime.apply_target('default', paths=paths, host=host)
    assert result['state'] == 'pending'
    assert observed == [(candidate.url, '2.0.0', store.load('default')['desired']['revision'])]
    assert store.load('default')['committed']['version'] == '1.0.0'
