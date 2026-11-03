"""Durable update outcomes against synthetic files and injected hosts only."""
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import json
import subprocess
import time

import pytest

from ala import update
from ala.jobs import change_lock
from ala.system import Command, Paths


class FakeHost:
    def __init__(self):
        self.calls = []
        self.launch = Command(0)
        self.unit = Command(0, 'ActiveState=active\nLoadState=loaded\n')
        self.version = Command(0, '2.0.0\n')

    def run(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if argv[0] == 'systemd-run':
            return self.launch
        if argv[0] == 'systemctl':
            return self.unit
        assert argv[-1] == '--version'
        assert '-I' in argv and '-B' in argv
        return self.version


@pytest.fixture
def context(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    host = FakeHost()
    boot = paths.at('/proc/sys/kernel/random/boot_id')
    boot.parent.mkdir(parents=True)
    boot.write_text('synthetic-boot')

    def switch(version):
        release = paths.at('/opt/ala/releases') / version
        release.mkdir(parents=True, exist_ok=True)
        (release / 'VERSION').write_text(version + '\n')
        current = paths.at('/opt/ala/current')
        if current.is_symlink():
            current.unlink()
        current.symlink_to(release)
        return release

    switch('1.0.0')
    launcher = paths.at('/usr/local/bin/ala')
    launcher.parent.mkdir(parents=True)
    current = paths.at('/opt/ala/current')
    launcher.write_text(f'#!/usr/bin/env bash\n# ala managed launcher v1\nset -e\nexec "{current}/.venv/bin/python" -I "{current}/entry.py" "$@"\n')
    launcher.chmod(0o755)
    selected = {'available': True, 'latest': '2.0.0', 'tag': 'v2.0.0', 'exit_code': 0,
                'current': update.__version__, 'runtime_version': update.__version__, 'installed_version': '1.0.0'}
    original_check = update.check
    monkeypatch.setattr(update, 'check', lambda *args, **kwargs: dict(selected))
    return SimpleNamespace(paths=paths, host=host, boot=boot, switch=switch, launcher=launcher,
                           selected=selected, check=original_check,
                           store=update.UpdateStore(paths, host, tmp_path / 'pinned-source'))


def enqueue(context):
    return update.submit(context.store.release, paths=context.paths, host=context.host)


def snapshot(paths):
    return {str(path.relative_to(paths.root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in paths.root.rglob('*') if path.is_file() and not path.is_symlink()}


def test_submit_persists_queued_attempt_and_pins_run_id(context):
    result = enqueue(context)
    assert result['state'] == 'queued' and result['exit_code'] == 2 and result['started']
    assert result['before_version'] == result['installed_version'] == '1.0.0'
    record = context.store.load(result['run_id'])
    path = context.store.root / result['run_id'] / 'state.json'
    assert path.stat().st_mode & 0o777 == 0o600
    assert record['release'] == str(Path(context.store.release).resolve())
    command = context.host.calls[0][0]
    assert command[:3] == ['systemd-run', '--unit=ala-update', '--collect']
    assert command[-2:] == ['_update-worker', result['run_id']]
    assert '/current/' not in ' '.join(command)


def test_worker_verifies_installed_pointer_and_actual_entry_before_success(context, monkeypatch):
    result = enqueue(context)
    phases = []
    original = update.UpdateStore.save
    def save(self, value):
        phases.append(value['phase'])
        original(self, value)
    monkeypatch.setattr(update.UpdateStore, 'save', save)

    def install(tag, *, on_phase):
        assert tag == 'v2.0.0'
        # The actual installer must be able to acquire the same change lock.
        with change_lock(context.paths):
            pass
        for name in ('downloading', 'checksum', 'manifest', 'installing'):
            on_phase(name)
        context.switch('2.0.0')

    monkeypatch.setattr(update, 'install_release', install)
    assert update.run_worker(result['run_id'], context.paths, context.host) == 0
    actual = update.status(context.paths, context.host, result['run_id'])
    assert actual['state'] == 'completed' and actual['exit_code'] == 0
    assert actual['installed_version'] == '2.0.0' and actual['before_version'] == '1.0.0'
    assert actual['ended_at'] and actual['runtime_version'] == update.__version__
    assert phases == ['checking', 'checking', 'downloading', 'checksum', 'manifest', 'installing', 'rechecking', 'completed']
    assert context.host.calls[-1][0][1:] == ['-I', '-B', str(context.paths.at('/opt/ala/current/entry.py')), '--version']
    with pytest.raises(RuntimeError, match='재실행'):
        update.run_worker(result['run_id'], context.paths, context.host)


@pytest.mark.parametrize('phase,error', [
    ('downloading', OSError('synthetic GitHub unavailable')),
    ('checksum', ValueError('synthetic SHA-256 mismatch')),
    ('manifest', ValueError('synthetic manifest invalid')),
    ('installing', subprocess.CalledProcessError(9, ['synthetic-installer'])),
])
def test_worker_failure_preserves_existing_version_and_durable_reason(context, monkeypatch, phase, error):
    result = enqueue(context)
    def fail(tag, *, on_phase):
        on_phase(phase)
        raise error
    monkeypatch.setattr(update, 'install_release', fail)
    assert update.run_worker(result['run_id'], context.paths, context.host) == 1
    actual = update.status(context.paths, context.host)
    assert actual['state'] == 'failed' and actual['phase'] == phase and actual['exit_code'] == 1
    assert actual['installed_version'] == actual['before_version'] == '1.0.0'
    assert '유지했습니다' in actual['reason'] and actual['ended_at']
    assert not any(command[-1] == '--version' for command, _ in context.host.calls)


def test_successful_installer_return_without_target_pointer_is_not_success(context, monkeypatch):
    result = enqueue(context)
    monkeypatch.setattr(update, 'install_release', lambda tag, **kwargs: None)
    assert update.run_worker(result['run_id'], context.paths, context.host) == 1
    actual = update.status(context.paths, context.host)
    assert actual['state'] == 'failed' and '일치하지' in actual['reason']
    assert actual['installed_version'] == '1.0.0'


def test_entry_failure_after_pointer_switch_never_claims_old_version_preserved(context, monkeypatch):
    result = enqueue(context)
    monkeypatch.setattr(update, 'install_release', lambda tag, **kwargs: context.switch('2.0.0'))
    context.host.version = Command(0, '1.0.0\n')
    assert update.run_worker(result['run_id'], context.paths, context.host) == 1
    actual = update.status(context.paths, context.host)
    assert actual['state'] == 'failed' and actual['installed_version'] == '2.0.0'
    assert '유지했습니다' not in actual['reason'] and '기존 버전 유지로 판단하지' in actual['reason']


def test_installer_error_after_publication_reports_the_actual_new_version(context, monkeypatch):
    result = enqueue(context)
    def fail_after_switch(tag, **kwargs):
        context.switch('2.0.0')
        raise OSError('synthetic publication sync failed')
    monkeypatch.setattr(update, 'install_release', fail_after_switch)
    assert update.run_worker(result['run_id'], context.paths, context.host) == 1
    actual = update.status(context.paths, context.host)
    assert actual['state'] == 'failed' and actual['installed_version'] == '2.0.0'
    assert '유지했습니다' not in actual['reason']


def test_worker_waits_for_submit_lock_then_releases_it_before_installer(context, monkeypatch):
    result = enqueue(context)
    monkeypatch.setattr(update, 'install_release', lambda tag, **kwargs: context.switch('2.0.0'))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with change_lock(context.paths):
            future = pool.submit(update.run_worker, result['run_id'], context.paths, context.host)
            time.sleep(0.01)
            assert not future.done()
            assert context.store.load(result['run_id'])['state'] == 'queued'
        assert future.result(timeout=2) == 0


def test_github_check_failure_is_failed_attempt_without_starting_service(context):
    context.selected.update(exit_code=1, available=False, error='GitHub 확인 실패', latest=None, tag=None)
    result = enqueue(context)
    assert result['state'] == 'failed' and result['phase'] == 'checking' and result['exit_code'] == 1
    assert not result['started'] and not context.host.calls and 'error' not in result
    actual = update.status(context.paths, context.host)
    assert actual['state'] == 'failed' and actual['installed_version'] == '1.0.0'
    assert 'GitHub' in actual['reason'] and '유지했습니다' in actual['reason']


def test_failed_service_launch_is_recorded_and_preserves_current(context):
    context.host.launch = Command(1, '', 'synthetic unit exists')
    result = enqueue(context)
    assert result['state'] == 'failed' and result['exit_code'] == 1 and not result['started']
    assert context.store.load(result['run_id'])['ended_at']
    assert update.installed_version(context.paths) == '1.0.0'


def test_timed_out_launch_is_unknown_without_falsely_finishing_the_detached_job(context):
    context.host.launch = Command(124, '', 'synthetic response timeout')
    result = enqueue(context)
    assert result['state'] == 'unknown' and result['exit_code'] == 1
    assert '계속될 수' in result['reason']
    record = context.store.load(result['run_id'])
    assert record['state'] == 'queued' and record['ended_at'] is None
    assert update.status(context.paths, context.host, result['run_id'])['state'] == 'queued'


@pytest.mark.parametrize('query_failure', [False, True])
def test_duplicate_update_does_not_hide_the_original_active_attempt(context, query_failure):
    first = enqueue(context)
    if query_failure:
        context.host.unit = Command(124, '', 'synthetic timeout')
    before = snapshot(context.paths)
    with pytest.raises(RuntimeError, match='이미 진행 중'):
        enqueue(context)
    assert context.store.latest()['run_id'] == first['run_id']
    assert before == snapshot(context.paths)
    assert sum(command[0] == 'systemd-run' for command, _ in context.host.calls) == 1


def test_no_available_update_creates_no_attempt_or_lock(context):
    context.selected.update(available=False)
    before = snapshot(context.paths)
    result = enqueue(context)
    assert result['state'] == 'idle' and result['exit_code'] == 0 and not result['started']
    assert not context.store.root.exists() and not context.host.calls
    assert before == snapshot(context.paths)


@pytest.mark.parametrize('condition,expected', [('new-boot', 'interrupted'), ('missing-unit', 'interrupted'),
                                               ('query-failure', 'unknown'), ('startup-grace', 'queued')])
def test_status_calculates_interruption_without_rewriting_record(context, condition, expected):
    result = enqueue(context)
    value = context.store.load(result['run_id'])
    value['started_at'] = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    if condition == 'new-boot':
        context.boot.write_text('next-synthetic-boot')
    elif condition in {'missing-unit', 'startup-grace'}:
        context.host.unit = Command(1, 'LoadState=not-found\n')
    else:
        context.host.unit = Command(124, '', 'synthetic timeout')
    if condition == 'startup-grace':
        value['started_at'] = datetime.now(timezone.utc).isoformat()
    context.store.save(value)
    before = snapshot(context.paths)
    actual = update.status(context.paths, context.host, result['run_id'])
    assert actual['state'] == expected
    assert actual['exit_code'] == (2 if expected == 'queued' else 1)
    assert before == snapshot(context.paths)
    assert context.store.load(result['run_id'])['state'] == 'queued'


def test_idle_status_does_not_create_records_or_run_commands(context):
    before = snapshot(context.paths)
    actual = update.status(context.paths, context.host)
    assert actual['state'] == 'idle' and actual['exit_code'] == 0
    assert actual['installed_version'] == '1.0.0'
    assert before == snapshot(context.paths) and not context.store.root.exists()
    assert not context.host.calls


def test_status_reads_latest_timestamp_not_random_run_id_order(context):
    first = enqueue(context)
    old = context.store.load(first['run_id'])
    old.update(state='failed', phase='failed', ended_at=update.now(),
               started_at='2026-01-01T00:00:00+00:00')
    context.store.save(old)
    second = enqueue(context)
    assert update.status(context.paths, context.host)['run_id'] == second['run_id']


def test_status_keeps_terminal_evidence_written_during_unit_query(context):
    result = enqueue(context)
    value = context.store.load(result['run_id'])
    value['started_at'] = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    context.store.save(value)
    original = context.host.run
    def query(argv, **kwargs):
        if argv[0] == 'systemctl':
            context.switch('2.0.0')
            value.update(state='completed', phase='completed', ended_at=update.now())
            context.store.save(value)
            return Command(1, 'LoadState=not-found\n')
        return original(argv, **kwargs)
    context.host.run = query
    actual = update.status(context.paths, context.host)
    assert actual['state'] == 'completed' and actual['exit_code'] == 0


def test_later_active_unit_cannot_resurrect_an_old_interrupted_attempt(context):
    first = enqueue(context)
    value = context.store.load(first['run_id'])
    value['started_at'] = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    context.store.save(value)
    context.host.unit = Command(1, 'LoadState=not-found\n')
    assert update.status(context.paths, context.host, first['run_id'])['state'] == 'interrupted'
    second = enqueue(context)
    context.host.unit = Command(0, 'ActiveState=active\nLoadState=loaded\n')
    before = snapshot(context.paths)
    old = update.status(context.paths, context.host, first['run_id'])
    current = update.status(context.paths, context.host, second['run_id'])
    assert old['state'] == 'interrupted' and old['exit_code'] == 1
    assert current['state'] == 'queued' and current['exit_code'] == 2
    assert context.store.load(first['run_id'])['state'] == 'queued'
    assert before == snapshot(context.paths)


def test_completed_record_with_changed_current_is_not_verified_success(context):
    result = enqueue(context)
    value = context.store.load(result['run_id'])
    value.update(state='completed', phase='completed', ended_at=update.now())
    context.store.save(value)
    before = snapshot(context.paths)
    actual = update.status(context.paths, context.host)
    assert actual['state'] == 'unknown' and actual['exit_code'] == 1
    assert actual['installed_version'] == '1.0.0' and '다릅니다' in actual['reason']
    assert before == snapshot(context.paths)


def test_wrong_installed_file_or_launcher_never_executes_untrusted_entry(context, monkeypatch):
    result = enqueue(context)
    monkeypatch.setattr(update, 'install_release', lambda tag, **kwargs: context.switch('2.0.0'))
    context.launcher.write_text('unmanaged launcher')
    assert update.run_worker(result['run_id'], context.paths, context.host) == 1
    actual = update.status(context.paths, context.host)
    assert actual['state'] == 'failed' and '진입점' in actual['reason']
    assert not any(command[-1] == '--version' for command, _ in context.host.calls)


@pytest.mark.parametrize('run_id', ['../outside', 'bad', [], 1])
def test_status_rejects_unsafe_record_id_without_reading_or_writing(context, run_id):
    before = snapshot(context.paths)
    actual = update.status(context.paths, context.host, run_id)
    assert actual['state'] == 'unknown' and actual['exit_code'] == 1
    assert before == snapshot(context.paths) and not context.host.calls


def test_symlink_or_writable_record_is_unknown_and_never_repaired(context, tmp_path):
    result = enqueue(context)
    path = context.store.root / result['run_id'] / 'state.json'
    path.chmod(0o666)
    assert update.status(context.paths, context.host)['state'] == 'unknown'
    path.chmod(0o600)
    copy = tmp_path / 'external-record.json'
    copy.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(copy)
    before = copy.read_bytes()
    assert update.status(context.paths, context.host)['state'] == 'unknown'
    assert path.is_symlink() and copy.read_bytes() == before


def test_current_pointer_escape_is_unknown_and_not_executed(context, tmp_path):
    current = context.paths.at('/opt/ala/current')
    current.unlink()
    current.symlink_to(tmp_path)
    assert update.status(context.paths, context.host)['state'] == 'unknown'
    assert not context.host.calls


def test_check_distinguishes_process_and_installed_versions(context, monkeypatch):
    version = '1.0.0'
    data = [{'tag_name': 'v1.0.0', 'assets': [
        {'name': 'ala-' + version + '.tar.gz' + suffix, 'state': 'uploaded'} for suffix in ('', '.sha256')]}]
    monkeypatch.setattr(update, 'fetch', lambda *args, **kwargs: json.dumps(data).encode())
    actual = context.check(paths=context.paths)
    assert actual['current'] == actual['runtime_version'] == update.__version__
    assert actual['installed_version'] == version and not actual['available']


def test_network_check_failure_is_not_up_to_date(context, monkeypatch):
    monkeypatch.setattr(update, 'fetch', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('synthetic offline')))
    before = snapshot(context.paths)
    actual = context.check(paths=context.paths)
    assert actual['exit_code'] == 1 and actual['error'] and not actual['available']
    assert actual['installed_version'] == '1.0.0' and before == snapshot(context.paths)
