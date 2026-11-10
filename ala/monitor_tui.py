"""Directional monitor settings; the observed layout is shown on the main tab."""
from copy import deepcopy

from rich.text import Text
from textual import on
from textual.message_pump import MessagePump
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, Label, Select, Static, Switch, TextArea

from .chrome_tui import safe_text
from .monitor_draft import LayoutDraft


class LayoutReview(ModalScreen[bool]):
    BINDINGS = [('escape', 'cancel', '취소')]
    CSS = '''
    LayoutReview { align: center middle; background: #000000 65%; }
    #layout-review { width: 76; max-width: 96%; height: 80%; padding: 1 2; background: #211f29; border: round #cbc2ef; }
    #layout-review-text { height: 1fr; }
    #layout-review-buttons { height: 3; }
    #layout-review-buttons Button { width: 1fr; }
    '''
    def __init__(self, draft, persistent):
        super().__init__(); self.draft = draft; self.persistent = persistent

    def compose(self):
        before = {o['id']: o for o in self.draft.settings['edit_template']['outputs']}
        plan = self.draft.validate()
        lines = ['모니터 배치 변경', '각 사이트는 기존 출력에 유지됩니다.',
                 '적용 뒤 화면을 확인하고 30초 안에 유지 또는 복원을 선택하세요.',
                 '확인하지 않으면 원래 배치로 복원을 요청합니다.',
                 '확정한 배치를 저장하고 다음 로그인에 다시 적용합니다.' if self.persistent else '현재 세션에서만 시험합니다.', '']
        for item in plan['outputs']:
            old = before[item['id']]
            lines.extend([item['id'], f"위치 ({old['x']}, {old['y']}) → ({item['x']}, {item['y']})",
                          f"모드 {old['mode_id']} → {item['mode_id']}",
                          f"화면 배율 {old['scale'] * 100:g}% → {item['scale'] * 100:g}%", ''])
        with Vertical(id='layout-review'):
            yield TextArea('\n'.join(lines), id='layout-review-text', read_only=True, show_cursor=False)
            with Horizontal(id='layout-review-buttons'):
                yield Button('취소', id='layout-review-cancel')
                yield Button('배치 적용', id='layout-review-apply', variant='primary')

    def on_mount(self): self.query_one('#layout-review-cancel', Button).focus()
    def action_cancel(self): self.dismiss(False)
    @on(Button.Pressed)
    def pressed(self, event):
        event.stop(); self.dismiss(event.button.id == 'layout-review-apply')


