"""Observed output layout. Click and keyboard navigation never move a window."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math

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


def output_mode_text(report, logical_output):
    """Describe an observed physical mode and scale, never infer either.

    A single unambiguous connector is required for a physical resolution.
    Mirrored groups have no representative physical mode.  The explicitly
    labelled logical fallback is the compositor area, not panel pixels.
    """
    logical = logical_output if isinstance(logical_output, dict) else {}
    scale = logical.get('scale')
    percentage = None
    if type(scale) in (int, float):
        try:
            if scale > 0 and math.isfinite(scale) and math.isfinite(scale * 100):
                percentage = f'{scale * 100:g}%'
        except (OverflowError, ValueError):
            pass
    scale_text = percentage or '배율 미상'

    connectors = logical.get('connectors')
    valid = (isinstance(connectors, (list, tuple))
             and all(isinstance(value, str) and value for value in connectors))
    if logical.get('mirrored') is True or valid and len(set(connectors)) > 1:
        return '복제 · ' + scale_text

    def resolution(value):
        if (isinstance(value, dict) and type(value.get('width')) is int
                and type(value.get('height')) is int and value['width'] > 0 and value['height'] > 0):
            return f'{value["width"]}×{value["height"]}'
        return None

    outputs = report.get('outputs', []) if isinstance(report, dict) else []
    if valid and len(connectors) == 1 and isinstance(outputs, list):
        matches = [output for output in outputs if isinstance(output, dict)
                   and output.get('connector') == connectors[0]]
        physical = resolution(matches[0]) if len(matches) == 1 else None
        if physical:
            return physical + ' · ' + scale_text
    logical_size = resolution(logical.get('geometry', logical))
    if logical_size:
        return '논리 ' + logical_size + ' · ' + scale_text
    return ('해상도 미상 · ' + percentage) if percentage else '해상도·배율 확인 필요'


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


def _card_values(report, output, selected):
    targets = _targets_for_output(report, output['id'])
    labels = [('> ' if target.get('id') == selected else '') + str(target.get('id', '')) for target in targets]
    return (f"#{output['display_number']} {output['id']}", output_mode_text(report, output),
            ', '.join(labels) or '배정된 DID 없음', _card_status(report, output, targets))


def _coordinate_list(report, outputs, geometries, width):
    rows, boxes = ['좌표 목록 · 배치 축소 표시 불가'], []
    for output, geometry in zip(outputs, geometries):
        geometry = geometry if isinstance(geometry, dict) else {}
        targets = _targets_for_output(report, output['id'])
        labels = ', '.join(t['id'] for t in targets) or '배정 없음'
        y = len(rows)
        rows += [_fit(f"#{output['display_number']} {output['id']} · ({geometry.get('x', '?')}, {geometry.get('y', '?')})", width),
                 _fit(labels + ' · ' + _card_status(report, output, targets), width)]
        boxes.append(OutputBox(output['id'], safe_text(output['id']), geometry, Region(0, y, width, 2)))
    return '\n'.join(rows), boxes


def _topology(report, width, height=12, *, selected=None):
    """Project logical pixel rectangles with one scale and terminal cell aspect.

    Shared projected edges preserve adjacency. Unreadable or overlapping shapes
    use an explicit list instead of enlarging or moving individual outputs.
    """
    try:
        outputs = _outputs(report)
    except (ValueError, TypeError, AttributeError):
        return '출력 배치를 확인하지 못했습니다. 다시 조회하세요.', []
    if not outputs:
        return _empty_text(report), []
    width = max(1, width)
    geometries = [value.get('geometry', value) for value in outputs]
    valid = all(isinstance(g, dict) and all(type(g.get(key)) is int for key in ('x','y','width','height'))
                and g['width'] > 0 and g['height'] > 0 for g in geometries)
    if not valid:
        return _coordinate_list(report, outputs, geometries, width)
    origin_x = min(g['x'] for g in geometries)
    origin_y = min(g['y'] for g in geometries)
    span_x = max(g['x'] + g['width'] for g in geometries) - origin_x
    span_y = max(g['y'] + g['height'] for g in geometries) - origin_y
    factor = min(width / span_x, max(3, height) * 2 / span_y)
    projected_width = round(span_x * factor)
    left = max(0, (width - projected_width) // 2)
    boxes = []
    for output, geometry in zip(outputs, geometries):
        x = round((geometry['x'] - origin_x) * factor)
        y = round((geometry['y'] - origin_y) * factor / 2)
        right = round((geometry['x'] + geometry['width'] - origin_x) * factor)
        bottom = round((geometry['y'] + geometry['height'] - origin_y) * factor / 2)
        region = Region(left + x, y, right - x, bottom - y)
        if region.width < 8 or region.height < 3 or any(region.overlaps(box.region) for box in boxes):
            return _coordinate_list(report, outputs, geometries, width)
        boxes.append(OutputBox(output['id'], safe_text(output['id']), geometry, region))
    rows = [[' '] * (left + projected_width) for _ in range(max(box.region.bottom for box in boxes))]
    truncated = []
    for output, box in zip(outputs, boxes):
        region = box.region
        values = _card_values(report, output, selected)
        lines = ['┌' + '─' * (region.width - 2) + '┐']
        for index in range(region.height - 2):
            value = values[index] if index < len(values) else ''
            lines.append('│' + _fit(value, region.width - 2) + '│')
        lines.append('└' + '─' * (region.width - 2) + '┘')
        if region.height - 2 < len(values) or any(cell_len(v) > region.width - 2 for v in values):
            truncated.append(values)
        for row, line in enumerate(lines):
            # CJK text occupies two terminal cells while using one code point.
            cells = []
            for char in line:
                cells.append(char)
                cells.extend([''] * max(0, cell_len(char) - 1))
            rows[region.y + row][region.x:region.right] = cells
    drawing = [''.join(row) for row in rows]
    if truncated:
        drawing.append('모니터 정보')
        for values in truncated:
            drawing.extend((values[0] + ' · ' + values[1], values[2] + ' · ' + values[3]))
    return '\n'.join(drawing), boxes


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
                lines.append(f"{safe_text(output.get('id') or '출력')} · {output_mode_text(report, output)} · ({geometry['x']}, {geometry['y']})")
        return '\n'.join(lines) or '출력은 감지했지만 배치 좌표는 확인하지 못했습니다.'
    return _topology(report, width, height)[0]


class ScreenMap(Static, can_focus=True):
    """Read-only layout with click/keyboard links to site settings."""
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._report = {}
        self._selected = None
        self._boxes = []
        self._press = None
        self._focused_output = None

    def update_report(self, report, selected):
        if self._report.get('logical_outputs') != report.get('logical_outputs'):
            self._press = None
        changed_selection = self._selected != selected
        self._report = deepcopy(report)
        self._selected = selected
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

    @property
    def output_regions(self):
        return {box.output_id: box.region for box in self._boxes}

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
            style = ('bold #cbc2ef on #312b40' if self.has_focus and box.output_id == self._focused_output else
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

    def on_mouse_down(self, event: events.MouseDown):
        if event.button != 1 or self.is_disabled:
            return
        output = self._card_at(event)
        if output is not None:
            self._press = (output, event.screen_x, event.screen_y)
            self._focused_output = output
            self.focus(scroll_visible=False)
            self._draw()
            event.stop()
            event.prevent_default()

    def on_mouse_move(self, event: events.MouseMove):
        # Suppress a moved click; no capture, hover painting or move request.
        if self._press and max(abs(event.screen_x - self._press[1]), abs(event.screen_y - self._press[2])) >= 2:
            self._press = None

    def on_mouse_up(self, event: events.MouseUp):
        pressed, self._press = self._press, None
        if pressed is None or event.button != 1 or self.is_disabled:
            return
        output, x, y = pressed
        if self._card_at(event) == output and max(abs(event.screen_x - x), abs(event.screen_y - y)) < 2:
            self.post_message(self.OutputActivated(output))
        event.stop()
        event.prevent_default()

    def on_key(self, event: events.Key):
        if event.key in {'left', 'right', 'up', 'down', 'enter'} and self._boxes and not self.is_disabled:
            self._press = None
            if event.key == 'enter': self.post_message(self.OutputActivated(self._focused_output))
            else: self._navigate(event.key)
            event.stop()
            event.prevent_default()
        elif event.key == 'escape':
            self._press = None

    def _navigate(self, direction):
        current = next((box for box in self._boxes if box.output_id == self._focused_output), self._boxes[0])
        x, y = current.region.x, current.region.y
        candidates = []
        for box in self._boxes:
            dx, dy = box.region.x - x, box.region.y - y
            if ((direction == 'left' and dx < 0) or (direction == 'right' and dx > 0)
                    or (direction == 'up' and dy < 0) or (direction == 'down' and dy > 0)):
                distance = (abs(dy), abs(dx)) if direction in {'left', 'right'} else (abs(dx), abs(dy))
                candidates.append((distance, box))
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

    def on_mouse_leave(self): self._press = None

    def on_resize(self):
        self._press = None
        self._draw()

    def on_hide(self): self._press = None

    def on_blur(self):
        self._press = None
        self._draw()

    def on_unmount(self): self._press = None

    def watch_disabled(self, disabled):
        super().watch_disabled(disabled)
        if disabled: self._press = None
