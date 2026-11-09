"""Read-only Mutter mode inventory and OS layout planning.

This module neither writes configuration nor calls ApplyMonitorsConfig. Local
validation is deliberately distinct from compositor verification and acceptance.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
import struct


def _require(condition):
    if not condition:
        raise ValueError('모니터 모드·배치 정보의 형식을 확인하지 못했습니다.')


def _positive(value, maximum=65536):
    return type(value) in (int, float) and 0 < value <= maximum and math.isfinite(value)


def _coordinate(value):
    return type(value) is int and abs(value) <= 65536


def logical_dimensions(width, height, scale, transform, layout_mode):
    """Match Mutter's float32 scale/division before integral-size validation."""
    if transform % 2:
        width, height = height, width
    if layout_mode == 2:
        if float(scale).is_integer():
            return width, height
        raise ValueError('물리 좌표 방식에서는 정수 화면 배율을 선택하세요.')
    try:
        scale32 = struct.unpack('f', struct.pack('f', scale))[0]
        if scale32 <= 0:
            raise ValueError()
        width = struct.unpack('f', struct.pack('f', width / scale32))[0]
        height = struct.unpack('f', struct.pack('f', height / scale32))[0]
        if not width.is_integer() or not height.is_integer() or min(width, height) < 1:
            raise ValueError()
        return int(width), int(height)
    except (OverflowError, struct.error, ValueError) as exc:
        raise ValueError('선택한 배율의 논리 크기를 정수 좌표로 확인하지 못했습니다.') from exc


def _spec(value):
    _require(isinstance(value, list) and len(value) == 4
             and all(isinstance(part, str) and len(part) <= 256 for part in value)
             and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value[0]))
    return value[0]


def read_settings(raw):
    """Normalize current state; do not expose monitor vendor/product/serial."""
    try:
        return _read_settings(raw)
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError):
        return {'state': 'unknown', 'apply_supported': False,
                'reason': '모니터별 지원 모드·배율을 확인하지 못했습니다. GNOME 화면 설정을 확인하세요.'}


def _read_settings(raw):
    _require(isinstance(raw, list) and len(raw) == 4)
    serial, physical, logical, properties = raw
    _require(type(serial) is int and 0 <= serial < 2**32
             and isinstance(physical, list) and 0 < len(physical) <= 64
             and isinstance(logical, list) and 0 < len(logical) <= 64
             and isinstance(properties, dict))
    # Missing layout-mode cannot safely be guessed from physical pixels.
    layout_mode = properties.get('layout-mode')
    global_scale = properties.get('global-scale-required', False)
    _require(type(layout_mode) is int and layout_mode in (1, 2) and type(global_scale) is bool)
    outputs, specs = {}, {}
    for monitor in physical:
        _require(isinstance(monitor, list) and len(monitor) == 3)
        spec, modes, props = monitor
        connector = _spec(spec)
        _require(connector not in outputs and isinstance(props, dict)
                 and isinstance(modes, list) and len(modes) <= 512)
        normalized, current = [], None
        for mode in modes:
            _require(isinstance(mode, list) and len(mode) == 7)
            mode_id, width, height, refresh, preferred, scales, attributes = mode
            _require(isinstance(mode_id, str) and 0 < len(mode_id) <= 256
                     and mode_id not in {item['id'] for item in normalized}
                     and type(width) is int and _positive(width)
                     and type(height) is int and _positive(height)
                     and _positive(refresh, 10000) and _positive(preferred, 16)
                     and isinstance(scales, list) and 0 < len(scales) <= 64
                     and all(_positive(scale, 16) for scale in scales)
                     and len(set(scales)) == len(scales) and preferred in scales
                     and isinstance(attributes, dict))
            for flag in ('is-current', 'is-preferred', 'is-interlaced'):
                _require(flag not in attributes or type(attributes[flag]) is bool)
            _require(attributes.get('refresh-rate-mode', 'fixed') in ('fixed', 'variable'))
            if attributes.get('is-current'):
                _require(current is None)
                current = mode_id
            normalized.append({'id': mode_id, 'width': width, 'height': height,
                               'refresh_hz': refresh, 'preferred_scale': preferred,
                               'supported_scales': scales[:],
                               'refresh_mode': attributes.get('refresh-rate-mode', 'fixed'),
                               'interlaced': attributes.get('is-interlaced', False),
                               'preferred': attributes.get('is-preferred', False)})
        outputs[connector] = {'id': connector, 'modes': normalized,
                              'current_mode_id': current, 'active': False,
                              'hardware_key': hashlib.sha256(json.dumps(spec, separators=(',', ':')).encode()).hexdigest()}
        specs[connector] = spec
    normalized_logical, assigned = [], set()
    for item in logical:
        _require(isinstance(item, list) and len(item) == 7)
        x, y, scale, transform, primary, members, props = item
        _require(_coordinate(x) and _coordinate(y) and _positive(scale, 16)
                 and type(transform) is int and 0 <= transform <= 7
                 and type(primary) is bool and isinstance(props, dict)
                 and isinstance(members, list) and 0 < len(members) <= 64)
        connectors = []
        for spec in members:
            connector = _spec(spec)
            _require(connector in outputs and spec == specs[connector] and connector not in assigned)
            output = outputs[connector]
            mode = next((m for m in output['modes'] if m['id'] == output['current_mode_id']), None)
            _require(mode is not None and scale in mode['supported_scales'])
            output['active'] = True
            assigned.add(connector)
            connectors.append(connector)
        normalized_logical.append({'connectors': sorted(connectors), 'x': x, 'y': y,
                                   'scale': scale, 'transform': transform, 'primary': primary})
    _require(sum(item['primary'] for item in normalized_logical) == 1)
    _require(not global_scale or len({item['scale'] for item in normalized_logical}) == 1)
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(',', ':'),
                                      allow_nan=False).encode()).hexdigest()
    result = {'state': 'ok', 'serial': serial, 'revision': digest, 'layout_mode': layout_mode,
              'global_scale_required': global_scale, 'outputs': list(outputs.values()),
              'logical_outputs': normalized_logical, 'apply_supported': False}
    if all(len(item['connectors']) == 1 for item in normalized_logical):
        result['edit_template'] = {'schema_version': 1, 'expected_revision': digest,
                                  'outputs': [dict(id=item['connectors'][0],
                                                   mode_id=outputs[item['connectors'][0]]['current_mode_id'],
                                                   **{k: item[k] for k in ('x', 'y', 'scale')})
                                              for item in normalized_logical]}
    return result


