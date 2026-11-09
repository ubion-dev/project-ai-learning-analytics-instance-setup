"""Synthetic mode data: no desktop session or real monitor is changed."""
from copy import deepcopy
import json

import pytest

from ala import monitor_layout, monitors
from ala.cli import main
from ala.config import Config
from ala.service import Operations
from ala.system import Paths


def current_state(count=3):
    specs = [[f'DP-{i + 1}', 'Synthetic', 'Panel', ''] for i in range(count)]
    physical = [[spec, [
        ['full', 1920, 1080, 60.0, 1.0, [1.0, 1.5, 2.0], {'is-current': True}],
        ['small', 1280, 720, 60.0, 1.0, [1.0, 2.0], {}],
    ], {}] for spec in specs]
    logical = [[i * 1920, 0, 1.0, 0, i == 0, [spec], {}] for i, spec in enumerate(specs)]
    return [7, physical, logical, {'layout-mode': 1}]


def settings_and_request(count=3):
    settings = monitor_layout.read_settings(current_state(count))
    assert settings['state'] == 'ok'
    return settings, deepcopy(settings['edit_template'])


def test_inventory_exposes_per_mode_scales_without_hardware_identity():
    raw = current_state()
    raw[1][1][1][0][5] = [1.0, 2.0]
    before = deepcopy(raw)
    settings = monitor_layout.read_settings(raw)
    assert settings['outputs'][0]['modes'][0]['supported_scales'] == [1.0, 1.5, 2.0]
    assert settings['outputs'][1]['modes'][0]['supported_scales'] == [1.0, 2.0]
    assert settings['global_scale_required'] is False
    assert 'Synthetic' not in json.dumps(settings)
    assert raw == before


def test_three_monitor_swap_preserves_primary_rotation_and_inputs():
    settings, request = settings_and_request()
    before = deepcopy((settings, request))
    request['outputs'][0]['x'], request['outputs'][2]['x'] = 3840, 0
    edited = deepcopy(request)
    plan = monitor_layout.build_plan(settings, request)
    assert [(p['id'], p['x'], p['primary']) for p in plan['outputs']] == [
        ('DP-1', 3840, True), ('DP-2', 1920, False), ('DP-3', 0, False)]
    assert plan['site_assignments'] == 'preserved'
    assert plan['apply_supported'] is False and plan['backend_verified'] is False
    assert settings == before[0] and request == edited


def test_vertical_multirow_layout_and_negative_origin():
    settings, request = settings_and_request(4)
    for item, (x, y) in zip(request['outputs'], [(-1920, -1080), (0, -1080), (-1920, 0), (0, 0)]):
        item.update(x=x, y=y)
    plan = monitor_layout.build_plan(settings, request)
    assert [(item['x'], item['y']) for item in plan['outputs']] == [(0, 0), (1920, 0), (0, 1080), (1920, 1080)]


def test_resolution_change_requires_supported_scale_reselection():
    settings, request = settings_and_request(1)
    request['outputs'][0].update(mode_id='small', scale=1.5)
    with pytest.raises(ValueError, match='배율을 다시 선택'):
        monitor_layout.build_plan(settings, request)
    request['outputs'][0]['scale'] = 2.0
    plan = monitor_layout.build_plan(settings, request)
    assert (plan['outputs'][0]['width'], plan['outputs'][0]['height']) == (640, 360)


@pytest.mark.parametrize('layout_mode,expected', [(1, (540, 960)), (2, (1080, 1920))])
def test_rotation_and_layout_mode_determine_dimensions(layout_mode, expected):
    raw = current_state(1)
    raw[3]['layout-mode'] = layout_mode
    raw[2][0][3] = 1
    settings = monitor_layout.read_settings(raw)
    request = deepcopy(settings['edit_template'])
    request['outputs'][0]['scale'] = 2
    output = monitor_layout.build_plan(settings, request)['outputs'][0]
    assert (output['width'], output['height']) == expected
    assert output['transform'] == 1


def test_global_scale_requirement_blocks_independent_scales():
    raw = current_state(2)
    raw[3]['global-scale-required'] = True
    settings = monitor_layout.read_settings(raw)
    request = deepcopy(settings['edit_template'])
    request['outputs'][1]['scale'] = 2
    with pytest.raises(ValueError, match='같은 화면 배율'):
        monitor_layout.build_plan(settings, request)


