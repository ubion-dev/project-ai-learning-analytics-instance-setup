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
def test_scaled_physical_mode_keeps_fixed_cards_and_gesture_regions(size):
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
            assert all((region.width, region.height) == (26, 7) for region in view.output_regions.values())
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
        self.query_one(ScreenMap).update_report(self.observation, 'default', self.config)

    def on_screen_map_move_requested(self, event):
        self.moves.append(event)

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
def test_connector_alias_and_health_poll_keep_selected_drag_read_only(size):
    async def scenario():
        app = MapApp()
        before = deepcopy(app.observation)
        config = deepcopy(app.config)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            view = app.query_one(ScreenMap)
            # Two cloned physical connectors still make one logical drop region.
            assert set(view.output_regions) == {'main', 'side'}
            assert '> default' in str(view.render())
            await start_drag(pilot, view)
            refreshed = deepcopy(app.observation)
            refreshed['targets'][0]['health']['checked_at'] = 'after'
            view.update_report(refreshed, 'default', app.config)
            assert app.mouse_captured is view
            await pilot.mouse_up(offset=point(view, 'side'))
            await pilot.pause()
            assert app.mouse_captured is None
            assert len(app.moves) == 1
            request = app.moves[0]
            assert (request.target_id, request.output_id) == ('default', 'side')
            assert request.expected_config == config
            assert request.target_snapshot == before['targets'][0]
            assert app.observation == before
            assert app.config == config
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['config', 'topology', 'resize', 'hide', 'blur', 'escape'])
def test_changed_context_cancels_drag_and_releases_mouse(change):
    async def scenario():
        app = MapApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = app.query_one(ScreenMap)
            await start_drag(pilot, view)
            destination = point(view, 'side')
            if change in {'config', 'topology'}:
                observation, config = deepcopy(app.observation), deepcopy(app.config)
                if change == 'config':
                    config['dev_tools'] = True
                else:
                    observation['logical_outputs'][1]['x'] += 1
                view.update_report(observation, 'default', config)
            elif change == 'resize':
                await pilot.resize_terminal(120, 36)
            elif change == 'hide':
                view.display = False
            elif change == 'blur':
                app.query_one('#other', Button).focus()
            else:
                await pilot.press('escape')
            await pilot.pause()
            assert app.mouse_captured is None
            await pilot.mouse_up(offset=destination)
            await pilot.pause()
            assert app.moves == []
            assert app.activations == []
    asyncio.run(scenario())


@pytest.mark.parametrize('state', [None, 'unknown'])
def test_unknown_output_observation_never_captures_or_requests_move(state):
    async def scenario():
        observation = report()
        if state is None:
            observation.pop('output_state')
        else:
            observation['output_state'] = state
        app = MapApp(observation)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = app.query_one(ScreenMap)
            await pilot.mouse_down(offset=point(view, 'main'))
            assert app.mouse_captured is None
            await pilot.hover(offset=point(view, 'side'))
            await pilot.mouse_up(offset=point(view, 'side'))
            await pilot.pause()
            assert app.moves == []
            assert app.activations == []
    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(80, 24), (120, 36)])
def test_fixed_monitor_shape_click_and_keyboard_activation_are_read_only(size):
    async def scenario():
        app = MapApp()
        before = deepcopy(app.observation)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            view = app.query_one(ScreenMap)
            regions = view.output_regions
            assert all((region.width, region.height) == (26, 7) for region in regions.values())
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


def many_outputs(count):
    observation = report()
    observation['logical_outputs'] = [
        {'id': f'screen-{index}', 'connectors': [f'PORT-{index}'], 'confirmed': True,
         'x': 1920 * index, 'y': 0, 'width': 1920, 'height': 1080}
        for index in range(count)]
    observation['targets'][0]['output'] = 'screen-0'
    return observation


