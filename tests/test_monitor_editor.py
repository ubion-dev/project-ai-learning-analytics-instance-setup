import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from textual.app import App
from textual.widgets import Button, Select

from ala.monitor_draft import LayoutDraft
from ala.monitor_layout import read_settings
from ala.monitor_tui import MonitorEditor, LayoutReview
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




def test_review_is_explicit_and_confirmation_keeps_output_site_identity():
    async def scenario():
        service = Service(); app = EditorApp(service)
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause()
            editor = app.screen
            editor.draft.place('DP-1', 'below', 'DP-2'); editor.sync_fields()
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


def test_selection_and_direction_reuse_choices_but_mode_change_updates_scales(monkeypatch):
    async def scenario():
        app = EditorApp(Service(2))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            editor = app.screen
            calls = []
            original = Select.set_options
            def record(widget, options):
                calls.append(widget.id)
                return original(widget, options)
            monkeypatch.setattr(Select, 'set_options', record)
            editor.query_one('#monitor-port', Select).value = 'DP-2'
            await pilot.pause()
            assert calls == ['monitor-anchor']
            calls.clear()
            editor.draft.place('DP-2', 'below', 'DP-1')
            editor.sync_fields()
            await pilot.pause()
            assert calls == []
            editor.draft.set_scale(1.5)
            editor.draft.set_mode('small')
            editor.sync_fields()
            await pilot.pause()
            assert calls == ['monitor-scale']
            assert editor.query_one('#monitor-scale', Select).value is Select.NULL
            assert editor.query_one('#monitor-review', Button).disabled
            editor.query_one('#monitor-scale', Select).value = '1.0'
            await pilot.pause()
            assert editor.draft.output()['scale'] == 1
            assert not editor.query('#monitor-canvas')
    asyncio.run(scenario())


def test_direct_tui_monitor_entry_opens_editor_read_only():
    from ala.simple_tui import SimpleApp
    from test_screens_tui import Service as ScreenService
    class Combined(ScreenService):
        def __init__(self):
            super().__init__(); self.monitors = Service(2)
        async def call(self, action, payload=None):
            if action.startswith('monitor-'): return await self.monitors.call(action, payload)
            return await super().call(action, payload)
    async def scenario():
        service = Combined()
        app = SimpleApp(service=service, initial_section='screens', initial_monitor_editor=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, MonitorEditor) and app.screen.draft
            assert service.monitors.calls == [('monitor-layout-status', None), ('monitor-layout-state', None)]
            await pilot.press('escape'); await pilot.pause()
            assert not isinstance(app.screen, MonitorEditor)
    asyncio.run(scenario())


@pytest.mark.parametrize('relation,expected', [('left', (-1920, 0)), ('right', (1920, 0)),
    ('above', (0, -1080)), ('below', (0, 1080)), ('swap', (0, 0))])
def test_explicit_relative_position_keeps_modes_and_site_assignments(relation, expected):
    model = draft(2)
    before = deepcopy(model.request)
    model.place('DP-2', relation, 'DP-1')
    assert (model.output('DP-2')['x'], model.output('DP-2')['y']) == expected
    assert model.output('DP-2')['mode_id'] == before['outputs'][1]['mode_id']
    assert model.output('DP-2')['scale'] == before['outputs'][1]['scale']
    assert model.validate()['site_assignments'] == 'preserved'


def test_relative_placement_uses_logical_dimensions_and_preserves_third_monitor():
    raw = current_state(3)
    raw[1][0][1][0][1:3] = [3840, 2160]
    raw[2][0][2] = 2.0
    model = LayoutDraft(read_settings(raw))
    original = deepcopy(model.output('DP-3'))
    model.place('DP-2', 'below', 'DP-1')
    assert (model.output('DP-2')['x'], model.output('DP-2')['y']) == (0, 1080)
    assert model.output('DP-3') == original


def test_relative_placement_rejects_overlap_without_changing_any_output():
    model = draft(3)
    original = deepcopy(model.request)
    with pytest.raises(ValueError, match='겹칩니다'):
        model.place('DP-1', 'right', 'DP-2')
    assert model.request == original
    for anchor in ['DP-1', 'missing']:
        with pytest.raises(ValueError): model.place('DP-1', 'below', anchor)
    assert model.request == original










