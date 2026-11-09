"""Explicit, resumable preparation of blank displays after a graphical login.

Only the privileged setup worker requests this operation.  The user session
reads a small fence; it never submits configuration or invokes sudo at login.
"""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

from .config import Config, ScreenConfig
from .jobs import change_lock, protected_directory
from .runtime import RuntimeStore
from .system import Host, Paths, atomic_json, atomic_write

MARKER = '# ala managed initial display preparation'
SERVICE = 'ala-display-onboarding.service'
TIMER = 'ala-display-onboarding.timer'
PENDING_PHASES = {'installing', 'waiting', 'prepared', 'configured', 'starting'}


def _revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def _path(paths):
    return paths.at('/var/lib/ala/display-onboarding.json')


def _fence_path(paths):
    return paths.at('/var/lib/ala-runtime/display-onboarding.json')


def _load(paths, host=None):
    path = _path(paths)
    if not path.exists() and not path.is_symlink():
        return None
    value = RuntimeStore(paths, host)._read(path)
    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != 1
            or value.get('state') not in {'pending', 'completed', 'blocked', 'cancelled'}
            or not isinstance(value.get('request_id'), str)
            or not isinstance(value.get('release'), str)
            or not isinstance(value.get('expected_config'), dict)):
        raise ValueError('초기 화면 준비 기록을 확인할 수 없습니다.')
    Config.from_dict(value['expected_config'])
    if value.get('candidate') is not None:
        Config.from_dict(value['candidate'])
    return value


def _save(paths, value, host=None):
    store = RuntimeStore(paths, host)
    store._safe(_path(paths))
    protected_directory(paths.at('/var/lib/ala'))
    value['updated_at'] = datetime.now(timezone.utc).isoformat()
    atomic_json(_path(paths), value, 0o600)


def _fence(paths, value, *, pending, host=None):
    config = Config.from_dict(value['expected_config'])
    RuntimeStore(paths, host)._write(_fence_path(paths), {
        'schema_version': 1, 'request_id': value['request_id'], 'user': config.user,
        'pending': pending,
        'preview_pending': bool(value.get('preview_active') and value.get('state') == 'pending'),
        'expected_revision': _revision(value['expected_config']),
        'candidate_revision': _revision(value['candidate']) if value.get('candidate') else None,
    })


def pending_for_user(config, paths=None):
    return _fenced_for_user(config, paths, 'pending')


def preview_for_user(config, paths=None):
    return _fenced_for_user(config, paths, 'preview_pending')


def _fenced_for_user(config, paths, field):
    """Fail closed on a malformed root-owned fence; reading never creates it."""
    paths = paths or Paths()
    path = _fence_path(paths)
    if not path.exists() and not path.is_symlink():
        return False
    value = RuntimeStore(paths)._read(path)
    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != 1
            or type(value.get('pending')) is not bool or type(value.get('preview_pending', False)) is not bool
            or not isinstance(value.get('user'), str)
            or not isinstance(value.get('expected_revision'), str)
            or len(value['expected_revision']) != 64
            or set(value['expected_revision']) - set('0123456789abcdef')
            or (value.get('candidate_revision') is not None and
                (not isinstance(value['candidate_revision'], str) or len(value['candidate_revision']) != 64
                 or set(value['candidate_revision']) - set('0123456789abcdef')))):
        raise ValueError('초기 화면 준비의 로그인 보호 상태를 확인할 수 없습니다.')
    if value['user'] != config.user or not value.get(field, False):
        return False
    # Explicit configuration changes invalidate this request even if the setup
    # worker failed before it could register its retry timer. Read the global
    # protected model rather than a per-target context with legacy URL fields.
    current = Config.from_dict(RuntimeStore(paths)._read(paths.config))
    return _revision(current.to_dict()) in {value['expected_revision'], value.get('candidate_revision')}


def _summary(value):
    if value is None:
        return {'state': 'not_requested', 'reason': '초기 화면 준비 요청 없음'}
    return {key: value[key] for key in (
        'state', 'phase', 'request_id', 'run_id', 'reason', 'next_action', 'targets',
        'updated_at', 'start_results', 'schedule_warning', 'display_verified', 'registration_issue', 'preview') if key in value}


def status(*, paths=None, host=None):
    return _summary(_load(paths or Paths(), host))


