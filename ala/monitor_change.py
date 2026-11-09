"""Detached, session-only monitor trials with durable rollback ownership.

Persistent GNOME settings are deliberately not written. The systemd worker holds
the common mutation lock until confirmation or rollback. ExecStopPost attempts
rollback after a crash; a damaged or unresolved journal blocks later changes.
"""
from copy import deepcopy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

from .config import Config
from .jobs import boot_id, change_lock, protected_directory, trusted_release
from .monitor_layout import build_plan
from .runtime import RuntimeStore
from .system import Host, Paths, atomic_json

TERMINAL = {'confirmed', 'rolled_back', 'not_applied'}
STATES = TERMINAL | {'queued', 'applying', 'awaiting_confirmation', 'rolling_back', 'recovery_required'}
CONFIRM_SECONDS = 30
ID = re.compile(r'[0-9a-f]{32}\Z')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _signature(settings):
    outputs = {o['id']: o for o in settings['outputs']}
    return sorted([{'id': logical['connectors'][0], 'mode_id': outputs[logical['connectors'][0]]['current_mode_id'],
                    **{key: logical[key] for key in ('x', 'y', 'scale', 'transform', 'primary')}}
                   for logical in settings['logical_outputs']], key=lambda item: item['id'])


def _planned(plan):
    return sorted([{key: output[key] for key in ('id', 'mode_id', 'x', 'y', 'scale', 'transform', 'primary')}
                   for output in plan['outputs']], key=lambda item: item['id'])


class Store:
    def __init__(self, paths=None, host=None):
        self.paths, self.host = paths or Paths(), host or Host()
        self.runtime = RuntimeStore(self.paths, self.host)
        self.base = self.paths.at('/var/lib/ala/monitor-layout')
        self.current = self.base / 'current.json'

    def load(self):
        self.runtime._safe(self.current)
        if not self.current.exists():
            return None
        value = self.runtime._read(self.current)
        if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
                or value['schema_version'] != 1 or not isinstance(value.get('operation_id'), str)
                or not ID.fullmatch(value['operation_id']) or value.get('state') not in STATES
                or type(value.get('persistent')) is not bool or not isinstance(value.get('backup'), dict)
                or _digest(value['backup']) != value.get('backup_sha256')
                or not isinstance(value.get('boot_id'), str)
                or not isinstance(value.get('session'), str)
                or not isinstance(value.get('release'), str)
                or value.get('unit') != 'ala-monitor-layout-' + value['operation_id'] + '.service'):
            raise ValueError('모니터 배치 복구 기록을 확인하지 못했습니다.')
        Config.from_dict(value['backup']['config'])
        # Reconstruct the reviewed plan rather than trusting a stored rectangle.
        reviewed = build_plan(value['backup']['settings'], value['request'])
        plan = value.get('plan')
        if (not isinstance(plan, dict) or _planned(reviewed) != _planned(plan)
                or reviewed['layout_mode'] != plan.get('layout_mode')
                or reviewed['expected_revision'] != plan.get('expected_revision')):
            raise ValueError('모니터 배치 계획 기록이 일치하지 않습니다.')
        deadline = value.get('deadline_monotonic')
        if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= 0):
            raise ValueError('모니터 배치 확인 기한이 올바르지 않습니다.')
        if value['state'] == 'awaiting_confirmation' and (deadline is None
                or not isinstance(value.get('applied_revision'), str)
                or not re.fullmatch('[a-f0-9]{64}', value['applied_revision'])):
            raise ValueError('모니터 배치 확인 상태가 올바르지 않습니다.')
        return value

    def save(self, value):
        self.runtime._safe(self.current)
        protected_directory(self.base)
        os.chmod(self.base, 0o700)
        if len(json.dumps(value, allow_nan=False).encode()) > 1048576:
            raise ValueError('모니터 배치 기록이 너무 큽니다.')
        atomic_json(self.current, value)

    def decision_path(self, operation_id):
        if not isinstance(operation_id, str) or not ID.fullmatch(operation_id):
            raise ValueError('모니터 배치 작업 ID를 확인하세요.')
        return self.base / (operation_id + '-decision.json')

    def decision(self, operation_id):
        path = self.decision_path(operation_id)
        self.runtime._safe(path)
        if not path.exists():
            return None
        value = self.runtime._read(path)
        if (not isinstance(value, dict) or set(value) != {'operation_id', 'decision'}
                or value['operation_id'] != operation_id or value['decision'] not in {'confirm', 'revert'}):
            raise ValueError('모니터 배치 확인 응답을 읽지 못했습니다.')
        return value['decision']


