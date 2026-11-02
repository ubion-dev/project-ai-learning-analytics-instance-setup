"""Two-task TUI for one-click setup and read-only expected-state checks."""
from __future__ import annotations

import asyncio
import json
import os
from copy import deepcopy

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, ContentSwitcher, DataTable, Footer, Static, Tab, Tabs, TextArea

from . import __version__
from .console import LABELS, PRIORITY, group_name
from .model import redact
from .progress import progress_rows, STATE_LABELS, STATE_STYLES

ACTIVE = {'queued', 'running'}
JOB_LABELS = {'queued': '시작 대기', 'running': '실행 중', 'completed': '완료',
              'pending': '적용 대기', 'failed': '조치 필요', 'interrupted': '중단됨'}


class SetupTable(DataTable):
    class Resized(Message):
        """The table's measured viewport has changed after screen layout."""

    def on_resize(self, event: events.Resize):
        self.post_message(self.Resized())


class DetailArea(TextArea):
    def load_text(self, text: str):
        super().load_text(text)
        # With no cursor highlight, unchanged document geometry need not repaint.
        self.refresh()


def value_text(value):
    value = redact(value)
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def result_detail(rows):
    """Keep problem causes ahead of pending actions, without inventing success."""
    if not rows:
        return '검사 결과가 없습니다. 정상으로 판단할 수 없습니다.'
    issues = [row for row in rows if row.get('state') not in {'ok', 'not_applicable'}]
    if not issues:
        return f'정상 {sum(row.get("state") == "ok" for row in rows)}개 · 해당 없음 {sum(row.get("state") == "not_applicable" for row in rows)}개\n기대 구성과 일치합니다. 실제 화면 표시는 모니터에서 확인하세요.'
    issues.sort(key=lambda row: PRIORITY.get(row.get('state'), 5), reverse=True)
    lines = []
    for row in issues:
        identifier = str(row.get('id', ''))
        state = LABELS.get(row.get('state'), '확인 불가')
        if identifier.startswith('docker.conflict.podman'):
            lines.append('Podman 설치됨 · Docker 관련 설정에 조치 필요')
        else:
            lines.append(f'{group_name(identifier)} · {state}')
        if row.get('next_action'):
            lines.append('다음: ' + value_text(row['next_action']))
        values = '현재: ' + value_text(row.get('actual'))
        if row.get('expected') is not None:
            values += ' · 기대: ' + value_text(row['expected'])
        lines.append(values)
        lines.append('')
    return '\n'.join(lines).rstrip()


