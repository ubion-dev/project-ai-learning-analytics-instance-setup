from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ala import frontend
from ala.display_health import state_path
from ala.system import Command, Paths, atomic_json
from test_display_health import fixture, healthy, document


def manifest(seq=1, build='build-a', ready=True):
    return {'schema_version': 1, 'deployment_seq': seq, 'build_id': build, 'ready': ready}


@pytest.mark.parametrize('value', (
    {'enabled': True}, {'enabled': 1}, {'enabled': True, 'manifest_url': 'file:///tmp/manifest'},
    {'enabled': True, 'manifest_url': 'https://user:password@example.com/manifest'},
    {'enabled': True, 'manifest_url': 'https://example.com/manifest?token=synthetic'},
    {'enabled': True, 'manifest_url': 'https://example.com/manifest?token='},
    {'enabled': True, 'manifest_url': 'https://example.com/manifest#fragment'},
))
def test_frontend_is_explicit_optin_with_noncredential_manifest_url(value):
    with pytest.raises(ValueError):
        frontend.FrontendConfig.from_dict(value)


def test_frontend_default_is_disabled_and_manifest_is_data_only():
    assert frontend.FrontendConfig().to_dict() == {'enabled': False, 'manifest_url': None}
    assert frontend.FrontendConfig.from_dict({'enabled': True, 'manifest_url': 'https://example.com/deployment.json'}).enabled


@pytest.mark.parametrize('field,value', (('deployment_seq', True), ('deployment_seq', 0),
    ('deployment_seq', -1), ('deployment_seq', '1'), ('ready', 1), ('build_id', 'bad build'),
    ('build_id', 'x' * 129), ('schema_version', True)))
def test_manifest_rejects_ambiguous_or_unbounded_fields(field, value):
    data = manifest()
    data[field] = value
    with pytest.raises(ValueError):
        frontend.Deployment.from_dict(data)


class Response:
    status = 200
    headers = {'Cache-Control': 'no-store'}
    def __init__(self, raw): self.raw = raw
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def read(self, limit): return self.raw[:limit]


def test_manifest_fetch_is_bounded_uncached_and_not_an_html_or_etag_inference():
    calls = []
    def open_url(request, *, timeout):
        calls.append((request, timeout))
        return Response(json.dumps(manifest()).encode())
    value = frontend.fetch_deployment('https://example.com/deployment.json', opener=open_url)
    assert value == frontend.Deployment(1, 'build-a', True)
    assert calls[0][1] == 5 and 'no-cache' in calls[0][0].get_header('Cache-control')
    for raw in (b'<html>new build</html>', b'x' * (frontend.MAX_BYTES + 1),
                b'{"schema_version":1,"deployment_seq":1,"deployment_seq":2,"build_id":"a","ready":true}'):
        with pytest.raises(ValueError):
            frontend.fetch_deployment('https://example.com/deployment.json', opener=lambda *_, **__: Response(raw))
    response = Response(json.dumps(manifest()).encode())
    response.headers = {}
    with pytest.raises(ValueError):
        frontend.fetch_deployment('https://example.com/deployment.json', opener=lambda *_, **__: response)
    response.headers = {'Cache-Control': 'x-no-store, no-store=false'}
    with pytest.raises(ValueError):
        frontend.fetch_deployment('https://example.com/deployment.json', opener=lambda *_, **__: response)


class Fleet:
    def __init__(self):
        self.data = frontend.Deployment(1, 'build-a', True)
        self.saved = None
        self.calls = []
        self.failed = None
        self.stopped = set()
        self.busy = set()
        self.valid = True
        self.already = set()
        self.coordinator = frontend.Coordinator({}, save=self.save, fetch=self.fetch)
    def save(self, value): self.saved = deepcopy(value)
    def fetch(self, _):
        if isinstance(self.data, Exception): raise self.data
        return self.data
    def poll(self, retry=False):
        def reserve(target, actual):
            self.calls.append(('reserve', target))
            return None if target in self.busy else {'target_id': target, 'revision': 'revision-a'}
        def reload(target, deployment, token):
            self.calls.append(('reload', target, deployment.deployment_seq))
            return {'exit_code': 1, 'state': 'failed'} if target == self.failed else {'exit_code': 0, 'state': 'completed'}
        return self.coordinator.poll('https://example.com/deployment.json', ['first', 'second', 'third'],
            observe=lambda target: {'running': target not in self.stopped,
                                    'build_id': self.data.build_id if target in self.already else None,
                                    'app_ready': target in self.already}, reserve=reserve,
            check=lambda token: self.valid, reload=reload,
            finish=lambda token, success: self.calls.append(('finish', token['target_id'], success)), retry=retry)
    def reloads(self): return [item for item in self.calls if item[0] == 'reload']


