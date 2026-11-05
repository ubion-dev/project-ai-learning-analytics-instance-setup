"""The final installer lock must honor durable swaps, including old/dead leases."""
import ast
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from ala import update
from test_install import REPO, installation


OPERATION = 'a' * 32


def swap_record(root, state='recovery_required', **changes):
    selection = {'release': str(root / 'opt/ala/releases/0.1.0rc1'), 'version': '0.1.0rc1',
                 'revision': '1' * 64, 'config_path': str(root / 'var/lib/ala-runtime/configs' / ('1' * 64 + '.json'))}
    value = {'schema_version': 1, 'operation_id': OPERATION, 'targets': ['default', 'other'],
             'unit': 'ala-runtime-swap-' + OPERATION + '.service', 'boot_id': 'old-boot',
             'expires_monotonic': 0, 'state': state,
             'decision': {'completed': 'commit', 'rolled_back': 'rollback'}.get(state),
             'phase': state if state in {'completed', 'rolled_back'} else 'rollback',
             'participants': {target: {key: deepcopy(selection) for key in ('before', 'after', 'desired_before', 'desired_after')}
                              for target in ('default', 'other')}}
    value.update(changes)
    return value


def write_swap(installation, value=None):
    path = installation.root / 'var/lib/ala-runtime/swaps' / (OPERATION + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value or swap_record(installation.root)))
    return path


def existing_installation(installation):
    release = installation.root / 'opt/ala/releases/0.1.0rc1'
    release.mkdir(parents=True)
    (release / 'VERSION').write_text('0.1.0rc1\n')
    (release / 'entry.py').write_text('# existing trusted program\n')
    installation.current.symlink_to(release)
    installation.launcher.parent.mkdir(parents=True)
    installation.launcher.write_text('#!/bin/sh\n# ala managed launcher v1\n')
    config = installation.root / 'etc/ala/config.json'
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({'user': 'user', 'url': None, 'autostart': True}))


def installed_state(installation):
    result = {}
    for prefix in ('opt', 'etc', 'usr/local/bin'):
        directory = installation.root / prefix
        if not directory.exists():
            continue
        for path in [directory, *directory.rglob('*')]:
            if path.is_symlink():
                result[str(path)] = ('link', os.readlink(path), path.lstat().st_mtime_ns)
            elif path.is_file():
                result[str(path)] = ('file', path.read_bytes(), path.stat().st_mtime_ns)
            else:
                result[str(path)] = ('directory',)
    return result


@pytest.mark.parametrize('state,boot,expires', [
    ('queued', 'test-boot', 0), ('running', 'previous-boot', 10 ** 20),
    ('unknown', 'test-boot', 0), ('recovery_required', 'previous-boot', 0),
])
@pytest.mark.parametrize('handoff', ['direct', 'updater'])
def test_unfinished_swap_blocks_installer_after_lease_expiry_and_reboot(installation, state, boot, expires, handoff):
    bundle, _ = installation.bundle('9.0.0')
    existing_installation(installation)
    journal = write_swap(installation, swap_record(installation.root, state, boot_id=boot, expires_monotonic=expires))
    # A partial reservation may have no targets directory at all. The journal
    # alone fences both participants; no live worker or lease is necessary.
    before = installed_state(installation)
    recorded = journal.read_bytes(), journal.stat().st_mtime_ns
    result = installation.install(bundle, **({'ALA_UPDATE_ONLY': '1'} if handoff == 'updater' else {}))
    assert result.returncode == 1 and '두 화면 교환 또는 복구' in result.stderr
    assert installed_state(installation) == before
    assert (journal.read_bytes(), journal.stat().st_mtime_ns) == recorded


@pytest.mark.parametrize('damage', ['json', 'symlink', 'directory-symlink', 'writable', 'unreadable',
                                  'writable-parent', 'wrong-id', 'bool-schema', 'partial-terminal'])