def build_plan(settings, request):
    """Validate a complete active layout against a fresh mode inventory.

    Output IDs keep site assignments attached to their original monitor. Primary,
    rotation and unspecified modes are never inferred from a spatial position.
    """
    if settings.get('state') != 'ok':
        raise ValueError('지원 모드 조회를 완료한 뒤 다시 계획하세요.')
    _require(isinstance(request, dict) and set(request) == {'schema_version', 'expected_revision', 'outputs'}
             and type(request['schema_version']) is int and request['schema_version'] == 1)
    if request['expected_revision'] != settings['revision']:
        raise ValueError('모니터 상태가 변경됐습니다. 다시 조회한 값으로 계획하세요.')
    if any(len(item['connectors']) != 1 for item in settings['logical_outputs']):
        raise ValueError('복제 출력 배치는 현재 편집 범위에서 지원하지 않습니다.')
    current = {item['connectors'][0]: item for item in settings['logical_outputs']}
    outputs = {item['id']: item for item in settings['outputs']}
    entries = request['outputs']
    _require(isinstance(entries, list) and len(entries) == len(current))
    planned, seen = [], set()
    for entry in entries:
        _require(isinstance(entry, dict) and set(entry) == {'id', 'x', 'y', 'mode_id', 'scale'})
        connector = entry['id']
        _require(isinstance(connector, str) and connector in current and connector not in seen)
        seen.add(connector)
        _require(_coordinate(entry['x']) and _coordinate(entry['y']))
        mode = next((mode for mode in outputs[connector]['modes'] if mode['id'] == entry['mode_id']), None)
        if mode is None:
            raise ValueError('선택한 모니터에서 지원하는 해상도·주사율을 다시 선택하세요.')
        if not _positive(entry['scale'], 16) or entry['scale'] not in mode['supported_scales']:
            raise ValueError('선택한 모드에서 지원하는 화면 배율을 다시 선택하세요.')
        width, height = logical_dimensions(mode['width'], mode['height'], entry['scale'],
                                           current[connector]['transform'], settings['layout_mode'])
        planned.append(deepcopy(entry) | {'width': int(width), 'height': int(height),
                                         'transform': current[connector]['transform'],
                                         'primary': current[connector]['primary'],
                                         'refresh_hz': mode['refresh_hz']})
    if settings['global_scale_required'] and len({item['scale'] for item in planned}) != 1:
        raise ValueError('현재 GNOME 환경은 모든 모니터에 같은 화면 배율을 요구합니다.')
    min_x, min_y = min(p['x'] for p in planned), min(p['y'] for p in planned)
    for p in planned:
        p['x'] -= min_x
        p['y'] -= min_y
    _require(all(p['x'] + p['width'] <= 65536 and p['y'] + p['height'] <= 65536 for p in planned))
    neighbors = {p['id']: set() for p in planned}
    for index, a in enumerate(planned):
        for b in planned[index + 1:]:
            x_overlap = min(a['x'] + a['width'], b['x'] + b['width']) - max(a['x'], b['x'])
            y_overlap = min(a['y'] + a['height'], b['y'] + b['height']) - max(a['y'], b['y'])
            if x_overlap > 0 and y_overlap > 0:
                raise ValueError('모니터가 겹칩니다. 배치를 조정하세요.')
            if (x_overlap == 0 and y_overlap > 0) or (y_overlap == 0 and x_overlap > 0):
                neighbors[a['id']].add(b['id'])
                neighbors[b['id']].add(a['id'])
    connected, pending = set(), [planned[0]['id']]
    while pending:
        node = pending.pop()
        if node not in connected:
            connected.add(node)
            pending.extend(neighbors[node] - connected)
    if len(connected) != len(planned):
        raise ValueError('모니터 사이에 빈틈이 있습니다. 화면 가장자리를 연결하세요.')
    return {'state': 'validated', 'apply_supported': False, 'backend_verified': False,
            'expected_revision': settings['revision'], 'layout_mode': settings['layout_mode'],
            'outputs': planned, 'site_assignments': 'preserved',
            'reason': '읽기 전용 배치 검사를 통과했습니다. 이 결과만으로 OS를 변경하지 않습니다.'}


def load_request(path):
    """Bounded, unambiguous JSON input; never evaluate configuration as code."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('배치 파일에 중복된 항목이 있습니다.')
            result[key] = value
        return result

    def constant(_):
        raise ValueError('배치 파일의 숫자가 올바르지 않습니다.')

    with path.open('rb') as stream:
        data = stream.read(1048577)
    if len(data) > 1048576:
        raise ValueError('배치 파일은 1 MiB 이하여야 합니다.')
    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError('배치 파일을 JSON으로 읽지 못했습니다.') from exc
