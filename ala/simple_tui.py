"""Setup, expected-state checks and independent per-screen management."""
from __future__ import annotations

import asyncio
import json
import os
from time import monotonic
from copy import deepcopy

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, ContentSwitcher, DataTable, Footer, Static, Tab, Tabs, TextArea

from . import __version__
from .console import LABELS, PRIORITY, group_name
from .model import redact
from .progress import progress_rows, STATE_LABELS, STATE_STYLES
from .screens_tui import ScreenPanel
from .quick_tui import QuickPanel

ACTIVE = {'queued', 'running'}
JOB_LABELS = {'queued': '시작 대기', 'running': '실행 중', 'completed': '완료',
              'pending': '적용 대기', 'failed': '조치 필요', 'interrupted': '중단됨'}


class PodmanConfirmation(ModalScreen[bool]):
    BINDINGS = [('escape', 'cancel', '취소')]
    CSS = """
    PodmanConfirmation { align: center middle; background: #000000 65%; }
    #podman-dialog { width: 90%; max-width: 90; height: 85%; padding: 1 2; background: #211f29; border: round #cbc2ef; }
    #podman-title { height: auto; }
    #podman-details { height: 1fr; margin: 1 0; }
    #podman-buttons { height: 3; }
    #podman-buttons Button { width: 1fr; margin-right: 1; padding: 0; }
    """

    def __init__(self, plan):
        super().__init__()
        self.plan = plan

    def compose(self):
        ready = self.plan.get('state') == 'ready'
        with Vertical(id='podman-dialog'):
            yield Static('미사용 Podman 제거 확인' if ready else 'Podman 확인 필요', id='podman-title')
            if ready:
                users = ', '.join(str(a['user']) for a in self.plan.get('accounts', []))
                text = ('조회 계정 · ' + users +
                        '\n컨테이너·볼륨·이미지·pod와 사용자 정의 네트워크 없음' +
                        '\n\n제거 예정 패키지\n' + '\n'.join(self.plan['packages']) +
                        '\n\n승인하면 이 목록의 패키지를 제거하고 간편 설정과 Docker 설치를 진행합니다.' +
                        '\n점검 중에는 다른 터미널에서 Podman 작업을 시작하지 마세요.')
            else:
                text = self.plan.get('reason', '사용 여부를 확인하지 못했습니다.')
            yield TextArea(text, read_only=True, id='podman-details')
            with Horizontal(id='podman-buttons'):
                yield Button('취소', id='podman-cancel')
                yield Button('제거 후 설정 계속', id='podman-confirm', variant='primary', disabled=not ready)

    def on_mount(self):
        self.query_one('#podman-cancel', Button).focus()

    @on(Button.Pressed, '#podman-cancel')
    def action_cancel(self):
        self.dismiss(False)

    @on(Button.Pressed, '#podman-confirm')
    def confirm(self):
        self.dismiss(True)


class DisplayInitializationConfirmation(ModalScreen[bool]):
    BINDINGS = [('escape', 'cancel', '취소')]
    CSS = """
    DisplayInitializationConfirmation { align: center middle; background: #000000 65%; }
    #initialization-dialog { width: 90%; max-width: 90; height: 80%; padding: 1 2; background: #211f29; border: round #cbc2ef; }
    #initialization-details { height: 1fr; margin: 1 0; }
    #initialization-buttons { height: 3; }
    #initialization-buttons Button { width: 1fr; margin-right: 1; padding: 0; }
    """

    def __init__(self, plan):
        super().__init__()
        self.plan = plan

    def compose(self):
        allowed = self.plan.get('state') in {'ready', 'pending'}
        lines = [self.plan.get('reason') or '연결된 모든 논리 화면에 미설정 DID를 준비합니다.']
        for target in self.plan.get('targets', []):
            lines.append(f"{target.get('id', 'DID')} · {target.get('output', '출력 확인 대기')} · 로그인 후 자동 시작 켜짐")
        for change in self.plan.get('changes', []):
            lines.append(value_text(change))
        if self.plan.get('state') == 'pending':
            lines.append('현재 출력 목록을 확정할 수 없습니다. 다음 정상 GNOME 세션에서 확인한 모든 논리 화면을 준비합니다.')
        lines += ['기존 사이트가 있는 장비에는 적용하지 않습니다.',
                  '준비되면 일반 사용자 Chrome으로 설정 안내를 표시합니다. 재부팅은 자동 수행하지 않습니다.']
        with Vertical(id='initialization-dialog'):
            yield Static('모니터별 안내 화면 준비 확인')
            yield TextArea('\n'.join(value_text(line) for line in lines), id='initialization-details', read_only=True)
            with Horizontal(id='initialization-buttons'):
                yield Button('취소', id='initialization-cancel')
                yield Button('안내 준비 후 설정', id='initialization-confirm', variant='primary', disabled=not allowed)

    def on_mount(self):
        self.query_one('#initialization-cancel', Button).focus()

    @on(Button.Pressed, '#initialization-cancel')
    def action_cancel(self):
        self.dismiss(False)

    @on(Button.Pressed, '#initialization-confirm')
    def confirm(self):
        self.dismiss(True)


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


