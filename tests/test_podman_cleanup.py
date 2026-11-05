import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from ala.engine import SCRIPTS
from ala.podman import inspect_cleanup
from ala.system import Command

spec = importlib.util.spec_from_file_location('podman_cleanup', SCRIPTS / 'lib/podman_cleanup.py')
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


class Package:
    def __init__(self, name, version='1'):
        self.name, self.version = name, version

    def __str__(self):
        return self.name + '-' + self.version


class Base:
    def __init__(self):
        self.packages = [Package('podman'), Package('podman-docker'), Package('base')]
        self.sack = SimpleNamespace(query=lambda: SimpleNamespace(installed=lambda: self.packages))
        self.transaction = SimpleNamespace(install_set=[], remove_set=self.packages[:2])
        self.removed = []
        self.applied = False
        self.conf = SimpleNamespace(read=lambda: None)
    def remove(self, name):
        self.removed.append(name)
    def resolve(self, *, allow_erasing):
        assert allow_erasing is True
    def do_transaction(self):
        self.applied = True
    def fill_sack(self, **kwargs):
        assert kwargs == {'load_available_repos': False}
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass


@pytest.fixture
def base(monkeypatch):
    base = Base()
    monkeypatch.setattr(cleanup, 'inspect_usage', lambda: [{'user': 'user', 'uid': 1000}])
    import sys
    monkeypatch.setitem(sys.modules, 'dnf', SimpleNamespace(Base=lambda: base))
    return base


def test_preview_does_not_remove_and_approval_rechecks_snapshot(base):
    plan = cleanup.run()
    assert plan['state'] == 'ready' and len(plan['packages']) == 2
    assert not base.applied
    assert cleanup.run(plan['token'])['state'] == 'completed'
    assert base.applied


def test_changed_removal_list_requires_new_approval(base):
    plan = cleanup.run()
    base.transaction.remove_set.append(Package('dependent'))
    with pytest.raises(cleanup.InspectionError, match='바뀌었습니다'):
        cleanup.run(plan['token'])
    assert not base.applied


def test_usage_appearing_after_approval_blocks_removal(base, monkeypatch):
    plan = cleanup.run()
    def busy():
        raise cleanup.InspectionError('사용 중')
    monkeypatch.setattr(cleanup, 'inspect_usage', busy)
    with pytest.raises(cleanup.InspectionError):
        cleanup.run(plan['token'])
    assert not base.applied


@pytest.mark.parametrize('resource', ['ps', 'volume', 'images', 'pod', 'network'])
def test_any_account_resources_block_cleanup(monkeypatch, resource):
    accounts = [SimpleNamespace(pw_name='root', pw_uid=0, pw_dir='/root'),
                SimpleNamespace(pw_name='user', pw_uid=1000, pw_dir='/home/user')]
    monkeypatch.setattr(cleanup, 'local_accounts', lambda: accounts)
    commands = []
    def query(command):
        commands.append(command)
        return [{'name': 'custom'}] if command[0].endswith('runuser') and resource in command else []
    monkeypatch.setattr(cleanup, 'query_json', query)
    with pytest.raises(cleanup.InspectionError):
        cleanup.inspect_usage()
    assert any(command[0].endswith('runuser') for command in commands)


@pytest.mark.parametrize('output,code', [('not-json', 0), ('{}', 0), ('{"state":"ready"}', 0),
                                        ('{"state":"not_required"}', 1)])
def test_query_failure_cannot_be_approved(output, code):
    host = SimpleNamespace(run=lambda *args, **kwargs: Command(code, output))
    assert inspect_cleanup(host, Path('/release'))['state'] == 'blocked'


def test_probe_errors_do_not_mean_empty(monkeypatch):
    monkeypatch.setattr(cleanup.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1))
    with pytest.raises(cleanup.InspectionError):
        cleanup.query_json(['podman', 'ps'])


def test_usage_accounts_include_high_uid_and_system_storage_owner(monkeypatch, tmp_path):
    home = tmp_path / 'system'
    (home / '.config/containers').mkdir(parents=True)
    accounts = [
        SimpleNamespace(pw_name='root', pw_uid=0, pw_dir='/root'),
        SimpleNamespace(pw_name='user', pw_uid=70000, pw_dir=str(tmp_path)),
        SimpleNamespace(pw_name='service', pw_uid=900, pw_dir=str(home)),
    ]
    monkeypatch.setattr(cleanup.pwd, 'getpwall', lambda: accounts)
    assert {account.pw_name for account in cleanup.local_accounts()} == {'root', 'user', 'service'}



def test_other_setup_packages_do_not_invalidate_approved_removal(base):
    plan = cleanup.run()
    base.packages.append(Package('setup-prerequisite'))
    assert cleanup.run(plan['token'])['state'] == 'completed'
    assert base.applied
