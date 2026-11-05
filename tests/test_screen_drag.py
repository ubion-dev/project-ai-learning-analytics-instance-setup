"""Exercise real terminal mouse events through the screen-management entry point."""
import asyncio
from copy import deepcopy

import pytest
from textual.widgets import Button

from ala.screen_map import ScreenMap
from ala.screens_tui import MoveConfirmation, ScreenPanel
from ala.simple_tui import SimpleApp
from test_screens_tui import Service


def point(map_view, output):
    region = map_view.output_regions[output]
    return (map_view.content_region.x + region.x + min(2, region.width - 1),
            map_view.content_region.y + region.y + min(1, region.height - 1))


async def drag(pilot, map_view, source='HDMI-1', destination='DP-1'):
    start = point(map_view, source)
    end = point(map_view, destination)
    await pilot.mouse_down(offset=start)
    await pilot.hover(offset=end)
    await pilot.mouse_up(offset=end)
    await pilot.pause()


@pytest.mark.parametrize('size', [(80, 24), (120, 36), (125, 42)])
@pytest.mark.parametrize('approved', [False, True])
@pytest.mark.parametrize('target_id', ['default', 'display-2'])
def test_drag_reviews_then_saves_and_applies_only_selected_did(size, approved, target_id):
    async def scenario():
        service = Service()
        service.screens['targets'] = [target for target in service.screens['targets'] if target['id'] == target_id]
        source, destination = ('HDMI-1', 'DP-1') if target_id == 'default' else ('DP-1', 'HDMI-1')
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            before = deepcopy(service.screens['targets'][0])
            await drag(pilot, app.query_one('#screens-map', ScreenMap), source, destination)
            assert isinstance(app.screen, MoveConfirmation)
            assert app.focused.id == 'move-cancel'
            assert app.screen.query_one('#move-confirm', Button).region.bottom <= size[1]
            assert not any(action in {'screens-save', 'screens-apply'} for action, _ in service.calls)
            if approved:
                await pilot.press('tab', 'enter')
            else:
                await pilot.press('escape')
            await pilot.pause()
            mutations = [(action, payload) for action, payload in service.calls
                         if action in {'screens-save', 'screens-apply', 'screens-action'}]
            if approved:
                assert [action for action, _ in mutations] == ['screens-save', 'screens-apply']
                saved = mutations[0][1]['target']
                assert saved == {key: (destination if key == 'output' else before[key])
                                 for key in ('id', 'url', 'output', 'layout', 'autostart', 'frontend')}
                assert mutations[1][1] == {'target': target_id, 'expected_config': service.config}
            else:
                assert mutations == []
                assert service.screens['targets'][0] == before
    asyncio.run(scenario())


@pytest.mark.parametrize('condition', ['multiple-occupied', 'split-occupied', 'other-pending', 'pending', 'disconnected', 'outside', 'same', 'escape'])
def test_invalid_drop_or_cancel_does_not_save_or_apply(condition):
    async def scenario():
        service = Service()
        if condition not in {'multiple-occupied', 'split-occupied', 'other-pending'}:
            service.screens['targets'] = service.screens['targets'][:1]
        if condition == 'multiple-occupied':
            service.screens['targets'].append(service.target('display-3', 'DP-1'))
        if condition == 'other-pending':
            # This saved output no longer describes the still-running placement.
            service.screens['targets'][1]['output'] = 'HDMI-2'
            service.screens['targets'][1]['settings'] = {'pending': True}
        if condition == 'split-occupied':
            service.screens['targets'][0]['layout'] = 'left'
            service.screens['targets'][1]['layout'] = 'right'
        if condition == 'pending':
            service.screens['targets'][0]['settings'] = {'pending': True}
        if condition == 'disconnected':
            service.screens['logical_outputs'][1]['confirmed'] = False
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            map_view = app.query_one('#screens-map', ScreenMap)
            if condition in {'outside', 'same', 'escape'}:
                start = point(map_view, 'HDMI-1')
                await pilot.mouse_down(offset=start)
                await pilot.hover(offset=point(map_view, 'DP-1'))
                if condition == 'escape':
                    await pilot.press('escape')
                await pilot.mouse_up(offset=(0, 0) if condition == 'outside' else start)
            else:
                await drag(pilot, map_view)
            await pilot.pause()
            assert not isinstance(app.screen, MoveConfirmation)
            assert not any(action in {'screens-save', 'screens-apply', 'screens-action'}
                           for action, _ in service.calls)
    asyncio.run(scenario())


