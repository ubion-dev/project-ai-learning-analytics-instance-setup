"""SSH keyboard acceptance without commands, network, or real display changes."""
import asyncio
from copy import deepcopy
from rich.cells import cell_len
import pytest

from textual.widgets import Button, Collapsible, DataTable, Input, Select, Static, Tabs, TextArea

from ala.config import Config
from ala.model import CheckResult, Report
from ala.screens_tui import ScreenPanel, TargetEditor, site_label, topology_text
from ala.simple_tui import SimpleApp


async def open_target(pilot, app, target_id='default', *, operations=False, details=False):
    app.query_one('#screens', ScreenPanel).open_target(target_id)
    await pilot.pause()
    assert isinstance(app.screen, TargetEditor)
    if operations:
        app.screen.query_one('#target-operations', Collapsible).collapsed = False
    if details:
        app.screen.query_one('#target-details', Collapsible).collapsed = False
    await pilot.pause()
    return app.screen


class Service:
    def __init__(self):
        self.calls = []
        self.config = Config(user='user', url='https://example.com/display').to_dict()
        self.report = Report([CheckResult('base.package.chrome', 'ok')]).to_dict()
        self.screens = {'output_state': 'ok', 'outputs': [{'connector': 'HDMI-1'}, {'connector': 'DP-1'}],
                        'logical_outputs': [{'id': 'HDMI-1', 'connectors': ['HDMI-1'], 'x': 0, 'y': 0, 'width': 1920, 'height': 1080, 'confirmed': True},
                                            {'id': 'DP-1', 'connectors': ['DP-1'], 'x': 1920, 'y': 0, 'width': 1920, 'height': 1080, 'confirmed': True}],
                        'confirmed_windows': 2,
                        'targets': [self.target('default', 'HDMI-1'), self.target('display-2', 'DP-1')]}
        self.gate = None

    def target(self, key, output):
        return {'id': key, 'url': 'https://example.com/'+key, 'applied_url': 'https://example.com/'+key,
                'output': output, 'layout': 'fullscreen',
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
        if action == 'screens-login-logs':
            return {'log': '로그인·세션 시작 오류 단서', 'exit_code': 0}
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
            if payload['expected_config'] != self.config:
                raise RuntimeError('다른 작업에서 구성이 변경됐습니다.')
            saved = payload['target']
            selected = next(v for v in self.screens['targets'] if v['id'] == saved['id'])
            selected.update(saved, settings={'pending': saved['url'] != selected['applied_url'] or saved['output'] != selected['output'], 'state': 'pending'})
            return {'saved': True, 'config': self.config, 'state': 'pending', 'reason': '설정 적용 대기'}
        if action == 'screens-apply':
            selected = next(v for v in self.screens['targets'] if v['id'] == payload['target'])
            selected['settings'] = {'pending': True, 'state': 'queued', 'operation_id': 'test-apply'}
            return selected['settings'] | {'exit_code': 2}
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


def test_four_tabs_and_opening_screen_management_is_read_only():
    async def scenario():
        service = Service()
        app = SimpleApp(service=service)
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.pause()
            assert len(app.query_one('#tabs', Tabs).query('Tab')) == 4
            assert not any(a.startswith('screens-') for a, _ in service.calls)
            await pilot.press('c')
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert app.query_one('#pages').current == 'screens'
            summary = str(app.query_one('#screens-summary', Static).render())
            assert '관리 대상 2개' in summary and '확인된 창 2개' in summary
            assert not app.screen.query('#screens-table')
            assert not app.screen.query('#screens-detail')
            for action in ('start', 'stop', 'restart', 'reload', 'apply', 'logs', 'retry'):
                assert not app.screen.query('#screens-'+action)
            editor = await open_target(pilot, app)
            assert editor.target_id == 'default'
            assert editor.query_one('#target-operations', Collapsible).collapsed
            assert 'https://example.com/default' in editor.query_one('#target-url', Input).value
            await pilot.press('escape')
            assert not any(a in {'screens-action', 'screens-save', 'submit'} for a, _ in service.calls)
    asyncio.run(scenario())


def test_stop_only_selected_target_and_exit_leaves_other_screen_running():
    async def scenario():
        service = Service()
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            editor = await open_target(pilot, app, 'display-2', operations=True)
            editor.query_one('#screens-stop', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            requests = [p for a, p in service.calls if a == 'screens-action']
            assert requests == [{'action': 'stop', 'target': 'display-2'}]
            assert service.screens['targets'][0]['service']['ActiveState'] == 'active'
            await pilot.press('escape')
            await pilot.press('q')
        assert len([a for a, _ in service.calls if a == 'screens-action']) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(80, 24), (120, 36)])
