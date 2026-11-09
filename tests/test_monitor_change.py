"""Synthetic GNOME and clock; detached command is inspected, never executed."""
from copy import deepcopy
import json

import pytest

from ala import monitor_change as change
from ala.config import Config
from ala.jobs import change_lock
from ala.monitor_layout import read_settings
from ala.system import Command, Paths, atomic_json
from test_monitor_layout import current_state


class Host:
    def __init__(self): self.calls = []; self.delivery = 0

    def run(self, argv, **kwargs):
        self.calls.append(argv)
        assert argv[0] == 'systemd-run'
        return Command(self.delivery)


class Desktop:
    def __init__(self):
        self.raw = current_state(2)
        self.session = '00000000-0000-0000-0000-000000000001'
        self.calls = []
        self.fail = None
        self.crash = False
        self.current = 100.0

    def observe(self, store, config):
        if self.crash and self.calls and self.calls[-1][0] == 'temporary':
            raise KeyboardInterrupt('simulated worker termination')
        return read_settings(self.raw), self.session

    def backend(self, store, config, settings, session, plan, method):
        self.calls.append((method, deepcopy(plan)))
        assert settings['serial'] == self.raw[0] and session == self.session
        if self.fail == 'verify' and method == 'verify':
            raise RuntimeError('synthetic verify denial')
        if method == 'temporary':
            self.raw[0] += 1
            by_id = {p['id']: p for p in plan['outputs']}
            for monitor in self.raw[1]:
                for mode in monitor[1]:
                    mode[6]['is-current'] = mode[0] == by_id[monitor[0][0]]['mode_id']
            for logical in self.raw[2]:
                p = by_id[logical[5][0][0]]
                logical[:5] = [p['x'], p['y'], p['scale'], p['transform'], p['primary']]
            if self.fail == 'after-apply':
                self.fail = None
                raise RuntimeError('synthetic lost reply')

    def sleep(self, seconds): self.current += seconds


@pytest.fixture
def trial(tmp_path, monkeypatch):
    paths, host, desktop = Paths(tmp_path), Host(), Desktop()
    paths.config.parent.mkdir(parents=True)
    atomic_json(paths.config, Config(user='user', url='https://example.com').to_dict())
    boot = paths.at('/proc/sys/kernel/random/boot_id')
    boot.parent.mkdir(parents=True)
    boot.write_text('synthetic-boot')
    monkeypatch.setattr(change, 'trusted_release', lambda p: p)
    monkeypatch.setattr(change, '_observe', desktop.observe)
    monkeypatch.setattr(change, '_backend', desktop.backend)
    monkeypatch.setattr(change.time, 'monotonic', lambda: desktop.current)
    request = deepcopy(read_settings(desktop.raw)['edit_template'])
    request['outputs'][0]['x'], request['outputs'][1]['x'] = 1920, 0
    return paths, host, desktop, request


def submit(trial):
    paths, host, desktop, request = trial
    return change.submit(request, release='/opt/ala/releases/0.1.0rc30', paths=paths, host=host)


def test_read_only_status_creates_nothing(tmp_path):
    assert change.status(paths=Paths(tmp_path)) == {'state': 'none', 'persistent': False}
    assert list(tmp_path.iterdir()) == []


def test_submission_verifies_and_detaches_with_independent_stop_recovery(trial):
    paths, host, desktop, request = trial
    value = submit(trial)
    assert value['state'] == 'queued' and value['persistent'] is False
    command = host.calls[0]
    assert '--expand-environment=no' in command
    assert '--property=RuntimeMaxSec=120' in command and '--property=TimeoutStopSec=90' in command
    assert any(p.startswith('--property=ExecStopPost=') and '_layout-recover ' + value['operation_id'] in p for p in command)
    assert command[-2:] == ['_layout-worker', value['operation_id']]
    assert [method for method, _ in desktop.calls] == ['verify']
    assert desktop.raw[2][0][0] == 0
    assert change.Store(paths, host).current.stat().st_mode & 0o777 == 0o600
    with pytest.raises(RuntimeError, match='모니터 배치'):
        with change_lock(paths): pass


def test_worker_confirms_only_after_actual_readback_and_preserves_sites(trial):
    paths, host, desktop, _ = trial
    before_config = paths.config.read_bytes()
    value = submit(trial)

    def confirm(seconds):
        assert change.status(paths=paths)['state'] == 'awaiting_confirmation'
        with pytest.raises(RuntimeError):
            with change_lock(paths): pass
        result = change.decide(value['operation_id'], 'confirm', paths=paths, host=host)
        assert result['state'] == 'decision_recorded'
        desktop.sleep(seconds)

    result = change.run_worker(value['operation_id'], paths=paths, host=host, sleep=confirm)
    assert result['state'] == 'confirmed' and result['persistent'] is False
    assert desktop.raw[2][0][0] == 1920 and paths.config.read_bytes() == before_config
    with change_lock(paths): pass
    assert change.recover(value['operation_id'], paths=paths, host=host)['state'] == 'confirmed'
    assert [method for method, _ in desktop.calls] == ['verify', 'verify', 'temporary']


