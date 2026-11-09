import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from textual.app import App
from textual.widgets import Button, Select

from ala.monitor_draft import LayoutDraft
from ala.monitor_layout import read_settings
from ala.monitor_tui import MonitorCanvas, MonitorEditor, LayoutReview
from test_monitor_layout import current_state


def draft(count=3): return LayoutDraft(read_settings(current_state(count)))


def test_three_monitor_swap_preserves_other_positions_and_site_independence():
    model = draft()
    original = deepcopy(model.request)
    model.drop('DP-1', 0, 0, 'DP-3')
    assert [o['x'] for o in model.request['outputs']] == [3840, 1920, 0]
    assert model.request['outputs'][1] == original['outputs'][1]
    assert model.validate()['site_assignments'] == 'preserved'


def test_unequal_adjacent_screens_swap_without_moving_third_output():
    raw = current_state(3)
    raw[1][0][1][0][1] = 1280
    raw[2][1][0] = 1280; raw[2][2][0] = 3200
    model = LayoutDraft(read_settings(raw))
    model.drop('DP-1', 0, 0, 'DP-2')
    assert [o['x'] for o in model.request['outputs']] == [1920, 0, 3200]
    model.validate()


def test_vertical_drop_and_invalid_scale_need_explicit_reselection():
    model = draft(2)
    model.drop('DP-2', 0, 1080)
    assert model.validate()['outputs'][1]['y'] == 1080
    model.selected = 'DP-2'; model.set_scale(1.5); model.set_mode('small')
    assert model.output()['scale'] is None
    with pytest.raises(ValueError): model.validate()
    model.set_scale(1)
    assert model.validate()['outputs'][1]['width'] == 1280


class Service:
    def __init__(self, count=3):
        self.calls = []; self.settings = read_settings(current_state(count)) | {'trial_supported': True}
        self.operation = {'state': 'none'}
    async def call(self, action, payload=None):
        self.calls.append((action, deepcopy(payload)))
        if action == 'monitor-layout-state': return deepcopy(self.settings)
        if action == 'monitor-layout-status': return deepcopy(self.operation)
        if action == 'monitor-layout-submit':
            self.operation = {'operation_id': 'a' * 32, 'state': 'awaiting_confirmation', 'reason': '화면을 확인하세요.'}
            return deepcopy(self.operation)
        if action in {'monitor-layout-confirm', 'monitor-layout-revert'}:
            self.operation['state'] = 'confirmed' if action.endswith('confirm') else 'rolled_back'
            return {'state': 'decision_recorded'}
        raise AssertionError(action)


class EditorApp(App):
    def __init__(self, service):
        super().__init__(); self.service = service; self.opened = []
        self.panel = SimpleNamespace(owner=self, targets_on_output=lambda key: [{'id': 'site-' + key}],
                                     refresh_state=self.refresh_state, open_target=lambda key: self.opened.append(key))
    async def refresh_state(self, **kwargs): pass
    def on_mount(self): self.push_screen(MonitorEditor(self.panel))


def point(canvas, key):
    left, top, w, h = canvas.projection.regions[key]
    return canvas.content_region.x + left + w // 2, canvas.content_region.y + top + h // 2


@pytest.mark.parametrize('size', [(80, 24), (120, 40)])
def test_opening_is_read_only_and_sites_use_existing_monitor_identity(size):
    async def scenario():
        service = Service(); app = EditorApp(service)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            editor = app.screen
            assert isinstance(editor, MonitorEditor) and editor.draft
            assert editor.query_one('#monitor-review', Button).region.bottom <= size[1]
            assert service.calls == [('monitor-layout-status', None), ('monitor-layout-state', None)]
            editor.query_one('#monitor-port', Select).value = 'DP-3'
            await pilot.pause()
            assert editor.draft.selected == 'DP-3'
            await pilot.click('#monitor-site')
            assert app.opened == ['site-DP-3']
    asyncio.run(scenario())