def _eligible(config):
    if not config.display:
        return '화면 사용을 켠 뒤 초기 화면 준비를 선택하세요.'
    if not config.user:
        return '기존 일반 화면 계정을 지정하세요.'
    if any(screen.url for screen in config.screen_targets()):
        return '사이트가 설정된 DID는 초기 화면 준비로 변경하지 않습니다.'
    if any(screen.frontend.enabled for screen in config.screen_targets()):
        return '배포 연동이 설정된 DID는 초기 화면 준비로 변경하지 않습니다.'
    return ''


def _candidate(config, inventory):
    from .monitors import resolve_output
    outputs = inventory.get('logical_outputs') or []
    if not outputs or any(output.get('confirmed') is not True for output in outputs):
        return {'state': 'pending', 'reason': '연결된 모든 논리 화면의 좌표 확인을 기다립니다.'}
    if len(outputs) > 16 or len({output.get('id') for output in outputs}) != len(outputs):
        return {'state': 'blocked', 'reason': '초기 화면은 서로 다른 논리 출력 최대 16개를 지원합니다.'}
    ordered = sorted(outputs, key=lambda item: (not item.get('primary', False),
                                               item.get('y', 0), item.get('x', 0), item['id']))
    used = set()
    targets = []
    deferred = []
    for screen in config.screen_targets():
        if screen.output is None:
            deferred.append(screen)
            continue
        resolved = resolve_output(inventory, screen.output)
        if resolved.get('state') != 'ok':
            return {'state': 'pending', 'reason': '기존 미설정 DID의 출력 연결을 먼저 확인하세요.'}
        output = resolved['output']['id']
        if output in used:
            return {'state': 'blocked', 'reason': '초기 화면 준비 전에 겹치는 DID 배정을 정리하세요.'}
        used.add(output)
        targets.append(replace(screen, autostart=True))
    deferred.sort(key=lambda screen: (screen.id != 'default', screen.id))
    available = [output for output in ordered if output['id'] not in used]
    if len(deferred) > len(available):
        return {'state': 'pending', 'reason': '미설정 DID 수보다 연결된 논리 화면이 적습니다.'}
    for screen in deferred:
        output = available.pop(0)['id']
        targets.append(replace(screen, output=output, autostart=True))
        used.add(output)
    for output in available:
        identifier = 'display-' + hashlib.sha256(output['id'].encode()).hexdigest()[:12]
        if any(screen.id == identifier for screen in targets):
            return {'state': 'blocked', 'reason': '자동 생성 화면 ID와 기존 대상이 겹칩니다.'}
        targets.append(ScreenConfig(id=identifier, output=output['id'], autostart=True))
    targets.sort(key=lambda screen: (screen.id != 'default', screen.id))
    candidate = replace(config, schema_version=2, screens=tuple(targets))
    before = {screen.id: screen for screen in config.screen_targets()}
    changes = [{'id': screen.id, 'output': screen.output, 'created': screen.id not in before,
                'autostart_before': before[screen.id].autostart if screen.id in before else None,
                'autostart_after': screen.autostart} for screen in targets]
    return {'state': 'ready', 'reason': '연결된 논리 화면마다 초기 안내를 준비합니다.',
            'targets': [screen.to_dict() for screen in targets], 'changes': changes,
            'config': candidate.to_dict()}


def preview(config, *, paths=None, host=None):
    """Resolve a plan from protected config and read-only session observation."""
    paths, host = paths or Paths(), host or Host()
    reason = _eligible(config)
    if reason:
        return {'state': 'blocked', 'reason': reason, 'targets': [], 'changes': []}
    from .screens import _inventory, inspect_session
    session = inspect_session(config, host)
    if session != 'ok':
        return {'state': 'pending', 'reason': '단일 활성·잠금 해제 GNOME 세션을 기다립니다.',
                'next_action': '화면 계정으로 GNOME에 로그인한 뒤 자동 준비 상태를 확인하세요.',
                'targets': [], 'changes': []}
    try:
        inventory = _inventory(config, host, paths)
        if inventory.get('state') != 'ok':
            return {'state': 'pending', 'reason': 'GNOME 화면 확장의 출력 확인을 기다립니다.',
                    'next_action': '확장 첫 설치 후에는 GNOME에 다시 로그인하세요.',
                    'targets': [], 'changes': []}
        return _candidate(config, inventory)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return {'state': 'pending', 'reason': '초기 안내 대상 출력을 확인하지 못했습니다.',
                'targets': [], 'changes': []}


