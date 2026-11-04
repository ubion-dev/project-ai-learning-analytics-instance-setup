import json
import hashlib
import os
from pathlib import Path
import shutil
import stat

import pytest

from ala import runtime
from ala.config import Config, ScreenConfig
from ala.jobs import change_lock
from ala.system import Command, Paths, atomic_json


class Clock:
    def __init__(self): self.value = 0
    def __call__(self): return self.value
    def sleep(self, value): self.value += value


class Host:
    def __init__(self):
        self.calls = []
        self.units = set()
    def run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[-1:] == ['--version']:
            return Command(0, Path(args[-2]).parent.name + '\n')
        if args[0] == 'systemd-run':
            self.units.add(args[1].removeprefix('--unit='))
            return Command(0)
        if args[:2] == ['systemctl', 'show'] and args[2] in self.units:
            return Command(0, 'ActiveState=active\n')
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


def controller(paths, *, fail=False, pending_stop=False, cancel_during_observation=False):
    calls = []
    active = True
    stop_count = 0
    def control(action, selection, **kwargs):
        nonlocal active, stop_count
        # Lifecycle requests are fenced against competing mutations. Long
        # observations must not hold the common root lock.
        if action in {'start', 'stop'}:
            with pytest.raises(RuntimeError), change_lock(paths):
                pytest.fail('lifecycle request must hold the common lock')
        else:
            with change_lock(paths):
                pass
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
            return {'state': 'pending', 'exit_code': 2}
        if action == 'status':
            return {'service': {'ActiveState': 'active' if active else 'inactive'}}
        if cancel_during_observation:
            runtime.cancel('default', paths=paths, host=Host())
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
    control, calls = controller(paths, cancel_during_observation=True)
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


def queue_settings(fixture, *, stopped=False):
    paths, host, _, store, _, new, _ = fixture
    saved = Config(user='user', url='https://example.com/?view=second', timezone='UTC')
    store.settings_saved('default')
    if stopped:
        store.cancel('default', stopped=True)
    request = runtime.submit_apply('default', release=new, paths=paths, host=host, settings_config=lambda: saved)
    assert request['state'] == 'queued'
    return saved, request


def apply_queued_settings(fixture, request, control):
    paths, host, clock, *_ = fixture
    return runtime.apply_target('default', paths=paths, host=host, control=control, monotonic=clock,
                                sleep=clock.sleep, unit=request['unit'], settings_operation_id=request['operation_id'])


def test_explicit_settings_apply_keeps_running_code_and_preserves_pending_code(fixture):
    paths, host, _, store, old, _, original = fixture
    saved, request = queue_settings(fixture)
    state = store.load('default')
    pending_code = state['desired']
    assert state['committed']['release'] == str(old)
    assert runtime.settings_status(saved, 'default', paths=paths, host=host, state=state) == {
        'state': 'queued', 'pending': True, 'applied_url': original.url,
        'applied_frontend': {'enabled': False, 'manifest_url': None},
        'reason': '저장한 설정을 적용하는 중입니다.', 'operation_id': request['operation_id']}
    assert host.calls[-2][0][-3:] == ['_runtime-worker', 'default', request['operation_id']]
    control, calls = controller(paths)

    def observe(action, selection, **kwargs):
        if action == 'preflight':
            candidate = Config.from_dict(json.loads(Path(selection['config_path']).read_text()))
            assert candidate.url == saved.url and candidate.timezone == original.timezone
            status = runtime.settings_status(saved, 'default', paths=paths, host=host, state=store.load('default'))
            assert status['state'] == 'running' and status['applied_url'] == original.url
        return control(action, selection, **kwargs)

    result = apply_queued_settings(fixture, request, observe)
    state = store.load('default')
    assert result['state'] == 'completed'
    assert {version for _, version in calls} == {'1.0.0'}
    assert state['desired']['release'] == pending_code['release']
    assert Config.from_dict(json.loads(Path(state['desired']['config_path']).read_text())) == Config(user='user', url=saved.url)
    assert state['committed']['release'] == str(old)
    status = runtime.settings_status(saved, 'default', paths=paths, host=host, state=state)
    assert status['state'] == 'completed' and status['pending'] is False
    assert status['applied_url'] == saved.url and status['operation_id'] == request['operation_id']


