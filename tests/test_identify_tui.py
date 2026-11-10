"""Identification belongs to the graphical session, independently of a DID."""
import asyncio
from copy import deepcopy

import pytest
from rich.cells import cell_len
from textual.widgets import Button, Collapsible, Static

from ala.screen_map import ScreenMap
from ala.screens_tui import ScreenPanel, TargetEditor
from ala.simple_tui import SimpleApp
from test_screens_tui import Service


REASON = '현재 GNOME 세션의 화면 확장은 모니터 식별 기능을 제공하지 않습니다.'
NEXT = '간편 설정에서 화면 확장을 반영한 뒤 GNOME 세션에 재로그인하고 다시 식별하세요.'
UNKNOWN = {'state': 'unknown', 'reason': REASON, 'next_action': NEXT}


class IdentifyService(Service):
    def __init__(self, outcome=None):
        super().__init__()
        self.outcome = deepcopy(UNKNOWN if outcome is None else outcome)
        self.started = asyncio.Event()
        self.release = None

    async def call(self, action, payload=None):
        if action in {'screens-identify', 'screens-swap-retry'}:
            self.calls.append((action, deepcopy(payload)))
            if action == 'screens-swap-retry':
                return {'state': 'queued', 'operation_id': payload['operation_id'],
                        'targets': ['default', 'display-2'], 'reason': '교환 복구 접수'}
            self.started.set()
            if self.release is not None:
                await self.release.wait()
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return deepcopy(self.outcome)
        return await super().call(action, payload)


async def identify(pilot, app):
    app.query_one('#screens-identify', Button).focus()
    await pilot.press('enter')
    await pilot.pause()


def summary(panel):
    return str(panel.query_one('#screens-result', Static).render())


@pytest.mark.parametrize('size', [(80, 24), (120, 36)])
def test_failed_did_and_unknown_identify_have_independent_results_and_visible_next_action(size):
    async def scenario():
        service = IdentifyService()
        service.screens['targets'][0]['service']['ActiveState'] = 'failed'
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            panel = app.query_one(ScreenPanel)
            panel.messages['default'] = '기존 Chrome 로그 결과'
            panel.following['display-2'] = ('apply', 'settings', 'operation_id', 'synthetic-operation')
            panel.launches['display-2'] = ('start', '12')
            before = deepcopy((panel.messages, panel.following, panel.launches, service.screens))
            await identify(pilot, app)
            assert summary(panel).startswith('모니터 식별 · 확인 불가')
            assert 'Chrome 실행 실패' not in summary(panel)
            assert not summary(panel).startswith('default')
            assert panel.session_notice == UNKNOWN
            assert panel.query_one('#screens-result', Static).tooltip == REASON
            next_line = panel.query_one('#screens-next', Static)
            visible_action = str(next_line.render())
            assert '재로그인' in visible_action and '간편 설정' in visible_action
            assert cell_len(visible_action) <= next_line.content_size.width
            assert next_line.tooltip == NEXT
            assert (panel.messages, panel.following, panel.launches, service.screens) == before
            assert '실패 · 로그 확인' in str(panel.query_one(ScreenMap).render())
            assert sum(action == 'screens-identify' for action, _ in service.calls) == 1
            assert not any(action in {'screens-save', 'screens-apply', 'screens-action'} for action, _ in service.calls)

            panel.selected = 'display-2'
            panel.render_report()
            await panel.poll()
            assert summary(panel).startswith('모니터 식별 · 확인 불가')
            panel.open_target('default')
            await pilot.pause()
            assert isinstance(app.screen, TargetEditor)
            assert 'Chrome 실행 실패' in str(app.screen.query_one('#target-result', Static).render())
            assert '모니터 식별' not in str(app.screen.query_one('#target-result', Static).render())
            assert not app.screen.query('#screens-detail')
            app.screen.query_one('#target-details', Collapsible).collapsed = False
            await pilot.pause()
            assert REASON not in app.screen.query_one('#screens-detail').text
            assert panel.session_notice == UNKNOWN
            await pilot.press('escape')
            assert panel.session_notice == UNKNOWN

            panel.activate()
            await pilot.pause()
            assert panel.session_notice is None
            assert 'Chrome 실행 실패' in summary(panel)
            assert '기존 Chrome 로그 결과' in summary(panel)
    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(80, 24), (120, 36)])
