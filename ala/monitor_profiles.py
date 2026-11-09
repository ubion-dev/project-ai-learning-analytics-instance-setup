"""Confirmed monitor profiles, reapplied once after a matching GNOME login.

Mutter's native persistent dialog is not bypassed. ALA stores its own profile and
uses the same validated temporary-apply transaction after a new session appears.
"""
import hashlib
import json
from pathlib import Path
import re

from .config import Config
from .jobs import boot_id, change_lock, protected_directory
from .monitor_layout import build_plan
from .runtime import RuntimeStore
from .system import Host, Paths, atomic_json, atomic_write

MARKER = '# ala managed monitor profile v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class Profiles:
    def __init__(self, paths=None, host=None):
        self.paths, self.host = paths or Paths(), host or Host()
        self.runtime = RuntimeStore(self.paths, self.host)
        self.base = self.paths.at('/var/lib/ala/monitor-layout')
        self.path = self.base / 'profile.json'
        self.attempt_path = self.base / 'restore-status.json'

    def read(self):
        self.runtime._safe(self.path)
        if not self.path.exists():
            return None
        value = self.runtime._read(self.path)
        return self.validate(value)

    @staticmethod
    def validate(value):
        if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
                or value['schema_version'] != 1 or type(value.get('enabled')) is not bool
                or type(value.get('layout_mode')) is not int or value['layout_mode'] not in (1, 2)
                or not isinstance(value.get('user'), str) or not value['user']
                or not isinstance(value.get('all_outputs'), dict) or not 1 <= len(value['all_outputs']) <= 64
                or any(not isinstance(k, str) or not isinstance(v, str) or not re.fullmatch('[a-f0-9]{64}', v)
                       for k, v in value['all_outputs'].items())
                or not isinstance(value.get('outputs'), list) or not 1 <= len(value['outputs']) <= 64):
            raise ValueError('저장한 모니터 배치의 형식을 확인하지 못했습니다.')
        seen = set()
        for output in value['outputs']:
            if (not isinstance(output, dict) or not isinstance(output.get('id'), str)
                    or output['id'] in seen or output['id'] not in value['all_outputs']
                    or not isinstance(output.get('mode_id'), str) or not isinstance(output.get('mode'), dict)
                    or any(type(output.get(k)) is not int or not 0 <= output[k] <= 65536 for k in ('x', 'y'))
                    or type(output.get('scale')) not in (int, float) or not 0 < output['scale'] <= 16
                    or type(output.get('transform')) is not int or not 0 <= output['transform'] <= 7
                    or type(output.get('primary')) is not bool):
                raise ValueError('저장한 모니터 배치의 출력 정보를 확인하지 못했습니다.')
            mode = output['mode']
            if (any(type(mode.get(k)) is not int or not 0 < mode[k] <= 65536 for k in ('width', 'height'))
                    or type(mode.get('refresh_hz')) not in (int, float) or not 0 < mode['refresh_hz'] <= 10000
                    or mode.get('refresh_mode') not in {'fixed', 'variable'} or type(mode.get('interlaced')) is not bool):
                raise ValueError('저장한 모니터 모드를 확인하지 못했습니다.')
            seen.add(output['id'])
        if sum(o['primary'] for o in value['outputs']) != 1:
            raise ValueError('저장한 주 모니터를 확인하지 못했습니다.')
        return value

    def write(self, value):
        self.validate(value)
        self.runtime._safe(self.path)
        protected_directory(self.base)
        atomic_json(self.path, value)
        self.read()

    def attempt(self):
        self.runtime._safe(self.attempt_path)
        if not self.attempt_path.exists():
            return None
        value = self.runtime._read(self.attempt_path)
        if (not isinstance(value, dict) or not isinstance(value.get('key'), str)
                or value.get('state') not in {'applied', 'queued', 'blocked'}):
            raise ValueError('모니터 배치 복원 기록을 확인하지 못했습니다.')
        return value

    def note(self, key, state, reason, operation_id=None):
        self.runtime._safe(self.attempt_path)
        protected_directory(self.base)
        atomic_json(self.attempt_path, {'key': key, 'state': state, 'reason': reason, 'operation_id': operation_id})


