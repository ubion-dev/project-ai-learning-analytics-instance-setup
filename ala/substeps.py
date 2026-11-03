"""Named setup tasks and honest projection of structured substep observations.

Patterns name actual inspector result IDs. Empty patterns describe actions whose
completion is known only from an explicit script marker, never guessed from time.
"""
from __future__ import annotations

from fnmatch import fnmatchcase
import json

from .model import redact


def task(identifier, label, *checks, option=None):
    value = {'id': identifier, 'label': label, 'checks': checks}
    if option:
        value['option'] = option
    return value


STAGE_TASKS = {
    'base': [
        task('packages', '기본 패키지 설치', 'base.package.*', '!base.package.google-chrome-stable'),
        task('chrome', 'Chrome 설치·실행 확인', 'base.package.google-chrome-stable', 'base.chrome.version'),
        task('chrome_policy', 'Chrome 번역·복원 안내 정책', 'base.chrome.policy*', 'base.chrome.directory.*'),
        task('chrome_launcher', 'Chrome 실행 도구 준비', 'base.chrome.launcher'),
        task('timezone', '시간대 설정', 'base.timezone'),
        task('time_sync', '시간 동기화 서비스', 'base.service.chronyd.*'),
    ],
    'update': [
        task('offline_check', '진행·예약된 업데이트 확인', 'update.offline.*', 'update.transaction.*'),
        task('desktop_policy', 'GNOME 자동 업데이트 차단', 'update.dconf.*'),
        task('units', '자동 업데이트 서비스·타이머 차단', 'update.mask.*'),
    ],
    'hardware': [
        task('detection', '장치·실행 커널 조사', 'hardware.inventory.*', 'hardware.kernel', 'hardware.boot'),
        task('firmware', '공식 펌웨어 준비', 'hardware.package.*firmware*'),
        task('modules', '커널 모듈 준비·재부팅 상태', 'hardware.package.kernel*', 'hardware.reboot', 'hardware.apply', 'hardware.device.*'),
    ],
    'shell': [
        task('packages', 'Bash·자동완성 패키지', 'shell.package.*'),
        task('user_shell', '일반 사용자 Bash·테마', 'shell.*', '!shell.package.*', '!shell.root*'),
        task('root_shell', 'root Bash·테마 (선택)', 'shell.root*', option='root_bash'),
    ],
    'ssh': [
        task('packages', 'SSH·방화벽 패키지', 'ssh.package.*'),
        task('sshd', 'SSH 설정·호스트 키 확인', 'ssh.syntax', 'ssh.ports'),
        task('firewall', '현재 SSH 포트 방화벽 허용', 'ssh.rules', 'ssh.zone', 'ssh.firewall.*'),
        task('services', 'SSH·방화벽 부팅 등록', 'ssh.service.*'),
    ],
    'docker': [
        task('conflicts', '기존 컨테이너 패키지 충돌 확인', 'docker.conflict.*'),
        task('repository', 'Docker 공식 저장소 준비'),
        task('packages', 'Docker·Compose 패키지 설치', 'docker.package.*'),
        task('logging', '기존 Docker 로그 정책 보존·보완', 'docker.config*', 'docker.runtime.logging'),
        task('services', 'Docker·containerd 부팅 등록', 'docker.service.*'),
        task('verify', 'Docker·Compose 실행 확인', 'docker.info', 'docker.compose', 'docker.runtime'),
    ],
    'gpu': [
        task('detection', 'NVIDIA GPU 감지', 'gpu.detection'),
        task('compatibility', '기존 드라이버·Secure Boot·지원 확인', 'gpu.secure_boot', 'gpu.support'),
        task('build_packages', '실행 커널용 빌드 도구 준비'),
        task('repository', 'NVIDIA 공식 저장소 준비'),
        task('driver', 'NVIDIA 드라이버 설치', 'gpu.driver'),
        task('verify', '드라이버 설치·재부팅 확인', 'gpu.driver'),
    ],
    'gpu_container': [
        task('host_check', '호스트 GPU·Docker 사전 확인'),
        task('repository', 'NVIDIA Container 저장소 준비'),
        task('packages', 'NVIDIA Container Toolkit 설치', 'gpu.package.nvidia-container-toolkit'),
        task('runtime', 'Docker GPU runtime 설정', 'gpu.runtime.config'),
        task('verify', '실행 중 GPU runtime 확인', 'gpu.runtime.live', 'gpu_container.apply'),
    ],
    'dev_tools': [
        task('repository', '선택 도구 저장소 준비'),
        task('packages', 'Neovim·btop·tmux 설치', 'dev_tools.package.*'),
    ],
    'display': [
        task('schema_check', 'GNOME 화면 정책 지원 확인', 'display.gnome'),
        task('desktop_policy', '유휴 화면·잠금 방지', 'display.dconf.*'),
        task('system_sleep', '시스템 절전·최대절전 방지', 'display.logind*', 'display.mask.*'),
    ],
    'chrome': [
        task('autologin', 'GDM 자동 로그인·그래픽 부팅', 'display.gdm', 'display.gdm_enabled', 'display.autologin', 'display.default_target', 'display.configuration'),
        task('profile', '표시 프로필·첫 실행 안내 준비', 'display.initial_setup', 'chrome.account', 'chrome.option.user-data-dir'),
        task('service', 'Chrome 자동 시작 등록', 'chrome.unit', 'chrome.autostart'),
        task('session', '로그인·Chrome 실행 상태 확인', 'display.session*', 'display.login_screen', 'display.gdm_runtime', 'display.gdm_diagnostics', 'chrome.runtime*', 'chrome.url', 'chrome.environment', 'chrome.legacy', 'chrome.process'),
        task('policy', 'Chrome 실행 옵션·실제 화면 정책 확인', 'display.live.*', 'chrome.option.*', '!chrome.option.user-data-dir', 'chrome.forbidden'),
    ],
}

