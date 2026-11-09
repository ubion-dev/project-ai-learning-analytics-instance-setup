"""The independent installer honors pending session-layout recovery."""
import hashlib
import json

import pytest

from test_install import installation


def write_record(installation, state):
    backup = {'config': {'user': 'user'}, 'settings': {}}
    record = {'schema_version': 1, 'operation_id': 'a' * 32, 'state': state,
              'persistent': False, 'backup': backup,
              'backup_sha256': hashlib.sha256(json.dumps(backup, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
    path = installation.root / 'var/lib/ala/monitor-layout/current.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record))
    return path


@pytest.mark.parametrize('state', ['queued', 'applying', 'awaiting_confirmation', 'rolling_back', 'recovery_required'])
def test_unresolved_trial_blocks_installer_even_without_a_live_worker(installation, state):
    bundle, _ = installation.bundle()
    path = write_record(installation, state)
    before = path.read_bytes()
    result = installation.install(bundle)
    assert result.returncode != 0 and '모니터 배치' in result.stderr
    assert path.read_bytes() == before and not installation.current.exists()


@pytest.mark.parametrize('damage', ['backup', 'symlink', 'permissions', 'json'])
def test_damaged_terminal_record_blocks_installation(installation, damage):
    bundle, _ = installation.bundle()
    path = write_record(installation, 'confirmed')
    if damage == 'backup':
        value = json.loads(path.read_text()); value['backup']['config']['user'] = 'other'
        path.write_text(json.dumps(value))
    if damage == 'permissions': path.chmod(0o666)
    if damage == 'symlink':
        target = path.with_name('other.json'); path.rename(target); path.symlink_to(target)
    if damage == 'json': path.write_text('{')
    before = path.read_bytes()
    assert installation.install(bundle).returncode != 0
    assert path.read_bytes() == before and not installation.current.exists()


@pytest.mark.parametrize('state', ['confirmed', 'rolled_back', 'not_applied'])
def test_terminal_record_allows_installation_without_rewriting_record(installation, state):
    bundle, _ = installation.bundle()
    path = write_record(installation, state)
    before = path.read_bytes(), path.stat().st_mtime_ns
    result = installation.install(bundle)
    assert result.returncode == 0, result.stderr
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
