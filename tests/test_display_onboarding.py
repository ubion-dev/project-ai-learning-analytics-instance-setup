"""Privileged onboarding with synthetic outputs; never controls a real session."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from ala import display_onboarding as onboarding
from ala.config import Config, ScreenConfig
from ala.jobs import JobStore, run_worker
from ala.model import CheckResult, Report
from ala.runtime import RuntimeStore
from ala.system import Command, Paths, atomic_json


def idle_startup():
    return {'state': 'ok', 'exit_code': 0, 'service': {'ActiveState': 'inactive'},
            'legacy_service': {'ActiveState': 'inactive'}}


class Host:
    def __init__(self):
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        return Command(0)


@pytest.fixture
def environment(tmp_path, monkeypatch):
    paths, host = Paths(tmp_path), Host()
    release = paths.at('/opt/ala/releases/0.1.0rc27')
    (release / '.venv/bin').mkdir(parents=True)
    (release / '.venv/bin/python').touch()
    (release / 'entry.py').touch()
    (release / 'VERSION').write_text('0.1.0rc27\n')
    (release / 'COMPATIBILITY.json').write_text(json.dumps({
        'schema_version': 1, 'runtime_protocol': 1, 'config_schemas': [1, 2],
    }))
    boot = paths.at('/proc/sys/kernel/random/boot_id')
    boot.parent.mkdir(parents=True)
    boot.write_text('boot-one')
    config = Config(user='user', autostart=False)
    atomic_json(paths.config, config.to_dict(), 0o644)
    inventory = {'state': 'ok', 'logical_outputs': [
        {'id': 'HDMI-1+HDMI-2', 'connectors': ['HDMI-1', 'HDMI-2'], 'confirmed': True,
         'x': 0, 'y': 0, 'width': 1920, 'height': 1080, 'primary': False},
        {'id': 'DP-1', 'connectors': ['DP-1'], 'confirmed': True,
         'x': 1920, 'y': 0, 'width': 1920, 'height': 1080, 'primary': True},
    ]}
    starts = []
    monkeypatch.setattr('ala.screens.inspect_session', lambda *args: 'ok')
    monkeypatch.setattr('ala.screens._inventory', lambda *args: deepcopy(inventory))
    monkeypatch.setattr('ala.display.apply_display', lambda *args, **kwargs: [
        CheckResult('chrome.unit', 'ok'), CheckResult('chrome.url', 'pending'),
    ])

    def user_call(config, host, paths, action):
        if action == 'startup-status':
            return idle_startup()
        assert action == 'ensure-start'
        starts.append(config.target_id)
        return {'state': 'starting', 'exit_code': 0}

    monkeypatch.setattr('ala.display._user_call', user_call)
    return paths, host, release, config, inventory, starts


def test_preview_is_read_only_and_uses_primary_and_logical_clone(environment):
    paths, host, _, config, _, starts = environment
    before = {str(path.relative_to(paths.root)): path.read_bytes()
              for path in paths.root.rglob('*') if path.is_file()}
    result = onboarding.preview(config, paths=paths, host=host)
    assert result['state'] == 'ready'
    assert len(result['targets']) == 2
    assert next(target for target in result['targets'] if target['id'] == 'default')['output'] == 'DP-1'
    assert {target['output'] for target in result['targets']} == {'DP-1', 'HDMI-1+HDMI-2'}
    assert all(target['url'] is None and target['autostart'] for target in result['targets'])
    assert result['changes'][0]['autostart_before'] is False
    assert not starts and not host.calls
    assert before == {str(path.relative_to(paths.root)): path.read_bytes()
                      for path in paths.root.rglob('*') if path.is_file()}


def test_request_prepares_each_output_once_and_preserves_non_display_options(environment):
    paths, host, release, config, _, starts = environment
    config = replace(config, root_bash=True, dev_tools=True)
    atomic_json(paths.config, config.to_dict(), 0o644)
    result = onboarding.request(config, release, paths=paths, host=host, run_id='test-run')
    assert result['state'] == 'completed'
    saved = Config.load(paths.config)
    assert saved.schema_version == 2 and saved.root_bash and saved.dev_tools
    assert len(saved.screen_targets()) == 2
    assert set(starts) == {screen.id for screen in saved.screen_targets()}
    store = RuntimeStore(paths, host)
    assert len(store.selections()) == 2
    for state in store.selections():
        assert store._validate_selection(state['committed']) == saved.to_dict()
    assert not onboarding.pending_for_user(saved, paths)
    assert ['systemctl', 'enable', '--now', onboarding.TIMER] in host.calls
    assert ['systemctl', 'disable', '--now', onboarding.TIMER] in host.calls
    before = list(starts)
    assert onboarding.run_pending(paths=paths, host=host)['state'] == 'completed'
    assert starts == before


def test_no_graphical_session_defers_without_requiring_another_setup(environment, monkeypatch):
    paths, host, release, config, _, starts = environment
    monkeypatch.setattr('ala.screens.inspect_session', lambda *args: 'pending')
    result = onboarding.request(config, release, paths=paths, host=host)
    assert result['state'] == 'pending'
    assert onboarding.pending_for_user(config, paths)
    assert Config.load(paths.config) == config and not starts
    paths.at('/proc/sys/kernel/random/boot_id').write_text('boot-two')
    monkeypatch.setattr('ala.screens.inspect_session', lambda *args: 'ok')
    result = onboarding.run_pending(paths=paths, host=host)
    assert result['state'] == 'completed' and len(starts) == 2


def test_extension_pending_keeps_fence_until_units_ready(environment, monkeypatch):
    paths, host, release, config, _, starts = environment
    monkeypatch.setattr('ala.display.apply_display', lambda *args, **kwargs: [
        CheckResult('display.prepare', 'pending'),
    ])
    assert onboarding.request(config, release, paths=paths, host=host)['state'] == 'pending'
    assert onboarding.status(paths=paths)['phase'] == 'configured'
    assert onboarding.pending_for_user(Config.load(paths.config), paths) and not starts
    monkeypatch.setattr('ala.display.apply_display', lambda *args, **kwargs: [CheckResult('chrome.unit', 'ok')])
    assert onboarding.run_pending(paths=paths, host=host)['state'] == 'completed'
    assert len(starts) == 2


@pytest.mark.parametrize('state', ['blocked', 'error', 'unknown', 'change_required', 'pending'])
def test_registration_issue_exposes_its_reason_without_releasing_partial_config(environment, monkeypatch, state):
    paths, host, release, config, _, starts = environment
    reason = '관리되지 않은 자동 실행 파일을 변경하지 않습니다.'
    action = '기존 자동 실행 파일을 확인한 뒤 초기 준비 상태를 다시 확인하세요.'
    monkeypatch.setattr('ala.display.apply_display', lambda *args, **kwargs: [
        CheckResult('display.apply', state, actual=reason, next_action=action),
        CheckResult('display.prepare', 'pending', actual='확장 활성화 대기'),
    ])
    result = onboarding.request(config, release, paths=paths, host=host)
    assert result['state'] == 'pending' and result['phase'] == 'configured'
    assert reason in result['reason'] and result['next_action'] == action
    assert result['registration_issue'] == {
        'id': 'display.apply', 'state': state, 'reason': reason, 'next_action': action,
    }
    assert onboarding.pending_for_user(Config.load(paths.config), paths) and not starts
    assert onboarding.run_pending(paths=paths, host=host)['registration_issue'] == result['registration_issue']
    assert onboarding.pending_for_user(Config.load(paths.config), paths) and not starts
    monkeypatch.setattr('ala.display.apply_display', lambda *args, **kwargs: [CheckResult('chrome.unit', 'ok')])
    completed = onboarding.run_pending(paths=paths, host=host)
    assert completed['state'] == 'completed' and 'registration_issue' not in completed
    assert len(starts) == 2


def test_interruption_after_config_write_resumes_candidate_without_duplicate_targets(environment, monkeypatch):
    paths, host, release, config, _, starts = environment
    original = onboarding.atomic_json
    interrupted = []

    def write(path, value, mode=0o600):
        original(path, value, mode)
        if path == paths.config and not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt()

    monkeypatch.setattr(onboarding, 'atomic_json', write)
    with pytest.raises(KeyboardInterrupt):
        onboarding.request(config, release, paths=paths, host=host)
    assert onboarding.status(paths=paths)['phase'] == 'prepared'
    assert onboarding.pending_for_user(Config.load(paths.config), paths)
    monkeypatch.setattr(onboarding, 'atomic_json', original)
    assert onboarding.run_pending(paths=paths, host=host)['state'] == 'completed'
    assert len(Config.load(paths.config).screen_targets()) == 2
    assert len(starts) == 2


def test_configuration_change_cancels_pending_and_never_overwrites_it(environment, monkeypatch):
    paths, host, release, config, _, starts = environment
    monkeypatch.setattr('ala.screens.inspect_session', lambda *args: 'pending')
    onboarding.request(config, release, paths=paths, host=host)
    changed = replace(config, url='https://example.com/display')
    atomic_json(paths.config, changed.to_dict(), 0o644)
    assert onboarding.run_pending(paths=paths, host=host)['state'] == 'cancelled'
    assert Config.load(paths.config) == changed
    assert not starts and not onboarding.pending_for_user(changed, paths)


@pytest.mark.parametrize('active', ['active', 'activating', 'failed', None])
def test_existing_running_failed_or_unknown_chrome_is_never_replaced(environment, monkeypatch, active):
    paths, host, release, config, _, starts = environment
    monkeypatch.setattr('ala.display._user_call', lambda *args: idle_startup() | {
        'service': {'ActiveState': active},
    })
    result = onboarding.request(config, release, paths=paths, host=host)
    assert result['state'] == ('pending' if active is None else 'blocked')
    assert Config.load(paths.config) == config and not starts


def test_explicit_blank_initialization_can_turn_on_inactive_registered_targets(environment):
    paths, host, release, config, _, starts = environment
    RuntimeStore(paths, host).bootstrap('default', release, config)
    result = onboarding.request(config, release, paths=paths, host=host)
    assert result['state'] == 'completed'
    saved = Config.load(paths.config)
    assert all(screen.autostart for screen in saved.screen_targets())
    assert len(starts) == 2


@pytest.mark.parametrize('legacy', ['active', 'failed', None])
def test_legacy_chrome_is_preserved_before_any_config_change(environment, monkeypatch, legacy):
    paths, host, release, config, _, starts = environment
    monkeypatch.setattr('ala.display._user_call', lambda *args: idle_startup() | {
        'legacy_service': {'ActiveState': legacy},
    })
    assert onboarding.request(config, release, paths=paths, host=host)['state'] == ('pending' if legacy is None else 'blocked')
    assert Config.load(paths.config) == config and not starts


def test_user_service_query_delay_retries_without_a_second_setup(environment, monkeypatch):
    paths, host, release, config, _, starts = environment
    from ala import display
    original = display._user_call
    monkeypatch.setattr('ala.display._user_call', lambda *args: {
        'state': 'unknown', 'exit_code': 1, 'reason': '사용자 서비스 상태 조회 대기',
    })
    assert onboarding.request(config, release, paths=paths, host=host)['state'] == 'pending'
    assert Config.load(paths.config) == config and not starts
    assert onboarding.pending_for_user(config, paths)
    monkeypatch.setattr('ala.display._user_call', original)
    assert onboarding.run_pending(paths=paths, host=host)['state'] == 'completed'
    assert len(starts) == 2


def test_missing_manager_environment_does_not_deadlock_disabled_autostart_registration(environment, monkeypatch):
    paths, host, release, config, _, starts = environment
    import ala.display as display
    original = display._user_call
    waiting = [True]

    def user_call(model, host, paths, action):
        if action == 'ensure-start' and waiting[0]:
            return {'state': 'pending', 'exit_code': 2, 'reason': '사용자 화면 환경 대기'}
        return original(model, host, paths, action)

    monkeypatch.setattr(display, '_user_call', user_call)
    monkeypatch.setattr(display, 'chrome_action', lambda *args, **kwargs: pytest.fail('manager inspection before registration'))
    assert config.autostart is False
    assert onboarding.request(config, release, paths=paths, host=host)['state'] == 'pending'
    assert onboarding.status(paths=paths)['phase'] == 'starting'
    saved = Config.load(paths.config)
    assert saved.schema_version == 2 and all(screen.autostart for screen in saved.screen_targets())
    assert not onboarding.pending_for_user(saved, paths) and not starts
    waiting[0] = False
    assert onboarding.run_pending(paths=paths, host=host)['state'] == 'completed'
    assert len(starts) == 2


def test_interruption_before_fence_release_clears_it_before_ensure_start(environment, monkeypatch):
    paths, host, release, config, _, starts = environment
    original = onboarding._fence
    interrupted = []

    def fence(paths, value, *, pending, host=None):
        if not pending and not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt()
        return original(paths, value, pending=pending, host=host)

    monkeypatch.setattr(onboarding, '_fence', fence)
    with pytest.raises(KeyboardInterrupt):
        onboarding.request(config, release, paths=paths, host=host)
    assert onboarding.status(paths=paths)['phase'] == 'starting'
    assert onboarding.pending_for_user(Config.load(paths.config), paths)
    monkeypatch.setattr(onboarding, '_fence', original)

    def user_call(config, host, paths, action):
        assert not onboarding.pending_for_user(config, paths)
        starts.append(config.target_id)
        return {'state': 'starting', 'exit_code': 0}

    monkeypatch.setattr('ala.display._user_call', user_call)
    assert onboarding.run_pending(paths=paths, host=host)['state'] == 'completed'
    assert len(starts) == 2


def test_manual_stop_prevents_initialization_from_reviving_a_target(environment, monkeypatch):
    paths, host, release, config, _, starts = environment
    store = RuntimeStore(paths, host)
    state = store.bootstrap('default', release, config)
    state.update(stopped=True, stop_boot_id='boot-one', stop_session_id=None)
    store._write(store.state_path('default'), state)
    assert onboarding.request(config, release, paths=paths, host=host)['state'] == 'blocked'
    assert Config.load(paths.config) == config and not starts


def test_a_configured_site_is_ineligible_even_when_its_autostart_is_off(environment):
    paths, host, release, config, _, starts = environment
    configured = replace(config, url='https://example.com/display')
    atomic_json(paths.config, configured.to_dict(), 0o644)
    assert onboarding.preview(configured, paths=paths, host=host)['state'] == 'blocked'
    assert onboarding.request(configured, release, paths=paths, host=host)['state'] == 'blocked'
    assert not onboarding._path(paths).exists()
    assert Config.load(paths.config) == configured and not starts


@pytest.mark.parametrize('condition', ['unconfirmed', 'too-many', 'overlapping'])
def test_incomplete_or_unsafe_inventory_never_partially_configures_outputs(environment, condition):
    paths, host, release, config, inventory, starts = environment
    if condition == 'unconfirmed':
        inventory['logical_outputs'][1]['confirmed'] = False
    elif condition == 'too-many':
        inventory['logical_outputs'] = [dict(inventory['logical_outputs'][0], id=f'HDMI-{i}') for i in range(17)]
    else:
        config = replace(config, schema_version=2, screens=(
            ScreenConfig(id='default', output='HDMI-1', layout='left', autostart=False),
            ScreenConfig(id='second', output='HDMI-2', layout='right', autostart=False),
        ))
        atomic_json(paths.config, config.to_dict(), 0o644)
    result = onboarding.request(config, release, paths=paths, host=host)
    assert result['state'] in {'blocked', 'pending'}
    assert Config.load(paths.config) == config and not starts


def test_fence_is_read_only_and_rejects_malformed_or_symlinked_records(environment):
    paths, _, _, config, _, _ = environment
    assert not onboarding.pending_for_user(config, paths)
    assert not onboarding._fence_path(paths).exists()
    store = RuntimeStore(paths)
    store._write(onboarding._fence_path(paths), {'schema_version': 1, 'user': 'user', 'pending': 'true'})
    with pytest.raises(ValueError):
        onboarding.pending_for_user(config, paths)
    onboarding._fence_path(paths).unlink()
    onboarding._fence_path(paths).symlink_to(paths.config)
    with pytest.raises(PermissionError):
        onboarding.pending_for_user(config, paths)


def test_fence_boolean_schema_does_not_bypass_login_protection(environment):
    paths, host, release, config, _, _ = environment
    onboarding.reserve(config, release, paths=paths, host=host)
    store = RuntimeStore(paths, host)
    value = store._read(onboarding._fence_path(paths))
    value.update(schema_version=True, pending=False)
    store._write(onboarding._fence_path(paths), value)
    with pytest.raises(ValueError):
        onboarding.pending_for_user(config, paths)


def test_foreign_schedule_and_untrusted_release_are_preserved(environment):
    paths, host, release, config, _, starts = environment
    foreign = paths.at('/etc/systemd/system/' + onboarding.SERVICE)
    foreign.parent.mkdir(parents=True)
    foreign.write_text('foreign service\n')
    with pytest.raises(ValueError, match='관리되지 않은'):
        onboarding.request(config, release, paths=paths, host=host)
    assert foreign.read_text() == 'foreign service\n'
    assert not starts
    with pytest.raises((PermissionError, FileNotFoundError)):
        onboarding.request(config, paths.root, paths=paths, host=host)


@pytest.mark.parametrize('chrome_state', ['completed', 'pending'])
def test_setup_worker_records_opt_in_and_continues_deferred_initialization(environment, monkeypatch, chrome_state):
    paths, host, release, config, _, _ = environment
    paths.config.unlink()
    jobs = JobStore(paths, host)
    job = jobs.submit(config, release, release / '.venv/bin/python', persist_config=True,
                      initialize_displays=True)
    assert job['first_config'] is True and job['initialize_displays'] is True
    calls = []

    def request(config, release, **kwargs):
        calls.append((config, release, kwargs))
        return {'state': 'pending', 'reason': 'GNOME 로그인 대기'}

    monkeypatch.setattr(onboarding, 'request', request)

    class Engine:
        def __init__(self, *args, **kwargs):
            pass

        def apply(self, on_stage):
            on_stage('chrome', chrome_state)
            return Report([CheckResult('base', 'ok')])

    assert run_worker(job['run_id'], paths, host, Engine) == 2
    recorded = jobs.load(job['run_id'])
    assert recorded['display_onboarding']['state'] == 'pending'
    assert calls[0][2]['locked'] is True and calls[0][2]['run_id'] == job['run_id']


@pytest.mark.parametrize('choice', [None, 'true', 1, []])
def test_setup_onboarding_requires_a_boolean(environment, choice):
    paths, host, release, config, _, _ = environment
    with pytest.raises(ValueError, match='true/false'):
        JobStore(paths, host).submit(config, release, release / '.venv/bin/python', initialize_displays=choice)


def test_existing_setup_without_opt_in_never_requests_initialization(environment, monkeypatch):
    paths, host, release, config, _, _ = environment
    jobs = JobStore(paths, host)
    job = jobs.submit(config, release, release / '.venv/bin/python')
    assert job['first_config'] is False and job['initialize_displays'] is False
    monkeypatch.setattr(onboarding, 'request', lambda *args, **kwargs: pytest.fail('unexpected onboarding'))

    class Engine:
        def __init__(self, *args, **kwargs):
            pass

        def apply(self, on_stage):
            on_stage('chrome', 'completed')
            return Report([CheckResult('base', 'ok')])

    assert run_worker(job['run_id'], paths, host, Engine) == 0


def test_submit_fences_login_before_worker_can_register_chrome(environment):
    paths, host, release, config, _, starts = environment
    job = JobStore(paths, host).submit(config, release, release / '.venv/bin/python',
                                     initialize_displays=True)
    assert onboarding.pending_for_user(config, paths)
    assert onboarding.status(paths=paths)['phase'] == 'installing'
    assert onboarding.status(paths=paths)['run_id'] == job['run_id']
    assert not starts
    assert not any(call[:3] == ['systemctl', 'enable', '--now'] for call in host.calls)
    assert onboarding.run_pending(paths=paths, host=host)['phase'] == 'installing'
    assert not starts


def test_worker_start_failure_keeps_visible_retry_state_and_explicit_site_can_run(environment, monkeypatch):
    paths, host, release, config, _, starts = environment
    original = host.run

    def failed(argv, **kwargs):
        if argv[0] == 'systemd-run':
            return Command(1, stderr='synthetic start failure')
        return original(argv, **kwargs)

    monkeypatch.setattr(host, 'run', failed)
    with pytest.raises(RuntimeError, match='설치 unit 시작 실패'):
        JobStore(paths, host).submit(config, release, release / '.venv/bin/python', initialize_displays=True)
    assert onboarding.status(paths=paths)['state'] == 'blocked'
    assert onboarding.status(paths=paths)['phase'] == 'install_failed'
    assert onboarding.pending_for_user(config, paths)
    changed = replace(config, url='https://example.com/display')
    atomic_json(paths.config, changed.to_dict(), 0o644)
    assert not onboarding.pending_for_user(changed, paths)
    assert not starts


@pytest.mark.parametrize('raises', [False, True])
def test_setup_failure_is_durable_and_can_be_retried_explicitly(environment, raises):
    paths, host, release, config, _, starts = environment
    jobs = JobStore(paths, host)
    job = jobs.submit(config, release, release / '.venv/bin/python', initialize_displays=True)

    class Engine:
        def __init__(self, *args, **kwargs):
            pass

        def apply(self, on_stage):
            if raises:
                raise RuntimeError('synthetic setup failure')
            on_stage('chrome', 'blocked')
            return Report([CheckResult('chrome', 'blocked')])

    assert run_worker(job['run_id'], paths, host, Engine) == 1
    assert jobs.load(job['run_id'])['state'] == 'failed'
    assert onboarding.status(paths=paths)['phase'] == 'install_failed'
    assert onboarding.pending_for_user(config, paths) and not starts
    assert onboarding.request(config, release, paths=paths, host=host)['state'] == 'completed'
    assert len(starts) == 2 and not onboarding.pending_for_user(Config.load(paths.config), paths)


def test_unfinished_pair_journal_fences_setup_and_initialization(environment, monkeypatch):
    paths, host, release, config, _, starts = environment

    def guard(**kwargs):
        raise RuntimeError('synthetic unfinished pair')

    monkeypatch.setattr('ala.screen_swap.guard', guard)
    with pytest.raises(RuntimeError, match='unfinished pair'):
        JobStore(paths, host).submit(config, release, release / '.venv/bin/python')
    with pytest.raises(RuntimeError, match='unfinished pair'):
        onboarding.request(config, release, paths=paths, host=host)
    assert Config.load(paths.config) == config and not starts
    assert not paths.runs.exists()
