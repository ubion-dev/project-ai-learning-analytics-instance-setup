"""Quick diagnosis enters the monitor editor without a late navigation surprise."""
import asyncio

from textual.widgets import Input, Select, Static, Tabs

from ala.quick_tui import QuickPanel
from ala.screens_tui import ScreenPanel, TargetEditor
from ala.simple_tui import SimpleApp
from test_screens_tui import Service


def test_quick_manage_keeps_requested_did_and_opens_settings_read_only():
    async def scenario():
        service = Service()
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_one('#tabs', Tabs).active = 'quick-tab'
            await pilot.pause()
            quick = app.query_one(QuickPanel)
            quick.query_one(Select).value = 'display-2'
            await pilot.pause()
            quick.manage_pressed()
            quick.selected = 'default'
            await pilot.pause()
            assert isinstance(app.screen, TargetEditor)
            assert app.screen.query_one('#target-id', Input).value == 'display-2'
            assert app.screen.query_one('#target-operations').region.y >= app.screen.query_one('#target-fields').region.y
            assert not any(action in {'screens-save', 'screens-apply', 'screens-action'} for action, _ in service.calls)
            await pilot.press('escape')
            assert app.query_one('#screens-map').has_focus
    asyncio.run(scenario())


def test_keyboard_monitor_selection_updates_status_before_opening_settings():
    async def scenario():
        service = Service()
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_one('#screens-map').focus()
            await pilot.press('right')
            assert app.query_one(ScreenPanel).selected == 'display-2'
            assert 'display-2' in str(app.query_one('#screens-result', Static).render())
            await pilot.press('enter')
            assert isinstance(app.screen, TargetEditor)
            assert app.screen.query_one('#target-id', Input).value == 'display-2'
            assert not any(action in {'screens-save', 'screens-apply', 'screens-action'} for action, _ in service.calls)
    asyncio.run(scenario())


def test_leaving_while_quick_manage_waits_does_not_open_a_late_editor():
    async def scenario():
        service = Service()
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_one('#tabs', Tabs).active = 'quick-tab'
            await pilot.pause()
            quick = app.query_one(QuickPanel)
            quick.query_one(Select).value = 'display-2'
            await pilot.pause()
            service.gate = asyncio.Event()
            quick.manage_pressed()
            await pilot.pause()
            app.query_one('#tabs', Tabs).active = 'status-tab'
            await pilot.pause()
            service.gate.set()
            await pilot.pause()
            assert app.query_one('#pages').current == 'status'
            assert len(app.screen_stack) == 1
            assert not any(action in {'screens-save', 'screens-apply', 'screens-action'} for action, _ in service.calls)
    asyncio.run(scenario())


def test_vertical_observed_layout_scrolls_on_main_tab_and_opens_selected_site():
    from ala.screen_map import ScreenMap
    async def scenario():
        service = Service()
        service.screens['logical_outputs'][1].update(x=0, y=1080)
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(); view = app.query_one(ScreenMap)
            regions = view.output_regions
            assert regions['HDMI-1'].x == regions['DP-1'].x
            assert regions['HDMI-1'].y < regions['DP-1'].y
            view.focus(); await pilot.press('down'); await pilot.pause()
            assert app.query_one(ScreenPanel).selected == 'display-2'
            assert app.query_one('#screens-map-scroll').scroll_y > 0
            await pilot.press('enter'); await pilot.pause()
            assert isinstance(app.screen, TargetEditor) and app.screen.target_id == 'display-2'
            assert not any(a in {'screens-save','screens-apply','screens-swap-submit','screens-action'} for a, _ in service.calls)
    asyncio.run(scenario())
