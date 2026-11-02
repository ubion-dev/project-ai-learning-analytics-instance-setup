import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/check-public.py'
spec = importlib.util.spec_from_file_location('public_policy', SCRIPT)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def test_addresses_and_email_are_not_confused_with_release_versions():
    assert policy.findings(b'127.0.0.1 ::1 localhost 0.1.0rc4') == []
    external = '.'.join(map(str, [10, 8, 1, 3]))
    assert 'non-loopback IP' in policy.findings(external.encode())
    ipv6 = ':'.join(['fe80', '', '1'])
    assert 'non-loopback IP' in policy.findings(ipv6.encode())
    personal = 'developer' + '@' + 'mail.invalid'
    assert 'personal email' in policy.findings(personal.encode())
    assert policy.identity(b'123+user@users.noreply.github.com')
    assert not policy.identity(personal.encode())


def test_host_allowlist_does_not_allow_suffix_spoofing():
    assert policy.findings(b'https://docs.docker.com/manual https://example.com') == []
    host = 'github.com' + '.invalid'
    assert 'unapproved URL host' in policy.findings(('https://' + host).encode())


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(['git', 'init', '-q'], check=True)
    subprocess.run(['git', 'config', 'user.name', 'Test'], check=True)
    subprocess.run(['git', 'config', 'user.email', '123+user@users.noreply.github.com'], check=True)
    return tmp_path


def stage(text):
    Path('sample.txt').write_text(text)
    subprocess.run(['git', 'add', 'sample.txt'], check=True)


def test_index_is_checked_even_if_worktree_is_cleaned(repository, monkeypatch):
    monkeypatch.setattr(policy, 'secret_scan', lambda blobs: None)
    stage('.'.join(map(str, [10, 9, 8, 7])))
    Path('sample.txt').write_text('localhost')
    with pytest.raises(RuntimeError, match='non-loopback'):
        policy.inspect(staged=True)


def test_historical_removed_data_is_still_blocked(repository, monkeypatch):
    monkeypatch.setattr(policy, 'secret_scan', lambda blobs: None)
    stage('.'.join(map(str, [10, 9, 8, 7])))
    subprocess.run(['git', 'commit', '-qm', 'old'], check=True)
    stage('localhost')
    subprocess.run(['git', 'commit', '-qm', 'clean'], check=True)
    with pytest.raises(RuntimeError, match='non-loopback'):
        policy.inspect(refs=['HEAD'])


def test_personal_commit_identity_is_blocked(repository, monkeypatch):
    monkeypatch.setattr(policy, 'secret_scan', lambda blobs: None)
    stage('localhost')
    subprocess.run(['git', 'config', 'user.email', 'person' + '@' + 'mail.invalid'], check=True)
    with pytest.raises(RuntimeError, match='identity'):
        policy.inspect(staged=True)


def test_missing_scanner_fails_closed(monkeypatch):
    monkeypatch.setattr(policy.shutil, 'which', lambda _: None)
    with pytest.raises(RuntimeError, match='required'):
        policy.secret_scan([b'localhost'])


def test_real_secret_scanner_and_output_redaction(repository):
    # Synthetic credential shape, constructed at runtime; never an active token.
    fake = 'ghp_' + 'A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8'
    with pytest.raises(RuntimeError, match='secret scan'):
        policy.secret_scan([('token=' + fake).encode()])
    stage('.'.join(map(str, [10, 9, 8, 7])))
    result = subprocess.run([sys.executable, str(SCRIPT), '--staged'], capture_output=True, text=True)
    assert result.returncode == 1
    assert Path('sample.txt').read_text() not in result.stdout + result.stderr


def test_commit_message_is_checked_without_printing_it(repository):
    message = Path('message')
    value = 'contact ' + 'developer' + '@' + 'mail.invalid'
    message.write_text(value)
    result = subprocess.run([sys.executable, str(SCRIPT), '--message-file', str(message)], capture_output=True, text=True)
    assert result.returncode == 1
    assert value not in result.stdout + result.stderr
