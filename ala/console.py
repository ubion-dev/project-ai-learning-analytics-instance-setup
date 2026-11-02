"""Compact terminal entry: one setup action and an expected-state summary."""
from __future__ import annotations

import json
import os
import time
import queue
import threading

from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import __version__
from .model import redact
from .update import check as check_update

GROUPS = {
    'base': '기본 패키지·Chrome', 'time': '시간 동기화', 'shell': '터미널 환경',
    'tools': '선택 도구', 'ssh': 'SSH·방화벽', 'docker': 'Docker', 'gpu': 'GPU',
    'nvidia': 'NVIDIA', 'display': '자동 로그인·화면', 'chrome': 'DID 표시',
    'updates': '자동 업데이트 차단', 'update': '자동 업데이트 차단',
    'hardware': '장치 드라이버', 'network': '네트워크 진단', 'user': '화면 계정',
    'platform': '운영체제', 'prepare': '초기 OS 준비', 'bash': '터미널 환경',
}
LABELS = {'ok': '정상', 'change_required': '설정 필요', 'pending': '적용 대기',
          'blocked': '조치 필요', 'unknown': '확인 불가', 'error': '실패',
          'not_applicable': '해당 없음'}
PRIORITY = {'error': 7, 'blocked': 6, 'unknown': 5, 'change_required': 4,
            'pending': 3, 'ok': 2, 'not_applicable': 1}


def group_name(identifier):
    return GROUPS.get(str(identifier).split('.')[0], str(identifier).split('.')[0])


def summarize(report, console=None):
    console = console or Console(highlight=False)
    rows = report.get('results', []) + report.get('preparation', {}).get('results', [])
    active = [job for job in report.get('runs', []) if job.get('state') in {'queued', 'running'}]
    if active:
        console.print(Text(f"설정 작업 진행 중 · {active[0].get('run_id')}"), style='yellow')
    if report.get('hostname'):
        console.print(Text('상태 확인 · ' + str(report['hostname'])))
    if not rows:
        console.print('검사 결과가 없습니다. 권한과 오류 메시지를 확인하세요.')
        return
    groups = {}
    for row in rows:
        groups.setdefault(group_name(row.get('id', '')), []).append(row)
    table = Table(box=None, show_header=False, padding=(0, 2, 0, 0))
    for name, items in groups.items():
        state = max((r.get('state', 'unknown') for r in items), key=lambda value: PRIORITY.get(value, 5))
        color = 'green' if state == 'ok' else 'dim' if state == 'not_applicable' else 'yellow' if state in {'pending', 'change_required'} else 'red'
        table.add_row(Text(name), Text(LABELS.get(state, '확인 불가'), style=color))
    console.print(table)
    issues = [r for r in rows if r.get('state') in {'blocked', 'unknown', 'error'}]
    pending = [r for r in rows if r.get('state') in {'change_required', 'pending'}]
    # Show causes; keep long inventories and normal values in --json.
    for row in (issues + pending)[:6]:
        name = str(row.get('id', ''))
        current = redact(row.get('actual'))
        current = '미확인' if current is None else json.dumps(current, ensure_ascii=False) if not isinstance(current, str) else current
        current = current[:300]
        console.print(Text(f"  {name}: {LABELS.get(row.get('state'), '확인 필요')} · {current}"))
        if row.get('next_action'):
            console.print(Text('  → ' + str(redact(row['next_action']))))
    if len(issues) + len(pending) > 6:
        console.print('  나머지 세부 항목: ala check --json', style='dim')
    if not active and all(row.get('state') in {'ok', 'not_applicable'} for row in rows):
        console.print('기대 설정과 일치합니다. 실제 화면 표시는 모니터에서 확인하세요.', style='green')
    elif issues:
        console.print('확인할 항목이 있습니다. 위의 원인과 조치를 확인하세요.', style='dim')
    elif any(row.get('state') == 'change_required' for row in rows):
        console.print('설정이 필요한 항목은 간편 설정으로 보완할 수 있습니다.', style='dim')
    elif active:
        console.print('설정 작업이 끝나면 상태를 다시 확인하세요.', style='dim')
    else:
        console.print('적용 대기 항목은 안내된 재부팅·재로그인 후 다시 확인하세요.', style='dim')