def candidate(settings, plan, user):
    physical = {o['id']: o for o in settings['outputs']}
    outputs = []
    for output in plan['outputs']:
        mode = next(m for m in physical[output['id']]['modes'] if m['id'] == output['mode_id'])
        outputs.append({k: output[k] for k in ('id', 'mode_id', 'x', 'y', 'scale', 'transform', 'primary')} | {
            'mode': {k: mode[k] for k in ('width', 'height', 'refresh_hz', 'refresh_mode', 'interlaced')}})
    return {'schema_version': 1, 'enabled': True, 'user': user, 'layout_mode': settings['layout_mode'],
            'all_outputs': {o['id']: o['hardware_key'] for o in settings['outputs']}, 'outputs': outputs}


def request_for(settings, profile, user):
    if not profile['enabled'] or profile['user'] != user:
        raise ValueError('저장한 모니터 배치의 계정·활성화를 확인하세요.')
    if settings['layout_mode'] != profile['layout_mode']:
        raise ValueError('GNOME 좌표 방식이 달라 저장 배치를 자동 적용하지 않습니다.')
    physical = {o['id']: o for o in settings['outputs']}
    if {k: o['hardware_key'] for k, o in physical.items()} != profile['all_outputs']:
        raise ValueError('연결된 모니터가 저장한 구성과 다릅니다. 배치를 다시 확인하세요.')
    active = {o['connectors'][0]: o for o in settings['logical_outputs'] if len(o['connectors']) == 1}
    if set(active) != {o['id'] for o in profile['outputs']}:
        raise ValueError('활성 모니터 구성이 달라 저장 배치를 자동 적용하지 않습니다.')
    entries = []
    for output in profile['outputs']:
        old = active[output['id']]
        if old['primary'] != output['primary'] or old['transform'] != output['transform']:
            raise ValueError('주 모니터·회전이 달라 자동 적용하지 않습니다. 배치를 다시 확인하세요.')
        matches = [m for m in physical[output['id']]['modes']
                   if all(m[k] == output['mode'][k] for k in ('width', 'height', 'refresh_mode', 'interlaced'))
                   and abs(m['refresh_hz'] - output['mode']['refresh_hz']) < 0.001]
        exact = [m for m in matches if m['id'] == output['mode_id']]
        if len(exact or matches) != 1:
            raise ValueError('저장한 모드·주사율을 하나로 확인하지 못했습니다. 다시 선택하세요.')
        mode = (exact or matches)[0]
        entries.append({k: output[k] for k in ('id', 'x', 'y', 'scale')} | {'mode_id': mode['id']})
    request = {'schema_version': 1, 'expected_revision': settings['revision'], 'outputs': entries}
    build_plan(settings, request)
    return request


def register(profiles):
    # current is a root-owned dispatcher. Each actual transaction pins its release.
    command = '/opt/ala/current/.venv/bin/python -I /opt/ala/current/entry.py _layout-restore'
    units = {'ala-monitor-profile.service': f'''{MARKER}
[Unit]
Description=Restore a confirmed ALA monitor profile after graphical login
[Service]
Type=oneshot
ExecStart={command}
UMask=0077
TimeoutStartSec=120
''', 'ala-monitor-profile.timer': f'''{MARKER}
[Unit]
Description=Check for a new ALA graphical session
[Timer]
OnBootSec=15
OnUnitInactiveSec=15
Unit=ala-monitor-profile.service
[Install]
WantedBy=timers.target
'''}
    for name, content in units.items():
        path = profiles.paths.at('/etc/systemd/system/' + name)
        profiles.runtime._safe(path)
        if path.exists() and (not path.is_file() or MARKER not in path.read_text()):
            raise RuntimeError('관리되지 않은 모니터 복원 서비스를 덮어쓰지 않습니다.')
        atomic_write(path, content)
    for command in (['systemctl', 'daemon-reload'], ['systemctl', 'enable', '--now', 'ala-monitor-profile.timer']):
        if profiles.host.run(command).returncode:
            raise RuntimeError('모니터 배치 로그인 복원 등록을 확인하지 못했습니다.')


def save_confirmed(store, value):
    profiles = Profiles(store.paths, store.host)
    if profiles.read() != value['backup'].get('profile'):
        raise RuntimeError('배치 검토 후 저장 프로필이 변경됐습니다.')
    proposed = candidate(value['backup']['settings'], value['plan'], value['backup']['config']['user'])
    register(profiles)
    profiles.write(proposed)


