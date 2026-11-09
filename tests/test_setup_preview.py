from copy import deepcopy
from dataclasses import replace
import json

import pytest

from ala import setup_preview as preview, display_onboarding as onboarding, display
from ala.config import Config, ScreenConfig
from ala.runtime import RuntimeStore
from ala.system import Command, atomic_json
from test_display_onboarding import environment


def enable(environment):
    paths, host, release, config, inventory, starts = environment
    metadata = release / 'COMPATIBILITY.json'
    metadata.write_text(json.dumps(json.loads(metadata.read_text()) | {'setup_preview_protocol': 1}))
    return environment


def test_unknown_topology_starts_local_preview_without_changing_did_config(environment, monkeypatch):
    paths, host, release, config, _, starts = enable(environment)
    calls = []
    monkeypatch.setattr('ala.screens._inventory', lambda *a: {'state': 'pending', 'logical_outputs': []})
    def user_call(model, host, paths, action):
        assert action == 'setup-preview-start'
        calls.append(action)
        assert onboarding.preview_for_user(model, paths)
        return {'state': 'starting', 'exit_code': 0}
    monkeypatch.setattr(display, '_user_call', user_call)
    before = paths.config.read_bytes()
    result = onboarding.request(config, release, paths=paths, host=host)
    assert result['state'] == 'pending' and result['preview']['state'] == 'starting'
    assert calls == ['setup-preview-start'] and not starts and paths.config.read_bytes() == before
    text = paths.at('/etc/systemd/user/' + preview.UNIT).read_text()
    assert '_setup-preview' in text and 'Restart=no' in text
    assert onboarding.pending_for_user(config, paths) and onboarding.preview_for_user(config, paths)


def test_preview_kept_until_owned_per_monitor_local_document_is_ready(environment, monkeypatch):
    paths, host, release, config, inventory, _ = enable(environment)
    original = deepcopy(inventory)
    inventory['logical_outputs'] = []
    ready = set()
    actions = []
    def user_call(model, host, paths, action):
        actions.append(action)
        if action == 'setup-preview-start': return {'state': 'starting', 'exit_code': 0}
        if action == 'startup-status':
            from test_display_onboarding import idle_startup
            return idle_startup()
        if action == 'ensure-start': return {'state': 'starting', 'exit_code': 0}
        if action == 'status':
            return {'state': 'running', 'process_verified': True,
                    'health': {'state': 'waiting_configuration', 'local_page_ready': model.target_id in ready}}
        raise AssertionError(action)
    monkeypatch.setattr(display, '_user_call', user_call)
    onboarding.request(config, release, paths=paths, host=host)
    inventory.update(original)
    result = onboarding.run_pending(paths=paths, host=host)
    saved = Config.load(paths.config)
    assert result['state'] == 'pending' and result['phase'] == 'starting'
    assert not onboarding.pending_for_user(saved, paths) and onboarding.preview_for_user(saved, paths)
    ready.add(saved.screen_targets()[0].id)
    assert len(saved.screen_targets()) > 1
    assert onboarding.run_pending(paths=paths, host=host)['state'] == 'pending'
    assert onboarding.preview_for_user(saved, paths)
    ready.update(screen.id for screen in saved.screen_targets())
    assert onboarding.run_pending(paths=paths, host=host)['state'] == 'completed'
    assert not onboarding.preview_for_user(saved, paths)


def test_preview_readiness_can_start_before_remaining_install_stages(environment, monkeypatch):
    paths, host, release, config, _, _ = enable(environment)
    onboarding.reserve(config, release, paths=paths, host=host, run_id='synthetic-run')
    monkeypatch.setattr(display, '_user_call', lambda *a: {'state': 'starting', 'exit_code': 0})
    assert onboarding.preview_ready('synthetic-run', paths=paths, host=host)['state'] == 'starting'
    assert onboarding.status(paths=paths)['phase'] == 'installing'
    assert onboarding.preview_for_user(config, paths)
    assert onboarding.preview_ready('other-run', paths=paths, host=host) is None


def test_preview_process_exits_when_configuration_is_replaced(environment, monkeypatch):
    paths, host, release, config, inventory, _ = enable(environment)
    inventory['logical_outputs'] = []
    monkeypatch.setattr(display, '_user_call', lambda *a: {'state': 'starting', 'exit_code': 0})
    onboarding.request(config, release, paths=paths, host=host)
    monkeypatch.setattr('ala.setup_preview.os.getuid', lambda: 1000)
    seen = []
    def runner(**kwargs):
        assert kwargs['config'].url is None and kwargs['config'].target_id == preview.TARGET
        assert kwargs['setup_preview'] is True and kwargs['stop_requested']() is False
        atomic_json(paths.config, replace(config, url='https://example.com/ready').to_dict(), 0o644)
        assert kwargs['stop_requested']() is True
        seen.append(kwargs['config'].target_id)
        return 0
    assert preview.run(config=config, home=paths.at('/home/user'), host=host, paths=paths, runner=runner) == 0
    assert seen == [preview.TARGET]


def test_preview_failure_does_not_abort_initialization_or_touch_site(environment, monkeypatch):
    paths, host, release, config, inventory, _ = enable(environment)
    inventory['logical_outputs'] = []
    monkeypatch.setattr(display, '_user_call', lambda *a: (_ for _ in ()).throw(RuntimeError('synthetic failure')))
    result = onboarding.request(config, release, paths=paths, host=host)
    assert result['state'] == 'pending' and result['preview']['state'] == 'pending'
    assert Config.load(paths.config) == config


