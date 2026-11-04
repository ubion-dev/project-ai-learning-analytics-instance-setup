import hashlib
import io
import os
import subprocess
import sys
import tarfile

import pytest

from ala.engine import SCRIPTS


@pytest.fixture
def installation(tmp_path):
    home = tmp_path / 'home'
    config = home / '.config/server-bash'
    config.mkdir(parents=True)
    (config / 'managed').touch()
    bashrc = home / '.bashrc'
    bashrc.write_bytes(b"# retained exactly\nalias custom='true'\n")
    bashrc.chmod(0o640)
    (tmp_path / 'common.sh').write_text(
        (SCRIPTS / 'common.sh').read_text() + '\nrequire_rocky() { :; }\n')
    script = tmp_path / 'setup-bash-assist.sh'
    script.write_bytes((SCRIPTS / script.name).read_bytes())
    bin_path = tmp_path / 'bin'
    bin_path.mkdir()
    download_log = tmp_path / 'downloads'
    fixture = tmp_path / 'fixture.tar.xz'
    with tarfile.open(fixture, 'w:xz') as archive:
        content = b'echo "interactive ble.sh loaded"\n'
        member = tarfile.TarInfo('ble-0.4.0-devel3/ble.sh')
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    fixture_digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
    test_path = str(bin_path) + os.pathsep + os.environ['PATH']

    commands = {
        'rpm': 'raise SystemExit(0)\n',
        'getent': f'print(f"{{sys.argv[-1]}}:x:{{0 if sys.argv[-1] == \'root\' else 1000}}:1000::{home}:/bin/bash")\n',
        'runuser': (
            "assert sys.argv[1:4] == ['-u', 'user', '--']\n"
            f"argv = ['PATH={test_path}' if value == 'PATH=/usr/bin:/bin' else value for value in sys.argv[4:]]\n"
            "raise SystemExit(subprocess.call(argv))\n"
        ),
        'curl': (
            "assert any(url.endswith('/v0.4.0-devel3/ble-0.4.0-devel3.tar.xz') for url in sys.argv)\n"
            f"with Path({str(download_log)!r}).open('a') as log: log.write('download\\n')\n"
            f"Path(sys.argv[-1]).write_bytes(Path({str(fixture)!r}).read_bytes())\n"
        ),
        'sha256sum': (
            "assert sys.argv[1:] == ['--check', '--status']\n"
            "digest, path = sys.stdin.read().strip().split('  ', 1)\n"
            "assert digest == 'c8612ee612bc6b10dbfd6e85c6cbdfd7caf152a12d1f9de22ea0a9d735b3080c'\n"
            f"raise SystemExit(hashlib.sha256(Path(path).read_bytes()).hexdigest() != {fixture_digest!r})\n"
        ),
        # GNU mv has -T, while the macOS development runner uses BSD mv.
        'mv': 'os.rename(sys.argv[-2], sys.argv[-1])\n',
    }
    for name, body in commands.items():
        command = bin_path / name
        command.write_text(f'#!{sys.executable}\nfrom pathlib import Path\nimport hashlib, os, subprocess, sys\n' + body)
        command.chmod(0o755)

    def install(user='user'):
        return subprocess.run(['bash', str(script), '--user', user],
                              env=dict(os.environ, PATH=test_path), capture_output=True, text=True)

    return home, config, bashrc, fixture, download_log, install


def test_bash_assist_preserves_user_settings_and_is_quiet_without_tty(installation):
    home, config, bashrc, _, downloads, install = installation
    before = bashrc.read_bytes()
    result = install()
    assert result.returncode == 0, result.stderr
    assert (config / 'ble-0.4.0-devel3/ble.sh').is_file()
    assert before in bashrc.read_bytes()
    assert bashrc.stat().st_mode & 0o777 == 0o640
    assert [backup.read_bytes() for backup in home.glob('.bashrc.ala-backup.*')] == [before]
    assert bashrc.read_text().count('# ala bash-assist load') == 1
    assert bashrc.read_text().count('# ala bash-assist attach') == 1

    installed = bashrc.read_bytes(), bashrc.stat().st_mtime_ns
    result = install()
    assert result.returncode == 0, result.stderr
    assert (bashrc.read_bytes(), bashrc.stat().st_mtime_ns) == installed
    assert len(list(home.glob('.bashrc.ala-backup.*'))) == 1
    assert downloads.read_text().splitlines() == ['download']
    result = subprocess.run(['bash', '--noprofile', '--norc', '-c',
                             'source "$HOME/.bashrc"; printf "%s\\n" "0.1.0"'],
                            env=dict(os.environ, HOME=str(home)), capture_output=True, text=True)
    assert (result.returncode, result.stdout, result.stderr) == (0, '0.1.0\n', '')


def test_bash_assist_checksum_failure_is_retryable_without_editing_bashrc(installation):
    home, config, bashrc, fixture, downloads, install = installation
    before, valid = bashrc.read_bytes(), fixture.read_bytes()
    fixture.write_bytes(b'invalid archive')
    result = install()
    assert result.returncode != 0
    assert 'SHA-256' in result.stderr
    assert bashrc.read_bytes() == before
    assert not list(home.glob('.bashrc.ala*'))
    assert not list(config.glob('.ble.*'))
    assert not (config / 'ble-0.4.0-devel3').exists()

    fixture.write_bytes(valid)
    result = install()
    assert result.returncode == 0, result.stderr
    assert downloads.read_text().splitlines() == ['download', 'download']


@pytest.mark.parametrize('content', [
    '  source "$HOME/.local/share/blesh/ble.sh"\n',
    '# ala bash-assist attach\n',
])
def test_bash_assist_rejects_conflicts_before_download(installation, content):
    _, _, bashrc, _, downloads, install = installation
    bashrc.write_text(content)
    result = install()
    assert result.returncode != 0
    assert bashrc.read_text() == content
    assert not downloads.exists()


def test_bash_assist_refuses_root_and_symbolic_link(installation):
    home, _, bashrc, _, downloads, install = installation
    assert install('root').returncode != 0
    previous = home / 'user-rc'
    bashrc.rename(previous)
    bashrc.symlink_to(previous)
    before = previous.read_bytes()
    assert install().returncode != 0
    assert bashrc.is_symlink()
    assert previous.read_bytes() == before
    assert not downloads.exists()