def test_two_ready_observations_then_sequential_targets_and_same_deployment_never_reload_again():
    fleet = Fleet()
    assert fleet.poll()['state'] == 'pending' and fleet.calls == []
    assert fleet.poll()['state'] == 'completed'
    assert fleet.calls == [('reserve', 'first'), ('reload', 'first', 1), ('finish', 'first', True),
                           ('reserve', 'second'), ('reload', 'second', 1), ('finish', 'second', True),
                           ('reserve', 'third'), ('reload', 'third', 1), ('finish', 'third', True)]
    assert fleet.poll()['state'] == 'completed' and len(fleet.reloads()) == 3
    fleet.coordinator = frontend.Coordinator(deepcopy(fleet.saved), save=fleet.save, fetch=fleet.fetch)
    assert fleet.poll()['state'] == 'completed' and len(fleet.reloads()) == 3


def test_failure_stops_remaining_deployment_and_manual_retry_only_retries_unconfirmed_targets():
    fleet = Fleet()
    fleet.failed = 'second'
    fleet.poll()
    assert fleet.poll()['state'] == 'failed'
    assert fleet.reloads() == [('reload', 'first', 1), ('reload', 'second', 1)]
    assert fleet.poll()['state'] == 'blocked' and len(fleet.reloads()) == 2
    fleet.failed = None
    assert fleet.poll(retry=True)['state'] == 'completed'
    assert fleet.reloads() == [('reload', 'first', 1), ('reload', 'second', 1), ('reload', 'second', 1), ('reload', 'third', 1)]


def test_interruption_after_attempt_record_is_not_automatically_replayed():
    fleet = Fleet()
    fleet.poll()
    group = next(iter(fleet.coordinator.state.values()))
    group['attempts']['first'] = {'state': 'started', 'revision': 'revision-a'}
    assert fleet.poll()['state'] == 'blocked' and fleet.reloads() == []


def test_failed_manifest_or_unready_breaks_consecutive_ready_but_never_changes_current_screen():
    fleet = Fleet()
    fleet.poll()
    fleet.data = ValueError('synthetic unavailable')
    assert fleet.poll()['state'] == 'unknown'
    fleet.data = frontend.Deployment(1, 'build-a', True)
    assert fleet.poll()['state'] == 'pending' and fleet.reloads() == []
    fleet.data = frontend.Deployment(1, 'build-a', False)
    assert fleet.poll()['state'] == 'pending'
    fleet.data = frontend.Deployment(1, 'build-a', True)
    assert fleet.poll()['state'] == 'pending' and fleet.reloads() == []


def test_lower_sequence_is_ignored_and_rollback_uses_a_new_sequence():
    fleet = Fleet()
    fleet.poll(); fleet.poll()
    fleet.data = frontend.Deployment(1, 'changed-same-sequence', True)
    assert fleet.poll()['state'] == 'blocked'
    fleet.data = frontend.Deployment(2, 'older-build-as-new-deployment', True)
    assert fleet.poll()['state'] == 'pending'
    assert fleet.poll()['state'] == 'completed' and len(fleet.reloads()) == 6
    fleet.data = frontend.Deployment(1, 'build-a', True)
    assert fleet.poll()['state'] == 'kept' and len(fleet.reloads()) == 6


