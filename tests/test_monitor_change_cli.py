import json

import pytest

from ala.cli import main


class Service:
    def __init__(self): self.calls = []
    def authenticate(self): pass
    def request(self, action, payload=None):
        self.calls.append((action, payload))
        return {'state': 'decision_recorded', 'persistent': False}


@pytest.mark.parametrize('command', ['confirm', 'revert', 'recover'])
def test_decisions_require_exact_operation_id(command, capsys):
    service = Service()
    operation_id = 'a' * 32
    assert main(['display', command, '--operation-id', operation_id, '--json'], service=service) == 0
    assert service.calls == [('monitor-layout-' + command, {'operation_id': operation_id})]
    assert json.loads(capsys.readouterr().out)['persistent'] is False


def test_trial_uses_bounded_json_input_and_status_is_separate(tmp_path, capsys):
    path = tmp_path / 'layout.json'
    path.write_text('{"schema_version":1}')
    service = Service()
    assert main(['display', 'trial', str(path), '--json'], service=service) == 0
    assert main(['display', 'trial-status', '--json'], service=service) == 0
    assert service.calls == [('monitor-layout-submit', {'layout': {'schema_version': 1}}), ('monitor-layout-status', {})]


@pytest.mark.parametrize('argv', [['display', 'trial'], ['display', 'confirm'], ['display', 'revert'],
                                ['display', 'recover'], ['display', 'trial-status', '--operation-id', 'a' * 32],
                                ['display', 'modes', '--operation-id', 'a' * 32]])
def test_invalid_usage_never_reaches_privileged_service(argv):
    service = Service()
    with pytest.raises(SystemExit) as exc: main(argv, service=service)
    assert exc.value.code == 64 and service.calls == []


def test_private_monitor_call_carries_json_without_chrome_start(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr('ala.display.user_action', lambda action, **kwargs: calls.append((action, kwargs)) or {'exit_code': 0})
    assert main(['_chrome-user', 'monitor-layout', '--request', '{"method":"verify"}']) == 0
    assert calls == [('monitor-layout', {'request': {'method': 'verify'}})]