@pytest.mark.parametrize('decision', [None, 'revert'])
def test_disconnect_or_explicit_revert_restores_actual_previous_layout(trial, decision):
    paths, host, desktop, _ = trial
    before = change._signature(read_settings(desktop.raw))
    value = submit(trial)

    def wait(seconds):
        if decision:
            change.decide(value['operation_id'], decision, paths=paths)
        desktop.sleep(seconds)

    result = change.run_worker(value['operation_id'], paths=paths, host=host, sleep=wait)
    assert result['state'] == 'rolled_back'
    assert change._signature(read_settings(desktop.raw)) == before
    assert [method for method, _ in desktop.calls].count('temporary') == 2


def test_lost_apply_reply_is_reconciled_and_rolled_back(trial):
    paths, host, desktop, _ = trial
    value = submit(trial)
    desktop.fail = 'after-apply'
    assert change.run_worker(value['operation_id'], paths=paths, host=host, sleep=desktop.sleep)['state'] == 'rolled_back'
    assert desktop.raw[2][0][0] == 0


def test_killed_worker_leaves_durable_fence_and_stop_post_can_recover(trial):
    paths, host, desktop, _ = trial
    value = submit(trial)
    desktop.crash = True
    with pytest.raises(KeyboardInterrupt):
        change.run_worker(value['operation_id'], paths=paths, host=host, sleep=desktop.sleep)
    assert change.status(paths=paths)['state'] == 'applying'
    with pytest.raises(RuntimeError):
        with change_lock(paths): pass
    desktop.crash = False
    assert change.recover(value['operation_id'], paths=paths, host=host)['state'] == 'rolled_back'
    assert desktop.raw[2][0][0] == 0


@pytest.mark.parametrize('changed', ['session', 'hotplug', 'config', 'external-layout'])
def test_outside_changes_are_never_overwritten_on_recovery(trial, changed):
    paths, host, desktop, _ = trial
    value = submit(trial)

    def outside(seconds):
        if changed == 'session': desktop.session = '00000000-0000-0000-0000-000000000002'
        if changed == 'hotplug':
            desktop.raw[1].pop(); desktop.raw[2].pop()
        if changed == 'config': atomic_json(paths.config, Config(user='user', url='https://other.example.com').to_dict())
        if changed == 'external-layout':
            desktop.raw[0] += 1
            desktop.raw[2][0][1] = 1080
        desktop.sleep(seconds)

    result = change.run_worker(value['operation_id'], paths=paths, host=host, sleep=outside)
    assert result['state'] == 'recovery_required'
    assert [method for method, _ in desktop.calls].count('temporary') == 1
    with pytest.raises(RuntimeError):
        with change_lock(paths): pass


def test_stale_queued_state_never_applies(trial):
    paths, host, desktop, _ = trial
    value = submit(trial)
    desktop.raw[0] += 1
    assert change.run_worker(value['operation_id'], paths=paths, host=host)['state'] == 'not_applied'
    assert [method for method, _ in desktop.calls] == ['verify']


def test_verify_denial_makes_no_journal(trial):
    paths, _, desktop, _ = trial
    desktop.fail = 'verify'
    with pytest.raises(RuntimeError): submit(trial)
    assert not change.Store(paths).base.exists()


def test_delivery_uncertainty_does_not_clear_fence(trial):
    paths, host, desktop, _ = trial
    host.delivery = 124
    value = submit(trial)
    assert value['state'] == 'recovery_required'
    assert change.recover(value['operation_id'], paths=paths, host=host)['state'] == 'rolled_back'
    assert [method for method, _ in desktop.calls] == ['verify']


def test_timeout_cannot_be_confirmed_late(trial):
    paths, host, desktop, _ = trial
    value = submit(trial)

    def late(seconds):
        desktop.current += change.CONFIRM_SECONDS
        with pytest.raises(RuntimeError, match='시간'):
            change.decide(value['operation_id'], 'confirm', paths=paths)

    assert change.run_worker(value['operation_id'], paths=paths, host=host, sleep=late)['state'] == 'rolled_back'


def test_conflicting_or_old_decisions_are_rejected(trial):
    paths, host, desktop, _ = trial
    value = submit(trial)

    def decide(seconds):
        with pytest.raises(RuntimeError): change.decide('b' * 32, 'confirm', paths=paths)
        change.decide(value['operation_id'], 'revert', paths=paths)
        with pytest.raises(RuntimeError): change.decide(value['operation_id'], 'confirm', paths=paths)
        desktop.sleep(seconds)

    assert change.run_worker(value['operation_id'], paths=paths, host=host, sleep=decide)['state'] == 'rolled_back'


@pytest.mark.parametrize('damage', ['json', 'backup', 'plan', 'symlink', 'permissions'])
def test_damaged_journal_blocks_all_common_changes_without_repair(trial, tmp_path, damage):
    paths, _, _, _ = trial
    submit(trial)
    path = change.Store(paths).current
    if damage == 'json': path.write_text('{')
    if damage in {'backup', 'plan'}:
        value = json.loads(path.read_text())
        if damage == 'backup': value['backup']['settings']['serial'] += 1
        else: value['plan']['outputs'][0]['x'] += 1
        atomic_json(path, value)
    if damage == 'permissions': path.chmod(0o666)
    if damage == 'symlink':
        target = tmp_path / 'record.json'; path.rename(target); path.symlink_to(target)
    before = path.read_bytes()
    with pytest.raises((ValueError, PermissionError, RuntimeError)):
        with change_lock(paths): pass
    assert path.read_bytes() == before