_PRIORITY = {'error': 7, 'blocked': 6, 'unknown': 5, 'change_required': 4,
             'pending': 3, 'ok': 2, 'not_applicable': 1}
_EVENTS = {'started': 'running', 'completed': 'completed', 'kept': 'kept',
           'pending': 'pending', 'failed': 'error', 'blocked': 'blocked'}
_ACTIVE = {'queued', 'running'}
_EVENT_LABELS = {'started': '진행 중', 'completed': '처리 완료', 'kept': '정상 유지',
                 'pending': '적용 대기', 'failed': '실패', 'blocked': '조치 필요'}


def step_ids(stage):
    return {entry['id'] for entry in STAGE_TASKS.get(stage, ())}


def _matches(identifier, patterns):
    return (any(fnmatchcase(identifier, pattern) for pattern in patterns if not pattern.startswith('!'))
            and not any(fnmatchcase(identifier, pattern[1:]) for pattern in patterns if pattern.startswith('!')))


def _state(items):
    states = [item.get('state') if item.get('state') in _PRIORITY else 'unknown' for item in items]
    return max(states, key=_PRIORITY.__getitem__) if states else 'unknown'


def _value(value):
    value = redact(value)
    if value is None:
        return '미확인'
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def child_rows(stage, *, report=None, job=None, parent_state='unknown'):
    """Return children with observation provenance, without inventing live work.

    Evidence describes an available individual check or valid event, not proof
    of execution or final verification. Missing evidence never implies failure.
    """
    catalog = STAGE_TASKS.get(stage, ())
    report = report if isinstance(report, dict) else {}
    job = job if isinstance(job, dict) else None
    active = bool(job and job.get('state') in _ACTIVE)
    terminal = bool(job and not active)
    observed = (job.get('report') or {}) if terminal else report
    results = observed.get('results', [])
    config = report.get('config', {})
    events = [event for event in (job or {}).get('substeps', [])
              if isinstance(event, dict) and event.get('stage') == stage and event.get('id') in step_ids(stage)]
    latest = {event['id']: event for event in events}
    last = events[-1] if events else None
    current_id = (last['id'] if active and parent_state == 'running' and last
                  and last.get('state') == 'started' and not job.get('query_warning') else None)
    rows = []
    current = ''
    for entry in catalog:
        items = [item for item in results if isinstance(item, dict) and _matches(str(item.get('id', '')), entry['checks'])]
        actual = _state(items)
        event = latest.get(entry['id'])
        event_state = event.get('state') if event else None
        evidence = 'event' if event_state in _EVENTS else 'check' if items else 'missing'
        disabled = entry.get('option') in config and config[entry['option']] is False
        if parent_state == 'not_applicable' or disabled:
            state = 'not_applicable'
        elif not job:
            state = actual
        elif terminal:
            if event_state in {'failed', 'blocked'}:
                state = _EVENTS[event_state]
            elif actual == 'not_applicable':
                state = actual
                evidence = 'check'
            elif actual in {'error', 'blocked', 'change_required', 'pending'}:
                state = actual
                evidence = 'check'
            elif not event:
                if parent_state in {'waiting', 'not_run'}:
                    state = 'not_run'
                elif items:
                    # Native stages may have no substep markers: a final check
                    # proves current state, not whether this task executed.
                    state = actual
                else:
                    state = 'not_run' if parent_state == 'kept' else 'unknown'
            elif event_state == 'started':
                state = ('interrupted' if job.get('state') == 'interrupted' else
                         'error' if parent_state in {'error', 'blocked'} else 'unknown')
            elif items and actual == 'unknown':
                state = 'unknown'
                evidence = 'check'
            elif not isinstance(job.get('report'), dict) or not results:
                state = 'unknown'
            else:
                state = _EVENTS.get(event_state, 'unknown')
        elif event:
            state = _EVENTS.get(event_state, 'unknown')
            if state == 'running' and entry['id'] != current_id:
                state = 'unknown'
        elif actual == 'not_applicable':
            state = 'not_applicable'
        elif parent_state in {'waiting', 'not_run'}:
            state = parent_state
        else:
            state = 'waiting' if parent_state == 'running' else 'unknown'
        if active and job.get('query_warning') and event_state == 'started':
            state = 'unknown'
        details = [entry['label']]
        if terminal and parent_state == 'not_run' and actual == 'change_required':
            details.append('이번 작업에서 이 단계는 실행되지 않았습니다. 선행 단계의 결과를 확인한 뒤 간편 설정을 다시 실행하세요.')
        if event:
            details.append('세부 작업 기록: ' + _EVENT_LABELS.get(event_state, '확인 불가'))
        elif active:
            details.append('이 세부 작업의 실행 신호가 아직 없습니다. 상위 진행만으로 현재 작업을 추정하지 않습니다.')
        elif not items:
            details.append('이 세부 항목의 개별 검사 결과가 없습니다.')
        if active and items:
            details.append('아래 값은 실행 전 조회 결과입니다.')
        ordered = sorted(items, key=lambda item: _PRIORITY.get(item.get('state'), 5), reverse=True)
        for index, item in enumerate(ordered):
            if index:
                details.append('')
            actual_value = _value(item.get('actual'))
            long_value = len(actual_value) > 160 or '\n' in actual_value
            if long_value and item.get('next_action'):
                details.append('다음: ' + _value(item['next_action']))
            details.append('현재: ' + actual_value)
            if not long_value and item.get('next_action'):
                details.append('다음: ' + _value(item['next_action']))
            details.append('검사 항목: ' + str(item.get('id', '')))
        rows.append({'id': stage + '.' + entry['id'], 'label': entry['label'],
                     'state': state, 'evidence': evidence, 'detail': '\n'.join(details)})
        if current_id == entry['id'] and state == 'running':
            current = entry['label']
    return rows, current
