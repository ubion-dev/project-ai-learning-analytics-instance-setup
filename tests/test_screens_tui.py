"""SSH keyboard acceptance without commands, network, or real display changes."""
import asyncio
from copy import deepcopy
from rich.cells import cell_len

from textual.widgets import Button, Input, Static, Tabs, TextArea

from ala.config import Config
from ala.model import CheckResult, Report
from ala.screens_tui import ScreenPanel, TargetEditor, site_label, topology_text
from ala.simple_tui import SimpleApp


class Service:
    def __init__(self):
        self.calls = []
        self.config = Config(user='user', url='https://example.com/display').to_dict()
        self.report = Report([CheckResult('base.package.chrome', 'ok')]).to_dict()
        self.screens = {'outputs': [{'connector': 'HDMI-1'}, {'connector': 'DP-1'}],
                        'logical_outputs': [{'id': 'HDMI-1', 'connectors': ['HDMI-1'], 'x': 0, 'y': 0, 'width': 1920, 'height': 1080},
                                            {'id': 'DP-1', 'connectors': ['DP-1'], 'x': 1920, 'y': 0, 'width': 1920, 'height': 1080}],
                        'confirmed_windows': 2,
                        'targets': [self.target('default', 'HDMI-1'), self.target('display-2', 'DP-1')]}
        self.gate = None

    def target(self, key, output):
        return {'id': key, 'url': 'https://example.com/'+key, 'output': output, 'layout': 'fullscreen',
                'autostart': True, 'frontend': {'enabled': False, 'manifest_url': None},
                'service': {'ActiveState': 'active'}, 'health': {'state': 'healthy', 'target_matches_config': True},
                'placement': {'state': 'ok', 'window_count': 1}}

    async def call(self, action, payload=None):
        self.calls.append((action, deepcopy(payload)))
        await asyncio.sleep(0)
        if action == 'config-show':
            return {'config': deepcopy(self.config), 'exists': True}
        if action == 'runs-list':
            return {'runs': []}
        if action == 'status':
            return dict(self.report, config=self.config, runs=[])
        if action == 'screens-inspect':
            if self.gate:
                await self.gate.wait()
            return deepcopy(self.screens)
        if action == 'screens-action':
            if payload['action'] == 'logs':
                return {'log': 'fixed event\n', 'exit_code': 0}
            if payload['action'] == 'stop':
                selected = next(v for v in self.screens['targets'] if v['id'] == payload['target'])
                selected['service']['ActiveState'] = 'inactive'
                selected['health'] = {}
                selected['placement']['window_count'] = 0
                self.screens['confirmed_windows'] -= 1
            return {'state': 'ok', 'reason': '대상 종료를 확인했습니다.', 'exit_code': 0}
        if action == 'screens-save':
            assert payload['expected_config'] == self.config
            return {'saved': True, 'config': self.config, 'state': 'pending', 'reason': '등록 적용 대기'}
        raise AssertionError(action)


def test_diagram_uses_geometry_and_small_terminal_uses_list():
    report = Service().screens
    drawing = topology_text(report, 110, 8)
    assert '┌' in drawing and 'HDMI-1' in drawing and 'DP-1' in drawing
    compact = topology_text(report, 70, 3)
    assert '(1920, 0)' in compact and '1920×1080' in compact
    assert '확인 불가' in topology_text({})


def test_site_overview_omits_query_and_fragment_and_rejects_userinfo():
    assert site_label('https://example.com/page?session=private#part') == 'https://example.com/page'
    assert site_label('https://user:fake@example.com/page') == '주소 확인 필요'
    assert site_label(None) == '주소 미설정'


def test_three_tabs_and_opening_screen_management_is_read_only():
    async def scenario():
        service = Service()
        app = SimpleApp(service=service)
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.pause()
            assert len(app.query_one('#tabs', Tabs).query('Tab')) == 3
            assert not any(a.startswith('screens-') for a, _ in service.calls)
            await pilot.press('c')
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert app.query_one('#pages').current == 'screens'
            summary = str(app.query_one('#screens-summary', Static).render())
            assert '관리 대상 2개' in summary and '확인된 창 2개' in summary
            assert 'https://example.com/default' in app.query_one('#screens-detail', TextArea).text
            assert not any(a in {'screens-action', 'screens-save', 'submit'} for a, _ in service.calls)
    asyncio.run(scenario())


def test_stop_only_selected_target_and_exit_leaves_other_screen_running():
    async def scenario():
        service = Service()
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            await pilot.press('down')
            await pilot.pause()
            app.query_one('#screens-stop', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            requests = [p for a, p in service.calls if a == 'screens-action']
            assert requests == [{'action': 'stop', 'target': 'display-2'}]
            assert service.screens['targets'][0]['service']['ActiveState'] == 'active'
            await pilot.press('q')
        assert len([a for a, _ in service.calls if a == 'screens-action']) == 1
    asyncio.run(scenario())


def test_small_terminal_controls_and_detail_remain_inside_viewport():
    async def scenario():
        service = Service()
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            for selector in ('#screens-summary', '#screens-table', '#screens-detail', '#screens-actions'):
                region = app.query_one(selector).region
                assert region.x >= 0 and region.y >= 0
                assert region.right <= 80 and region.bottom <= 23
            assert app.query_one('#screens-stop', Button).region.width >= 7
            for button in app.query('#screens-actions Button'):
                assert button.content_size.width >= cell_len(str(button.label)) + 2
            await pilot.resize_terminal(125, 42)
            await pilot.pause()
            await app.query_one('#screens', ScreenPanel).refresh_state()
            assert '┌' in str(app.query_one('#screens-map', Static).render())
    asyncio.run(scenario())


def test_slow_screen_query_does_not_block_navigation_or_submit_changes():
    async def scenario():
        service = Service()
        service.gate = asyncio.Event()
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press('c')
            await pilot.pause()
            await pilot.press('s')
            await pilot.pause()
            assert app.query_one('#pages').current == 'status'
            service.gate.set()
            await pilot.pause()
            assert not any(a in {'screens-action', 'screens-save'} for a, _ in service.calls)
    asyncio.run(scenario())


def test_editor_escape_preserves_config_and_performs_no_save():
    async def scenario():
        service = Service()
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_one('#screens-edit', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            assert isinstance(app.screen, TargetEditor)
            app.screen.query_one('#target-url', Input).value = 'https://example.com/new'
            await pilot.press('escape')
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert not any(a == 'screens-save' for a, _ in service.calls)
    asyncio.run(scenario())