def rollback_profile(store, value):
    if not value.get('persistent'):
        return
    profiles = Profiles(store.paths, store.host)
    before = value['backup'].get('profile')
    now = profiles.read()
    proposed = candidate(value['backup']['settings'], value['plan'], value['backup']['config']['user'])
    if now == before:
        return
    if now != proposed:
        raise RuntimeError('외부에서 변경한 저장 프로필을 덮어쓰지 않습니다.')
    if before is None:
        profiles.path.unlink()
        import os
        fd = os.open(profiles.base, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    else:
        profiles.write(before)


def status(*, paths=None, host=None):
    profiles = Profiles(paths, host)
    profile = profiles.read()
    return {'saved': bool(profile), 'enabled': bool(profile and profile['enabled']), 'restore': profiles.attempt()}


def disable(*, paths=None, host=None):
    profiles = Profiles(paths, host)
    with change_lock(profiles.paths):
        profile = profiles.read()
        if profile:
            profiles.write(profile | {'enabled': False})
    return status(paths=profiles.paths, host=profiles.host)


def restore(*, release=None, paths=None, host=None, retry=False):
    from . import monitor_change as change
    profiles = Profiles(paths, host)
    if profiles.paths.root != Path('/') and type(profiles.host) is Host:
        raise RuntimeError('대체 경로에서 실제 모니터 복원을 허용하지 않습니다.')
    pending = change.Store(profiles.paths, profiles.host).load()
    if pending and pending['state'] not in change.TERMINAL:
        response = profiles.host.run(['systemctl', 'show', pending['unit'], '--property=ActiveState', '--property=LoadState'], timeout=5)
        state = dict(line.split('=', 1) for line in response.stdout.splitlines() if '=' in line)
        stopped = state.get('LoadState') == 'not-found' or (response.returncode == 0 and state.get('ActiveState') in {'inactive', 'failed'})
        if not stopped:
            return {'state': 'pending', 'reason': '배치 시험 실행·복구 완료를 기다립니다.'}
        recovered = change.recover(pending['operation_id'], paths=profiles.paths, host=profiles.host)
        if recovered['state'] not in change.TERMINAL:
            return recovered
    profile = profiles.read()
    if not profile or not profile['enabled']:
        return {'state': 'disabled'}
    try:
        config = Config.from_dict(profiles.runtime._read(profiles.paths.config))
        settings, session = change._observe(change.Store(profiles.paths, profiles.host), config)
    except (OSError, ValueError, RuntimeError):
        return {'state': 'pending', 'reason': '활성·잠금 해제 GNOME 세션과 모드 정보를 기다립니다.'}
    key = digest([boot_id(profiles.paths), session, digest(profile)])
    attempt = profiles.attempt()
    if attempt and attempt['key'] == key and not retry:
        return attempt
    with change_lock(profiles.paths):
        if profiles.read() != profile:
            raise RuntimeError('저장 프로필이 변경됐습니다. 다시 조회하세요.')
        try:
            request = request_for(settings, profile, config.user)
            plan = build_plan(settings, request)
        except (ValueError, KeyError):
            profiles.note(key, 'blocked', '계정·연결·모드·배율을 다시 확인하세요. 저장 배치를 자동 적용하지 않았습니다.')
            return profiles.attempt()
        if change._signature(settings) == change._planned(plan):
            profiles.note(key, 'applied', '현재 배치가 저장 프로필과 일치합니다.')
            return profiles.attempt()
        profiles.note(key, 'queued', '새 GNOME 세션에 저장 배치를 적용합니다.')
    try:
        result = change.submit(request, release=release, paths=profiles.paths, host=profiles.host, auto_profile=digest(profile))
    except (OSError, ValueError, RuntimeError):
        profiles.note(key, 'blocked', '저장 배치 적용을 접수하지 못했습니다. 모니터 설정에서 다시 확인하세요.')
        return profiles.attempt()
    with change_lock(profiles.paths, blocking=True, monitor_operation_id=result.get('operation_id')):
        latest = profiles.attempt()
        if latest and latest['key'] == key and latest['state'] == 'queued':
            profiles.note(key, 'queued', '배치 적용 결과는 현재 작업에서 확인하세요.', result.get('operation_id'))
    return result