def test_drag_does_not_move_target_or_draft_until_mouse_release():
    async def scenario():
        service = Service(); app = EditorApp(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            editor = app.screen; canvas = editor.query_one(MonitorCanvas)
            original = deepcopy(editor.draft.request)
            target = point(canvas, 'DP-3')
            await pilot.mouse_down(offset=point(canvas, 'DP-1'))
            await pilot.hover(offset=target); await pilot.pause()
            assert canvas.drag['target'] == 'DP-3'
            assert editor.draft.request == original
            assert canvas.projection.regions['DP-3'][0] > canvas.projection.regions['DP-2'][0]
            await pilot.mouse_up(offset=target); await pilot.pause()
            assert [o['x'] for o in editor.draft.request['outputs']] == [3840, 1920, 0]
            assert not any(a == 'monitor-layout-submit' for a, _ in service.calls)
    asyncio.run(scenario())


def test_review_is_explicit_and_confirmation_keeps_output_site_identity():
    async def scenario():
        service = Service(); app = EditorApp(service)
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause()
            editor = app.screen
            editor.draft.drop('DP-1', 0, 0, 'DP-2'); editor.sync_fields()
            await pilot.click('#monitor-review'); await pilot.pause()
            assert isinstance(app.screen, LayoutReview)
            assert not any(a == 'monitor-layout-submit' for a, _ in service.calls)
            await pilot.click('#layout-review-apply'); await pilot.pause()
            await editor.poll(); await pilot.pause()
            submissions = [p for a, p in service.calls if a == 'monitor-layout-submit']
            assert len(submissions) == 1 and submissions[0]['persistent'] is True
            assert editor.query_one('#monitor-keep', Button).disabled is False
            await pilot.click('#monitor-keep'); await pilot.pause(); await editor.poll()
            assert ('monitor-layout-confirm', {'operation_id': 'a' * 32}) in service.calls
    asyncio.run(scenario())


def test_escape_during_drag_cancels_edit_without_apply():
    async def scenario():
        service = Service(); app = EditorApp(service)
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause(); editor = app.screen; canvas = editor.query_one(MonitorCanvas)
            before = deepcopy(editor.draft.request)
            await pilot.mouse_down(offset=point(canvas, 'DP-1')); await pilot.hover(offset=point(canvas, 'DP-3'))
            await pilot.press('escape'); await pilot.pause()
            assert canvas.drag is None and editor.draft.request == before
            assert isinstance(app.screen, MonitorEditor)
    asyncio.run(scenario())


def test_mode_change_keeps_unsupported_scale_unselected_in_actual_form():
    async def scenario():
        service = Service(1); app = EditorApp(service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(); editor = app.screen
            editor.query_one('#monitor-scale', Select).value = '1.5'
            await pilot.pause()
            editor.query_one('#monitor-mode', Select).value = 'small'
            await pilot.pause()
            assert editor.draft.output()['scale'] is None
            assert editor.query_one('#monitor-scale', Select).value is Select.NULL
            assert editor.query_one('#monitor-review', Button).disabled
            editor.query_one('#monitor-scale', Select).value = '1.0'
            await pilot.pause()
            assert editor.draft.validate()['outputs'][0]['width'] == 1280
    asyncio.run(scenario())


def test_existing_screen_panel_opens_editor_and_monitor_site_settings():
    from test_screens_tui import Service as ExistingService
    from ala.simple_tui import SimpleApp
    from ala.screens_tui import TargetEditor
    from textual.widgets import Input
    class IntegratedService(ExistingService):
        async def call(self, action, payload=None):
            if action in {'monitor-layout-state', 'monitor-layout-status'}:
                return deepcopy(read_settings(current_state(1)) | {'trial_supported': True}) if action.endswith('state') else {'state': 'none'}
            return await super().call(action, payload)
    async def scenario():
        service = IntegratedService()
        service.screens['logical_outputs'] = [{'id': 'DP-1', 'connectors': ['DP-1'], 'x': 0, 'y': 0, 'width': 1920, 'height': 1080, 'confirmed': True}]
        service.screens['targets'] = [service.target('default', 'DP-1')]
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.click('#screens-monitor-layout'); await pilot.pause()
            assert isinstance(app.screen, MonitorEditor)
            await pilot.click('#monitor-site'); await pilot.pause()
            assert isinstance(app.screen, TargetEditor)
            assert app.screen.query_one('#target-url', Input).value == 'https://example.com/default'
            await pilot.click('#target-cancel'); await pilot.pause()
            assert isinstance(app.screen, MonitorEditor)
    asyncio.run(scenario())