def test_stopped_target_is_not_started_and_runtime_reservation_prevents_overlapping_code_switch():
    fleet = Fleet()
    fleet.stopped.add('first')
    fleet.busy.add('second')
    fleet.poll()
    assert fleet.poll()['state'] == 'pending' and fleet.reloads() == []
    fleet.busy.clear()
    assert fleet.poll()['state'] == 'completed'
    assert [entry[1] for entry in fleet.reloads()] == ['second', 'third']


def test_invalidated_reservation_never_navigates():
    fleet = Fleet()
    fleet.valid = False
    fleet.poll()
    assert fleet.poll()['state'] == 'pending' and fleet.reloads() == []
    assert fleet.calls[-1] == ('finish', 'first', False)


def test_cancel_during_finish_is_not_success_and_never_advances_next_target():
    fleet = Fleet()
    fleet.poll()
    reloaded = []
    def finish(*_): raise RuntimeError('synthetic generation changed')
    value = fleet.coordinator.poll('https://example.com/deployment.json', ['first', 'second'],
        observe=lambda _: {'running': True}, reserve=lambda *_: {'revision': 'revision-a'},
        check=lambda _: True, reload=lambda target, *_: reloaded.append(target) or {'exit_code': 0, 'state': 'completed'},
        finish=finish)
    assert value['state'] == 'failed' and reloaded == ['first']
    assert next(iter(fleet.coordinator.state.values()))['attempts']['first']['state'] == 'failed'


def test_verified_current_build_and_ready_page_does_not_need_even_an_initial_reload():
    fleet = Fleet()
    fleet.already.update({'first', 'second', 'third'})
    fleet.poll()
    assert fleet.poll()['state'] == 'completed' and fleet.calls == []


def test_unsupported_old_supervisor_is_deferred_without_consuming_a_deployment_attempt():
    fleet = Fleet()
    fleet.poll()
    result = fleet.coordinator.poll('https://example.com/deployment.json', ['first'],
        observe=lambda _: {'running': True, 'deferred': True},
        reserve=lambda *_: pytest.fail('must not reserve unsupported supervisor'),
        check=lambda _: True, reload=lambda *_: pytest.fail('must not reload'), finish=lambda *_: None)
    assert result['state'] == 'pending'
    assert next(iter(fleet.coordinator.state.values()))['attempts'] == {}


def seed_control(supervisor, clock, *, expected='build-a', source='frontend'):
    request = {'schema_version': 1, 'nonce': 'a' * 32, 'target_id': supervisor.target_id,
               'config_revision': supervisor.config_revision, 'run_nonce': supervisor.run_nonce,
               'created_at': clock.now, 'expires_at': clock.now + 60,
               'expected_build_id': expected, 'deployment_seq': 1 if expected else None, 'source': source,
               'operation_token': None}
    path = frontend.control_path(supervisor.home, supervisor.target_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, request, 0o600)
    return path, request


def test_owned_supervisor_reloads_once_and_confirms_document_build_and_app_readiness(fixture):
    supervisor, _, clock, _ = healthy(fixture)
    supervisor.frontend = frontend.FrontendConfig(True, 'https://example.com/deployment.json')
    path, request = seed_control(supervisor, clock)
    before = len(supervisor.browser.navigations())
    supervisor.control_tick()
    assert len(supervisor.browser.navigations()) == before + 1
    assert supervisor.value['control']['state'] == 'pending'
    supervisor.control_tick()
    assert len(supervisor.browser.navigations()) == before + 1
    document(supervisor)
    original = supervisor.browser.call
    current = {'build_id': 'old-build', 'ready': True}
    def call(method, params=None, **kwargs):
        return {'result': {'value': dict(current)}} if method == 'Runtime.evaluate' else original(method, params, **kwargs)
    supervisor.browser.call = call
    supervisor.control_tick()
    assert supervisor.value['control']['state'] == 'pending'
    current.update(build_id='build-a', ready=False)
    supervisor.control_tick()
    assert supervisor.value['control']['state'] == 'pending'
    current['ready'] = True
    supervisor.control_tick()
    assert supervisor.value['control']['state'] == 'completed'
    supervisor.control_tick()
    assert len(supervisor.browser.navigations()) == before + 1
    assert 'https://' not in state_path(supervisor.home).read_text()


