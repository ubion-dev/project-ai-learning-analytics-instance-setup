"""Detached, generation-fenced manual reloads for one managed display.

The root lock protects only submission/reservation/result writes. A private
user-side CDP request and its document acknowledgement run outside that lock;
an operator's stop can cancel the reservation while the request is waiting.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .config import Config
from .jobs import JobStore, change_lock, protected_directory
from .runtime import RuntimeStore, target_id
from .system import Host, Paths, atomic_json

RUN_ID = re.compile(r'[a-f0-9]{32}\Z')
STATES = {'queued', 'running', 'completed', 'failed', 'pending', 'cancelled', 'interrupted', 'unknown'}


def _now():
    return datetime.now(timezone.utc).isoformat()


class ControlStore:
    def __init__(self, paths=None, host=None):
        self.paths, self.host = paths or Paths(), host or Host()
        self.runtime = RuntimeStore(self.paths, self.host)
        self.base = self.paths.at('/var/lib/ala/screen-control')

    def path(self, run_id):
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise ValueError('유효한 화면 갱신 실행 ID가 필요합니다.')
        return self.base / 'runs' / (run_id + '.json')

    def index(self, target):
        return self.base / 'targets' / (target_id(target) + '.json')

    def _prepare(self, parent):
        self.runtime._safe(parent)
        protected_directory(parent)
        self.runtime._safe(parent)

    def _read(self, path):
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > 65536):
            raise PermissionError('화면 갱신 기록의 소유자·권한·크기를 확인하세요.')
        return self.runtime._read(path)

    def load(self, run_id):
        value = self._read(self.path(run_id))
        if (not isinstance(value, dict) or value.get('schema_version') != 1
                or value.get('run_id') != run_id or value.get('state') not in STATES):
            raise ValueError('화면 갱신 실행 기록의 형식이 올바르지 않습니다.')
        target = target_id(value.get('target_id'))
        if value.get('unit') != f'ala-screen-reload-{target}.service':
            raise ValueError('화면 갱신 서비스가 고정 대상과 다릅니다.')
        if not isinstance(value.get('release'), str) or not Path(value['release']).is_absolute():
            raise ValueError('화면 갱신 실행 경로가 올바르지 않습니다.')
        return value

    def save(self, value):
        if os.geteuid() != self.runtime.owner:
            raise PermissionError('화면 갱신 기록 변경은 관리자 작업입니다.')
        path = self.path(value['run_id'])
        self._prepare(path.parent)
        self.runtime._safe(path)
        atomic_json(path, value, 0o600)

    def latest(self, target):
        path = self.index(target)
        if not path.exists() and not path.is_symlink():
            return None
        value = self._read(path)
        if not isinstance(value, dict) or set(value) != {'run_id'}:
            raise ValueError('화면 갱신 대상 기록이 올바르지 않습니다.')
        record = self.load(value['run_id'])
        if record['target_id'] != target:
            raise ValueError('화면 갱신 대상 기록이 일치하지 않습니다.')
        return record

    def select(self, value):
        self.save(value)
        path = self.index(value['target_id'])
        self._prepare(path.parent)
        self.runtime._safe(path)
        atomic_json(path, {'run_id': value['run_id']}, 0o600)


def _public(value):
    return {key: value[key] for key in ('run_id', 'target_id', 'unit', 'state', 'exit_code',
            'reason', 'started_at', 'ended_at') if key in value}


def _unit_state(store, unit):
    result = store.host.run(['systemctl', 'show', unit,
                            '--property=ActiveState', '--property=LoadState'])
    props = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if props.get('LoadState') == 'not-found' and props.get('ActiveState') == 'inactive':
        return 'inactive'
    if result.returncode or props.get('LoadState') not in {'loaded', 'not-found'} or not props.get('ActiveState'):
        raise RuntimeError('화면 갱신 서비스의 실행 여부를 확인하지 못했습니다.')
    return props['ActiveState']


def read_status(target='default', *, paths=None, host=None):
    """Read-only status; boot changes and missing workers never resume a reload."""
    store = ControlStore(paths, host)
    try:
        value = store.latest(target_id(target))
        if value is None:
            return {'state': 'idle', 'exit_code': 0, 'reason': '화면 갱신 실행 기록이 없습니다.'}
        value = dict(value)
        if value['state'] in {'queued', 'running', 'unknown'}:
            if value.get('boot_id') != store.runtime.boot_id():
                value.update(state='interrupted', exit_code=1, reason='부팅이 바뀌어 갱신 작업이 중단됐습니다. 자동 재개하지 않습니다.')
            else:
                active = _unit_state(store, value['unit'])
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(value['started_at'])).total_seconds()
                if age > 15 and active in {'inactive', 'failed'}:
                    value.update(state='interrupted', exit_code=1, reason='완료 기록 없이 갱신 서비스가 종료됐습니다.')
        return _public(value)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return {'state': 'unknown', 'exit_code': 1, 'reason': '화면 갱신 기록과 서비스 상태를 확인하지 못했습니다.'}


def submit_reload(config, target='default', release=None, *, paths=None, host=None):
    """Queue one fixed root worker; this function owns its short mutation lock."""
    paths, host = paths or Paths(), host or Host()
    target = target_id(target)
    config.for_screen(target)
    store = ControlStore(paths, host)
    pinned, _ = store.runtime.release(release or Path(__file__).resolve().parents[1])
    with change_lock(paths):
        if any(row['state'] in {'queued', 'running'} for row in JobStore(paths, host).list()):
            raise RuntimeError('설치 중에는 화면 갱신을 시작하지 않습니다.')
        state = store.runtime.load(target)
        if state is None:
            return {'state': 'pending', 'exit_code': 2,
                    'reason': '화면의 최초 관리 버전 등록 후 갱신을 다시 요청하세요. 현재 표시는 유지합니다.'}
        if store.runtime.is_stopped(state):
            return {'state': 'pending', 'exit_code': 2, 'reason': '수동 중지한 화면은 갱신으로 켜지 않습니다.'}
        if store.runtime.active(state):
            raise RuntimeError('선택 대상의 화면 작업이 진행 중입니다.')
        unit = f'ala-screen-reload-{target}.service'
        if _unit_state(store, unit) not in {'inactive', 'failed'}:
            raise RuntimeError('이 대상의 화면 갱신 서비스가 이미 진행 중입니다.')
        previous = store.latest(target)
        if previous and previous['boot_id'] == store.runtime.boot_id() and previous['state'] in {'queued', 'running', 'unknown'}:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(previous['started_at'])).total_seconds()
            if age < 15:
                raise RuntimeError('직전 화면 갱신 요청의 실행 여부를 확인 중입니다.')
        value = {'schema_version': 1, 'run_id': uuid.uuid4().hex, 'target_id': target,
                 'release': str(pinned), 'boot_id': store.runtime.boot_id(), 'unit': unit,
                 'state': 'queued', 'exit_code': 2, 'generation': state['generation'],
                 'selection': state['committed'], 'started_at': _now(), 'ended_at': None,
                 'reason': '화면 갱신을 접수했습니다. 실제 문서 확인을 기다립니다.'}
        store.select(value)
        result = host.run(['systemd-run', '--unit=' + unit, '--collect', '--expand-environment=no',
                           '--property=Type=exec', '--property=UMask=0077', '--property=TimeoutStartSec=150',
                           str(pinned / '.venv/bin/python'), '-I', str(pinned / 'entry.py'),
                           '_screen-worker', value['run_id']], timeout=30)
        if result.returncode:
            # A timed-out systemd client cannot establish whether the worker was
            # accepted. Keep its record available for a read-only reconciliation.
            observed = store.load(value['run_id'])
            if observed['state'] == 'queued':
                observed.update(state='unknown' if result.returncode == 124 else 'failed', exit_code=1,
                                reason='갱신 서비스 접수 여부를 확인하지 못했습니다.' if result.returncode == 124 else
                                       '갱신 서비스를 시작하지 못했습니다.')
                if result.returncode != 124:
                    observed['ended_at'] = _now()
                store.save(observed)
            return _public(observed)
    return _public(value)


def _context(store, value):
    selection = value['selection']
    config = Config.from_dict(store.runtime._validate_selection(selection))
    return replace(config, runtime_pin=selection).for_screen(value['target_id'], revision=selection['revision'])


def _supports_control(report, revision):
    if (not isinstance(report, dict) or not isinstance(report.get('health'), dict)
            or not isinstance(report.get('service'), dict)):
        return False
    health = report.get('health') or {}
    return bool(report.get('process_verified') is True and (report.get('service') or {}).get('ActiveState') == 'active'
            and health.get('state') not in {'unknown', 'failed'} and health.get('config_revision') == revision
            and isinstance(health.get('run_nonce'), str) and re.fullmatch(r'[a-f0-9]{32}', health['run_nonce']))


def run_worker(run_id, *, paths=None, host=None):
    """Private fixed entry: a fresh token fences both the IPC and its completion."""
    from . import display

    store = ControlStore(paths, host)
    token = None
    claimed = False
    try:
        with change_lock(store.paths):
            value = store.load(run_id)
            if value['state'] not in {'queued', 'unknown'} or value['boot_id'] != store.runtime.boot_id():
                raise RuntimeError('다시 실행할 수 없는 화면 갱신 요청입니다.')
            # A repeated private entry must never overwrite a running/completed
            # worker's record, even if both processes read it at the same time.
            claimed = True
            store.runtime.release(value['release'])
            if any(row['state'] in {'queued', 'running'} for row in JobStore(store.paths, store.host).list()):
                raise RuntimeError('설치 중에는 화면 갱신을 시작하지 않습니다.')
            state = store.runtime.load(value['target_id'])
            if (not state or state['committed'] != value['selection'] or state['generation'] != value['generation']):
                raise RuntimeError('갱신 요청 후 화면 선택이 바뀌었습니다.')
            token = store.runtime.reserve(value['target_id'], 'frontend', expected_generation=value['generation'],
                        expected_revision=value['selection']['revision'], ttl=120, unit=value['unit'])
            value.update(state='running', reason='기존 화면의 소유권·문서 갱신 결과를 확인 중입니다.')
            store.save(value)
        context = _context(store, value)
        bounded = display.BudgetHost(store.host, 90)
        before = display.chrome_action('check', context, host=bounded, paths=store.paths)
        if not _supports_control(before, token['revision']):
            outcome = {'state': 'pending', 'exit_code': 2,
                       'reason': '현재 감시의 소유권·갱신 프로토콜을 확인하지 못했습니다. 최초 화면 코드 적용 후 다시 확인하세요.'}
        else:
            _, _, home = display._account(context, bounded)
            payload = {'target_id': value['target_id'], 'config_revision': token['revision'], 'source': 'manual',
                       'operation_token': {key: token[key] for key in ('target_id', 'operation_id', 'generation', 'revision')}}
            if not store.runtime.valid(token):
                raise RuntimeError('화면 갱신 예약이 취소되었습니다.')
            result = bounded.run(['runuser', '-u', context.user, '--', 'env', '-i', f'HOME={home}',
                       'PATH=/usr/sbin:/usr/bin:/sbin:/bin',
                       str(Path(value['release']) / '.venv/bin/python'), '-I', str(Path(value['release']) / 'entry.py'),
                       '_frontend-control', json.dumps(payload)], timeout=65)
            response = json.loads(result.stdout)
            if not isinstance(response, dict):
                raise ValueError('화면 제어 응답 형식 오류')
            nonce = response.get('nonce')
            success = (result.returncode == 0 and type(response.get('exit_code')) is int
                       and response.get('exit_code') == 0 and response.get('state') == 'completed'
                       and isinstance(nonce, str) and re.fullmatch(r'[a-f0-9]{32}', nonce))
            if success and store.runtime.valid(token):
                after = display.chrome_action('check', context, host=bounded, paths=store.paths)
                health = after.get('health') or {}
                ack = health.get('control') or {}
                success = (_supports_control(after, token['revision']) and health.get('run_nonce') == before['health']['run_nonce']
                           and isinstance(ack, dict) and ack.get('nonce') == nonce and ack.get('state') == 'completed')
            outcome = {'state': 'completed' if success else 'failed', 'exit_code': 0 if success else 1,
                       'reason': '현재 관리 문서의 갱신 완료를 확인했습니다.' if success else
                                 '문서 갱신의 실제 완료를 확인하지 못했습니다. 현재 화면 상태와 로그를 확인하세요.'}
        with change_lock(store.paths):
            if not store.runtime.valid(token):
                raise RuntimeError('화면 갱신 예약이 취소되었습니다.')
            store.runtime.finish(token, success=outcome['state'] == 'completed', reason=outcome['reason'])
            value.update(**outcome, ended_at=_now())
            store.save(value)
        return outcome['exit_code']
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        if not claimed:
            return 1
        # Never finish or revive another generation after an explicit stop.
        with change_lock(store.paths):
            valid = token is not None and store.runtime.valid(token)
            if valid:
                store.runtime.finish(token, success=False, reason='수동 갱신의 후조건 확인 실패')
            value.update(state='failed' if valid or token is None else 'cancelled', exit_code=1, ended_at=_now(),
                         reason='화면 갱신이 취소·교체되었거나 후조건을 확인하지 못했습니다. 현재 화면을 다시 확인하세요.')
            store.save(value)
        return 1
