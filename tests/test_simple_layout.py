"""Responsive keyboard layout acceptance using only the synthetic service."""
import asyncio
from copy import deepcopy
from xml.etree import ElementTree

import pytest
from rich.cells import cell_len
from textual.widgets import Button, DataTable, Footer, TextArea

from ala.model import CheckResult, Report
from ala.simple_tui import SimpleApp
from test_simple_tui import FakeService


SIZES = ((80, 24), (120, 36), (160, 48))


def service_for(state='initial'):
    service = FakeService()
    service.config.update(autostart=True, url='https://example.com/display')
    service.report = Report([
        CheckResult('base.package.git', 'ok'),
        CheckResult('docker.conflict.podman', 'blocked', actual='podman',
                    next_action='기존 컨테이너와 볼륨 사용 여부를 확인하세요.'),
        CheckResult('gpu.detection', 'ok'),
        CheckResult('gpu.driver', 'pending', actual='재부팅 대기',
                    next_action='재부팅 후 드라이버 동작을 다시 확인하세요.'),
        CheckResult('display.session', 'pending', actual='로그인 대기'),
    ]).to_dict()
    if state in {'running', 'completed'}:
        service.jobs['test-run'] = {
            'run_id': 'test-run', 'state': 'running',
            'stages': [{'id': stage, 'state': 'kept'}
                       for stage in ('base', 'update', 'hardware', 'shell', 'ssh')]
                      + [{'id': 'docker', 'state': 'completed'},
                         {'id': 'gpu', 'state': 'started'}],
            'substeps': [{'stage': 'gpu', 'id': 'repository', 'state': 'started'}],
        }
    if state == 'error':
        service.fail_status = True
    return service


def assert_inside_screen(widget, size):
    region = widget.region
    assert region.width > 0 and region.height > 0, (widget.id, region)
    assert region.x >= 0 and region.y >= 0, (widget.id, region)
    assert region.right <= size[0] and region.bottom <= size[1], (widget.id, region, size)


def assert_nonoverlapping(widgets):
    for index, widget in enumerate(widgets):
        for other in widgets[index + 1:]:
            assert not widget.region.overlaps(other.region), (
                widget.id, widget.region, other.id, other.region)


def assert_setup_geometry(app, size):
    selectors = ['#config-summary', '#setup-progress', '#setup-summary',
                 '#setup-table', '#setup-output']
    if app.query_one('#setup-actions').display:
        selectors.extend(['#setup-start', '#setup-note'])
    widgets = [app.query_one(selector) for selector in selectors]
    footer = app.query_one(Footer)
    for widget in [*widgets, footer]:
        assert_inside_screen(widget, size)
    assert_nonoverlapping([*widgets, footer])
    table = app.query_one('#setup-table', DataTable)
    detail = app.query_one('#setup-output', TextArea)
    assert table.region.height >= table.header_height + 2
    assert detail.content_region.height >= 1
    assert table.region.bottom <= detail.region.y < footer.region.y
    if size[1] >= 36:
        overview_end = max(app.query_one(selector).region.bottom
                           for selector in ('#setup-progress', '#setup-summary'))
        assert table.region.y - overview_end >= 1
        assert detail.region.y - table.region.bottom >= 1
        if app.query_one('#setup-actions').display:
            toolbar_end = max(app.query_one(selector).region.bottom
                              for selector in ('#config-summary', '#setup-note', '#setup-start'))
            assert app.query_one('#setup-progress').region.y - toolbar_end >= 1


@pytest.mark.parametrize('size', SIZES)
@pytest.mark.parametrize('state', ('initial', 'running', 'completed', 'error'))
def test_setup_regions_remain_readable_and_separate_for_representative_states(size, state):
    async def scenario():
        service = service_for(state)
        app = SimpleApp(service=service)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            if state == 'completed':
                service.jobs['test-run'].update(state='pending', exit_code=2,
                                                report=deepcopy(service.report))
                await app.poll_job()
                await pilot.pause()
            assert_setup_geometry(app, size)
            if state == 'initial':
                button = app.query_one('#setup-start', Button)
                caption_width = max(cell_len(line) for line in button.label.plain.splitlines())
                assert caption_width <= button.content_region.width - 2 * button.styles.line_pad
            assert not any(action == 'submit' for action, _ in service.calls)
    asyncio.run(scenario())


