"""Mouse capture and stale-observation guards for the read-only output map."""
import asyncio
from copy import deepcopy

import pytest
from rich.cells import cell_len
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Button

from ala.screen_map import ScreenMap, output_mode_text


def report():
    return {
        'output_state': 'ok',
        'logical_outputs': [
            {'id': 'main', 'connectors': ['HDMI-1', 'HDMI-2'], 'confirmed': True,
             'x': 0, 'y': 0, 'width': 1920, 'height': 1080},
            {'id': 'side', 'connectors': ['DP-1'], 'confirmed': True,
             'x': 1920, 'y': 0, 'width': 1920, 'height': 1080},
        ],
        'targets': [
            {'id': 'default', 'url': 'https://example.com/did', 'output': 'HDMI-2',
             'layout': 'fullscreen', 'autostart': True, 'frontend': {'enabled': False},
             'health': {'checked_at': 'before'}},
        ],
    }


def scaled_output():
    logical = {'id': 'DP-1', 'connectors': ['DP-1'], 'confirmed': True,
               'x': -2560, 'y': 0, 'width': 2560, 'height': 1440, 'scale': 1.5}
    observation = {'output_state': 'ok', 'logical_outputs': [logical], 'outputs': [
        {'id': 'DP-1', 'connector': 'DP-1', 'width': 3840, 'height': 2160, 'active': True},
    ]}
    return observation, logical


def test_mode_text_uses_physical_current_mode_and_observed_scale_without_mutation():
    observation, logical = scaled_output()
    before = deepcopy(observation)
    assert output_mode_text(observation, logical) == '3840×2160 · 150%'
    assert observation == before


@pytest.mark.parametrize('scale,expected', [(1, '100%'), (1.25, '125%'), (1.5, '150%'), (2.0, '200%')])
def test_mode_text_formats_positive_observed_scaling(scale, expected):
    observation, logical = scaled_output()
    logical['scale'] = scale
    assert output_mode_text(observation, logical) == '3840×2160 · ' + expected


@pytest.mark.parametrize('scale', [None, True, False, 0, -1, -1.25, float('nan'), float('inf'), -float('inf'), '1.5', 1e308])
def test_unknown_scale_is_never_inferred_from_physical_and_logical_size(scale):
    observation, logical = scaled_output()
    logical['scale'] = scale
    assert output_mode_text(observation, logical) == '3840×2160 · 배율 미상'


@pytest.mark.parametrize('condition', ['missing', 'different', 'duplicate', 'duplicate-identical', 'logical-duplicate'])
def test_ambiguous_connector_mapping_labels_the_logical_fallback(condition):
    observation, logical = scaled_output()
    if condition == 'missing':
        observation['outputs'] = []
    elif condition == 'different':
        observation['outputs'][0]['connector'] = 'HDMI-1'
    elif condition in {'duplicate', 'duplicate-identical'}:
        duplicate = deepcopy(observation['outputs'][0])
        if condition == 'duplicate':
            duplicate.update(width=1920, height=1080)
        observation['outputs'].append(duplicate)
    else:
        logical['connectors'] = ['DP-1', 'DP-1']
    assert output_mode_text(observation, logical) == '논리 2560×1440 · 150%'


@pytest.mark.parametrize('dimensions', [(None, None), (0, 2160), (-3840, 2160), (3840, True), ('3840', 2160)])
def test_invalid_physical_mode_does_not_become_a_pixel_resolution(dimensions):
    observation, logical = scaled_output()
    observation['outputs'][0].update(width=dimensions[0], height=dimensions[1])
    assert output_mode_text(observation, logical) == '논리 2560×1440 · 150%'


@pytest.mark.parametrize('same_mode', [False, True])
def test_mirrored_outputs_never_choose_a_representative_physical_mode(same_mode):
    observation, logical = scaled_output()
    logical.update(id='DP-1+HDMI-1', connectors=['DP-1', 'HDMI-1'], mirrored=True)
    observation['outputs'].append({'connector': 'HDMI-1', 'width': 3840 if same_mode else 1920,
                                   'height': 2160 if same_mode else 1080})
    assert output_mode_text(observation, logical) == '복제 · 150%'


def test_missing_resolution_and_scale_are_explicitly_unknown():
    observation, logical = scaled_output()
    observation['outputs'] = []
    logical.update(width=None, height=None)
    assert output_mode_text(observation, logical) == '해상도 미상 · 150%'
    logical['scale'] = None
    assert output_mode_text(observation, logical) == '해상도·배율 확인 필요'


