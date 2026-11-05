"""Read-only output diagram with a guarded request to move one selected DID."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from rich.text import Text
from textual import events
from textual.geometry import Offset, Region
from textual.message import Message
from textual.widgets import Static

from .chrome_tui import safe_text
from .monitors import numbered_outputs, resolve_output


@dataclass(frozen=True)
class OutputBox:
    output_id: str
    name: str
    geometry: dict
    region: Region


def _topology(report, width, height, *, minimum_height=7):
    """Return the rendered diagram and the same cell regions used for hit tests."""
    logical = report.get('logical_outputs') or []
    if not logical:
        if report.get('output_state') == 'ok':
            return 'GNOME 활성 출력 없음', []
        reason = safe_text(report.get('reason') or '활성 그래픽 세션과 GNOME 연결을 확인하세요.')
        action = safe_text(report.get('output_next_action') or '')
        return '\n'.join(filter(None, ('GNOME 배치 확인 불가 · ' + reason, action))), []
    valid = []
    try:
        numbers = {item['id']: item['display_number'] for item in numbered_outputs({'logical_outputs': logical})}
    except ValueError:
        numbers = {}
    for item in logical:
        geometry = item.get('geometry', item)
        if (all(type(geometry.get(key)) is int for key in ('x', 'y', 'width', 'height'))
                and geometry['width'] > 0 and geometry['height'] > 0):
            connectors = item.get('connectors', [])
            name = item.get('id') or ', '.join(str(value) for value in connectors) or '출력'
            if item.get('id') in numbers:
                name = f"#{numbers[item['id']]} {name}"
            valid.append((str(item.get('id') or ''), safe_text(name), geometry))
    if not valid:
        return '출력은 감지했지만 배치 좌표는 확인하지 못했습니다.', []
    if width < 95 or height < minimum_height:
        lines = []
        boxes = []
        for row, (output_id, name, geometry) in enumerate(valid):
            lines.append(f'{name} · {geometry["width"]}×{geometry["height"]} · ({geometry["x"]}, {geometry["y"]})')
            boxes.append(OutputBox(output_id, name, geometry, Region(0, row, width, 1)))
        return '\n'.join(lines), boxes
    width, height = min(width, 124), min(height, 10)
    left = min(geometry['x'] for _, _, geometry in valid)
    top = min(geometry['y'] for _, _, geometry in valid)
    span_x = max(geometry['x'] + geometry['width'] for _, _, geometry in valid) - left
    span_y = max(geometry['y'] + geometry['height'] for _, _, geometry in valid) - top
    grid = [[' '] * width for _ in range(height)]

    def put(x, y, value):
        if 0 <= y < height:
            for offset, character in enumerate(value):
                if 0 <= x + offset < width:
                    grid[y][x + offset] = character

    boxes = []
    for output_id, name, geometry in valid:
        x = int((geometry['x'] - left) * (width - 1) / span_x)
        y = int((geometry['y'] - top) * (height - 1) / span_y)
        w = max(8, int(geometry['width'] * (width - 1) / span_x))
        h = max(3, int(geometry['height'] * (height - 1) / span_y))
        w, h = min(w, width - x), min(h, height - y)
        if w < 4 or h < 3:
            return _topology(report, min(width, 80), 3)
        put(x, y, '┌' + '─' * (w - 2) + '┐')
        for row in range(y + 1, y + h - 1):
            put(x, row, '│' + ' ' * (w - 2) + '│')
        put(x, y + h - 1, '└' + '─' * (w - 2) + '┘')
        put(x + 2, y + 1, name[:max(0, w - 4)])
        if h > 3:
            put(x + 2, y + 2, f'{geometry["width"]}×{geometry["height"]}'[:max(0, w - 4)])
        boxes.append(OutputBox(output_id, name, geometry, Region(x, y, w, h)))
    return '\n'.join(''.join(row).rstrip() for row in grid), boxes


def topology_text(report, width=100, height=7):
    return _topology(report, width, height)[0]


class ScreenMap(Static, can_focus=True):
    """Capture a selected DID drag, then leave mutations to the enclosing panel."""
    ALLOW_SELECT = False

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
        self._hover_output = None

    def update_report(self, report, selected, expected_config, *, enabled=True):
        if self._drag is not None and (
                not enabled or report.get('output_state') != 'ok'
                or report.get('logical_outputs') != self._drag['topology']
                or selected != self._drag['target']['id']
                or expected_config != self._drag['config']
                or self._assignment(self._target(report, selected)) != self._assignment(self._drag['target'])):
            self.cancel_drag()
        self._report = deepcopy(report)
        self._selected = selected
        self._expected_config = deepcopy(expected_config)
        self._move_enabled = enabled
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
        width, height = self.content_size
        width = max(1, width)
        # A 95-column widget keeps the same readable list as narrower layouts.
        drawing, self._boxes = _topology(self._report, min(width, 94) if width <= 95 else width,
                                                height, minimum_height=5)
        lines = drawing.splitlines()
        for box in self._boxes:
            region = box.region
            targets = [target for target in self._report.get('targets', [])
                       if self._target_output(target) == box.output_id]
            labels = [('> ' if target.get('id') == self._selected else '') + safe_text(str(target.get('id', '')))
                      for target in targets]
            if not labels:
                continue
            if region.height == 1:
                lines[region.y] += ' · DID ' + ', '.join(labels)
                continue
            label = ('DID ' if region.height > 3 else box.name + ' · ') + ', '.join(labels)
            row = region.y + (3 if region.height > 4 else region.height - 2)
            available = max(0, region.width - 4)
            if row < len(lines):
                line = lines[row].ljust(region.right)
                start = region.x + 2
                lines[row] = line[:start] + label[:available].ljust(available) + line[start + available:]
        rendered = Text('\n'.join(lines), no_wrap=True, overflow='ellipsis')
        source = self._target_output(self._target(self._report, self._selected))
        starts = []
        offset = 0
        for line in lines:
            starts.append(offset)
            offset += len(line) + 1
        for box in self._boxes:
            style = 'bold #f1d5a3 on #383346' if box.output_id == self._hover_output else (
                'bold #b5e9d9' if box.output_id == source else '')
            if style:
                for row in range(box.region.y, min(box.region.bottom, len(lines))):
                    start = starts[row] + min(box.region.x, len(lines[row]))
                    end = starts[row] + min(box.region.right, len(lines[row]))
                    rendered.stylize(style, start, end)
        self.update(rendered)

    def _output_at(self, event):
        point = Offset(event.screen_x - self.content_region.x, event.screen_y - self.content_region.y)
        if not self.content_region.contains(event.screen_x, event.screen_y):
            return None
        return next((box.output_id for box in self._boxes if box.region.contains(point.x, point.y)
                     and box.output_id in self._confirmed_ids()), None)

    def on_mouse_down(self, event: events.MouseDown):
        if (event.button != 1 or not self._move_enabled or self.is_disabled
                or self._report.get('output_state') != 'ok'):
            return
        target = self._target(self._report, self._selected)
        source = self._target_output(target)
        if not target or source not in self._confirmed_ids() or self._output_at(event) != source:
            return
        self.cancel_drag()
        self._drag = {'target': deepcopy(target), 'config': deepcopy(self._expected_config),
                      'topology': deepcopy(self._report.get('logical_outputs')),
                      'source': source, 'origin': (event.screen_x, event.screen_y), 'moved': False}
        self.focus()
        self.capture_mouse()
        event.stop()
        event.prevent_default()

    def on_mouse_move(self, event: events.MouseMove):
        if self._drag is None:
            return
        self._drag['moved'] |= (event.screen_x, event.screen_y) != self._drag['origin']
        self._hover_output = self._output_at(event)
        self._draw()
        event.stop()
        event.prevent_default()

    def on_mouse_up(self, event: events.MouseUp):
        if self._drag is None or event.button != 1:
            return
        drag = self._drag
        output = self._output_at(event)
        valid = (self._move_enabled and not self.is_disabled and self.is_on_screen
                 and self._report.get('output_state') == 'ok'
                 and drag['moved'] and output and output != drag['source'])
        self.cancel_drag()
        event.stop()
        event.prevent_default()
        if valid:
            self.post_message(self.MoveRequested(drag['target']['id'], output, drag['config'], drag['target'], drag['topology']))

    def cancel_drag(self):
        if self._drag is None:
            return
        self._drag = None
        self._hover_output = None
        self.release_mouse()
        self._draw()

    def on_key(self, event: events.Key):
        if event.key == 'escape' and self._drag is not None:
            self.cancel_drag()
            event.stop()
            event.prevent_default()

    def on_resize(self):
        self.cancel_drag()
        self._draw()

    def on_hide(self):
        self.cancel_drag()

    def on_blur(self):
        self.cancel_drag()

    def on_unmount(self):
        self.cancel_drag()

    def on_mouse_release(self):
        self.cancel_drag()

    def watch_disabled(self, disabled):
        super().watch_disabled(disabled)
        if disabled:
            self.cancel_drag()
