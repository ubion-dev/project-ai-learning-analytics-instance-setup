from copy import deepcopy

import pytest

from ala import monitor_change as change, monitor_profiles as profiles
from ala.monitor_layout import build_plan, read_settings
from ala.system import Command, Paths
from test_monitor_change import trial
from test_monitor_layout import current_state


@pytest.fixture
def saved_trial(trial, monkeypatch):
    paths, host, desktop, request = trial
    original = host.run
    def run(argv, **kwargs):
        if argv[0] == 'systemctl':
            host.calls.append(argv)
            return Command(0)
        return original(argv, **kwargs)
    monkeypatch.setattr(host, 'run', run)
    return trial


def save(trial):
    paths, host, desktop, request = trial
    value = change.submit(request, release='/opt/ala/releases/0.1.0rc31', paths=paths, host=host, persistent=True)
    def confirm(seconds):
        change.decide(value['operation_id'], 'confirm', paths=paths)
        desktop.sleep(seconds)
    result = change.run_worker(value['operation_id'], paths=paths, host=host, sleep=confirm)
    assert result['state'] == 'confirmed' and result['persistent'] is True
    return result


def test_profile_status_is_read_only(tmp_path):
    assert profiles.status(paths=Paths(tmp_path)) == {'saved': False, 'enabled': False, 'restore': None}
    assert list(tmp_path.iterdir()) == []


def test_save_requires_confirmation_and_registers_fixed_login_dispatcher(saved_trial):
    paths, host, desktop, _ = saved_trial
    save(saved_trial)
    store = profiles.Profiles(paths, host)
    value = store.read()
    assert value['outputs'][0]['x'] == 1920 and value['user'] == 'user'
    assert store.path.stat().st_mode & 0o777 == 0o600
    unit = paths.at('/etc/systemd/system/ala-monitor-profile.service').read_text()
    assert '/opt/ala/current/entry.py _layout-restore' in unit
    assert ['systemctl', 'enable', '--now', 'ala-monitor-profile.timer'] in host.calls


def test_timeout_keeps_previous_profile_and_config(saved_trial):
    paths, host, desktop, request = saved_trial
    save(saved_trial)
    before = profiles.Profiles(paths).path.read_bytes()
    request = deepcopy(read_settings(desktop.raw)['edit_template'])
    request['outputs'][0]['x'], request['outputs'][1]['x'] = 0, 1920
    value = change.submit(request, release='/opt/ala/releases/0.1.0rc31', paths=paths, host=host, persistent=True)
    assert change.run_worker(value['operation_id'], paths=paths, host=host, sleep=desktop.sleep)['state'] == 'rolled_back'
    assert profiles.Profiles(paths).path.read_bytes() == before


def test_new_login_restores_once_and_manual_changes_are_not_fought(saved_trial):
    paths, host, desktop, _ = saved_trial
    save(saved_trial)
    desktop.raw = current_state(2)
    desktop.session = '00000000-0000-0000-0000-000000000002'
    paths.at('/proc/sys/kernel/random/boot_id').write_text('next-synthetic-boot')
    value = profiles.restore(release='/opt/ala/releases/0.1.0rc31', paths=paths, host=host)
    assert value['state'] == 'queued'
    result = change.run_worker(value['operation_id'], paths=paths, host=host, sleep=lambda _: pytest.fail('automatic restore must not wait for another human confirmation'))
    assert result['state'] == 'confirmed' and desktop.raw[2][0][0] == 1920
    assert profiles.status(paths=paths)['restore']['state'] == 'applied'
    count = len(desktop.calls)
    desktop.raw = current_state(2)
    assert profiles.restore(release='/opt/ala/releases/0.1.0rc31', paths=paths, host=host)['state'] == 'applied'
    assert len(desktop.calls) == count


