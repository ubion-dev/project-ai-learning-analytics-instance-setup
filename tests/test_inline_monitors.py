"""Synthetic inline controls, safe defaults and output allocation; no host changes."""
import asyncio
from copy import deepcopy

import pytest
from textual.widgets import Button, Input, Select, Static

from ala.monitor_draft import LayoutDraft
from ala.monitor_layout import read_settings
from ala.monitor_tui import LayoutReview, MonitorControls
from ala.screen_map import ScreenMap
from ala.screens_tui import ScreenPanel, TargetEditor
from ala.simple_tui import SimpleApp
from test_monitor_editor import Service as MonitorService
from test_monitor_layout import current_state
from test_screens_tui import Service as ScreenService


class Service(ScreenService):
    def __init__(self):
        super().__init__()
        self.monitors = MonitorService(2)
        self.screens['logical_outputs'] = [
            {'id': 'DP-1', 'connectors': ['DP-1'], 'x': 0, 'y': 0, 'width': 1920, 'height': 1080, 'scale': 1, 'confirmed': True},
            {'id': 'DP-2', 'connectors': ['DP-2'], 'x': 1920, 'y': 0, 'width': 1920, 'height': 1080, 'scale': 1, 'confirmed': True}]
        self.screens['targets'] = [self.target('default', 'DP-1'), self.target('display-2', 'DP-2')]
    async def call(self, action, payload=None):
        if action.startswith('monitor-'): return await self.monitors.call(action, payload)
        return await super().call(action, payload)


@pytest.mark.parametrize('size', [(80, 24), (120, 40)])
def test_inline_remote_preview_scale_reset_review_and_confirmation(size):
    async def scenario():
        service = Service(); app = SimpleApp(service=service, initial_section='screens')
        original = deepcopy(service.screens)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            assert not service.monitors.calls
            await pilot.click('#screens-monitor-layout'); await pilot.pause()
            controls = app.query_one(MonitorControls)
            assert len(app.screen_stack) == 1 and controls.draft
            for direction in ('left','right','above','below'):
                assert controls.query_one('#monitor-'+direction,Button).content_region.height == 1
            controls.query_one('#monitor-below', Button).focus(); await pilot.press('enter'); await pilot.pause()
            assert controls.draft.output()['y'] == 1080
            assert '초안' in str(app.query_one('#screens-layout-preview', Static).render())
            regions = app.query_one(ScreenMap).output_regions
            assert regions['DP-1'].y > regions['DP-2'].y
            assert service.screens == original
            controls.query_one('#monitor-scale', Select).value = '2.0'; await pilot.pause()
            assert controls.draft.output()['scale'] == 2
            assert controls.draft.output('DP-2')['scale'] == 1
            controls.query_one('#monitor-reset', Button).focus(); await pilot.press('enter'); await pilot.pause()
            assert [(o['x'],o['y'],o['scale']) for o in controls.draft.request['outputs']] == [(0,0,1),(1920,0,1)]
            controls.query_one('#monitor-below', Button).focus(); await pilot.press('enter'); await pilot.pause()
            calls = deepcopy(service.monitors.calls)
            assert calls == [('monitor-layout-status',None),('monitor-layout-state',None)]
            controls.query_one('#monitor-review', Button).focus(); await pilot.press('enter'); await pilot.pause()
            assert isinstance(app.screen, LayoutReview)
            await pilot.click('#layout-review-apply'); await pilot.pause()
            assert len(app.screen_stack) == 1
            assert sum(a == 'monitor-layout-submit' for a,_ in service.monitors.calls) == 1
            controls.query_one('#monitor-keep', Button).focus(); await pilot.press('enter'); await pilot.pause()
            assert ('monitor-layout-confirm',{'operation_id':'a'*32}) in service.monitors.calls
            assert service.screens == original
    asyncio.run(scenario())


def test_defaults_are_atomic_preserve_primary_rotation_and_reject_unsupported_scale():
    raw = current_state(3)
    raw[1][0][1][1][6]['is-preferred'] = True
    raw[2][1][3] = 1
    model = LayoutDraft(read_settings(raw)); model.set_scale(2)
    before = deepcopy(model.settings)
    model.reset_defaults(); plan = model.validate()
    assert plan['outputs'][0]['mode_id'] == 'small'
    assert [o['x'] for o in plan['outputs']] == [0,1280,2360]
    assert plan['outputs'][1]['transform'] == 1 and plan['outputs'][0]['primary']
    assert model.settings == before and plan['site_assignments'] == 'preserved'
    raw[1][1][1][0][5] = [2.0]; raw[2][1][2] = 2.0
    raw[1][1][1][0][4] = 2.0
    model = LayoutDraft(read_settings(raw)); request = deepcopy(model.request)
    with pytest.raises(ValueError, match='100%'): model.reset_defaults()
    assert model.request == request


def test_occupied_ports_are_excluded_but_own_and_opposite_split_are_allowed():
    async def scenario():
        service = Service(); app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=(100,36)) as pilot:
            await pilot.pause(); panel = app.query_one(ScreenPanel)
            panel.open_target('default'); await pilot.pause(); editor = app.screen
            assert isinstance(editor,TargetEditor)
            choices = {value for _,value in editor.output_choices('fullscreen')}
            assert 'DP-1' in choices and 'DP-2' not in choices
            assert panel.output_conflict('DP-2','fullscreen','default') == 'display-2'
            panel.report['targets'][1]['layout'] = 'left'
            assert 'DP-2' in {value for _,value in editor.output_choices('right')}
            assert 'DP-2' not in {value for _,value in editor.output_choices('left')}
            editor.query_one('#target-layout',Select).value = 'right'; await pilot.pause()
            editor.query_one('#target-output',Select).value = 'DP-2'; await pilot.pause()
            # An external allocation change is checked again at save time.
            panel.report['targets'][1]['layout'] = 'fullscreen'
            editor.query_one('#target-save',Button).focus(); await pilot.press('enter'); await pilot.pause()
            assert app.screen is editor
            assert '다른 화면이 사용 중' in str(editor.query_one('#target-error',Static).render())
            assert not any(a=='screens-save' for a,_ in service.calls)
    asyncio.run(scenario())


def test_open_is_single_flight_and_hiding_preserves_unsaved_monitor_draft():
    async def scenario():
        service = Service(); app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=(100,36)) as pilot:
            await pilot.pause(); panel = app.query_one(ScreenPanel)
            await asyncio.gather(panel.open_monitor_controls(),panel.open_monitor_controls())
            await pilot.pause(); controls=app.query_one(MonitorControls)
            assert len(app.query(MonitorControls)) == 1
            controls.draft.place('DP-1','below','DP-2'); controls.sync_fields()
            before=deepcopy(controls.draft.request)
            await controls.action_close_editor(); await panel.open_monitor_controls(); await pilot.pause()
            assert controls.draft.request == before and controls.display
            assert service.monitors.calls == [('monitor-layout-status',None),('monitor-layout-state',None)]
            # Clicking a monitor in inline editing selects it without opening a modal.
            view=app.query_one(ScreenMap)
            view.focus(); await pilot.press('right','enter'); await pilot.pause()
            assert len(app.screen_stack) == 1
    asyncio.run(scenario())