def test_reserved_id_does_not_replace_existing_target_or_unmanaged_unit(environment, monkeypatch):
    paths, host, release, config, _, _ = enable(environment)
    path = paths.at('/etc/systemd/user/' + preview.UNIT)
    path.parent.mkdir(parents=True); path.write_text('unmanaged')
    with pytest.raises(ValueError): preview.prepare(config, release, paths, host)
    assert path.read_text() == 'unmanaged'
    configured = replace(config, schema_version=2, screens=(ScreenConfig(id=preview.TARGET),))
    with pytest.raises(ValueError): preview.prepare(configured, release, paths, host)


def test_preview_supervisor_uses_separate_profile_and_stops_after_handoff(environment, monkeypatch):
    paths, host, release, config, inventory, _ = enable(environment)
    inventory['logical_outputs'] = []
    monkeypatch.setattr(display, '_user_call', lambda *a: {'state': 'starting', 'exit_code': 0})
    onboarding.request(config, release, paths=paths, host=host)
    monkeypatch.setattr('ala.setup_preview.os.getuid', lambda: 1000)
    monkeypatch.setattr(display, '_wait_for_graphical_ready', lambda *a, **kw: {'WAYLAND_DISPLAY': 'wayland-0'})
    received = []
    class Supervisor:
        def __init__(self, config, home, argv, **options):
            assert config.url is None
            assert any(arg.endswith('/.config/ala/chrome-setup-preview') for arg in argv)
            assert 'placement_callback' not in options
            assert options['setup_context_callback']() == {'preparing': True}
            assert options['stop_requested']() is False
            received.append(options)
        def run(self): return 0
    def runner(**kwargs): return display.run_chrome(**kwargs, supervisor_factory=Supervisor)
    assert preview.run(config=config, home=paths.at('/home/user'), host=host, paths=paths, runner=runner) == 0
    record = onboarding._load(paths)
    record['state'] = 'completed'
    onboarding._fence(paths, record, pending=False)
    assert received[0]['stop_requested']() is True


@pytest.mark.parametrize('issue', [None, 'no-chrome', 'locked', 'running', 'failed-preview', 'shadow-unit'])
def test_user_preview_start_checks_binary_session_and_service_ownership(environment, monkeypatch, issue):
    paths, host, release, config, inventory, _ = enable(environment)
    inventory['logical_outputs'] = []
    monkeypatch.setattr(display, '_user_call', lambda *a: {'state': 'starting', 'exit_code': 0})
    onboarding.request(config, release, paths=paths, host=host)
    calls = []
    class UserHost:
        def run(self, argv, **kw):
            calls.append(argv)
            if argv[0] == 'test': return Command(1 if issue == 'no-chrome' else 0)
            if argv[:3] == ['systemctl', '--user', 'show-environment']: return Command(0, 'WAYLAND_DISPLAY=wayland-0\n')
            if argv[0] == 'loginctl': return Command(0)
            if argv[:3] == ['systemctl', '--user', 'show']:
                if '--property=FragmentPath' in argv:
                    fragment = '/home/user/unmanaged.service' if issue == 'shadow-unit' else '/etc/systemd/user/' + preview.UNIT
                    return Command(0, 'LoadState=loaded\nFragmentPath=' + fragment + '\n')
                active = 'failed' if issue == 'failed-preview' and argv[3] == preview.UNIT else 'active' if issue == 'running' and argv[3] == display.UNIT else 'inactive'
                return Command(0, 'LoadState=loaded\nActiveState=' + active + '\nResult=success\n')
            return Command(0)
    monkeypatch.setattr(display, '_graphical_sessions', lambda *a: ([('session', {'Active': 'yes', 'LockedHint': 'yes' if issue == 'locked' else 'no'})], ''))
    result = preview.ensure(config, paths.at('/home/user'), UserHost(), paths)
    starts = [a for a in calls if a[:3] == ['systemctl', '--user', 'start']]
    assert bool(starts) is (issue is None)
    if starts: assert starts[0][-1] == preview.UNIT
    assert not any(a[:3] in (['systemctl', '--user', 'stop'], ['systemctl', '--user', 'reset-failed']) for a in calls)


def test_blank_browser_starts_without_monitor_coordinates_but_keeps_placement_pending(environment, monkeypatch):
    paths, host, _, config, _, _ = environment
    model = replace(config, schema_version=2, screens=(ScreenConfig(id='default', output='DP-1'),)).for_screen('default')
    monkeypatch.setattr(display.os, 'getuid', lambda: 1000)
    monkeypatch.setattr(display, '_wait_for_graphical_ready', lambda *a, **kw: {'WAYLAND_DISPLAY': 'wayland-0'})
    monkeypatch.setattr('ala.monitors.topology', lambda **kw: {'state': 'pending', 'logical_outputs': []})
    received = []
    class Supervisor:
        def __init__(self, cfg, home, argv, **kw):
            received.append(kw)
        def run(self): return 0
    assert display.run_chrome(config=model, home=paths.at('/home/user'), host=host, paths=paths, supervisor_factory=Supervisor) == 0
    assert 'placement_callback' in received[0]
    assert received[0]['setup_context_callback']()['display_number'] is None
