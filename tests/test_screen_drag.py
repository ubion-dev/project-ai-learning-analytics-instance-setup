"""Retired drag gestures must never move sites, start jobs or open editors."""
import asyncio
from copy import deepcopy

import pytest

from ala.screen_map import ScreenMap
from ala.simple_tui import SimpleApp
from test_screens_tui import Service


def point(map_view, output):
    region = map_view.output_regions[output]
    return (map_view.content_region.x + region.x + min(2, region.width - 1),
            map_view.content_region.y + region.y + min(1, region.height - 1))


async def drag(pilot, view, source='HDMI-1', destination='DP-1'):
    await pilot.mouse_down(offset=point(view, source))
    await pilot.hover(offset=point(view, destination))
    await pilot.mouse_up(offset=point(view, destination))
    await pilot.pause()


@pytest.mark.parametrize('size', [(80, 24), (120, 36)])
@pytest.mark.parametrize('condition', ['normal', 'unassigned', 'pending', 'split', 'failed'])
def test_drag_is_ignored_without_capture_editor_or_mutation(size, condition):
    async def scenario():
        service = Service()
        if condition == 'unassigned': service.screens['targets'] = service.screens['targets'][:1]
        if condition == 'pending': service.screens['targets'][0]['settings'] = {'pending': True}
        if condition == 'split': service.screens['targets'][0]['layout'] = 'left'
        if condition == 'failed': service.screens['targets'][0]['service']['ActiveState'] = 'failed'
        before = deepcopy(service.screens)
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            view = app.query_one(ScreenMap)
            await pilot.mouse_down(offset=point(view, 'HDMI-1'))
            assert app.mouse_captured is None
            await pilot.hover(offset=point(view, 'DP-1'))
            assert app.mouse_captured is None
            await pilot.mouse_up(offset=point(view, 'DP-1')); await pilot.pause()
            assert len(app.screen_stack) == 1
            assert service.screens == before
            assert not any(a in {'screens-save','screens-apply','screens-action','screens-swap-submit',
                                 'monitor-layout-submit'} for a, _ in service.calls)
    asyncio.run(scenario())