def guard(paths, operation_id=None):
    value = Store(paths).load()
    if value and value['state'] not in TERMINAL and value['operation_id'] != operation_id:
        raise RuntimeError('모니터 배치 확인 또는 복구가 남아 있습니다. ala display trial-status로 확인하세요.')


def _public(value):
    if value is None:
        return {'state': 'none', 'persistent': False}
    return {key: value.get(key) for key in ('operation_id', 'state', 'persistent', 'reason', 'deadline_monotonic')} | {
        'confirmation_seconds': CONFIRM_SECONDS,
        'next_action': ('확정한 배치를 저장하고 다음 GNOME 로그인에서 같은 모니터 구성에 다시 적용합니다.'
                        if value['persistent'] else '현재 세션의 시험입니다. 다음 로그인에는 저장한 배치를 사용합니다.')}


def status(*, paths=None, host=None):
    return _public(Store(paths, host).load())


def _observe(store, config):
    from .screens import _inventory, inspect_session
    if inspect_session(config, store.host) != 'ok':
        raise RuntimeError('단일 활성·잠금 해제 GNOME 화면 세션이 필요합니다.')
    inventory = _inventory(config, store.host, store.paths)
    settings = inventory.get('layout_settings', {})
    session = inventory.get('layout_session')
    if (inventory.get('state') != 'ok' or settings.get('state') != 'ok'
            or 'monitor-layout-trial' not in inventory.get('capabilities', [])
            or not isinstance(session, str) or not re.fullmatch('[a-f0-9-]{36}', session)
            or 'edit_template' not in settings):
        raise RuntimeError('세션 배치 시험을 지원하는 GNOME 확장과 모드 정보를 확인하세요.')
    current_plan = build_plan(settings, settings['edit_template'])
    rectangles = {output['id']: output for output in inventory.get('logical_outputs', [])}
    if (set(rectangles) != {output['id'] for output in current_plan['outputs']}
            or any(rectangles[output['id']].get('confirmed') is not True
                   or any(rectangles[output['id']].get(key) != output[key]
                          for key in ('x', 'y', 'width', 'height', 'scale', 'transform', 'primary'))
                   for output in current_plan['outputs'])):
        raise RuntimeError('Mutter 상태와 실제 compositor 영역이 일치하지 않습니다. 다시 조회하세요.')
    return settings, session


def _backend(store, config, settings, session, plan, method):
    from .display import _user_call
    result = _user_call(config, store.host, store.paths, 'monitor-layout', request={
        'serial': settings['serial'], 'session': session, 'method': method, 'outputs': plan['outputs']})
    if result.get('state') != 'ok' or result.get('exit_code', 0):
        raise RuntimeError('GNOME 모니터 배치 요청 결과를 확인하지 못했습니다.')


def submit(request, *, release=None, paths=None, host=None, persistent=False, auto_profile=None):
    store = Store(paths, host)
    if type(persistent) is not bool or (auto_profile is not None and
            (not isinstance(auto_profile, str) or not re.fullmatch('[a-f0-9]{64}', auto_profile))):
        raise ValueError('배치 저장·자동 복원 요청을 확인하세요.')
    if store.paths.root != Path('/') and type(store.host) is Host:
        raise RuntimeError('대체 경로에서 실제 장비 변경을 허용하지 않습니다.')
    release = trusted_release(Path(release or Path(__file__).resolve().parents[1]))
    with change_lock(store.paths):
        from .display_reset import guard as reset_guard
        from .screen_swap import guard as swap_guard
        reset_guard(paths=store.paths, host=store.host)
        swap_guard(paths=store.paths, host=store.host)
        if any(store.runtime.active(v) for v in store.runtime.selections()):
            raise RuntimeError('화면 코드·프론트 작업 완료 후 모니터 배치를 변경하세요.')
        config = Config.from_dict(store.runtime._read(store.paths.config))
        settings, session = _observe(store, config)
        plan = build_plan(settings, request)
        if _signature(settings) == _planned(plan) and not persistent:
            return {'state': 'unchanged', 'persistent': False}
        # Mutter verify must succeed before any journal or change is created.
        _backend(store, config, settings, session, plan, 'verify')
        fresh, fresh_session = _observe(store, config)
        if fresh['revision'] != settings['revision'] or fresh_session != session:
            raise RuntimeError('검토 후 모니터 상태가 변경됐습니다. 다시 조회하세요.')
        operation_id = uuid.uuid4().hex
        from .monitor_profiles import Profiles, digest
        profile = Profiles(store.paths, store.host).read()
        if auto_profile is not None and (not profile or digest(profile) != auto_profile):
            raise RuntimeError('자동 복원 프로필이 변경됐습니다.')
        backup = {'config': config.to_dict(), 'settings': settings, 'profile': profile}
        value = {'schema_version': 1, 'operation_id': operation_id, 'state': 'queued', 'persistent': persistent,
                 'boot_id': boot_id(store.paths), 'session': session, 'release': str(release),
                 'unit': 'ala-monitor-layout-' + operation_id + '.service', 'backup': backup,
                 'backup_sha256': _digest(backup), 'request': deepcopy(request), 'plan': plan,
                 'deadline_monotonic': None, 'applied_revision': None, 'reason': '세션 배치 시험을 접수했습니다.'}
        value['auto_profile'] = auto_profile
        store.save(value)
        store.load()
        # Recovery is a different process, triggered even if the worker is killed.
        post = ' '.join([str(release / '.venv/bin/python'), '-I', str(release / 'entry.py'),
                         '_layout-recover', operation_id])
        if any(c in post for c in ('\n', '\r', '\t', '"', "'", '\\', '$', '%')) or ' ' in str(release):
            value.update(state='not_applied', reason='고정 실행 경로 형식을 확인하지 못했습니다.')
            store.save(value)
            raise ValueError(value['reason'])
        response = store.host.run(['systemd-run', '--unit=' + value['unit'], '--collect', '--expand-environment=no',
                                   '--property=Type=exec', '--property=UMask=0077', '--property=TimeoutStartSec=180',
                                   '--property=RuntimeMaxSec=120', '--property=TimeoutStopSec=90',
                                   '--property=ExecStopPost=' + post,
                                   str(release / '.venv/bin/python'), '-I', str(release / 'entry.py'),
                                   '_layout-worker', operation_id], timeout=30)
        if response.returncode:
            # Delivery may have succeeded. Leave the fence in place until recovery.
            value.update(state='recovery_required', reason='실행기 접수 여부를 확인하지 못했습니다. 복구 상태를 확인하세요.')
            store.save(value)
        return _public(value)