def _unit_files(release):
    from .display import _quote
    command = ' '.join(_quote(value) for value in (
        release / '.venv/bin/python', '-I', release / 'entry.py', '_display-onboarding-worker'))
    return {
        SERVICE: f'''{MARKER}
[Unit]
Description=ala initial blank display preparation
After=display-manager.service
[Service]
Type=oneshot
ExecStart={command}
UMask=0077
TimeoutStartSec=600
''',
        TIMER: f'''{MARKER}
[Unit]
Description=ala pending initial display preparation
[Timer]
OnBootSec=15s
OnUnitInactiveSec=15s
AccuracySec=1s
[Install]
WantedBy=timers.target
''',
    }


def _schedule(paths, host, release):
    store = RuntimeStore(paths, host)
    for name, text in _unit_files(release).items():
        path = paths.at('/etc/systemd/system/' + name)
        store._safe(path)
        if path.exists() and (not path.is_file() or MARKER not in path.read_text()):
            raise ValueError('관리되지 않은 초기 화면 준비 unit을 변경하지 않습니다.')
        if not path.exists() or path.read_text() != text:
            atomic_write(path, text, 0o644)
    for command in (['systemctl', 'daemon-reload'], ['systemctl', 'enable', '--now', TIMER]):
        if host.run(command).returncode:
            raise RuntimeError('초기 화면 준비 재시도 등록을 확인하지 못했습니다.')


def _finish(paths, host, value, state, reason):
    value.update(state=state, reason=reason)
    # Release before publishing a terminal state so a crash cannot leave a
    # completed/cancelled request with a fence no worker will revisit.
    _fence(paths, value, pending=False, host=host)
    _save(paths, value, host)
    if host.run(['systemctl', 'disable', '--now', TIMER]).returncode:
        value['schedule_warning'] = '초기 준비 재시도 timer의 해제를 확인하세요.'
        _save(paths, value, host)
    return _summary(value)


def _wait(paths, host, value, reason, next_action=None):
    value.update(state='pending', reason=reason)
    value['next_action'] = next_action or '다음 GNOME 로그인 후 초기 화면 준비 상태를 확인하세요.'
    _save(paths, value, host)
    return _summary(value)


def _show_preview(paths, host, value):
    """Best effort only: a preview failure never changes setup's outcome."""
    from .setup_preview import prepare
    store = RuntimeStore(paths, host)
    try:
        current = Config.from_dict(store._read(paths.config))
        if current.to_dict() not in (value['expected_config'], value.get('candidate')) or _eligible(current):
            return
        release, _ = store.release(value['release'])
        if not prepare(current, release, paths, host):
            return
        value['preview_active'] = True
        _fence(paths, value, pending=value.get('phase') != 'starting', host=host)
        # Publish intent before starting the independent user service.
        _save(paths, value, host)
        from .display import _user_call
        result = _user_call(current, host, paths, 'setup-preview-start')
        value['preview'] = {key: result.get(key) for key in ('state', 'reason', 'exit_code')}
        _save(paths, value, host)
    except (OSError, ValueError, RuntimeError, KeyError):
        value['preview'] = {'state': 'pending', 'reason': '로컬 설정 안내의 준비·시작을 확인하지 못했습니다.'}
        _save(paths, value, host)


def preview_ready(run_id, *, paths=None, host=None):
    """Setup worker callback after Chrome registration, before later stages."""
    paths, host = paths or Paths(), host or Host()
    value = _load(paths, host)
    if value and value.get('run_id') == run_id and value['state'] == 'pending' and value['phase'] == 'installing':
        _show_preview(paths, host, value)
        return value.get('preview')
    return None


