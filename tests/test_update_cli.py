import json

import pytest

from ala.cli import main


class Service:
    def __init__(self, statuses=()):
        self.calls = []
        self.statuses = iter(statuses)

    def authenticate(self):
        self.calls.append(('authenticate', None))

    def request(self, action, payload):
        self.calls.append((action, payload))
        if action == 'update-submit':
            return {'started':True, 'state':'queued', 'exit_code':2,
                    'run_id':'test-run', 'latest':'2.0.0', 'target_version':'2.0.0'}
        assert action == 'update-status'
        value = next(self.statuses)
        if isinstance(value, BaseException):
            raise value
        return value


def result(state='completed', **extra):
    return {'run_id':'test-run', 'state':state, 'phase':state,
            'installed_version':'2.0.0', 'target_version':'2.0.0',
            'runtime_version':'1.0.0', 'exit_code':0 if state == 'completed' else 2,
            **extra}


def test_default_update_tracks_background_worker_until_verified_completion(monkeypatch, capsys):
    service = Service([result('running', phase='downloading'), result('running', phase='rechecking'), result()])
    monkeypatch.setattr('ala.cli.time.sleep', lambda _:None)
    assert main(['update'], service=service) == 0
    out = capsys.readouterr().out
    assert '완료' in out and '설치된 버전: 2.0.0' in out
    assert [action for action, _ in service.calls].count('update-status') == 3
    assert all(payload == {'run_id':'test-run'} for action, payload in service.calls if action == 'update-status')


def test_json_wait_output_contains_only_final_structured_result(monkeypatch, capsys):
    service = Service([result('running'), result()])
    monkeypatch.setattr('ala.cli.time.sleep', lambda _:None)
    assert main(['update', '--json'], service=service) == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'completed'


def test_failed_update_is_not_reported_as_success(monkeypatch, capsys):
    service = Service([result('failed', exit_code=1, installed_version='1.0.0', reason='다운로드 확인 실패')])
    monkeypatch.setattr('ala.cli.time.sleep', lambda _:None)
    assert main(['update'], service=service) == 1
    out = capsys.readouterr().out
    assert '실패' in out and '설치된 버전: 1.0.0' in out
    assert '갱신: 완료' not in out


def test_detach_returns_pending_without_waiting(capsys):
    service = Service()
    assert main(['update', '--detach', '--json'], service=service) == 2
    assert json.loads(capsys.readouterr().out)['started']
    assert not any(action == 'update-status' for action, _ in service.calls)


def test_status_is_local_read_only_query(capsys):
    service = Service([result()])
    assert main(['update', '--status', '--run', 'test-run', '--json'], service=service) == 0
    assert json.loads(capsys.readouterr().out)['installed_version'] == '2.0.0'
    assert service.calls == [('authenticate',None), ('update-status',{'run_id':'test-run'})]


@pytest.mark.parametrize('args', [
    ['update', '--check', '--detach'], ['update', '--status', '--detach'],
    ['update', '--run', 'test-run'], ['update', '--status', '--prerelease'],
    ['update', '--status', '--check'],
])
def test_invalid_update_options_never_authorize(args):
    service = Service()
    with pytest.raises(SystemExit) as exc:
        main(args, service=service)
    assert exc.value.code == 64 and not service.calls


def test_interrupted_monitoring_does_not_request_worker_cancellation(capsys):
    service = Service([KeyboardInterrupt()])
    assert main(['update'], service=service) == 130
    assert all(action in {'authenticate','update-submit','update-status'} for action, _ in service.calls)
    assert '계속' in capsys.readouterr().err


def test_status_query_failure_is_one_json_error_not_success(capsys):
    service = Service([RuntimeError('상태 조회 실패')])
    assert main(['update', '--json'], service=service) == 1
    value = json.loads(capsys.readouterr().out)
    assert value['exit_code'] == 1 and '상태 조회 실패' in value['error']


def test_status_rpc_uses_paths_and_never_reads_configuration(tmp_path, monkeypatch):
    from ala.service import Operations
    from ala.system import Paths
    from ala import update
    paths = Paths(tmp_path)
    calls = []
    expected = result()
    monkeypatch.setattr(update, 'status', lambda **kw:calls.append(kw) or expected, raising=False)
    operations = Operations(paths=paths)
    operations.config = lambda:pytest.fail('status read configuration')
    assert operations.dispatch('update-status', {'run_id':'test-run'}) == expected
    assert calls[0]['paths'] == paths and calls[0]['run_id'] == 'test-run'
    assert not list(tmp_path.iterdir())


def test_status_rpc_rejects_non_data_run_id(tmp_path):
    from ala.service import Operations
    from ala.system import Paths
    with pytest.raises(ValueError):
        Operations(paths=Paths(tmp_path)).dispatch('update-status', {'run_id':['bad']})
    assert not list(tmp_path.iterdir())


def test_piped_update_authentication_never_waits_for_password(monkeypatch):
    from ala import service
    from ala.system import Command
    monkeypatch.setattr(service.os, 'geteuid', lambda:1000)
    monkeypatch.setattr(service.sys.stdin, 'isatty', lambda:True)
    monkeypatch.setattr(service.sys.stdout, 'isatty', lambda:False)
    calls = []
    monkeypatch.setattr(service.subprocess, 'run', lambda argv, **kw:calls.append(argv) or Command(0,''))
    service.Service().authenticate()
    assert calls == [['sudo','-n','-v']]
