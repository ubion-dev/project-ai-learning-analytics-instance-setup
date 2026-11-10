"""Plain terminal acceptance with synthetic RPCs; no device changes."""
from copy import deepcopy
import io
import json
import subprocess
import sys
import time

import pytest

from ala.config import Config, ScreenConfig
from ala.terminal import Terminal, Cancelled, run_terminal
from ala.terminal_monitors import monitors, operation
from ala.terminal_screens import edit_target
from ala.monitor_layout import read_settings
from test_monitor_layout import current_state


class Service:
    def __init__(self):
        self.calls = []
        self.config = Config(user='user', schema_version=2, screens=[ScreenConfig(id='default', output='DP-1')]).to_dict()
        self.settings = read_settings(current_state(3)) | {'trial_supported': True}
        self.operation = {'state': 'none'}
        self.plan_state = 'validated'
        self.reject_save = False
    def request(self, action, payload=None):
        self.calls.append((action, deepcopy(payload)))
        if action == 'config-show': return {'config': deepcopy(self.config)}
        if action == 'screens-inspect': return {'targets': [], 'logical_outputs': []}
        if action == 'monitor-layout-status': return deepcopy(self.operation)
        if action == 'monitor-layout-state': return deepcopy(self.settings)
        if action == 'monitor-layout-plan': return {'state': self.plan_state}
        if action == 'monitor-layout-submit':
            self.operation = {'state': 'awaiting_confirmation', 'operation_id': 'a' * 32,
                              'deadline_monotonic': time.monotonic() + 30}
            return deepcopy(self.operation)
        if action in {'monitor-layout-confirm', 'monitor-layout-revert'}: return {'state': 'decision_recorded'}
        if action == 'screens-save':
            if self.reject_save: raise RuntimeError('다른 작업에서 구성이 변경됐습니다.')
            assert payload['expected_config'] == self.config
            self.config['screens'][0] = payload['target']
            return {'saved': True, 'config': deepcopy(self.config)}
        if action == 'screens-apply': return {'state': 'queued'}
        raise AssertionError(action)


def terminal(service, inputs):
    entries = iter(inputs)
    output = io.StringIO()
    return Terminal(service, read_line=lambda _: next(entries), stream=output, sleep=lambda _: None), output


def test_default_start_and_idle_make_no_rpc_or_terminal_control_sequences():
    service = Service()
    out = io.StringIO()
    entries = iter(['not-a-command; echo text', 'q'])
    assert run_terminal(service, read_line=lambda _: next(entries), stream=out) == 0
    assert service.calls == []
    assert '\x1b' not in out.getvalue() and '\r' not in out.getvalue()


def test_plain_entry_does_not_import_rich_or_textual():
    code = """
import sys, io
from ala.terminal import run_terminal
assert run_terminal(None,read_line=lambda _: 'q',stream=io.StringIO()) == 0
assert not any(k.split('.')[0] in {'rich','textual'} for k in sys.modules)
"""
    subprocess.run([sys.executable, '-c', code], check=True, capture_output=True, text=True, timeout=10)


def test_large_mode_lists_are_paged_without_rpc():
    service = Service(); t, out = terminal(service, ['n', '20'])
    assert t.pick('모드', list(range(100))) == 19
    assert '99.' not in out.getvalue() and len(out.getvalue().splitlines()) < 30
    assert not service.calls


def test_selection_mode_scale_and_swap_are_local_until_review():
    service = Service()
    before = deepcopy(service.settings)
    t, out = terminal(service, ['1', '1', '2', '2', 'b', 'b'])
    with pytest.raises(Cancelled): monitors(t)
    assert [a for a, _ in service.calls] == ['monitor-layout-status', 'monitor-layout-state']
    assert service.settings == before
    assert '아직 OS에 적용하지 않은 초안' in out.getvalue()


@pytest.mark.parametrize('decision,expected', [('yes', 'monitor-layout-confirm'), ('', 'monitor-layout-revert')])
def test_apply_checks_plan_and_tracks_only_owned_operation(decision, expected):
    service = Service()
    # Swap output one with output three. No other output may move.
    t, _ = terminal(service, ['1', '1', '2', '2', 'b', 'a', '', 'yes', decision])
    monitors(t)
    actions = [a for a, _ in service.calls]
    assert actions == ['monitor-layout-status', 'monitor-layout-state', 'monitor-layout-plan', 'monitor-layout-submit', expected]
    plan = service.calls[2][1]['layout']; submission = service.calls[3][1]
    assert submission == {'layout': plan, 'persistent': True}
    assert [o['x'] for o in plan['outputs']] == [3840, 1920, 0]
    assert service.calls[4][1] == {'operation_id': 'a' * 32}


def test_invalid_scale_never_reaches_plan_or_submit():
    service = Service()
    # Set scale 150%, switch to a mode that only supports 100%, review.
    t, _ = terminal(service, ['1', '3', '2', '2', '2', 'b', 'a', 'b'])
    with pytest.raises(Cancelled): monitors(t)
    assert [a for a, _ in service.calls] == ['monitor-layout-status', 'monitor-layout-state']


