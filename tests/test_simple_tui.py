"""Two-task keyboard acceptance; no real host commands or network."""
import asyncio
from copy import deepcopy

from textual.widgets import Button, DataTable, Static, Tabs, TextArea

from ala.config import Config
from ala.model import CheckResult, Report
from ala.simple_tui import SimpleApp


class FakeService:
    def __init__(self):
        self.config = Config(user='user', autostart=False).to_dict()
        self.calls = []
        self.report = Report([
            CheckResult('base.package.chrome', 'change_required', actual='미설치'),
            CheckResult('gpu', 'not_applicable'),
        ]).to_dict()
        self.jobs = {}
        self.gate = None
        self.fail_status = False

    async def call(self, action, payload=None):
        self.calls.append((action, deepcopy(payload)))
        await asyncio.sleep(0)
        if action == 'config-show':
            return {'config': deepcopy(self.config), 'exists': True}
        if action == 'status':
            snapshot = dict(deepcopy(self.report), config=deepcopy(self.config), runs=deepcopy(list(self.jobs.values())))
            if self.gate is not None:
                await self.gate.wait()
            if self.fail_status:
                raise PermissionError('상태 접근 거부')
            return snapshot
        if action == 'submit':
            assert payload == {'expected_config': self.config}
            self.jobs['test-run'] = {'run_id': 'test-run', 'state': 'queued', 'stages': []}
            return deepcopy(self.jobs['test-run'])
        if action == 'runs-show':
            return deepcopy(self.jobs[payload['run_id']])
        raise AssertionError(action)


def test_keyboard_one_action_applies_current_config_and_prevents_duplicates():
    async def scenario():
        service = FakeService()
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert len(app.query_one('#tabs', Tabs).query('Tab')) == 2
            start = app.query_one('#setup-start', Button)
            assert app.focused is start
            assert not start.disabled and start.region.bottom <= 23
            await pilot.press('enter')
            await pilot.pause()
            assert app._active_job == 'test-run' and start.disabled
            await app.start_setup()
            assert sum(action == 'submit' for action, _ in service.calls) == 1
            assert not any(action in {'config-save', 'plan', 'chrome'} for action, _ in service.calls)
            await pilot.press('q')
        assert service.jobs['test-run']['state'] == 'queued'
    asyncio.run(scenario())


def test_status_is_read_only_and_separates_podman_blocker_from_reboot_wait():
    async def scenario():
        service = FakeService()
        service.report = Report([
            CheckResult('docker.conflict.podman', 'blocked', actual='podman', next_action='컨테이너·볼륨 확인'),
            CheckResult('display.session', 'pending', actual='적용 대기', next_action='재부팅 후 검사'),
        ]).to_dict()
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press('s')
            await pilot.pause()
            assert app.query_one('#pages').current == 'status'
            assert app.focused is app.query_one('#status-table')
            detail = app.query_one('#status-detail', TextArea).text
            assert 'Podman' in detail and '컨테이너·볼륨 확인' in detail
            assert '이전' not in detail
            await pilot.press('down')
            await pilot.pause()
            assert '재부팅 후 검사' in app.query_one('#status-detail', TextArea).text
            assert '조치·확인 필요 1' in str(app.query_one('#status-summary', Static).render())
            assert not any(action in {'submit', 'config-save'} for action, _ in service.calls)
    asyncio.run(scenario())


def test_reconnect_running_job_and_final_report_never_claims_all_applied():
    async def scenario():
        service = FakeService()
        service.jobs['test-run'] = {'run_id': 'test-run', 'state': 'running', 'stages': [{'id': 'base', 'state': 'completed'}]}
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert app._active_job == 'test-run'
            assert app.query_one('#setup-start', Button).disabled
            service.jobs['test-run'].update(state='failed', exit_code=1, report=Report([
                CheckResult('docker.conflict.podman', 'blocked', actual='podman', next_action='Podman 사용 여부 확인'),
                CheckResult('display.policy', 'change_required', actual='설정 전', next_action='다시 간편 설정'),
            ]).to_dict())
            await app.poll_job()
            assert app._active_job is None
            rows = {row['id']: row for row in app._progress_rows}
            assert 'podman' in rows['docker']['detail']
            assert rows['chrome']['state'] == 'not_run'
            assert rows['docker']['state'] not in {'completed', 'kept', 'ok'}
            assert not app.query_one('#setup-start', Button).disabled
    asyncio.run(scenario())


def test_slow_readonly_query_does_not_block_tab_navigation_and_failure_is_unknown():
    async def scenario():
        service = FakeService()
        service.gate = asyncio.Event()
        app = SimpleApp(service=service)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press('s')
            await pilot.pause()
            assert app.query_one('#pages').current == 'status'
            assert app.query_one('#setup-start', Button).disabled
            service.fail_status = True
            service.gate.set()
            await pilot.pause()
            assert '정상으로 판단할 수 없습니다' in str(app.query_one('#status-summary', Static).render())
            assert app.query_one('#setup-start', Button).disabled
            assert not any(action == 'submit' for action, _ in service.calls)
    asyncio.run(scenario())


def test_resizing_and_stale_configuration_guard():
    async def scenario():
        service = FakeService()
        app = SimpleApp(service=service)
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            for size in ((60, 15), (80, 24), (120, 36)):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                assert app.query_one('#small-terminal').display == (size[0] < 80 or size[1] < 24)
                if size[0] >= 80:
                    assert app.query_one('#setup-start').region.bottom < size[1]
            service.config['dev_tools'] = True
            await app.start_setup()
            assert app.query_one('#setup-start', Button).disabled
            assert '구성이 변경' in str(app.query_one('#notice', Static).render())
            assert not any(action == 'submit' for action, _ in service.calls)
    asyncio.run(scenario())