def reserve(config, release, *, paths=None, host=None, locked=False, run_id=None):
    """Fence the initial login before the setup worker can install autostart."""
    paths, host = paths or Paths(), host or Host()
    if paths.root != Path('/') and type(host) is Host:
        raise RuntimeError('대체 경로에서 실제 systemd 변경을 허용하지 않습니다.')
    with nullcontext() if locked else change_lock(paths):
        from .screen_swap import guard
        guard(paths=paths, host=host)
        reason = _eligible(config)
        if reason:
            raise ValueError(reason)
        store = RuntimeStore(paths, host)
        release, _ = store.release(release)
        if store._read(paths.config) != config.to_dict():
            raise RuntimeError('검토 후 구성이 변경돼 초기 안내 준비를 예약하지 않았습니다.')
        existing = _load(paths, host)
        if existing and existing['state'] == 'pending':
            if config.to_dict() not in (existing['expected_config'], existing.get('candidate')):
                raise RuntimeError('기존 초기 화면 준비의 구성이 달라 현재 요청을 보존했습니다.')
            return _summary(existing)
        value = {'schema_version': 1, 'request_id': uuid.uuid4().hex, 'run_id': run_id,
                 'state': 'pending', 'phase': 'installing', 'release': str(release),
                 'expected_config': config.to_dict(), 'candidate': None,
                 'reason': '간편 설정의 Chrome 등록 완료를 기다립니다.',
                 'next_action': '간편 설정 작업의 진행 상태를 확인하세요.'}
        _save(paths, value, host)
        _fence(paths, value, pending=True, host=host)
        return _summary(value)


def setup_failed(run_id, *, paths=None, host=None, locked=False):
    """Keep blank unassigned login fenced; explicit later config is unaffected."""
    paths, host = paths or Paths(), host or Host()
    with nullcontext() if locked else change_lock(paths):
        value = _load(paths, host)
        if not value or value.get('run_id') != run_id or value.get('phase') != 'installing':
            return _summary(value)
        value.update(state='blocked', phase='install_failed',
                     reason='간편 설정이 Chrome 등록을 완료하지 못해 초기 안내를 시작하지 않았습니다.',
                     next_action='설치 실패 원인을 확인하고 초기 안내 옵션을 선택해 간편 설정을 다시 실행하세요.')
        _save(paths, value, host)
        _fence(paths, value, pending=True, host=host)
        return _summary(value)


def request(config, release, *, paths=None, host=None, locked=False, run_id=None):
    paths, host = paths or Paths(), host or Host()
    if paths.root != Path('/') and type(host) is Host:
        return {'state': 'blocked', 'reason': '대체 경로에서 실제 systemd 변경을 허용하지 않습니다.'}
    with nullcontext() if locked else change_lock(paths):
        from .screen_swap import guard
        guard(paths=paths, host=host)
        reason = _eligible(config)
        if reason:
            return {'state': 'blocked', 'reason': reason}
        store = RuntimeStore(paths, host)
        release, _ = store.release(release)
        current = Config.from_dict(store._read(paths.config))
        if current != config:
            return {'state': 'cancelled', 'reason': '검토 후 구성이 변경돼 초기 안내 준비를 취소했습니다.'}
        existing = _load(paths, host)
        if existing and existing['state'] == 'pending':
            valid = (current.to_dict() == existing['expected_config']
                     or current.to_dict() == existing.get('candidate'))
            if not valid:
                return _finish(paths, host, existing, 'cancelled', '구성이 변경돼 초기 안내 준비를 취소했습니다.')
            if existing['phase'] == 'installing':
                existing['phase'] = 'waiting'
                _save(paths, existing, host)
            if existing['phase'] == 'waiting' and existing['release'] != str(release):
                existing['release'] = str(release)
                _save(paths, existing, host)
            _schedule(paths, host, release)
            return _run(paths, host, existing)
        value = {'schema_version': 1, 'request_id': uuid.uuid4().hex, 'run_id': run_id,
                 'state': 'pending', 'phase': 'waiting', 'release': str(release),
                 'expected_config': config.to_dict(), 'candidate': None,
                 'reason': '초기 안내 화면을 준비합니다.'}
        _save(paths, value, host)
        _fence(paths, value, pending=True, host=host)
        _schedule(paths, host, release)
        return _run(paths, host, value)


