"""Synthetic update RPC/release responses; no host commands or network."""
import asyncio
from copy import deepcopy
from threading import Event

from textual.widgets import Button, Static, Switch, Tabs, TextArea

from ala import __version__
from ala.simple_tui import SimpleApp
from ala.update_tui import UpdateScreen
from test_simple_tui import FakeService


class UpdateService(FakeService):
    def __init__(self):
        super().__init__()
        self.update = {'state': 'idle', 'installed_version': __version__, 'runtime_version': __version__}
        self.submit_gate = None
        self.fail_update_status = False

    async def call(self, action, payload=None):
        if action not in {'update-status', 'update-submit'}:
            return await super().call(action, payload)
        self.calls.append((action, deepcopy(payload)))
        await asyncio.sleep(0)
        if action == 'update-status':
            if self.fail_update_status:
                raise PermissionError('synthetic denied')
            return deepcopy(self.update)
        self.update = dict(self.update, run_id='update-test', state='running',
                           target_version='99.0.0', before_version=__version__, phase='download')
        if self.submit_gate is not None:
            await self.submit_gate.wait()
        return dict(deepcopy(self.update), started=True, available=True)


def available(prerelease=False):
    return {'available': True, 'current': __version__, 'latest': '99.0.0',
            'tag': 'v99.0.0', 'exit_code': 0}