def test_advertised_fractional_scale_uses_mutter_float32_dimensions():
    raw = current_state(1)
    scale = 1.3333333730697632
    raw[1][0][1][0][5].append(scale)
    settings = monitor_layout.read_settings(raw)
    request = deepcopy(settings['edit_template'])
    request['outputs'][0]['scale'] = scale
    output = monitor_layout.build_plan(settings, request)['outputs'][0]
    assert (output['width'], output['height']) == (1440, 810)


@pytest.mark.parametrize('x,y,reason', [(100, 0, '겹칩니다'), (2000, 0, '빈틈'), (1920, 1080, '빈틈')])
def test_overlap_gap_and_corner_only_contact_block(x, y, reason):
    settings, request = settings_and_request(2)
    request['outputs'][1].update(x=x, y=y)
    with pytest.raises(ValueError, match=reason):
        monitor_layout.build_plan(settings, request)


def test_unequal_sizes_swap_cannot_move_third_monitor_to_hide_overlap():
    settings, request = settings_and_request()
    request['outputs'][0]['x'] = 3840
    request['outputs'][2].update(mode_id='small', x=0)
    # The smaller monitor no longer reaches its neighbor; never silently reflow.
    with pytest.raises(ValueError, match='빈틈'):
        monitor_layout.build_plan(settings, request)
    assert request['outputs'][1]['x'] == 1920


def test_stale_revision_detects_serial_mode_properties_and_hotplug_changes():
    settings, request = settings_and_request()
    for change in ('serial', 'mode', 'properties', 'hotplug'):
        raw = current_state()
        if change == 'serial': raw[0] += 1
        if change == 'mode': raw[1][0][1][1][3] = 59.9
        if change == 'properties': raw[3]['global-scale-required'] = True
        if change == 'hotplug':
            raw[1].pop()
            raw[2].pop()
        with pytest.raises(ValueError, match='상태가 변경'):
            monitor_layout.build_plan(monitor_layout.read_settings(raw), request)


def test_mirrored_outputs_are_visible_but_not_editable():
    raw = current_state(2)
    raw[2][0][5].extend(raw[2][1][5])
    raw[2].pop()
    settings = monitor_layout.read_settings(raw)
    assert settings['state'] == 'ok' and 'edit_template' not in settings
    with pytest.raises(ValueError, match='복제 출력'):
        monitor_layout.build_plan(settings, {'schema_version': 1, 'expected_revision': settings['revision'], 'outputs': []})


@pytest.mark.parametrize('field,value', [('x', True), ('y', 0.5), ('scale', True), ('scale', float('nan')),
                                        ('scale', float('inf')), ('scale', 10**1000),
                                        ('mode_id', 'missing'), ('id', 'DP-9')])
def test_invalid_edit_does_not_get_coerced(field, value):
    settings, request = settings_and_request(1)
    request['outputs'][0][field] = value
    with pytest.raises(ValueError):
        monitor_layout.build_plan(settings, request)


def test_duplicate_missing_and_extra_outputs_or_fields_block():
    settings, template = settings_and_request()
    for change in ('duplicate', 'missing', 'extra', 'url', 'version'):
        request = deepcopy(template)
        if change == 'duplicate': request['outputs'][1] = deepcopy(request['outputs'][0])
        if change == 'missing': request['outputs'].pop()
        if change == 'extra': request['outputs'].append(deepcopy(request['outputs'][0]))
        if change == 'url': request['outputs'][0]['url'] = 'https://example.com'
        if change == 'version': request['schema_version'] = True
        with pytest.raises(ValueError):
            monitor_layout.build_plan(settings, request)


@pytest.mark.parametrize('change', ['missing-layout-mode', 'bool-layout-mode', 'bad-scale', 'duplicate-mode',
                                   'nan-refresh', 'duplicate-current', 'bad-spec', 'bool-primary'])