def _inactive_existing(config, paths, host):
    from . import display
    store = RuntimeStore(paths, host)
    for screen in config.screen_targets():
        state = store.load(screen.id)
        if state and (store.active(state) or store.is_stopped(state)):
            return {'state': 'blocked', 'reason': '기존 DID의 작업 또는 수동 중지 상태를 유지합니다.'}
        # This intentionally does not inspect graphical manager variables:
        # a previously disabled desktop entry may not have imported them yet.
        observed = display._user_call(config.for_screen(screen.id), host, paths, 'startup-status')
        service = observed.get('service') or {}
        legacy = observed.get('legacy_service') or {}
        if (observed.get('state') != 'ok' or observed.get('exit_code') != 0
                or service.get('QueryError') or legacy.get('QueryError')
                or service.get('ActiveState') is None or legacy.get('ActiveState') is None):
            return {'state': 'pending', 'reason': '사용자 Chrome 서비스의 실행 상태 조회를 기다립니다.'}
        if legacy.get('ActiveState') != 'inactive':
            return {'state': 'blocked', 'reason': '기존 did-chrome 실행을 유지합니다. 초기 안내로 자동 전환하지 않습니다.'}
        if service.get('ActiveState') != 'inactive':
            return {'state': 'blocked', 'reason': '기존 Chrome의 실행·실패 상태를 유지합니다. 직접 확인한 뒤 초기 준비를 다시 요청하세요.'}
    return None


def _existing_result(paths, host, value, issue):
    if issue['state'] == 'pending':
        return _wait(paths, host, value, issue['reason'])
    return _finish(paths, host, value, 'blocked', issue['reason'])


def _select_blank(store, target, release, config):
    state = store.load(target)
    if state is None:
        return store.bootstrap(target, release, config)
    if store.active(state) or store.is_stopped(state):
        raise RuntimeError('기존 DID의 작업 또는 수동 중지 상태를 유지합니다.')
    committed = Config.from_dict(store._validate_selection(state['committed']))
    desired = Config.from_dict(store._validate_selection(state['desired']))
    if any(screen.url for model in (committed, desired) for screen in model.screen_targets()):
        raise RuntimeError('기존 사이트 선택은 초기 안내 준비로 변경하지 않습니다.')
    candidate = store.selection(release, config)
    if state['committed'] != candidate:
        state.update(committed=candidate, desired=candidate, generation=state['generation'] + 1,
                     operation=None, last_result={'kind': 'initialization', 'state': 'selected_unverified',
                                                 'reason': '초기 안내 배정 준비·실제 표시는 확인 대기'})
        store._write(store.state_path(target), state)
    return state