def test_map_and_modal_primary_actions_remain_inside_viewport(size):
    async def scenario():
        service = Service()
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            assert not app.query_one('#screens', ScreenPanel).query(DataTable)
            for selector in ('#screens-map', '#screens-identify'):
                region = app.query_one(selector).region
                assert region.x >= 0 and region.y >= 0
                assert region.right <= size[0] and region.bottom <= size[1] - 1
            editor = await open_target(pilot, app, operations=True, details=True)
            start = editor.query_one('#screens-start', Button)
            stop = editor.query_one('#screens-stop', Button)
            reload = editor.query_one('#screens-reload', Button)
            start.focus()
            await pilot.press('tab')
            assert stop.has_focus
            assert reload.content_size.width >= cell_len(str(reload.label)) + 2
            for identifier in ('target-save', 'target-cancel'):
                button = editor.query_one('#'+identifier, Button)
                button.focus()
                await pilot.pause()
                assert button.region.right <= size[0] and button.region.bottom <= size[1]
            await pilot.press('escape')
            await pilot.resize_terminal(120, 36)
            await pilot.pause()
            await app.query_one('#screens', ScreenPanel).refresh_state()
            assert app.query_one('#screens-map').region.bottom <= 35
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
            await open_target(pilot, app)
            app.screen.query_one('#target-url', Input).value = 'https://example.com/new'
            await pilot.press('escape')
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert not any(a == 'screens-save' for a, _ in service.calls)
    asyncio.run(scenario())