@pytest.mark.parametrize('empty', [False, True])
@pytest.mark.parametrize('outcome,expected,next_text', [
    ({'state': 'ok', 'exit_code': 0, 'reason': '식별 표시 요청 완료'}, '완료', '표시 번호'),
    ({'state': 'unknown', 'reason': '합성 식별 조회 실패'}, '확인 불가', '다시 조회'),
    (RuntimeError('합성 식별 연결 실패'), '확인 불가', '다시 조회'),
])
def test_global_success_unknown_and_exception_work_without_a_selected_did(size, empty, outcome, expected, next_text):
    async def scenario():
        service = IdentifyService()
        service.outcome = outcome
        if empty:
            service.screens['targets'] = []
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            panel = app.query_one(ScreenPanel)
            before = deepcopy((panel.messages, panel.following, panel.launches))
            await identify(pilot, app)
            assert summary(panel).startswith('모니터 식별 · '+expected)
            assert next_text in str(panel.query_one('#screens-next', Static).render())
            assert (panel.messages, panel.following, panel.launches) == before
            assert None not in panel.messages
            assert panel.identifying is False and panel.mutating is False
            assert sum(action == 'screens-identify' for action, _ in service.calls) == 1
            assert not any(action in {'screens-save', 'screens-apply', 'screens-action'} for action, _ in service.calls)
    asyncio.run(scenario())


def test_running_identify_keeps_did_status_and_blocks_duplicate_operations():
    async def scenario():
        service = IdentifyService()
        service.release = asyncio.Event()
        service.screens['targets'][0]['service']['ActiveState'] = 'failed'
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            panel = app.query_one(ScreenPanel)
            request = asyncio.create_task(panel.session_action('identify'))
            try:
                await asyncio.wait_for(service.started.wait(), 1)
                assert panel.identifying and not panel.mutating
                assert summary(panel).startswith('모니터 식별 · 진행')
                assert panel.priority(panel.target())[0] == 'Chrome 실행 실패'
                assert not panel.action_state('add', None)[0]
                assert not panel.action_state('edit', panel.target())[0]
                await panel.session_action('identify')
                assert await panel.save_target(service.screens['targets'][0], service.config) is None
                panel.open_target('default')
                await pilot.pause()
                assert 'Chrome 실행 실패' in str(app.screen.query_one('#target-result', Static).render())
                await pilot.press('escape')
            finally:
                service.release.set()
                await asyncio.wait_for(request, 1)
            assert sum(action == 'screens-identify' for action, _ in service.calls) == 1
            assert not any(action in {'screens-save', 'screens-action'} for action, _ in service.calls)
    asyncio.run(scenario())


def test_current_backend_relogin_advice_is_complete_in_80_columns_without_setup_recommendation(monkeypatch):
    from ala import monitors
    monkeypatch.setattr(monitors, 'topology', lambda **_kwargs: {'state': 'ok', 'capabilities': []})
    outcome = monitors.identify_outputs(host=object())

    async def scenario():
        service = IdentifyService(outcome)
        service.screens['targets'][0]['service']['ActiveState'] = 'failed'
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            panel = app.query_one(ScreenPanel)
            panel.messages['default'] = '기존 Chrome 진단 결과'
            await identify(pilot, app)
            next_line = panel.query_one('#screens-next', Static)
            action = str(next_line.render())
            assert action == '다음 · '+outcome['next_action']
            assert cell_len(action) <= next_line.content_size.width
            assert '상태 확인' in action and '재로그인' in action
            assert '간편 설정' not in action
            assert next_line.tooltip == outcome['next_action']
            assert panel.session_notice == outcome
            assert panel.messages == {'default': '기존 Chrome 진단 결과'}
            assert 'Chrome 실행 실패' not in summary(panel)
            assert sum(command == 'screens-identify' for command, _ in service.calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['perform', 'save', 'recover'])
def test_explicit_target_work_clears_session_notice_and_keeps_target_result_visible(operation):
    async def scenario():
        service = IdentifyService()
        if operation == 'recover':
            service.screens['targets'][0]['swap'] = {'state': 'recovery_required', 'operation_id': 'f' * 32}
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            panel = app.query_one(ScreenPanel)
            await identify(pilot, app)
            assert panel.session_notice == UNKNOWN
            original = deepcopy(service.screens['targets'][0])
            if operation == 'perform':
                await panel.perform('logs', target_id='default')
            elif operation == 'save':
                value = {key: deepcopy(original[key]) for key in ('id', 'url', 'output', 'layout', 'autostart', 'frontend')}
                value['url'] = 'https://example.com/changed'
                await panel.save_target(value, deepcopy(service.config))
            else:
                await panel.session_action('swap-recover', target_id='default')
            assert panel.session_notice is None
            assert '모니터 식별' not in summary(panel)
            assert panel.messages['default']
    asyncio.run(scenario())
