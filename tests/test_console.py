import io

from rich.console import Console

from ala.console import run_console, simple_setup, summarize
from ala.config import Config
from ala.model import Report, CheckResult


class Service:
    def __init__(self):
        self.calls = []
        self.report = Report([CheckResult('docker.conflict.podman', 'blocked', actual='podman', next_action='컨테이너·볼륨 확인')]).to_dict()
    def request(self, action, payload=None):
        self.calls.append((action, payload))
        if action == 'status':
            return self.report
        if action == 'config-show':
            return {'config': Config(user='user', autostart=False).to_dict()}
        if action == 'submit':
            return {'run_id': 'test-run', 'state': 'queued', 'stages': []}
        if action == 'runs-show':
            return {'run_id': 'test-run', 'state': 'failed', 'exit_code': 1,
                    'stages': [{'id': 'base', 'state': 'completed'}], 'report': self.report}
        raise AssertionError(action)


def output():
    stream = io.StringIO()
    return Console(file=stream, width=100, color_system=None), stream


def test_quit_and_status_never_apply_and_input_is_not_a_shell():
    service = Service()
    terminal, stream = output()
    entries = iter(['s', 'not-a-command; echo text', 'q'])
    assert run_console(service, console=terminal, read_line=lambda _: next(entries), update_check=None) == 0
    assert [action for action, _ in service.calls] == ['status']
    assert 'podman' in stream.getvalue() and '컨테이너·볼륨 확인' in stream.getvalue()


def test_one_setup_action_reuses_expected_config_and_tracks_detached_job():
    service = Service()
    terminal, stream = output()
    assert simple_setup(service, terminal, sleep=lambda _: None) == 1
    assert [action for action, _ in service.calls] == ['config-show', 'submit', 'runs-show']
    assert service.calls[1][1]['expected_config']['user'] == 'user'
    assert '바탕화면' in stream.getvalue() and 'podman' in stream.getvalue()


def test_summary_collapses_normal_inventory_and_preserves_issue_reason():
    terminal, stream = output()
    report = Report([CheckResult(f'base.package.item{i}', 'ok') for i in range(120)] + [
        CheckResult('gpu.driver', 'pending', actual='재부팅 대기', next_action='재부팅 후 검사')]).to_dict()
    summarize(report, terminal)
    text = stream.getvalue()
    assert '기본 패키지' in text and '정상' in text
    assert 'item119' not in text and '재부팅 후 검사' in text
    assert '기대 설정과 일치' not in text
    assert '간편 설정' not in text  # Pending reboot must not suggest another installation.
    assert len(text.splitlines()) < 10


def test_broken_query_does_not_become_success_and_eof_exits():
    terminal, stream = output()
    summarize({'results': []}, terminal)
    assert '검사 결과가 없습니다' in stream.getvalue()
    def eof(_):
        raise EOFError
    assert run_console(Service(), console=terminal, read_line=eof, update_check=None) == 0


def test_release_notice_is_async_and_does_not_apply_anything():
    import threading
    terminal, stream = output()
    ready = threading.Event()
    service = Service()
    def update():
        ready.set()
        return {'available': True, 'latest': '2.0.0'}
    def entry(_):
        assert ready.wait(1)
        return 'q'
    assert run_console(service, console=terminal, read_line=entry, update_check=update) == 0
    assert 'ala update' in stream.getvalue() and not service.calls