@pytest.mark.parametrize('problem', ['hardware', 'inactive', 'scale', 'layout-mode', 'rotation', 'user'])
def test_changed_hardware_or_unsupported_profile_is_never_applied(saved_trial, problem):
    paths, host, desktop, _ = saved_trial
    save(saved_trial)
    settings = read_settings(current_state(2))
    profile = profiles.Profiles(paths).read()
    user = 'user'
    if problem == 'hardware': settings['outputs'][0]['hardware_key'] = 'b' * 64
    if problem == 'inactive': settings['logical_outputs'].pop()
    if problem == 'scale': profile['outputs'][0]['scale'] = 1.25
    if problem == 'layout-mode': settings['layout_mode'] = 2
    if problem == 'rotation': settings['logical_outputs'][0]['transform'] = 1
    if problem == 'user': user = 'other'
    with pytest.raises(ValueError): profiles.request_for(settings, profile, user)


def test_mode_id_can_change_only_with_unambiguous_matching_mode(saved_trial):
    paths, _, desktop, _ = saved_trial
    save(saved_trial)
    raw = current_state(2)
    for physical in raw[1]: physical[1][0][0] = 'renamed-mode'
    settings = read_settings(raw)
    request = profiles.request_for(settings, profiles.Profiles(paths).read(), 'user')
    assert all(o['mode_id'] == 'renamed-mode' for o in request['outputs'])
    duplicate = deepcopy(settings['outputs'][0]['modes'][0]); duplicate['id'] = 'ambiguous'
    settings['outputs'][0]['modes'].append(duplicate)
    with pytest.raises(ValueError): profiles.request_for(settings, profiles.Profiles(paths).read(), 'user')


def test_disabled_profile_does_not_launch_restore(saved_trial):
    paths, host, desktop, _ = saved_trial
    save(saved_trial)
    assert profiles.disable(paths=paths)['enabled'] is False
    desktop.raw = current_state(2)
    count = len(host.calls)
    assert profiles.restore(paths=paths, host=host)['state'] == 'disabled'
    assert len(host.calls) == count


def test_profile_save_failure_rolls_back_without_overwriting_unmanaged_unit(saved_trial):
    paths, host, desktop, request = saved_trial
    unit = paths.at('/etc/systemd/system/ala-monitor-profile.service')
    unit.parent.mkdir(parents=True); unit.write_text('unmanaged service')
    result = change.submit(request, release='/opt/ala/releases/0.1.0rc31', paths=paths, host=host, persistent=True)
    def confirm(seconds):
        change.decide(result['operation_id'], 'confirm', paths=paths); desktop.sleep(seconds)
    assert change.run_worker(result['operation_id'], paths=paths, host=host, sleep=confirm)['state'] == 'rolled_back'
    assert desktop.raw[2][0][0] == 0 and unit.read_text() == 'unmanaged service'
    assert profiles.Profiles(paths).read() is None


def test_killed_after_profile_write_restores_both_profile_and_actual_layout(saved_trial, monkeypatch):
    paths, host, desktop, request = saved_trial
    original_write = profiles.Profiles.write
    def killed(self, value):
        original_write(self, value)
        raise KeyboardInterrupt('synthetic termination after atomic profile write')
    monkeypatch.setattr(profiles.Profiles, 'write', killed)
    value = change.submit(request, release='/opt/ala/releases/0.1.0rc31', paths=paths, host=host, persistent=True)
    def confirm(seconds):
        change.decide(value['operation_id'], 'confirm', paths=paths); desktop.sleep(seconds)
    with pytest.raises(KeyboardInterrupt): change.run_worker(value['operation_id'], paths=paths, host=host, sleep=confirm)
    assert profiles.Profiles(paths).read() is not None
    monkeypatch.setattr(profiles.Profiles, 'write', original_write)
    assert change.recover(value['operation_id'], paths=paths, host=host)['state'] == 'rolled_back'
    assert profiles.Profiles(paths).read() is None and desktop.raw[2][0][0] == 0


def test_invalid_profile_write_preserves_existing_saved_profile(saved_trial):
    paths, _, _, _ = saved_trial
    save(saved_trial)
    store = profiles.Profiles(paths)
    before = store.path.read_bytes()
    invalid = store.read(); invalid['outputs'][0]['scale'] = float('nan')
    with pytest.raises(ValueError): store.write(invalid)
    assert store.path.read_bytes() == before
