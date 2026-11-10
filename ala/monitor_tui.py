"""Spatial monitor editor; site editing reuses the existing DID settings flow."""
from copy import deepcopy
from itertools import groupby

from rich.text import Text
from textual import on
from textual.actions import SkipAction
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, Input, Label, Select, Static, Switch, TextArea

from .chrome_tui import safe_text
from .monitor_draft import LayoutDraft, Projection


class MonitorCanvas(Static, can_focus=True):
    DEFAULT_CSS = 'MonitorCanvas { height: 1fr; min-height: 7; background: #181722; padding: 0; } MonitorCanvas:focus { background: #242032; }'
    BINDINGS = [('left', 'move(-1,0)', '왼쪽'), ('right', 'move(1,0)', '오른쪽'),
                ('up', 'move(0,-1)', '위'), ('down', 'move(0,1)', '아래'), ('escape', 'stop_drag', '드래그 취소')]

    class Selected(Message): pass
    class Changed(Message):
        def __init__(self, reason=''):
            super().__init__(); self.reason = reason

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.draft = None
        self.editable = True
        self.projection = None
        self.drag = None

    def set_draft(self, draft):
        self.draft = draft
        self.drag = None
        self.refresh()

    def on_resize(self): self.refresh()

    def render(self):
        if not self.draft:
            return Text('모니터 정보를 조회하고 있습니다.')
        width, height = max(8, self.content_size.width), max(4, self.content_size.height)
        if not self.drag:
            self.projection = Projection(self.draft.rects(), width, height)
        p = self.projection
        grid = [[(' ', '') for _ in range(width)] for _ in range(height)]
        numbers = {key: i + 1 for i, key in enumerate(sorted(p.regions))}
        for key, box in p.regions.items():
            x, y, w, h = box
            style = '#b5e9d9' if key == self.draft.selected else '#aabdd0'
            if self.drag and self.drag.get('moving'):
                if key == self.drag['source']:
                    x, y = self.drag['left'], self.drag['top']
                    style = 'bold #cbc2ef'
                elif key == self.drag.get('target'):
                    style = 'bold #f1d5a3'
            lines = ['┌' + '─' * (w - 2) + '┐']
            lines += ['│' + ' ' * (w - 2) + '│' for _ in range(h - 2)]
            lines += ['└' + '─' * (w - 2) + '┘']
            label = f'#{numbers[key]} {key}'
            lines[1] = '│' + label[:w - 2].center(w - 2) + '│'
            for row, line in enumerate(lines):
                for col, char in enumerate(line):
                    if 0 <= x + col < width and 0 <= y + row < height:
                        grid[y + row][x + col] = (char, style)
        text = Text()
        for row, line in enumerate(grid):
            for style, cells in groupby(line, key=lambda cell: cell[1]):
                text.append(''.join(char for char, _ in cells), style)
            if row < height - 1: text.append('\n')
        return text

    def point(self, event):
        return event.screen_x - self.content_region.x, event.screen_y - self.content_region.y

    def on_mouse_down(self, event):
        if event.button != 1 or not self.editable or not self.draft or not self.projection:
            return
        x, y = self.point(event)
        source = self.projection.hit(x, y)
        if source:
            self.focus(); self.draft.selected = source
            left, top, _, _ = self.projection.regions[source]
            self.drag = {'source': source, 'x': x, 'y': y, 'left': left, 'top': top,
                         'dx': x - left, 'dy': y - top, 'moving': False, 'target': None}
            self.capture_mouse(); self.post_message(self.Selected()); self.refresh(); event.stop()

    def on_mouse_move(self, event):
        if not self.drag:
            return
        x, y = self.point(event)
        d = self.drag
        if abs(x - d['x']) + abs(y - d['y']) >= 2:
            d['moving'] = True
        if d['moving']:
            d.update(left=x - d['dx'], top=y - d['dy'], target=self.projection.hit(x, y, exclude=d['source']))
            self.refresh()
        event.stop()

    def on_mouse_up(self, event):
        if not self.drag:
            return
        x, y = self.point(event)
        d, p = self.drag, self.projection
        self.drag = None; self.release_mouse()
        reason = ''
        if d['moving'] and 0 <= x < self.content_size.width and 0 <= y < self.content_size.height:
            original = self.draft.output(d['source'])
            wx = original['x'] + round((x - d['x']) / p.factor)
            wy = original['y'] + round((y - d['y']) * 2 / p.factor)
            try:
                self.draft.drop(d['source'], wx, wy, p.hit(x, y, exclude=d['source']), snap=max(120, round(1.5 / p.factor)))
            except ValueError as exc: reason = str(exc)
            self.post_message(self.Changed(reason))
        self.refresh(); event.stop()

    def action_stop_drag(self):
        if not self.drag:
            raise SkipAction()
        self.drag = None; self.release_mouse(); self.refresh()

    def action_move(self, dx, dy):
        if not self.editable or not self.draft:
            return
        reason = ''
        try: self.draft.nudge(dx, dy)
        except ValueError as exc: reason = str(exc)
        self.post_message(self.Changed(reason)); self.refresh()


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