@pytest.mark.parametrize('change', ('nonce', 'target', 'revision', 'expired', 'permissions', 'symlink'))
def test_control_request_cannot_cross_process_target_revision_expiry_or_file_boundary(fixture, change):
    supervisor, _, clock, _ = healthy(fixture)
    path, request = seed_control(supervisor, clock, expected=None, source='manual')
    if change == 'nonce': request['run_nonce'] = 'b' * 32
    if change == 'target': request['target_id'] = 'other'
    if change == 'revision': request['config_revision'] = 'other-revision'
    if change == 'expired': request['expires_at'] = clock.now - 1
    atomic_json(path, request, 0o600)
    if change == 'permissions': path.chmod(0o644)
    if change == 'symlink':
        saved = path.with_suffix('.saved')
        path.rename(saved)
        path.symlink_to(saved)
    before = list(supervisor.browser.navigations())
    supervisor.control_tick()
    assert supervisor.browser.navigations() == before and supervisor.value['control'] is None


def test_manual_reload_requires_real_new_document_but_not_an_external_page_build_contract(fixture):
    supervisor, _, clock, _ = healthy(fixture)
    seed_control(supervisor, clock, expected=None, source='manual')
    supervisor.control_tick()
    assert supervisor.value['control']['state'] == 'pending'
    document(supervisor)
    supervisor.control_tick()
    assert supervisor.value['control']['state'] == 'completed'
    assert not any(method == 'Runtime.evaluate' for method, _, _ in supervisor.browser.calls)


def test_explicit_control_same_document_fragment_move_forces_one_document_reload(fixture):
    supervisor, _, clock, _ = healthy(fixture)
    seed_control(supervisor, clock, expected=None, source='manual')
    original = supervisor.browser.call
    calls = []
    def call(method, params=None, **kwargs):
        calls.append(method)
        if method == 'Page.navigate': return {'frameId': 'main'}
        return original(method, params, **kwargs)
    supervisor.browser.call = call
    supervisor.control_tick()
    assert calls == ['Page.navigate', 'Page.reload']
    assert supervisor.value['control']['state'] == 'pending' and supervisor.loader is None
    document(supervisor, loader='fresh-document')
    supervisor.control_tick()
    assert supervisor.value['control']['state'] == 'completed'
    assert supervisor.value['navigation']['display_requests'] == 2
    assert supervisor.value['navigation']['browser_navigations'] == 0
    for _ in range(3): supervisor.probe(); supervisor.control_tick()
    assert calls == ['Page.navigate', 'Page.reload']


def test_generation_cancellation_between_same_document_move_and_reload_prevents_second_action(fixture, monkeypatch):
    supervisor, _, clock, _ = healthy(fixture)
    path, request = seed_control(supervisor, clock, expected=None, source='manual')
    request['operation_token'] = {'target_id': 'default', 'operation_id': 'c' * 32, 'revision': '', 'generation': 2}
    atomic_json(path, request, 0o600)
    valid = iter([True, False])
    monkeypatch.setattr(supervisor, '_valid_operation', lambda _: next(valid))
    calls = []
    def call(method, params=None, **kwargs):
        calls.append(method)
        return {'frameId': 'main'}
    supervisor.browser.call = call
    supervisor.control_tick()
    assert calls == ['Page.navigate'] and supervisor.value['control']['state'] == 'failed'


def test_frontend_control_disabled_or_document_error_is_never_a_success(fixture):
    supervisor, _, clock, _ = healthy(fixture)
    path, request = seed_control(supervisor, clock)
    before = list(supervisor.browser.navigations())
    supervisor.control_tick()
    assert supervisor.value['control']['state'] == 'rejected' and supervisor.browser.navigations() == before
    supervisor.frontend = frontend.FrontendConfig(True, 'https://example.com/deployment.json')
    request['nonce'] = 'b' * 32
    atomic_json(path, request, 0o600)
    supervisor.control_tick()
    document(supervisor, 503)
    assert supervisor.value['control']['state'] == 'failed' and supervisor.value['state'] == 'recovering'


