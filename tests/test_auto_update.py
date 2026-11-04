from datetime import datetime, timezone
import json

import pytest

from ala import auto_update, signature
from ala.config import Config
from ala.runtime import RuntimeStore
from ala.system import Command, Paths


@pytest.fixture
def fixture(tmp_path):
    paths = Paths(tmp_path)
    boot = paths.at('/proc/sys/kernel/random/boot_id')
    boot.parent.mkdir(parents=True)
    boot.write_text('boot-one')
    releases = []
    for version in ('1.0.0', '2.0.0'):
        path = paths.at('/opt/ala/releases') / version
        (path / '.venv/bin').mkdir(parents=True)
        (path / 'VERSION').write_text(version)
        (path / 'entry.py').write_text('# fixture')
        (path / '.venv/bin/python').write_text('# fixture')
        (path / 'COMPATIBILITY.json').write_text(json.dumps(
            {'schema_version': 1, 'runtime_protocol': 1, 'config_schemas': [1, 2]}))
        releases.append(path)
    paths.at('/opt/ala/current').symlink_to(releases[1])
    class Host:
        def __init__(self): self.calls = []
        def run(self, args, **kwargs):
            self.calls.append((args, kwargs))
            if args[0] == 'systemctl' and 'show' in args:
                return Command(0, 'ActiveState=active\nLoadState=loaded\n')
            return Command(0)
    host = Host()
    return paths, host, releases


def test_disabled_policy_never_requests_network_or_runs_updater(fixture, monkeypatch):
    paths, host, _ = fixture
    monkeypatch.setattr('ala.update.submit', lambda *a, **k: pytest.fail('disabled updater ran'))
    assert auto_update.run_scheduled('check', paths=paths, host=host) == 0
    assert host.calls == []
    assert not auto_update._state_path(paths).exists()


def test_schedule_registration_idempotent_and_no_enable_side_effect(fixture):
    paths, host, _ = fixture
    assert auto_update.register_units(paths=paths, host=host)['changed']
    files = list(paths.at('/etc/systemd/system').glob('*'))
    snapshot = {p: p.stat().st_mtime_ns for p in files}
    assert not auto_update.register_units(paths=paths, host=host)['changed']
    assert snapshot == {p: p.stat().st_mtime_ns for p in files}
    assert [c[0] for c in host.calls] == [['systemctl', 'daemon-reload']]
    assert 'RandomizedDelaySec=30min' in auto_update.unit_files()['ala-cli-update.timer']
    assert 'Asia/Seoul' in auto_update.unit_files()['ala-runtime-nightly.timer']
    assert 'TimeoutStartSec=infinity' in auto_update.unit_files()['ala-frontend.service']


def test_enable_needs_actual_signature_and_explicit_acceptance(fixture, monkeypatch):
    paths, host, releases = fixture
    monkeypatch.setattr(signature, 'ensure_verifier', lambda *_: '/verified/fixture')
    with pytest.raises(ValueError): auto_update.enable(paths=paths, host=host)
    with pytest.raises(ValueError): auto_update.enable(accepted=True, paths=paths, host=host)
    signature.record_verified({'state': 'verified', 'version': '2.0.0', 'verifier_version': signature.COSIGN_VERSION}, paths=paths)
    RuntimeStore(paths, host).bootstrap('default', releases[0], Config(user='user'))
    result = auto_update.enable(accepted=True, paths=paths, host=host)
    assert result['enabled'] is True
    assert ['systemctl', 'enable', '--now', *auto_update.TIMERS] in [c[0] for c in host.calls]
    auto_update.disable(paths=paths, host=host)
    assert auto_update._load(paths)['enabled'] is False
    disable = [c[0] for c in host.calls if 'disable' in c[0]][0]
    assert 'ala-frontend.timer' not in disable
    assert not any('restart' in c[0] or 'reboot' in c[0] for c in host.calls)


def test_bootstrap_detaches_with_pinned_run_id_and_readonly_interrupt_status(fixture, monkeypatch):
    paths, host, releases = fixture
    monkeypatch.setattr(signature, 'ensure_verifier', lambda *_: '/verified/fixture')
    response = auto_update.submit_bootstrap(releases[1], paths=paths, host=host)
    assert response['exit_code'] == 2
    command = host.calls[-1][0]
    assert command[-2:] == ['_auto-bootstrap', response['run_id']]
    assert str(releases[1] / 'entry.py') in command
    before = auto_update._state_path(paths).read_bytes()
    paths.at('/proc/sys/kernel/random/boot_id').write_text('boot-two')
    assert auto_update.status(paths=paths, host=host)['bootstrap']['state'] == 'interrupted'
    assert auto_update._state_path(paths).read_bytes() == before


