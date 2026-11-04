"""A manual reload has the same short-lock/generation fence as automatic work."""
import json
from pathlib import Path

import pytest

from ala import display, runtime, screen_control
from ala.config import Config
from ala.jobs import change_lock
from ala.system import Command, Paths


class Host:
    def __init__(self, paths):
        self.paths, self.calls = paths, []
        self.launch_code = 0
        self.active = False
        self.reply = {'state': 'completed', 'exit_code': 0, 'nonce': 'b' * 32}
        self.after_control = None
        self.controlled = False

    def run(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if argv[:2] == ['systemctl', 'show']:
            return Command(0, 'LoadState=loaded\nActiveState=' + ('active' if self.active else 'inactive'))
        if argv[0] == 'systemd-run':
            self.active = self.launch_code in {0, 124}
            return Command(self.launch_code)
        if argv[0] == 'runuser':
            # A stop/configuration request can acquire the root lock even while
            # the private CDP helper is waiting for its real document result.
            with change_lock(self.paths):
                pass
            self.controlled = True
            if self.after_control:
                self.after_control()
            return Command(0, json.dumps(self.reply))
        pytest.fail('Unexpected host operation in the synthetic reload test')


def release(paths, version):
    path = paths.at('/opt/ala/releases') / version
    (path / '.venv/bin').mkdir(parents=True)
    (path / 'VERSION').write_text(version)
    (path / 'entry.py').write_text('# synthetic release\n')
    (path / '.venv/bin/python').write_text('# synthetic interpreter\n')
    (path / 'COMPATIBILITY.json').write_text(json.dumps({
        'schema_version': 1, 'runtime_protocol': 1, 'config_schemas': [1, 2]}))
    return path


@pytest.fixture
def case(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    boot = paths.at('/proc/sys/kernel/random/boot_id')
    boot.parent.mkdir(parents=True)
    boot.write_text('synthetic-boot')
    host = Host(paths)
    old, current = release(paths, '1.0.0'), release(paths, '2.0.0')
    config = Config(user='user', url='https://example.com/committed')
    store = runtime.RuntimeStore(paths, host)
    monkeypatch.setattr(runtime.RuntimeStore, '_session', lambda *_: 'synthetic-session')
    state = store.bootstrap('default', old, config)
    inspections = []

    def inspect(action, actual, **kwargs):
        assert action == 'check'
        with change_lock(paths):
            pass
        inspections.append(actual)
        return {'process_verified': True, 'service': {'ActiveState': 'active'},
                'health': {'state': 'healthy', 'config_revision': state['committed']['revision'],
                           'run_nonce': 'a' * 32,
                           'control': {'nonce': 'b' * 32, 'state': 'completed'} if host.controlled else {}}}

    monkeypatch.setattr(display, 'chrome_action', inspect)
    monkeypatch.setattr(display, '_account', lambda *_: (1000, 1000, '/synthetic/home'))
    return paths, host, store, current, config, inspections


def submit(case, **kwargs):
    paths, host, _, current, config, _ = case
    return screen_control.submit_reload(config, release=current, paths=paths, host=host, **kwargs)


def run(case, value):
    paths, host, *_ = case
    return screen_control.run_worker(value['run_id'], paths=paths, host=host)


def test_submission_is_detached_target_fixed_and_only_an_accepted_request(case):
    paths, host, store, current, *_ = case
    value = submit(case)
    assert value['state'] == 'queued' and value['exit_code'] == 2
    assert store.load('default')['operation'] is None
    command = next(args for args, _ in host.calls if args[0] == 'systemd-run')
    assert '--unit=ala-screen-reload-default.service' in command
    assert '--expand-environment=no' in command and '--property=UMask=0077' in command
    assert command[-4:] == ['-I', str(current / 'entry.py'), '_screen-worker', value['run_id']]
    assert not any(args[0] == 'runuser' for args, _ in host.calls)
    record = screen_control.ControlStore(paths, host).path(value['run_id'])
    assert record.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('api', ['chrome', 'screens-action'])
def test_public_reload_enters_the_detached_runner_without_holding_an_outer_lock(case, monkeypatch, api):
    from ala.service import Operations
    paths, host, _, current, config, _ = case
    operations = Operations(paths=paths, host=host, release=current)
    monkeypatch.setattr(operations, 'config', lambda: config)

    value = operations.dispatch(api, {'action': 'reload', 'target': 'default'})

    assert value['state'] == 'queued' and value['exit_code'] == 2
    assert not host.controlled
    assert any(args[-2:] == ['_screen-worker', value['run_id']] for args, _ in host.calls)


def test_reload_uses_committed_config_and_checks_nonce_after_completion_without_a_long_lock(case):
    paths, host, store, current, config, inspections = case
    newer = Config(user='user', url='https://example.com/desired')
    store.stage('default', current, newer)
    value = screen_control.submit_reload(newer, release=current, paths=paths, host=host)
    assert run(case, value) == 0
    assert [actual.url for actual in inspections] == [config.url, config.url]
    assert all(actual.runtime_pin['version'] == '1.0.0' for actual in inspections)
    command = next(args for args, _ in host.calls if args[0] == 'runuser')
    assert command[:4] == ['runuser', '-u', 'user', '--']
    assert str(current / 'entry.py') in command
    payload = json.loads(command[-1])
    assert payload['source'] == 'manual'
    assert payload['operation_token']['revision'] == store.load('default')['committed']['revision']
    assert store.load('default')['operation'] is None
    assert screen_control.read_status(paths=paths, host=host)['state'] == 'completed'


def test_same_target_duplicate_is_refused_but_another_target_can_be_queued(case):
    paths, host, store, current, config, _ = case
    submit(case)
    with pytest.raises(RuntimeError):
        submit(case)
    host.active = False  # A different target has an independent fixed unit.
    multi = Config.from_dict({'schema_version': 2, 'user': 'user', 'screens': [
        {'id': 'default', 'url': config.url}, {'id': 'display-2', 'url': 'https://example.com/second'}]})
    store.bootstrap('display-2', current, multi)
    second = screen_control.submit_reload(multi, 'display-2', current, paths=paths, host=host)
    assert second['unit'] == 'ala-screen-reload-display-2.service'


def test_unknown_worker_launch_is_not_reported_as_completed_and_can_be_claimed_once(case):
    case[1].launch_code = 124
    value = submit(case)
    assert value['state'] == 'unknown' and value['exit_code'] == 1
    assert run(case, value) == 0
    saved = screen_control.ControlStore(case[0], case[1]).path(value['run_id']).read_bytes()
    assert run(case, value) == 1
    assert screen_control.ControlStore(case[0], case[1]).path(value['run_id']).read_bytes() == saved


def test_manual_stop_before_worker_start_prevents_any_reload(case):
    paths, host, store, *_ = case
    value = submit(case)
    store.cancel('default', stopped=True)
    assert run(case, value) == 1
    assert not host.controlled
    assert store.is_stopped(store.load('default'))


def test_manual_stop_while_cdp_waits_cancels_the_result_without_reviving_the_window(case):
    paths, host, store, *_ = case
    value = submit(case)
    host.after_control = lambda: runtime.cancel('default', paths=paths, host=host)
    assert run(case, value) == 1
    assert store.is_stopped(store.load('default'))
    assert store.load('default')['last_result']['state'] == 'cancelled'
    assert screen_control.read_status(paths=paths, host=host)['state'] == 'cancelled'
    assert not any('start' in args for args, _ in host.calls)


@pytest.mark.parametrize('kind', ['frontend', 'runtime'])
def test_existing_automatic_reservation_blocks_a_manual_reload(case, kind):
    paths, host, store, *_ = case
    token = store.reserve('default', kind)
    with pytest.raises(RuntimeError):
        submit(case)
    assert store.valid(token)
    assert not host.controlled


def test_reservation_taken_after_queueing_still_blocks_the_worker(case):
    _, host, store, *_ = case
    value = submit(case)
    token = store.reserve('default', 'frontend')
    assert run(case, value) == 1
    assert store.valid(token) and not host.controlled


def test_missing_pin_is_pending_without_bootstrap_or_root_execution(case):
    paths, host, store, *_ = case
    store.state_path('default').unlink()
    value = submit(case)
    assert value['state'] == 'pending' and value['exit_code'] == 2
    assert store.load('default') is None
    assert not host.calls and not paths.at('/var/lib/ala/screen-control').exists()


def test_older_supervisor_without_control_nonce_remains_pending(case, monkeypatch):
    _, host, store, *_ = case
    monkeypatch.setattr(display, 'chrome_action', lambda *a, **kw: {
        'process_verified': True, 'service': {'ActiveState': 'active'}, 'health': {'state': 'healthy'}})
    value = submit(case)
    assert run(case, value) == 2
    assert not host.controlled and store.load('default')['operation'] is None
    assert screen_control.read_status(paths=case[0], host=host)['state'] == 'pending'


@pytest.mark.parametrize('reply', [
    {'state': 'pending', 'exit_code': 2, 'nonce': 'b' * 32},
    {'state': 'completed', 'exit_code': 0},
    {'state': 'completed', 'exit_code': False, 'nonce': 'b' * 32},
    {'state': 'completed', 'exit_code': 0, 'nonce': 'c' * 32},
])
def test_acceptance_or_mismatched_nonce_never_becomes_actual_completion(case, reply):
    paths, host, store, *_ = case
    host.reply = reply
    value = submit(case)
    assert run(case, value) == 1
    assert screen_control.read_status(paths=paths, host=host)['state'] == 'failed'
    assert store.load('default')['operation'] is None


def test_boot_change_is_reported_readonly_without_resuming_or_editing_records(case):
    paths, host, store, *_ = case
    value = submit(case)
    record = screen_control.ControlStore(paths, host).path(value['run_id'])
    before = record.read_bytes(), record.stat().st_mtime_ns
    paths.at('/proc/sys/kernel/random/boot_id').write_text('next-synthetic-boot')
    assert screen_control.read_status(paths=paths, host=host)['state'] == 'interrupted'
    assert run(case, value) == 1
    assert (record.read_bytes(), record.stat().st_mtime_ns) == before
    assert not host.controlled


def test_a_malformed_user_owned_ack_is_a_recorded_failure_not_success_or_a_worker_crash(case, monkeypatch):
    paths, host, store, _, _, inspections = case
    previous = display.chrome_action
    def malformed(*args, **kwargs):
        value = previous(*args, **kwargs)
        if host.controlled:
            value['health']['control'] = ['not-an-acknowledgement']
        return value
    monkeypatch.setattr(display, 'chrome_action', malformed)
    value = submit(case)
    assert run(case, value) == 1
    assert len(inspections) == 2 and store.load('default')['operation'] is None
    assert screen_control.read_status(paths=paths, host=host)['state'] == 'failed'


@pytest.mark.parametrize('target', ['../default', 'default;false', '', True])
def test_target_and_record_paths_are_data_and_reject_shell_or_path_syntax(case, target):
    with pytest.raises(ValueError):
        submit(case, target=target)
    assert not case[1].calls


def test_symlinked_control_storage_is_rejected_before_launch(case, tmp_path):
    paths, host, *_ = case
    base = paths.at('/var/lib/ala/screen-control')
    base.parent.mkdir(parents=True)
    outside = tmp_path / 'unrelated'
    outside.mkdir()
    base.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PermissionError):
        submit(case)
    assert not any(args[0] == 'systemd-run' for args, _ in host.calls)
    assert not list(outside.iterdir())