def first_issue(rows):
    return max(
        (row for row in rows if row.get('state') not in {'ok', 'not_applicable'}),
        key=lambda row: PRIORITY.get(row.get('state'), PRIORITY['unknown']),
        default=None,
    )


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
                Binding('g', 'setup', '간편 설정'), Binding('r', 'refresh', '새로고침'),
                Binding('u', 'update', '업데이트'), Binding('c', 'chrome', '화면 관리')]
    CSS = """
    Screen { background: #17161d; color: #e9f0f7; align-horizontal: center; }
    #heading, #tabs, #notice, #update-notice, #query-status, #small-terminal, #body { width: 100%; max-width: 132; }
    #heading { height: 3; background: #211f29; padding: 1 3; color: #b5e9d9; }
    #small-terminal { display: none; height: auto; max-height: 2; background: #493622; color: #ffe0af; padding: 0 3; }
    #tabs { height: 3; padding: 0 3; }
    #tabs Underline > .underline--bar { color: #cbc2ef; background: #50475f; }
    #notice, #update-notice { height: auto; max-height: 2; padding: 0 3; color: #c1d2e2; }
    #update-notice { color: #b5e9d9; }
    #query-status { display: none; height: 1; padding: 0 3; color: #8ed4ed; }
    #body { height: 1fr; margin-top: 1; }
    #pages { width: 100%; height: 1fr; padding: 0 3 1 3; }
    #setup, #status, #screens { height: 1fr; }
    #setup-toolbar { height: auto; margin-bottom: 1; padding: 0 2; }
    #setup-context { width: 1fr; height: auto; }
    #config-summary { height: auto; max-height: 3; text-style: bold; text-wrap: nowrap; text-overflow: ellipsis; }
    #setup-bash-assist, #setup-initialize-displays { height: 1; border: none; padding: 0; width: 100%; }
    .actions { height: 3; margin-bottom: 1; }
    #setup-actions { width: 24; margin: 0 0 0 3; }
    Button { height: 3; padding: 0 3; background: #383346; color: #e9f0f7; border: round #605673; }
    Button.-primary { background: #b5e9d9; color: #10251d; border: tall #b5e9d9; text-style: bold; }
    Button:focus { border: round #f3e8ff; }
    Button:disabled { background: #292831; border: tall #292831; color: #aaa4b4; }
    #setup-start { width: 24; }
    #chrome-open { width: 16; height: 3; margin-left: 1; padding: 0 1; }
    #screens-actions { height: 3; }
    #screens-actions Button { padding: 0; min-width: 12; width: 1fr; margin-right: 1; }
    #screens-unassigned, #target-operations, #target-details { background: #211f29; border-top: solid #50475f; }
    #screens-unassigned CollapsibleTitle, #target-editor CollapsibleTitle { color: #cbc2ef; }
    #screens-unassigned CollapsibleTitle:focus, #target-editor CollapsibleTitle:focus { background: #514661; color: #f3e8ff; text-style: bold; }
    #screens-unassigned-list, #target-choices { background: #211f29; border: round #50475f; }
    #screens-unassigned-list:focus, #target-choices:focus { border: round #e4dcff; }
    #screens-unassigned-list > .option-list--option-highlighted, #target-choices > .option-list--option-highlighted { background: #514661; color: #f3e8ff; text-style: bold; }
    #screens-map-scroll, #target-fields, #screens-unassigned-list, #target-choices { scrollbar-color: #a99bc5; scrollbar-color-hover: #cbc2ef; scrollbar-color-active: #e4dcff; scrollbar-background: #211f29; scrollbar-background-hover: #2a2634; scrollbar-background-active: #2a2634; }
    #quick-actions Button { padding: 0; margin-right: 1; }
    #quick-actions Button:last-of-type { margin-right: 0; }
    #setup-overview, #status-overview { height: auto; margin-bottom: 1; padding: 0 2; background: #2a2634; border-left: solid #a99bc5; }
    #setup-overview.running { background: #2c2639; border-left: solid #cbc2ef; }
    #setup-overview.pending { background: #2b2518; border-left: solid #f1d5a3; }
    #setup-overview.failed, #setup-overview.interrupted { background: #321d25; border-left: solid #f3b8c5; }
    #setup-overview.completed { background: #162b25; border-left: solid #b5e9d9; }
    #setup-progress, #status-summary { height: auto; max-height: 2; text-style: bold; color: #b5e9d9; }
    #setup-next, #status-next { height: auto; max-height: 2; }
    #setup-summary { height: auto; max-height: 2; padding: 0 2; }
    DataTable { background: #201e27; border: solid #50475f; border-title-color: #aabdd0; }
    DataTable > .datatable--header { background: #383346; color: #e9f0f7; text-style: bold; }
    DataTable > .datatable--cursor { background: #514661; }
    DataTable:focus { border: solid #b5e9d9; }
    #setup-table { height: 1fr; min-height: 5; background: #24212c; }
    #setup-output { height: 7; min-height: 5; margin-top: 1; }
    #status-table { height: 1fr; min-height: 3; }
    #status-detail { height: 9; min-height: 5; margin-top: 1; }
    TextArea { background: #1b1922; border: round #52677a; border-title-color: #c0d1e0; padding: 0 2; }
    TextArea:focus { border: round #b5e9d9; }
    Footer { width: 100%; background: #211f29; color: #b5c8da; }
    Screen.compact #heading { height: 1; padding: 0 2; }
    Screen.compact #tabs { padding: 0 1; }
    Screen.compact #notice, Screen.compact #update-notice { padding: 0 2; }
    Screen.compact #query-status { padding: 0 2; }
    Screen.compact #body { margin-top: 0; }
    Screen.compact #pages { padding: 0 2; }
    Screen.compact #config-summary { max-height: 2; }
    Screen.compact #setup-toolbar { margin-bottom: 0; padding: 0 1; }
    Screen.compact #setup-actions { width: 22; margin-left: 0; }
    Screen.compact #setup-start { width: 22; }
    Screen.compact #chrome-open { width: 15; margin-left: 0; }
    Screen.compact Button { padding: 0 2; }
    Screen.compact #setup-overview, Screen.compact #status-overview { margin-bottom: 0; padding: 0 1; }
    Screen.compact #setup-summary { padding: 0 1; }
    Screen.compact #setup-table { min-height: 4; }
    Screen.compact #setup-output { height: 5; margin-top: 0; }
    Screen.compact .actions { margin-bottom: 0; }
    Screen.compact #status-detail { height: 7; margin-top: 0; }
    Screen.compact TextArea { padding: 0 1; }
    Screen.compact #screens-summary { height: 1; margin-bottom: 0; }
    Screen.compact #screens-help { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    Screen.compact #screens-actions { height: 3; }
    Screen.compact #quick-actions Button:focus { background: #514661; color: #f3e8ff; text-style: bold; }
    Screen.compact #quick-actions Button { height: 2; border: none; padding: 0; }
    Screen.compact #quick-logs { width: 21; }
    """

    def __init__(self, *, service, initial_section='setup', **kwargs):
        super().__init__(**kwargs)
        self.service = service
        self.initial_section = initial_section
        self._config = None
        self._active_job = None
        self._submitting = False
        self._polling = False
        self._loading = False
        self._status_known = False
        self._groups = {}
        self._selected_status_group = None
        self._setup_report = None
        self._full_report = None
        self._display_job = None
        self._progress_rows = []
        self._last_followed_step = None
        self._expanded_steps = set()
        self._table_layout = None
        self._has_checked = False
        self._query_started = 0.0
        self._update_status = None
        self._update_run_id = None
        self._update_release = None
        self._update_polling = False
        self._update_lock = asyncio.Lock()
        self._chrome_mutating = False
        self._initialization_option_loaded = False
        self._config_exists = True
        self._initialization_status = {}
        self._initialization_polling = False

    def compose(self) -> ComposeResult:
        yield Static(f'ala {__version__} · 서버 환경 관리' + (' · 관리자' if os.geteuid() == 0 else ''), id='heading', markup=False)
        yield Static('화면이 작습니다. 80×24 이상으로 늘려주세요.', id='small-terminal', markup=False)
        yield Tabs(Tab('간편 설정', id='setup-tab'), Tab('상태 확인', id='status-tab'), Tab('화면 관리', id='screens-tab'), Tab('DID 빠른 점검', id='quick-tab'), id='tabs')
        yield Static('현재 구성을 확인하고 있습니다.', id='notice', markup=False)
        yield Static('', id='update-notice', markup=False)
        yield Static('', id='query-status', markup=False)
        with Vertical(id='body'):
            with ContentSwitcher(initial='setup', id='pages'):
                with Vertical(id='setup'):
                    with Vertical(id='setup-overview'):
                        yield Static('환경 상태 확인 중', id='setup-progress', markup=False)
                        yield Static('다음 · 조회가 끝날 때까지 기다리세요.', id='setup-next', markup=False)
                    with Horizontal(id='setup-toolbar'):
                        with Vertical(id='setup-context'):
                            yield Static('현재 구성을 확인하고 있습니다.', id='config-summary', markup=False)
                            option = Checkbox('Bash 자동 제안·구문 강조 설치', id='setup-bash-assist', disabled=True)
                            option.tooltip = '이번 설정에서 일반 운영 계정에 설치합니다. 다음 대화형 SSH 로그인부터 적용됩니다.'
                            yield option
                            initial = Checkbox('모니터별 안내 화면 준비', id='setup-initialize-displays', disabled=True)
                            initial.tooltip = '미설정 장비에서만 연결된 모든 논리 화면에 안내 DID를 준비합니다. 실행 전 변경 내용을 확인합니다.'
                            yield initial
                        with Horizontal(classes='actions', id='setup-actions'):
                            yield Button('간편 설정 실행', id='setup-start', variant='primary', disabled=True)
                        yield Button('화면 관리', id='chrome-open')
                    yield Static('현재 상태를 확인하고 있습니다.', id='setup-summary', markup=False)
                    yield SetupTable(id='setup-table', cursor_type='row', cursor_foreground_priority='renderable')
                    yield DetailArea('', id='setup-output', read_only=True, show_cursor=False, highlight_cursor_line=False)
                with Vertical(id='status'):
                    with Vertical(id='status-overview'):
                        yield Static('상태 조회 대기', id='status-summary', markup=False)
                        yield Static('다음 · 상태를 다시 확인하세요.', id='status-next', markup=False)
                    with Horizontal(classes='actions'):
                        yield Button('상태 다시 확인', id='status-refresh')
                    yield DataTable(id='status-table', cursor_type='row')
                    yield DetailArea('', id='status-detail', read_only=True, show_cursor=False, highlight_cursor_line=False)
                yield ScreenPanel(self, id='screens')
                yield QuickPanel(self, id='quick')
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
        self.set_interval(3, self.poll_initialization)
        self.set_interval(0.2, self.refresh_query_label)
        self.set_interval(1.5, self.poll_update)
        self.run_worker(self.poll_update(), group='update-status', exit_on_error=False)
        self.run_worker(self.load_state(), group='load', exclusive=True, exit_on_error=False)
        if self.initial_section == 'screens':
            self.query_one('#tabs', Tabs).active = 'screens-tab'
        if not self.is_headless:
            self.run_worker(self.check_update(), group='updates', exit_on_error=False)

    def on_resize(self, event: events.Resize):
        if self.is_mounted:
            self._resize_warning(event.size)

    def _resize_warning(self, size=None):
        if not self.is_running:
            return
        size = size or self.size
        self.query_one('#small-terminal').display = size.width < 80 or size.height < 24
        self.screen.set_class(size.height < 32, 'compact')
        self.update_button()
        self.call_after_refresh(self._fit_setup_table)

    def refresh_query_label(self):
        if not self.is_running:
            return
        # A queued timer may run while the screen's children are unmounting.
        labels = self.query('#query-status')
        if not labels:
            return
        label = labels.first(Static)
        label.display = self._loading
        if self._loading:
            mode = '갱신 중 · 아래는 이전 조회 결과' if self._setup_report is not None else '환경 상태 확인 중'
            label.update(Text(f'{mode} · {monotonic() - self._query_started:.1f}초'))

    def _fit_setup_table(self):
        """Use measured space without horizontal scrolling; keep the inspected row."""
        if not self.is_running:
            return
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
        if not self.is_running:
            return
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
        if not self.is_running:
            return
        self.query_one('#setup-actions').display = not bool(self._active_job)
        disabled = bool(
            self._loading or self._submitting or self._active_job or not self._status_known
            or not self._config or not self._config.get('user')
            or self._chrome_mutating or (self._update_status or {}).get('state') in ACTIVE)
        button = self.query_one('#setup-start', Button)
        button.disabled = disabled
        self.query_one('#setup-bash-assist', Checkbox).disabled = disabled
        initialization = self.query_one('#setup-initialize-displays', Checkbox)
        eligible = self.can_initialize_displays()
        if not eligible:
            initialization.value = False
        initialization.disabled = disabled or not eligible
        self.render_setup_header()

    def can_initialize_displays(self):
        config = self._config or {}
        screens = config.get('screens') or [config]
        return bool(config.get('display', True) and not any(
            screen.get('url') or (screen.get('frontend') or {}).get('enabled') for screen in screens))

    def render_setup_header(self):
        job = self._display_job or {}
        state = job.get('state')
        style = ''
        recommend_setup = False
        initialization = self._initialization_status or {}
        if not self._active_job and initialization.get('state') in {'pending', 'blocked'}:
            heading = '초기 화면 준비 · ' + ('대기' if initialization['state'] == 'pending' else '확인 필요')
            action = initialization.get('next_action') or initialization.get('reason') or '초기 화면 준비 상태를 확인하세요.'
            style = '#f1d5a3'
        elif self._active_job or (state in JOB_LABELS and state not in ACTIVE):
            heading = '설정 작업 · ' + JOB_LABELS.get(state, '진행 중')
            style = STATE_STYLES.get(state, '#cbc2ef' if state in ACTIVE else '#f1d5a3')
            if state in ACTIVE:
                action = '항목별 진행을 확인하세요. SSH를 닫아도 작업은 계속됩니다.'
            else:
                result = job.get('report') or {}
                issue = first_issue(result.get('results', []))
                action = ((issue or {}).get('next_action') or job.get('reason')
                          or '항목별 결과를 확인하세요.')
        elif self._submitting:
            heading, action = '설정 요청 중', '작업 접수를 기다리세요.'
        elif self._loading:
            heading, action = '환경 상태 확인 중', '조회가 끝날 때까지 기다리세요.'
        elif not self._config or not self._status_known:
            heading, action = '설정 실행 전 확인 필요', '상태를 다시 확인하세요.'
            style = '#f1d5a3'
        elif not self._config.get('user'):
            heading, action = '일반 운영 계정이 필요합니다', '일반 사용자로 접속하거나 화면 계정을 지정하세요.'
            style = '#f1d5a3'
        elif self._chrome_mutating or (self._update_status or {}).get('state') in ACTIVE:
            heading, action = '다른 작업 진행 중', '현재 작업이 끝난 뒤 간편 설정을 실행하세요.'
        else:
            issue = first_issue((self._setup_report or {}).get('results', []))
            if issue:
                kind = issue.get('state', 'unknown')
                heading = f"{group_name(issue.get('id'))} · {LABELS.get(kind, '확인 불가')}"
                style = STATE_STYLES.get(kind, '#f1d5a3')
                recommend_setup = kind == 'change_required'
                action = issue.get('next_action') or (
                    '간편 설정을 실행해 필요한 항목을 적용하세요.' if recommend_setup
                    else '아래 항목에서 원인과 조치를 확인하세요.')
            else:
                heading, action = '간편 설정 실행 가능', '대상과 설치 옵션을 확인한 뒤 실행하세요.'
                style = '#b5e9d9'
                recommend_setup = True
        button = self.query_one('#setup-start', Button)
        button.variant = 'primary' if recommend_setup and not button.disabled else 'default'
        self.query_one('#setup-progress', Static).update(Text(heading, style=style))
        self.query_one('#setup-next', Static).update('다음 · '+value_text(action))

    async def load_state(self):
        if not self.is_running or self._loading or self._submitting:
            return
        tracked_at_start = self._active_job
        self._loading = True
        self._query_started = monotonic()
        self.refresh_query_label()
        self.update_button()
        self.query_one('#status-refresh', Button).disabled = True
        self.set_status_header('장비 상태 조회 중', '조회가 끝날 때까지 기다리세요.')
        # Read the cheap context independently of the full host inspection.
        config_task = asyncio.create_task(self.api('config-show'))
        runs_task = asyncio.create_task(self.api('runs-list'))
        report_task = asyncio.create_task(self.api('status'))
        attached_early = False
        try:
            config = await config_task
            if not self.is_running:
                return
            if config is None:
                self._config = None
                self.query_one('#config-summary', Static).update('구성을 확인하지 못했습니다. 상태 다시 확인으로 재시도하세요.')
            if config is not None:
                self._config = deepcopy(config['config'])
                self._config_exists = config.get('exists', True)
                self._initialization_status = config.get('display_initialization') or {}
                if not self._initialization_option_loaded:
                    self.query_one('#setup-initialize-displays', Checkbox).value = not self._config_exists and self._config.get('display', True)
                    self._initialization_option_loaded = True
                user = self._config.get('user') or '일반 계정 지정 필요'
                targets = self._config.get('screens') or [self._config]
                mode = ('DID ' + str(len(targets)) + '개 · 사이트 설정 ' + str(sum(bool(target.get('url')) for target in targets)) + '개'
                        if self._config.get('schema_version') == 2 else
                        self._config.get('url') or ('사이트 미설정 · 안내 화면' if self._config.get('autostart') else '자동 시작 꺼짐'))
                self.query_one('#config-summary', Static).update(Text(f'설정 대상 · {user}\n화면 · {mode}'))
            runs = await runs_task
            if not self.is_running:
                return
            active = next((job for job in (runs or {}).get('runs', []) if job.get('state') in ACTIVE), None)
            if active and not self._active_job and tracked_at_start is None:
                attached_early = True
                self._active_job = active['run_id']
                self.render_job(active)
            report = await report_task
            if not self.is_running:
                return
            self._status_known = bool(report and (report.get('results') or report.get('preparation', {}).get('results')))
            if report is None:
                self._setup_report = None
                self._full_report = None
                if not self._active_job:
                    self._display_job = None
                    self.render_setup_table()
                self._groups = {}
                self.query_one('#status-table', DataTable).clear()
                self.set_status_header('상태 조회 실패 · 정상으로 판단할 수 없습니다.', '상태를 다시 확인하세요.')
                self.query_one('#status-detail', TextArea).load_text('현재 조회가 실패했습니다. 상태 다시 확인으로 재시도하세요.')
            if report is not None:
                self._full_report = deepcopy(report)
                self._setup_report = deepcopy(report)
                configuration = self._setup_report.get('configuration_report')
                if (isinstance(configuration, dict) and configuration.get('scope') == 'configuration'
                        and isinstance(configuration.get('results'), list)):
                    self._setup_report.update(configuration)
                self.render_status(report)
                active = next((job for job in report.get('runs', []) if job.get('state') in ACTIVE), None)
                if active and not self._active_job and tracked_at_start is None and not attached_early:
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
                if not self._active_job and tracked_at_start is None and not attached_early:
                    self._display_job = None
                    self.render_setup_table()
        finally:
            for task in (config_task, runs_task, report_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(config_task, runs_task, report_task, return_exceptions=True)
            self._loading = False
            self._has_checked = True
            if self.is_running:
                self.refresh_query_label()
                self.render_setup_table()
                self.query_one('#status-refresh', Button).disabled = False
                self.update_button()
                if self.query_one('#tabs', Tabs).active == 'setup-tab' and not self.query_one('#setup-start', Button).disabled:
                    self.query_one('#setup-start', Button).focus()

    @on(Tabs.TabActivated, '#tabs')
    def activate_tab(self, event):
        if not self.is_running:
            return
        section = {'setup-tab': 'setup', 'status-tab': 'status', 'screens-tab': 'screens', 'quick-tab': 'quick'}[event.tab.id]
        self.query_one('#pages', ContentSwitcher).current = section
        target = {'setup': '#setup-start', 'screens': '#screens-map', 'status': '#status-table', 'quick': '#quick-refresh'}[section]
        self.call_after_refresh(self.query_one(target).focus)
        if section == 'setup':
            self.call_after_refresh(self._fit_setup_table)
        elif section == 'screens':
            self.query_one('#screens', ScreenPanel).activate()
        elif section == 'quick':
            self.query_one('#quick', QuickPanel).activate()

    @on(Button.Pressed, '#status-refresh')
    def refresh_pressed(self):
        self.action_refresh()

    def action_status(self):
        self.query_one('#tabs', Tabs).active = 'status-tab'

    def action_setup(self):
        self.query_one('#tabs', Tabs).active = 'setup-tab'

    def action_refresh(self):
        if self.is_running and self.query_one('#pages').current == 'quick':
            self.query_one('#quick', QuickPanel).activate()
            return
        if self.is_running and self.query_one('#pages').current == 'screens':
            self.query_one('#screens', ScreenPanel).activate()
            return
        if not self.is_running or self._loading or self._submitting:
            return
        self.run_worker(self.load_state(), group='load', exclusive=True, exit_on_error=False)

    @on(Button.Pressed, '#setup-start')
    def setup_pressed(self):
        self.run_worker(self.start_setup(), group='submit', exit_on_error=False)

    async def start_setup(self):
        if not self.is_running or self._submitting or self._active_job or self._loading or not self._status_known or not self._config or not self._config.get('user'):
            return
        option = self.query_one('#setup-bash-assist', Checkbox)
        initialization = self.query_one('#setup-initialize-displays', Checkbox)
        initialize_requested = initialization.value and self.can_initialize_displays()
        payload = {'expected_config': deepcopy(self._config)}
        if option.value:
            payload['bash_assist'] = True
        if initialize_requested or not self._config_exists:
            payload['initialize_displays'] = initialize_requested
        self._submitting = True
        self.update_button()
        try:
            current = await self.api('config-show')
            if not self.is_running:
                return
            if current is None:
                return
            if current.get('config') != self._config:
                self.notice('구성이 변경됐습니다. 상태를 다시 확인한 뒤 실행하세요.')
                self._status_known = False
                return
            if initialize_requested:
                plan = await self.api('display-initialization-preview')
                if plan is None or not self.is_running:
                    return
                approved = await self.push_screen_wait(DisplayInitializationConfirmation(plan))
                if not approved or plan.get('state') not in {'ready', 'pending'}:
                    self.notice('안내 화면 준비와 간편 설정을 취소했습니다.')
                    return
            conflicts = (self._setup_report or {}).get('results', [])
            if any(row.get('id') in {'docker.conflict.podman', 'docker.conflict.podman-docker'}
                   for row in conflicts):
                self.notice('Podman 사용 여부와 제거 목록을 확인하고 있습니다.')
                plan = await self.api('podman-plan')
                if not self.is_running or plan is None:
                    return
                if plan.get('state') != 'not_required':
                    approved = await self.push_screen_wait(PodmanConfirmation(plan))
                    if not approved or plan.get('state') != 'ready':
                        self.notice('Podman 정리와 간편 설정을 취소했습니다.')
                        return
                    payload['podman_cleanup'] = plan['token']
            job = await self.api('submit', payload)
            if not self.is_running:
                return
            if job is not None:
                option.value = False
                initialization.value = False
                if payload.get('initialize_displays'):
                    self._initialization_status = {'state': 'pending', 'reason': '설치 후 모니터별 안내를 준비합니다.'}
                self._active_job = job['run_id'] if job.get('state') in ACTIVE else None
                self.render_job(job)
                self.notice('설정을 시작했습니다. 이 화면을 종료해도 작업은 계속됩니다.')
        finally:
            self._submitting = False
            self.update_button()

    async def poll_job(self):
        if not self.is_running or self._polling or not self._active_job:
            return
        self._polling = True
        try:
            job = await self.api('runs-show', {'run_id': self._active_job})
            if not self.is_running:
                return
            if job is None:
                return
            self.render_job(job)
            if isinstance(job.get('display_onboarding'), dict):
                self._initialization_status = deepcopy(job['display_onboarding'])
            if job.get('query_warning'):
                self.notice(value_text(job['query_warning']) + ' · 진행 중단으로 단정하지 않습니다.')
                return
            if job.get('state') not in ACTIVE:
                self._active_job = None
                if job.get('report'):
                    if job['report'].get('scope') == 'configuration':
                        self._setup_report = dict(self._setup_report or {}, **deepcopy(job['report']))
                        self._setup_report['configuration_report'] = deepcopy(job['report'])
                        self.set_status_header('설정 결과 수신', '현재 실행 상태를 다시 확인하고 있습니다.')
                        self.run_worker(self.refresh_runtime_status(), group='runtime-status', exclusive=True, exit_on_error=False)
                    else:
                        self._full_report = deepcopy(job['report'])
                        self.render_status(job['report'])
                self.notice('작업 결과와 다음 조치를 확인하세요. 정상 항목은 유지합니다.')
        finally:
            self._polling = False
            self.update_button()

    async def poll_initialization(self):
        if (not self.is_running or self._initialization_polling or self._loading or self._submitting
                or self._initialization_status.get('state') != 'pending'):
            return
        self._initialization_polling = True
        try:
            response = await self.api('config-show')
            if not response or not self.is_running:
                return
            self._initialization_status = response.get('display_initialization') or {}
            if response.get('config') != self._config and not self._active_job:
                await self.load_state()
                if self.query_one('#pages').current == 'screens':
                    self.query_one('#screens', ScreenPanel).activate()
            self.render_setup_header()
        finally:
            self._initialization_polling = False

    async def refresh_runtime_status(self):
        """A configuration-only installation result is not a live runtime check."""
        value = await self.api('status')
        if not self.is_running:
            return
        if isinstance(value, dict) and isinstance(value.get('results'), list) and value['results']:
            self._full_report = deepcopy(value)
            configuration = value.get('configuration_report')
            if (isinstance(configuration, dict) and configuration.get('scope') == 'configuration'
                    and isinstance(configuration.get('results'), list)):
                self._setup_report = {**deepcopy(value), **deepcopy(configuration)}
            self.render_status(value)
        else:
            self._full_report = None
            self._groups = {}
            self.query_one('#status-table', DataTable).clear()
            self.set_status_header('현재 실행 상태 조회 실패 · 정상으로 판단할 수 없습니다.', '상태를 다시 확인하세요.')
            self.query_one('#status-detail', TextArea).load_text('현재 실행 상태를 확인하지 못했습니다. 상태를 다시 확인하세요.')

    def render_job(self, job):
        if not self.is_running:
            return
        overview = self.query_one('#setup-overview')
        overview.set_classes(job.get('state') if job.get('state') in JOB_LABELS else '')
        if not self._display_job or self._display_job.get('run_id') != job.get('run_id'):
            self._last_followed_step = None
        self._display_job = deepcopy(job)
        self.render_setup_header()
        self.render_setup_table()

    def render_setup_table(self):
        if not self.is_running:
            return
        if self._display_job is None:
            self.query_one('#setup-overview').set_classes('')
        parents = progress_rows(report=self._setup_report, job=self._display_job)
        rows = []
        for parent in parents:
            row = dict(parent)
            children = row.get('children', [])
            expanded = row['id'] in self._expanded_steps
            row['label'] = ('▾ ' if expanded else '▸ ') + row['label'] if children else row['label']
            rows.append(row)
            if expanded:
                for index, child in enumerate(children):
                    branch = '└─ ' if index == len(children) - 1 else '├─ '
                    rows.append(dict(child, label='  ' + branch + child['label'], parent_id=parent['id']))
        table = self.query_one('#setup-table', DataTable)
        old_ids = [row['id'] for row in self._progress_rows]
        ids = [row['id'] for row in rows]
        selected = old_ids[table.cursor_row] if old_ids and table.cursor_row < len(old_ids) else None
        self._progress_rows = rows
        if old_ids != ids:
            table.clear()
            for row in rows:
                height = 1 if row.get('parent_id') else (2 if self.size.height >= 40 else 1)
                table.add_row(self.label_cell(row), self.progress_cell(row), Text(row.get('current_step', ''), style='#cbc2ef'), key=row['id'], height=height)
            if selected in ids:
                table.move_cursor(row=ids.index(selected), animate=False)
        else:
            for row in rows:
                table.update_cell(row['id'], 'item', self.label_cell(row))
                table.update_cell(row['id'], 'state', self.progress_cell(row))
                table.update_cell(row['id'], 'current', Text(row.get('current_step', '')))
        counts = {}
        groups = {'ok': ('✓ 정상·완료', '#b5e9d9'), 'kept': ('✓ 정상·완료', '#b5e9d9'),
                  'completed': ('✓ 정상·완료', '#b5e9d9'), 'running': ('● 진행 중', 'bold #cbc2ef'),
                  'pending': ('◷ 적용 대기', '#f1d5a3'), 'blocked': ('! 조치 필요', 'bold #f3b8c5'),
                  'error': ('! 조치 필요', 'bold #f3b8c5'), 'unknown': ('? 확인 필요', '#f1d5a3'),
                  'change_required': ('자동 설정 대상', '#f1d5a3'), 'waiting': ('… 실행 대기', 'dim'),
                  'not_run': ('— 미실행', 'dim'), 'not_applicable': ('— 해당 없음', 'dim')}
        for row in parents:
            label, style = groups.get(row['state'], (STATE_LABELS.get(row['state'], row['state']), '#f3b8c5'))
            if row['state'] == 'unknown' and row.get('evidence') == 'missing':
                label, style = '— 결과 없음', 'dim'
            counts.setdefault((label, style), 0)
            counts[(label, style)] += 1
        summary = Text()
        for (label, style), count in counts.items():
            if summary:
                summary.append('   ')
            summary.append(f'{label} {count}', style=style)
        if not self._has_checked and self._setup_report is None and self._display_job is None:
            summary = Text('… 검사 결과를 기다리고 있습니다.', style='#cbc2ef')
        if (self._display_job or {}).get('run_id'):
            summary.append(f"   실행 ID {self._display_job['run_id']}", style='dim')
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
    def label_cell(row):
        return Text(row['label'], style='#b9cad8' if row.get('parent_id') else 'bold #e9f0f7')

    def progress_cell(self, row):
        if row['state'] == 'unknown':
            if not self._has_checked and self._setup_report is None and self._display_job is None:
                return Text('… 검사 중', style='dim')
            if row.get('evidence') == 'missing':
                return Text('— 세부 기록 없음' if row.get('parent_id') else '— 검사 결과 없음', style='dim')
            return Text('? 조회 확인 필요', style='#f1d5a3')
        return Text(STATE_LABELS.get(row['state'], row['state']), style=STATE_STYLES.get(row['state'], '#f1d5a3'))

    def show_setup_row(self, index):
        if not self.is_running:
            return
        if not 0 <= index < len(self._progress_rows):
            return
        row = self._progress_rows[index]
        label = row['label'].lstrip(' ▸▾├└─')
        detail = row.get('detail', '')
        if not detail.startswith(label + '\n'):
            detail = label + '\n' + detail
        if not self._has_checked and self._setup_report is None and self._display_job is None:
            detail = row['label'] + '\n환경 상태를 검사하고 있습니다.\n검사가 끝나면 현재 값과 필요한 조치를 표시합니다.'
        output = self.query_one('#setup-output', TextArea)
        if output.text != detail:
            output.load_text(detail)

    @on(DataTable.RowHighlighted, '#setup-table')
    def setup_row_highlighted(self, event):
        keys = [row['id'] for row in self._progress_rows]
        if event.row_key.value in keys:
            self.show_setup_row(keys.index(event.row_key.value))

    def render_status(self, report):
        if not self.is_running:
            return
        rows = report.get('results', []) + report.get('preparation', {}).get('results', [])
        groups = {}
        for row in rows:
            groups.setdefault(group_name(row.get('id')), []).append(row)
        self._groups = groups
        table = self.query_one('#status-table', DataTable)
        issue = first_issue(rows)
        with table.prevent(DataTable.RowHighlighted):
            table.clear()
            for index, (name, items) in enumerate(groups.items()):
                state = max((row.get('state', 'unknown') for row in items), key=lambda value: PRIORITY.get(value, 5))
                style = '#b5e9d9' if state == 'ok' else 'dim' if state == 'not_applicable' else '#f1d5a3' if state in {'change_required', 'pending', 'unknown'} else '#f3b8c5'
                label = '조회 확인 필요' if state == 'unknown' else '자동 설정 대상' if state == 'change_required' else LABELS.get(state, '확인 불가')
                table.add_row(Text(name, style='bold'), Text(label, style=style), key=str(index))
            if groups:
                preferred = group_name(issue.get('id')) if issue else next(iter(groups))
                selected = self._selected_status_group if self._selected_status_group in groups else preferred
                index = list(groups).index(selected)
                table.move_cursor(row=index, animate=False)
                self.show_group(index)
        if not rows:
            self.set_status_header('검사 결과가 없습니다. 정상으로 판단할 수 없습니다.', '상태를 다시 확인하세요.')
            self.query_one('#status-detail', TextArea).load_text('상태 다시 확인으로 재검사하세요.')
        elif issue:
            kind = issue.get('state', 'unknown')
            issues = sum(row.get('state') in {'error', 'blocked', 'unknown'} for row in rows)
            heading = f"{group_name(issue.get('id'))} · {LABELS.get(kind, '확인 불가')}"
            if issues:
                heading += f'   조치·확인 필요 {issues}'
            action = issue.get('next_action') or (
                '아래에서 관련 항목의 현재 상태를 확인하세요.' if kind != 'unknown'
                else '상태를 다시 확인하세요.')
            self.set_status_header(heading, action, style=STATE_STYLES.get(kind, '#f1d5a3'))
        elif any(row.get('state') == 'ok' for row in rows):
            self.set_status_header('서버 상태 검사 통과', '실제 DID 표시는 모니터에서 확인하세요.', style='#b5e9d9')
        else:
            self.set_status_header('해당하는 검사 항목이 없습니다.', '현재 장비 구성을 확인하세요.')

    def set_status_header(self, heading, action, *, style=''):
        self.query_one('#status-summary', Static).update(Text(heading, style=style))
        self.query_one('#status-next', Static).update('다음 · '+value_text(action))

    @on(DataTable.RowHighlighted, '#status-table')
    def row_highlighted(self, event):
        try:
            index = int(event.row_key.value)
            self._selected_status_group = list(self._groups)[index]
            self.show_group(index)
        except (IndexError, TypeError, ValueError):
            pass

    def show_group(self, index):
        if not self.is_running:
            return
        names = list(self._groups)
        if 0 <= index < len(names):
            name = names[index]
            self.query_one('#status-detail', TextArea).load_text(name + '\n' + result_detail(self._groups[name]))

    async def check_update(self):
        from .update import check
        try:
            result = await asyncio.to_thread(check)
        except Exception:
            result = {'exit_code': 1}
        if not self.is_running:
            return
        self._update_release = result if isinstance(result, dict) else {'exit_code': 1}
        self.render_update_notice()

    def action_update(self):
        from .update_tui import UpdateScreen
        if not isinstance(self.screen, UpdateScreen):
            self.push_screen(UpdateScreen(self))

    def action_chrome(self):
        if len(self.screen_stack) == 1:
            self.query_one('#tabs', Tabs).active = 'screens-tab'

    @on(Button.Pressed, '#chrome-open')
    def chrome_pressed(self):
        self.action_chrome()

    def on_click(self, event: events.Click):
        if event.widget and event.widget.id == 'update-notice':
            self.action_update()

    def accept_update_status(self, value):
        from .update_tui import STATES
        if (not isinstance(value, dict) or not isinstance(value.get('state'), str)
                or value['state'] not in STATES):
            value = {'state': 'unknown', 'reason': '업데이트 상태 응답을 확인하지 못했습니다.'}
        elif value['state'] == 'completed' and (not value.get('installed_version')
                or value.get('installed_version') != value.get('target_version')
                or value.get('exit_code', 0)):
            value = dict(value, state='unknown', reason='설치 완료 버전 검증 결과를 확인하지 못했습니다.')
        previous = self._update_status or {}
        self._update_status = {**previous, **value, 'runtime_version': __version__}
        if value.get('run_id'):
            self._update_run_id = value['run_id']
        if self.is_running:
            self.render_update_notice()
            self.update_button()

    async def poll_update(self, latest=False):
        if not self.is_running or (self._update_polling and not latest):
            return
        async with self._update_lock:
            if not self.is_running:
                return
            if latest:
                self._update_run_id = None
            self._update_polling = True
            try:
                payload = {'run_id': self._update_run_id} if self._update_run_id else {}
                value = await self.service.call('update-status', payload)
            except AssertionError:
                # Older injected services do not implement this optional RPC.
                return
            except Exception:
                value = {'state': 'unknown', 'reason': '업데이트 상태 조회 실패 · 다시 확인하세요.'}
            finally:
                self._update_polling = False
            if not self.is_running:
                return
            self.accept_update_status(value)

    def render_update_notice(self):
        if not self.is_running or not self.query('#update-notice'):
            return
        from .update_tui import LABELS, newer_than_installed
        status, release = self._update_status or {}, self._update_release or {}
        state = status.get('state')
        message = ''
        if state and state != 'idle':
            message = LABELS.get(state, '업데이트 상태 확인 불가')
            if status.get('target_version'):
                message += ' · ' + str(status['target_version'])
        elif release.get('exit_code') or release.get('error'):
            message = '업데이트 조회 실패 · 네트워크 연결을 확인하세요.'
        elif newer_than_installed(release, status):
            message = f"새 버전 {release.get('latest', '확인 불가')}"
        installed = status.get('installed_version')
        runtime = status.get('runtime_version') or __version__
        if installed and installed != runtime:
            message += (' · ' if message else '') + f'설치 {installed} / 실행 {runtime} · ala 재실행 필요'
        if message:
            message += ' · u 업데이트 관리'
        notice = self.query_one('#update-notice', Static)
        notice.update(Text(message, style='#f1d5a3' if state in {'failed', 'interrupted', 'unknown'} else ''))
        notice.display = bool(message)