class MonitorSettings(MessagePump):
    inline = False
    BINDINGS = [('escape', 'close_editor', '닫기')]
    CSS = '''
    MonitorEditor { align: center middle; background: #17161d; }
    #monitor-editor { width: 96%; height: 96%; padding: 0 1; background: #211f29; border: round #cbc2ef; }
    #monitor-title, #monitor-selected { height: 1; }
    #monitor-message { height: auto; max-height: 3; color: #f1d5a3; }
    #monitor-settings { height: 1fr; }
    #monitor-fields, #monitor-anchor-row, #monitor-directions, #monitor-buttons, #monitor-confirm-buttons, #monitor-options { height: 3; }
    #monitor-port { width: 22; }
    #monitor-mode { width: 1fr; }
    #monitor-scale { width: 15; }
    #monitor-anchor-row Label { width: 16; height: 3; content-align: left middle; }
    #monitor-anchor { width: 1fr; }
    #monitor-options Label { width: 23; height: 3; content-align: left middle; }
    #monitor-options Switch { width: 8; }
    #monitor-options Button, #monitor-directions Button { width: 1fr; padding: 0; min-width: 10; }
    #monitor-buttons Button, #monitor-confirm-buttons Button { width: 1fr; min-width: 10; padding: 0; }
    #monitor-profile { height: auto; padding: 0; }
    '''

    def __init__(self, panel):
        super().__init__(); self.panel = panel; self.service = panel.owner.service
        self.draft = None; self.syncing = False; self.busy = False; self.operation = None
        self.operation_state = None; self.polling = False; self.persistent = True
        self.trial_supported = False
        self._select_options = {}

    def compose(self):
        with Vertical(id='monitor-editor'):
            yield Static('모니터 설정 · 변경은 검토 후 적용', id='monitor-title')
            yield Static('지원 모드를 조회하고 있습니다.', id='monitor-message', markup=False)
            yield Static('', id='monitor-selected', markup=False)
            with VerticalScroll(id='monitor-settings'):
                with Horizontal(id='monitor-fields'):
                    yield Select([], prompt='모니터', id='monitor-port')
                    yield Select([], prompt='해상도·주사율', id='monitor-mode')
                    yield Select([], prompt='화면 배율', id='monitor-scale')
                with Horizontal(id='monitor-anchor-row'):
                    yield Label('기준 모니터')
                    yield Select([], prompt='다른 모니터 선택', id='monitor-anchor')
                if self.inline:
                    with Grid(id='monitor-remote'):
                        yield Static('')
                        yield Button('↑ 위', id='monitor-above', disabled=True, compact=True)
                        yield Static('')
                        yield Button('← 왼쪽', id='monitor-left', disabled=True, compact=True)
                        yield Static('기준 대비', id='monitor-remote-center')
                        yield Button('오른쪽 →', id='monitor-right', disabled=True, compact=True)
                        yield Static('')
                        yield Button('아래 ↓', id='monitor-below', disabled=True, compact=True)
                        yield Static('')
                else:
                    with Horizontal(id='monitor-directions'):
                        for relation, label in [('left', '← 왼쪽'), ('right', '오른쪽 →'), ('above', '↑ 위'), ('below', '아래 ↓')]:
                            yield Button(label, id='monitor-' + relation, disabled=True)
                with Horizontal(id='monitor-options'):
                    yield Label('다음 로그인에도 유지')
                    yield Switch(True, id='monitor-persist')
                    yield Button('이 모니터의 사이트', id='monitor-site')
                    yield Button('모니터 식별', id='monitor-identify')
                yield Button('모니터 전체 기본값 초안', id='monitor-reset')
                with Collapsible(title='로그인 배치 복원', id='monitor-profile', collapsed=True):
                    yield Button('저장 배치 로그인 복원 끄기', id='monitor-profile-disable')
                    yield Button('저장 배치 복원 재시도', id='monitor-profile-retry')
            with Horizontal(id='monitor-confirm-buttons'):
                yield Button('이 배치 유지', id='monitor-keep', variant='primary')
                yield Button('원래 배치로 복원', id='monitor-revert')
            with Horizontal(id='monitor-buttons'):
                yield Button('확장 갱신', id='monitor-extension')
                yield Button('다시 조회', id='monitor-refresh')
                yield Button('편집 취소', id='monitor-undo')
                yield Button('변경 검토', id='monitor-review', variant='primary')
                yield Button('접기' if self.inline else '닫기', id='monitor-close')

    async def on_mount(self):
        self.query_one('#monitor-confirm-buttons').display = False
        self.set_interval(1, self.poll)
        await self.load()
        self.call_after_refresh(self.update_controls)

    def message(self, value): self.query_one('#monitor-message', Static).update(safe_text(value))

    async def load(self):
        if self.busy or self.operation:
            return
        self.busy = True
        try:
            operation = await self.service.call('monitor-layout-status')
            if operation.get('state') not in {'none', 'confirmed', 'rolled_back', 'not_applied'}:
                self.operation = operation.get('operation_id'); self.operation_state = operation.get('state')
                self.message(operation.get('reason') or '진행·복구 상태를 확인하세요.'); self.update_controls(); return
            settings = await self.service.call('monitor-layout-state')
            self.trial_supported = bool(settings.get('trial_supported'))
            self.draft = LayoutDraft(settings)
            self.query_one('#monitor-extension', Button).display = not settings.get('trial_supported')
            self.message('대상과 기준 모니터를 선택하고 상하좌우 버튼으로 초안을 변경하세요.' if settings.get('trial_supported')
                         else '배치 적용 기능이 있는 확장으로 갱신하세요. 사이트 설정은 유지합니다.')
            self.sync_fields()
        except Exception as exc: self.message(str(exc))
        finally:
            self.busy = False; self.update_controls()

    def sync_fields(self):
        with self.prevent(Select.Changed):
            self._sync_fields()

    def _sync_fields(self):
        if not self.draft:
            return
        self.syncing = True
        try:
            output = self.draft.output(); modes = self.draft.modes(); mode = self.draft.mode()
            self.sync_select('monitor-port',
                [(f'#{i + 1} {key}', key) for i, key in enumerate(sorted(o['id'] for o in self.draft.request['outputs']))],
                self.draft.selected)
            self.sync_select('monitor-mode',
                [(f"{m['width']}×{m['height']} · {m['refresh_hz']:g} Hz" + (' · 권장' if m['preferred'] else ''), m['id']) for m in modes],
                output['mode_id'])
            # JSON may carry 1 where Mutter advertises 1.0.
            scale = Select.NULL if output['scale'] is None else str(next(s for s in mode['supported_scales'] if s == output['scale']))
            self.sync_select('monitor-scale',
                [(f'{s * 100:g}%', str(s)) for s in mode['supported_scales']], scale)
            anchors = [key for key in sorted(o['id'] for o in self.draft.request['outputs']) if key != self.draft.selected]
            current_anchor = self.query_one('#monitor-anchor', Select).value
            self.sync_select('monitor-anchor', [(key, key) for key in anchors],
                             current_anchor if current_anchor in anchors else anchors[0] if anchors else Select.NULL)
            targets = self.panel.targets_on_output(self.draft.selected)
            sites = ', '.join(t['id'] for t in targets) or '사이트 미설정'
            self.query_one('#monitor-selected', Static).update(safe_text(self.draft.selected + ' · ' + sites))
        finally:
            self.syncing = False
        self.update_controls()

    def sync_select(self, key, options, value):
        """Reuse mounted choices when only selection or coordinates changed."""
        widget = self.query_one('#' + key, Select)
        options = tuple(options)
        if self._select_options.get(key) != options:
            widget.set_options(options)
            self._select_options[key] = options
        if widget.value != value:
            widget.value = value

    def update_controls(self):
        if not self.is_mounted:
            return
        editable = bool(self.draft) and not self.busy and not self.operation
        for key in ('monitor-port', 'monitor-mode', 'monitor-scale', 'monitor-persist', 'monitor-anchor', 'monitor-site', 'monitor-identify', 'monitor-review', 'monitor-undo', 'monitor-refresh', 'monitor-extension',
                    'monitor-profile-disable', 'monitor-profile-retry', 'monitor-reset'):
            self.query_one('#' + key).disabled = not editable if key not in {'monitor-refresh', 'monitor-extension'} else bool(self.busy or self.operation)
        anchor = self.query_one('#monitor-anchor', Select).value
        valid_anchor = (self.draft is not None and isinstance(anchor, str) and anchor != self.draft.selected
                        and anchor in {o['id'] for o in self.draft.request['outputs']})
        for relation in ('left', 'right', 'above', 'below'):
            self.query_one('#monitor-' + relation, Button).disabled = not editable or not valid_anchor
        if self.draft:
            try:
                self.draft.validate()
            except ValueError as exc:
                self.query_one('#monitor-review', Button).disabled = True
                if editable: self.message(str(exc))
            if not self.trial_supported:
                self.query_one('#monitor-review', Button).disabled = True
        waiting = self.operation_state == 'awaiting_confirmation'
        self.query_one('#monitor-confirm-buttons').display = waiting or self.operation_state == 'recovery_required'
        self.query_one('#monitor-keep', Button).disabled = not waiting or self.busy
        self.query_one('#monitor-revert', Button).disabled = self.busy
        self.query_one('#monitor-revert', Button).label = '복구 재시도' if self.operation_state == 'recovery_required' else '원래 배치로 복원'
        if self.inline: self.panel.render_report()

    @on(Select.Changed)
    def selected(self, event):
        if event.select.id == 'monitor-anchor':
            self.update_controls()
            return
        if self.syncing or self.busy or self.operation or not self.draft or event.value is Select.NULL:
            return
        if event.select.id == 'monitor-port':
            if event.value == self.draft.selected: return
            self.draft.selected = event.value
        elif event.select.id == 'monitor-mode':
            if event.value == self.draft.output()['mode_id']: return
            self.draft.set_mode(event.value)
        elif event.select.id == 'monitor-scale':
            if float(event.value) == self.draft.output()['scale']: return
            self.draft.set_scale(float(event.value))
        self.sync_fields()

    async def poll(self):
        if not self.operation or self.polling or self.busy:
            return
        self.polling = True
        try:
            result = await self.service.call('monitor-layout-status')
            if result.get('operation_id') != self.operation:
                raise RuntimeError('배치 작업이 변경됐습니다. 다시 조회하세요.')
            self.operation_state = result.get('state')
            self.message(result.get('reason') or self.operation_state)
            if self.operation_state in {'confirmed', 'rolled_back', 'not_applied'}:
                message = result.get('reason') or ''
                self.operation = None
                await self.panel.refresh_state(wait=True)
                await self.load()
                self.message(message)
        except Exception as exc: self.message(str(exc))
        finally:
            self.polling = False; self.update_controls()

    async def apply(self, accepted, request, persistent):
        if not accepted or self.operation or self.busy:
            return
        self.busy = True; self.update_controls()
        try:
            result = await self.service.call('monitor-layout-submit', {'layout': request, 'persistent': persistent})
            self.operation = result.get('operation_id'); self.operation_state = result.get('state')
            self.message(result.get('reason') or '배치 적용 상태를 확인하세요.')
        except Exception as exc: self.message(str(exc))
        finally:
            self.busy = False; self.update_controls()

    @on(Button.Pressed)
    async def pressed(self, event):
        event.stop()
        key = event.button.id
        if key == 'monitor-close':
            await self.action_close_editor(); return
        if self.busy: return
        if key == 'monitor-refresh': await self.load()
        elif key == 'monitor-extension':
            self.busy = True; self.update_controls()
            note = ''
            try:
                result = await self.service.call('monitor-extension-refresh')
                note = result.get('reason') or '확장 준비 후 GNOME에서 다시 로그인하세요.'
            except Exception as exc: note = str(exc)
            finally: self.busy = False
            await self.load()
            self.message(note)
        elif key in {'monitor-keep', 'monitor-revert'} and self.operation:
            self.busy = True; self.update_controls()
            try:
                action = 'recover' if self.operation_state == 'recovery_required' else 'confirm' if key == 'monitor-keep' else 'revert'
                result = await self.service.call('monitor-layout-' + action, {'operation_id': self.operation})
                self.message(result.get('reason') or '배치 결과를 확인하고 있습니다.')
            except Exception as exc: self.message(str(exc))
            finally: self.busy = False; self.update_controls()
        elif not self.operation and self.draft:
            if key in {'monitor-profile-disable', 'monitor-profile-retry'}:
                self.busy = True; self.update_controls()
                try:
                    result = await self.service.call(key)
                    fallback = '로그인 복원을 껐습니다.' if key.endswith('disable') else '저장 배치 복원 상태를 확인하세요.'
                    self.message(result.get('reason') or fallback)
                    if result.get('operation_id'):
                        self.operation = result['operation_id']; self.operation_state = result.get('state')
                except Exception as exc: self.message(str(exc))
                finally: self.busy = False; self.update_controls()
            elif key == 'monitor-undo':
                self.draft = LayoutDraft(self.draft.settings)
                self.sync_fields()
            elif key == 'monitor-reset':
                try:
                    self.draft.reset_defaults()
                    self.sync_fields()
                    self.message('전체 기본값 초안: 권장 해상도·100% 배율·가로 정렬. 사이트는 보존하며 검토 후 적용합니다.')
                except ValueError as exc: self.message(str(exc))
            elif key in {'monitor-left', 'monitor-right', 'monitor-above', 'monitor-below'}:
                relation = key.removeprefix('monitor-')
                anchor = self.query_one('#monitor-anchor', Select).value
                try:
                    self.draft.place(self.draft.selected, relation, anchor)
                    self.sync_fields()
                    words = {'left': '왼쪽', 'right': '오른쪽', 'above': '위', 'below': '아래'}
                    self.message(f'{self.draft.selected}를 {anchor}의 {words[relation]}에 배치하는 초안입니다. 변경 검토 후 적용하세요.')
                except ValueError as exc: self.message(str(exc))
            elif key == 'monitor-identify': await self.panel.session_action('identify')
            elif key == 'monitor-site':
                targets = self.panel.targets_on_output(self.draft.selected)
                if len(targets) == 1: self.panel.open_target(targets[0]['id'])
                elif not targets: self.panel.edit_target(new=True, output_id=self.draft.selected)
                else:
                    from .screens_tui import TargetChooser
                    self.app.push_screen(TargetChooser(self.draft.selected, targets), lambda key: self.panel.open_target(key) if key else None)
            elif key == 'monitor-review':
                try:
                    self.draft.validate()
                    request = deepcopy(self.draft.request)
                    persistent = self.query_one('#monitor-persist', Switch).value
                    self.app.push_screen(LayoutReview(self.draft, persistent),
                        lambda accepted: self.run_worker(self.apply(accepted, request, persistent), exit_on_error=False))
                except ValueError as exc: self.message(str(exc))

    async def action_close_editor(self):
        if self.busy:
            return
        if self.operation and self.operation_state == 'awaiting_confirmation':
            try: await self.service.call('monitor-layout-revert', {'operation_id': self.operation})
            except Exception: pass  # The detached worker still owns its deadline.
        if self.inline:
            self.display = False
            self.panel.query_one('#screens-monitor-layout', Button).focus()
            self.panel.render_report()
        else:
            self.dismiss()