@pytest.mark.parametrize('size', [(80, 24), (120, 36)])
def test_scaled_mode_keeps_proportional_shapes_and_click_regions(size):
    async def scenario():
        observation = report()
        observation['logical_outputs'][1].update(x=-2560, width=2560, height=1440, scale=1.5)
        observation['outputs'] = [{'connector': 'DP-1', 'width': 3840, 'height': 2160}]
        before = deepcopy(observation)
        app = MapApp(observation)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            view = app.query_one(ScreenMap)
            assert '3840×2160 · 150%' in str(view.render())
            regions = view.output_regions
            assert regions['side'].width > regions['main'].width
            assert regions['side'].height > regions['main'].height
            await pilot.click(offset=point(view, 'side'))
            await pilot.pause()
            assert app.activations == ['side'] and not app.moves
            assert observation == before
    asyncio.run(scenario())


class MapApp(App):
    CSS = '#map-scroll { height: 1fr; } ScreenMap { height: auto; border: round white; padding: 0 1; }'

    def __init__(self, observation=None):
        super().__init__()
        self.observation = report() if observation is None else observation
        self.config = {'user': 'user', 'url': 'https://example.com/did'}
        self.moves = []
        self.activations = []
        self.focused_outputs = []

    def compose(self) -> ComposeResult:
        with VerticalScroll(id='map-scroll'):
            yield ScreenMap(id='map')
        yield Button('Other action', id='other')

    def on_mount(self):
        self.query_one(ScreenMap).update_report(self.observation, 'default')

    def on_screen_map_output_activated(self, event):
        self.activations.append(event.output_id)

    def on_screen_map_output_focused(self, event):
        self.focused_outputs.append(event.output_id)


def point(view, output):
    box = view.output_regions[output]
    return (view.content_region.x + box.x + min(2, box.width - 1),
            view.content_region.y + box.y + min(1, box.height - 1))


async def start_drag(pilot, view):
    await pilot.mouse_down(offset=point(view, 'main'))
    assert view.app.mouse_captured is view
    await pilot.hover(offset=point(view, 'side'))








@pytest.mark.parametrize('size', [(80, 24), (120, 36)])
def test_equal_monitor_shapes_click_and_keyboard_activation_are_read_only(size):
    async def scenario():
        app = MapApp()
        before = deepcopy(app.observation)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            view = app.query_one(ScreenMap)
            regions = view.output_regions
            assert abs(regions['main'].width - regions['side'].width) <= 1
            assert regions['main'].height == regions['side'].height
            assert regions['main'].y == regions['side'].y
            for output in ('main', 'side'):
                await pilot.click(offset=point(view, output))
                await pilot.pause()
            assert app.activations == ['main', 'side']
            assert app.focused_outputs == []
            view.focus()
            await pilot.press('left', 'enter', 'right', 'enter')
            await pilot.pause()
            assert app.activations == ['main', 'side', 'main', 'side']
            assert app.focused_outputs == ['main', 'side']
            assert app.moves == [] and app.observation == before
    asyncio.run(scenario())














@pytest.mark.parametrize('status', ['pending', 'guide', 'failed'])
def test_card_status_updates_keep_cjk_borders_aligned_without_actions(status):
    async def scenario():
        app = MapApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = app.query_one(ScreenMap)
            before = view.output_regions
            observation = deepcopy(app.observation)
            target = observation['targets'][0]
            target['service'] = {'ActiveState': 'active'}
            if status == 'pending':
                target['settings'] = {'pending': True}
                expected = '설정 적용 대기'
            elif status == 'guide':
                target.update(applied_url=None, health={'state': 'waiting_configuration', 'local_page_ready': True})
                expected = '설정 안내 표시 중'
            else:
                target['service']['ActiveState'] = 'failed'
                expected = '실패 · 로그 확인'
            view.update_report(observation, 'default')
            await pilot.pause()
            rendered = str(view.render())
            assert expected in rendered
            right = max(region.right for region in view.output_regions.values())
            assert all(cell_len(line) == right for line in rendered.splitlines())
            assert view.output_regions == before
            assert app.moves == [] and app.activations == []
    asyncio.run(scenario())