def test_saved_url_change_requires_selected_target_apply_even_with_identical_labels():
    async def scenario():
        service = Service()
        target = service.screens['targets'][0]
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            await open_target(pilot, app)
            new_url = 'https://example.com/default?revision=2#section'
            app.screen.query_one('#target-url', Input).value = new_url
            app.screen.query_one('#target-save', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            editor = await open_target(pilot, app, details=True)
            detail = editor.query_one('#screens-detail', TextArea)
            assert target['applied_url'] == 'https://example.com/default'
            assert '설정 · 적용 대기' in detail.text and 'revision=2' not in detail.text
            assert not any(a == 'screens-apply' for a, _ in service.calls)
            editor.query_one('#screens-apply', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            assert [p for a, p in service.calls if a == 'screens-apply'] == [
                {'target': 'default', 'expected_config': service.config}]
            status = editor.query_one('#target-result', Static)
            assert '설정 적용 · 접수' in str(status.render())
            target['settings'].update(state='running')
            await app.query_one('#screens', ScreenPanel).refresh_state()
            editor.refresh_status()
            assert '설정 적용 · 진행' in str(status.render())
            target.update(applied_url=new_url)
            target['settings'].update(pending=False, state='completed')
            await app.query_one('#screens', ScreenPanel).refresh_state()
            editor.refresh_status()
            assert '설정 적용 · 완료' in str(status.render())
            assert '설정 · 반영됨' in detail.text
            assert service.screens['targets'][1]['applied_url'] == 'https://example.com/display-2'
    asyncio.run(scenario())


def test_screen_action_help_distinguishes_restart_reload_and_disabled_frontend_retry():
    async def scenario():
        service = Service()
        service.screens['targets'][0]['frontend'] = {'enabled': True, 'manifest_url': 'https://example.com/deployment.json'}
        service.screens['targets'][0]['applied_frontend'] = {'enabled': False, 'manifest_url': None}
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=(80, 24), tooltips=True) as pilot:
            await pilot.pause()
            editor = await open_target(pilot, app, operations=True, details=True)
            help_line = editor.query_one('#target-help', Static)
            for key, wording in (('restart', 'Chrome을 다시 시작'), ('reload', 'Chrome을 유지')):
                button = editor.query_one('#screens-'+key, Button)
                assert wording in str(button.tooltip)
                button.focus()
                await pilot.pause()
                assert wording in str(help_line.render())
            retry = editor.query_one('#screens-retry', Button)
            assert retry.disabled
            assert '프론트 배포 연동이 없습니다' in str(retry.tooltip)
            assert '프론트 배포 연동이 없습니다' in editor.query_one('#screens-detail', TextArea).text
    asyncio.run(scenario())


def test_hdmi_status_and_gnome_error_are_separate_and_editor_keeps_disconnected_selection():
    async def scenario():
        service = Service()
        service.screens.update(
            outputs=[], logical_outputs=[], output_state='pending', reason='GNOME 확장 연결 대기',
            output_next_action='GNOME 재로그인 후 확인하세요.',
            hardware_output_state='ok',
            hardware_outputs=[{'id': 'card0-HDMI-A-1', 'name': 'HDMI-A-1', 'status': 'connected'}],
        )
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert app.query_one('#screens', ScreenPanel).report['hardware_outputs'][0]['status'] == 'connected'
            assert 'GNOME 확장 연결 대기' in str(app.query_one('#screens-map', Static).render())
            editor = await open_target(pilot, app, details=True)
            assert 'GNOME 재로그인 후 확인하세요' in editor.query_one('#screens-detail', TextArea).text
            output = editor.query_one('#target-output', Select)
            assert output.value == 'HDMI-1'
            assert 'HDMI-1' in {value for _, value in output._options}
            assert 'DP-1' not in {value for _, value in output._options}
    asyncio.run(scenario())


def test_save_conflict_refreshes_config_and_explains_retry():
    async def scenario():
        service = Service()
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            panel = app.query_one('#screens', ScreenPanel)
            edited = next(value for value in Config.from_dict(service.config).screen_targets() if value.id == 'default')
            old = deepcopy(service.config)
            service.config['timezone'] = 'UTC'
            await panel.save_target(edited.to_dict(), old)
            detail = panel.messages['default']
            assert '다른 작업에서 구성이 변경됐습니다' in detail
            assert '설정을 다시 열고' in detail
            assert app._config == service.config
            await panel.save_target(edited.to_dict(), app._config)
            assert '설정 저장 · 완료' in panel.messages['default']
    asyncio.run(scenario())



def test_quick_check_opens_current_boot_log_and_starts_only_selected_did():
    async def scenario():
        class QuickService(Service):
            async def call(self, action, payload=None):
                if action == 'screens-action' and payload['action'] in {'start', 'restart'}:
                    self.calls.append((action, deepcopy(payload)))
                    selected = self.screens['targets'][1]
                    pid = selected['service'].get('MainPID', 4320)
                    selected['service'] = {'ActiveState': 'active', 'MainPID': pid + 1}
                    return {'submitted': True, 'exit_code': 2}
                return await super().call(action, payload)

        service = QuickService()
        service.screens['session'] = 'ok'
        service.screens['targets'][1]['service'] = {'ActiveState': 'failed'}
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_one('#tabs', Tabs).active = 'quick-tab'
            await pilot.pause()
            assert ('screens-inspect', {'quick': True}) in service.calls
            assert app.query_one('#quick').report, str(app.query_one('#quick-note', Static).render())
            assert app.query_one('#quick-target', Select).display, str(app.query_one('#quick-note', Static).render())
            assert not any(action == 'screens-action' for action, _ in service.calls)
            app.query_one('#quick-target', Select).value = 'display-2'
            await pilot.pause()
            assert 'Chrome 서비스 시작 실패' in str(app.query_one('#quick-reason', Static).render())
            assert '창 배치 · 확인 대기' in app.query_one('#quick-detail', TextArea).text
            await pilot.click('#quick-logs')
            await pilot.pause()
            assert 'fixed event' in app.query_one('#quick-detail', TextArea).text
            assert ('screens-action', {'target': 'display-2', 'action': 'logs'}) in service.calls
            control = app.query_one('#quick-control', Button)
            assert not control.disabled
            control.focus()
            await pilot.press('enter')
            await pilot.pause()
            assert not any(payload and payload.get('action') == 'start' for _, payload in service.calls)
            assert str(control.label) == '시작 확인'
            await pilot.pause(control.active_effect_duration)
            await pilot.press('enter')
            await pilot.pause()
            assert ('screens-action', {'target': 'display-2', 'action': 'start'}) in service.calls
            assert '서버 점검 통과' in str(app.query_one('#quick-reason', Static).render())
            assert '실제 DID' in str(app.query_one('#quick-next', Static).render())
            assert str(control.label) == '재시작'
            await pilot.pause(control.active_effect_duration)
            control.focus()
            await pilot.press('enter')
            await pilot.pause(control.active_effect_duration)
            await pilot.press('enter')
            await pilot.pause()
            assert ('screens-action', {'target': 'display-2', 'action': 'restart'}) in service.calls
            assert '서버 점검 완료' in str(app.query_one('#quick-note', Static).render())
            assert control.region.bottom <= app.size.height

    asyncio.run(scenario())


def test_quick_check_keeps_gnome_log_button_visible_in_compact_terminal():
    async def scenario():
        service = Service()
        service.screens['session'] = 'pending'
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_one('#tabs', Tabs).active = 'quick-tab'
            await pilot.pause()
            app.query_one('#quick-target', Select).value = 'display-2'
            await pilot.pause()
            button = app.query_one('#quick-logs', Button)
            assert cell_len(button.label.plain) + 2 * button.styles.line_pad <= button.content_region.width
            assert button.variant == 'primary'
            assert app.query_one('#quick-control', Button).disabled
            assert await pilot.click('#quick-logs')
            await pilot.pause()
            assert ('screens-login-logs', {'target': 'display-2'}) in service.calls
            assert '로그인·세션 시작 오류 단서' in app.query_one('#quick-detail', TextArea).text

    asyncio.run(scenario())
