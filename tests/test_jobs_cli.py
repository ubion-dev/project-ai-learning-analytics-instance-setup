from datetime import datetime, timedelta, timezone
from pathlib import Path
import asyncio
import json
import os
import sys
import time
import pytest

from ala.cli import main
from ala.config import Config
from ala.jobs import JobStore, change_lock, run_worker, trusted_release
from ala.model import CheckResult, Report
from ala.system import Paths, Command

class FakeHost:
    def __init__(self):
        self.calls=[]
    def run(self, argv, **kwargs):
        self.calls.append(argv)
        return Command(0, 'ActiveState=active\nLoadState=loaded\n')

@pytest.fixture
def store(tmp_path):
    paths=Paths(tmp_path)
    p=paths.at('/proc/sys/kernel/random/boot_id')
    p.parent.mkdir(parents=True);p.write_text('boot-one')
    return JobStore(paths, FakeHost())

def test_submission_pins_release_and_snapshot_and_prevents_duplicate(store):
    config=Config(user='user', url='https://example.com/?x=$literal')
    job=store.submit(config, Path('/opt/ala/releases/v0.1.0'), Path('/opt/ala/releases/v0.1.0/.venv/bin/python'))
    cmd=store.host.calls[-1]
    assert '--expand-environment=no' in cmd
    assert '/opt/ala/releases/v0.1.0/entry.py' in cmd
    assert '/opt/ala/current/entry.py' not in cmd
    assert Config.load(store.directory(job['run_id'])/'config.json') == config
    with pytest.raises(RuntimeError):
        store.submit(config, Path('/opt/ala/releases/new'), Path('/bin/python'))

def test_power_interruption_is_computed_without_mutating_records(store):
    job=store.submit(Config(user='user'), Path('/release'), Path('/python'))
    result_file=store.directory(job['run_id'])/'result.json'
    before=result_file.read_bytes()
    store.paths.at('/proc/sys/kernel/random/boot_id').write_text('boot-two')
    assert store.load(job['run_id'])['state']=='interrupted'
    assert result_file.read_bytes()==before

def test_reviewed_config_is_checked_inside_submission_lock(store):
    before=Config(user='user',url='https://before.example.com')
    after=Config(user='user',url='https://after.example.com')
    with pytest.raises(RuntimeError, match='구성이 변경'):
        store.submit(lambda:after,Path('/release'),Path('/python'),expected_config=before.to_dict())
    assert not store.paths.runs.exists()