@pytest.mark.parametrize('size', [(80, 24), (120, 36)])
@pytest.mark.parametrize('relation,expected', [('left', (-1920, 0)), ('right', (1920, 0)),
    ('above', (0, -1080)), ('below', (0, 1080))])
def test_four_direction_buttons_edit_only_local_draft_without_canvas(size, relation, expected):
    async def scenario():
        service = Service(2); app = EditorApp(service)
        observed = deepcopy(service.settings)
        async with app.run_test(size=size) as pilot:
            await pilot.pause(); editor = app.screen
            assert not editor.query('#monitor-canvas') and not editor.query('#monitor-x')
            assert not editor.query('#monitor-position')
            editor.query_one('#monitor-port', Select).focus()
            await pilot.press('enter', 'end', 'enter'); await pilot.pause()
            assert editor.draft.selected == 'DP-2'
            assert editor.query_one('#monitor-anchor', Select).value == 'DP-1'
            button = editor.query_one('#monitor-' + relation, Button)
            assert button.region.bottom <= size[1]
            button.focus(); await pilot.press('enter'); await pilot.pause()
            assert (editor.draft.output()['x'], editor.draft.output()['y']) == expected
            assert editor.draft.validate()['site_assignments'] == 'preserved'
            assert service.settings == observed
            assert service.calls == [('monitor-layout-status', None), ('monitor-layout-state', None)]
            await pilot.press('escape'); await pilot.pause()
            assert app.screen is not editor
    asyncio.run(scenario())


def test_direction_overlap_keeps_all_outputs_and_single_output_disables_directions():
    from textual.widgets import Static
    async def scenario(count):
        service = Service(count); app = EditorApp(service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(); editor = app.screen
            before = deepcopy(editor.draft.request)
            if count == 1:
                assert editor.query_one('#monitor-anchor', Select).value is Select.NULL
                for direction in ('left', 'right', 'above', 'below'):
                    assert editor.query_one('#monitor-' + direction, Button).disabled
            else:
                await pilot.click('#monitor-right'); await pilot.pause()
                assert '겹칩니다' in str(editor.query_one('#monitor-message', Static).render())
                assert editor.draft.request == before
            assert service.calls == [('monitor-layout-status', None), ('monitor-layout-state', None)]
    asyncio.run(scenario(3)); asyncio.run(scenario(1))


def test_direction_change_requires_review_apply_and_explicit_confirmation():
    async def scenario():
        service = Service(2); app = EditorApp(service)
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause(); editor = app.screen
            editor.query_one('#monitor-port', Select).value = 'DP-2'
            await pilot.pause(); await pilot.click('#monitor-below'); await pilot.pause()
            assert not any(a == 'monitor-layout-submit' for a, _ in service.calls)
            assert editor.draft.output()['y'] == 1080
            await pilot.click('#monitor-review'); await pilot.pause()
            assert isinstance(app.screen, LayoutReview)
            await pilot.click('#layout-review-apply'); await pilot.pause()
            assert [p for a, p in service.calls if a == 'monitor-layout-submit'] == [
                {'layout': editor.draft.request, 'persistent': True}]
            await pilot.click('#monitor-keep'); await pilot.pause()
            assert ('monitor-layout-confirm', {'operation_id': 'a' * 32}) in service.calls
    asyncio.run(scenario())


def test_clearing_anchor_disables_direction_buttons_and_initial_trial_is_visible():
    async def scenario(active):
        service = Service(2)
        if active: service.operation = {'state': 'awaiting_confirmation', 'operation_id': 'a' * 32}
        app = EditorApp(service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(); editor = app.screen
            if active:
                assert editor.query_one('#monitor-confirm-buttons').display
                assert not editor.query_one('#monitor-keep', Button).disabled
                assert editor.query_one('#monitor-anchor', Select).disabled
                assert service.calls == [('monitor-layout-status', None)]
            else:
                assert not editor.query_one('#monitor-left', Button).disabled
                editor.query_one('#monitor-anchor', Select).value = Select.NULL
                await pilot.pause()
            for direction in ('left', 'right', 'above', 'below'):
                assert editor.query_one('#monitor-' + direction, Button).disabled
    asyncio.run(scenario(False)); asyncio.run(scenario(True))