def test_update_menu_requires_explicit_channel_and_start_then_tracks_completion(monkeypatch):
    channels = []
    def check(prerelease=False):
        channels.append(prerelease)
        return available(prerelease)
    monkeypatch.setattr('ala.update_tui.check_release', check)
    async def scenario():
        service = UpdateService()
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert len(app.query_one('#tabs', Tabs).query('Tab')) == 4
            await pilot.press('u')
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, UpdateScreen)
            assert screen.query_one('#update-prerelease', Switch).value is False
            assert channels == [False]
            assert not any(action == 'update-submit' for action, _ in service.calls)
            assert '새 버전 · 99.0.0' in str(screen.query_one('#update-release', Static).render())
            assert screen.query_one('#update-start', Button).variant == 'primary'
            order = [screen.query_one('#update-'+key).region.y
                     for key in ('release', 'next', 'start', 'versions', 'detail')]
            assert order == sorted(set(order))
            for selector in ('#update-start', '#update-check', '#update-close', '#update-detail', '#update-next'):
                region = screen.query_one(selector).region
                assert 0 <= region.x < region.right <= 80
                assert 0 <= region.y < region.bottom <= 24
            screen.query_one('#update-prerelease', Switch).focus()
            await pilot.press('space')
            await pilot.pause()
            assert channels == [False, True]
            screen.query_one('#update-start', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            assert [(action, payload) for action, payload in service.calls if action == 'update-submit'] == [
                ('update-submit', {'prerelease': True})]
            assert screen.query_one('#update-start', Button).disabled
            assert '업데이트 진행 중' in str(screen.query_one('#update-release', Static).render())
            assert screen.query_one('#update-start', Button).variant == 'default'
            await screen.start_update()
            assert sum(action == 'update-submit' for action, _ in service.calls) == 1
            assert '진행 중' in screen.query_one('#update-detail', TextArea).text
            await pilot.press('escape')
            await pilot.pause()
            assert not isinstance(app.screen, UpdateScreen)
            assert service.update['state'] == 'running'
            service.update.update(state='completed', installed_version='99.0.0', phase='complete', exit_code=0)
            await app.poll_update()
            banner = str(app.query_one('#update-notice', Static).render())
            assert '설치 완료' in banner and '99.0.0' in banner and '재실행 필요' in banner
            assert __version__ in banner
            await pilot.click('#update-notice')
            await pilot.pause()
            assert isinstance(app.screen, UpdateScreen)
            versions = str(app.screen.query_one('#update-versions', Static).render())
            assert '설치 버전 · 99.0.0' in versions and f'현재 실행 버전 · {__version__}' in versions
            assert app.screen.query_one('#update-start', Button).disabled
            assert 'ala를 종료하고 다시 실행' in str(app.screen.query_one('#update-next', Static).render())
    asyncio.run(scenario())


def test_dismissing_modal_during_submit_keeps_app_owned_request(monkeypatch):
    monkeypatch.setattr('ala.update_tui.check_release', available)
    async def scenario():
        service = UpdateService()
        service.submit_gate = asyncio.Event()
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press('u')
            await pilot.pause()
            app.screen.query_one('#update-start', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            assert service.update['state'] == 'running'
            await pilot.press('escape')
            service.submit_gate.set()
            await pilot.pause()
            assert app._update_run_id == 'update-test'
            assert app._update_status['state'] == 'running'
            assert sum(action == 'update-submit' for action, _ in service.calls) == 1
            assert not any(action in {'stop', 'cancel'} for action, _ in service.calls)
    asyncio.run(scenario())


def test_failure_unknown_and_unverified_completion_are_never_success(monkeypatch):
    monkeypatch.setattr('ala.update_tui.check_release', available)
    async def scenario():
        service = UpdateService()
        service.update.update(state='failed', target_version='99.0.0', installed_version='98.0.0',
                              reason='manifest verification failed', next_action='배포 파일을 다시 확인하세요.', exit_code=1)
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press('u')
            await pilot.pause()
            detail = app.screen.query_one('#update-detail', TextArea).text
            assert '업데이트 실패' in detail and 'manifest verification failed' in detail
            assert '배포 파일을 다시 확인' in detail
            assert '업데이트 실패' in str(app.screen.query_one('#update-release', Static).render())
            assert '배포 파일을 다시 확인' in str(app.screen.query_one('#update-next', Static).render())
            assert app.screen.query_one('#update-check', Button).variant == 'primary'
            assert '98.0.0' in str(app.screen.query_one('#update-versions', Static).render())
            service.fail_update_status = True
            await app.screen.refresh_status()
            assert '상태 확인 불가' in app.screen.query_one('#update-detail', TextArea).text
            assert app.screen.query_one('#update-start', Button).disabled
            service.fail_update_status = False
            service.update.update(state='completed', installed_version=__version__, exit_code=0)
            await app.screen.refresh_status()
            assert app._update_status['state'] == 'unknown'
            assert app.screen.query_one('#update-detail', TextArea).text.splitlines()[0] == '업데이트 상태 확인 불가'
            service.update = {'state': []}
            await app.screen.refresh_status()
            assert app._update_status['state'] == 'unknown'
            assert app.screen.query_one('#update-start', Button).disabled
    asyncio.run(scenario())


def test_release_query_failure_is_visible_and_never_means_no_update(monkeypatch):
    failed = lambda prerelease=False: {'available': False, 'exit_code': 1}
    monkeypatch.setattr('ala.update_tui.check_release', failed)
    monkeypatch.setattr('ala.update.check', failed)
    async def scenario():
        app = SimpleApp(service=UpdateService())
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.check_update()
            assert '업데이트 조회 실패' in str(app.query_one('#update-notice', Static).render())
            await pilot.press('u')
            await pilot.pause()
            text = str(app.screen.query_one('#update-release', Static).render())
            assert '조회 실패' in text and '버전이 없습니다' not in text
            assert app.screen.query_one('#update-start', Button).disabled
            assert not any(action == 'update-submit' for action, _ in app.service.calls)
    asyncio.run(scenario())


def test_late_release_result_after_modal_close_does_not_touch_unmounted_widgets(monkeypatch):
    gate = Event()
    def check(prerelease=False):
        gate.wait(timeout=2)
        return available(prerelease)
    monkeypatch.setattr('ala.update_tui.check_release', check)
    async def scenario():
        app = SimpleApp(service=UpdateService())
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                await pilot.press('u')
                await pilot.pause()
                assert app.screen.query_one('#update-start', Button).disabled
                await pilot.press('escape')
                gate.set()
                await pilot.pause()
                assert not isinstance(app.screen, UpdateScreen)
                assert not any(action == 'update-submit' for action, _ in app.service.calls)
        finally:
            gate.set()
    asyncio.run(scenario())


def test_recorded_submit_failure_is_shown_immediately_and_explicit_check_reads_latest(monkeypatch):
    monkeypatch.setattr('ala.update_tui.check_release', available)
    class FailedSubmit(UpdateService):
        async def call(self, action, payload=None):
            if action == 'update-submit':
                self.calls.append((action, deepcopy(payload)))
                self.update = dict(self.update, run_id='failed-test', state='failed', phase='checking',
                                   reason='공개 릴리스 조회 실패', next_action='연결 상태를 확인하세요.',
                                   started=False, exit_code=1)
                return deepcopy(self.update)
            return await super().call(action, payload)
    async def scenario():
        service = FailedSubmit()
        service.update.update(run_id='old-test', state='failed', phase='manifest', reason='이전 실행 실패')
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press('u')
            await pilot.pause()
            screen = app.screen
            await screen.start_update()
            assert app._update_run_id == 'failed-test'
            detail = screen.query_one('#update-detail', TextArea).text
            assert '업데이트 실패' in detail and '공개 릴리스 조회 실패' in detail
            assert '업데이트 조건 확인' in detail and '이전 실행 실패' not in detail
            service.update.update(run_id='new-test', state='running', phase='rechecking', exit_code=2)
            service.calls.clear()
            screen.query_one('#update-check', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            assert ('update-status', {}) in service.calls
            assert app._update_run_id == 'new-test'
            assert '설치 버전·실행 재검사' in screen.query_one('#update-detail', TextArea).text
    asyncio.run(scenario())