def simple_setup(service, console, *, sleep=time.sleep):
    value = service.request('config-show')
    config = value['config']
    user = config.get('user')
    if not user:
        raise ValueError('기존 일반 사용자로 SSH 접속한 뒤 ala를 실행하세요. root 직접 접속 시에는 화면 계정을 먼저 지정하세요.')
    mode = 'DID 표시' if config.get('url') and config.get('autostart') else '바탕화면'
    console.print(Text(f'간편 설정 시작 · 계정 {user} · {mode}'))
    job = service.request('submit', {'expected_config': config})
    console.print(Text(f"실행 ID: {job['run_id']} · SSH가 끊겨도 계속 실행됩니다."), style='dim')
    seen = set()
    while True:
        for stage in job.get('stages', []):
            marker = (stage.get('id'), stage.get('state'), stage.get('time'))
            if marker in seen:
                continue
            seen.add(marker)
            state = {'started': '진행 중', 'completed': '완료', 'kept': '정상 유지', 'failed': '실패',
                     'blocked': '조치 필요', 'pending': '적용 대기'}.get(stage.get('state'), stage.get('state'))
            console.print(Text(f"  {group_name(stage.get('id'))} · {state}"))
        if job.get('query_warning'):
            console.print(Text(job['query_warning']))
            console.print('진행 확인: ala runs list', style='dim')
            return 1
        if job.get('state') not in {'queued', 'running'}:
            break
        sleep(1)
        job = service.request('runs-show', {'run_id': job['run_id']})
    return show_job(job, console)


def show_job(job, console=None):
    console = console or Console(highlight=False)
    if job.get('reason'):
        console.print(Text(str(redact(job['reason']))), style='red')
    if job.get('report'):
        summarize(job['report'], console)
    else:
        console.print(Text({'completed': '설정 완료', 'pending': '추가 적용 대기', 'failed': '설정 실패',
                            'interrupted': '작업이 중단되었습니다.'}.get(job.get('state'), '결과 확인 필요')))
    return job.get('exit_code', 1)


def run_console(service, *, console=None, read_line=None, update_check=check_update):
    console = console or Console(highlight=False)
    read_line = read_line or input
    role = '관리자' if os.geteuid() == 0 else '관리자 작업 연결'
    console.print(Text(f'ala {__version__}  ·  {role}', style='bold'))
    console.print('Enter 간편 설정  ·  s 상태 확인  ·  q 종료', style='dim')
    notices = queue.Queue()
    if update_check is not None:
        def fetch_update():
            try:
                notices.put(update_check())
            except Exception:
                pass  # Release discovery never blocks management.
        threading.Thread(target=fetch_update, daemon=True).start()
    def notify_update():
        try:
            result = notices.get_nowait()
        except queue.Empty:
            return
        if result.get('available'):
            console.print(Text(f"새 버전 {result['latest']} · ala update로 갱신하세요."), style='cyan')
    while True:
        notify_update()
        try:
            choice = read_line('> ').strip().lower()
            notify_update()
        except EOFError:
            return 0
        if choice in {'q', 'quit', 'exit'}:
            return 0
        if choice in {'', 'setup'}:
            try:
                simple_setup(service, console)
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                console.print(Text(f'실패: {redact(str(exc))}'), style='red')
        elif choice in {'s', 'status', 'check'}:
            try:
                summarize(service.request('status'), console)
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                console.print(Text(f'확인 실패: {redact(str(exc))}'), style='red')
        else:
            console.print('Enter 간편 설정 · s 상태 확인 · q 종료', style='dim')
