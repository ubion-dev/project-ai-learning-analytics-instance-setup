"""Display topology and per-target controls; opening this view never changes a host."""
from __future__ import annotations

import asyncio
from time import monotonic
from copy import deepcopy
from urllib.parse import urlsplit, urlunsplit

from rich.text import Text
from rich.cells import cell_len
from textual import events, on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, Input, Label, OptionList, Select, Static, Switch, TextArea
from textual.widgets.option_list import Option

from .chrome_tui import safe_logs, safe_text
from .screen_map import ScreenMap, output_mode_text, topology_text
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


class TargetEditor(ModalScreen[dict | None]):
    BINDINGS = [('escape', 'cancel', '취소')]
    CSS = """
    TargetEditor { align: center middle; background: #17161d; }
    #target-editor { width: 76; max-width: 96%; height: 34; max-height: 96%; padding: 0 1; background: #211f29; border: round #b5e9d9; }
    #target-title { height: 1; color: #cbc2ef; text-style: bold; }
    #target-result, #target-next { height: auto; max-height: 2; }
    #target-result { background: #2a2634; text-style: bold; }
    #target-primary { height: 3; width: 100%; margin: 0; }
    #target-fields { height: 1fr; }
    #target-editor Label { height: 1; margin-top: 1; }
    #target-editor Input, #target-editor Select { height: 3; }
    #target-error { height: auto; color: #f3b8c5; }
    #target-help { height: 1; color: #aabdd0; text-wrap: nowrap; text-overflow: ellipsis; }
    #target-buttons { height: 3; }
    #target-buttons Button { width: 1fr; margin-right: 1; }
    #target-editor Switch { height: 3; }
    #target-controls { height: 11; layout: grid; grid-size: 2 3; grid-rows: 3 3 3; grid-gutter: 1; }
    #target-controls Button { width: 1fr; min-width: 10; padding: 0; }
    #screens-apply, #screens-swap-recover { width: 100%; height: 3; }
    #target-detail-content { height: auto; }
    #screens-detail { height: 12; min-height: 4; }
    #target-operations, #target-details { height: auto; padding: 0; }
    """

    def __init__(self, target=None, outputs=(), *, panel=None, output_id=None):
        super().__init__()
        self.target = deepcopy(target) if target else None
        self.outputs = outputs
        self.panel = panel
        self.output_id = output_id
        self.target_id = target['id'] if target else None
        self._status_pending = False
        self._status_labels = {}
        self.expected_config = deepcopy(panel.owner._config) if panel else None

    def compose(self) -> ComposeResult:
        value = self.target or {'id': '', 'autostart': True, 'output': self.output_id}
        frontend = value.get('frontend') or {}
        current = value.get('output')
        options = [(safe_text(output), output) for output in self.outputs]
        if value['id'] == 'default':
            options.insert(0, ('자동 · 활성 논리 출력 하나', _AUTO_OUTPUT))
        if current and current not in self.outputs:
            options.append((safe_text(current) + ' · 기존 지정', current))
        with Vertical(id='target-editor'):
            yield Static('화면 설정 · '+safe_text(self.target_id or self.output_id or '새 화면'), id='target-title', markup=False)
            yield Static('', id='target-result', markup=False)
            yield Static('저장한 뒤 설정 적용을 선택하세요.', id='target-next', markup=False)
            yield Button('다음 행동', id='target-primary', variant='primary')
            with VerticalScroll(id='target-fields'):
                if self.panel and self.target_id:
                    yield Button('설정 적용', id='screens-apply')
                    with Collapsible(title='운영 · 시작·중지·로그', id='target-operations', collapsed=True):
                        with Vertical(id='target-controls'):
                            for key in ('start', 'stop', 'restart', 'reload', 'logs', 'retry'):
                                button = Button(ACTIONS[key][0], id='screens-'+key)
                                button.tooltip = ACTIONS[key][1]
                                yield button
                        yield Button('두 화면 교환 복구 재시도', id='screens-swap-recover')
                    with Collapsible(title='상태 상세·작업 로그', id='target-details', collapsed=True):
                        yield Vertical(id='target-detail-content')
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
                yield Label('로그인 후 DID 자동 시작 · 끄면 대기 안내')
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
                cancel = Button('닫기', id='target-cancel')
                cancel.tooltip = '이번 편집을 취소합니다.'
                yield cancel

    def on_mount(self):
        self.refresh_status()
        if self.panel and self.target_id:
            primary = self.query_one('#target-primary', Button)
            if primary.display:
                primary.focus()
            else:
                self.query_one('#target-operations CollapsibleTitle').focus()
        else:
            self.query_one('#target-url' if self.target else '#target-id', Input).focus()

    def dirty(self):
        if not self.is_mounted:
            return False
        value = self.target or {'id': '', 'autostart': True, 'output': self.output_id}
        frontend = value.get('frontend') or {}
        output = self.query_one('#target-output', Select).value
        return (self.query_one('#target-id', Input).value != value.get('id', '')
                or self.query_one('#target-url', Input).value != (value.get('url') or '')
                or (output if isinstance(output, str) and output else None) != value.get('output')
                or self.query_one('#target-layout', Select).value != value.get('layout', 'fullscreen')
                or self.query_one('#target-autostart', Switch).value != value.get('autostart', True)
                or self.query_one('#target-frontend', Switch).value != frontend.get('enabled', False)
                or self.query_one('#target-manifest', Input).value != (frontend.get('manifest_url') or ''))

    @on(Input.Changed)
    @on(Select.Changed)
    @on(Switch.Changed)
    def fields_changed(self):
        if not self._status_pending:
            self._status_pending = True
            self.call_later(self.refresh_status)

    @on(Collapsible.Expanded, '#target-details')
    async def details_expanded(self):
        if not self.query('#screens-detail'):
            await self.query_one('#target-detail-content').mount(
                TextArea('', id='screens-detail', read_only=True, show_cursor=False, highlight_cursor_line=False))
        self.refresh_status()

    def refresh_status(self):
        self._status_pending = False
        if not self.is_mounted:
            return
        primary = self.query_one('#target-primary', Button)
        if not self.panel or not self.target_id:
            primary.display = False
            return
        value = self.panel.find_target(self.target_id)
        headline, next_action, action, tone = self.panel.priority(value)
        message = self.panel.messages.get(self.target_id, '').split('\n', 1)[0]
        self.status_label('target-result', '\n'.join(filter(None, (headline, message))), tone)
        self.status_label('target-next', '다음 · ' + ('아래에서 사이트 주소를 저장한 뒤 설정 적용을 선택하세요.' if action == 'edit' else next_action))
        dirty = self.dirty()
        for key in ('start', 'stop', 'restart', 'reload', 'logs', 'retry', 'apply', 'swap-recover'):
            button = self.query_one('#screens-'+key, Button)
            enabled, reason = self.panel.action_state(key, value)
            if dirty and key != 'logs':
                enabled, reason = False, '편집 내용을 저장하거나 설정을 다시 열고 실행하세요.'
            if key == 'apply' and self.expected_config != self.panel.owner._config:
                enabled, reason = False, '구성이 바뀌었습니다. 설정을 다시 열고 확인하세요.'
            button.disabled = not enabled
            button.tooltip = reason
            button.variant = 'primary' if key == action and enabled else 'default'
            if key in {'apply', 'swap-recover'}:
                button.display = bool((value or {}).get('settings', {}).get('pending')) if key == 'apply' else bool(((value or {}).get('swap') or {}).get('state') in {'unknown', 'recovery_required'})
        editable, reason = self.panel.action_state('edit', value)
        self.query_one('#target-save', Button).disabled = not editable
        self.query_one('#target-save', Button).tooltip = reason if not editable else '설정 내용을 저장합니다. 화면 전환은 설정 적용으로 진행합니다.'
        primary_visible = False
        if action in {'start', 'logs', 'retry', 'apply'}:
            source = self.query_one('#screens-'+action, Button)
            primary_visible = not source.disabled
            primary.label = ACTIONS[action][0]
            primary.tooltip = source.tooltip
        primary.display = primary_visible
        if not self.query_one('#target-details', Collapsible).collapsed and self.query('#screens-detail'):
            detail = self.query_one('#screens-detail', TextArea)
            content = self.panel.details(self.target_id)
            if detail.text != content:
                detail.load_text(content)

    def status_label(self, key, content, style=''):
        if self._status_labels.get(key) != (content, style):
            self.query_one('#' + key, Static).update(Text(content, style=style))
            self._status_labels[key] = (content, style)

    def on_descendant_focus(self, event: events.DescendantFocus):
        widget = event.widget
        self.query_one('#target-help', Static).update(
            str(widget.tooltip or '') if isinstance(widget, Button) else '')

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
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
        elif self.panel and self.target_id:
            action = (event.button.id or '').removeprefix('screens-')
            if event.button.id == 'target-primary':
                action = self.panel.priority(self.panel.find_target(self.target_id))[2]
            if action == 'swap-recover':
                self.panel.owner.run_worker(self.panel.session_action(action, target_id=self.target_id), exit_on_error=False)
            elif action in {'start', 'stop', 'restart', 'reload', 'logs', 'retry', 'apply'}:
                # The editor keeps its DID identity even if the panel selection changes during a poll.
                if self.dirty() and action != 'logs':
                    return
                self.panel.owner.run_worker(self.panel.perform(action, target_id=self.target_id,
                    expected_config=self.expected_config if action == 'apply' else None), exit_on_error=False)
                if action == 'logs':
                    self.query_one('#target-details', Collapsible).collapsed = False

    def action_cancel(self):
        self.dismiss(None)