def test_wrapped_cards_keep_size_and_keyboard_scrolls_parent_to_selected_row():
    async def scenario():
        app = MapApp(many_outputs(6))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = app.query_one(ScreenMap)
            assert view.output_regions['screen-2'].y == 8
            assert view.output_regions['screen-4'].y == 16
            assert all(region.height == 7 for region in view.output_regions.values())
            view.focus()
            await pilot.press('down', 'down', 'enter')
            await pilot.pause()
            assert app.activations == ['screen-4']
            assert app.query_one('#map-scroll', VerticalScroll).scroll_y > 0
            x, y = point(view, 'screen-4')
            assert app.query_one('#map-scroll').content_region.contains(x, y)
            await pilot.press('right', 'up', 'enter')
            await pilot.pause()
            assert app.activations[-1] == 'screen-3'
    asyncio.run(scenario())


@pytest.mark.parametrize('distance,activates', [(1, True), (2, False)])
def test_pointer_threshold_distinguishes_a_click_from_a_cancelled_drag(distance, activates):
    async def scenario():
        app = MapApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = app.query_one(ScreenMap)
            x, y = point(view, 'main')
            await pilot.mouse_down(offset=(x, y))
            await pilot.hover(offset=(x + distance, y))
            await pilot.mouse_up(offset=(x + distance, y))
            await pilot.pause()
            assert app.activations == (['main'] if activates else [])
            assert app.moves == [] and app.mouse_captured is None
    asyncio.run(scenario())


def test_a_single_did_in_an_unselected_card_is_frozen_at_drag_start():
    async def scenario():
        observation = report()
        other = dict(deepcopy(observation['targets'][0]), id='other', output='DP-1')
        observation['targets'].append(other)
        app = MapApp(observation)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = app.query_one(ScreenMap)
            await pilot.mouse_down(offset=point(view, 'side'))
            assert app.mouse_captured is view
            refreshed = deepcopy(observation)
            refreshed['targets'][1]['health']['checked_at'] = 'after'
            view.update_report(refreshed, 'default', app.config)
            assert app.mouse_captured is view
            await pilot.hover(offset=point(view, 'main'))
            await pilot.mouse_up(offset=point(view, 'main'))
            await pilot.pause()
            assert len(app.moves) == 1
            assert (app.moves[0].target_id, app.moves[0].output_id) == ('other', 'main')
            assert app.moves[0].target_snapshot == other
            assert app.activations == []
    asyncio.run(scenario())


def test_split_monitor_click_activates_parent_chooser_without_guessing_a_drag_target():
    async def scenario():
        observation = report()
        observation['targets'][0]['layout'] = 'left'
        observation['targets'].append(dict(deepcopy(observation['targets'][0]), id='other', layout='right'))
        app = MapApp(observation)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = app.query_one(ScreenMap)
            await pilot.mouse_down(offset=point(view, 'main'))
            assert app.mouse_captured is None
            await pilot.hover(offset=point(view, 'side'))
            await pilot.mouse_up(offset=point(view, 'side'))
            await pilot.click(offset=point(view, 'main'))
            await pilot.pause()
            assert app.moves == [] and app.activations == ['main']
    asyncio.run(scenario())


def test_captured_drop_outside_parent_viewport_cannot_hit_a_clipped_card():
    class ClippedApp(MapApp):
        CSS = '#map-scroll { height: 9; }'

    async def scenario():
        app = ClippedApp(many_outputs(4))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = app.query_one(ScreenMap)
            hidden = point(view, 'screen-2')
            assert not app.query_one('#map-scroll').content_region.contains(*hidden)
            await pilot.mouse_down(offset=point(view, 'screen-0'))
            await pilot.hover(offset=hidden)
            await pilot.mouse_up(offset=hidden)
            await pilot.pause()
            assert app.moves == [] and app.activations == []
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
            view.update_report(observation, 'default', app.config)
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
            view.update_report(refreshed, 'default', app.config)
            assert app.activations == []
            await pilot.press('enter')
            await pilot.pause()
            assert app.activations == ['side'] and app.moves == []
            assert app.focused_outputs == ['side']
    asyncio.run(scenario())