def test_settings_apply_persists_preflight_reason_without_stopping(fixture):
    paths, host, _, store, *_ = fixture
    saved, request = queue_settings(fixture)
    calls = []

    def control(action, *_args, **_kwargs):
        calls.append(action)
        if action == 'status':
            return {'service': {'ActiveState': 'active'}}
        return {'state': 'pending', 'exit_code': 2, 'reason': 'GNOME 확장 연결 대기', 'next_action': '다음 로그인 후 다시 조회하세요.'}

    result = apply_queued_settings(fixture, request, control)
    state = store.load('default')
    assert result['state'] == 'pending' and calls == ['status', 'preflight']
    assert state['operation'] is None
    status = runtime.settings_status(saved, 'default', paths=paths, host=host, state=state)
    assert status['state'] == 'pending' and status['pending']
    assert status['reason'] == 'GNOME 확장 연결 대기'
    assert status['next_action'] == '다음 로그인 후 다시 조회하세요.'
    assert status['operation_id'] == request['operation_id']


def test_settings_apply_checks_applied_output_of_a_later_added_target(fixture, monkeypatch):
    from ala import display, screens

    paths, host, clock, store, old, new, original = fixture
    other = ScreenConfig(id='other', url='https://example.com/other', output='HDMI-1')
    store.bootstrap('other', old, Config(user='user', schema_version=2, screens=(other,)))
    saved = Config(user='user', schema_version=2, screens=(
        ScreenConfig(url=original.url, output='HDMI-1'),
        ScreenConfig(id='other', url=other.url, output='DP-1'),
    ))
    paths.config.parent.mkdir(parents=True)
    atomic_json(paths.config, saved.to_dict(), 0o644)
    unit = paths.at('/etc/systemd/user/ala-chrome.service')
    unit.parent.mkdir(parents=True)
    unit.write_text(display.unit_text('default'))
    monkeypatch.setattr(display, '_account', lambda *_args: (1000, 'user', Path('/home/user')))
    monkeypatch.setattr(display, '_graphical_sessions', lambda *_args: ([('session-one', {'Active': 'yes'})], ''))
    monkeypatch.setattr(display, '_user_call', lambda _config, _host, _paths, action: {
        'environment': {'state': 'ready', 'exit_code': 0}, 'windows': {'state': 'ok'},
    }[action])
    monkeypatch.setattr(screens, '_inventory', lambda *_args: {'state': 'ok', 'logical_outputs': [
        {'id': 'HDMI-1', 'connectors': ['HDMI-1'], 'confirmed': True},
        {'id': 'DP-1', 'connectors': ['DP-1'], 'confirmed': True},
    ]})
    calls = []
    monkeypatch.setattr(display, 'chrome_action', lambda action, *_args, **_kwargs: calls.append(action) or ready('1.0.0'))

    request = runtime.submit_apply('default', release=new, paths=paths, host=host, settings_config=lambda: saved)
    result = runtime.apply_target('default', paths=paths, host=host, monotonic=clock, sleep=clock.sleep,
                                  unit=request['unit'], settings_operation_id=request['operation_id'])

    assert result['state'] == 'pending' and calls == ['status']
    assert '겹칩니다' in result['reason']
    assert store.load('other')['committed'] == store.load('other')['desired']
    assert store._validate_selection(store.load('default')['committed']) == original.to_dict()


def test_settings_apply_rolls_back_to_previous_revision_of_the_same_code(fixture):
    paths, host, _, store, *_ = fixture
    _, request = queue_settings(fixture)
    state = store.load('default')
    committed, desired = state['committed'], state['desired']
    failed_revision = state['operation']['candidate']['revision']
    control, _ = controller(paths)
    started = []

    def trial(action, selection, **kwargs):
        if action == 'start':
            started.append(selection['revision'])
        if action == 'check' and selection['revision'] == failed_revision:
            return {'state': 'failed', 'exit_code': 1}
        return control(action, selection, **kwargs)

    result = apply_queued_settings(fixture, request, trial)
    assert result['state'] == 'failed' and result['restored']
    assert started == [failed_revision, committed['revision']]
    state = store.load('default')
    assert state['committed'] == committed and state['desired'] == desired
    assert state['last_settings_result']['state'] == 'failed'
    assert '이전 화면 복원 완료' in state['last_settings_result']['reason']