def test_untrusted_or_damaged_swap_fails_closed_without_changing_installation(installation, damage):
    bundle, _ = installation.bundle('9.0.0')
    existing_installation(installation)
    journal = write_swap(installation, swap_record(installation.root, 'completed'))
    if damage == 'json':
        journal.write_text('{')
    elif damage == 'symlink':
        outside = installation.tmp / 'external.json'
        journal.rename(outside)
        journal.symlink_to(outside)
    elif damage == 'directory-symlink':
        outside = installation.tmp / 'external-swaps'
        journal.parent.rename(outside)
        journal.parent.symlink_to(outside, target_is_directory=True)
    elif damage == 'writable':
        journal.chmod(0o666)
    elif damage == 'unreadable':
        journal.chmod(0o000)
    elif damage == 'writable-parent':
        journal.parent.chmod(0o777)
    else:
        value = json.loads(journal.read_text())
        if damage == 'wrong-id':
            value['operation_id'] = 'b' * 32
        elif damage == 'bool-schema':
            value['schema_version'] = True
        else:
            value['participants'].pop('other')
        journal.write_text(json.dumps(value))
    before = installed_state(installation)
    result = installation.install(bundle)
    assert result.returncode == 1 and ('화면 교환' in result.stderr or '설치 디렉터리' in result.stderr or '심볼릭 링크' in result.stderr)
    assert installed_state(installation) == before


@pytest.mark.parametrize('state', ['completed', 'rolled_back'])
def test_only_valid_terminal_pair_records_allow_installation(installation, state):
    bundle, _ = installation.bundle('9.0.0')
    existing_installation(installation)
    journal = write_swap(installation, swap_record(installation.root, state))
    before = journal.read_bytes(), journal.stat().st_mtime_ns
    result = installation.install(bundle)
    assert result.returncode == 0, result.stderr
    assert (installation.current / 'VERSION').read_text().strip() == '9.0.0'
    assert (journal.read_bytes(), journal.stat().st_mtime_ns) == before


def test_final_installer_catches_swap_accepted_after_updater_download(installation, monkeypatch):
    _, archive = installation.bundle('9.0.0')
    existing_installation(installation)
    before = installed_state(installation)
    data = archive.read_bytes()
    checksum = hashlib.sha256(data).hexdigest() + '  ala-9.0.0.tar.gz\n'
    monkeypatch.setattr(update, 'fetch', lambda url, *args, **kwargs: checksum.encode() if url.endswith('.sha256') else data)
    phases = []
    real_run = subprocess.run

    def launch(arguments, **options):
        assert arguments[0] == 'bash'
        assert options['env']['ALA_UPDATE_ONLY'] == '1'
        assert phases[-1] == 'installing'
        write_swap(installation)
        return real_run(arguments, check=True, env=dict(options['env'], **installation.env),
                        text=True, capture_output=True, timeout=60)

    monkeypatch.setattr(update.subprocess, 'run', launch)
    with pytest.raises(subprocess.CalledProcessError) as error:
        update.install_release('v9.0.0', on_phase=phases.append)
    assert phases == ['downloading', 'checksum', 'manifest', 'installing']
    assert '두 화면 교환 또는 복구' in error.value.stderr
    assert installed_state(installation) == before


def test_journal_owner_check_uses_kernel_metadata_and_fails_closed(installation, monkeypatch):
    # Execute the installer's actual read-only guard; only its filesystem inode
    # UID is synthetic. No root ownership changes or installer overrides exist.
    existing_installation(installation)
    journal = write_swap(installation, swap_record(installation.root, 'completed'))
    body = (REPO / 'install.sh').read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    parsed = ast.parse(body)
    guard = ast.Module(body=[node for node in parsed.body if isinstance(node, (ast.Import, ast.ImportFrom))
                            or isinstance(node, ast.FunctionDef) and node.name in {'fail', 'secure_directory', 'busy_screen_swaps'}],
                       type_ignores=[])
    namespace = {'ROOT': installation.root, 'OWNER': os.geteuid()}
    exec(compile(guard, 'installer-swap-guard', 'exec'), namespace)
    original = Path.lstat

    def different_owner(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == journal:
            fields = list(info)
            fields[4] = os.geteuid() + 1
            return os.stat_result(fields)
        return info

    before = installed_state(installation)
    monkeypatch.setattr(Path, 'lstat', different_owner)
    with pytest.raises(RuntimeError, match='소유자'):
        namespace['busy_screen_swaps']()
    assert installed_state(installation) == before
