"""Managed single-Chrome keyboard controls using synthetic RPC only."""
import asyncio
from copy import deepcopy
from xml.etree import ElementTree

import pytest
from rich.cells import cell_len
from textual.widgets import Button, Static, Tabs, TextArea

from ala.chrome_tui import ChromeScreen, navigation_details, safe_logs
from ala.model import CheckResult, Report
from ala.simple_tui import SimpleApp as ManagementApp
from test_simple_tui import FakeService


class SimpleApp(ManagementApp):
    """Keep legacy modal component regression; main UX is tested in test_screens_tui.

    The approved three-tab UX replaced the c shortcut. This explicit component
    harness retains the original control/diagnostic safety assertions.
    """
    def action_chrome(self):
        if len(self.screen_stack) == 1:
            self.push_screen(ChromeScreen(self))


class ChromeService(FakeService):
    def __init__(self):
        super().__init__()
        self.config.update(url='https://example.com/display', autostart=True)
        preparation = Report([CheckResult(key, 'ok') for key in
                              ('display.configuration', 'chrome.unit', 'chrome.autostart', 'display.initial_setup')]).to_dict()
        preparation['scope'] = 'configuration'
        self.report = Report([CheckResult('chrome.runtime', 'unknown')]).to_dict()
        self.report['configuration_report'] = preparation
        self.chrome = {'state': 'pending', 'exit_code': 0,
                       'service': {'ActiveState': 'inactive'}, 'health': None}
        self.action_gate = None
        self.status_gate = None
        self.status_failure = False

    async def call(self, action, payload=None):
        if action != 'chrome':
            return await super().call(action, payload)
        self.calls.append((action, deepcopy(payload)))
        await asyncio.sleep(0)
        command = payload['action']
        if command == 'status':
            if self.status_gate:
                await self.status_gate.wait()
            if self.status_failure:
                raise PermissionError('synthetic denied')
            return deepcopy(self.chrome)
        if command == 'logs':
            return {'exit_code': 0, 'log': 'normal event\nAuthorization: synthetic-test-value\n{"token":"synthetic-test-value"}\n'}
        if self.action_gate:
            await self.action_gate.wait()
        if command == 'start':
            self.chrome.update(state='pending', service={'ActiveState': 'activating'},
                               health={'state': 'waiting_site', 'reason': '사이트 응답 대기'})
            return {'state': 'starting', 'exit_code': 0, 'reason': '비동기 요청 접수'}
        if command == 'stop':
            self.chrome.update(state='pending', service={'ActiveState': 'inactive'}, health=None)
            return {'state': 'stopped', 'exit_code': 0, 'reason': '현재 실행 중지. 다음 로그인에는 자동 시작'}
        if command == 'restart':
            self.chrome.update(state='running', service={'ActiveState': 'active'},
                               health={'state': 'healthy', 'reason': '주 문서 응답 확인'})
            return {'state': 'starting', 'exit_code': 0, 'reason': '재시작 요청 접수'}
        raise AssertionError(command)


def mutations(service):
    return [payload['action'] for action, payload in service.calls
            if action == 'chrome' and payload['action'] in {'start', 'stop', 'restart'}]


def test_button_and_keyboard_open_single_display_controls_without_mutating_or_extra_tabs():
    async def scenario():
        service = ChromeService()
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert len(app.query_one('#tabs', Tabs).query('Tab')) == 3
            button = app.query_one('#chrome-open', Button)
            assert cell_len(button.label.plain) <= button.content_region.width - 2 * button.styles.line_pad
            await pilot.click('#chrome-open')
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ChromeScreen)
            assert '등록 준비 완료' in str(screen.query_one('#chrome-registration', Static).render())
            assert '현재 중지됨' in str(screen.query_one('#chrome-runtime', Static).render())
            assert '다음 그래픽 로그인에는 자동 시작' in screen.query_one('#chrome-detail', TextArea).text
            assert screen.query_one('#chrome-start', Button).disabled is False
            assert service.report['results'][0]['state'] == 'unknown'
            assert app._setup_report['scope'] == 'configuration'
            assert not any(row['id'] == 'chrome.runtime' for row in app._setup_report['results'])
            assert any(row['id'] == 'chrome.runtime' for group in app._groups.values() for row in group)
            assert not mutations(service)
            for size in ((120, 36), (80, 24)):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                for selector in ('#chrome-start', '#chrome-stop', '#chrome-restart', '#chrome-status', '#chrome-logs', '#chrome-close', '#chrome-detail'):
                    widget = screen.query_one(selector)
                    region = widget.region
                    assert 0 <= region.x < region.right <= size[0]
                    assert 0 <= region.y < region.bottom <= size[1]
                    if isinstance(widget, Button):
                        assert cell_len(widget.label.plain) <= widget.content_region.width - 2 * widget.styles.line_pad
            await pilot.press('escape')
            await pilot.pause()
            assert not app.query_one('#setup-note').display
            await pilot.press('c')
            await pilot.pause()
            assert isinstance(app.screen, ChromeScreen) and not mutations(service)
    asyncio.run(scenario())


