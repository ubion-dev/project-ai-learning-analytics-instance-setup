"""Durable two-target exchanges without running Chrome, SSH, or systemd."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import os

import pytest

from ala import runtime, screen_swap, screens
from ala.config import Config, ScreenConfig
from ala.jobs import change_lock
from ala.system import Command, Paths, atomic_json

SCREEN_ACTION = screens.screen_action


class Clock:
    value = 0
    def __call__(self):
        return self.value
    def sleep(self, duration):
        self.value += duration


class Host:
    def __init__(self):
        self.calls = []
        self.units = {'ala-screen-swap-recover.service'}
        self.submission_code = 0
        self.query_code = 0

    def run(self, args, **kwargs):
        self.calls.append(list(args))
        if args[0] == 'systemd-run':
            if not self.submission_code:
                self.units.add(args[1].removeprefix('--unit='))
            return Command(self.submission_code)
        if args[:2] == ['systemctl', 'show']:
            return Command(self.query_code, 'ActiveState=' + ('active' if args[2] in self.units else 'inactive') + '\n')
        if args[:2] == ['systemctl', 'stop']:
            self.units.discard(args[2])
            return Command(0)
        return Command(0)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    paths, host, clock = Paths(tmp_path), Host(), Clock()
    boot = paths.at('/proc/sys/kernel/random/boot_id')
    boot.parent.mkdir(parents=True)
    boot.write_text('synthetic-boot')
    config = Config(schema_version=2, user='user', screens=(
        ScreenConfig(output='HDMI-1', url='https://example.com/site'),
        ScreenConfig(id='other', output='DP-1', url=None)))
    paths.config.parent.mkdir(parents=True)
    atomic_json(paths.config, config.to_dict(), 0o644)
    store = runtime.RuntimeStore(paths, host, monotonic=clock)
    releases = []
    for version in ('1.0.0', '2.0.0'):
        release = paths.at('/opt/ala/releases') / version
        (release / '.venv/bin').mkdir(parents=True)
        (release / 'entry.py').write_text('# synthetic release\n')
        (release / '.venv/bin/python').write_text('# synthetic interpreter\n')
        (release / 'VERSION').write_text(version)
        (release / 'COMPATIBILITY.json').write_text(json.dumps({
            'schema_version': 1, 'runtime_protocol': 1, 'config_schemas': [1, 2], 'screen_swap_protocol': 1,
            'setup_page_protocol': 1}))
        releases.append(release)
    for target in ('default', 'other'):
        store.bootstrap(target, releases[0], config)
        store.stage(target, releases[1], config)
    topology = {'state': 'ok', 'logical_outputs': [
        {'id': 'HDMI-1', 'connectors': ['HDMI-1'], 'x': 0, 'y': 0, 'width': 1920, 'height': 1080, 'confirmed': True},
        {'id': 'DP-1', 'connectors': ['DP-1'], 'x': 1920, 'y': 0, 'width': 1920, 'height': 1080, 'confirmed': True}]}
    active = {'default': True, 'other': True}
    monkeypatch.setattr(runtime.RuntimeStore, '_session', lambda *_: 'synthetic-session')
    monkeypatch.setattr(screens, '_inventory', lambda *_: deepcopy(topology))
    monkeypatch.setattr(screens, 'screen_action', lambda _action, _config, target, **_: {
        'service': {'ActiveState': 'active' if active[target] else 'inactive'}})
    return paths, host, clock, store, config, topology, releases, active


def submit(fixture):
    paths, host, _, _, config, topology, releases, _ = fixture
    return screen_swap.submit('default', 'other', config.to_dict(), topology,
                              release=releases[1], paths=paths, host=host)


def control_for(fixture, *, fail_target=None, fail_restore=False, stop_during_check=False):
    paths, host, _, store, _, _, _, active = fixture
    calls = []
    cancelled = False
    original = {'default': 'HDMI-1', 'other': 'DP-1'}

    def control(target, action, selection, **kwargs):
        nonlocal cancelled
        config = Config.from_dict(store._validate_selection(selection)).for_screen(target)
        if action in {'start', 'stop'}:
            with pytest.raises(RuntimeError), change_lock(paths):
                pytest.fail('lifecycle changes need the common root lock')
        else:
            with change_lock(paths):
                pass
        calls.append((target, action, config.output))
        if action == 'preflight':
            return {'state': 'ready', 'exit_code': 0}
        if action == 'status':
            return {'service': {'ActiveState': 'active' if active[target] else 'inactive'}}
        if action == 'stop':
            active[target] = False
            return {'state': 'stopped', 'exit_code': 0}
        if action == 'start':
            if fail_restore and config.output == original[target]:
                return {'state': 'error', 'exit_code': 1}
            active[target] = True
            return {'state': 'starting', 'exit_code': 2}
        if action == 'check':
            if stop_during_check and not cancelled:
                cancelled = True
                with change_lock(paths):
                    store.cancel('default', stopped=True)
                active['default'] = False
            bad = target == fail_target and config.output != original[target]
            return {'state': 'running', 'service': {'ActiveState': 'active' if active[target] else 'inactive'},
                    'health': {'state': 'waiting_configuration' if config.url is None else 'healthy', 'local_page_ready': True,
                               'supervisor_version': '0.0.0' if bad else selection['version'],
                               'config_revision': selection['revision'], 'browser_pid': 123},
                    'placement': {'state': 'ok'}, 'results': [], 'exit_code': 0}
        raise AssertionError(action)
    return control, calls


def execute(fixture, operation_id, **options):
    paths, host, clock, *_ = fixture
    return screen_swap._execute(operation_id, paths=paths, host=host, monotonic=clock, sleep=clock.sleep, **options)


@pytest.mark.parametrize('inactive', [(), ('other',), ('default', 'other')])
def test_exchange_preserves_sites_code_versions_and_stopped_intent(fixture, inactive):
    paths, host, _, store, before, _, _, active = fixture
    for target in inactive:
        active[target] = False
        store.cancel(target, stopped=True)
    request = submit(fixture)
    with pytest.raises(RuntimeError):
        screen_swap.guard(paths=paths, host=host)
    controller, calls = control_for(fixture)
    result = execute(fixture, request['operation_id'], control=controller)
    assert result['state'] == 'completed'
    after = Config.load(paths.config)
    for target, old_output, new_output in [('default', 'HDMI-1', 'DP-1'), ('other', 'DP-1', 'HDMI-1')]:
        current = store.load(target)
        assert current['committed']['version'] == '1.0.0'
        assert current['desired']['version'] == '2.0.0'
        assert Config.from_dict(store._validate_selection(current['committed'])).for_screen(target).output == new_output
        assert after.for_screen(target).url == before.for_screen(target).url
        assert store.is_stopped(current) == (target in inactive)
        assert active[target] == (target not in inactive)
        assert not any(t == target and action == 'start' for t, action, _ in calls) if target in inactive else True
    first_start = next((index for index, (_, action, _) in enumerate(calls) if action == 'start'), len(calls))
    assert {target for target, action, _ in calls[:first_start] if action == 'stop'} == {'default', 'other'}
    assert screen_swap.status(paths=paths, host=host, target='other')['operation_id'] == request['operation_id']
    screen_swap.guard(paths=paths, host=host)


def test_second_candidate_failure_rolls_both_assignments_back(fixture):
    paths, host, _, store, before, *_ = fixture
    originals = {target: deepcopy(store.load(target)) for target in ('default', 'other')}
    request = submit(fixture)
    controller, calls = control_for(fixture, fail_target='other')
    result = execute(fixture, request['operation_id'], control=controller)
    assert result['state'] == 'rolled_back'
    assert Config.load(paths.config) == before
    for target in originals:
        assert store.load(target)['committed'] == originals[target]['committed']
        assert store.load(target)['desired'] == originals[target]['desired']
    restore = next(index for index, (target, action, output) in enumerate(calls)
                   if action == 'start' and output == ('HDMI-1' if target == 'default' else 'DP-1'))
    assert {target for target, action, _ in calls[:restore] if action == 'stop'} == {'default', 'other'}


def test_latest_operator_stop_wins_while_other_target_is_restored(fixture):
    paths, _, _, store, before, _, _, active = fixture
    request = submit(fixture)
    controller, calls = control_for(fixture, stop_during_check=True)
    result = execute(fixture, request['operation_id'], control=controller)
    assert result['state'] == 'rolled_back' and Config.load(paths.config) == before
    assert store.is_stopped(store.load('default')) and not active['default']
    assert active['other']
    assert ('default', 'start', 'HDMI-1') not in calls
    assert ('other', 'start', 'DP-1') in calls


def test_failed_restore_keeps_journal_and_blocks_changes_after_lease_expiry(fixture):
    paths, host, _, store, *_ = fixture
    request = submit(fixture)
    controller, _ = control_for(fixture, fail_target='other', fail_restore=True)
    result = execute(fixture, request['operation_id'], control=controller)
    assert result['state'] == 'recovery_required'
    with pytest.raises(RuntimeError):
        screen_swap.guard(paths=paths, host=host, target='default')
    with pytest.raises(RuntimeError):
        store.stage('other', store.load('other')['desired']['release'], store._validate_selection(store.load('other')['desired']))


@pytest.mark.parametrize('change', ['configuration', 'topology', 'mirror', 'split', 'pending', 'unsupported'])
def test_submission_refuses_stale_or_unsupported_exchange_without_mutation(fixture, change):
    paths, host, _, store, config, topology, releases, _ = fixture
    expected = config.to_dict()
    if change == 'configuration':
        atomic_json(paths.config, replace(config, dev_tools=True).to_dict(), 0o644)
    elif change == 'topology':
        topology['logical_outputs'][1]['x'] = 3000
    elif change == 'mirror':
        topology['logical_outputs'] = [dict(topology['logical_outputs'][0], id='DP-1+HDMI-1', connectors=['HDMI-1', 'DP-1'])]
    elif change == 'split':
        config = replace(config, screens=(replace(config.screens[0], layout='left'), config.screens[1]))
        expected = config.to_dict()
        atomic_json(paths.config, expected, 0o644)
    elif change == 'pending':
        atomic_json(paths.config, replace(config, screens=(replace(config.screens[0], url='https://example.com/changed'), config.screens[1])).to_dict(), 0o644)
        expected = Config.load(paths.config).to_dict()
    else:
        (releases[0] / 'COMPATIBILITY.json').write_text(json.dumps({'schema_version': 1, 'runtime_protocol': 1, 'config_schemas': [1, 2]}))
    reviewed = deepcopy(topology)
    if change == 'topology':
        reviewed['logical_outputs'][1]['x'] = 1920
    originals = {target: deepcopy(store.load(target)) for target in ('default', 'other')}
    with pytest.raises((ValueError, RuntimeError)):
        screen_swap.submit('default', 'other', expected, reviewed, release=releases[1], paths=paths, host=host)
    assert {target: store.load(target) for target in originals} == originals
    assert not screen_swap.Store(paths, host).records()
    assert not any(call[0] == 'systemd-run' for call in host.calls)


def test_partial_reservation_is_journaled_and_recovered_after_boot(fixture, monkeypatch):
    paths, host, _, store, before, *_ = fixture
    reserve = runtime.RuntimeStore.reserve
    def fail_second(self, target, *args, **kwargs):
        if target == 'other':
            raise OSError('synthetic second-write failure')
        return reserve(self, target, *args, **kwargs)
    monkeypatch.setattr(runtime.RuntimeStore, 'reserve', fail_second)
    with pytest.raises(OSError):
        submit(fixture)
    journal = screen_swap.Store(paths, host).pending()[0]
    assert journal['participants']['default']['token'] is not None
    assert journal['participants']['other']['token'] is None
    paths.at('/proc/sys/kernel/random/boot_id').write_text('next-synthetic-boot')
    assert screen_swap.recover_all(paths=paths, host=host) == 0
    assert Config.load(paths.config) == before
    assert all(store.load(target)['operation'] is None for target in ('default', 'other'))


def test_commit_decision_replays_both_after_partial_state_write(fixture, monkeypatch):
    paths, host, _, store, _, *_ = fixture
    request = submit(fixture)
    write = runtime.RuntimeStore._write
    interrupted = False
    def interrupt_second(self, path, value):
        nonlocal interrupted
        if path == self.state_path('other') and value.get('operation') is None and not interrupted:
            interrupted = True
            raise KeyboardInterrupt('synthetic process termination')
        return write(self, path, value)
    monkeypatch.setattr(runtime.RuntimeStore, '_write', interrupt_second)
    controller, _ = control_for(fixture)
    with pytest.raises(KeyboardInterrupt):
        execute(fixture, request['operation_id'], control=controller)
    assert screen_swap.Store(paths, host).load(request['operation_id'])['decision'] == 'commit'
    host.units.discard('ala-runtime-swap-' + request['operation_id'] + '.service')
    with pytest.raises(RuntimeError):
        runtime.load_user_selection('default', paths=paths, host=host)
    assert screen_swap.recover_all(paths=paths, host=host) == 0
    assert Config.load(paths.config).for_screen('default').output == 'DP-1'
    assert Config.load(paths.config).for_screen('other').output == 'HDMI-1'
    assert all(store.load(target)['operation'] is None for target in ('default', 'other'))


def test_stopped_partner_uses_pair_candidate_for_conflict_without_start_permission(fixture):
    paths, host, _, store, config, topology, _, active = fixture
    active['other'] = False
    store.cancel('other', stopped=True)
    request = submit(fixture)
    journal = screen_swap.Store(paths, host).load(request['operation_id'])
    screen_swap._phase(screen_swap.Store(paths, host), journal, 'preflight')
    own = journal['participants']['default']['after']
    context = replace(Config.from_dict(store._validate_selection(own)), runtime_pin=own).for_screen('default')
    assert screens._conflict(config, 'default', topology, selected=context, paths=paths, host=host)['state'] == 'ok'
    screen_swap._phase(screen_swap.Store(paths, host), journal, 'trial')
    selected = runtime.load_user_selection('other', paths=paths, host=host)
    assert Config.from_dict(selected['config']).for_screen('other').output == 'HDMI-1'
    assert runtime.dispatch('other', paths=paths, host=host, execute=lambda *_: pytest.fail('stopped target launched')) == 78


def test_retry_and_recovery_registration_use_fixed_commands_and_preserve_external_units(fixture):
    paths, host, _, _, _, _, releases, _ = fixture
    host.submission_code = 1
    request = submit(fixture)
    assert request['state'] == 'recovery_required'
    host.submission_code = 0
    assert screen_swap.retry(request['operation_id'], release=releases[1], paths=paths, host=host)['state'] == 'queued'
    assert host.calls[-1][-2:] == ['_swap-worker', request['operation_id']]
    assert screen_swap.register_recovery(release=releases[1], paths=paths, host=host)['state'] == 'ready'
    unit = paths.at('/etc/systemd/system/ala-screen-swap-recover.service')
    assert '_swap-recover' in unit.read_text()
    unit.write_text('external service\n')
    with pytest.raises(RuntimeError):
        screen_swap.register_recovery(release=releases[1], paths=paths, host=host)


def test_status_is_read_only_and_symlink_records_fail_closed(fixture):
    paths, host, *_ = fixture
    request = submit(fixture)
    store = screen_swap.Store(paths, host)
    path = store.path(request['operation_id'])
    original = path.read_bytes()
    host.units.clear()
    assert screen_swap.status(paths=paths, host=host, operation_id=request['operation_id'])['state'] == 'recovery_required'
    assert path.read_bytes() == original
    replacement = path.with_suffix('.backup')
    path.rename(replacement)
    path.symlink_to(replacement)
    with pytest.raises(PermissionError):
        screen_swap.guard(paths=paths, host=host)


def test_candidates_change_only_pair_outputs_and_preserve_desired_and_third_settings(fixture):
    paths, _, _, store, config, _, releases, _ = fixture
    third = ScreenConfig(id='third', output='DP-2', url='https://example.com/applied')
    historical = replace(config, screens=(*config.screens, third))
    desired = replace(historical, screens=(replace(config.screens[0], url='https://example.com/future', autostart=False),
                                        config.screens[1], third))
    for target in ('default', 'other'):
        state = store.load(target)
        state['committed'] = store.selection(releases[0], historical)
        state['desired'] = store.selection(releases[1], desired)
        store._write(store.state_path(target), state)
    saved = replace(historical, screens=(*config.screens, replace(third, url='https://example.com/unapplied')))
    atomic_json(paths.config, saved.to_dict(), 0o644)
    args = list(fixture)
    args[4] = saved
    request = submit(tuple(args))
    journal = screen_swap.Store(paths, args[1]).load(request['operation_id'])
    for target, part in journal['participants'].items():
        candidate = Config.from_dict(store._validate_selection(part['after']))
        future = Config.from_dict(store._validate_selection(part['desired_after']))
        assert candidate.for_screen('third').url == 'https://example.com/applied'
        assert future.for_screen('third').url == 'https://example.com/applied'
        assert future.for_screen('default').url == 'https://example.com/future'
        assert future.for_screen('default').autostart is False
        assert candidate.for_screen(target).output != historical.for_screen(target).output


def test_configuration_symlink_or_writable_mode_prevents_submission(fixture):
    paths, _, _, _, _, *_ = fixture
    original = paths.config.with_suffix('.original')
    paths.config.rename(original)
    paths.config.symlink_to(original)
    with pytest.raises(PermissionError):
        submit(fixture)
    paths.config.unlink()
    original.rename(paths.config)
    paths.config.chmod(0o666)
    with pytest.raises(PermissionError):
        submit(fixture)


def test_unknown_worker_state_never_starts_parallel_recovery(fixture, monkeypatch):
    paths, host, *_ = fixture
    request = submit(fixture)
    host.query_code = 124
    monkeypatch.setattr(screen_swap, '_execute', lambda *_args, **_kwargs: pytest.fail('parallel recovery'))
    assert screen_swap.recover_all(paths=paths, host=host) == 1
    with pytest.raises(RuntimeError, match='종료 여부'):
        screen_swap.retry(request['operation_id'], paths=paths, host=host)


def test_failed_automatic_recovery_does_not_repeat_on_timer(fixture, monkeypatch):
    paths, host, *_ = fixture
    request = submit(fixture)
    controller, _ = control_for(fixture, fail_target='other', fail_restore=True)
    assert execute(fixture, request['operation_id'], control=controller)['state'] == 'recovery_required'
    monkeypatch.setattr(screen_swap, '_execute', lambda *_args, **_kwargs: pytest.fail('automatic retry repeated'))
    assert screen_swap.recover_all(paths=paths, host=host) == 0
    host.units.discard('ala-runtime-swap-' + request['operation_id'] + '.service')
    assert screen_swap.retry(request['operation_id'], paths=paths, host=host)['state'] == 'queued'


def test_expired_lease_fences_user_start_and_is_recovered_once(fixture):
    paths, host, _, store, before, *_ = fixture
    request = submit(fixture)
    journals = screen_swap.Store(paths, host)
    value = journals.load(request['operation_id'])
    screen_swap._phase(journals, value, 'trial')
    value = journals.load(request['operation_id'])
    value['expires_monotonic'] = 0
    journals.save(value)
    for target in value['targets']:
        state = store.load(target)
        state['operation']['expires_monotonic'] = 0
        store._write(store.state_path(target), state)
    with pytest.raises(RuntimeError):
        runtime.load_user_selection('default', paths=paths, host=host)
    assert screen_swap.recover_all(paths=paths, host=host) == 0
    assert Config.load(paths.config) == before
    assert screen_swap.status(paths=paths, host=host)['state'] == 'rolled_back'


def test_dead_worker_allows_only_pinned_observation_and_stop_not_dispatch(fixture, monkeypatch):
    paths, host, _, store, *_ = fixture
    request = submit(fixture)
    value = screen_swap.Store(paths, host).load(request['operation_id'])
    host.units.clear()
    selected = value['participants']['default']['before']
    monkeypatch.setenv('ALA_RUNTIME_CONFIG', selected['config_path'])
    monkeypatch.setenv('ALA_RUNTIME_TARGET', 'default')
    monkeypatch.setenv('ALA_RUNTIME_ACTION', 'stop')
    assert runtime.load_user_selection('default', paths=paths, host=host, observation=True)['revision'] == selected['revision']
    with pytest.raises(RuntimeError):
        runtime.load_user_selection('default', paths=paths, host=host)
    with pytest.raises(RuntimeError):
        runtime.dispatch('default', paths=paths, host=host, execute=lambda *_: pytest.fail('unresolved swap started'))


def test_preflight_output_change_never_stops_or_restarts_original_screens(fixture):
    paths, _, _, _, before, topology, *_ = fixture
    request = submit(fixture)
    topology['logical_outputs'][1]['confirmed'] = False
    controller, calls = control_for(fixture)
    assert execute(fixture, request['operation_id'], control=controller)['state'] == 'rolled_back'
    assert not calls
    assert Config.load(paths.config) == before


def test_local_setup_readiness_requires_explicit_document_confirmation():
    report = {'service': {'ActiveState': 'active'}, 'health': {'state': 'waiting_configuration',
              'supervisor_version': '1.0.0', 'config_revision': 'synthetic', 'browser_pid': 123},
              'placement': {'state': 'ok'}, 'results': []}
    assert not runtime.code_ready(report, '1.0.0', 'synthetic', setup=True)
    report['health']['local_page_ready'] = True
    assert runtime.code_ready(report, '1.0.0', 'synthetic', setup=True)
    report['health']['state'] = 'healthy'
    assert not runtime.code_ready(report, '1.0.0', 'synthetic', setup=True)
    report['health']['state'] = 'recovering'
    assert runtime.code_ready(report, '1.0.0', 'synthetic')


def test_real_stop_rpc_accepts_before_pin_while_candidate_revision_is_running(fixture, monkeypatch):
    from ala import display, display_health, monitors
    from test_display import FakeHost

    paths, host, _, store, config, *_ = fixture
    request = submit(fixture)
    monkeypatch.setattr(screens, 'screen_action', SCREEN_ACTION)
    value = screen_swap.Store(paths, host).load(request['operation_id'])
    value.update(state='running', phase='rollback', decision='rollback', lifecycle_started=True)
    screen_swap.Store(paths, host).save(value)
    before, after = (value['participants']['default'][side] for side in ('before', 'after'))
    home = paths.at('/home/user')
    home.mkdir(parents=True)
    paths.at('/run/user/test').mkdir(parents=True)
    metadata = Path.lstat
    def root_metadata(path, *args, **kwargs):
        result = metadata(path, *args, **kwargs)
        if path == paths.config:
            fields = list(result)
            fields[4] = 0
            return os.stat_result(fields)
        return result
    monkeypatch.setattr(Path, 'lstat', root_metadata)
    monkeypatch.setattr(display.os, 'getuid', lambda: 12345)
    monkeypatch.setattr(display.pwd, 'getpwuid', lambda _uid: SimpleNamespace(pw_name='user', pw_dir=str(home)))

    class RpcHost(FakeHost):
        def run(self, args, **kwargs):
            if args[0] == 'runuser':
                self.calls.append((args, kwargs))
                child_environment = {}
                start = args.index('-i') + 1
                while '=' in args[start] and not args[start].startswith('/'):
                    key, text = args[start].split('=', 1)
                    child_environment[key] = text
                    start += 1
                action = args[args.index('_chrome-user') + 1]
                with monkeypatch.context() as child:
                    for key in ('ALA_RUNTIME_CONFIG', 'ALA_RUNTIME_TARGET', 'ALA_RUNTIME_ACTION'):
                        child.delenv(key, raising=False)
                    for key, text in child_environment.items():
                        child.setenv(key, text)
                    response = display.user_action(action, paths=paths, host=self)
                return Command(response['exit_code'], json.dumps(response))
            return super().run(args, **kwargs)

    rpc = RpcHost(paths, config)
    rpc.states[display.UNIT].update(ActiveState='active', MainPID='100')
    health = {'supervisor_pid': 100, 'browser_pid': 101, 'supervisor_start_ticks': '1',
              'browser_start_ticks': '2', 'boot_id': 'synthetic-boot', 'config_revision': after['revision']}
    monkeypatch.setattr(display, '_check_user', lambda *_: {'process_verified': True, 'health': health})
    monkeypatch.setattr(display, '_foreign_profile_pid', lambda *_args, **_kwargs: None)
    def identity(_paths, _pid):
        if rpc.states[display.UNIT]['ActiveState'] == 'inactive':
            raise FileNotFoundError('synthetic browser has exited')
        return '2'
    monkeypatch.setattr(display_health, 'process_identity', identity)
    monkeypatch.setattr(monitors, 'bridge', lambda method, *_args, **_kwargs:
                        {'state': 'ok', 'windows': [{'id': 77}]} if method == 'Inspect'
                        else {'state': 'ok', 'remaining_window_count': 0})
    assert before['revision'] != after['revision']
    result = screen_swap._control(screen_swap.Store(paths, rpc), 'default', 'stop', before, timeout=30)
    assert result['state'] == 'stopped' and result['exit_code'] == 0
    assert any(args[:3] == ['systemctl', '--user', 'stop'] for args, _ in rpc.calls)


def test_process_termination_between_stops_restores_both_before_next_trial(fixture):
    paths, host, _, _, before, *_ = fixture
    request = submit(fixture)
    controller, calls = control_for(fixture)
    def interrupted(target, action, selection, **kwargs):
        if target == 'other' and action == 'stop':
            raise KeyboardInterrupt('synthetic worker termination between stops')
        return controller(target, action, selection, **kwargs)
    with pytest.raises(KeyboardInterrupt):
        execute(fixture, request['operation_id'], control=interrupted)
    value = screen_swap.Store(paths, host).load(request['operation_id'])
    assert value['phase'] == 'stopping' and value['decision'] is None
    host.units.discard(value['unit'])
    assert execute(fixture, request['operation_id'], control=controller, recovering=True)['state'] == 'rolled_back'
    assert Config.load(paths.config) == before
    assert ('default', 'start', 'HDMI-1') in calls
    assert ('other', 'start', 'DP-1') in calls
    assert not any(action == 'start' and output != ('HDMI-1' if target == 'default' else 'DP-1')
                   for target, action, output in calls)


def test_single_apply_post_commit_autostart_failure_keeps_applied_selection(fixture, monkeypatch):
    paths, host, clock, store, config, _, releases, _ = fixture
    saved = replace(config, screens=(replace(config.screens[0], url='https://example.com/new'), config.screens[1]))
    atomic_json(paths.config, saved.to_dict(), 0o644)
    sync_calls = []
    def sync(actual, *_):
        sync_calls.append(actual)
        if len(sync_calls) == 2:
            raise RuntimeError('synthetic autostart registration failure')
    monkeypatch.setattr(runtime, '_sync_autostart', sync)
    request = runtime.submit_apply('default', release=releases[1], paths=paths, host=host, settings_config=lambda: saved)
    controller, calls = control_for(fixture)
    result = runtime.apply_target('default', paths=paths, host=host, monotonic=clock, sleep=clock.sleep,
        control=lambda action, selection, **kwargs: controller('default', action, selection, **kwargs),
        unit=request['unit'], settings_operation_id=request['operation_id'])
    assert result['state'] == 'pending' and result['applied'] is True
    selected = Config.from_dict(store._validate_selection(store.load('default')['committed'])).for_screen('default')
    assert selected.url == 'https://example.com/new'
    assert len(sync_calls) == 2
    assert sum(action == 'start' for _, action, _ in calls) == 1


@pytest.mark.parametrize('action', ['start', 'restart', 'preflight'])
def test_old_pinned_runtime_guides_code_update_before_setup_page_start(fixture, monkeypatch, action):
    from ala import display
    paths, host, _, store, config, _, releases, _ = fixture
    metadata = releases[0] / 'COMPATIBILITY.json'
    value = json.loads(metadata.read_text())
    value.pop('setup_page_protocol')
    metadata.write_text(json.dumps(value))
    selected = store.load('other')['committed']
    model = replace(config, runtime_pin=selected).for_screen('other')
    monkeypatch.setattr(screens, 'screen_action', SCREEN_ACTION)
    monkeypatch.setattr(display, 'chrome_action', lambda *_args, **_kwargs: pytest.fail('old runtime launch'))
    response = screens.screen_action(action, model, target='other', paths=paths, host=host)
    assert response['state'] == 'pending' and response['exit_code'] == 2
    assert '화면 코드' in response['next_action']


def test_settings_only_null_url_requires_capability_without_reserving_or_starting(fixture):
    paths, host, _, store, config, _, releases, _ = fixture
    metadata = releases[0] / 'COMPATIBILITY.json'
    value = json.loads(metadata.read_text())
    value.pop('setup_page_protocol')
    metadata.write_text(json.dumps(value))
    saved = replace(config, screens=(replace(config.screens[0], url=None), config.screens[1]))
    original = deepcopy(store.load('default'))
    response = runtime.submit_apply('default', release=releases[1], paths=paths, host=host, settings_config=lambda: saved)
    assert response['state'] == 'pending'
    assert store.load('default') == original
    assert not any(args[0] == 'systemd-run' for args in host.calls)


def test_interrupted_partial_rollback_resumes_on_next_boot(fixture, monkeypatch):
    paths, host, _, store, before, *_ = fixture
    originals = {target: deepcopy(store.load(target)) for target in ('default', 'other')}
    request = submit(fixture)
    write = runtime.RuntimeStore._write
    interrupted = False
    def terminate_second_restore(self, path, value):
        nonlocal interrupted
        if path == self.state_path('other') and value.get('operation') is None and not interrupted:
            interrupted = True
            raise KeyboardInterrupt('synthetic termination halfway through rollback')
        return write(self, path, value)
    monkeypatch.setattr(runtime.RuntimeStore, '_write', terminate_second_restore)
    controller, _ = control_for(fixture, fail_target='other')
    with pytest.raises(KeyboardInterrupt):
        execute(fixture, request['operation_id'], control=controller)
    journal = screen_swap.Store(paths, host).load(request['operation_id'])
    assert journal['automatic_recovery_attempted'] is True
    assert journal['state'] == 'running' and journal['decision'] == 'rollback'
    assert store.load('default')['operation'] is None
    assert store.load('other')['operation'] is not None
    paths.at('/proc/sys/kernel/random/boot_id').write_text('next-synthetic-boot')
    assert screen_swap.recover_all(paths=paths, host=host) == 0
    assert Config.load(paths.config) == before
    assert screen_swap.status(paths=paths, host=host)['state'] == 'rolled_back'
    for target in originals:
        current = store.load(target)
        assert current['committed'] == originals[target]['committed']
        assert current['desired'] == originals[target]['desired']
        assert current['operation'] is None


def test_interrupted_partial_automatic_commit_redo_resumes_after_owner_exit(fixture, monkeypatch):
    paths, host, _, store, *_ = fixture
    request = submit(fixture)
    write = runtime.RuntimeStore._write
    interruption = 0
    def terminate_commit_then_redo(self, path, value):
        nonlocal interruption
        if value.get('operation') is None and path == self.state_path('default') and interruption == 0:
            interruption = 1
            raise KeyboardInterrupt('synthetic original worker termination after commit decision')
        if value.get('operation') is None and path == self.state_path('other') and interruption == 1:
            interruption = 2
            raise KeyboardInterrupt('synthetic automatic recovery termination halfway through redo')
        return write(self, path, value)
    monkeypatch.setattr(runtime.RuntimeStore, '_write', terminate_commit_then_redo)
    controller, _ = control_for(fixture)
    with pytest.raises(KeyboardInterrupt):
        execute(fixture, request['operation_id'], control=controller)
    journals = screen_swap.Store(paths, host)
    value = journals.load(request['operation_id'])
    host.units.discard(value['unit'])
    with pytest.raises(KeyboardInterrupt):
        screen_swap.recover_all(paths=paths, host=host)
    value = journals.load(request['operation_id'])
    assert value['automatic_recovery_attempted'] is True
    assert value['state'] == 'running' and value['decision'] == 'commit'
    assert value['recovery_unit'] == 'ala-screen-swap-recover.service'
    assert store.load('default')['committed'] == value['participants']['default']['after']
    assert store.load('other')['committed'] == value['participants']['other']['before']
    host.units.discard('ala-screen-swap-recover.service')
    assert screen_swap.recover_all(paths=paths, host=host) == 0
    assert journals.load(request['operation_id'])['state'] == 'completed'
    assert Config.load(paths.config).to_dict() == value['after_config']
    for target, part in value['participants'].items():
        current = store.load(target)
        assert current['committed'] == part['after']
        assert current['desired'] == part['desired_after']
        assert current['operation'] is None


@pytest.mark.parametrize('phase,decision', [('rollback', 'rollback'), ('committing', 'commit')])
def test_live_recovery_owner_is_not_duplicated_even_after_attempt_started(fixture, monkeypatch, phase, decision):
    paths, host, *_ = fixture
    request = submit(fixture)
    journals = screen_swap.Store(paths, host)
    value = journals.load(request['operation_id'])
    value.update(state='running', phase=phase, decision=decision, automatic_recovery_attempted=True,
                 recovery_unit='ala-screen-swap-recover.service')
    journals.save(value)
    monkeypatch.setattr(screen_swap, '_execute', lambda *_args, **_kwargs: pytest.fail('live recovery duplicated'))
    assert screen_swap.recover_all(paths=paths, host=host) == 0