def test_bad_inventory_does_not_offer_an_edit_template(change):
    raw = current_state()
    if change == 'missing-layout-mode': raw[3].pop('layout-mode')
    if change == 'bool-layout-mode': raw[3]['layout-mode'] = True
    if change == 'bad-scale': raw[1][0][1][0][5] = [True]
    if change == 'duplicate-mode': raw[1][0][1].append(deepcopy(raw[1][0][1][0]))
    if change == 'nan-refresh': raw[1][0][1][0][3] = float('nan')
    if change == 'duplicate-current': raw[1][0][1][1][6]['is-current'] = True
    if change == 'bad-spec': raw[2][0][5][0] = ['DP-1', 'Wrong', 'Panel', '']
    if change == 'bool-primary': raw[2][0][4] = 1
    result = monitor_layout.read_settings(raw)
    assert result['state'] == 'unknown' and result['apply_supported'] is False
    assert 'edit_template' not in result


def test_layout_metadata_failure_does_not_break_existing_window_inventory(monkeypatch):
    raw = current_state(1)
    raw[3] = {}
    monkeypatch.setattr(monitors, 'bridge', lambda *a, **k: {
        'state': 'ok', 'gnome_version': '49.4', 'display_config': raw,
        'compositor_monitors': [{'x': 0, 'y': 0, 'width': 1920, 'height': 1080}]})
    value = monitors.topology()
    assert value['state'] == 'ok' and value['logical_outputs'][0]['confirmed']
    assert value['layout_settings']['state'] == 'unknown'


@pytest.mark.parametrize('data', [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}',
                                  b' ' * 1048577, b'[' * 1500, b'\xff'])
def test_plan_file_rejects_ambiguous_or_unbounded_json(tmp_path, data):
    path = tmp_path / 'layout.json'
    path.write_bytes(data)
    with pytest.raises(ValueError):
        monitor_layout.load_request(path)


class ReadOnlyService:
    def __init__(self): self.calls = []

    def authenticate(self): pass

    def request(self, action, payload=None):
        self.calls.append((action, payload))
        assert action in {'screens-inspect', 'monitor-layout-state', 'monitor-layout-plan'}
        return {'state': 'validated', 'apply_supported': False}


def test_cli_keeps_existing_display_commands_and_adds_read_only_plan(tmp_path, capsys):
    service = ReadOnlyService()
    for action in ('list', 'status', 'modes'):
        assert main(['display', action, '--json'], service=service) == 0
        assert json.loads(capsys.readouterr().out)['apply_supported'] is False
    _, request = settings_and_request()
    path = tmp_path / 'layout.json'
    path.write_text(json.dumps(request))
    assert main(['display', 'plan', str(path), '--json'], service=service) == 0
    assert service.calls == [('screens-inspect', None), ('screens-inspect', None),
                             ('monitor-layout-state', None), ('monitor-layout-plan', {'layout': request})]


@pytest.mark.parametrize('args', [['display', 'plan'], ['display', 'modes', 'layout.json'], ['display', 'apply']])
def test_cli_invalid_usage_never_contacts_service(args):
    service = ReadOnlyService()
    with pytest.raises(SystemExit) as exc:
        main(args, service=service)
    assert exc.value.code == 64 and service.calls == []


def test_service_requeries_for_plan_without_writing_or_changing_sites(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    operations = Operations(paths=paths)
    config = Config(user='user', url='https://example.com')
    monkeypatch.setattr(operations, 'config', lambda: config)
    monkeypatch.setattr('ala.screens.inspect_session', lambda *a: 'ok')
    settings, request = settings_and_request()
    calls = []

    def inventory(*args):
        calls.append(args)
        return {'state': 'ok', 'layout_settings': settings}

    monkeypatch.setattr('ala.screens._inventory', inventory)
    assert operations.dispatch('monitor-layout-state', {})['revision'] == settings['revision']
    plan = operations.dispatch('monitor-layout-plan', {'layout': request})
    assert plan['state'] == 'validated' and len(calls) == 2
    assert config.url == 'https://example.com' and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('session', ['disabled', 'unknown', 'multiple', 'pending', 'locked', 'lock_unknown'])
def test_service_blocks_unusable_session_before_inventory(tmp_path, monkeypatch, session):
    operations = Operations(paths=Paths(tmp_path))
    monkeypatch.setattr(operations, 'config', lambda: Config(user='user'))
    monkeypatch.setattr('ala.screens.inspect_session', lambda *a: session)
    monkeypatch.setattr('ala.screens._inventory', lambda *a: pytest.fail('unexpected inventory'))
    result = operations.dispatch('monitor-layout-state', {})
    assert result['exit_code'] == 2 and result['session'] == session
    assert list(tmp_path.iterdir()) == []
