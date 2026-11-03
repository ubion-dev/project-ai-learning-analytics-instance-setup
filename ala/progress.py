"""Pure projection of setup events and checks into one current row per stage."""
from __future__ import annotations

import json
from collections import OrderedDict

from .model import redact
from .substeps import child_rows

STAGES = (
    ('base', '기본 패키지·Chrome'), ('update', '자동 업데이트 차단'),
    ('hardware', '장치 드라이버'), ('shell', '터미널 환경'),
    ('ssh', 'SSH·방화벽'), ('docker', 'Docker'), ('gpu', 'GPU 드라이버'),
    ('gpu_container', 'GPU 컨테이너'), ('dev_tools', '선택 도구'),
    ('display', '화면 절전·잠금'), ('chrome', '자동 로그인·Chrome'),
)
STATE_LABELS = {
    'ok': '✓ 현재 정상', 'kept': '✓ 정상 유지', 'completed': '✓ 완료',
    'running': '● 진행 중', 'waiting': '… 실행 대기', 'not_run': '— 미실행',
    'change_required': '자동 설정 대상', 'pending': '◷ 적용 대기',
    'blocked': '! 조치 필요', 'error': '× 실패', 'unknown': '? 확인 불가',
    'interrupted': '! 중단됨', 'not_applicable': '— 해당 없음',
}
STATE_STYLES = {
    'ok': 'green', 'kept': 'green', 'completed': 'green', 'running': 'bold cyan',
    'waiting': 'dim', 'not_run': 'dim', 'change_required': 'yellow', 'pending': 'yellow',
    'blocked': 'bold red', 'error': 'bold red', 'unknown': 'red',
    'interrupted': 'bold red', 'not_applicable': 'dim',
}
_PRIORITY = {'error': 7, 'blocked': 6, 'unknown': 5, 'change_required': 4,
             'pending': 3, 'ok': 2, 'not_applicable': 1}
_EVENT_STATES = {'kept': 'kept', 'completed': 'completed', 'started': 'running',
                 'pending': 'pending', 'blocked': 'blocked', 'failed': 'error'}
_ACTIVE = {'queued', 'running'}
_LABELS = dict(STAGES)
_DIAGNOSTICS = {'platform': '운영체제 진단', 'user': '화면 계정 진단',
                'network': '네트워크 진단', 'execution': '실행 환경 진단'}
_DISPLAY_POLICY = ('display.gnome', 'display.dconf', 'display.logind', 'display.mask.',
                   'display.service', 'display.sleep')


def _family(identifier):
    """Match the actual inspector families, including the final display integration."""
    identifier = str(identifier)
    if identifier.startswith(('gpu.runtime.', 'gpu.package.nvidia-container-toolkit', 'gpu_container')):
        return 'gpu_container'
    if identifier == 'display' or identifier == 'display_policy' or identifier.startswith(_DISPLAY_POLICY):
        return 'display'
    if identifier.startswith(('display.', 'chrome.')) or identifier == 'extra_display':
        return 'chrome'
    first = identifier.split('.', 1)[0]
    return {'updates': 'update', 'tools': 'dev_tools', 'bash': 'shell', 'time': 'base'}.get(first, first)


def _aggregate(items):
    if not items:
        return 'unknown'
    states = [row.get('state') if row.get('state') in _PRIORITY else 'unknown' for row in items]
    return max(states, key=_PRIORITY.__getitem__)