def test_wider_terminals_give_long_subtasks_space_without_horizontal_scrolling():
    async def scenario():
        app = SimpleApp(service=service_for('running'))
        async with app.run_test(size=SIZES[0]) as pilot:
            await pilot.pause()
            table = app.query_one('#setup-table', DataTable)
            table.focus()
            table.move_cursor(row=next(index for index, row in enumerate(app._progress_rows)
                                       if row['id'] == 'gpu'))
            await pilot.press('enter')
            await pilot.pause()
            widths = {}
            for size in SIZES:
                await pilot.resize_terminal(*size)
                await pilot.pause()
                widths[size[0]] = {column.key.value: column.width for column in table.columns.values()}
                assert_setup_geometry(app, size)
                if size[0] >= 120:
                    assert table.max_scroll_x == 0
            assert widths[120]['item'] > widths[80]['item']
            assert widths[160]['current'] > widths[80]['current']
            longest_child = max(cell_len(row['label']) for row in app._progress_rows if row.get('parent_id'))
            assert widths[160]['item'] >= longest_child
    asyncio.run(scenario())


def test_keyboard_can_reach_last_expanded_subtask_and_resize_keeps_selection():
    async def scenario():
        app = SimpleApp(service=service_for())
        async with app.run_test(size=SIZES[0]) as pilot:
            await pilot.pause()
            table = app.query_one('#setup-table', DataTable)
            table.focus()
            await pilot.press('ctrl+home', 'enter', 'ctrl+end', 'enter')
            await pilot.pause()
            last_parent = app._progress_rows[table.cursor_row]['id']
            assert last_parent == 'chrome'
            children = next(row['children'] for row in app._progress_rows if row['id'] == last_parent)
            await pilot.press(*(['down'] * len(children)))
            await pilot.pause()
            selected = app._progress_rows[table.cursor_row]['id']
            assert selected == children[-1]['id']
            assert table.cursor_row == table.row_count - 1
            assert table.scroll_y > 0
            assert table.scroll_y >= table.max_scroll_y - 1
            assert children[-1]['label'] in app.query_one('#setup-output', TextArea).text
            for size in (SIZES[2], SIZES[1], SIZES[0]):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                assert app._progress_rows[table.cursor_row]['id'] == selected
                assert app.focused is table
                assert table.scroll_y >= table.max_scroll_y - 1
                assert_setup_geometry(app, size)
            assert not any(action == 'submit' for action, _ in app.service.calls)
    asyncio.run(scenario())


@pytest.mark.parametrize('size', SIZES)
def test_status_summary_table_details_and_footer_stay_separate(size):
    async def scenario():
        app = SimpleApp(service=service_for())
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await pilot.press('s')
            await pilot.pause()
            widgets = [app.query_one(selector) for selector in
                       ('#status-refresh', '#status-summary', '#status-table', '#status-detail')]
            footer = app.query_one(Footer)
            for widget in [*widgets, footer]:
                assert_inside_screen(widget, size)
            assert_nonoverlapping([*widgets, footer])
            button, summary, table, detail = widgets
            assert table.region.height >= table.header_height + 2
            assert detail.content_region.height >= 2
            assert table.region.bottom <= detail.region.y < footer.region.y
            if size[1] >= 36:
                assert table.region.y - max(button.region.bottom, summary.region.bottom) >= 1
                assert detail.region.y - table.region.bottom >= 1
            assert app.focused is table
            await pilot.press('down')
            await pilot.pause()
            assert '컨테이너' in app.query_one('#status-detail', TextArea).text
            assert not any(action == 'submit' for action, _ in app.service.calls)
    asyncio.run(scenario())


def test_detail_replacement_repaints_visible_text_when_document_geometry_is_unchanged():
    def rendered_text(app):
        svg = ElementTree.fromstring(app.export_screenshot())
        text = ''.join(''.join(node.itertext()) for node in svg.iterfind('.//{*}text'))
        return ''.join(text.split())

    async def scenario():
        app = SimpleApp(service=service_for())
        async with app.run_test(size=SIZES[0]) as pilot:
            await pilot.pause()
            detail = app.query_one('#setup-output', TextArea)
            before, after = 'OLD-DETAIL-ALPHA', 'NEW-DETAIL-BRAVO'
            detail.load_text(before)
            await pilot.pause()
            assert before in rendered_text(app)
            detail.load_text(after)
            await pilot.pause()
            visible = rendered_text(app)
            assert after in visible and before not in visible
    asyncio.run(scenario())