class SimpleApp(App[None]):
    """No wizard: the only mutable action is the explicit setup button."""

    TITLE = 'ala · 환경 관리'
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding('q', 'quit', '종료'), Binding('s', 'status', '상태 확인'),
                Binding('g', 'setup', '간편 설정'), Binding('r', 'refresh', '새로고침')]
    CSS = """
    Screen { background: #10151c; color: #e9f0f7; align-horizontal: center; }
    #heading, #tabs, #notice, #update-notice, #small-terminal, #body { width: 100%; max-width: 132; }
    #heading { height: 3; background: #17212d; padding: 1 3; color: #b5e9d9; }
    #small-terminal { display: none; height: auto; max-height: 2; background: #493622; color: #ffe0af; padding: 0 3; }
    #tabs { height: 3; padding: 0 3; }
    #notice, #update-notice { height: auto; max-height: 2; padding: 0 3; color: #c1d2e2; }
    #update-notice { color: #83e3c4; }
    #body { height: 1fr; margin-top: 1; }
    #pages { width: 100%; height: 1fr; padding: 0 3 1 3; }
    #setup, #status { height: 1fr; }
    #setup-toolbar { height: auto; margin-bottom: 1; }
    #setup-context { width: 1fr; height: auto; }
    #config-summary { height: auto; max-height: 3; text-style: bold; }
    #setup-note { height: auto; max-height: 2; margin-top: 1; color: #aabdd0; }
    .actions { height: 3; margin-bottom: 1; }
    #setup-actions { width: 24; margin: 0 0 0 3; }
    Button { height: 3; padding: 0 3; background: #233344; color: #e9f0f7; border: tall #233344; }
    Button.-primary { background: #83e3c4; color: #10251d; border: tall #83e3c4; text-style: bold; }
    Button:focus { border: tall #ccf7e8; }
    Button:disabled { background: #1c2835; border: tall #1c2835; color: #a4b5c6; }
    #setup-start { width: 24; }
    #setup-overview { height: auto; margin-bottom: 1; }
    #setup-progress { height: auto; max-height: 2; margin-bottom: 1; text-style: bold; }
    #setup-summary { height: auto; max-height: 2; }
    DataTable { background: #141d28; border: solid #34475d; border-title-color: #aabdd0; }
    DataTable > .datatable--header { background: #233344; color: #e9f0f7; text-style: bold; }
    DataTable > .datatable--cursor { background: #244859; }
    DataTable:focus { border: solid #83e3c4; }
    #setup-table { height: 1fr; min-height: 5; }
    #setup-output { height: 7; min-height: 5; margin-top: 1; }
    #status-summary { height: auto; max-height: 2; margin-bottom: 1; }
    #status-table { height: 1fr; min-height: 3; }
    #status-detail { height: 9; min-height: 5; margin-top: 1; }
    TextArea { background: #141d28; border: round #34475d; border-title-color: #aabdd0; padding: 0 2; }
    TextArea:focus { border: round #83e3c4; }
    Footer { width: 100%; background: #17212d; color: #b5c8da; }
    Screen.compact #heading { height: 1; padding: 0 2; }
    Screen.compact #tabs { padding: 0 1; }
    Screen.compact #notice, Screen.compact #update-notice { padding: 0 2; }
    Screen.compact #body { margin-top: 0; }
    Screen.compact #pages { padding: 0 2; }
    Screen.compact #config-summary { max-height: 2; }
    Screen.compact #setup-note { margin-top: 0; max-height: 1; }
    Screen.compact #setup-toolbar { margin-bottom: 0; }
    Screen.compact #setup-actions { width: 22; margin-left: 2; }
    Screen.compact #setup-start { width: 22; }
    Screen.compact Button { padding: 0 2; }
    Screen.compact #setup-overview, Screen.compact #setup-progress { margin-bottom: 0; }
    Screen.compact #setup-table { min-height: 4; }
    Screen.compact #setup-output { height: 5; margin-top: 0; }
    Screen.compact #status-summary, Screen.compact .actions { margin-bottom: 0; }
    Screen.compact #status-detail { height: 7; margin-top: 0; }
    Screen.compact TextArea { padding: 0 1; }
    """

    def __init__(self, *, service, **kwargs):
        super().__init__(**kwargs)
        self.service = service
        self._config = None
        self._active_job = None
        self._submitting = False
        self._polling = False
        self._loading = False
        self._status_known = False
        self._groups = {}
        self._setup_report = None
        self._display_job = None
        self._progress_rows = []
        self._last_followed_step = None
        self._expanded_steps = set()
        self._table_layout = None

    def compose(self) -> ComposeResult:
        yield Static(f'ala {__version__} · 서버 환경 관리' + (' · 관리자' if os.geteuid() == 0 else ''), id='heading', markup=False)
        yield Static('화면이 작습니다. 80×24 이상으로 늘려주세요.', id='small-terminal', markup=False)
        yield Tabs(Tab('간편 설정', id='setup-tab'), Tab('상태 확인', id='status-tab'), id='tabs')
        yield Static('현재 구성을 확인하고 있습니다.', id='notice', markup=False)
        yield Static('', id='update-notice', markup=False)
        with Vertical(id='body'):
            with ContentSwitcher(initial='setup', id='pages'):
                with Vertical(id='setup'):
                    with Horizontal(id='setup-toolbar'):
                        with Vertical(id='setup-context'):
                            yield Static('현재 구성을 확인하고 있습니다.', id='config-summary', markup=False)
                            yield Static('기존 설정을 재사용하고 필요한 항목만 보완합니다.', id='setup-note', markup=False)
                        with Horizontal(classes='actions', id='setup-actions'):
                            yield Button('간편 설정 실행', id='setup-start', variant='primary', disabled=True)
                    with Vertical(id='setup-overview'):
                        yield Static('실행하면 SSH·화면을 닫아도 설정 작업은 계속됩니다.', id='setup-progress', markup=False)
                        yield Static('현재 상태를 확인하고 있습니다.', id='setup-summary', markup=False)
                    yield SetupTable(id='setup-table', cursor_type='row', cursor_foreground_priority='renderable')
                    yield DetailArea('', id='setup-output', read_only=True, show_cursor=False, highlight_cursor_line=False)
                with Vertical(id='status'):
                    with Horizontal(classes='actions'):
                        yield Button('상태 다시 확인', id='status-refresh')
                    yield Static('아직 확인하지 않았습니다.', id='status-summary', markup=False)
                    yield DataTable(id='status-table', cursor_type='row')
                    yield DetailArea('', id='status-detail', read_only=True, show_cursor=False, highlight_cursor_line=False)
        yield Footer()

    def on_mount(self):
        self.query_one('#status-table', DataTable).add_columns('기능', '상태')
        self.query_one('#setup-table', DataTable).add_column('설정 항목', key='item', width=28)
        self.query_one('#setup-table', DataTable).add_column('현재 상태', key='state', width=14)
        self.query_one('#setup-table', DataTable).add_column('진행 중인 세부 작업', key='current', width=24)
        self.query_one('#setup-table').border_title = '설정 항목 · Enter로 세부 보기'
        self.query_one('#setup-output').border_title = '선택 항목 · 원인과 다음 조치'
        self.query_one('#status-table').border_title = '기능별 검사 결과'
        self.query_one('#status-detail').border_title = '선택 항목 · 원인과 다음 조치'
        self.render_setup_table()
        self._resize_warning()
        self.set_interval(1.5, self.poll_job)
        self.run_worker(self.load_state(), group='load', exclusive=True, exit_on_error=False)
        if not self.is_headless:
            self.run_worker(self.check_update(), group='updates', exit_on_error=False)

    def on_resize(self, event: events.Resize):
        if self.is_mounted:
            self._resize_warning(event.size)

    def _resize_warning(self, size=None):
        size = size or self.size
        self.query_one('#small-terminal').display = size.width < 80 or size.height < 24
        self.screen.set_class(size.height < 32, 'compact')
        self.call_after_refresh(self._fit_setup_table)

    def _fit_setup_table(self):
        """Use measured space without horizontal scrolling; keep the inspected row."""
        table = self.query_one('#setup-table', DataTable)
        if not table.size.width:
            return
        padding = 2 if self.size.width >= 100 else 1
        available = max(40, table.content_size.width - 2 - 6 * padding)
        current = max(18, min(32, available // 3))
        widths = (max(8, available - 14 - current), 14, current)
        row_height = 2 if self.size.height >= 40 else 1
        layout = (widths, padding, row_height)
        if layout == self._table_layout:
            return
        self._table_layout = layout
        selected = (self._progress_rows[table.cursor_row]['id']
                    if self._progress_rows and table.cursor_row < len(self._progress_rows) else None)
        table.clear(columns=True)
        table.cell_padding = padding
        for label, key, width in zip(('설정 항목', '현재 상태', '진행 중인 세부 작업'),
                                     ('item', 'state', 'current'), widths):
            table.add_column(label, key=key, width=width)
        self._progress_rows = []
        self.render_setup_table()
        ids = [row['id'] for row in self._progress_rows]
        if selected in ids:
            table.move_cursor(row=ids.index(selected), animate=False)

    @on(SetupTable.Resized)
    def setup_table_resized(self):
        self.call_after_refresh(self._fit_setup_table)

    def notice(self, message):
        notice = self.query_one('#notice', Static)
        notice.update(Text(message))
        notice.display = bool(message)

    async def api(self, action, payload=None):
        try:
            return await self.service.call(action, payload)
        except Exception as exc:
            self.notice('확인 실패: ' + value_text(str(exc)))
            return None

    def update_button(self):
        self.query_one('#setup-actions').display = not bool(self._active_job)
        self.query_one('#setup-note').display = not bool(self._active_job)
        self.query_one('#setup-start', Button).disabled = bool(
            self._loading or self._submitting or self._active_job or not self._status_known
            or not self._config or not self._config.get('user'))

    async def load_state(self):
        if self._loading or self._submitting:
            return
        tracked_at_start = self._active_job
        self._loading = True
        self.update_button()
        self.query_one('#status-refresh', Button).disabled = True
        try:
            config, report = await asyncio.gather(self.api('config-show'), self.api('status'))
            if config is None:
                self._config = None
                self.query_one('#config-summary', Static).update('구성을 확인하지 못했습니다. 상태 다시 확인으로 재시도하세요.')
            if config is not None:
                self._config = deepcopy(config['config'])
                user = self._config.get('user') or '일반 계정 지정 필요'
                mode = self._config.get('url') if self._config.get('autostart') and self._config.get('url') else '바탕화면만 표시'
                self.query_one('#config-summary', Static).update(Text(f'계정: {user} · 표시: {mode}'))
            self._status_known = bool(report and (report.get('results') or report.get('preparation', {}).get('results')))
            if report is None:
                self._setup_report = None
                if not self._active_job:
                    self._display_job = None
                    self.render_setup_table()
                self._groups = {}
                self.query_one('#status-table', DataTable).clear()
                self.query_one('#status-summary', Static).update('상태 조회 실패 · 정상으로 판단할 수 없습니다.')
                self.query_one('#status-detail', TextArea).load_text('현재 조회가 실패했습니다. 상태 다시 확인으로 재시도하세요.')
            if report is not None:
                self._setup_report = deepcopy(report)
                self.render_status(report)
                active = next((job for job in report.get('runs', []) if job.get('state') in ACTIVE), None)
                if active and not self._active_job and tracked_at_start is None:
                    self._active_job = active['run_id']
                    self.render_job(active)
                if config is None:
                    pass
                elif not self._config.get('user'):
                    self.notice('기존 일반 사용자로 접속해 ala를 실행하세요. root 직접 접속 시에는 화면 계정을 지정해야 합니다.')
                elif self._active_job:
                    self.notice('기존 설정 작업에 연결했습니다. 중복 실행하지 않습니다.')
                else:
                    self.notice('')
                if not self._active_job and tracked_at_start is None:
                    self._display_job = None
                    self.render_setup_table()
                    self.query_one('#setup-progress', Static).update('현재 설정 상태입니다. 필요한 항목만 적용합니다.')
        finally:
            self._loading = False
            self.query_one('#status-refresh', Button).disabled = False
            self.update_button()
            if self.query_one('#tabs', Tabs).active == 'setup-tab' and not self.query_one('#setup-start', Button).disabled:
                self.query_one('#setup-start', Button).focus()

    @on(Tabs.TabActivated, '#tabs')
    def activate_tab(self, event):
        section = 'setup' if event.tab.id == 'setup-tab' else 'status'
        self.query_one('#pages', ContentSwitcher).current = section
        target = '#setup-start' if section == 'setup' else '#status-table'
        self.call_after_refresh(self.query_one(target).focus)
        if section == 'setup':
            self.call_after_refresh(self._fit_setup_table)

    @on(Button.Pressed, '#status-refresh')
    def refresh_pressed(self):
        self.action_refresh()

    def action_status(self):
        self.query_one('#tabs', Tabs).active = 'status-tab'

    def action_setup(self):
        self.query_one('#tabs', Tabs).active = 'setup-tab'

    def action_refresh(self):
        if self._loading or self._submitting:
            return
        self.run_worker(self.load_state(), group='load', exclusive=True, exit_on_error=False)

    @on(Button.Pressed, '#setup-start')
    def setup_pressed(self):
        self.run_worker(self.start_setup(), group='submit', exit_on_error=False)

    async def start_setup(self):
        if self._submitting or self._active_job or self._loading or not self._status_known or not self._config or not self._config.get('user'):
            return
        self._submitting = True
        self.update_button()
        try:
            current = await self.api('config-show')
            if current is None:
                return
            if current.get('config') != self._config:
                self.notice('구성이 변경됐습니다. 상태를 다시 확인한 뒤 실행하세요.')
                self._status_known = False
                return
            job = await self.api('submit', {'expected_config': deepcopy(self._config)})
            if job is not None:
                self._active_job = job['run_id'] if job.get('state') in ACTIVE else None
                self.render_job(job)
                self.notice('설정을 시작했습니다. 이 화면을 종료해도 작업은 계속됩니다.')
        finally:
            self._submitting = False
            self.update_button()

    async def poll_job(self):
        if self._polling or not self._active_job:
            return
        self._polling = True
        try:
            job = await self.api('runs-show', {'run_id': self._active_job})
            if job is None:
                return
            self.render_job(job)
            if job.get('query_warning'):
                self.notice(value_text(job['query_warning']) + ' · 진행 중단으로 단정하지 않습니다.')
                return
            if job.get('state') not in ACTIVE:
                self._active_job = None
                if job.get('report'):
                    self.render_status(job['report'])
                self.notice('작업 결과와 다음 조치를 확인하세요. 정상 항목은 유지합니다.')
        finally:
            self._polling = False
            self.update_button()

    def render_job(self, job):
        state = JOB_LABELS.get(job.get('state'), '확인 필요')
        self.query_one('#setup-progress', Static).update(Text(f"설정 {state} · 실행 ID {job.get('run_id', '-') }"))
        if not self._display_job or self._display_job.get('run_id') != job.get('run_id'):
            self._last_followed_step = None
        self._display_job = deepcopy(job)
        self.render_setup_table()

    def render_setup_table(self):
        parents = progress_rows(report=self._setup_report, job=self._display_job)
        rows = []
        for parent in parents:
            row = dict(parent)
            children = row.get('children', [])
            expanded = row['id'] in self._expanded_steps
            row['label'] = ('▾ ' if expanded else '▸ ') + row['label'] if children else row['label']
            rows.append(row)
            if expanded:
                for child in children:
                    rows.append(dict(child, label='  ' + child['label'], parent_id=parent['id']))
        table = self.query_one('#setup-table', DataTable)
        old_ids = [row['id'] for row in self._progress_rows]
        ids = [row['id'] for row in rows]
        selected = old_ids[table.cursor_row] if old_ids and table.cursor_row < len(old_ids) else None
        self._progress_rows = rows
        if old_ids != ids:
            table.clear()
            for row in rows:
                height = 1 if row.get('parent_id') else (2 if self.size.height >= 40 else 1)
                table.add_row(Text(row['label']), self.progress_cell(row), Text(row.get('current_step', '')), key=row['id'], height=height)
            if selected in ids:
                table.move_cursor(row=ids.index(selected), animate=False)
        else:
            for row in rows:
                table.update_cell(row['id'], 'item', Text(row['label']))
                table.update_cell(row['id'], 'state', self.progress_cell(row))
                table.update_cell(row['id'], 'current', Text(row.get('current_step', '')))
        counts = {}
        for row in parents:
            label = STATE_LABELS.get(row['state'], row['state'])
            counts[label] = counts.get(label, 0) + 1
        summary = Text()
        for state in dict.fromkeys(row['state'] for row in parents):
            label = STATE_LABELS.get(state, state)
            if summary:
                summary.append('   ')
            summary.append(f'{label} {counts[label]}', style=STATE_STYLES.get(state, ''))
        self.query_one('#setup-summary', Static).update(summary)
        running = next((row['id'] for row in rows if row.get('parent_id') and row['state'] == 'running'), None)
        running = running or next((row['id'] for row in rows if row['state'] in {'running', 'started'}), None)
        inspecting = self.focused is table or self.focused is self.query_one('#setup-output')
        if running and running != self._last_followed_step:
            if not inspecting:
                table.move_cursor(row=ids.index(running), animate=False)
            self._last_followed_step = running
        if rows:
            self.show_setup_row(min(table.cursor_row, len(rows) - 1))

    @on(DataTable.RowSelected, '#setup-table')
    def toggle_step_details(self, event):
        identifier = event.row_key.value
        row = next((row for row in self._progress_rows if row['id'] == identifier), None)
        if row and row.get('children'):
            if identifier in self._expanded_steps:
                self._expanded_steps.remove(identifier)
            else:
                self._expanded_steps.add(identifier)
            self.render_setup_table()

    @staticmethod
    def progress_cell(row):
        return Text(STATE_LABELS.get(row['state'], row['state']), style=STATE_STYLES.get(row['state'], 'yellow'))

    def show_setup_row(self, index):
        if not 0 <= index < len(self._progress_rows):
            return
        row = self._progress_rows[index]
        detail = row['label'] + '\n' + row.get('detail', '')
        output = self.query_one('#setup-output', TextArea)
        if output.text != detail:
            output.load_text(detail)

    @on(DataTable.RowHighlighted, '#setup-table')
    def setup_row_highlighted(self, event):
        keys = [row['id'] for row in self._progress_rows]
        if event.row_key.value in keys:
            self.show_setup_row(keys.index(event.row_key.value))

    def render_status(self, report):
        rows = report.get('results', []) + report.get('preparation', {}).get('results', [])
        groups = {}
        for row in rows:
            groups.setdefault(group_name(row.get('id')), []).append(row)
        self._groups = groups
        table = self.query_one('#status-table', DataTable)
        table.clear()
        for index, (name, items) in enumerate(groups.items()):
            state = max((row.get('state', 'unknown') for row in items), key=lambda value: PRIORITY.get(value, 5))
            style = 'green' if state == 'ok' else 'dim' if state == 'not_applicable' else 'yellow' if state in {'change_required', 'pending'} else 'red'
            table.add_row(Text(name), Text(LABELS.get(state, '확인 불가'), style=style), key=str(index))
        if rows:
            issues = sum(row.get('state') in {'error', 'blocked', 'unknown'} for row in rows)
            pending = sum(row.get('state') == 'pending' for row in rows)
            changes = sum(row.get('state') == 'change_required' for row in rows)
            self.query_one('#status-summary', Static).update(Text(f'조치·확인 필요 {issues} · 설정 필요 {changes} · 적용 대기 {pending}'))
            self.show_group(0)
        else:
            self.query_one('#status-summary', Static).update('검사 결과가 없습니다. 정상으로 판단할 수 없습니다.')
            self.query_one('#status-detail', TextArea).load_text('상태 다시 확인으로 재검사하세요.')

    @on(DataTable.RowHighlighted, '#status-table')
    def row_highlighted(self, event):
        try:
            self.show_group(int(event.row_key.value))
        except (TypeError, ValueError):
            pass

    def show_group(self, index):
        names = list(self._groups)
        if 0 <= index < len(names):
            name = names[index]
            self.query_one('#status-detail', TextArea).load_text(name + '\n' + result_detail(self._groups[name]))

    async def check_update(self):
        from .update import check
        try:
            result = await asyncio.to_thread(check)
        except Exception:
            return
        if result.get('available'):
            self.query_one('#update-notice', Static).update(Text(f"새 버전 {result['latest']} · ala update로 갱신"))