def test_failed_current_plan_cannot_submit():
    service = Service(); service.plan_state = 'pending'
    t, out = terminal(service, ['a', 'b'])
    with pytest.raises(Cancelled): monitors(t)
    assert not any(a == 'monitor-layout-submit' for a, _ in service.calls)
    assert '배치 검증을 통과하지 못했습니다' in out.getvalue()


def test_expired_confirmation_does_not_send_a_decision():
    service = Service(); t, _ = terminal(service, ['yes'])
    with pytest.raises(Cancelled):
        operation(t, {'state': 'awaiting_confirmation', 'operation_id': 'a' * 32,
                      'deadline_monotonic': time.monotonic() - 1})
    assert not service.calls


def test_site_save_and_apply_keep_exact_saved_snapshot_and_other_settings():
    service = Service(); before = deepcopy(service.config)
    t, _ = terminal(service, ['https://example.com/display', '', '', '', '', 'yes', 'yes'])
    edit_target(t, 'default')
    assert [a for a, _ in service.calls] == ['config-show', 'screens-save', 'screens-apply']
    assert service.calls[1][1]['expected_config'] == before
    assert service.calls[1][1]['target']['url'] == 'https://example.com/display'
    assert service.calls[2][1] == {'target': 'default', 'expected_config': service.config}
    assert service.calls[1][1]['target']['frontend'] == before['screens'][0]['frontend']


@pytest.mark.parametrize('save_answer', ['', 'b'])
def test_site_cancel_never_saves_or_applies(save_answer):
    service = Service()
    t, _ = terminal(service, ['', '', '', '', '', save_answer])
    try: edit_target(t, 'default')
    except Cancelled: pass
    assert [a for a, _ in service.calls] == ['config-show']


def test_concurrent_site_save_failure_cannot_apply():
    service = Service(); service.reject_save = True
    t, _ = terminal(service, ['', '', '', '', '', 'yes'])
    with pytest.raises(RuntimeError): edit_target(t, 'default')
    assert [a for a, _ in service.calls] == ['config-show', 'screens-save']


def test_control_sequences_in_remote_output_are_not_executed():
    t, output = terminal(None, [])
    t.result({'reason': '\x1b[2J\x1b]52;c;synthetic\x07\rhidden'})
    assert '\x1b' not in output.getvalue() and '\x07' not in output.getvalue() and '\r' not in output.getvalue()


def test_input_eof_exits_without_rpc():
    def eof(_): raise EOFError
    service = Service()
    assert run_terminal(service, read_line=eof, stream=io.StringIO()) == 0
    assert not service.calls


@pytest.mark.parametrize('args,initial', [([], 'home'), (['console'], 'home'), (['screen'], 'screens'),
    (['chrome'], 'screens'), (['config', 'edit'], 'config'), (['display', 'edit'], 'monitors')])
def test_interactive_cli_routes_to_plain_menu(monkeypatch, args, initial):
    from ala.cli import main
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    calls = []
    monkeypatch.setattr('ala.terminal.run_terminal', lambda service, **kw: calls.append(kw) or 0)
    assert main(args, service=Service()) == 0
    assert calls == [{'initial': initial}]


def test_setup_cancel_never_submits():
    service = Service(); t, _ = terminal(service, [''])
    t.setup()
    assert [a for a, _ in service.calls] == ['config-show']


def test_operation_change_cannot_confirm_an_unreviewed_job():
    service = Service()
    original = service.request
    def request(action, payload=None):
        value = original(action, payload)
        if action == 'monitor-layout-submit':
            value['state'] = 'queued'
            service.operation['operation_id'] = 'b' * 32
        return value
    service.request = request
    t, out = terminal(service, ['a', '', 'yes', 'b'])
    with pytest.raises(Cancelled): monitors(t)
    assert '배치 작업이 변경됐습니다' in out.getvalue()
    assert not any(a == 'monitor-layout-confirm' for a, _ in service.calls)


def test_real_pty_default_cli_exits_without_screen_or_mouse_modes():
    import errno
    import os
    import pty
    import select
    master, slave = pty.openpty()
    process = subprocess.Popen([sys.executable, '-c',
        'from ala.cli import main; raise SystemExit(main([],service=object()))'],
        stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    output = bytearray()
    try:
        os.write(master, b'q\n')
        until = time.monotonic() + 10
        while time.monotonic() < until:
            if select.select([master], [], [], 0.1)[0]:
                try: data = os.read(master, 8192)
                except OSError as exc:
                    if exc.errno == errno.EIO: break
                    raise
                if not data: break
                output.extend(data)
                assert len(output) < 4096
            elif process.poll() is not None: break
        assert process.wait(timeout=1) == 0
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill(); process.wait()
    assert b'\x1b' not in output
    assert '텍스트 메뉴'.encode() in output
