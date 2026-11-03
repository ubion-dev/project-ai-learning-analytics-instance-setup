from pathlib import Path
import sys

import pytest

from ala.config import Config
from ala.engine import Engine
from ala.model import CheckResult, Report
from ala.system import Host, Paths


def test_stream_reports_before_command_finishes_and_keeps_output(tmp_path, capsys):
    ack = tmp_path / 'ack'
    program = """import pathlib,sys,time
p=pathlib.Path(sys.argv[1])
print('__ALA_SUBSTEP__\\tpackages\\tstarted',flush=True)
deadline=time.monotonic()+3
while not p.exists() and time.monotonic()<deadline: time.sleep(.01)
if not p.exists(): sys.exit(9)
print('finished',flush=True)
"""
    lines = []
    def observe(line):
        lines.append(line)
        ack.write_text('observed')
    assert Host().execute_reported([sys.executable, '-c', program, str(ack)], on_line=observe) == 0
    assert any('packages' in line for line in lines)
    assert 'finished' in capsys.readouterr().out


def test_observer_failure_drains_child_to_completion_before_raising(tmp_path, capsys):
    done = tmp_path / 'done'
    program = "import pathlib,sys; print('begin',flush=True); print('x'*200000,flush=True); pathlib.Path(sys.argv[1]).write_text('done')"
    def broken(_):
        raise ValueError('record write failed')
    with pytest.raises(ValueError, match='record write failed'):
        Host().execute_reported([sys.executable, '-c', program, str(done)], on_line=broken)
    assert done.read_text() == 'done'
    capsys.readouterr()


def test_engine_accepts_only_structured_known_task_events(tmp_path, monkeypatch):
    monkeypatch.setattr('ala.engine.platform_error', lambda paths: None)
    class FakeHost:
        done = False
        def execute_reported(self, argv, *, env, on_line):
            assert argv[-1] == 'Asia/Seoul'
            assert env['ALA_REPORT_PROGRESS'] == '1'
            for line in ('ordinary localized log\n', '__ALA_SUBSTEP__\tpackages\tstarted\n',
                         '__ALA_SUBSTEP__\tunknown-task\tcompleted\n',
                         '__ALA_SUBSTEP__\tpackages\tunknown-state\n',
                         'prefix __ALA_SUBSTEP__\tchrome\tstarted\n',
                         '__ALA_SUBSTEP__\tpackages\tcompleted\n'):
                on_line(line)
            self.done = True
            return 0
    host = FakeHost()
    engine = Engine(Config(user='user'), host=host, paths=Paths(tmp_path))
    engine._account = lambda name: (None, CheckResult('user', 'ok'))
    engine._base = lambda: [CheckResult('base.package.git', 'ok' if host.done else 'change_required')]
    for method in ('_updates','_hardware','_shell','_ssh','_docker','_gpu','_dev_tools','_display_policy'):
        setattr(engine, method, lambda *args, **kwargs: [CheckResult('unused', 'not_applicable')])
    engine._extra_display = lambda **kwargs: [CheckResult('chrome', 'not_applicable')]
    engine.inspect = lambda: Report([CheckResult('base.package.git', 'ok')])
    events = []
    assert engine.apply(on_substep=lambda *values: events.append(values)).exit_code == 0
    assert events == [('base','packages','started'), ('base','packages','completed')]


def test_stream_preserves_nonzero_module_exit_status(capsys):
    assert Host().execute_reported([sys.executable, '-c', "print('failed module'); raise SystemExit(7)"]) == 7
    assert 'failed module' in capsys.readouterr().out
