"""Read-only output diagram with a guarded request to move one selected DID."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from rich.text import Text
from rich.cells import cell_len
from textual import events
from textual.geometry import Offset, Region
from textual.message import Message
from textual.widgets import Static
from textual.widget import Widget

from .chrome_tui import safe_text
from .monitors import numbered_outputs, resolve_output


@dataclass(frozen=True)
class OutputBox:
    output_id: str
    name: str
    geometry: dict
    region: Region


CARD_WIDTH = 26
CARD_HEIGHT = 7
CARD_GAP = 3


def _outputs(report):
    """Use geometry for reading order; labels keep the shared ID-based numbers."""
    logical = report.get('logical_outputs') or []
    numbered = numbered_outputs({'logical_outputs': logical})
    def position(item):
        geometry = item.get('geometry', item)
        x, y = geometry.get('x'), geometry.get('y')
        return (y if type(y) is int else 0, x if type(x) is int else 0, item['id'])
    return sorted(numbered, key=position)


def _empty_text(report):
    if report.get('output_state') == 'ok':
        return 'GNOME 활성 출력 없음'
    reason = safe_text(report.get('reason') or '활성 그래픽 세션과 GNOME 연결을 확인하세요.')
    action = safe_text(report.get('output_next_action') or '')
    return '\n'.join(filter(None, ('GNOME 배치 확인 불가 · ' + reason, action)))


def _targets_for_output(report, output):
    inventory = {'state': report.get('output_state', 'unknown'),
                 'logical_outputs': [dict(value, connectors=value.get('connectors', []))
                                     for value in report.get('logical_outputs', [])]}
    targets = []
    for target in report.get('targets', []):
        resolved = resolve_output(inventory, target.get('output'), auto=target.get('id') == 'default')
        if (resolved.get('output') or {}).get('id') == output:
            targets.append(target)
    return targets


def _fit(value, width):
    text = Text(safe_text(str(value)), no_wrap=True)
    text.truncate(width, overflow='ellipsis', pad=True)
    return text.plain


def _card_status(report, output, targets):
    if report.get('output_state') != 'ok' or output.get('confirmed') is not True:
        return '출력 확인 필요'
    if not targets:
        return '클릭 또는 Enter로 추가'
    if len(targets) > 1:
        return f'DID {len(targets)}개 · 선택하여 관리'
    target = targets[0]
    settings = target.get('settings') or {}
    if (target.get('swap') or {}).get('state') in {'unknown', 'recovery_required'}:
        return '교환 복구 확인 필요'
    if settings.get('state') in {'queued', 'running'} or (target.get('runtime') or {}).get('operation'):
        return '화면 작업 진행 중'
    if settings.get('pending'):
        return '설정 적용 대기'
    state = (target.get('service') or {}).get('ActiveState')
    if state == 'failed' or settings.get('state') in {'failed', 'interrupted'}:
        return '실패 · 로그 확인'
    if state == 'inactive':
        return '중지됨'
    if state != 'active':
        return '실행 확인 필요'
    health = target.get('health') or {}
    revision = ((target.get('runtime') or {}).get('committed') or {}).get('revision')
    matches = not revision or health.get('config_revision') == revision
    if target.get('applied_url', target.get('url')) is None:
        return ('설정 안내 표시 중' if health.get('state') == 'waiting_configuration'
                and health.get('local_page_ready') is True and matches else '안내 표시 확인 필요')
    return ('실행 중 · 사이트 확인' if health.get('state') == 'healthy'
            and health.get('target_matches_config') and matches else '실행 중 · 사이트 확인 필요')


def _topology(report, width, height=7, *, selected=None):
    """Fixed-size monitor cards and matching content-relative hit regions.

    Height never scales a card. Extra rows belong to the parent's scroll area.
    """
    try:
        outputs = _outputs(report)
    except (ValueError, TypeError, AttributeError):
        return '출력 배치를 확인하지 못했습니다. 다시 조회하세요.', []
    if not outputs:
        return _empty_text(report), []
    width = max(CARD_WIDTH, width)
    columns = max(1, (width + CARD_GAP) // (CARD_WIDTH + CARD_GAP))
    rows, boxes = [], []
    for first in range(0, len(outputs), columns):
        group = outputs[first:first + columns]
        left = max(0, (width - len(group) * CARD_WIDTH - (len(group) - 1) * CARD_GAP) // 2)
        cards = []
        y = len(rows)
        for column, output in enumerate(group):
            geometry = output.get('geometry', output)
            name = f"#{output['display_number']} {output['id']}"
            resolution = (f"{geometry['width']}×{geometry['height']}"
                          if type(geometry.get('width')) is int and type(geometry.get('height')) is int
                          else '해상도 확인 필요')
            targets = _targets_for_output(report, output['id'])
            labels = [('> ' if target.get('id') == selected else '') + str(target.get('id', '')) for target in targets]
            values = (name, resolution, ', '.join(labels) or '배정된 DID 없음', _card_status(report, output, targets))
            cards.append(['┌' + '─' * 24 + '┐', *['│ ' + _fit(value, 22) + ' │' for value in values],
                          '└' + '─' * 11 + '┬' + '─' * 12 + '┘', ' ' * 11 + '─┴─' + ' ' * 12])
            boxes.append(OutputBox(output['id'], safe_text(name), geometry,
                                   Region(left + column * (CARD_WIDTH + CARD_GAP), y, CARD_WIDTH, CARD_HEIGHT)))
        rows += [' ' * left + (' ' * CARD_GAP).join(card[line] for card in cards) for line in range(CARD_HEIGHT)]
        if first + columns < len(outputs):
            rows.append('')
    return '\n'.join(rows), boxes


def topology_text(report, width=100, height=7):
    """Keep compact diagnostic coordinates available to non-card details."""
    logical = report.get('logical_outputs') or []
    if not logical:
        return _empty_text(report)
    if width < 95 or height < 7:
        lines = []
        for output in logical:
            geometry = output.get('geometry', output)
            if all(type(geometry.get(key)) is int for key in ('x', 'y', 'width', 'height')):
                lines.append(f"{safe_text(output.get('id') or '출력')} · {geometry['width']}×{geometry['height']} · ({geometry['x']}, {geometry['y']})")
        return '\n'.join(lines) or '출력은 감지했지만 배치 좌표는 확인하지 못했습니다.'
    return _topology(report, width, height)[0]


class ScreenMap(Static, can_focus=True):
    """Capture a selected DID drag, then leave mutations to the enclosing panel."""
    ALLOW_SELECT = False
    DEFAULT_CSS = 'ScreenMap { height: auto; width: 1fr; }'

    class OutputActivated(Message):
        def __init__(self, output_id):
            super().__init__()
            self.output_id = output_id

    class OutputFocused(Message):
        def __init__(self, output_id):
            super().__init__()
            self.output_id = output_id

    class MoveRequested(Message):
        def __init__(self, target_id, output_id, expected_config, target_snapshot, expected_topology=None):
            super().__init__()
            self.target_id = target_id
            self.output_id = output_id
            self.expected_config = deepcopy(expected_config)
            self.target_snapshot = deepcopy(target_snapshot)
            self.expected_topology = deepcopy(expected_topology)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._report = {}
        self._selected = None
        self._expected_config = {}
        self._move_enabled = False
        self._boxes = []
        self._drag = None
        self._press = None
        self._hover_output = None
        self._focused_output = None

    def update_report(self, report, selected, expected_config, *, enabled=True):
        gesture = self._press
        if gesture is not None and (
                not enabled or report.get('output_state') != 'ok'
                or report.get('logical_outputs') != gesture['topology']
                or selected != gesture['selection']
                or expected_config != gesture['config']
                or gesture['target'] is not None and self._assignment(self._target(report, gesture['target']['id'])) != self._assignment(gesture['target'])):
            self.cancel_drag()
        changed_selection = self._selected != selected
        self._report = deepcopy(report)
        self._selected = selected
        self._expected_config = deepcopy(expected_config)
        self._move_enabled = enabled
        ids = {value.get('id') for value in report.get('logical_outputs', [])}
        source = self._target_output(self._target(report, selected))
        if changed_selection and source in ids:
            self._focused_output = source
        elif self._focused_output not in ids:
            self._focused_output = source if source in ids else None
        self._draw()

    @staticmethod
    def _target(report, selected):
        return next((value for value in report.get('targets', []) if value.get('id') == selected), None)

    @staticmethod
    def _assignment(target):
        return {key: (target or {}).get(key) for key in ('id', 'url', 'output', 'layout', 'autostart', 'frontend')}

    @property
    def output_regions(self):
        return {box.output_id: box.region for box in self._boxes}

    def _confirmed_ids(self):
        return {value.get('id') for value in self._report.get('logical_outputs', [])
                if value.get('confirmed') is True and isinstance(value.get('id'), str)}

    def _target_output(self, target):
        if not target:
            return None
        logical = [dict(value, connectors=value.get('connectors', []))
                   for value in self._report.get('logical_outputs', [])]
        inventory = {'state': self._report.get('output_state', 'unknown'), 'logical_outputs': logical}
        resolved = resolve_output(inventory, target.get('output'), auto=target.get('id') == 'default')
        return (resolved.get('output') or {}).get('id')

    def _draw(self):
        drawing, self._boxes = _topology(self._report, max(1, self.content_size.width), selected=self._selected)
        if self._focused_output not in {box.output_id for box in self._boxes}:
            self._focused_output = self._boxes[0].output_id if self._boxes else None
        lines = drawing.splitlines()
        rendered = Text('\n'.join(lines), no_wrap=True, overflow='ellipsis')
        source = self._target_output(self._target(self._report, self._selected))
        starts = []
        offset = 0
        for line in lines:
            starts.append(offset)
            offset += len(line) + 1
        for box in self._boxes:
            style = 'bold #f1d5a3 on #383346' if box.output_id == self._hover_output else (
                'bold #cbc2ef on #312b40' if self.has_focus and box.output_id == self._focused_output else
                'bold #b5e9d9' if box.output_id == source else '')
            if style:
                for row in range(box.region.y, min(box.region.bottom, len(lines))):
                    start = starts[row] + self._cell_index(lines[row], box.region.x)
                    end = starts[row] + self._cell_index(lines[row], box.region.right)
                    rendered.stylize(style, start, end)
        self.update(rendered)

    @staticmethod
    def _cell_index(line, column):
        cells = 0
        for index, character in enumerate(line):
            if cells >= column:
                return index
            cells += cell_len(character)
        return len(line)

    def _card_at(self, event):
        if not self.is_on_screen:
            return None
        visible = self.screen.find_widget(self).visible_region.intersection(self.content_region)
        if not visible.contains(event.screen_x, event.screen_y):
            return None
        point = Offset(event.screen_x - self.content_region.x, event.screen_y - self.content_region.y)
        return next((box.output_id for box in self._boxes if box.region.contains(point.x, point.y)), None)

    def _output_at(self, event):
        output = self._card_at(event)
        return output if output in self._confirmed_ids() else None

    def on_mouse_down(self, event: events.MouseDown):
        if event.button != 1 or self.is_disabled:
            return
        output = self._card_at(event)
        if output is None:
            return
        self.cancel_drag()
        targets = _targets_for_output(self._report, output)
        target = targets[0] if len(targets) == 1 else None
        movable = (self._move_enabled and self._report.get('output_state') == 'ok'
                   and output in self._confirmed_ids() and target is not None
                   and target.get('layout') == 'fullscreen')
        self._press = {'target': deepcopy(target), 'config': deepcopy(self._expected_config),
                       'topology': deepcopy(self._report.get('logical_outputs')), 'selection': self._selected,
                       'source': output, 'origin': (event.screen_x, event.screen_y), 'moved': False}
        self._focused_output = output
        if movable:
            self._drag = self._press
        self.focus(scroll_visible=False)
        if movable:
            self.capture_mouse()
        self._draw()
        event.stop()
        event.prevent_default()

    def on_mouse_move(self, event: events.MouseMove):
        if self._press is None:
            return
        x, y = self._press['origin']
        self._press['moved'] |= max(abs(event.screen_x - x), abs(event.screen_y - y)) >= 2
        self._hover_output = self._output_at(event) if self._drag and self._press['moved'] else None
        self._draw()
        event.stop()
        event.prevent_default()

    def on_mouse_up(self, event: events.MouseUp):
        if self._press is None or event.button != 1:
            return
        gesture = self._press
        origin_x, origin_y = gesture['origin']
        gesture['moved'] |= max(abs(event.screen_x - origin_x), abs(event.screen_y - origin_y)) >= 2
        clicked = not gesture['moved'] and self._card_at(event) == gesture['source']
        output = self._output_at(event)
        valid = (self._drag is not None and self._move_enabled and not self.is_disabled and self.is_on_screen
                 and self._report.get('output_state') == 'ok'
                 and gesture['moved'] and output and output != gesture['source'])
        self.cancel_drag()
        event.stop()
        event.prevent_default()
        if valid:
            self.post_message(self.MoveRequested(gesture['target']['id'], output, gesture['config'], gesture['target'], gesture['topology']))
        elif clicked and not self.is_disabled:
            self.post_message(self.OutputActivated(gesture['source']))

    def cancel_drag(self):
        if self._press is None:
            return
        captured = self._drag is not None
        self._press = None
        self._drag = None
        self._hover_output = None
        if captured:
            self.release_mouse()
        self._draw()

    def on_key(self, event: events.Key):
        if event.key == 'escape' and self._press is not None:
            self.cancel_drag()
            event.stop()
            event.prevent_default()
        elif event.key in {'left', 'right', 'up', 'down', 'enter'} and self._boxes and not self.is_disabled:
            if self._press is not None:
                self.cancel_drag()
            elif event.key == 'enter':
                self.post_message(self.OutputActivated(self._focused_output))
            else:
                self._navigate(event.key)
            event.stop()
            event.prevent_default()

    def _navigate(self, direction):
        current = next((box for box in self._boxes if box.output_id == self._focused_output), self._boxes[0])
        x, y = current.region.x, current.region.y
        candidates = []
        for box in self._boxes:
            dx, dy = box.region.x - x, box.region.y - y
            if ((direction == 'left' and dx < 0) or (direction == 'right' and dx > 0)
                    or (direction == 'up' and dy < 0) or (direction == 'down' and dy > 0)):
                candidates.append(((abs(dy), abs(dx)), box))
        if candidates:
            self._focused_output = min(candidates, key=lambda item: item[0])[1].output_id
            self._draw()
            self._scroll_focused()
            self.post_message(self.OutputFocused(self._focused_output))

    def _scroll_focused(self):
        box = next((box for box in self._boxes if box.output_id == self._focused_output), None)
        if box is None:
            return
        for parent in self.ancestors:
            if isinstance(parent, Widget) and parent.is_scrollable:
                offset = self.content_region.offset - parent.content_region.offset + parent.scroll_offset
                parent.scroll_to_region(box.region.translate(offset), animate=False, origin_visible=False)
                break

    def on_focus(self):
        self._draw()

    def on_mouse_leave(self):
        if self._press is not None and self._drag is None:
            self.cancel_drag()

    def on_resize(self):
        self.cancel_drag()
        self._draw()

    def on_hide(self):
        self.cancel_drag()

    def on_blur(self):
        self.cancel_drag()
        self._draw()

    def on_unmount(self):
        self.cancel_drag()

    def on_mouse_release(self):
        self.cancel_drag()

    def watch_disabled(self, disabled):
        super().watch_disabled(disabled)
        if disabled:
            self.cancel_drag()