def _run(paths, host, value):
    from .screen_swap import guard
    guard(paths=paths, host=host)
    store = RuntimeStore(paths, host)
    release, _ = store.release(value['release'])
    current = Config.from_dict(store._read(paths.config))
    phase = value['phase']
    if phase not in PENDING_PHASES:
        raise ValueError('초기 화면 준비 단계가 올바르지 않습니다.')
    if current.to_dict() not in (value['expected_config'], value.get('candidate')):
        return _finish(paths, host, value, 'cancelled', '구성이 변경돼 초기 안내 준비를 취소했습니다.')
    if phase == 'installing':
        return _wait(paths, host, value, '간편 설정의 Chrome 등록 완료를 기다립니다.',
                     '간편 설정 작업의 진행 상태를 확인하세요.')
    if phase == 'waiting':
        plan = preview(current, paths=paths, host=host)
        if plan['state'] == 'blocked':
            return _finish(paths, host, value, 'blocked', plan['reason'])
        if plan['state'] != 'ready':
            _show_preview(paths, host, value)
            return _wait(paths, host, value, plan['reason'], plan.get('next_action'))
        issue = _inactive_existing(current, paths, host)
        if issue:
            return _existing_result(paths, host, value, issue)
        value.update(candidate=plan['config'], targets=plan['targets'], phase='prepared')
        _save(paths, value, host)
        phase = 'prepared'
    candidate = Config.from_dict(value['candidate'])
    if phase == 'prepared':
        _fence(paths, value, pending=True, host=host)
        if current.to_dict() == value['expected_config']:
            issue = _inactive_existing(current, paths, host)
            if issue:
                return _existing_result(paths, host, value, issue)
            store._safe(paths.config)
            atomic_json(paths.config, candidate.to_dict(), 0o644)
        value['phase'] = 'configured'
        _save(paths, value, host)
        phase = 'configured'
    if phase == 'configured':
        # The public fence still blocks login launches while config and target
        # selections/units are being prepared, including an interrupted retry.
        issue = _inactive_existing(candidate, paths, host)
        if issue:
            return _existing_result(paths, host, value, issue)
        for screen in candidate.screen_targets():
            _select_blank(store, screen.id, release, candidate)
        from .display import apply_display
        results = apply_display(candidate, host=host, paths=paths)
        pending = [result for result in results if result.state != 'ok'
                   and not result.id.startswith('chrome.url') and result.state != 'not_applicable']
        if pending:
            _show_preview(paths, host, value)
            priority = {'error': 0, 'blocked': 1, 'unknown': 2, 'change_required': 3, 'pending': 4}
            issue = min(pending, key=lambda result: priority[result.state]).to_dict()
            detail = issue.get('detail') or issue.get('actual')
            if not isinstance(detail, str) or not detail.strip():
                detail = 'Chrome 등록과 GNOME 확장 준비를 확인하세요.'
            clean = lambda text: ''.join(character if ord(character) >= 32 and ord(character) != 127 else ' '
                                         for character in text).strip()[:500]
            detail = clean(detail)
            action = clean(issue.get('next_action') or '등록 상태와 설치 로그를 확인한 뒤 다시 시도하세요.')
            value['registration_issue'] = {'id': issue['id'], 'state': issue['state'],
                                           'reason': detail, 'next_action': action}
            label = {'blocked': '조치 필요', 'error': '실패', 'unknown': '확인 불가',
                     'change_required': '등록 필요', 'pending': '적용 대기'}[issue['state']]
            # Keep the configured-stage fence: a registration collision must
            # never release a partially prepared set of target selections.
            return _wait(paths, host, value, 'Chrome 등록 · ' + label + ' · ' + detail, action)
        value.pop('registration_issue', None)
        value['phase'] = 'starting'
        _save(paths, value, host)
    # Repeat this after an interruption between phase persistence and release.
    _fence(paths, value, pending=False, host=host)
    from . import display
    starts = []
    for screen in candidate.screen_targets():
        state = store.load(screen.id)
        if state is not None and store.is_stopped(state):
            starts.append({'id': screen.id, 'state': 'stopped', 'reason': '수동 중지 상태 유지'})
            continue
        result = display._user_call(candidate.for_screen(screen.id), host, paths, 'ensure-start')
        starts.append({'id': screen.id, 'state': result.get('state', 'unknown'),
                       'exit_code': result.get('exit_code', 1)})
        if result.get('state') in {'failed', 'error', 'blocked', 'unknown'} or result.get('exit_code', 1) == 1:
            value['start_results'] = starts
            return _finish(paths, host, value, 'blocked', '실패 상태를 재시작하지 않았습니다. DID 로그와 상태를 확인하세요.')
        if result.get('exit_code') == 2:
            value['start_results'] = starts
            return _wait(paths, host, value, '사용자 화면 환경과 안내 화면 시작을 기다립니다.')
    value['start_results'] = starts
    if value.get('preview_active'):
        ready = False
        for screen in candidate.screen_targets():
            observed = display._user_call(candidate.for_screen(screen.id), host, paths, 'status')
            health = observed.get('health') or {}
            if observed.get('process_verified') is True and health.get('state') == 'waiting_configuration' and health.get('local_page_ready') is True:
                ready = True
                break
        if not ready:
            return _wait(paths, host, value, '설정 중 안내를 유지하며 모니터별 안내 문서의 실제 로드를 기다립니다.',
                         '화면 계정의 Chrome 상태·로그와 모니터 배치를 확인하세요.')
    value['display_verified'] = False
    return _finish(paths, host, value, 'completed', '초기 안내 화면 시작을 요청했습니다. 실제 모니터 표시를 확인하세요.')


def run_pending(*, paths=None, host=None, locked=False):
    paths, host = paths or Paths(), host or Host()
    if paths.root != Path('/') and type(host) is Host:
        return {'state': 'blocked', 'reason': '대체 경로에서 실제 systemd 변경을 허용하지 않습니다.'}
    try:
        with nullcontext() if locked else change_lock(paths):
            value = _load(paths, host)
            if value is None or value['state'] != 'pending':
                return _summary(value)
            return _run(paths, host, value)
    except (OSError, ValueError, RuntimeError):
        return {'state': 'pending', 'reason': '초기 화면 준비 상태를 확인하지 못했습니다.',
                'next_action': '설치 작업과 초기 화면 준비 로그를 확인하세요.'}
