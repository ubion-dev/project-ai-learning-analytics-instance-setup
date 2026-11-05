"""Display topology and per-target controls; opening this view never changes a host."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from urllib.parse import urlsplit, urlunsplit

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Select, Static, Switch, TextArea

from .chrome_tui import safe_logs, safe_text
from .screen_map import ScreenMap, topology_text
from .monitors import resolve_output


ACTIONS = {
    'start': ('시작', '현재 적용된 설정으로 이 화면의 Chrome을 시작합니다.'),
    'stop': ('중지', '이 화면의 Chrome 실행을 중지합니다.'),
    'restart': ('재시작', '현재 적용된 설정으로 이 화면의 Chrome을 다시 시작합니다.'),
    'reload': ('페이지 새로고침', 'Chrome을 유지하고 현재 적용된 사이트를 다시 읽습니다.'),
    'apply': ('설정 적용', '저장한 설정을 이 화면에 반영합니다. 실행 중인 Chrome은 잠깐 재시작될 수 있습니다.'),
    'edit': ('설정', '사이트와 출력 등 이 화면의 설정을 편집합니다.'),
    'add': ('추가', '관리할 화면을 추가합니다.'),
    'logs': ('로그', '이 화면의 Chrome 로그를 표시합니다.'),
    'retry': ('배포 재시도', '프론트 배포 연동에서 실패한 갱신을 다시 시도합니다.'),
    'identify': ('모니터 식별', '모든 활성 화면에 번호와 연결 이름을 10초간 표시합니다. 사이트는 유지합니다.'),
}
RETRY_DISABLED = '배포 재시도 · 현재 적용된 설정에 프론트 배포 연동이 없습니다.'
RESULT_LABELS = {
    'queued': '접수', 'running': '진행', 'completed': '완료',
    'failed': '실패', 'pending': '대기', 'cancelled': '취소', 'unknown': '확인 불가', 'interrupted': '중단',
    'selected_unverified': '설정 확정 · 중지 유지',
    'rolled_back': '실패 · 원래 배정 복구', 'recovery_required': '복구 확인 필요',
}
_AUTO_OUTPUT = ''


def site_label(value):
    """Show only origin/path; query strings and fragments never enter the overview."""
    if not isinstance(value, str):
        return '주소 미설정'
    try:
        p = urlsplit(value)
        if p.scheme not in {'http', 'https'} or not p.hostname or p.username or p.password:
            return '주소 확인 필요'
        return safe_text(urlunsplit((p.scheme, p.netloc, p.path, '', '')))
    except ValueError:
        return '주소 확인 필요'


def site_confirmed(value):
    settings = value.get('settings') or {}
    health = value.get('health') or {}
    revision = ((value.get('runtime') or {}).get('committed') or {}).get('revision')
    return (health.get('target_matches_config')
            and settings.get('state') not in {'queued', 'running'}
            and (not revision or health.get('config_revision') == revision))


def hardware_text(report):
    label = '물리 출력 · '
    connectors = report.get('hardware_outputs') or []
    if report.get('hardware_output_state') != 'ok' and not connectors:
        return label + '확인 불가'
    if not connectors:
        return label + 'DRM 커넥터 없음'
    states = {'connected': '연결됨', 'disconnected': '연결 안 됨', 'unknown': '확인 불가'}
    ordered = sorted(connectors, key=lambda port: (port.get('status') != 'connected', port['id']))
    return label + ' · '.join(f"{safe_text(port['id'])} {states.get(port.get('status'), '확인 불가')}"
                              for port in ordered)


class MoveConfirmation(ModalScreen[bool]):
    BINDINGS = [('escape', 'cancel', '취소')]
    CSS = """
    MoveConfirmation { align: center middle; background: #000000 65%; }
    #move-dialog { width: 74; max-width: 96%; height: auto; max-height: 90%; padding: 1 2; background: #211f29; border: round #cbc2ef; }
    #move-summary { height: auto; margin-bottom: 1; }
    #move-buttons { height: 3; }
    #move-buttons Button { width: 1fr; margin-right: 1; padding: 0; }
    """

    def __init__(self, target, output, other=None):
        super().__init__()
        self.target = target
        self.output = output
        self.other = other

    def compose(self):
        with Vertical(id='move-dialog'):
            text = ('DID 출력 이동 확인\n' + safe_text(self.target['id']) +
                    '\n사이트 · ' + site_label(self.target.get('url')) +
                    '\n출력 · ' + safe_text(self.target.get('output') or '자동') +
                    ' → ' + safe_text(self.output) +
                    '\n\n실행 중인 DID는 적용 중 잠깐 재시작될 수 있습니다.' +
                    '\n중지된 DID는 배정만 적용됩니다. 시작은 화면 관리에서 선택하세요.')
            if self.other is not None:
                text = ('두 화면 교환 확인\n' + safe_text(self.target['id']) + ' · ' + site_label(self.target.get('url')) +
                        '\n' + safe_text(self.target.get('output')) + ' → ' + safe_text(self.output) +
                        '\n\n' + safe_text(self.other['id']) + ' · ' + site_label(self.other.get('url')) +
                        '\n' + safe_text(self.other.get('output')) + ' → ' + safe_text(self.target.get('output')) +
                        '\n\n두 화면이 잠깐 끊길 수 있습니다. 실패하면 이전 배정을 복구합니다.' +
                        '\n중지된 DID는 중지 상태를 유지합니다.')
            yield Static(text, markup=False, id='move-summary')
            with Horizontal(id='move-buttons'):
                yield Button('취소', id='move-cancel')
                yield Button('두 화면 교환 적용' if self.other else '이동 적용', id='move-confirm', variant='primary')

    def on_mount(self):
        self.query_one('#move-cancel', Button).focus()

    @on(Button.Pressed, '#move-cancel')
    def action_cancel(self):
        self.dismiss(False)

    @on(Button.Pressed, '#move-confirm')
    def confirm(self):
        self.dismiss(True)


class TargetEditor(ModalScreen[dict | None]):
    BINDINGS = [('escape', 'cancel', '취소')]
    CSS = """
    TargetEditor { align: center middle; background: #000000 65%; }
    #target-editor { width: 76; max-width: 96%; height: 29; max-height: 96%; padding: 1 2; background: #211f29; border: round #b5e9d9; }
    #target-fields { height: 1fr; }
    #target-editor Label { height: 1; margin-top: 1; }
    #target-editor Input, #target-editor Select { height: 3; }
    #target-error { height: auto; color: #f3b8c5; }
    #target-help { height: 1; color: #aabdd0; text-wrap: nowrap; text-overflow: ellipsis; }
    #target-buttons { height: 3; margin-top: 1; }
    #target-buttons Button { width: 1fr; }
    #target-editor Switch { height: 3; }
    """

    def __init__(self, target=None, outputs=()):
        super().__init__()
        self.target = deepcopy(target) if target else None
        self.outputs = outputs

    def compose(self) -> ComposeResult:
        value = self.target or {'id': '', 'autostart': True}
        frontend = value.get('frontend') or {}
        current = value.get('output')
        options = [(safe_text(output), output) for output in self.outputs]
        if value['id'] == 'default':
            options.insert(0, ('자동 · 활성 논리 출력 하나', _AUTO_OUTPUT))
        if current and current not in self.outputs:
            options.append((safe_text(current) + ' · 기존 지정', current))
        with Vertical(id='target-editor'):
            yield Static('표시 대상 설정 · 저장 후 화면 관리에서 설정 적용을 선택하세요.')
            with VerticalScroll(id='target-fields'):
                yield Label('대상 ID')
                yield Input(value.get('id', ''), id='target-id', disabled=self.target is not None, placeholder='display-1')
                yield Label('표시 사이트')
                yield Input(value.get('url') or '', id='target-url', placeholder='https://example.com/display')
                yield Label('출력 · GNOME이 확인한 활성 논리 화면')
                yield Select(options, value=current or (_AUTO_OUTPUT if value['id'] == 'default' else Select.NULL),
                             allow_blank=value['id'] != 'default',
                             prompt='출력 선택' if self.outputs else '활성 출력 확인 대기', id='target-output')
                yield Label('창 배치')
                yield Select([('전체화면', 'fullscreen'), ('테스트 · 왼쪽 절반', 'left'), ('테스트 · 오른쪽 절반', 'right')], value=value.get('layout', 'fullscreen'), allow_blank=False, id='target-layout')
                yield Label('로그인 후 자동 시작')
                yield Switch(value=value.get('autostart', True), id='target-autostart')
                yield Label('프론트 배포 감지')
                yield Switch(value=frontend.get('enabled', False), id='target-frontend')
                yield Label('배포 정보 주소 · 프론트 계약 연동 후 사용')
                yield Input(frontend.get('manifest_url') or '', id='target-manifest', placeholder='https://example.com/version.json')
            yield Static('', id='target-error', markup=False)
            yield Static('', id='target-help', markup=False)
            with Horizontal(id='target-buttons'):
                save = Button('저장', id='target-save', variant='primary')
                save.tooltip = '편집 내용을 저장하고 화면 관리로 돌아갑니다.'
                yield save
                cancel = Button('취소', id='target-cancel')
                cancel.tooltip = '이번 편집을 취소합니다.'
                yield cancel

    def on_mount(self):
        self.query_one('#target-url' if self.target else '#target-id', Input).focus()

    def on_descendant_focus(self, event: events.DescendantFocus):
        widget = event.widget
        self.query_one('#target-help', Static).update(
            str(widget.tooltip or '') if isinstance(widget, Button) else '')

    @on(Button.Pressed)
    def pressed(self, event):
        if event.button.id == 'target-cancel':
            self.action_cancel()
        elif event.button.id == 'target-save':
            from .config import ScreenConfig
            output = self.query_one('#target-output', Select).value
            if output is Select.NULL and self.target is None:
                self.query_one('#target-error', Static).update('GNOME 활성 출력이 감지되면 출력 목록에서 선택하세요.')
                return
            value = {'id': self.query_one('#target-id', Input).value,
                     'url': self.query_one('#target-url', Input).value or None,
                     'output': output if isinstance(output, str) and output else None,
                     'layout': self.query_one('#target-layout', Select).value,
                     'autostart': self.query_one('#target-autostart', Switch).value,
                     'frontend': {'enabled': self.query_one('#target-frontend', Switch).value,
                                  'manifest_url': self.query_one('#target-manifest', Input).value or None}}
            try:
                ScreenConfig.from_dict(value)
            except (ValueError, TypeError) as exc:
                self.query_one('#target-error', Static).update(safe_text(str(exc)))
                return
            self.dismiss(value)

    def action_cancel(self):
        self.dismiss(None)


class ScreenPanel(Vertical):
    DEFAULT_CSS = """
    ScreenPanel { height: 1fr; }
    #screens-summary { height: auto; max-height: 2; color: #b5e9d9; }
    #screens-hardware, #screens-result, #screens-next { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    #screens-help { height: 2; }
    #screens-guidance { height: 2; }
    #screens-help { width: 1fr; }
    #screens-swap-recover { display: none; width: 18; height: 1; min-width: 16; padding: 0; border: none; }
    #screens-hardware, #screens-help { color: #aabdd0; }
    #screens-result { padding: 0 1; background: #2a2634; color: #b5e9d9; text-style: bold; }
    #screens-next { padding: 0 1; color: #e9f0f7; }
    #screens-map { height: 8; padding: 0 1; background: #2a2634; border: round #50475f; }
    #screens-map:focus { border: round #e4dcff; }
    #screens-map.no-layout { height: auto; max-height: 4; }
    #screens-table { height: 1fr; min-height: 3; margin-top: 1; }
    #screens-detail { height: 6; min-height: 3; margin-top: 1; }
    #screens-actions { height: 3; }
    #screens-actions Button { width: 1fr; min-width: 7; padding: 0; }
    Screen.compact #screens-summary { margin-bottom: 0; max-height: 1; }
    Screen.compact #screens-map { height: auto; max-height: 3; padding: 0; border: none; }
    Screen.compact #screens-table { margin-top: 0; }
    Screen.compact #screens-detail { height: 4; margin-top: 0; }
    Screen.compact #screens-actions { margin-top: 0; }
    ScreenPanel.tight #screens-map { height: auto; max-height: 2; padding: 0; border: none; }
    ScreenPanel.tight #screens-help { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    ScreenPanel.tight #screens-guidance, Screen.compact #screens-guidance { height: 1; }
    ScreenPanel.tight #screens-table, ScreenPanel.tight #screens-detail { margin-top: 0; }
    ScreenPanel.tight #screens-detail { height: 4; }
    """

    def __init__(self, owner, **kwargs):
        super().__init__(**kwargs)
        self.owner = owner
        self.report = {}
        self.selected = None
        self.reading = False
        self._read_lock = asyncio.Lock()
        self.mutating = False
        self.messages = {}
        self.following = {}
        self.swap_following = {}
        self.launches = {}
        self.loaded = False

    def compose(self):
        yield Static('표시 대상 확인 중', id='screens-result', markup=False)
        yield Static('', id='screens-next', markup=False)
        with Horizontal(id='screens-actions'):
            for key, (label, help_text) in ACTIONS.items():
                button = Button(label, id='screens-'+key, disabled=key != 'add')
                button.tooltip = help_text
                yield button
        with Horizontal(id='screens-guidance'):
            yield Static('', id='screens-help', markup=False)
            yield Button('복구 재시도', id='screens-swap-recover')
        yield Static('화면 관리를 열면 연결된 출력과 창을 조회합니다.', id='screens-summary', markup=False)
        yield Static('', id='screens-hardware', markup=False)
        yield ScreenMap(id='screens-map')
        yield DataTable(id='screens-table', cursor_type='row')
        yield TextArea('', id='screens-detail', read_only=True, show_cursor=False, highlight_cursor_line=False)

    def on_mount(self):
        self.query_one('#screens-map').border_title = '출력 배정 · DID 선택 후 드래그'
        self.query_one('#screens-table').border_title = '표시 대상 · 실제 관측과 설정을 구분합니다'
        self.query_one('#screens-detail').border_title = '선택 대상 · 상태와 다음 조치'
        self.set_interval(3, self.poll)
        self.render_report()

    def on_resize(self):
        if self.is_mounted:
            self.call_after_refresh(self.render_report)

    def active(self):
        return self.is_mounted and self.owner.is_running and self.owner.query_one('#pages').current == 'screens'

    def activate(self):
        self.run_worker(self.refresh_state(), group='screens-read', exclusive=True, exit_on_error=False)

    async def poll(self):
        if self.active() and not self.reading and not self.mutating:
            await self.refresh_state()

    async def refresh_state(self, *, wait=False):
        if self.reading and not wait:
            return
        async with self._read_lock:
            await self._refresh_state()

    async def _refresh_state(self):
        if self.reading or not self.is_mounted:
            return
        self.reading = True
        try:
            value = await self.owner.service.call('screens-inspect')
            if not isinstance(value, dict) or not isinstance(value.get('targets'), list):
                raise ValueError('잘못된 화면 조회 응답')
            self.report = value
            self.loaded = True
        except Exception as exc:
            self.report = {'targets': [], 'reason': safe_text(str(exc)), 'state': 'unknown'}
        finally:
            self.reading = False
        if self.is_mounted:
            self.render_report()

    def target(self):
        return next((v for v in self.report.get('targets', []) if v.get('id') == self.selected), None)

    def render_help(self):
        widget = self.screen.focused
        key = (widget.id or '').removeprefix('screens-') if isinstance(widget, Button) else ''
        help_text = (str(widget.tooltip or '') if key in ACTIONS else
                     'DID 선택 → 현재 출력에서 다른 출력으로 드래그 → 이동 확인')
        if key == 'apply' and self.screen.has_class('compact') and help_text == ACTIONS['apply'][1]:
            help_text = '이 화면에 설정을 반영합니다. Chrome이 잠깐 재시작될 수 있습니다.'
        self.query_one('#screens-help', Static).update(help_text)

    def on_descendant_focus(self, event: events.DescendantFocus):
        self.render_help()

    @staticmethod
    def retry_enabled(value):
        if not value:
            return False
        frontend = value.get('applied_frontend', value.get('frontend')) or {}
        return (value.get('applied_url', value.get('url')) is not None
                and frontend.get('enabled') is True and bool(frontend.get('manifest_url')))

    def render_report(self):
        if not self.is_mounted:
            return
        targets = self.report.get('targets', [])
        if self.selected not in {v.get('id') for v in targets}:
            self.selected = targets[0].get('id') if targets else None
        physical = len(self.report.get('outputs') or [])
        logical = len(self.report.get('logical_outputs') or [])
        confirmed = self.report.get('confirmed_windows')
        if type(confirmed) is not int:
            counts = [v.get('placement', {}).get('confirmed_window_count', v.get('placement', {}).get('window_count')) for v in targets]
            confirmed = sum(v for v in counts if type(v) is int) if any(type(v) is int for v in counts) else '확인 불가'
        output_label = f'출력 {physical}개 · 논리 화면 {logical}개' if self.report.get('output_state', 'ok') == 'ok' else '출력 조회 대기·확인 필요'
        confirmed_label = str(confirmed)+'개' if type(confirmed) is int else '확인 불가'
        self.query_one('#screens-summary', Static).update(Text(
            f'{output_label} · 관리 대상 {len(targets)}개 · 확인된 창 {confirmed_label}'
            if self.loaded else '출력·창 조회 확인 필요 · '+str(self.report.get('reason', '조회 중'))))
        self.query_one('#screens-hardware', Static).update(Text(hardware_text(self.report)))
        map_view = self.query_one('#screens-map', ScreenMap)
        short = self.size.height < 32 or self.size.width < 99
        self.set_class(short and not self.screen.has_class('compact'), 'tight')
        map_view.set_class(not self.report.get('logical_outputs'), 'no-layout')
        map_view.update_report(self.report, self.selected, self.owner._config,
                               enabled=not self.mutating and not self.owner._active_job)
        map_view.tooltip = '목록에서 DID를 선택하고, 해당 출력에서 다른 출력으로 드래그해 이동하세요.'
        table = self.query_one('#screens-table', DataTable)
        compact = self.size.width < 100
        table.clear(columns=True)
        available = max(50, self.size.width - (10 if compact else 16))
        columns = (('대상 · 출력', min(20, available//3)),
                   ('적용 사이트', max(12, available-min(20, available//3)-22)),
                   ('실행 · 사이트', 22)) if compact else (
                   ('대상', 14), ('출력', 13), ('적용 사이트', max(16, available-66)),
                   ('실행 · 적용', 17), ('사이트', 12), ('창 배치', 10))
        for label, width in columns:
            table.add_column(label, width=width)
        run_labels = {'active': '실행 중', 'activating': '시작 중', 'inactive': '중지됨', 'failed': '실패'}
        health_labels = {'healthy': '정상', 'waiting_configuration': '설정 안내', 'recovering': '복구 중', 'waiting_site': '응답 대기', 'unknown': '확인 불가'}
        for value in targets:
            service = value.get('service') or {}
            health = value.get('health') or {}
            placement = value.get('placement') or {}
            run = run_labels.get(service.get('ActiveState'), '확인 불가')
            runtime = value.get('runtime') or {}
            settings = value.get('settings') or {}
            code_pending = (runtime.get('committed') or {}).get('version') != (runtime.get('desired') or {}).get('version')
            update_pending = settings.get('pending') or code_pending
            if update_pending:
                run = run.replace(' ', '')+'·적용대기'
            site = '중지됨' if service.get('ActiveState') == 'inactive' else health_labels.get(health.get('state'), '확인 불가')
            deployment = value.get('frontend_status') or {}
            if self.retry_enabled(value) and deployment.get('state') == 'blocked':
                site += ' · 배포 보류'
            output = value.get('output') or ('자동' if value['id'] == 'default' else '미선택')
            color = '#b5e9d9' if site == '정상' else 'dim' if site == '중지됨' else '#f1d5a3'
            items = (f'{value["id"]} · {output}', site_label(value.get('applied_url', value.get('url'))), Text(run+'·'+site, style='#f1d5a3' if update_pending else color)) if compact else (
                value['id'], output, site_label(value.get('applied_url', value.get('url'))), Text(run, style='#f1d5a3' if update_pending else ''), Text(site, style=color),
                {'ok': '확인됨', 'pending': '배치 대기', 'unknown': '확인 불가'}.get(placement.get('state'), '확인 불가'))
            table.add_row(*items, key=value['id'])
        if self.selected:
            table.move_cursor(row=next(i for i, v in enumerate(targets) if v['id'] == self.selected), animate=False)
        target = self.target()
        settings = (target or {}).get('settings') or {}
        applying = settings.get('state') in {'queued', 'running'}
        locked = self.mutating or bool(self.owner._active_job)
        for key, (_, help_text) in ACTIONS.items():
            button = self.query_one('#screens-'+key, Button)
            disabled = locked or (key not in {'add', 'identify'} and not target)
            swap = (target or {}).get('swap') or {}
            if swap.get('state') in {'queued', 'running', 'unknown', 'recovery_required'} and key not in {'stop', 'logs', 'identify'}:
                disabled = True
            if key == 'apply':
                disabled |= not settings.get('pending') or applying
                if applying:
                    help_text = '이 화면의 설정을 적용하고 있습니다.'
                elif not settings.get('pending'):
                    help_text = settings.get('reason') or '먼저 변경한 설정을 저장하세요.'
            if key == 'retry' and target and not self.retry_enabled(target):
                disabled = True
                help_text = RETRY_DISABLED
            if applying and key in {'start', 'restart', 'reload', 'edit', 'retry'}:
                disabled = True
                help_text = '설정 적용이 끝나면 사용할 수 있습니다.'
            button.disabled = bool(disabled)
            button.tooltip = safe_text(help_text)
        self.query_one('#screens-reload', Button).label = '새로고침' if compact else ACTIONS['reload'][0]
        recover = self.query_one('#screens-swap-recover', Button)
        recover.display = bool(((target or {}).get('swap') or {}).get('state') in {'recovery_required', 'unknown'})
        recover.disabled = locked
        self.render_help()
        self.render_detail()

    def priority(self, value):
        if not value:
            if self.report.get('state') == 'unknown':
                return '표시 대상을 확인할 수 없습니다', 'r로 다시 확인하세요.', None, '#f1d5a3'
            if not self.loaded:
                return '표시 대상 확인 중', '조회 결과를 기다리세요.', None, '#cbc2ef'
            return '등록된 표시 대상이 없습니다', '추가를 선택해 화면을 등록하세요.', 'add', '#f1d5a3'
        settings = value.get('settings') or {}
        state = (value.get('service') or {}).get('ActiveState')
        swap = value.get('swap') or {}
        if swap.get('state') in {'queued', 'running', 'unknown', 'recovery_required'}:
            return ('두 화면 교환 · ' + ('복구 확인 필요' if swap['state'] in {'unknown', 'recovery_required'} else '진행 중'),
                    swap.get('next_action') or swap.get('reason') or '작업 결과를 확인하세요.', None, '#f1d5a3')
        if (self.mutating or self.owner._active_job or self.selected in self.following
                or self.selected in self.launches or settings.get('state') in {'queued', 'running'}):
            return '작업 진행 중', '작업 결과를 확인하세요.', None, '#cbc2ef'
        if state == 'failed':
            return 'Chrome 실행 실패', '선택한 화면의 Chrome 로그를 확인하세요.', 'logs', '#f3b8c5'
        if state not in {'active', 'inactive', 'activating', 'deactivating'} or settings.get('state') == 'unknown':
            return '화면 상태 확인 불가', 'r로 다시 확인하고 화면 상세를 살펴보세요.', None, '#f1d5a3'
        if settings.get('state') in {'failed', 'interrupted'}:
            return '설정 적용 실패·중단', '선택한 화면의 작업 결과와 원인을 확인하세요.', None, '#f3b8c5'
        if settings.get('pending'):
            return '저장한 설정 적용 대기', '설정 적용을 선택하면 화면이 잠시 끊길 수 있습니다.', 'apply', '#f1d5a3'
        if state in {'activating', 'deactivating'}:
            return 'Chrome 상태 전환 중', '조회 결과를 기다리세요.', None, '#cbc2ef'
        if state == 'inactive':
            return 'Chrome 중지됨', '표시가 필요하면 시작을 선택하세요.', 'start', '#f1d5a3'
        if self.report.get('output_state') not in {None, 'ok'}:
            return 'Chrome 실행 중 · 출력 확인 필요', '출력 조회와 화면 배치 상세를 확인하세요.', None, '#f1d5a3'
        if (value.get('placement') or {}).get('state') not in {'ok', 'not_applicable'}:
            return 'Chrome 실행 중 · 창 배치 확인 필요', '선택 대상의 출력과 창 배치를 확인하세요.', None, '#f1d5a3'
        if not value.get('applied_url', value.get('url')):
            health = value.get('health') or {}
            revision = (((value.get('runtime') or {}).get('committed') or {}).get('revision'))
            if (health.get('state') == 'waiting_configuration' and health.get('local_page_ready') is True
                    and (not revision or health.get('config_revision') == revision)):
                return '설정 안내 표시 중', '설정에서 사이트 주소를 저장하고 적용하세요.', 'edit', '#b5e9d9'
            return '설정 안내 표시 확인 필요', 'Chrome 로그에서 로컬 안내 문서 상태를 확인하세요.', 'logs', '#f1d5a3'
        if self.retry_enabled(value) and (value.get('frontend_status') or {}).get('state') == 'blocked':
            return 'Chrome 실행 중 · 프론트 배포 보류', '배포 상세를 확인하고 필요하면 재시도하세요.', 'retry', '#f1d5a3'
        if (value.get('health') or {}).get('state') != 'healthy' or not site_confirmed(value):
            return 'Chrome 실행 중 · 사이트 확인 필요', '선택한 화면의 Chrome 로그와 적용 주소를 확인하세요.', 'logs', '#f1d5a3'
        return 'Chrome 실행 중 · 사이트 응답 확인', '실제 표시 상태는 DID 화면에서 확인하세요.', None, '#b5e9d9'

    def render_detail(self):
        value = self.target()
        settings = (value or {}).get('settings') or {}
        message = self.messages.get(self.selected, '')
        follow = self.following.get(self.selected)
        launch = self.launches.get(self.selected)
        swap = (value or {}).get('swap') or {}
        if swap.get('operation_id') == self.swap_following.get(self.selected) and swap.get('operation_id'):
            message = self.result_message('두 화면 교환', swap)
            self.messages[self.selected] = message
            if swap.get('state') in {'completed', 'rolled_back'}:
                self.swap_following.pop(self.selected, None)
        if follow and value:
            action, source, key, identifier = follow
            latest = value.get(source) or {}
            if latest.get(key) == identifier:
                message = self.result_message(ACTIONS[action][0], latest)
                self.messages[self.selected] = message
                if latest.get('state') not in {'queued', 'running'}:
                    self.following.pop(self.selected, None)
        elif launch and value:
            action, previous_pid = launch
            label = ACTIONS[action][0]
            service = value.get('service') or {}
            current_pid = service.get('MainPID')
            placement = (value.get('placement') or {}).get('state')
            if service.get('ActiveState') == 'failed':
                outcome, reason = '실패', 'Chrome 서비스 로그를 확인하세요.'
            elif (value.get('state') == 'running' and service.get('ActiveState') == 'active'
                  and current_pid and current_pid != previous_pid and placement in {'ok', 'not_applicable'}):
                outcome, reason = '완료', '관리 Chrome의 실행을 확인했습니다.'
            else:
                outcome, reason = '진행', '관리 Chrome 실행 확인을 기다립니다.'
            message = f'{label} · {outcome} · {reason}'
            if outcome != '진행':
                self.messages[self.selected] = message
                self.launches.pop(self.selected, None)
        elif not message and settings.get('state') in RESULT_LABELS:
            message = self.result_message('설정 적용', settings)
        headline, next_action, primary, tone = self.priority(value)
        parts = (self.selected, headline, message.split('\n', 1)[0])
        self.query_one('#screens-result', Static).update(Text(' · '.join(filter(None, parts)), style=tone))
        self.query_one('#screens-next', Static).update('다음 · '+next_action)
        for key in ACTIONS:
            button = self.query_one('#screens-'+key, Button)
            button.variant = 'primary' if key == primary and not button.disabled else 'default'
        lines = []
        if value:
            runtime = value.get('runtime') or {}
            lines += ['저장 주소 · '+site_label(value.get('url')),
                      '적용 주소 · '+site_label(value.get('applied_url', value.get('url'))),
                      '설정 · '+('적용 대기' if settings.get('pending') else '적용 확인 필요' if settings.get('state') == 'unknown' else '반영됨'),
                      '실제 사이트 · '+(site_label(value['actual_url']) if value.get('actual_url') else '적용 주소의 문서 확인' if site_confirmed(value) else '확인 대기'),
                      '자동 시작 · '+('켜짐' if value.get('autostart') else '꺼짐')]
            if message:
                lines.append(message)
            swap = value.get('swap') or {}
            if swap.get('operation_id'):
                lines += ['두 화면 교환 · ' + safe_text(swap.get('state')) + ' / ' + safe_text(swap.get('phase')),
                          '작업 ID · ' + safe_text(swap['operation_id']),
                          safe_text(swap.get('reason') or ''), safe_text(swap.get('next_action') or '')]
            if settings.get('state') == 'unknown' and settings.get('reason'):
                lines.append(safe_text(settings['reason']))
                if settings.get('next_action'):
                    lines.append(safe_text(settings['next_action']))
            if not self.retry_enabled(value):
                lines.append(RETRY_DISABLED)
            if runtime.get('committed'):
                selected = runtime['committed'].get('version', '확인 불가')
                running = (value.get('health') or {}).get('supervisor_version', '확인 불가')
                desired = (runtime.get('desired') or {}).get('version', selected)
                lines.append(f'화면 관리 버전 · 선택 {selected} / 실행 {running} / 후보 {desired}')
                if desired != selected:
                    lines.append('새 화면 코드 적용 대기 · 자동 운영 시 03:00–04:00 한국 시간')
                last = runtime.get('last_result') or {}
                if last.get('state'):
                    labels = {'completed': '적용 완료', 'failed': '적용 실패', 'cancelled': '취소됨',
                              'pending': '준비 대기', 'selected_unverified': '코드 선택 완료 · 실행 미검증'}
                    lines.append('최근 화면 작업 · '+labels.get(last['state'], '결과 확인 필요'))
                    if last.get('reason'):
                        lines.append(safe_text(last['reason']))
            for key in ('service', 'health', 'placement', 'runtime', 'frontend_status', 'reload'):
                item = value.get(key) or {}
                if isinstance(item, dict):
                    state = item.get('state', item.get('ActiveState'))
                    if state:
                        label = {'service': 'Chrome 실행', 'health': '사이트', 'placement': '창 배치', 'runtime': '화면 코드', 'frontend_status': '프론트 배포', 'reload': '수동 새로고침'}[key]
                        description = {'active': '실행 중', 'inactive': '중지됨', 'healthy': '정상', 'waiting_configuration': '설정 안내 표시 중', 'recovering': '연결 복구 중', 'pending': '적용 대기', 'ok': '정상', 'unknown': '확인 불가', 'completed': '완료', 'failed': '실패', 'interrupted': '중단됨', 'queued': '접수됨', 'running': '진행 중', 'idle': '기록 없음', 'blocked': '조치 필요'}.get(state, safe_text(state))
                        lines.append(f'{label} · {description}')
                    if key == 'frontend_status' and item.get('deployment_seq') is not None:
                        lines.append('배포 순번 '+safe_text(item['deployment_seq'])+' · 빌드 '+safe_text(item.get('build_id')))
                    for field in ('reason', 'next_action'):
                        if item.get(field):
                            lines.append(safe_text(item[field]))
        lines.append(hardware_text(self.report))
        if self.report.get('hardware_output_reason'):
            lines.append(safe_text(self.report['hardware_output_reason']))
        if self.report.get('output_state') not in {None, 'ok'}:
            lines.append(topology_text(self.report))
        detail = self.query_one('#screens-detail', TextArea)
        content = '\n'.join(lines)
        if detail.text != content:
            detail.load_text(content)

    def move_allowed(self, target, output):
        if (not target or self.mutating or self.owner._active_job or self.owner._chrome_mutating
                or (self.owner._update_status or {}).get('state') in {'queued', 'running'}):
            return '다른 작업이 끝난 뒤 이동하세요.'
        if target.get('layout') != 'fullscreen':
            return '전체화면 DID만 드래그로 이동하거나 교환할 수 있습니다.'
        settings = target.get('settings') or {}
        if settings.get('pending') or settings.get('state') in {'queued', 'running'}:
            return '이 DID의 기존 설정 적용을 마친 뒤 이동하세요.'
        runtime = target.get('runtime') or {}
        if runtime.get('operation'):
            return '이 DID의 화면 작업이 끝난 뒤 이동하세요.'
        outputs = self.report.get('logical_outputs') or []
        if self.report.get('output_state') != 'ok' or not any(
                item.get('confirmed') and item['id'] == output for item in outputs):
            return '대상 모니터 연결을 확인하고 다시 조회하세요.'
        inventory = {'state': self.report.get('output_state'), 'logical_outputs': outputs}
        source = resolve_output(inventory, target.get('output'), auto=target['id'] == 'default')
        if source.get('state') != 'ok':
            return '현재 모니터 연결과 출력 배정을 먼저 확인하세요.'
        occupants = []
        for other in self.report.get('targets', []):
            if (other.get('settings') or {}).get('pending'):
                return '다른 DID의 설정 적용을 마친 뒤 모니터 배정을 다시 확인하세요.'
            if ((other.get('runtime') or {}).get('operation')
                    or ((other.get('swap') or {}).get('state') in {'queued', 'running', 'recovery_required', 'unknown'})):
                return '다른 화면 작업이 끝난 뒤 이동하세요.'
            if other['id'] == target['id']:
                continue
            resolved = resolve_output(inventory, other.get('output'), auto=other['id'] == 'default')
            if resolved.get('state') != 'ok':
                continue
            if resolved['output']['id'] == source['output']['id']:
                return '현재 모니터에 여러 DID가 배정돼 있습니다. 배정을 먼저 확인하세요.'
            if resolved['output']['id'] == output:
                occupants.append(other)
        if len(occupants) > 1 or any(other.get('layout') != 'fullscreen' for other in occupants):
            return '전체화면 DID 하나가 배정된 모니터와만 교환할 수 있습니다.'
        return ''

    def destination_target(self, target_id, output):
        inventory = {'state': self.report.get('output_state'), 'logical_outputs': self.report.get('logical_outputs') or []}
        for value in self.report.get('targets', []):
            resolved = resolve_output(inventory, value.get('output'), auto=value['id'] == 'default')
            if value['id'] != target_id and resolved.get('state') == 'ok' and resolved['output']['id'] == output:
                return value
        return None

    @on(ScreenMap.MoveRequested)
    def move_requested(self, event):
        event.stop()
        target = next((t for t in self.report.get('targets', []) if t['id'] == event.target_id), None)
        reason = self.move_allowed(target, event.output_id)
        if event.expected_config != self.owner._config:
            reason = '구성이 변경됐습니다. 다시 조회하고 이동하세요.'
        topology = event.expected_topology if event.expected_topology is not None else self.report.get('logical_outputs', [])
        if topology != self.report.get('logical_outputs', []):
            reason = '모니터 연결이 변경됐습니다. 다시 조회하고 이동하세요.'
        if reason:
            self.messages[event.target_id] = 'DID 이동 · 확인 필요 · ' + reason
            self.render_report()
            return
        other = deepcopy(self.destination_target(event.target_id, event.output_id))
        def confirmed(approved):
            if approved:
                self.owner.run_worker(
                    self.move_target(event.target_snapshot, event.output_id, event.expected_config, other=other, topology=topology),
                    group='screen-move', exit_on_error=False)
        self.owner.push_screen(MoveConfirmation(event.target_snapshot, event.output_id, other=other), confirmed)

    async def move_target(self, original, output, expected, *, other=None, topology=None):
        await self.refresh_state(wait=True)
        current = next((t for t in self.report.get('targets', []) if t['id'] == original['id']), None)
        reason = self.move_allowed(current, output)
        keys = ('id', 'url', 'output', 'layout', 'autostart', 'frontend')
        if current is None or any(current.get(key) != original.get(key) for key in keys):
            reason = '이 DID의 설정이 바뀌었습니다. 다시 조회하고 이동하세요.'
        if expected != self.owner._config:
            reason = '구성이 변경됐습니다. 다시 조회하고 이동하세요.'
        if topology is not None and topology != self.report.get('logical_outputs', []):
            reason = '모니터 연결이 변경됐습니다. 다시 조회하고 이동하세요.'
        current_other = self.destination_target(original['id'], output)
        if ((other is None) != (current_other is None) or other is not None and
                any((current_other or {}).get(key) != other.get(key) for key in keys)):
            reason = '대상 모니터의 DID가 변경됐습니다. 다시 확인하세요.'
        if reason:
            self.messages[original['id']] = 'DID 이동 · 확인 필요 · ' + reason
            self.render_report()
            return
        if other is not None:
            self.mutating = self.owner._chrome_mutating = True
            self.owner.update_button()
            try:
                result = await self.owner.service.call('screens-swap-submit', {
                    'source': original['id'], 'destination': other['id'],
                    'expected_config': expected, 'expected_topology': topology})
                for target_id in (original['id'], other['id']):
                    self.following.pop(target_id, None)
                    self.launches.pop(target_id, None)
                    self.messages[target_id] = self.result_message('두 화면 교환', result)
                    if result.get('operation_id'):
                        self.swap_following[target_id] = result['operation_id']
            except Exception as exc:
                self.messages[original['id']] = '두 화면 교환 · 확인 필요 · ' + safe_text(str(exc))
            finally:
                self.mutating = self.owner._chrome_mutating = False
                self.owner.update_button()
            self.selected = original['id']
            await self.refresh_state(wait=True)
            return
        value = {key: deepcopy(original.get(key)) for key in keys}
        value['output'] = output
        saved = await self.save_target(value, expected)
        if saved is not None:
            await self.perform('apply', target_id=original['id'],
                               expected_config=saved)

    @on(DataTable.RowHighlighted, '#screens-table')
    def highlighted(self, event):
        self.selected = event.row_key.value
        self.query_one('#screens-map', ScreenMap).update_report(
            self.report, self.selected, self.owner._config,
            enabled=not self.mutating and not self.owner._active_job)
        self.render_detail()

    @on(DataTable.RowSelected, '#screens-table')
    def selected_row(self, event):
        self.edit_target()

    @on(Button.Pressed)
    def pressed(self, event):
        key = (event.button.id or '').removeprefix('screens-')
        if key in {'edit', 'add'}:
            self.edit_target(new=key == 'add')
        elif key in {'identify', 'swap-recover'} and not event.button.disabled:
            self.owner.run_worker(self.session_action(key), group='screen-session-control', exclusive=True, exit_on_error=False)
        elif key in ACTIONS and not event.button.disabled:
            self.owner.run_worker(self.perform(key), group='screen-control', exclusive=False, exit_on_error=False)

    async def session_action(self, action):
        if self.mutating or self.owner._active_job:
            return
        self.mutating = True
        target = self.selected
        try:
            if action == 'identify':
                result = await self.owner.service.call('screens-identify')
                label = '모니터 식별'
            else:
                operation = (self.target() or {}).get('swap') or {}
                if not operation.get('operation_id'):
                    return
                result = await self.owner.service.call('screens-swap-retry', {'operation_id': operation['operation_id']})
                label = '두 화면 교환 복구'
                for participant in result.get('targets') or [target]:
                    self.following.pop(participant, None)
                    self.launches.pop(participant, None)
                    if result.get('operation_id'):
                        self.swap_following[participant] = result['operation_id']
            self.messages[target] = self.result_message(label, result)
        except Exception as exc:
            self.messages[target] = '요청 확인 필요 · ' + safe_text(str(exc))
        finally:
            self.mutating = False
        await self.refresh_state(wait=True)

    def edit_target(self, new=False):
        if self.mutating or self.query_one('#screens-add' if new else '#screens-edit', Button).disabled:
            return
        current = self.target()
        value = None if new else {k: current.get(k) for k in ('id', 'url', 'output', 'layout', 'autostart', 'frontend')} if current else None
        if not new and value is None:
            return
        expected = deepcopy(self.owner._config)
        outputs = [v['id'] for v in self.report.get('logical_outputs', []) if v.get('confirmed')]
        def saved(target):
            if target is not None:
                self.owner.run_worker(self.save_target(target, expected), exit_on_error=False)
        self.owner.push_screen(TargetEditor(value, outputs), saved)

    async def save_target(self, value, expected):
        if self.mutating:
            return None
        saved = None
        target = value['id']
        shown_target = target if self.target() and target == self.selected else self.selected
        self.mutating = True
        self.following.pop(shown_target, None)
        self.swap_following.pop(shown_target, None)
        self.launches.pop(shown_target, None)
        self.messages[shown_target] = '설정 저장 · 진행'
        self.render_report()
        try:
            response = await self.owner.service.call('screens-save', {'target': value, 'expected_config': expected})
            if response.get('saved') is not True:
                raise RuntimeError('설정 저장을 확인하지 못했습니다.')
            saved = deepcopy(response['config'])
            self.owner._config = deepcopy(saved)
            self.messages[target] = self.result_message('설정 저장', response, state='완료')
            if shown_target != target:
                self.messages.pop(shown_target, None)
            self.selected = target
        except Exception as exc:
            reason = '설정 저장 · 실패 · '+safe_text(str(exc))
            try:
                latest = await self.owner.service.call('config-show')
                self.owner._config = deepcopy(latest['config'])
                action = '설정을 다시 열고 현재 값을 확인한 뒤 저장하세요.'
            except Exception:
                action = '상태 확인 탭에서 r로 다시 조회한 뒤 설정을 저장하세요.'
            self.messages[shown_target] = reason+'\n'+action
        finally:
            self.mutating = False
        await self.refresh_state(wait=True)
        return saved

    @staticmethod
    def result_message(label, value, *, state=None):
        state = state or RESULT_LABELS.get(value.get('state')) or {
            0: '완료', 2: '대기',
        }.get(value.get('exit_code'), '실패')
        summary = ' · '.join(filter(None, (label, state, safe_text(value.get('reason') or ''))))
        return summary + ('\n'+safe_text(value['next_action']) if value.get('next_action') else '')

    async def perform(self, action, *, target_id=None, expected_config=None):
        target = target_id or self.selected
        current = next((t for t in self.report.get('targets', []) if t['id'] == target), None)
        if self.mutating or not current:
            return
        if target_id is None and self.query_one('#screens-'+action, Button).disabled:
            return
        if target_id is not None:
            settings = current.get('settings') or {}
            if (action != 'apply' or not settings.get('pending')
                    or settings.get('state') in {'queued', 'running'} or self.owner._active_job):
                return
        self.mutating = self.owner._chrome_mutating = True
        previous_pid = (current.get('service') or {}).get('MainPID')
        label = ACTIONS[action][0]
        self.owner.update_button()
        self.following.pop(target, None)
        self.swap_following.pop(target, None)
        self.launches.pop(target, None)
        self.messages[target] = label+' · 요청 중'
        self.render_report()
        try:
            command = {'apply': 'screens-apply', 'retry': 'frontend-retry'}.get(action, 'screens-action')
            payload = {'target': target, 'action': action} if command == 'screens-action' else {'target': target}
            if expected_config is not None and command == 'screens-apply':
                payload['expected_config'] = expected_config
            value = await self.owner.service.call(command, payload)
            if action == 'logs':
                self.messages[target] = self.result_message(label, value)+'\n'+safe_logs(value.get('log', ''))
            else:
                accepted = value.get('submitted') or (action == 'retry' and value.get('exit_code') == 2)
                self.messages[target] = self.result_message(label, value, state='접수' if accepted else None)
                if action in {'start', 'restart'} and value.get('submitted'):
                    self.launches[target] = (action, previous_pid)
                source, key = {'apply': ('settings', 'operation_id'), 'reload': ('reload', 'run_id')}.get(action, (None, None))
                if key and value.get('state') == 'queued' and value.get(key):
                    self.following[target] = (action, source, key, value[key])
        except Exception as exc:
            self.messages[target] = label+' · 실패 · '+safe_text(str(exc))
        finally:
            self.mutating = self.owner._chrome_mutating = False
            if self.owner.is_running:
                self.owner.update_button()
        await self.refresh_state()