def test_configuration_change_during_confirmation_blocks_the_move():
    async def scenario():
        service = Service()
        service.screens['targets'] = service.screens['targets'][:1]
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await drag(pilot, app.query_one('#screens-map', ScreenMap))
            service.config['dev_tools'] = True
            await pilot.press('tab', 'enter')
            await pilot.pause()
            assert not any(action == 'screens-apply' for action, _ in service.calls)
            assert service.screens['targets'][0]['output'] == 'HDMI-1'
            assert '구성이 변경' in app.query_one('#screens', ScreenPanel).messages['default']
    asyncio.run(scenario())


def test_monitor_disconnect_during_confirmation_blocks_the_move():
    async def scenario():
        service = Service()
        service.screens['targets'] = service.screens['targets'][:1]
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await drag(pilot, app.query_one('#screens-map', ScreenMap))
            service.screens['logical_outputs'][1]['confirmed'] = False
            await pilot.press('tab', 'enter')
            await pilot.pause()
            assert not any(action in {'screens-save', 'screens-apply'} for action, _ in service.calls)
    asyncio.run(scenario())


def test_unsuccessful_save_never_applies_or_starts_a_did():
    class FailedSave(Service):
        async def call(self, action, payload=None):
            if action == 'screens-save':
                self.calls.append((action, deepcopy(payload)))
                return {'saved': False}
            return await super().call(action, payload)

    async def scenario():
        service = FailedSave()
        service.screens['targets'] = service.screens['targets'][:1]
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await drag(pilot, app.query_one('#screens-map', ScreenMap))
            await pilot.press('tab', 'enter')
            await pilot.pause()
            assert not any(action in {'screens-apply', 'screens-action'} for action, _ in service.calls)
            assert service.screens['targets'][0]['output'] == 'HDMI-1'
            assert '설정 저장 · 실패' in app.query_one('#screens', ScreenPanel).messages['default']
    asyncio.run(scenario())


def test_confirmation_waits_for_inflight_query_and_rechecks_disconnected_output():
    async def scenario():
        service = Service()
        service.screens['targets'] = service.screens['targets'][:1]
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await drag(pilot, app.query_one('#screens-map', ScreenMap))
            panel = app.query_one('#screens', ScreenPanel)
            service.gate = asyncio.Event()
            query = asyncio.create_task(panel.refresh_state())
            try:
                await asyncio.sleep(0)
                assert panel.reading
                await pilot.press('tab', 'enter')
                await pilot.pause()
                assert not any(action in {'screens-save', 'screens-apply'}
                               for action, _ in service.calls)
                service.screens['logical_outputs'][1]['confirmed'] = False
                service.gate.set()
                await asyncio.wait_for(query, 1)
                await pilot.pause()
                assert not any(action in {'screens-save', 'screens-apply'}
                               for action, _ in service.calls)
            finally:
                service.gate.set()
                await asyncio.wait_for(query, 1)
    asyncio.run(scenario())


def test_apply_preserves_saved_snapshot_when_background_refresh_sees_new_configuration():
    class ChangedOnRefresh(Service):
        saved = False
        changed = False
        app = None

        async def call(self, action, payload=None):
            if action == 'screens-inspect' and self.saved and not self.changed:
                self.changed = True
                self.config['url'] = 'https://example.com/unreviewed'
                self.screens['targets'][0]['url'] = self.config['url']
                await self.app.load_state()
            response = await super().call(action, payload)
            if action == 'screens-save':
                self.saved = True
            return response

    async def scenario():
        service = ChangedOnRefresh()
        service.screens['targets'] = service.screens['targets'][:1]
        saved_snapshot = deepcopy(service.config)
        app = SimpleApp(service=service, initial_section='screens')
        service.app = app
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await drag(pilot, app.query_one('#screens-map', ScreenMap))
            await pilot.press('tab', 'enter')
            await pilot.pause()
            requests = [payload for action, payload in service.calls if action == 'screens-apply']
            assert requests == [{'target': 'default', 'expected_config': saved_snapshot}]
            assert app._config['url'] == 'https://example.com/unreviewed'
            assert requests[0]['expected_config'] != service.config
    asyncio.run(scenario())