def test_canceled_runtime_token_is_checked_by_private_owner_before_navigation(fixture, monkeypatch):
    supervisor, _, clock, _ = healthy(fixture)
    path, request = seed_control(supervisor, clock, expected=None, source='manual')
    request['operation_token'] = {'target_id': 'default', 'operation_id': 'c' * 32,
                                  'revision': '', 'generation': 2}
    atomic_json(path, request, 0o600)
    monkeypatch.setattr(supervisor, '_valid_operation', lambda _: False)
    before = list(supervisor.browser.navigations())
    supervisor.control_tick()
    assert supervisor.value['control']['state'] == 'rejected' and supervisor.browser.navigations() == before


def test_readonly_path_projection_and_placement_failure_do_not_control_other_chrome(fixture):
    supervisor, _, clock, _ = healthy(fixture)
    assert state_path(supervisor.home) == supervisor.home / '.local/state/ala/chrome-health.json'
    assert state_path(supervisor.home, 'second') == supervisor.home / '.local/state/ala/screens/second/chrome-health.json'
    def placement(_): raise RuntimeError('synthetic unavailable')
    supervisor.placement_callback = placement
    before = list(supervisor.browser.navigations())
    supervisor.placement_tick()
    assert supervisor.value['placement']['state'] == 'unknown'
    assert supervisor.value['state'] == 'healthy' and supervisor.browser.navigations() == before


