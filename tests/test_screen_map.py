"""Mouse capture and stale-observation guards for the read-only output map."""
import asyncio
from copy import deepcopy

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button

from ala.screen_map import ScreenMap


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


class MapApp(App):
    CSS = 'ScreenMap { height: 8; border: round white; padding: 0 1; }'

    def __init__(self, observation=None):
        super().__init__()
        self.observation = report() if observation is None else observation
        self.config = {'user': 'user', 'url': 'https://example.com/did'}
        self.moves = []

    def compose(self) -> ComposeResult:
        yield ScreenMap(id='map')
        yield Button('Other action', id='other')

    def on_mount(self):
        self.query_one(ScreenMap).update_report(self.observation, 'default', self.config)

    def on_screen_map_move_requested(self, event):
        self.moves.append(event)


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
    asyncio.run(scenario())