def test_unit_query_failure_does_not_claim_interruption(store):
    job=store.submit(Config(user='user'),Path('/release'),Path('/python'))
    job['started_at']=(datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()
    store.save(job)
    store.host.run=lambda *a,**k:Command(124,'','timeout')
    queried=store.load(job['run_id'])
    assert queried['state']=='queued' and 'query_warning' in queried
    store.host.run=lambda *a,**k:Command(1,'LoadState=not-found\nActiveState=inactive\n','')
    assert store.load(job['run_id'])['state']=='interrupted'

def test_worker_preserves_pending_and_records_stage(store):
    class Engine:
        def __init__(self, config, **kwargs): pass
        def apply(self, on_stage):
            on_stage('gpu','pending')
            return Report([CheckResult('gpu','pending', next_action='재부팅')])
    job=store.submit(Config(user='user'),Path('/release'),Path('/python'))
    assert run_worker(job['run_id'],store.paths,store.host,Engine)==2
    result=store.load(job['run_id'])
    assert result['state']=='pending'
    assert result['stages'][0]['id']=='gpu'
    with pytest.raises(RuntimeError):
        run_worker(job['run_id'],store.paths,store.host,Engine)

def test_worker_failure_is_durable(store):
    class Engine:
        def __init__(self,*a,**k): pass
        def apply(self,**kwargs): raise RuntimeError('mock failure')
    job=store.submit(Config(user='user'),Path('/release'),Path('/python'))
    assert run_worker(job['run_id'],store.paths,store.host,Engine)==1
    assert store.load(job['run_id'])['reason']=='mock failure'

def test_lock_exclusion_and_path_validation(store):
    with change_lock(store.paths):
        with pytest.raises(RuntimeError):
            with change_lock(store.paths): pass
    for value in ('../config', '/', 'bad', 'x;id'):
        with pytest.raises(ValueError): store.directory(value)

def test_source_tree_cannot_become_sudo_helper(tmp_path):
    with pytest.raises((PermissionError,FileNotFoundError)):
        trusted_release(tmp_path)

def test_missing_saved_config_without_general_user_is_rejected(store, monkeypatch):
    from ala.service import Operations
    operations=Operations(paths=store.paths,host=store.host)
    monkeypatch.delenv('SUDO_USER', raising=False)
    with pytest.raises(ValueError,match='일반 사용자'):
        operations.dispatch('submit',{})
    assert store.host.calls==[]
    assert not store.paths.runs.exists()

class Service:
    def __init__(self, report=None):
        self.calls=[]
        self.report=report or Report().to_dict()
    def authenticate(self): self.calls.append(('authenticate',None))
    def request(self, action, payload=None):
        self.calls.append((action,payload))
        if action in ('plan','inspect','status'):return self.report
        if action=='submit':return {'run_id':'some-run','state':'queued'}
        return {'exit_code':0}

def test_non_tty_no_command_and_invalid_options_do_not_authorize(monkeypatch):
    monkeypatch.setattr('sys.stdin.isatty',lambda:False)
    service=Service()
    with pytest.raises(SystemExit) as exc:main([],service=service)
    assert exc.value.code==64
    with pytest.raises(SystemExit) as exc:main(['setup','--detach'],service=service)
    assert exc.value.code==64
    assert service.calls==[]

def test_help_and_version_have_no_privilege_side_effects():
    service=Service()
    for args in (['--help'],['--version'],['setup','--help']):
        with pytest.raises(SystemExit) as exc:main(args,service=service)
        assert exc.value.code==0
    assert service.calls==[]

def test_plan_is_default_and_has_json_only(capsys):
    service=Service(Report([CheckResult('package','change_required')]).to_dict())
    assert main(['setup','--json'],service=service)==0
    assert json.loads(capsys.readouterr().out)['results'][0]['state']=='change_required'
    assert service.calls[-1][0]=='plan'
    assert all(action!='submit' for action,_ in service.calls)

def test_check_failure_has_priority_and_status_is_query_success(capsys):
    service=Service(Report([CheckResult('gpu','pending'),CheckResult('service','error')]).to_dict())
    assert main(['check','--json'],service=service)==1
    json.loads(capsys.readouterr().out)
    assert main(['status','--json'],service=service)==0

def test_detach_returns_run_id_without_waiting(capsys):
    service=Service()
    assert main(['setup','--apply','--detach','--json'],service=service)==0
    assert json.loads(capsys.readouterr().out)['run_id']=='some-run'

def test_async_rpc_is_json_and_cancellable_without_waiting_for_thread():
    from ala.service import Service as RealService
    service=RealService()
    service.command=lambda action:[sys.executable,'-c','import json; print(json.dumps({"ok":True}))']
    assert asyncio.run(service.call('status'))=={'ok':True}
    service.command=lambda action:[sys.executable,'-c','import time; time.sleep(30)']
    async def cancel():
        task=asyncio.create_task(service.call('status'))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    started=time.monotonic()
    asyncio.run(cancel())
    assert time.monotonic()-started<3

def test_private_user_rpc_connects_display_contract(monkeypatch,capsys):
    import ala.display as display
    monkeypatch.setattr(display,'user_action',lambda action:{'exit_code':2,'state':'pending','action':action})
    assert main(['_chrome-user','check'])==2
    assert json.loads(capsys.readouterr().out)['action']=='check'
    monkeypatch.setattr(display,'session_start',lambda:0)
    monkeypatch.setattr(display,'run_chrome',lambda:2)
    assert main(['_session'])==0
    assert main(['_chrome-run'])==2


def test_prepare_job_uses_same_lock_and_pins_operation(store):
    job = store.submit(Config(), Path('/release'), Path('/python'), operation='prepare')
    assert job['operation'] == 'prepare'
    assert job['unit'].startswith('ala-prepare-')
    with pytest.raises(RuntimeError):
        store.submit(Config(user='user'), Path('/release'), Path('/python'))
    with pytest.raises(ValueError):
        store.submit(Config(), Path('/release'), Path('/python'), operation='arbitrary')


def test_prepare_worker_routes_to_prepare_and_retains_pending(store, monkeypatch):
    import ala.prepare
    class Preparation:
        def __init__(self, config, **kwargs):
            assert config.user == ''
        def apply(self, on_stage):
            on_stage('prepare.reboot', 'pending')
            return Report([CheckResult('prepare.reboot', 'pending', next_action='재부팅 후 ala prepare --apply')])
    monkeypatch.setattr(ala.prepare, 'Prepare', Preparation)
    job = store.submit(Config(), Path('/release'), Path('/python'), operation='prepare')
    assert run_worker(job['run_id'], store.paths, store.host) == 2
    assert store.load(job['run_id'])['state'] == 'pending'


def test_prepare_and_network_cli_dispatch_json(capsys):
    class NewService(Service):
        def request(self, action, payload=None):
            self.calls.append((action, payload))
            if action == 'prepare-submit':
                return {'run_id': 'prepare-run', 'state': 'queued', 'operation': 'prepare'}
            return Report([CheckResult('test', 'pending')]).to_dict()
    service = NewService()
    assert main(['prepare', '--json'], service=service) == 0
    assert service.calls[-1][0] == 'prepare-plan'
    json.loads(capsys.readouterr().out)
    assert main(['prepare', '--apply', '--detach', '--json'], service=service) == 0
    assert json.loads(capsys.readouterr().out)['operation'] == 'prepare'
    assert main(['doctor', 'network', '--json'], service=service) == 2
    assert service.calls[-1] == ('doctor-network', {'probe_url': None})
    json.loads(capsys.readouterr().out)
    assert main(['doctor', 'network', '--probe-url', 'https://user:secret@example.com', '--json'], service=service) == 1
    assert 'secret' not in capsys.readouterr().out
    assert service.calls[-1][0] == 'authenticate'


def test_prepare_rpc_does_not_require_graphical_account(store, monkeypatch):
    from ala.service import Operations
    monkeypatch.delenv('SUDO_USER', raising=False)
    result = Operations(paths=store.paths, host=store.host, release=Path('/release')).dispatch('prepare-submit', {})
    assert result['operation'] == 'prepare'
    assert Config.load(store.directory(result['run_id']) / 'config.json').user == ''


def test_first_setup_persists_defaults_once_under_job_lock(store, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from ala.service import Operations
    home = tmp_path / 'user-home'
    home.mkdir()
    monkeypatch.setenv('SUDO_USER', 'user')
    monkeypatch.setattr('ala.service.pwd.getpwnam', lambda name: SimpleNamespace(pw_uid=1000, pw_dir=str(home), pw_shell='/bin/bash'))
    operations = Operations(paths=store.paths, host=store.host, release=Path('/release'))
    expected = operations.config().to_dict()
    assert not store.paths.config.exists()
    job = operations.dispatch('submit', {'expected_config': expected})
    assert Config.load(store.paths.config).user == 'user'
    assert Config.load(store.paths.config).autostart is False
    before = store.paths.config.stat().st_mtime_ns
    job.update(state='completed', exit_code=0)
    store.save(job)
    operations.dispatch('submit', {'expected_config': expected})
    assert store.paths.config.stat().st_mtime_ns == before


def test_unreviewed_or_busy_first_setup_does_not_write_config(store, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from ala.service import Operations
    monkeypatch.setenv('SUDO_USER', 'user')
    monkeypatch.setattr('ala.service.pwd.getpwnam', lambda name: SimpleNamespace(pw_uid=1000, pw_dir=str(tmp_path), pw_shell='/bin/bash'))
    operations = Operations(paths=store.paths, host=store.host, release=Path('/release'))
    mismatch = dict(operations.config().to_dict(), dev_tools=True)
    with pytest.raises(RuntimeError, match='구성이 변경'):
        operations.dispatch('submit', {'expected_config': mismatch})
    assert not store.paths.config.exists() and not store.paths.runs.exists()
    store.submit(Config(user='user'), Path('/release'), Path('/python'))
    with pytest.raises(RuntimeError, match='이미 진행'):
        operations.dispatch('submit', {})
    assert not store.paths.config.exists()