def test_stopped_screen_settings_commit_without_clearing_stop_intent(fixture, monkeypatch):
    paths, host, _, store, *_ = fixture
    monkeypatch.setattr(runtime.RuntimeStore, '_session', lambda *_: 'session-one')
    saved, request = queue_settings(fixture, stopped=True)
    calls = []

    def control(action, *_args, **_kwargs):
        calls.append(action)
        return {'service': {'ActiveState': 'inactive'}}

    result = apply_queued_settings(fixture, request, control)
    state = store.load('default')
    assert result['state'] == 'selected_unverified' and calls == ['status']
    assert store.is_stopped(state)
    assert state['committed']['version'] == '1.0.0' and state['desired']['version'] == '2.0.0'
    assert runtime.settings_status(saved, 'default', paths=paths, host=host, state=state)['applied_url'] == saved.url


def test_settings_apply_keeps_other_targets_in_each_selection(fixture):
    paths, host, _, store, old, new, _ = fixture
    shutil.rmtree(store.base)
    previous_screen = ScreenConfig(url='https://example.com/previous', output='HDMI-1')
    applied_other = ScreenConfig(id='other', url='https://example.com/other-current', output='DP-1')
    pending_other = ScreenConfig(id='other', url='https://example.com/other-code-candidate', output='DP-1')
    saved_screen = ScreenConfig(url='https://example.com/updated', output='HDMI-1')
    saved_other = ScreenConfig(id='other', url='https://example.com/other-unapplied', output='DP-1')
    committed = Config(user='user', schema_version=2, screens=(previous_screen, applied_other))
    desired = Config(user='user', schema_version=2, screens=(previous_screen, pending_other), timezone='UTC')
    saved = Config(user='user', schema_version=2, screens=(saved_screen, saved_other))
    store.bootstrap('default', old, committed)
    store.stage('default', new, desired)
    other_state = store.bootstrap('other', old, committed)

    request = runtime.submit_apply('default', release=new, paths=paths, host=host, settings_config=lambda: saved)
    result = apply_queued_settings(fixture, request, lambda *_args, **_kwargs: {'service': {'ActiveState': 'inactive'}})

    assert result['state'] == 'selected_unverified'
    current = store.load('default')
    confirmed = Config.from_dict(store._validate_selection(current['committed']))
    waiting = Config.from_dict(store._validate_selection(current['desired']))
    assert confirmed.screens == (saved_screen, applied_other)
    assert waiting.screens == (saved_screen, pending_other) and waiting.timezone == 'UTC'
    assert store.load('other') == other_state


def test_stop_before_queued_settings_worker_runs_prevents_any_transition(fixture):
    _, _, _, store, *_ = fixture
    _, request = queue_settings(fixture)
    store.cancel('default', stopped=True)

    def forbidden(*_args, **_kwargs):
        pytest.fail('cancelled settings operation must not control Chrome')

    result = apply_queued_settings(fixture, request, forbidden)
    assert result['state'] == 'cancelled'
    assert store.load('default')['last_settings_result']['state'] == 'cancelled'


def test_legacy_settings_apply_requires_explicit_code_migration(fixture):
    paths, host, _, store, old, new, _ = fixture
    (old / 'COMPATIBILITY.json').unlink()
    saved = Config(user='user', url='https://example.com/updated')
    value = runtime.submit_apply('default', release=new, paths=paths, host=host, settings_config=lambda: saved)
    assert value['state'] == 'pending' and '최초 이전' in value['next_action']
    assert not any(args[0] == 'systemd-run' for args, _ in host.calls)
    assert store.load('default')['operation'] is None
