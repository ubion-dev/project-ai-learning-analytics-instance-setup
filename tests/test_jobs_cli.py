from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os
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
        return Command(0, 'active\n')

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