class MonitorEditor(ModalScreen):
    BINDINGS = [('escape', 'close_editor', '닫기')]
    CSS = '''
    MonitorEditor { align: center middle; background: #17161d; }
    #monitor-editor { width: 96%; height: 96%; padding: 0 1; background: #211f29; border: round #cbc2ef; }
    #monitor-title, #monitor-selected { height: 1; }
    #monitor-message { height: auto; max-height: 3; color: #f1d5a3; }
    #monitor-guide { height: 1; color: #aabdd0; text-wrap: nowrap; text-overflow: ellipsis; }
    #monitor-fields, #monitor-buttons, #monitor-confirm-buttons, #monitor-options, #monitor-coordinate-row { height: 3; }
    #monitor-port { width: 22; }
    #monitor-mode { width: 1fr; }
    #monitor-scale { width: 15; }
    #monitor-options Label { width: 23; height: 3; content-align: left middle; }
    #monitor-options Switch { width: 8; }
    #monitor-options Button { width: 1fr; padding: 0; min-width: 10; }
    #monitor-buttons Button, #monitor-confirm-buttons Button { width: 1fr; min-width: 10; padding: 0; }
    #monitor-coordinates { height: auto; padding: 0; }
    #monitor-coordinate-row Input { width: 1fr; }
    #monitor-coordinate-row Button { width: 18; }
    '''

    def __init__(self, panel):
        super().__init__(); self.panel = panel; self.service = panel.owner.service
        self.draft = None; self.syncing = False; self.busy = False; self.operation = None
        self.operation_state = None; self.polling = False; self.persistent = True
        self.trial_supported = False
        self._select_options = {}

    def compose(self):
        with Vertical(id='monitor-editor'):
            yield Static('모니터 배치·해상도·화면 배율', id='monitor-title')
            yield Static('지원 모드를 조회하고 있습니다.', id='monitor-message', markup=False)
            yield MonitorCanvas(id='monitor-canvas')
            yield Static('드래그 후 놓으면 이동·교환 · 대상은 드롭 전까지 움직이지 않습니다 · 방향키 위치 교환', id='monitor-guide')
            yield Static('', id='monitor-selected', markup=False)
            with Horizontal(id='monitor-fields'):
                yield Select([], prompt='모니터', id='monitor-port')
                yield Select([], prompt='해상도·주사율', id='monitor-mode')
                yield Select([], prompt='화면 배율', id='monitor-scale')
            with Collapsible(title='정확한 좌표 입력', id='monitor-coordinates', collapsed=True):
                with Horizontal(id='monitor-coordinate-row'):
                    yield Input('0', id='monitor-x', placeholder='가로 X')
                    yield Input('0', id='monitor-y', placeholder='세로 Y')
                    yield Button('좌표 반영', id='monitor-coordinate-apply')
                yield Button('저장 배치 로그인 복원 끄기', id='monitor-profile-disable')
                yield Button('저장 배치 복원 재시도', id='monitor-profile-retry')
            with Horizontal(id='monitor-options'):
                yield Label('다음 로그인에도 유지')
                yield Switch(True, id='monitor-persist')
                yield Button('이 모니터의 사이트', id='monitor-site')
                yield Button('모니터 식별', id='monitor-identify')
            with Horizontal(id='monitor-confirm-buttons'):
                yield Button('이 배치 유지', id='monitor-keep', variant='primary')
                yield Button('원래 배치로 복원', id='monitor-revert')
            with Horizontal(id='monitor-buttons'):
                yield Button('확장 갱신', id='monitor-extension')
                yield Button('다시 조회', id='monitor-refresh')
                yield Button('편집 취소', id='monitor-undo')
                yield Button('변경 검토', id='monitor-review', variant='primary')
                yield Button('닫기', id='monitor-close')

    async def on_mount(self):
        self.query_one('#monitor-confirm-buttons').display = False
        self.set_interval(1, self.poll)
        await self.load()

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
            self.query_one('#monitor-canvas', MonitorCanvas).set_draft(self.draft)
            self.query_one('#monitor-extension', Button).display = not settings.get('trial_supported')
            self.message('원하는 위치로 드래그하고 모니터별 해상도·배율을 선택하세요.' if settings.get('trial_supported')
                         else '배치 적용 기능이 있는 확장으로 갱신하세요. 사이트 설정은 유지합니다.')
            self.sync_fields()
        except Exception as exc: self.message(str(exc))
        finally:
            self.busy = False; self.update_controls()

    def sync_fields(self):
        with self.prevent(Select.Changed, Input.Changed):
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
            self.query_one('#monitor-x', Input).value = str(output['x'])
            self.query_one('#monitor-y', Input).value = str(output['y'])
            targets = self.panel.targets_on_output(self.draft.selected)
            sites = ', '.join(t['id'] for t in targets) or '사이트 미설정'
            self.query_one('#monitor-selected', Static).update(safe_text(self.draft.selected + ' · ' + sites))
        finally:
            self.syncing = False
        self.query_one('#monitor-canvas', MonitorCanvas).refresh()
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
        for key in ('monitor-port', 'monitor-mode', 'monitor-scale', 'monitor-persist', 'monitor-coordinate-apply',
                    'monitor-site', 'monitor-identify', 'monitor-review', 'monitor-undo', 'monitor-refresh', 'monitor-extension',
                    'monitor-profile-disable', 'monitor-profile-retry'):
            self.query_one('#' + key).disabled = not editable if key not in {'monitor-refresh', 'monitor-extension'} else bool(self.busy or self.operation)
        canvas = self.query_one('#monitor-canvas', MonitorCanvas); canvas.editable = editable
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

    @on(Select.Changed)
    def selected(self, event):
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

    @on(MonitorCanvas.Selected)
    def canvas_selected(self): self.sync_fields()

    @on(MonitorCanvas.Changed)
    def canvas_changed(self, event):
        self.message(event.reason or '편집값만 변경했습니다. 변경 검토 후 적용하세요.')
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
                self.query_one('#monitor-canvas', MonitorCanvas).set_draft(self.draft); self.sync_fields()
            elif key == 'monitor-coordinate-apply':
                try:
                    self.draft.drop(self.draft.selected, int(self.query_one('#monitor-x', Input).value), int(self.query_one('#monitor-y', Input).value), snap=0)
                    self.sync_fields()
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
        self.dismiss()