def test_health_poll_preserves_keyboard_card_focus_until_explicit_enter():
    async def scenario():
        app = MapApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = app.query_one(ScreenMap)
            view.focus()
            await pilot.press('right', 'right')
            assert app.focused_outputs == ['side']
            refreshed = deepcopy(app.observation)
            refreshed['targets'][0]['health']['checked_at'] = 'after'
            view.update_report(refreshed, 'default')
            assert app.activations == []
            await pilot.press('enter')
            await pilot.pause()
            assert app.activations == ['side'] and app.moves == []
            assert app.focused_outputs == ['side']
    asyncio.run(scenario())


@pytest.mark.parametrize('vertical', [False, True])
def test_observed_layout_controls_display_positions_and_keyboard_navigation(vertical):
    async def scenario():
        observation = report()
        if vertical: observation['logical_outputs'][1].update(x=0, y=1080)
        app = MapApp(observation)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(); view = app.query_one(ScreenMap)
            first, second = view.output_regions['main'], view.output_regions['side']
            if vertical:
                assert first.x == second.x and first.y < second.y
            else:
                assert first.y == second.y and first.x < second.x
            view.focus()
            await pilot.press('down' if vertical else 'right', 'enter'); await pilot.pause()
            assert app.activations == ['side'] and not app.moves
            assert app.mouse_captured is None
    asyncio.run(scenario())


def test_narrow_layout_uses_coordinate_list_instead_of_false_wrapped_diagram():
    async def scenario():
        observation = report()
        observation['logical_outputs'][1].update(width=64, height=64)
        app = MapApp(observation)
        async with app.run_test(size=(50, 24)) as pilot:
            await pilot.pause(); view = app.query_one(ScreenMap)
            assert '좌표 목록' in str(view.render()) and '(1920, 0)' in str(view.render())
            assert all(region.height == 2 for region in view.output_regions.values())
            view.focus(); await pilot.press('down', 'enter'); await pilot.pause()
            assert app.activations == ['side'] and not app.moves
    asyncio.run(scenario())


@pytest.mark.parametrize('width', [80, 120])
def test_smaller_offset_screen_preserves_proportions_and_vertical_overlap(width):
    from ala.screen_map import _topology
    observation = {'output_state': 'ok', 'targets': [], 'logical_outputs': [
        {'id':'west','x':0,'y':164,'width':1280,'height':720,'confirmed':True},
        {'id':'center','x':1280,'y':0,'width':1920,'height':1080,'confirmed':True},
        {'id':'east','x':3200,'y':0,'width':1920,'height':1080,'confirmed':True}]}
    before = deepcopy(observation)
    drawing, boxes = _topology(observation, width)
    regions = {box.output_id: box.region for box in boxes}
    west, center, east = (regions[key] for key in ('west','center','east'))
    assert '좌표 목록' not in drawing
    assert west.width / center.width == pytest.approx(2/3, abs=.07)
    assert west.height / center.height == pytest.approx(2/3, abs=.1)
    assert center.size == east.size and center.y == east.y
    assert 0 <= west.y - center.y <= 2
    assert west.y < center.bottom and center.y < west.bottom
    assert west.right == center.x and center.right == east.x
    assert all(cell_len(line) == max(box.region.right for box in boxes)
               for line in drawing.splitlines()[:max(box.region.bottom for box in boxes)])
    assert observation == before


def test_projection_uses_logical_dimensions_and_handles_rotated_negative_origin():
    from ala.screen_map import _topology
    observation={'output_state':'ok','targets':[], 'logical_outputs':[
        {'id':'portrait','x':-1080,'y':-200,'width':1080,'height':1920,'scale':1,'transform':1},
        {'id':'landscape','x':0,'y':0,'width':1920,'height':1080,'scale':2}]}
    _, boxes = _topology(observation, 100)
    a,b = [box.region for box in boxes]
    assert a.x >= 0 and a.y == 0 and a.right == b.x
    assert a.width < b.width and a.height > b.height


@pytest.mark.parametrize('condition',['missing','overlap','tiny'])
def test_unreadable_projection_uses_coordinate_list_without_moving_outputs(condition):
    from ala.screen_map import _topology
    observation=report()
    if condition=='missing': observation['logical_outputs'][1].pop('width')
    if condition=='overlap': observation['logical_outputs'][1]['x']=0
    if condition=='tiny': observation['logical_outputs'][1].update(width=32,height=32)
    before=deepcopy(observation)
    drawing, boxes=_topology(observation,80)
    assert '좌표 목록' in drawing
    assert all(box.region.height==2 for box in boxes)
    assert observation==before