def test_bootstrap_worker_records_failure_without_enabling_anything(fixture, monkeypatch):
    paths, host, releases = fixture
    response = auto_update.submit_bootstrap(releases[1], paths=paths, host=host)
    def fail(**kwargs): raise ValueError('not a recorded raw diagnostic')
    monkeypatch.setattr(auto_update, 'bootstrap', fail)
    assert auto_update.run_bootstrap(response['run_id'], paths=paths, host=host) == 1
    state = auto_update._load(paths)
    assert state['bootstrap']['state'] == 'failed'
    assert state['enabled'] is False
    assert 'not a recorded' not in json.dumps(state)


def test_current_cli_stages_only_verified_candidate_and_keeps_committed(fixture):
    paths, host, releases = fixture
    store = RuntimeStore(paths, host)
    store.bootstrap('default', releases[0], Config(user='user'))
    assert auto_update.stage_installed_candidate(paths=paths, host=host)['state'] == 'pending'
    signature.record_verified({'state': 'verified', 'version': '2.0.0', 'verifier_version': signature.COSIGN_VERSION}, paths=paths)
    assert auto_update.stage_installed_candidate(paths=paths, host=host)['state'] == 'staged'
    value = store.load('default')
    assert value['committed']['version'] == '1.0.0'
    assert value['desired']['version'] == '2.0.0'
    assert host.calls == []


def test_nightly_rechecks_actual_clock_before_each_target(fixture):
    paths, host, releases = fixture
    store = RuntimeStore(paths, host)
    for target in ('one', 'two'):
        store.bootstrap(target, releases[0], Config(user='user'))
        store.stage(target, releases[1], Config(user='user'))
    signature.record_verified({'state': 'verified', 'version': '2.0.0', 'verifier_version': signature.COSIGN_VERSION}, paths=paths)
    # 03:55 Seoul then 03:57: less than the full rollback allowance remains.
    now = [datetime(2026, 1, 1, 18, 55, tzinfo=timezone.utc)]
    calls = []
    def apply(target, **kwargs):
        calls.append(target)
        now[0] = datetime(2026, 1, 1, 18, 57, tzinfo=timezone.utc)
        return {'state': 'completed'}
    result = auto_update.nightly(paths=paths, host=host, now=lambda: now[0], apply=apply)
    assert calls == ['one'] and result['state'] == 'deferred'


def test_unsigned_or_failed_target_stops_later_targets(fixture):
    paths, host, releases = fixture
    store = RuntimeStore(paths, host)
    for target in ('one', 'two'):
        store.bootstrap(target, releases[0], Config(user='user'))
        store.stage(target, releases[1], Config(user='user'))
    calls = []
    def apply(target, **kwargs):
        calls.append(target)
        return {'state': 'failed'}
    now = datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc)
    assert auto_update.nightly(paths=paths, host=host, now=now, apply=apply)['state'] == 'deferred'
    assert calls == []
    signature.record_verified({'state': 'verified', 'version': '2.0.0', 'verifier_version': signature.COSIGN_VERSION}, paths=paths)
    assert auto_update.nightly(paths=paths, host=host, now=now, apply=apply)['state'] == 'failed'
    assert calls == ['one']


def test_check_finishing_after_disable_never_resurrects_enabled_flag(fixture, monkeypatch):
    paths, host, releases = fixture
    state = auto_update._load(paths)
    state.update(enabled=True, accepted=True)
    auto_update._save(paths, state)
    monkeypatch.setattr(signature, 'ensure_verifier', lambda *_: '/verified/fixture')
    def submit(*a, **kwargs):
        auto_update.disable(paths=paths, host=host)
        return {'state': 'idle', 'exit_code': 0}
    monkeypatch.setattr('ala.update.submit', submit)
    assert auto_update.run_scheduled('check', paths=paths, host=host) == 0
    assert auto_update._load(paths)['enabled'] is False