def decide(operation_id, decision, *, paths=None, host=None):
    store = Store(paths, host)
    path = store.decision_path(operation_id)
    if decision not in {'confirm', 'revert'}:
        raise ValueError('허용되지 않은 모니터 배치 응답입니다.')
    # Do not acquire the common lock: its owner is waiting for this decision.
    store.runtime._safe(path)
    lock = store.base / 'decision.lock'
    store.runtime._safe(lock)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != store.runtime.owner or info.st_mode & 0o022:
            raise PermissionError('모니터 배치 응답 잠금 권한을 확인하세요.')
        fcntl.flock(fd, fcntl.LOCK_EX)
        value = store.load()
        if (not value or value['operation_id'] != operation_id or value['state'] != 'awaiting_confirmation'
                or value['boot_id'] != boot_id(store.paths) or value['deadline_monotonic'] is None
                or time.monotonic() >= value['deadline_monotonic']):
            raise RuntimeError('확인 가능한 배치 시험이 없거나 시간이 지났습니다.')
        old = store.decision(operation_id)
        if old is not None and old != decision:
            raise RuntimeError('이미 다른 확인 응답을 접수했습니다.')
        if old is None:
            atomic_json(path, {'operation_id': operation_id, 'decision': decision})
        return {'operation_id': operation_id, 'state': 'decision_recorded', 'persistent': False,
                'reason': '응답을 접수했습니다. trial-status에서 최종 결과를 확인하세요.'}
    finally:
        os.close(fd)


def _same_config(store, value):
    return Config.from_dict(store.runtime._read(store.paths.config)).to_dict() == value['backup']['config']


def _rollback(store, value):
    config = Config.from_dict(value['backup']['config'])
    try:
        settings, session = _observe(store, config)
        before = _signature(value['backup']['settings'])
        if _signature(settings) == before:
            value.update(state='rolled_back', reason='현재 출력이 적용 전 배치와 일치합니다.')
        else:
            if (not _same_config(store, value) or value['boot_id'] != boot_id(store.paths)
                    or session != value['session'] or _signature(settings) != _planned(value['plan'])
                    or (value.get('applied_revision') and settings['revision'] != value['applied_revision'])):
                raise RuntimeError('세션·구성 또는 외부 배치 변경을 덮어쓰지 않습니다.')
            value.update(state='rolling_back', reason='적용 전 배치로 복원을 요청합니다.')
            store.save(value)
            request = {'schema_version': 1, 'expected_revision': settings['revision'],
                       'outputs': [{key: item[key] for key in ('id', 'mode_id', 'x', 'y', 'scale')} for item in before]}
            plan = build_plan(settings, request)
            _backend(store, config, settings, session, plan, 'verify')
            _backend(store, config, settings, session, plan, 'temporary')
            restored, restored_session = _observe(store, config)
            if restored_session != session or _signature(restored) != before:
                raise RuntimeError('복원된 실제 배치를 확인하지 못했습니다.')
            value.update(state='rolled_back', reason='적용 전 실제 모니터 배치를 다시 확인했습니다.')
    except (OSError, ValueError, RuntimeError, KeyError):
        value.update(state='recovery_required', reason='배치 복원을 확인하지 못했습니다. GNOME 화면 설정과 복구 상태를 확인하세요.')
    if value['state'] == 'rolled_back':
        try:
            from .monitor_profiles import rollback_profile
            rollback_profile(store, value)
        except (OSError, ValueError, RuntimeError, KeyError):
            value.update(state='recovery_required', reason='저장 프로필 복원을 확인하지 못했습니다.')
    store.save(value)
    if value.get('auto_profile'):
        from .monitor_profiles import Profiles
        profiles = Profiles(store.paths, store.host)
        attempt = profiles.attempt()
        if attempt:
            profiles.note(attempt['key'], 'blocked', value['reason'], value['operation_id'])
    return _public(value)