class TargetChooser(ModalScreen[str | None]):
    """Split layouts and disconnected DIDs remain explicitly selectable."""
    BINDINGS = [('escape', 'cancel', '닫기')]
    CSS = """
    TargetChooser { align: center middle; background: #000000 65%; }
    #target-chooser { width: 62; max-width: 96%; height: auto; max-height: 90%; padding: 1 2; background: #211f29; border: round #cbc2ef; }
    #target-choices { height: auto; max-height: 12; }
    #target-chooser Button { width: 100%; }
    """

    def __init__(self, output_id, targets):
        super().__init__()
        self.output_id, self.targets = output_id, targets

    def compose(self):
        with Vertical(id='target-chooser'):
            yield Static(safe_text(self.output_id)+' · 설정할 화면을 선택하세요.', markup=False)
            yield OptionList(*(Option(safe_text(t['id'])+' · '+safe_text(t.get('layout'))+' · '+site_label(t.get('url')), id=t['id']) for t in self.targets), id='target-choices')
            yield Button('닫기', id='chooser-cancel')

    def on_mount(self):
        self.query_one(OptionList).focus()

    @on(OptionList.OptionSelected)
    def chosen(self, event):
        self.dismiss(event.option.id)

    @on(Button.Pressed)
    def action_cancel(self):
        self.dismiss(None)