def test_start_is_request_only_and_restart_stop_require_explicit_actions():
    async def scenario():
        service = ChromeService()
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press('c')
            await pilot.pause()
            screen = app.screen
            screen.query_one('#chrome-start', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            assert mutations(service) == ['start']
            assert '시작 중' in str(screen.query_one('#chrome-runtime', Static).render())
            assert '주 문서·HTTP 연결 확인' not in str(screen.query_one('#chrome-health', Static).render())
            assert '별도 조회' in screen.query_one('#chrome-detail', TextArea).text
            service.chrome.update(state='running', service={'ActiveState': 'active'}, health={'state': 'healthy'})
            await screen.refresh_status()
            screen.query_one('#chrome-restart', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            assert mutations(service) == ['start', 'restart']
            assert '주 문서·HTTP 연결 확인' in str(screen.query_one('#chrome-health', Static).render())
            screen.query_one('#chrome-stop', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            assert mutations(service) == ['start', 'restart', 'stop']
            assert '현재 중지됨' in str(screen.query_one('#chrome-runtime', Static).render())
            assert service.config['autostart'] is True
    asyncio.run(scenario())


@pytest.mark.parametrize('blocker', ('setup', 'update', 'registration', 'url', 'unknown'))
def test_missing_prerequisites_or_active_work_prevent_mutation_but_keep_readonly_controls(blocker):
    async def scenario():
        service = ChromeService()
        if blocker == 'registration':
            service.report['configuration_report']['results'] = []
        if blocker == 'url':
            service.config['url'] = None
        if blocker == 'unknown':
            service.status_failure = True
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            if blocker == 'setup':
                app._active_job = 'synthetic-run'
            if blocker == 'update':
                app._update_status = {'state': 'running'}
            await pilot.press('c')
            await pilot.pause()
            screen = app.screen
            assert screen.query_one('#chrome-start', Button).disabled
            assert screen.query_one('#chrome-restart', Button).disabled
            assert not screen.query_one('#chrome-status', Button).disabled
            assert not screen.query_one('#chrome-logs', Button).disabled
            if blocker == 'unknown':
                assert '확인 불가' in str(screen.query_one('#chrome-runtime', Static).render())
                assert '조회 실패' in screen.query_one('#chrome-detail', TextArea).text
            assert not mutations(service)
    asyncio.run(scenario())


def test_pending_control_survives_modal_close_and_duplicate_request_is_rejected():
    async def scenario():
        service = ChromeService()
        service.action_gate = asyncio.Event()
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press('c')
            await pilot.pause()
            screen = app.screen
            screen.query_one('#chrome-start', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            assert app._chrome_mutating
            await screen.perform_action('start')
            assert mutations(service) == ['start']
            await pilot.press('escape')
            await pilot.pause()
            assert app.query_one('#setup-start', Button).disabled
            service.action_gate.set()
            await pilot.pause()
            assert not app._chrome_mutating and service.chrome['service']['ActiveState'] == 'activating'
            assert mutations(service) == ['start']
    asyncio.run(scenario())


def test_logs_are_explicit_readonly_and_credential_lines_are_not_displayed():
    async def scenario():
        service = ChromeService()
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press('c')
            await pilot.pause()
            app.screen.query_one('#chrome-logs', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            text = app.screen.query_one('#chrome-detail', TextArea).text
            assert 'normal event' in text and '인증정보' in text
            assert 'synthetic-test-value' not in text
            assert not mutations(service)
    asyncio.run(scenario())


def test_late_readonly_status_after_close_never_updates_a_closed_modal():
    async def scenario():
        service = ChromeService()
        service.status_gate = asyncio.Event()
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.query_one('#chrome-start', Button).disabled
            await pilot.press('escape')
            service.status_gate.set()
            await pilot.pause()
            assert not isinstance(app.screen, ChromeScreen)
            assert not mutations(service)
            await pilot.press('c')
            await pilot.pause()
            assert isinstance(app.screen, ChromeScreen)
            assert not app.screen.query_one('#chrome-start', Button).disabled
    asyncio.run(scenario())


def test_configuration_only_finished_job_refreshes_full_runtime_without_losing_job_result():
    async def scenario():
        service = ChromeService()
        completed = deepcopy(service.report['configuration_report'])
        service.report['configuration_report']['results'] = []
        service.jobs['test-run'] = {'run_id': 'test-run', 'state': 'running', 'stages': []}
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            service.report['configuration_report'] = completed
            service.jobs['test-run'].update(state='completed', exit_code=0,
                                            report=completed)
            await app.poll_job()
            await pilot.pause()
            assert app._display_job['state'] == 'completed'
            assert app._setup_report['scope'] == 'configuration'
            assert any(row['id'] == 'chrome.runtime' for row in app._full_report['results'])
            assert any(row['id'] == 'chrome.runtime' for group in app._groups.values() for row in group)
            assert sum(action == 'status' for action, _ in service.calls) >= 2
            assert not mutations(service)
            await pilot.press('c')
            await pilot.pause()
            assert not app.screen.query_one('#chrome-start', Button).disabled
    asyncio.run(scenario())


def test_compact_setup_renders_account_and_url_prefix_beside_both_controls():
    async def scenario():
        service = ChromeService()
        service.config['url'] = 'https://example.com/display/' + 'long-path/' * 6
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            svg = ElementTree.fromstring(app.export_screenshot())
            visible = ''.join(''.join(node.itertext()) for node in svg.iterfind('.//{*}text'))
            visible = ''.join(visible.split())
            assert '설정대상·user' in visible
            assert '화면·https://example.com' in visible
            assert '간편설정실행' in visible and '화면관리' in visible
    asyncio.run(scenario())


def test_log_bound_discards_partial_credential_line_including_an_entire_long_single_line():
    synthetic = 'x' * 84015
    for suffix in ('', '\nnormal event'):
        visible = safe_logs('Authorization: ' + synthetic + suffix)
        assert 'x' * 32 not in visible
        assert '잘린 로그 첫 행 숨김' in visible
        if suffix:
            assert 'normal event' in visible


def test_log_bound_keeps_a_complete_first_line_when_cutoff_immediately_follows_newline():
    complete_tail = 'normal complete event\n' + 'n' * (65536 - len('normal complete event\n'))
    visible = safe_logs('previous omitted event\n' + complete_tail)
    assert visible == complete_tail
    assert '잘린 로그' not in visible


def test_complete_credential_lines_are_masked_without_hiding_other_complete_events():
    visible = safe_logs('normal before\nAuthorization: synthetic-only\nnormal after\n')
    assert 'synthetic-only' not in visible and '인증정보가 포함된 로그 행 숨김' in visible
    assert 'normal before' in visible and 'normal after' in visible
    assert '잘린 로그' not in visible


def test_ordinary_complete_log_lines_are_preserved():
    assert safe_logs('first normal event\nsecond normal event\n') == 'first normal event\nsecond normal event'


def navigation_sample():
    return {
        'display_requests': 2, 'recovery_requests': 1, 'browser_navigations': 5,
        'last_command': {'kind': 'display', 'at': '2026-01-01T09:00:00+09:00'},
        'last_browser_navigation': {'initiator': 'script', 'at': '2026-01-01T00:00:01Z'},
        'last_error': {'kind': 'aborted', 'at': '2026-01-01T00:00:02+00:00'},
    }


def test_navigation_details_distinguish_ala_requests_from_browser_observations_without_cause_claim():
    value = navigation_sample()
    value['url'] = 'https://example.com/not-for-diagnostics'
    value['last_browser_navigation']['stack'] = 'synthetic stack must stay hidden'
    text = '\n'.join(navigation_details({'supervisor_version': '0.1.0rc19', 'navigation': value}, running=True))
    assert '실행 중 감시 버전 · 0.1.0rc19' in text
    assert 'ala 표시 요청 2회 / 복구 안내 요청 1회 / 브라우저 문서 이동 5회' in text
    assert '마지막 ala 명령 · 표시 주소 요청 · 2026-01-01 00:00:00 UTC' in text
    assert '마지막 브라우저 이동 · 스크립트 관련 관측 · 2026-01-01 00:00:01 UTC' in text
    assert '마지막 탐색 오류 · 탐색 취소 · 2026-01-01 00:00:02 UTC' in text
    assert '사용자 입력·브라우저 동작도 포함하며 원인 확정이 아닙니다.' in text
    assert 'not-for-diagnostics' not in text and 'synthetic stack' not in text


@pytest.mark.parametrize('invalid', (True, False, -1, 1.0, '1', None, 2**70, [], {}))
def test_navigation_counter_invalid_values_are_not_displayed_as_zero(invalid):
    value = navigation_sample()
    value['display_requests'] = invalid
    text = '\n'.join(navigation_details({'navigation': value}, running=True))
    assert 'ala 표시 요청 확인 불가 / 복구 안내 요청 1회' in text
    assert 'ala 표시 요청 0회' not in text


def test_navigation_missing_invalid_or_explicitly_empty_history_remains_distinct():
    legacy = '\n'.join(navigation_details({'state': 'healthy'}, running=True))
    assert '실행 중 감시에 탐색 기록 없음' in legacy
    assert '명시적 재시작 후 진단' in legacy and '0회' not in legacy
    malformed = '\n'.join(navigation_details({'navigation': []}, running=True))
    assert '기록 형식 확인 필요' in malformed and '0회' not in malformed
    empty = {'display_requests': 0, 'recovery_requests': 0, 'browser_navigations': 0,
             'last_command': None, 'last_browser_navigation': None, 'last_error': None}
    text = '\n'.join(navigation_details({'navigation': empty}, running=True))
    assert 'ala 표시 요청 0회 / 복구 안내 요청 0회 / 브라우저 문서 이동 0회' in text
    assert text.count('기록 없음') == 3
    assert '실행 중 감시에 탐색 기록 없음' not in text


def test_navigation_categories_and_times_never_render_untrusted_diagnostic_fields():
    value = navigation_sample()
    for key in ('last_command', 'last_browser_navigation', 'last_error'):
        value[key] = {'kind': 'untrusted-kind', 'initiator': 'untrusted-initiator',
                      'at': 'untrusted-time', 'url': 'https://example.com/private'}
    text = '\n'.join(navigation_details({'supervisor_version': 'untrusted-version', 'navigation': value}))
    assert '감시 기록 버전 · 확인 불가' in text
    assert text.count('범주 확인 불가 · 시각 확인 불가') == 3
    assert 'untrusted' not in text and 'https://' not in text
    value['last_command'] = {'kind': 'display', 'at': '2026-01-01T00:00:00'}
    assert '표시 주소 요청 · 시각 확인 불가' in '\n'.join(navigation_details({'navigation': value}))


def test_navigation_diagnostics_in_modal_refresh_readonly_without_restarting_legacy_supervisor():
    async def scenario():
        service = ChromeService()
        service.chrome.update(state='running', service={'ActiveState': 'active'},
                              health={'state': 'healthy', 'supervisor_version': '0.1.0rc19',
                                      'navigation': navigation_sample()})
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press('c')
            await pilot.pause()
            screen = app.screen
            text = screen.query_one('#chrome-detail', TextArea).text
            assert 'ala 표시 요청 2회' in text and '브라우저 문서 이동 5회' in text
            assert '실행 중 감시 버전 · 0.1.0rc19' in text
            assert not mutations(service)
            service.chrome['health'].pop('navigation')
            await screen.refresh_status()
            text = screen.query_one('#chrome-detail', TextArea).text
            assert '실행 중 감시에 탐색 기록 없음' in text and '0회' not in text
            assert '명시적 재시작 후 진단' in text
            assert '주 문서·HTTP 연결 확인' in str(screen.query_one('#chrome-health', Static).render())
            assert not screen.query_one('#chrome-restart', Button).disabled
            assert not mutations(service)
    asyncio.run(scenario())
