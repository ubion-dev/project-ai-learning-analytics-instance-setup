"""Saved assignments and selected runtime settings are separate observations."""
from dataclasses import replace
import json
import pytest

from ala import display, screens
from ala.config import Config, ScreenConfig
from ala.runtime import RuntimeStore, settings_status
from ala.system import Paths, atomic_json


def test_inventory_reports_saved_and_selected_assignment_without_changing_either(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    boot = paths.at('/proc/sys/kernel/random/boot_id')
    boot.parent.mkdir(parents=True)
    boot.write_text('synthetic-boot')
    release = paths.at('/opt/ala/releases/1.0.0')
    (release / '.venv/bin').mkdir(parents=True)
    (release / '.venv/bin/python').write_text('# fixture')
    (release / 'entry.py').write_text('# fixture')
    (release / 'VERSION').write_text('1.0.0')
    (release / 'COMPATIBILITY.json').write_text(json.dumps(
        {'schema_version': 1, 'runtime_protocol': 1, 'config_schemas': [1, 2]}))
    applied = Config(schema_version=2, user='user', screens=(
        ScreenConfig(id='default', url='https://example.com/first', output='HDMI-1'),
        ScreenConfig(id='display-2', url='https://example.com/second', output='DP-1')))
    saved = replace(applied, screens=(applied.screens[0],
                    replace(applied.screens[1], output='DP-2', layout='right')))
    atomic_json(paths.config, saved.to_dict())
    store = RuntimeStore(paths)
    before = {target.id: store.bootstrap(target.id, release, applied) for target in applied.screens}
    inventory = {'state': 'ok', 'logical_outputs': [
        {'id': 'HDMI-1', 'connectors': ['HDMI-1'], 'x': 0, 'y': 0,
         'width': 1920, 'height': 1080, 'confirmed': True},
        {'id': 'DP-1', 'connectors': ['DP-1'], 'x': 1920, 'y': -180,
         'width': 2560, 'height': 1440, 'scale': 1.25, 'confirmed': True}]}
    monkeypatch.setattr(screens, 'hardware_topology', lambda paths: {})
    monkeypatch.setattr(screens, '_inventory', lambda *args: inventory)
    observed = []

    def status(action, context, **kwargs):
        assert action == 'status'
        observed.append((context.target_id, context.output, context.layout))
        return {'state': 'running', 'service': {'ActiveState': 'active'},
                'health': {'state': 'healthy', 'target_matches_config': True}}

    def windows(context, host, paths, action):
        assert action == 'windows'
        return {'state': 'ok', 'output': context.output, 'layout': context.layout,
                'confirmed_window_count': 1}

    monkeypatch.setattr(display, 'chrome_action', status)
    monkeypatch.setattr(display, '_user_call', windows)
    report = screens.inspect_screens(saved, paths=paths)
    first, second = report['targets']
    assert observed == [('default', 'HDMI-1', 'fullscreen'), ('display-2', 'DP-1', 'fullscreen')]
    assert first['output'] == first['applied_output'] == 'HDMI-1'
    assert (second['output'], second['layout']) == ('DP-2', 'right')
    assert (second['applied_output'], second['applied_layout']) == ('DP-1', 'fullscreen')
    assert second['placement']['output'] == 'DP-1'
    assert settings_status(saved, 'display-2', paths=paths, state=store.load('display-2'))['pending']
    assert Config.load(paths.config) == saved
    assert {target.id: store.load(target.id) for target in applied.screens} == before


def test_failed_context_does_not_present_saved_assignment_as_applied(tmp_path, monkeypatch):
    config = Config(schema_version=2, user='user', screens=(
        ScreenConfig(id='default', url='https://example.com/', output='HDMI-1'),))
    monkeypatch.setattr(screens, 'hardware_topology', lambda paths: {})
    monkeypatch.setattr(screens, '_inventory', lambda *args: {'state': 'ok'})

    def unavailable(*args):
        raise RuntimeError('synthetic unavailable selection')

    monkeypatch.setattr(screens, '_applied_context', unavailable)
    target = screens.inspect_screens(config, paths=Paths(tmp_path))['targets'][0]
    assert target['output'] == 'HDMI-1' and target['layout'] == 'fullscreen'
    assert target['state'] == 'unknown'
    assert 'applied_output' not in target and 'applied_layout' not in target


@pytest.mark.parametrize('capabilities', [[], ['identify-outputs'], ['identify-outputs','workspace-placement']])
def test_workspace_candidate_requires_running_extension_capability_without_changes(tmp_path, capabilities):
    paths=Paths(tmp_path)
    release=paths.at('/opt/ala/releases/1.0.0')
    metadata=release/'COMPATIBILITY.json'
    atomic_json(metadata, {'schema_version':1,'workspace_protocol':1})
    config=Config(schema_version=2,user='user',screens=(ScreenConfig(output='HDMI-1'),),
                  runtime_pin={'release':str(release)}).for_screen()
    before=metadata.read_bytes()
    inventory={'state':'ok','capabilities':capabilities}
    result=screens._workspace_runtime_guard(config,inventory,None,paths)
    if 'workspace-placement' in capabilities:
        assert result is None
    else:
        assert result['state']=='pending' and result['exit_code']==2
        assert 'GNOME 재로그인' in result['next_action']
    assert metadata.read_bytes()==before
    assert not paths.config.exists()


def test_old_runtime_without_workspace_contract_keeps_its_existing_preflight(tmp_path):
    paths=Paths(tmp_path)
    release=paths.at('/opt/ala/releases/1.0.0')
    atomic_json(release/'COMPATIBILITY.json', {'schema_version':1,'extension_protocol':1})
    config=Config(user='user',runtime_pin={'release':str(release)})
    assert screens._workspace_runtime_guard(config,{'capabilities':[]},None,paths) is None
