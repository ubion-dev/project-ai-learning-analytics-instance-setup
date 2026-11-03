"""Early context, loading feedback and evidence labels without host access."""
import asyncio
from copy import deepcopy

from textual.widgets import Button, DataTable, Static, TextArea

from ala.model import CheckResult, Report
from ala.simple_tui import SimpleApp
from test_simple_tui import FakeService


def test_slow_first_inspection_shows_context_and_neutral_loading_until_ready():
    async def scenario():
        service = FakeService()
        service.gate = asyncio.Event()
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert {'config-show', 'runs-list', 'status'} <= {action for action, _ in service.calls}
            assert 'user' in str(app.query_one('#config-summary', Static).render())
            assert app._config == service.config and app._setup_report is None
            assert app.query_one('#setup-start', Button).disabled
            query = app.query_one('#query-status', Static)
            assert query.display and '확인 중' in str(query.render())
            detail = app.query_one('#setup-output', TextArea).text
            assert '검사하고 있습니다' in detail and '확인 불가' not in detail
            table = app.query_one('#setup-table', DataTable)
            assert app._progress_rows
            for row in app._progress_rows:
                cell = table.get_cell(row['id'], 'state')
                assert row['state'] == 'unknown'
                assert '검사 중' in cell.plain and 'red' not in str(cell.style)
            service.gate.set()
            await pilot.pause()
            assert not app._loading and not query.display
            assert not app.query_one('#setup-start', Button).disabled
            assert '검사 중' not in table.get_cell('base', 'state').plain
            assert not any(action == 'submit' for action, _ in service.calls)
    asyncio.run(scenario())


def test_early_running_job_can_finish_before_old_full_snapshot_without_resurrection():
    async def scenario():
        service = FakeService()
        service.gate = asyncio.Event()
        service.jobs['test-run'] = {
            'run_id': 'test-run', 'state': 'running',
            'stages': [{'id': 'base', 'state': 'started'}],
        }
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert app._loading and app._setup_report is None
            assert app._active_job == 'test-run'
            assert app._display_job['state'] == 'running'
            assert app.query_one('#setup-start', Button).disabled
            assert app.query_one('#setup-table', DataTable).get_cell('base', 'state').plain == '● 진행 중'
            service.jobs['test-run'].update(state='completed', exit_code=0,
                                            report=deepcopy(service.report))
            await app.poll_job()
            assert app._active_job is None and app._display_job['state'] == 'completed'
            service.gate.set()
            await pilot.pause()
            assert app._setup_report['runs'][0]['state'] == 'running'
            assert app._active_job is None and app._display_job['state'] == 'completed'
            assert not app.query_one('#query-status').display
            assert not any(action == 'submit' for action, _ in service.calls)
    asyncio.run(scenario())


def test_refresh_marks_previous_results_then_failure_removes_old_green_state():
    async def scenario():
        service = FakeService()
        service.report = Report([CheckResult('base.package.git', 'ok')]).to_dict()
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            table = app.query_one('#setup-table', DataTable)
            assert table.get_cell('base', 'state').style == 'green'
            service.gate = asyncio.Event()
            app.action_refresh()
            await pilot.pause()
            query = app.query_one('#query-status', Static)
            assert query.display
            assert '갱신 중' in str(query.render()) and '이전 조회 결과' in str(query.render())
            assert table.get_cell('base', 'state').style == 'green'
            assert app.query_one('#setup-start', Button).disabled
            service.fail_status = True
            service.gate.set()
            await pilot.pause()
            assert not app._loading and not query.display
            assert app._setup_report is None and not app._status_known
            assert app.query_one('#status-table', DataTable).row_count == 0
            assert '조회가 실패' in app.query_one('#status-detail', TextArea).text
            assert all(row['state'] == 'unknown' for row in app._progress_rows)
            assert all('green' not in str(table.get_cell(row['id'], 'state').style)
                       for row in app._progress_rows)
            assert app.query_one('#setup-start', Button).disabled
    asyncio.run(scenario())


def test_missing_substep_record_is_neutral_but_actual_unknown_check_needs_attention():
    async def scenario():
        service = FakeService()
        service.report = Report([CheckResult('base.timezone', 'unknown', actual='조회 실패')]).to_dict()
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            table = app.query_one('#setup-table', DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press('enter')
            await pilot.pause()
            rows = {row['id']: row for row in app._progress_rows}
            missing = table.get_cell('base.chrome', 'state')
            failed_query = table.get_cell('base.timezone', 'state')
            assert rows['base.chrome']['state'] == rows['base.timezone']['state'] == 'unknown'
            assert rows['base.chrome']['evidence'] == 'missing'
            assert rows['base.timezone']['evidence'] == 'check'
            assert missing.plain == '— 세부 기록 없음' and missing.style == 'dim'
            assert failed_query.plain == '? 조회 확인 필요' and failed_query.style == 'yellow'
            assert service.report['results'][0]['state'] == 'unknown'
            assert not any(action == 'submit' for action, _ in service.calls)
    asyncio.run(scenario())


def test_fresh_status_clears_the_old_job_panel_color():
    async def scenario():
        service = FakeService()
        service.jobs['test-run'] = {'run_id': 'test-run', 'state': 'running', 'stages': []}
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            service.jobs['test-run'].update(state='failed', report=service.report)
            await app.poll_job()
            assert app.query_one('#setup-overview').has_class('failed')
            service.jobs.clear()
            service.report = Report([CheckResult('base.package.git', 'ok')]).to_dict()
            await app.load_state()
            assert app._display_job is None
            assert not app.query_one('#setup-overview').classes
            assert '검사 결과' in str(app.query_one('#setup-progress', Static).render())
            assert not any(action == 'submit' for action, _ in service.calls)
    asyncio.run(scenario())


def test_loading_timer_tolerates_the_label_unmounting_during_shutdown():
    async def scenario():
        app = SimpleApp(service=FakeService())
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.query_one('#query-status').remove()
            app.refresh_query_label()
            await pilot.press('q')
        assert not any(action == 'submit' for action, _ in app.service.calls)
    asyncio.run(scenario())