def test_empty_or_failed_check_does_not_enable_setup_or_leave_green_results():
    async def scenario():
        service = FakeService()
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            service.fail_status = True
            await app.load_state()
            assert app.query_one('#status-table', DataTable).row_count == 0
            assert '조회가 실패' in app.query_one('#status-detail', TextArea).text
            assert app.query_one('#setup-start', Button).disabled
            service.fail_status = False
            service.report = Report([]).to_dict()
            await app.load_state()
            assert app.query_one('#setup-start', Button).disabled
            assert all(row['state'] == 'unknown' for row in app._progress_rows)
            assert '검사 결과가 없습니다' in app.query_one('#setup-output', TextArea).text
            await app.start_setup()
            assert not any(action == 'submit' for action, _ in service.calls)
    asyncio.run(scenario())


def test_refresh_cannot_cancel_loading_or_resurrect_a_finished_job():
    async def scenario():
        service = FakeService()
        service.jobs['test-run'] = {'run_id': 'test-run', 'state': 'running', 'stages': []}
        app = SimpleApp(service=service)
        async with app.run_test() as pilot:
            await pilot.pause()
            service.gate = asyncio.Event()
            app.action_refresh()
            await pilot.pause()
            assert app._loading
            app.action_refresh()  # Must not cancel/restart the existing load worker.
            service.jobs['test-run'].update(state='completed', exit_code=0)
            await app.poll_job()
            assert app._active_job is None
            service.gate.set()
            await pilot.pause()
            assert not app._loading and app._active_job is None
            assert not app.query_one('#setup-start', Button).disabled
    asyncio.run(scenario())


def test_setup_progress_is_one_colored_row_per_stage_and_preserves_manual_selection():
    async def scenario():
        service = FakeService()
        service.report = Report([CheckResult('base.package.chrome', 'ok'),
                                 CheckResult('gpu.driver', 'change_required')]).to_dict()
        events = [{'id': name, 'state': 'kept'} for name in ('base', 'update', 'hardware', 'shell', 'ssh')]
        events += [{'id': 'docker', 'state': 'started'}, {'id': 'docker', 'state': 'completed'},
                   {'id': 'gpu', 'state': 'started'}]
        service.jobs['test-run'] = {'run_id': 'test-run', 'state': 'running', 'stages': events}
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            table = app.query_one('#setup-table', DataTable)
            row_ids = [row['id'] for row in app._progress_rows]
            assert len(row_ids) == len(set(row_ids)) == table.row_count
            assert table.get_cell('base', 'state').plain == '✓ 정상 유지'
            assert table.get_cell('base', 'state').style == 'green'
            assert table.get_cell('docker', 'state').plain == '✓ 완료'
            assert table.get_cell('gpu', 'state').plain == '● 진행 중'
            assert 'cyan' in table.get_cell('gpu', 'state').style
            assert table.get_cell('chrome', 'state').plain == '… 실행 대기'
            table.focus()
            table.move_cursor(row=row_ids.index('docker'))
            await pilot.pause()
            service.jobs['test-run']['stages'] += [{'id': 'gpu', 'state': 'completed'},
                                                  {'id': 'display', 'state': 'started'}]
            await app.poll_job()
            assert table.row_count == len(row_ids)
            assert table.cursor_row == row_ids.index('docker')
            assert table.get_cell('gpu', 'state').plain == '✓ 완료'
            assert table.get_cell('display', 'state').plain == '● 진행 중'
            assert 'Docker' in app.query_one('#setup-output', TextArea).text
            service.jobs['test-run'].update(state='pending', report=Report([
                CheckResult('gpu.driver', 'pending', actual='재부팅 대기', next_action='재부팅 후 검사'),
            ]).to_dict())
            await app.poll_job()
            assert table.get_cell('gpu', 'state').plain == '◷ 적용 대기'
            assert table.get_cell('gpu', 'state').style == 'yellow'
    asyncio.run(scenario())


def test_enter_expands_subtasks_and_live_column_uses_explicit_event():
    async def scenario():
        service = FakeService()
        service.report = Report([CheckResult('base.package.git', 'ok'),
                                 CheckResult('time.timezone', 'change_required')]).to_dict()
        service.jobs['test-run'] = {'run_id':'test-run', 'state':'running',
            'stages':[{'id':'base','state':'started'}],
            'substeps':[{'stage':'base','id':'packages','state':'completed'},
                        {'stage':'base','id':'timezone','state':'started'}]}
        app = SimpleApp(service=service)
        async with app.run_test(size=(80,24)) as pilot:
            await pilot.pause()
            table = app.query_one('#setup-table', DataTable)
            assert '시간대' in table.get_cell('base', 'current').plain
            before = table.row_count
            table.focus()
            table.move_cursor(row=0)
            await pilot.press('enter')
            await pilot.pause()
            assert table.row_count > before
            assert table.get_cell('base.packages', 'state').plain == '✓ 완료'
            assert table.get_cell('base.timezone', 'state').plain == '● 진행 중'
            await pilot.press('enter')
            await pilot.pause()
            assert table.row_count == before
            assert not any(a == 'submit' for a,_ in service.calls)
    asyncio.run(scenario())
