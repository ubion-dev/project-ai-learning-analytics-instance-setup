"""Local scheduling only; enrollment and physical acceptance precede enabling."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import os
import re
import stat
import time
import uuid
from zoneinfo import ZoneInfo

from . import __version__
from .jobs import change_lock
from .runtime import RuntimeStore, apply_target
from .system import Paths, Host, atomic_json, atomic_write

MARKER = '# ala managed local update schedule'
TIMERS = ('ala-cli-update.timer', 'ala-runtime-nightly.timer')


def unit_files():
    result = {}
    for name, command, description in (
        ('ala-cli-update', '_auto-update check', 'ala signed stable CLI update'),
        ('ala-runtime-nightly', '_auto-update nightly', 'ala sequential runtime maintenance'),
        ('ala-frontend', '_frontend-worker', 'ala sequential frontend deployment observation'),
    ):
        result[name + '.service'] = f'''{MARKER}
[Unit]
Description={description}
[Service]
Type=oneshot
ExecStart=/usr/local/bin/ala {command}
UMask=0077
TimeoutStartSec=infinity
'''
    result['ala-cli-update.timer'] = f'''{MARKER}
[Unit]
Description=ala stable CLI update every six hours
[Timer]
OnCalendar=*-*-* 00,06,12,18:00:00
RandomizedDelaySec=30min
Persistent=true
[Install]
WantedBy=timers.target
'''
    result['ala-runtime-nightly.timer'] = f'''{MARKER}
[Unit]
Description=ala nightly runtime maintenance
[Timer]
OnCalendar=*-*-* 03:00:00 Asia/Seoul
Persistent=false
[Install]
WantedBy=timers.target
'''
    result['ala-frontend.timer'] = f'''{MARKER}
[Unit]
Description=ala frontend deployment observation
[Timer]
OnBootSec=60s
OnUnitInactiveSec=60s
RandomizedDelaySec=10s
Persistent=false
[Install]
WantedBy=timers.target
'''
    return result


def _state_path(paths):
    return paths.at('/var/lib/ala/automatic/state.json')


def _load(paths):
    path = _state_path(paths)
    if not path.exists() and not path.is_symlink():
        return {'schema_version': 1, 'enabled': False, 'accepted': False, 'failures': 0,
                'next_attempt_at': 0, 'last_result': None}
    owner = 0 if paths.root == Path('/') else os.geteuid()
    for item in (path, *path.parents):
        if item.is_symlink() or (item.exists() and (item.stat().st_uid != owner or item.stat().st_mode & 0o022)):
            raise PermissionError('자동 갱신 상태의 소유자·권한을 확인하세요.')
        if item == paths.root:
            break
    if path.stat().st_size > 65536 or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('자동 갱신 상태 파일이 올바르지 않습니다.')
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or value.get('schema_version') != 1
            or type(value.get('enabled')) is not bool or type(value.get('accepted')) is not bool
            or type(value.get('failures')) is not int or value['failures'] < 0
            or type(value.get('next_attempt_at')) not in (int, float)):
        raise ValueError('자동 갱신 상태 형식이 올바르지 않습니다.')
    return value


def _save(paths, value):
    # Recheck even for a first write, before creating any directory.
    _load(paths)
    path = _state_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json(path, value, 0o600)


def status(*, paths=None, host=None):
    paths, host = paths or Paths(), host or Host()
    value = _load(paths)
    from .signature import ensure_verifier
    try:
        ensure_verifier(paths)
        verifier = 'ready'
    except (OSError, ValueError):
        verifier = 'missing_or_invalid'
    operation = value.get('bootstrap')
    if isinstance(operation, dict) and operation.get('state') in {'queued', 'running'}:
        operation = dict(operation)
        if operation.get('boot_id') != RuntimeStore(paths, host).boot_id():
            operation.update(state='interrupted', reason='부팅이 바뀌어 최초 준비가 중단됐습니다.')
        else:
            response = host.run(['systemctl', 'show', 'ala-auto-bootstrap.service', '--property=ActiveState', '--property=LoadState'])
            props = dict(line.split('=', 1) for line in response.stdout.splitlines() if '=' in line)
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(operation['started_at'])).total_seconds()
            if age > 15 and (props.get('LoadState') == 'not-found' or (
                    response.returncode == 0 and props.get('ActiveState') in {'inactive', 'failed'})):
                operation.update(state='interrupted', reason='최초 준비 서비스가 완료 기록 없이 종료됐습니다.')
            elif response.returncode or not props.get('ActiveState'):
                operation.update(state='unknown', reason='최초 준비 서비스의 상태를 확인하지 못했습니다.')
    return value | {'bootstrap': operation, 'verifier': verifier, 'runtime_version': __version__,
                    'state': 'enabled' if value['enabled'] else 'bootstrap_required',
                    'exit_code': 0,
                    'next_action': '' if value['enabled'] else
                    '최초 준비 → 서명된 배포 검증 → 실제 야간 화면 수용 확인 후 자동 운영을 켜세요.'}


def register_units(*, paths=None, host=None):
    paths, host = paths or Paths(), host or Host()
    owner = 0 if paths.root == Path('/') else os.geteuid()
    if os.geteuid() != owner:
        raise PermissionError('자동 갱신 등록은 관리자 작업입니다.')
    directory = paths.at('/etc/systemd/system')
    for item in (directory, *directory.parents):
        if item.is_symlink() or (item.exists() and (item.stat().st_uid != owner or item.stat().st_mode & 0o022)):
            raise PermissionError('자동 갱신 unit 경로를 확인하세요.')
        if item == paths.root:
            break
    directory.mkdir(parents=True, exist_ok=True)
    changed = False
    for name, content in unit_files().items():
        path = directory / name
        if path.is_symlink() or (path.exists() and (not path.is_file() or MARKER not in path.read_text())):
            raise PermissionError('관리 밖 자동 갱신 unit은 변경하지 않습니다.')
        if not path.exists() or path.read_text() != content or stat.S_IMODE(path.stat().st_mode) != 0o644:
            atomic_write(path, content, 0o644)
            changed = True
    if changed:
        response = host.run(['systemctl', 'daemon-reload'])
        if response.returncode:
            raise RuntimeError('자동 갱신 unit 등록을 확인하지 못했습니다.')
    # Deliberately do not enable timers as an installer side effect.
    return {'state': 'prepared', 'changed': changed, 'enabled': False}


def bootstrap(*, paths=None, host=None, opener=None, on_phase=None):
    paths, host = paths or Paths(), host or Host()
    from .signature import bootstrap as prepare_verifier
    from .signature import verify_installed_release
    if on_phase:
        on_phase('verifier')
    verifier = prepare_verifier(paths=paths, opener=opener)
    if on_phase:
        on_phase('installed_signature')
    signed = verify_installed_release(paths=paths, host=host)
    if on_phase:
        on_phase('registering')
    with change_lock(paths):
        prepared = register_units(paths=paths, host=host)
        value = _load(paths)
        _save(paths, value)
    staged = stage_installed_candidate(paths=paths, host=host)
    return {'state': 'prepared', 'verifier': verifier, 'signed_release': signed, 'schedules': prepared,
            'runtime_staging': staged,
            'next_action': '서명된 후보와 실제 야간 화면 수용을 확인한 후 자동 운영을 켜세요.'}


def enable(*, accepted=False, paths=None, host=None):
    paths, host = paths or Paths(), host or Host()
    if accepted is not True:
        raise ValueError('실제 장비의 최초 야간 수용을 명시적으로 확인해야 합니다.')
    from .signature import ensure_verifier
    ensure_verifier(paths)
    receipt = paths.at('/var/lib/ala/trust/last-verified.json')
    owner = 0 if paths.root == Path('/') else os.geteuid()
    if (receipt.is_symlink() or not receipt.is_file() or receipt.stat().st_uid != owner
            or receipt.stat().st_mode & 0o022):
        raise ValueError('서명된 ala 배포를 실제 검증한 기록이 필요합니다.')
    verified = json.loads(receipt.read_text())
    from .update import installed_version
    if verified.get('version') != installed_version(paths) or verified.get('state') != 'verified':
        raise ValueError('현재 CLI와 서명 검증 기록이 일치하지 않습니다.')
    store = RuntimeStore(paths, host)
    if not store.selections():
        raise ValueError('화면의 확정 런타임을 먼저 등록하세요.')
    with change_lock(paths):
        register_units(paths=paths, host=host)
        value = _load(paths)
        value.update(enabled=True, accepted=True, acceptance_kind='operator_confirmed_physical_acceptance',
                     accepted_at=datetime.now(timezone.utc).isoformat())
        # Commit the gate before enabling a timer, so an immediate tick can only
        # see an explicitly accepted configuration.
        _save(paths, value)
        result = host.run(['systemctl', 'enable', '--now', *TIMERS])
        if result.returncode:
            value['enabled'] = False
            _save(paths, value)
            raise RuntimeError('자동 갱신 timer 활성화를 확인하지 못했습니다.')
    return status(paths=paths, host=host)


def disable(*, paths=None, host=None):
    paths, host = paths or Paths(), host or Host()
    with change_lock(paths):
        value = _load(paths)
        value['enabled'] = False
        _save(paths, value)
        result = host.run(['systemctl', 'disable', '--now', *TIMERS])
    if result.returncode:
        raise RuntimeError('자동 운영은 비활성으로 기록했으나 timer 해제를 확인하지 못했습니다.')
    return status(paths=paths, host=host)


def submit_bootstrap(release, *, paths=None, host=None):
    paths, host = paths or Paths(), host or Host()
    store = RuntimeStore(paths, host)
    pinned, _ = store.release(release)
    with change_lock(paths):
        previous = status(paths=paths, host=host).get('bootstrap') or {}
        if previous.get('state') in {'queued', 'running', 'unknown'}:
            raise RuntimeError('최초 준비가 진행 중이거나 상태를 확인할 수 없습니다.')
        value = _load(paths)
        run_id = uuid.uuid4().hex
        operation = {'run_id': run_id, 'state': 'queued', 'phase': 'queued',
                     'boot_id': store.boot_id(), 'started_at': datetime.now(timezone.utc).isoformat(),
                     'ended_at': None, 'release': str(pinned), 'unit': 'ala-auto-bootstrap.service'}
        value['bootstrap'] = operation
        _save(paths, value)
        result = host.run(['systemd-run', '--unit=ala-auto-bootstrap', '--collect', '--expand-environment=no',
                           '--property=Type=exec', '--property=TimeoutStartSec=infinity', '--property=UMask=0077',
                           str(pinned / '.venv/bin/python'), '-I', str(pinned / 'entry.py'),
                           '_auto-bootstrap', run_id], timeout=30)
        if result.returncode and result.returncode != 124:
            value['bootstrap'] = operation | {'state': 'failed', 'reason': '최초 준비 서비스를 시작하지 못했습니다.'}
            _save(paths, value)
            return {'state': 'failed', 'exit_code': 1, 'run_id': run_id}
    return {'state': 'queued' if not result.returncode else 'unknown', 'exit_code': 2 if not result.returncode else 1,
            'run_id': run_id, 'next_action': 'ala update --policy로 최초 준비 진행 결과를 확인하세요.'}


def run_bootstrap(run_id, *, paths=None, host=None):
    if not isinstance(run_id, str) or not re.fullmatch(r'[a-f0-9]{32}', run_id):
        raise ValueError('유효한 최초 준비 실행 ID가 필요합니다.')
    paths, host = paths or Paths(), host or Host()
    with change_lock(paths):
        value = _load(paths)
        operation = value.get('bootstrap') or {}
        if (operation.get('run_id') != run_id or operation.get('state') != 'queued'
                or operation.get('boot_id') != RuntimeStore(paths, host).boot_id()):
            raise RuntimeError('재실행할 수 없는 최초 준비 작업입니다.')
        operation.update(state='running')
        value['bootstrap'] = operation
        _save(paths, value)
    def phase(name):
        with change_lock(paths):
            state = _load(paths)
            if (state.get('bootstrap') or {}).get('run_id') != run_id:
                raise RuntimeError('최초 준비 실행이 교체됐습니다.')
            state['bootstrap']['phase'] = name
            _save(paths, state)
    try:
        bootstrap(paths=paths, host=host, on_phase=phase)
        state, code, reason = 'completed', 0, '고정 검증기·현재 설치 서명 대조·timer 등록을 완료했습니다. 자동 운영은 아직 꺼져 있습니다.'
    except (OSError, ValueError, RuntimeError):
        state, code, reason = 'failed', 1, '최초 준비의 다운로드·서명·설치 대조에 실패했습니다. 기존 CLI와 화면을 유지합니다.'
    with change_lock(paths):
        value = _load(paths)
        if (value.get('bootstrap') or {}).get('run_id') != run_id:
            return 1
        value['bootstrap'].update(state=state, reason=reason, ended_at=datetime.now(timezone.utc).isoformat())
        _save(paths, value)
    return code


def in_window(now=None):
    current = now() if callable(now) else now or datetime.now(timezone.utc)
    local = current.astimezone(ZoneInfo('Asia/Seoul'))
    return local.hour == 3 and 3600 - local.minute * 60 - local.second >= 240


def nightly(*, paths=None, host=None, now=None, apply=None, allowed=None):
    paths, host = paths or Paths(), host or Host()
    if not in_window(now):
        return {'state': 'deferred', 'results': [], 'reason': '야간 시간 또는 잔여 4분 조건을 충족하지 않습니다.'}
    store = RuntimeStore(paths, host)
    results = []
    deferred = False
    for state in store.selections():
        if not in_window(now) or (allowed is not None and not allowed()):
            deferred = True
            break
        from .signature import verified_release
        candidate = state.get('desired') or {}
        if candidate != state.get('committed') and not verified_release(candidate.get('version'), paths=paths):
            results.append({'state': 'pending', 'target_id': state['target_id'],
                            'reason': '실제 서명 검증된 런타임 후보가 필요합니다.'})
            break
        result = (apply or apply_target)(state['target_id'], paths=paths, host=host)
        results.append(result)
        if result.get('state') in {'failed', 'cancelled', 'unknown'}:
            break
    outcome = ('failed' if any(item.get('state') == 'failed' for item in results) else
               'cancelled' if any(item.get('state') == 'cancelled' for item in results) else
               'deferred' if deferred or any(item.get('state') in {'pending', 'unknown'} for item in results) else 'completed')
    return {'state': outcome, 'results': results}


def stage_installed_candidate(*, paths=None, host=None):
    """A new CLI is only a desired candidate; never touch a live target here."""
    paths, host = paths or Paths(), host or Host()
    from .signature import verified_release
    store = RuntimeStore(paths, host)
    release, version = store.release(paths.at('/opt/ala/current'))
    if not verified_release(version, paths=paths):
        return {'state': 'pending', 'reason': '현재 CLI의 실제 서명 검증 기록이 필요합니다.', 'results': []}
    results = []
    with change_lock(paths):
        for state in store.selections():
            if store.active(state):
                results.append({'target_id': state['target_id'], 'state': 'pending'})
                continue
            data = store._validate_selection(state['desired'])
            if state['desired']['release'] == str(release):
                results.append({'target_id': state['target_id'], 'state': 'kept'})
                continue
            store.stage(state['target_id'], release, data, expected_generation=state['generation'])
            results.append({'target_id': state['target_id'], 'state': 'staged'})
    return {'state': 'pending' if any(item['state'] == 'pending' for item in results) else 'staged', 'results': results}


def run_scheduled(kind, *, paths=None, host=None, now=None):
    if kind not in {'check', 'nightly'}:
        raise ValueError('허용되지 않은 자동 갱신 작업입니다.')
    paths, host = paths or Paths(), host or Host()
    value = _load(paths)
    if not value['enabled'] or not value['accepted']:
        return 0
    if kind == 'nightly':
        stage_installed_candidate(paths=paths, host=host)
        result = nightly(paths=paths, host=host, now=now,
                         allowed=lambda: _load(paths)['enabled'] and _load(paths)['accepted'])
        return 1 if result['state'] == 'failed' else 0
    if time.time() < value['next_attempt_at']:
        return 0
    try:
        from .signature import ensure_verifier
        from .update import submit
        ensure_verifier(paths)
        result = submit(Path(__file__).resolve().parents[1], paths=paths, host=host, require_signature=True)
        if result.get('exit_code') not in {0, 2}:
            raise RuntimeError('서명된 정식 CLI 갱신을 시작하지 못했습니다.')
        value.update(failures=0, next_attempt_at=0, last_result={'state': result.get('state'), 'run_id': result.get('run_id')})
    except (OSError, ValueError, RuntimeError):
        value['failures'] += 1
        value.update(next_attempt_at=time.time() + min(21600, 300 * 2 ** min(value['failures'] - 1, 7)),
                     last_result={'state': 'failed', 'reason': '자동 갱신 조건·서명·네트워크 확인에 실패했습니다. 기존 화면을 유지합니다.'})
        with change_lock(paths):
            latest = _load(paths)
            latest.update({key: value[key] for key in ('failures', 'next_attempt_at', 'last_result')})
            _save(paths, latest)
        return 1
    with change_lock(paths):
        latest = _load(paths)
        latest.update({key: value[key] for key in ('failures', 'next_attempt_at', 'last_result')})
        _save(paths, latest)
    return 0
