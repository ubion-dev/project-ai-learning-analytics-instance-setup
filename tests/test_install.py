"""Real venv/offline-wheel installation inside a patched temporary filesystem.

Production installer has no test-root/environment override. Only the test copy is
rewritten; mocked dnf refuses every call. No /opt, /etc, /usr/local is modified.
"""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ala_build_release', REPO / 'scripts/build-release.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


def fake_wheel(directory):
    """A small Textual distribution stub; not a product dependency replacement."""
    directory.mkdir()
    path = directory / 'textual-8.2.8-py3-none-any.whl'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('textual/__init__.py', '__version__ = "8.2.8"\n')
        archive.writestr('textual-8.2.8.dist-info/METADATA', 'Metadata-Version: 2.1\nName: textual\nVersion: 8.2.8\nRequires-Python: >=3.9\n')
        archive.writestr('textual-8.2.8.dist-info/WHEEL', 'Wheel-Version: 1.0\nGenerator: ala-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
        archive.writestr('textual-8.2.8.dist-info/RECORD', '')
    return path


class Installation:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.root = tmp_path / 'host'
        self.root.mkdir()
        self.mock_bin = tmp_path / 'bin'
        self.mock_bin.mkdir()
        self.os_release = tmp_path / 'os-release'
        self.os_release.write_text('ID="rocky"\nVERSION_ID="10.2"\n')
        executable(self.mock_bin / 'python3', f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        executable(self.mock_bin / 'uname', '#!/bin/sh\nprintf "x86_64\\n"\n')
        executable(self.mock_bin / 'dnf', '#!/bin/sh\necho "Unexpected dnf call" >&2\nexit 99\n')
        executable(self.mock_bin / 'systemctl', '#!/bin/sh\nprintf "%s\\n" "${TEST_UNIT_STATE:-inactive}"\n')
        boot = self.root / 'proc/sys/kernel/random/boot_id'
        boot.parent.mkdir(parents=True)
        boot.write_text('test-boot\n')
        self.env = dict(os.environ, PATH=f'{self.mock_bin}:{os.environ["PATH"]}', PIP_DISABLE_PIP_VERSION_CHECK='1')

    @property
    def current(self):
        return self.root / 'opt/ala/current'

    @property
    def launcher(self):
        return self.root / 'usr/local/bin/ala'

    def bundle(self, version='0.1.0rc1', *, broken_entry=False):
        source = self.tmp / f'source-{version}'
        source.mkdir()
        (source / 'ala').mkdir()
        (source / 'ala/__init__.py').write_text(f'__version__ = "{version}"\n')
        (source / 'entry.py').write_text('raise SystemExit(7)\n' if broken_entry else f'print("{version}")\n')
        (source / 'pyproject.toml').write_text(f'[project]\nname = "ala-install-fixture"\nversion = "{version}"\n')
        legacy = source / 'scripts/rocky-linux'
        (legacy / 'lib').mkdir(parents=True)
        (legacy / 'bin').mkdir()
        (legacy / 'setup-base.sh').write_text('#!/bin/sh\nexit 99\n')
        (legacy / 'bin/chrome-did').write_text('#!/bin/sh\nexit 99\n')
        wheelhouse = source / 'wheelhouse'
        wheel = fake_wheel(wheelhouse)
        checksum = hashlib.sha256(wheel.read_bytes()).hexdigest()
        (source / 'requirements.lock').write_text(f'textual==8.2.8 --hash=sha256:{checksum}\n')
        installer = (REPO / 'install.sh').read_text()
        installer = installer.replace('if [[ $EUID != 0 ]]; then', 'if [[ false == true ]]; then')
        installer = installer.replace('OS_RELEASE=/etc/os-release', f'OS_RELEASE="{self.os_release}"')
        installer = installer.replace('ROOT = Path("/")', f'ROOT = Path({str(self.root)!r})')
        (source / 'install.sh').write_text(installer)
        (source / '.git').mkdir()
        (source / '.git/credentials').write_text('must not ship')
        (source / 'customer-secret.txt').write_text('must not ship')
        archive = builder.build(self.tmp / f'dist-{version}', wheelhouse, root=source)
        unpack = self.tmp / f'unpack-{version}'
        unpack.mkdir()
        with tarfile.open(archive) as stream:
            stream.extractall(unpack, filter='data')
        return unpack / f'ala-{version}', archive

    def install(self, bundle, **env):
        return subprocess.run(['bash', str(bundle / 'install.sh')], env=dict(self.env, **env),
                              text=True, capture_output=True, timeout=60)


@pytest.fixture
def installation(tmp_path):
    return Installation(tmp_path)


def test_builder_is_allowlisted_hashed_and_reproducible(installation):
    bundle, archive = installation.bundle()
    manifest = json.loads((bundle / 'MANIFEST.json').read_text())
    assert all('.git' not in key and 'secret' not in key and not key.startswith('tests/') for key in manifest['files'])
    assert all(hashlib.sha256((bundle / key).read_bytes()).hexdigest() == value for key, value in manifest['files'].items())
    again = builder.build(installation.tmp / 'again', installation.tmp / 'source-0.1.0rc1/wheelhouse',
                          root=installation.tmp / 'source-0.1.0rc1')
    assert archive.read_bytes() == again.read_bytes()
    assert archive.with_suffix('.gz.sha256').is_file()


def test_builder_rejects_wrong_wheel_hash(installation):
    installation.bundle()
    source = installation.tmp / 'source-0.1.0rc1'
    wheel = next((source / 'wheelhouse').glob('*.whl'))
    with wheel.open('ab') as stream:
        stream.write(b'changed')
    with pytest.raises(ValueError, match='lock과 다른 wheel'):
        builder.build(installation.tmp / 'bad', source / 'wheelhouse', root=source)


def test_corrupt_payload_fails_before_installation(installation):
    bundle, _ = installation.bundle()
    (bundle / 'entry.py').write_text('print("tampered")\n')
    result = installation.install(bundle)
    assert result.returncode == 1
    assert '검증 실패' in result.stderr
    assert not (installation.root / 'opt').exists()


def test_symlink_payload_and_extra_file_are_rejected(installation):
    bundle, _ = installation.bundle()
    (bundle / 'extra.txt').write_text('extra')
    result = installation.install(bundle)
    assert result.returncode == 1 and 'manifest에 없는' in result.stderr
    (bundle / 'extra.txt').unlink()
    (bundle / 'entry.py').unlink()
    (bundle / 'entry.py').symlink_to(installation.tmp / 'outside')
    result = installation.install(bundle)
    assert result.returncode == 1 and '심볼릭 링크' in result.stderr


def test_install_repeat_and_original_folder_removal(installation):
    bundle, _ = installation.bundle()
    result = installation.install(bundle)
    assert result.returncode == 0, result.stderr
    release = installation.current.resolve()
    tracked = [installation.launcher, release / 'entry.py', release / 'INSTALLATION.json', release / '.venv/pyvenv.cfg']
    before = [p.stat().st_mtime_ns for p in tracked]
    again = installation.install(bundle)
    assert again.returncode == 0, again.stderr
    assert [p.stat().st_mtime_ns for p in tracked] == before
    shutil.rmtree(bundle.parent)
    shutil.rmtree(installation.tmp / 'source-0.1.0rc1')
    command = subprocess.run([str(installation.launcher), '--version'], capture_output=True, text=True)
    assert command.returncode == 0 and command.stdout.strip() == '0.1.0rc1'
    assert installation.current.is_symlink()


def test_failed_upgrade_preserves_previous_current(installation):
    first, _ = installation.bundle()
    assert installation.install(first).returncode == 0
    previous = installation.current.resolve()
    broken, _ = installation.bundle('0.1.0rc2', broken_entry=True)
    result = installation.install(broken)
    assert result.returncode == 1
    assert installation.current.resolve() == previous
    assert not (previous.parent / '0.1.0rc2').exists()
    assert subprocess.run([str(installation.launcher), '--version'], capture_output=True, text=True).stdout.strip() == '0.1.0rc1'


def test_shared_change_lock_rejects_parallel_update(installation):
    bundle, _ = installation.bundle()
    lock = installation.root / 'run/lock/ala/operation.lock'
    lock.parent.mkdir(parents=True)
    with lock.open('w') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = installation.install(bundle)
    assert result.returncode == 1 and '다른 설치' in result.stderr
    assert not installation.current.exists()


def test_queued_or_running_unit_blocks_update(installation):
    bundle, _ = installation.bundle()
    run = installation.root / 'var/lib/ala/runs/20261029T010101Z-deadbeef'
    run.mkdir(parents=True)
    (run / 'result.json').write_text(json.dumps({'state': 'queued', 'boot_id': 'test-boot',
                                               'started_at': datetime.now(timezone.utc).isoformat()}))
    result = installation.install(bundle, TEST_UNIT_STATE='active')
    assert result.returncode == 1 and '설치 작업이 진행 중' in result.stderr
    assert not installation.current.exists()


def test_previous_boot_interrupted_job_does_not_block_update(installation):
    bundle, _ = installation.bundle()
    run = installation.root / 'var/lib/ala/runs/20261029T010101Z-deadbeef'
    run.mkdir(parents=True)
    (run / 'result.json').write_text(json.dumps({'state': 'running', 'boot_id': 'old-boot',
                                               'started_at': datetime.now(timezone.utc).isoformat()}))
    result = installation.install(bundle)
    assert result.returncode == 0, result.stderr


def test_installer_rejects_unrelated_command_and_external_current(installation):
    bundle, _ = installation.bundle()
    installation.launcher.parent.mkdir(parents=True)
    installation.launcher.write_text('#!/bin/sh\necho unrelated\n')
    result = installation.install(bundle)
    assert result.returncode == 1 and '다른 ala 명령' in result.stderr
    installation.launcher.unlink()
    (installation.root / 'opt/ala').mkdir(parents=True, exist_ok=True)
    installation.current.symlink_to(installation.tmp)
    result = installation.install(bundle)
    assert result.returncode == 1 and 'releases 밖' in result.stderr


def test_sticky_system_lock_parent_is_supported(installation):
    bundle, _ = installation.bundle()
    parent = installation.root / 'run/lock'
    parent.mkdir(parents=True)
    parent.chmod(0o1777)
    result = installation.install(bundle)
    assert result.returncode == 0, result.stderr


def test_root_payload_subdirectory_must_not_be_writable(installation):
    bundle, _ = installation.bundle()
    assert installation.install(bundle).returncode == 0
    package = installation.current.resolve() / 'ala'
    package.chmod(0o777)
    result = installation.install(bundle)
    assert result.returncode == 1 and '하위 디렉터리' in result.stderr


def test_same_version_changed_payload_is_not_overwritten(installation):
    bundle, _ = installation.bundle()
    assert installation.install(bundle).returncode == 0
    (bundle / 'entry.py').write_text('print("different")\n')
    manifest_file = bundle / 'MANIFEST.json'
    manifest = json.loads(manifest_file.read_text())
    manifest['files']['entry.py'] = hashlib.sha256((bundle / 'entry.py').read_bytes()).hexdigest()
    manifest_file.write_text(json.dumps(manifest))
    result = installation.install(bundle)
    assert result.returncode == 1 and '같은 버전에 다른 내용' in result.stderr
    assert (installation.current.resolve() / 'entry.py').read_text() == 'print("0.1.0rc1")\n'