def test_worker_uses_committed_snapshot_and_uid_context_instead_of_desired_site(tmp_path, monkeypatch):
    from ala import config, display, runtime
    paths = Paths(tmp_path)
    revision = 'a' * 64
    applied_path = tmp_path / 'committed.json'
    applied = config.Config(schema_version=2, user='user', screens=(config.ScreenConfig(
        id='default', url='https://example.com/current', frontend=frontend.FrontendConfig(True, 'https://example.com/current.json')),))
    applied_path.write_text(json.dumps(applied.to_dict()))
    snapshot = {'target_id': 'default', 'generation': 3, 'stopped': False,
                'committed': {'revision': revision, 'config_path': str(applied_path)},
                'desired': {'revision': 'b' * 64, 'config_path': 'unused-desired'}}
    monkeypatch.setattr(frontend.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(runtime, 'RuntimeStore', lambda *_: SimpleNamespace(selections=lambda: [deepcopy(snapshot)]))
    monkeypatch.setattr(runtime, 'snapshot', lambda *_, **__: deepcopy(snapshot))
    reservations, finished, observed, fetched, commands = [], [], [], [], []
    def reserve(target, **kwargs):
        reservations.append((target, kwargs))
        return {'target_id': target, 'operation_id': 'd' * 32, 'generation': 3, 'revision': revision}
    monkeypatch.setattr(runtime, 'reserve', reserve)
    monkeypatch.setattr(runtime, 'check', lambda *_, **__: True)
    monkeypatch.setattr(runtime, 'finish', lambda token, **kwargs: finished.append((token, kwargs)))
    def status(action, context, **kwargs):
        observed.append((action, context.url, context.config_revision))
        return {'service': {'ActiveState': 'active'}, 'health': {'run_nonce': 'c' * 32, 'config_revision': revision}}
    monkeypatch.setattr(display, 'chrome_action', status)
    monkeypatch.setattr(display, '_account', lambda *_: (12345, 12345, Path('/home/user')))
    class Host:
        def run(self, argv, **kwargs):
            commands.append((argv, kwargs))
            return Command(0, json.dumps({'exit_code': 0, 'state': 'completed'}))
    def fetch(url):
        fetched.append(url)
        return frontend.Deployment(1, 'build-a', True)
    desired = config.Config(user='user', url='https://example.com/unapplied')
    for _ in range(3):
        assert frontend.run_worker(config=desired, host=Host(), paths=paths, fetch=fetch) == 0
    assert fetched == ['https://example.com/current.json'] * 3
    assert observed == [('status', 'https://example.com/current', revision)]
    assert len(commands) == 1 and commands[0][0][:6] == ['runuser', '-u', 'user', '--', 'env', '-i']
    payload = json.loads(commands[0][0][-1])
    assert payload == {'target_id': 'default', 'config_revision': revision,
                       'expected_build_id': 'build-a', 'deployment_seq': 1, 'source': 'frontend',
                       'operation_token': {'target_id': 'default', 'operation_id': 'd' * 32,
                                           'generation': 3, 'revision': revision}}
    assert commands[0][1]['timeout'] == 65
    assert reservations[0][1]['expected_generation'] == 3 and reservations[0][1]['expected_revision'] == revision
    assert len(finished) == 1 and finished[0][1]['success'] is True


def test_worker_disallows_unscoped_retry_and_targets_only_selected_manifest_group(tmp_path, monkeypatch):
    from ala import config, runtime
    paths = Paths(tmp_path)
    monkeypatch.setattr(frontend.os, 'geteuid', lambda: 0)
    assert frontend.run_worker(paths=paths, retry=True) == 1
    applied_path = tmp_path / 'committed.json'
    applied = config.Config(schema_version=2, user='user', screens=tuple(config.ScreenConfig(
        id=target, url='https://example.com/' + target,
        frontend=frontend.FrontendConfig(True, 'https://example.com/' + target + '.json')) for target in ('first', 'second')))
    applied_path.write_text(json.dumps(applied.to_dict()))
    snapshots = [{'target_id': target, 'committed': {'config_path': str(applied_path), 'revision': 'a' * 64}}
                 for target in ('first', 'second')]
    monkeypatch.setattr(runtime, 'RuntimeStore', lambda *_: SimpleNamespace(selections=lambda: snapshots))
    polls = []
    def poll(self, url, targets, **kwargs):
        polls.append((url, targets, kwargs['retry']))
        return {'state': 'completed'}
    monkeypatch.setattr(frontend.Coordinator, 'poll', poll)
    assert frontend.run_worker(paths=paths, retry=True, target_id='second') == 0
    assert polls == [('https://example.com/second.json', ['second'], True)]
    polls.clear()
    assert frontend.run_worker(paths=paths, retry=True, manifest_url='https://example.com/first.json') == 0
    assert polls == [('https://example.com/first.json', ['first'], True)]


def test_read_status_is_readonly_and_reports_failed_remaining_targets_without_addresses(tmp_path):
    paths = Paths(tmp_path)
    path = paths.at('/var/lib/ala/frontend/state.json')
    assert frontend.read_status(paths)['targets'] == {} and not path.parent.exists()
    fleet = Fleet()
    fleet.failed = 'second'
    fleet.poll(); fleet.poll()
    atomic_json(path, fleet.saved, 0o600)
    before = path.stat().st_mtime_ns
    value = frontend.read_status(paths)
    assert value['state'] == 'ok'
    assert value['targets']['first']['state'] == 'completed'
    assert value['targets']['second']['state'] == value['targets']['third']['state'] == 'blocked'
    assert value['targets']['third']['failed_seq'] == 1
    assert '명시적으로 재시도' in value['targets']['third']['next_action']
    assert 'https://' not in json.dumps(value) and 'operation_id' not in json.dumps(value)
    assert path.stat().st_mtime_ns == before and list(path.parent.iterdir()) == [path]


def test_read_status_does_not_treat_bad_state_or_permissions_as_no_deployments(tmp_path):
    paths = Paths(tmp_path)
    path = paths.at('/var/lib/ala/frontend/state.json')
    atomic_json(path, {'invalid-key': {'targets': ['default']}}, 0o600)
    assert frontend.read_status(paths)['state'] == 'unknown'
    atomic_json(path, {}, 0o644)
    assert frontend.read_status(paths)['state'] == 'unknown'


def test_latest_manifest_query_failure_is_visible_even_after_a_previous_successful_deployment(tmp_path):
    fleet = Fleet()
    fleet.poll(); fleet.poll()
    fleet.data = ValueError('synthetic query unavailable')
    fleet.poll()
    paths = Paths(tmp_path)
    atomic_json(paths.at('/var/lib/ala/frontend/state.json'), fleet.saved, 0o600)
    value = frontend.read_status(paths)
    assert value['targets']['first']['state'] == 'unknown'
    assert '현재 화면을 유지' in value['targets']['first']['reason']