class ResetConfirmation(ModalScreen[bool]):
    BINDINGS = [('escape','cancel','취소')]
    CSS = """
    ResetConfirmation { align: center middle; background: #000000 65%; }
    #reset-dialog { width: 82; max-width: 96%; height: 90%; padding: 1 2; background: #211f29; border: round #cbc2ef; }
    #reset-title { height: 2; color: #f1d5a3; text-style: bold; }
    #reset-description { height: 1fr; }
    #reset-buttons { height: 3; }
    #reset-buttons Button { width: 1fr; margin-right: 1; }
    """
    def __init__(self,plan):
        super().__init__()
        self.plan=plan

    def compose(self):
        with Vertical(id='reset-dialog'):
            yield Static('DID 전체 초기화 · 변경 대상 확인',id='reset-title')
            before=self.plan.get('before',[])
            after=self.plan.get('after',[])
            lines=[f'기존 DID {len(before)}개 → 모니터별 안내 {len(after)}개',
                   '전체 화면 설정을 백업한 뒤 관리 Chrome을 중지합니다.',
                   '사이트·배정·자동 시작·프론트 연동을 초기화합니다.',
                   '계정·설치 환경·Chrome 프로필과 로그인 정보는 유지합니다.','', '현재 설정:']
            for target in before:
                lines.append(safe_text(target['id'])+' · '+site_label(target.get('url'))+' · '+safe_text(target.get('output') or '자동'))
            lines.extend(['','초기화 후:'])
            for target in after:
                lines.append(safe_text(target['id'])+' · '+safe_text(target['output'])+' · 안내·전체화면·자동 시작 켜짐·프론트 꺼짐')
            lines.extend(['','제외 DID: '+', '.join(self.plan.get('removed') or ['없음']),
                          '기존 수동 중지를 해제하고 안내 시작: '+', '.join(self.plan.get('manual_stops') or ['없음']),
                          '승인 이후 새로 요청한 수동 중지는 우선합니다.',
                          '중단·재부팅 시 기록된 결정으로 복구합니다.','완료된 초기화를 되돌리는 복원 버튼은 제공하지 않습니다.'])
            yield TextArea('\n'.join(lines),id='reset-description',read_only=True,show_cursor=False)
            with Horizontal(id='reset-buttons'):
                yield Button('취소',id='reset-cancel')
                yield Button('백업 후 전체 초기화',id='reset-confirm',variant='warning')

    def on_mount(self):
        self.query_one('#reset-cancel',Button).focus()

    def action_cancel(self):
        self.dismiss(False)

    @on(Button.Pressed)
    def pressed(self,event):
        event.stop()
        self.dismiss(event.button.id=='reset-confirm')


