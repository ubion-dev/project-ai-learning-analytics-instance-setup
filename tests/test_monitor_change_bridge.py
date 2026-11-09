import json

import pytest

from ala import display, monitor_change, monitors
from ala.config import Config
from ala.system import Paths
from test_display import setup
from test_monitor_layout import current_state
from test_monitors import BridgeHost


def test_user_rpc_sends_data_to_fixed_non_root_entry(setup):
    config, paths, host = setup
    host.script_result = {'state': 'ok', 'persistent': False}
    request = {'method': 'verify', 'literal': '$literal; `literal`'}
    value = display._user_call(config, host, paths, 'monitor-layout', request=request)
    assert value['state'] == 'ok'
    calls = [argv for argv, _ in host.calls if argv[0] == 'runuser']
    assert len(calls) == 1
    assert calls[0][:4] == ['runuser', '-u', 'user', '--']
    assert calls[0][-4:-1] == ['_chrome-user', 'monitor-layout', '--request']
    assert json.loads(calls[0][-1]) == request
    assert not any('google-chrome' in p for p in calls[0])


def test_user_action_only_calls_layout_bridge_as_normal_user(setup, monkeypatch):
    config, paths, host = setup
    monkeypatch.setattr('ala.display.os.getuid', lambda: 1000)
    calls = []
    monkeypatch.setattr(monitors, 'bridge', lambda method, request, **kwargs:
                        calls.append((method, request)) or {'state': 'ok', 'persistent': False})
    request = {'method': 'verify'}
    assert display.user_action('monitor-layout', config=config, paths=paths,
                               host=host, home=paths.at('/home/user'), request=request)['exit_code'] == 0
    assert calls == [('ApplyLayout', request)] and host.calls == []


@pytest.mark.parametrize('problem', [None, 'missing-capability', 'session', 'locked', 'compositor'])
def test_observation_requires_supported_session_and_actual_compositor_rectangles(tmp_path, monkeypatch, problem):
    raw = current_state(2)
    bridge_value = {'protocol': 1, 'state': 'ok', 'gnome_version': '49.4', 'display_config': raw,
                    'capabilities': ['monitor-layout-trial'],
                    'layout_session': '00000000-0000-0000-0000-000000000001',
                    'compositor_monitors': [{'x': 0, 'y': 0, 'width': 1920, 'height': 1080},
                                            {'x': 1920, 'y': 0, 'width': 1920, 'height': 1080}]}
    if problem == 'missing-capability': bridge_value['capabilities'] = []
    if problem == 'session': bridge_value['layout_session'] = None
    if problem == 'compositor': bridge_value['compositor_monitors'][0]['width'] = 1280
    inventory = monitors.topology(host=BridgeHost(bridge_value))
    monkeypatch.setattr('ala.screens._inventory', lambda *a: inventory)
    monkeypatch.setattr('ala.screens.inspect_session', lambda *a: 'locked' if problem == 'locked' else 'ok')
    store = monitor_change.Store(Paths(tmp_path))
    if problem:
        with pytest.raises(RuntimeError): monitor_change._observe(store, Config(user='user'))
    else:
        settings, session = monitor_change._observe(store, Config(user='user'))
        assert settings['state'] == 'ok' and session == bridge_value['layout_session']
    assert list(tmp_path.iterdir()) == []