class MonitorEditor(MonitorSettings, ModalScreen):
    BINDINGS = MonitorSettings.BINDINGS
    CSS = MonitorSettings.CSS


class MonitorControls(MonitorSettings, Vertical):
    """Lazy inline editor sharing the reviewed monitor trial lifecycle."""
    inline = True
    BINDINGS = MonitorSettings.BINDINGS
    DEFAULT_CSS = MonitorSettings.CSS + '''
    MonitorControls { height: auto; }
    MonitorControls #monitor-editor { width: 100%; height: auto; padding: 0 1; }
    MonitorControls #monitor-settings { height: auto; }
    MonitorControls #monitor-remote { height: 3; layout: grid; grid-size: 3 3; grid-rows: 1 1 1; }
    MonitorControls #monitor-remote Button { width: 1fr; height: 1; min-height: 1; min-width: 10; padding: 0; border: none; }
    MonitorControls #monitor-remote-center { content-align: center middle; height: 1; }
    MonitorControls #monitor-reset { width: 100%; height: 3; }
    '''

    async def choose_output(self, output_id):
        if not self.draft and not self.busy and not self.operation:
            await self.load()
        if self.draft and output_id in {o['id'] for o in self.draft.request['outputs']}:
            self.draft.selected = output_id
            self.sync_fields()