def _finish(store, value):
    from .monitor_profiles import Profiles, digest, save_confirmed
    if value.get('auto_profile'):
        if digest(Profiles(store.paths, store.host).read()) != value['auto_profile']:
            raise RuntimeError('자동 복원 중 저장 프로필이 변경됐습니다.')
    if value['persistent']:
        save_confirmed(store, value)
    value.update(state='confirmed', reason='모니터 배치를 확정하고 로그인 복원 프로필에 저장했습니다.' if value['persistent']
                 else '현재 세션의 모니터 배치를 확인했습니다.')
    store.save(value)
    if value.get('auto_profile'):
        profiles = Profiles(store.paths, store.host)
        attempt = profiles.attempt()
        if attempt:
            profiles.note(attempt['key'], 'applied', '저장한 모니터 배치의 실제 적용을 확인했습니다.', value['operation_id'])
    return _public(value)


def run_worker(operation_id, *, paths=None, host=None, sleep=time.sleep):
    store = Store(paths, host)
    store.decision_path(operation_id)
    with change_lock(store.paths, blocking=True, monitor_operation_id=operation_id):
        value = store.load()
        if not value or value['operation_id'] != operation_id:
            raise RuntimeError('모니터 배치 실행 기록이 일치하지 않습니다.')
        if value['state'] != 'queued':
            raise RuntimeError('이미 접수한 배치 시험을 중복 적용하지 않습니다.')
        config = Config.from_dict(value['backup']['config'])
        try:
            settings, session = _observe(store, config)
            if (not _same_config(store, value) or value['boot_id'] != boot_id(store.paths)
                    or session != value['session'] or settings['revision'] != value['request']['expected_revision']):
                value.update(state='not_applied', reason='검토 후 상태가 달라 적용하지 않았습니다.')
                store.save(value)
                return _public(value)
            _backend(store, config, settings, session, value['plan'], 'verify')
            value.update(state='applying', reason='현재 GNOME 세션에 배치를 적용합니다.')
            store.save(value)
            _backend(store, config, settings, session, value['plan'], 'temporary')
            applied, applied_session = _observe(store, config)
            if applied_session != session or _signature(applied) != _planned(value['plan']):
                raise RuntimeError('적용된 실제 배치를 확인하지 못했습니다.')
            value.update(state='awaiting_confirmation', applied_revision=applied['revision'],
                         deadline_monotonic=time.monotonic() + CONFIRM_SECONDS,
                         reason='화면을 확인한 뒤 30초 안에 확정하세요. 응답이 없으면 복원합니다.')
            store.save(value)
            if value.get('auto_profile'):
                return _finish(store, value)
            while time.monotonic() < value['deadline_monotonic']:
                decision = store.decision(operation_id)
                current, current_session = _observe(store, config)
                if (not _same_config(store, value) or current_session != session
                        or current['revision'] != value['applied_revision']):
                    raise RuntimeError('확인 중 모니터 상태가 달라졌습니다.')
                # Recheck after potentially slow session/DBus reads.
                if time.monotonic() >= value['deadline_monotonic'] or decision == 'revert':
                    break
                if decision == 'confirm':
                    return _finish(store, value)
                sleep(0.5)
        except (OSError, ValueError, RuntimeError, KeyError):
            pass
        return _rollback(store, value)


def recover(operation_id, *, paths=None, host=None):
    store = Store(paths, host)
    store.decision_path(operation_id)
    with change_lock(store.paths, blocking=True, monitor_operation_id=operation_id):
        value = store.load()
        if not value or value['operation_id'] != operation_id:
            raise RuntimeError('복구할 모니터 배치 기록이 일치하지 않습니다.')
        if value['state'] in TERMINAL:
            return _public(value)
        return _rollback(store, value)