class ScreenPanel(Vertical):
    DEFAULT_CSS = """
    ScreenPanel { height: 1fr; }
    #screens-summary { height: 1; color: #b5e9d9; text-wrap: nowrap; text-overflow: ellipsis; }
    #screens-result, #screens-next { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    #screens-help { height: 1; color: #aabdd0; text-wrap: nowrap; text-overflow: ellipsis; }
    #screens-result { padding: 0 1; background: #2a2634; color: #b5e9d9; text-style: bold; }
    #screens-next { padding: 0 1; color: #e9f0f7; }
    #screens-map-scroll { height: 1fr; min-height: 3; }
    #screens-map { padding: 0 1; background: #211f29; border: none; }
    #screens-map:focus { background: #2a2634; }
    #screens-actions { height: 3; }
    #screens-actions Button { width: 1fr; margin-right: 1; min-width: 12; padding: 0; }
    #screens-unassigned { height: auto; padding: 0; }
    #screens-unassigned-list { height: auto; max-height: 5; }
    #screens-add { height: 3; width: 100%; }
    #screens-tools { height: auto; padding: 0; }
    #screens-tools Button { height: 3; width: 100%; }
    """

    def __init__(self, owner, **kwargs):
        super().__init__(**kwargs)
        self.owner = owner
        self.report = {}
        self.selected = None
        self.reading = False
        self._read_lock = asyncio.Lock()
        self.mutating = False
        self.identifying = False
        self.session_notice = None
        self.messages = {}
        self.following = {}
        self.swap_following = {}
        self.launches = {}
        self.loaded = False
        self._reset_follow = None
        self._reset_config_seen = None
        self.editor = None
        self._unassigned_rows = None

    def compose(self):
        yield Static('표시 대상 확인 중', id='screens-result', markup=False)
        yield Static('', id='screens-next', markup=False)
        yield Static('화면 관리를 열면 연결된 출력과 창을 조회합니다.', id='screens-summary', markup=False)
        with VerticalScroll(id='screens-map-scroll'):
            yield ScreenMap(id='screens-map')
            with Collapsible(title='관리 도구',id='screens-tools',collapsed=True):
                yield Button('DID 전체 초기화',id='screens-reset')
                yield Button('초기화 작업 복구',id='screens-reset-recover',disabled=True)
                yield Static('초기화 기록 없음',id='screens-reset-status',markup=False)
        with Collapsible(title='화면 추가·연결되지 않은 화면', id='screens-unassigned', collapsed=True):
            yield OptionList(id='screens-unassigned-list')
            yield Button('화면 추가', id='screens-add')
        yield Static('사이트 설정은 클릭·Enter · OS 배치는 모니터 설정에서 상하좌우 선택 · 배치도는 조회용', id='screens-help', markup=False)
        with Horizontal(id='screens-actions'):
            yield Button('모니터 배치·해상도', id='screens-monitor-layout')
            yield Button('모니터 식별', id='screens-identify')
            yield Button('다시 조회', id='screens-refresh')

    def on_mount(self):
        self.set_interval(3, self.poll)
        self.render_report()

    def on_resize(self):
        if self.is_mounted:
            self.call_after_refresh(self.render_report)

    def active(self):
        return self.is_mounted and self.owner.is_running and self.owner.query_one('#pages').current == 'screens'

    def activate(self):
        if self.reading:
            return
        if not self.identifying:
            self.session_notice = None
            self.render_report()
        self.run_worker(self.refresh_state(), group='screens-read', exclusive=True, exit_on_error=False)

    async def poll(self):
        if self.active() and not self.reading and not self.mutating and not self.identifying:
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
        started = monotonic()
        self.query_one('#screens-summary', Static).update('모니터·화면 상태 조회 중…')
        try:
            value = await self.owner.service.call('screens-inspect')
            if not isinstance(value, dict) or not isinstance(value.get('targets'), list):
                raise ValueError('잘못된 화면 조회 응답')
            self.report = value
            reset=value.get('reset') or {}
            if (reset.get('operation_id') and reset.get('state') in {'completed','rolled_back'}
                    and reset['operation_id']!=self._reset_config_seen):
                shown=await self.owner.service.call('config-show')
                self.owner._config=deepcopy(shown['config'])
                self._reset_config_seen=reset['operation_id']
            if reset.get('state') in {'queued','running','unknown','recovery_required'} or (self._reset_follow and reset.get('operation_id')==self._reset_follow):
                self.session_notice = dict(reset,label='DID 전체 초기화')
                if reset.get('state') in {'completed','rolled_back'}:
                    self._reset_follow=None
            self.loaded = True
        except Exception as exc:
            self.report = {'targets': [], 'reason': safe_text(str(exc)), 'state': 'unknown'}
        finally:
            self.query_seconds = monotonic()-started
            self.reading = False
        if self.is_mounted:
            self.render_report()

    def target(self):
        return self.find_target(self.selected)

    def find_target(self, target_id):
        return next((v for v in self.report.get('targets', []) if v.get('id') == target_id), None)

    def render_help(self):
        self.query_one('#screens-help', Static).update('사이트 설정은 클릭·Enter · OS 배치는 모니터 설정에서 상하좌우 선택 · 배치도는 조회용')

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
            f'{output_label}'
            + (f' · 조회 {self.query_seconds:.1f}초' if hasattr(self,'query_seconds') else '')
            + f' · 관리 대상 {len(targets)}개 · 확인된 창 {confirmed_label}'
            if self.loaded else '출력·창 조회 확인 필요 · '+str(self.report.get('reason', '조회 중'))))
        map_view = self.query_one('#screens-map', ScreenMap)
        map_view.update_report(self.report, self.selected)
        map_view.tooltip = '배치도는 조회용입니다. 클릭·Enter로 사이트 설정을 열고 OS 배치는 모니터 설정의 상하좌우 버튼을 사용하세요.'
        assigned = {target['id'] for output in self.report.get('logical_outputs') or []
                    for target in self.targets_on_output(output['id'])}
        unassigned = [target for target in targets if target['id'] not in assigned]
        rows = tuple((v['id'], self.priority(v)[0]) for v in unassigned)
        options = self.query_one('#screens-unassigned-list', OptionList)
        if rows != self._unassigned_rows:
            old = options.get_option_at_index(options.highlighted).id if options.highlighted is not None and options.option_count else None
            options.clear_options()
            options.add_options(Option(safe_text(key)+' · '+label, id=key) for key, label in rows)
            if old and old in {key for key, _ in rows}:
                options.highlighted = next(i for i, (key, _) in enumerate(rows) if key == old)
            self._unassigned_rows = rows
        options.display = bool(rows)
        section = self.query_one('#screens-unassigned', Collapsible)
        section.title = f'연결되지 않은 화면 {len(rows)}개 · 화면 추가' if rows else '화면 추가'
        self.query_one('#screens-identify', Button).disabled = not self.action_state('identify', self.target())[0]
        self.query_one('#screens-add', Button).disabled = not self.action_state('add', None)[0]
        reset=self.report.get('reset') or {}
        reset_text='초기화 기록 없음'
        if reset.get('operation_id'):
            reset_text='작업 '+safe_text(reset['operation_id'])+' · '+safe_text(reset.get('state'))+' · '+safe_text(reset.get('phase'))
            reset_text+='\n'+safe_text(reset.get('reason') or '')
            reset_text+='\n대상: '+', '.join(safe_text(t) for t in reset.get('targets',[]))
            if reset.get('schedule_warning'):
                reset_text+='\n'+safe_text(reset['schedule_warning'])
            reset_text+='\n백업: /var/lib/ala/display-resets/'+safe_text(reset['operation_id'])+'.json (관리자 전용)'
        self.query_one('#screens-reset-status',Static).update(reset_text)
        self.query_one('#screens-reset',Button).disabled = not self.action_state('add',None)[0]
        self.query_one('#screens-reset-recover',Button).disabled = self.mutating or (self.report.get('reset') or {}).get('state') not in {'unknown','recovery_required'}
        self.render_detail()
        if self.editor and self.editor.is_mounted:
            self.editor.refresh_status()

    def targets_on_output(self, output_id):
        inventory = {'state': self.report.get('output_state'), 'logical_outputs': self.report.get('logical_outputs') or []}
        result = []
        for value in self.report.get('targets') or []:
            resolved = resolve_output(inventory, value.get('output'), auto=value['id'] == 'default')
            if resolved.get('state') == 'ok' and resolved['output']['id'] == output_id:
                result.append(value)
        return result

    def action_state(self, key, value):
        """The same target-specific guard serves every entry point, including modals."""
        help_text = ACTIONS.get(key, ('', '작업 결과를 확인하세요.'))[1]
        locked = (self.mutating or self.identifying or bool(self.owner._active_job) or self.owner._chrome_mutating
                  or (self.owner._update_status or {}).get('state') in {'queued', 'running'})
        reset=(self.report.get('reset') or {}).get('state')
        if reset in {'queued','running','unknown','recovery_required'} and key not in {'stop','logs'}:
            return False, 'DID 전체 초기화·복구 결과를 확인하세요.'
        if locked:
            return False, '다른 작업이 끝나면 사용할 수 있습니다.'
        if key in {'add', 'identify'}:
            return True, help_text
        if not value:
            return False, '대상 상태를 다시 조회하세요.'
        swap = value.get('swap') or {}
        if key == 'swap-recover':
            return (bool(swap.get('operation_id')) and swap.get('state') in {'unknown', 'recovery_required'},
                    '기록된 확정 결정에 따라 두 화면의 일관된 배정 복구를 재시도합니다.')
        if swap.get('state') in {'queued', 'running', 'unknown', 'recovery_required'} and key not in {'stop', 'logs'}:
            return False, '두 화면 교환·복구 결과를 먼저 확인하세요.'
        settings = value.get('settings') or {}
        applying = settings.get('state') in {'queued', 'running'}
        if key == 'apply' and (not settings.get('pending') or applying):
            return False, '이 화면의 설정을 적용하고 있습니다.' if applying else settings.get('reason') or '먼저 변경한 설정을 저장하세요.'
        if key == 'retry' and not self.retry_enabled(value):
            return False, RETRY_DISABLED
        if applying and key in {'start', 'restart', 'reload', 'edit', 'retry'}:
            return False, '설정 적용이 끝나면 사용할 수 있습니다.'
        return True, help_text

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
        if (self.mutating or self.owner._active_job or value.get('id') in self.following
                or value.get('id') in self.launches or settings.get('state') in {'queued', 'running'}):
            return '작업 진행 중', '작업 결과를 확인하세요.', None, '#cbc2ef'
        if state == 'failed':
            return 'Chrome 실행 실패', '선택한 화면의 Chrome 로그를 확인하세요.', 'logs', '#f3b8c5'
        if state not in {'active', 'inactive', 'activating', 'deactivating'} or settings.get('state') == 'unknown':
            return '화면 상태 확인 불가', 'r로 다시 확인하고 화면 상세를 살펴보세요.', None, '#f1d5a3'
        if settings.get('state') in {'failed', 'interrupted'}:
            return '설정 적용 실패·중단', '선택한 화면의 작업 결과와 원인을 확인하세요.', None, '#f3b8c5'
        if settings.get('pending'):
            return '저장한 설정 적용 대기', '모니터를 열어 설정 적용을 선택하세요. 화면이 잠시 끊길 수 있습니다.', 'apply', '#f1d5a3'
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

    def render_detail(self, target_id=None, *, summary=True):
        target_id = target_id or self.selected
        value = self.find_target(target_id)
        settings = (value or {}).get('settings') or {}
        message = self.messages.get(target_id, '')
        move_rejection = 'DID 이동 · 확인 필요 · '
        rejected_move = message if message.startswith(move_rejection) else None
        follow = self.following.get(target_id)
        launch = self.launches.get(target_id)
        swap = (value or {}).get('swap') or {}
        if swap.get('operation_id') == self.swap_following.get(target_id) and swap.get('operation_id'):
            message = self.result_message('두 화면 교환', swap)
            self.messages[target_id] = message
            if swap.get('state') in {'completed', 'rolled_back'}:
                self.swap_following.pop(target_id, None)
        if follow and value:
            action, source, key, identifier = follow
            latest = value.get(source) or {}
            if latest.get(key) == identifier:
                message = self.result_message(ACTIONS[action][0], latest)
                self.messages[target_id] = message
                if latest.get('state') not in {'queued', 'running'}:
                    self.following.pop(target_id, None)
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
                self.messages[target_id] = message
                self.launches.pop(target_id, None)
        elif not message and settings.get('state') in RESULT_LABELS:
            message = self.result_message('설정 적용', settings)
        if rejected_move:
            # Keep tracking work in the background without hiding why the
            # operator's latest move request was rejected.
            message = rejected_move
            self.messages[target_id] = message
        headline, next_action, primary, tone = self.priority(value)
        parts = (target_id, headline, message.split('\n', 1)[0])
        if message.startswith(move_rejection):
            parts = (target_id, 'DID 이동 · 확인 필요')
            next_action = message[len(move_rejection):].split('\n', 1)[0]
            tone = '#f1d5a3'
        if summary and self.session_notice is not None:
            self.render_session_notice()
        elif summary:
            self.query_one('#screens-result', Static).update(Text(' · '.join(filter(None, parts)), style=tone))
            self.query_one('#screens-result', Static).tooltip = None
            self.query_one('#screens-next', Static).update('다음 · '+next_action)
            self.query_one('#screens-next', Static).tooltip = None
        lines = []
        if value:
            runtime = value.get('runtime') or {}
            def assignment(prefix=''):
                output_key = prefix+'output'
                output = (safe_text(value[output_key] or ('자동' if target_id == 'default' else '미배정'))
                          if output_key in value else '확인 불가')
                layout = {'fullscreen': '전체화면', 'left': '왼쪽 절반', 'right': '오른쪽 절반'}.get(
                    value.get(prefix+'layout'), '확인 불가')
                return output+' · 배치 '+layout
            lines += ['저장 주소 · '+site_label(value.get('url')),
                      '적용 주소 · '+site_label(value.get('applied_url', value.get('url'))),
                      '저장 출력 · '+assignment(),
                      '적용 출력 · '+assignment('applied_'),
                      '적용 출력·배치는 선택된 설정 기준입니다. 실제 창 위치는 창 배치 관측으로 확인하세요.',
                      '설정 · '+('적용 대기' if settings.get('pending') else '적용 확인 필요' if settings.get('state') == 'unknown' else '반영됨'),
                      '실제 사이트 · '+(site_label(value['actual_url']) if value.get('actual_url') else '적용 주소의 문서 확인' if site_confirmed(value) else '확인 대기'),
                      '자동 시작 · '+('켜짐' if value.get('autostart') else '꺼짐')]
            resolved = resolve_output(
                {'state': self.report.get('output_state'), 'logical_outputs': self.report.get('logical_outputs') or []},
                value.get('output'), auto=target_id == 'default')
            if resolved.get('state') == 'ok':
                logical = resolved['output']
                geometry = (f"{logical['x']},{logical['y']} {logical['width']}×{logical['height']}"
                            if all(type(logical.get(key)) is int for key in ('x', 'y', 'width', 'height'))
                            and logical['width'] > 0 and logical['height'] > 0 else '확인 불가')
                lines.append('모니터 · '+output_mode_text(self.report, logical)+' · 논리 영역 '+geometry)
            else:
                lines.append('모니터 · 저장 출력의 연결 정보 확인 불가')
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
        return '\n'.join(lines)

    def details(self, target_id):
        return self.render_detail(target_id, summary=False)

    def render_session_notice(self):
        """Session-wide identification never becomes an individual DID result."""
        notice = self.session_notice
        state = notice.get('state')
        success = state in {'ok', 'completed', 'ready', 'kept'}
        running = state in {'queued', 'running'}
        action = safe_text(notice.get('next_action') or (
            '실제 모니터에서 표시 번호를 확인하세요.' if success else
            '모니터 식별 결과를 기다리세요.' if running else
            '다시 조회한 뒤 모니터 식별을 다시 시도하세요.'))
        if notice.get('label')=='DID 전체 초기화' and not notice.get('next_action'):
            action=('모니터를 식별한 뒤 각 안내 화면의 사이트 주소를 설정하세요.' if success else
                    '초기화 작업의 진행 단계와 대상별 결과를 확인하세요.' if running else
                    '관리 도구에서 초기화 작업 상태와 복구 안내를 확인하세요.')
        full_action = action
        width = max(1, self.query_one('#screens-next', Static).content_size.width - cell_len('다음 · '))
        if cell_len(action) > width and '간편 설정' in action and '재로그인' in action:
            action = '간편 설정으로 확장 반영 후 GNOME 재로그인하세요.'
        tone = '#b5e9d9' if success else '#cbc2ef' if running else '#f3b8c5' if state in {'failed', 'error'} else '#f1d5a3'
        result = self.query_one('#screens-result', Static)
        headline=self.result_message(notice.get('label','모니터 식별'),notice).split('\n',1)[0]
        if notice.get('label')=='DID 전체 초기화' and state=='completed' and notice.get('execution_verified') is False:
            headline='DID 전체 초기화 · 배정 확정 · 실행 확인 대기'
            action='GNOME 로그인 후 모니터별 안내 화면과 빠른 점검을 확인하세요.'
            full_action=action
            tone='#f1d5a3'
        result.update(Text(headline,style=tone))
        result.tooltip = safe_text(notice.get('reason') or '')
        next_line = self.query_one('#screens-next', Static)
        next_line.update('다음 · '+action)
        next_line.tooltip = full_action

    @on(ScreenMap.OutputFocused)
    def output_focused(self, event):
        event.stop()
        targets = self.targets_on_output(event.output_id)
        if len(targets) == 1:
            self.selected = targets[0]['id']
            self.render_report()

    @on(ScreenMap.OutputActivated)
    def output_activated(self, event):
        event.stop()
        targets = self.targets_on_output(event.output_id)
        if not targets:
            if self.report.get('output_state') == 'ok' and any(
                    v.get('id') == event.output_id and v.get('confirmed') for v in self.report.get('logical_outputs') or []):
                self.edit_target(new=True, output_id=event.output_id)
            else:
                self.owner.notify('출력 상태를 확인한 뒤 화면을 추가하세요.', severity='warning')
        elif len(targets) == 1:
            self.open_target(targets[0]['id'])
        else:
            self.owner.push_screen(TargetChooser(event.output_id, targets),
                                   lambda target_id: self.open_target(target_id) if target_id else None)

    @on(OptionList.OptionSelected, '#screens-unassigned-list')
    def unassigned_selected(self, event):
        event.stop()
        self.open_target(event.option.id)

    @on(Button.Pressed)
    def pressed(self, event):
        key = (event.button.id or '').removeprefix('screens-')
        if key == 'monitor-layout' and not event.button.disabled:
            from .monitor_tui import MonitorEditor
            self.owner.push_screen(MonitorEditor(self))
        elif key == 'reset' and not event.button.disabled:
            self.owner.run_worker(self.review_reset(),group='reset-control',exclusive=True,exit_on_error=False)
        elif key == 'reset-recover' and not event.button.disabled:
            self.owner.run_worker(self.reset_request('screens-reset-recover',{'operation_id':(self.report.get('reset') or {}).get('operation_id')}),group='reset-control',exclusive=True,exit_on_error=False)
        elif key == 'add':
            self.edit_target(new=True)
        elif key == 'refresh':
            self.activate()
        elif key == 'identify' and not event.button.disabled:
            self.owner.run_worker(self.session_action(key), group='screen-session-control', exclusive=True, exit_on_error=False)

    async def review_reset(self):
        if self.mutating or not self.action_state('add',None)[0]:
            return
        self.mutating=True
        self.render_report()
        try:
            plan=await self.owner.service.call('screens-reset-preview')
            if plan.get('state')!='ready':
                self.session_notice=dict(plan,label='DID 전체 초기화')
                return
            def selected(approved):
                if approved:
                    self.owner.run_worker(self.reset_request('screens-reset-submit',{'review':plan['review']}),group='reset-control',exclusive=True,exit_on_error=False)
            self.owner.push_screen(ResetConfirmation(plan),selected)
        except Exception as exc:
            self.session_notice={'label':'DID 전체 초기화','state':'unknown','reason':safe_text(str(exc))}
        finally:
            self.mutating=False
            self.render_report()

    async def reset_request(self,action,payload):
        if self.mutating:
            return
        self.mutating=True
        self.render_report()
        try:
            result=await self.owner.service.call(action,payload)
            self._reset_follow=result.get('operation_id')
            self.session_notice=dict(result,label='DID 전체 초기화')
        except Exception as exc:
            self.session_notice={'label':'DID 전체 초기화','state':'unknown','reason':safe_text(str(exc))}
        finally:
            self.mutating=False
            await self.refresh_state(wait=True)

    async def session_action(self, action, *, target_id=None):
        target = target_id or self.selected
        if not self.action_state(action, self.find_target(target))[0]:
            return
        if action == 'identify':
            self.identifying = True
            self.session_notice = {'state': 'running', 'reason': '모니터 식별을 요청하고 있습니다.'}
            self.render_report()
            try:
                result = await self.owner.service.call('screens-identify')
                if not isinstance(result, dict):
                    raise ValueError('모니터 식별 응답을 확인하지 못했습니다.')
                self.session_notice = deepcopy(result)
            except Exception as exc:
                self.session_notice = {'state': 'unknown', 'reason': safe_text(str(exc)),
                                       'next_action': '다시 조회한 뒤 모니터 식별을 다시 시도하세요.'}
            finally:
                self.identifying = False
                self.render_report()
            await self.refresh_state(wait=True)
            return
        self.session_notice = None
        self.mutating = True
        self.render_report()
        try:
            operation = (self.find_target(target) or {}).get('swap') or {}
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

    def open_target(self, target_id):
        if self.find_target(target_id):
            self.selected = target_id
            self.render_report()
            self.edit_target()

    def edit_target(self, new=False, output_id=None):
        if new and not self.action_state('add', None)[0]:
            return
        current = self.target()
        value = None if new else deepcopy(current)
        if not new and value is None:
            return
        expected = deepcopy(self.owner._config)
        outputs = [v['id'] for v in self.report.get('logical_outputs', []) if v.get('confirmed')]
        def saved(target):
            self.editor = None
            if target is not None:
                self.owner.run_worker(self.save_target(target, expected), exit_on_error=False)
            self.query_one('#screens-map', ScreenMap).focus()
        self.editor = TargetEditor(value, outputs, panel=self, output_id=output_id)
        self.owner.push_screen(self.editor, saved)

    async def save_target(self, value, expected):
        if self.mutating or self.identifying:
            return None
        self.session_notice = None
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
        if action not in {'start', 'stop', 'restart', 'reload', 'logs', 'retry', 'apply'} or not self.action_state(action, current)[0]:
            return
        self.session_notice = None
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