def _value(value):
    value = redact(value)
    if value is None:
        return '미확인'
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _detail(items, state, event=None, job=None, *, diagnostic=False):
    lines = [STATE_LABELS[state]]
    if diagnostic:
        lines.append('설치 단계와 별도로 확인한 진단 결과입니다.')
    if state == 'not_run':
        lines.append('이 작업에서 실행하지 않은 단계입니다. 현재 검사 결과는 아래를 확인하세요.')
    elif state == 'waiting':
        lines.append('아직 이 단계에 도달하지 않았습니다.')
    elif job and job.get('state') in _ACTIVE and state in {'kept', 'completed'}:
        lines.append('전체 최종 상태는 작업 종료 후 다시 확인합니다.')
    if items and job and job.get('state') in _ACTIVE:
        lines.append('아래 검사 값은 실행 전 조회 결과입니다. 최종 상태는 작업 종료 후 확인합니다.')
    if job and job.get('query_warning'):
        lines.append(_value(job['query_warning']))
    if job and job.get('state') == 'interrupted' and event and event.get('state') == 'started':
        lines.append('실행 중이던 작업이 중단됐습니다. 계속 실행 중으로 판단하지 않습니다.')
    issues = [row for row in items if row.get('state') not in {'ok', 'not_applicable'}]
    issues.sort(key=lambda row: _PRIORITY.get(row.get('state'), 5), reverse=True)
    for row in issues:
        lines.append('')
        lines.append(str(row.get('id', '검사 항목')))
        if row.get('next_action'):
            lines.append('다음: ' + _value(row['next_action']))
        lines.append('현재: ' + _value(row.get('actual')))
        if row.get('expected') is not None:
            lines.append('기대: ' + _value(row['expected']))
        if row.get('detail'):
            lines.append(_value(row['detail']))
    if items and not issues:
        lines.append(f'정상 {sum(row.get("state") == "ok" for row in items)}개 · 해당 없음 {sum(row.get("state") == "not_applicable" for row in items)}개')
    elif not items:
        lines.append('이 단계의 실제 검사 결과가 없습니다. 완료 검증으로 단정하지 않습니다.')
    if job and job.get('reason'):
        lines.append('작업 안내: ' + _value(job['reason']))
    return '\n'.join(lines)


def progress_rows(report=None, job=None):
    """Return stable ordered rows without mutating input or inferring completion.

    Live completion events describe module processing. A terminal report's actual
    checks override those events, and unvisited stages remain explicitly unrun.
    """
    report = report if isinstance(report, dict) else {}
    job = job if isinstance(job, dict) else None
    final = job.get('report') if job else None
    terminal = bool(job and job.get('state') not in _ACTIVE)
    if terminal:
        observed = final if isinstance(final, dict) else {}
    else:
        observed = report
    grouped = OrderedDict((identifier, []) for identifier, _ in STAGES)
    for row in observed.get('results', []):
        if isinstance(row, dict):
            grouped.setdefault(_family(row.get('id', 'unknown')), []).append(row)
    latest = {}
    for event in (job or {}).get('stages', []):
        if isinstance(event, dict) and isinstance(event.get('id'), str):
            latest[event['id']] = event
            grouped.setdefault(event['id'], [])
    gpu_absent = any(row.get('id') == 'gpu' and row.get('state') == 'not_applicable'
                     for row in observed.get('results', []) if isinstance(row, dict))
    rows = []
    for identifier, items in grouped.items():
        diagnostic = identifier not in _LABELS and identifier not in latest
        actual = _aggregate(items)
        event = latest.get(identifier)
        event_state = event.get('state') if event else None
        if identifier == 'gpu_container' and gpu_absent and not event:
            state = 'not_applicable'
        elif not job or diagnostic:
            state = actual
        elif terminal:
            if event_state in {'failed', 'blocked'}:
                state = 'error' if event_state == 'failed' else 'blocked'
            elif actual == 'not_applicable':
                state = 'not_applicable'
            elif not event:
                state = 'not_run'
            elif actual in {'error', 'blocked', 'unknown', 'change_required', 'pending'}:
                state = actual
            elif event_state in {'kept', 'completed'}:
                state = event_state
            elif event_state == 'pending':
                state = 'pending'
            else:
                state = 'interrupted' if job.get('state') == 'interrupted' else 'unknown'
            if event_state == 'started' and job.get('state') == 'interrupted':
                state = 'interrupted'
        elif event and event_state != 'kept':
            state = _EVENT_STATES.get(event_state, 'unknown')
        elif actual == 'not_applicable':
            state = 'not_applicable'
        elif not event:
            state = 'waiting'
        else:
            state = _EVENT_STATES.get(event_state, 'unknown')
        if job and job.get('query_warning') and state == 'running':
            state = 'unknown'
        label = _LABELS.get(identifier, _DIAGNOSTICS.get(identifier, identifier + ' 진단' if diagnostic else identifier))
        children, current_step = child_rows(identifier, report=report, job=job, parent_state=state)
        rows.append({'id': identifier, 'label': label, 'state': state,
                     'evidence': 'check' if items else 'event' if event else 'missing',
                     'detail': _detail(items, state, event, job, diagnostic=diagnostic),
                     'children': children, 'current_step': current_step})
    return rows
