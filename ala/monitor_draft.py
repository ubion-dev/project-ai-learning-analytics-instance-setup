"""Local monitor layout drafts; device changes require a reviewed request."""
from copy import deepcopy

from .monitor_layout import build_plan, logical_dimensions


class LayoutDraft:
    def __init__(self, settings):
        if settings.get('state') != 'ok' or 'edit_template' not in settings:
            raise ValueError('활성 모니터별 지원 모드를 확인하세요. 복제 출력은 편집하지 않습니다.')
        self.settings = deepcopy(settings)
        self.request = deepcopy(settings['edit_template'])
        self.selected = sorted(o['id'] for o in self.request['outputs'])[0]

    @property
    def dirty(self):
        return self.request != self.settings['edit_template']

    def output(self, output_id=None):
        return next(o for o in self.request['outputs'] if o['id'] == (output_id or self.selected))

    def modes(self, output_id=None):
        return next(o['modes'] for o in self.settings['outputs'] if o['id'] == (output_id or self.selected))

    def mode(self, output_id=None):
        value = self.output(output_id)
        return next(m for m in self.modes(output_id) if m['id'] == value['mode_id'])

    def set_mode(self, mode_id):
        if mode_id not in {m['id'] for m in self.modes()}:
            raise ValueError('이 모니터의 지원 모드에서 선택하세요.')
        value = self.output()
        value['mode_id'] = mode_id
        if value['scale'] not in self.mode()['supported_scales']:
            value['scale'] = None

    def set_scale(self, scale):
        if type(scale) not in (int, float) or scale not in self.mode()['supported_scales']:
            raise ValueError('선택한 모드에서 지원하는 배율을 선택하세요.')
        self.output()['scale'] = scale

    def rects(self):
        logical = {o['connectors'][0]: o for o in self.settings['logical_outputs']}
        result = []
        for value in self.request['outputs']:
            mode = self.mode(value['id'])
            scale = value['scale'] or mode['preferred_scale']
            try:
                width, height = logical_dimensions(mode['width'], mode['height'], scale,
                                                   logical[value['id']]['transform'], self.settings['layout_mode'])
            except ValueError:
                # An invalid draft remains visible while selection is corrected.
                width, height = mode['width'], mode['height']
                if logical[value['id']]['transform'] % 2: width, height = height, width
                if self.settings['layout_mode'] == 1: width, height = width / scale, height / scale
            result.append(dict(value, width=round(width), height=round(height)))
        return result

    def validate(self):
        return build_plan(self.settings, self.request)

    def drop(self, source_id, x, y, target_id=None, *, snap=120):
        """Change a local draft; reject a collision without moving a third output."""
        if type(x) is not int or type(y) is not int or not -65536 <= x <= 65536 or not -65536 <= y <= 65536:
            raise ValueError('모니터 좌표 범위를 확인하세요.')
        before = deepcopy(self.request)
        boxes = {o['id']: o for o in self.rects()}
        source = self.output(source_id)
        a = boxes[source_id]
        if target_id and target_id != source_id:
            target, b = self.output(target_id), boxes[target_id]
            source['x'], source['y'], target['x'], target['y'] = b['x'], b['y'], a['x'], a['y']
            # Adjacent unequal screens share the same pair footprint after a swap.
            if a['y'] == b['y'] and a['x'] + a['width'] == b['x']:
                source['x'] = a['x'] + b['width']
            elif a['y'] == b['y'] and b['x'] + b['width'] == a['x']:
                target['x'] = b['x'] + a['width']
            elif a['x'] == b['x'] and a['y'] + a['height'] == b['y']:
                source['y'] = a['y'] + b['height']
            elif a['x'] == b['x'] and b['y'] + b['height'] == a['y']:
                target['y'] = b['y'] + a['height']
        else:
            others = [b for key, b in boxes.items() if key != source_id]
            xs = [edge for b in others for edge in (b['x'], b['x'] + b['width'], b['x'] - a['width'])]
            ys = [edge for b in others for edge in (b['y'], b['y'] + b['height'], b['y'] - a['height'])]
            near_x = min(xs, key=lambda p: abs(p - x), default=x)
            near_y = min(ys, key=lambda p: abs(p - y), default=y)
            source['x'], source['y'] = near_x if abs(near_x - x) <= snap else x, near_y if abs(near_y - y) <= snap else y
        rects = self.rects()
        touched = {source_id, target_id}
        for index, a in enumerate(rects):
            for b in rects[index + 1:]:
                if (a['id'] in touched or b['id'] in touched) and (
                        max(a['x'], b['x']) < min(a['x'] + a['width'], b['x'] + b['width'])
                        and max(a['y'], b['y']) < min(a['y'] + a['height'], b['y'] + b['height'])):
                    self.request = before
                    raise ValueError('이 위치는 다른 모니터와 겹칩니다. 변경 전 위치를 유지했습니다.')

    def place(self, source_id, relation, anchor_id=None, *, x=None, y=None):
        """Place a local draft output explicitly without dragging or moving peers."""
        ids = {item['id'] for item in self.request['outputs']}
        if source_id not in ids:
            raise ValueError('현재 PC의 모니터를 선택하세요.')
        if relation == 'coordinates':
            self.drop(source_id, x, y, snap=0)
            return
        if relation not in {'left', 'right', 'above', 'below', 'swap'}:
            raise ValueError('지원하는 위치 변경 방법을 선택하세요.')
        if anchor_id not in ids or anchor_id == source_id:
            raise ValueError('같은 PC에 연결된 다른 기준 모니터를 선택하세요.')
        boxes = {item['id']: item for item in self.rects()}
        source, anchor = boxes[source_id], boxes[anchor_id]
        if relation == 'swap':
            self.drop(source_id, source['x'], source['y'], anchor_id)
            return
        x, y = {
            'left': (anchor['x'] - source['width'], anchor['y']),
            'right': (anchor['x'] + anchor['width'], anchor['y']),
            'above': (anchor['x'], anchor['y'] - source['height']),
            'below': (anchor['x'], anchor['y'] + anchor['height']),
        }[relation]
        self.drop(source_id, x, y, snap=0)
