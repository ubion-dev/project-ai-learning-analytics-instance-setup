from pathlib import Path

import pytest

from ala import cli
from ala import service as backend
from ala.system import Paths


def tty(monkeypatch, uid=1000):
    monkeypatch.setattr(backend.os, 'geteuid', lambda: uid)
    monkeypatch.setattr(backend.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(backend.sys.stdout, 'isatty', lambda: True)


def test_interactive_handoff_pins_trusted_release_and_preserves_arguments(monkeypatch):
    tty(monkeypatch)
    resolved = Path('/opt/ala/releases/test-version')
    monkeypatch.setattr(backend, 'trusted_release', lambda path: resolved)
    called = []
    def execute(program, argv):
        called.append((program, argv))
        raise SystemExit(91)
    monkeypatch.setattr(backend.os, 'execvp', execute)
    args = ['config', 'import', 'file with spaces;literal.json']
    with pytest.raises(SystemExit, match='91'):
        backend.Service().enter_admin(args)
    assert called == [('sudo', ['sudo', '--', str(resolved / '.venv/bin/python'), '-I', str(resolved / 'entry.py'), *args])]


def test_root_does_not_reauthenticate_and_non_tty_never_prompts(monkeypatch):
    tty(monkeypatch, uid=0)
    monkeypatch.setattr(backend.os, 'execvp', lambda *a: pytest.fail('unexpected sudo'))
    monkeypatch.setattr(backend.subprocess, 'run', lambda *a, **kw: pytest.fail('unexpected authentication'))
    backend.Service().enter_admin([])
    backend.Service().authenticate()
    monkeypatch.setattr(backend.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(backend.sys.stdin, 'isatty', lambda: False)
    backend.Service().enter_admin(['check', '--json'])


def test_source_checkout_cannot_elevate(monkeypatch, tmp_path):
    tty(monkeypatch)
    monkeypatch.setattr(backend.os, 'execvp', lambda *a: pytest.fail('untrusted source elevated'))
    with pytest.raises((PermissionError, FileNotFoundError)):
        backend.Service(release=tmp_path).enter_admin([])


def test_original_sudo_user_supplies_first_defaults_but_root_never_does(monkeypatch, tmp_path):
    monkeypatch.setenv('SUDO_USER', 'user')
    operations = backend.Operations(paths=Paths(tmp_path))
    config = operations.config()
    assert config.user == 'user' and config.display and config.autologin
    assert not config.autostart and not operations.paths.config.exists()
    monkeypatch.setenv('SUDO_USER', 'root')
    assert operations.config().user == ''


def test_help_and_update_check_never_elevate(monkeypatch, capsys):
    from ala import update
    tty(monkeypatch)
    monkeypatch.setattr(backend.Service, 'enter_admin', lambda *a: pytest.fail('read-only command elevated'))
    for args in (['--help'], ['--version'], ['setup', '--help']):
        with pytest.raises(SystemExit) as exc:
            cli.main(args)
        assert exc.value.code == 0
        capsys.readouterr()
    monkeypatch.setattr(update, 'check', lambda *a: {'available': False, 'exit_code': 0})
    assert cli.main(['update', '--check', '--json']) == 0
    capsys.readouterr()


def test_default_cli_opens_compact_console_after_admin_entry(monkeypatch):
    from ala import console
    tty(monkeypatch)
    calls = []
    monkeypatch.setattr(backend.Service, 'enter_admin', lambda self, argv: calls.append(('admin', argv)))
    monkeypatch.setattr(console, 'run_console', lambda service: calls.append(('console', None)) or 0)
    assert cli.main([]) == 0
    assert calls == [('admin', []), ('console', None)]
