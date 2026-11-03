"""Explicit frontend deployments and bounded commands to a private display owner."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import re
import stat
import time
import urllib.parse
import urllib.request
import uuid

from .system import Paths, atomic_json

MAX_BYTES = 16384
CONTROL_TIMEOUT = 60
TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
TARGET = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')


def _url(value):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('인증정보 없는 HTTP/HTTPS manifest URL이 필요합니다.')
    try:
        parts = urllib.parse.urlsplit(value)
        valid = parts.scheme in {'http', 'https'} and parts.hostname and parts.port != 0
    except ValueError:
        valid = False
    sensitive = {'token', 'access_token', 'refresh_token', 'id_token', 'password', 'passwd',
                 'api_key', 'apikey', 'authorization', 'auth', 'secret', 'client_secret', 'signature'}
    if not valid or parts.username is not None or parts.password is not None or parts.fragment:
        raise ValueError('인증정보 없는 HTTP/HTTPS manifest URL이 필요합니다.')
    if any(key.lower().replace('-', '_') in sensitive for key, _ in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)):
        raise ValueError('인증정보가 포함된 manifest URL은 사용하지 않습니다.')
    return value


@dataclass(frozen=True)
class FrontendConfig:
    enabled: bool = False
    manifest_url: str | None = None

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError('frontend.enabled에는 true/false가 필요합니다.')
        if self.manifest_url is not None:
            _url(self.manifest_url)
        if self.enabled and not self.manifest_url:
            raise ValueError('프론트 자동 갱신에는 명시적 manifest URL이 필요합니다.')

    @classmethod
    def from_dict(cls, value):
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict) or set(value) - {'enabled', 'manifest_url'}:
            raise ValueError('frontend 구성이 올바르지 않습니다.')
        return cls(**value)

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Deployment:
    deployment_seq: int
    build_id: str
    ready: bool

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {'schema_version', 'deployment_seq', 'build_id', 'ready'}:
            raise ValueError('배포 manifest 형식이 올바르지 않습니다.')
        seq, build, ready = value['deployment_seq'], value['build_id'], value['ready']
        if type(value['schema_version']) is not int or value['schema_version'] != 1:
            raise ValueError('지원하지 않는 배포 manifest 버전입니다.')
        if type(seq) is not int or not 0 < seq < 2**63 or not isinstance(build, str) or not TOKEN.fullmatch(build) or type(ready) is not bool:
            raise ValueError('배포 순번·build ID·ready가 올바르지 않습니다.')
        return cls(seq, build, ready)


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        _url(newurl)
        old, new = urllib.parse.urlsplit(request.full_url), urllib.parse.urlsplit(newurl)
        if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
            raise ValueError('manifest의 다른 출처 이동을 허용하지 않습니다.')
        return super().redirect_request(request, fp, code, message, headers, newurl)


def fetch_deployment(url, *, opener=None):
    _url(url)
    request = urllib.request.Request(url, headers={'Cache-Control': 'no-cache, no-store',
                                                   'User-Agent': 'ala-frontend-deployment/1'})
    try:
        open_url = opener or urllib.request.build_opener(_Redirect()).open
        with open_url(request, timeout=5) as response:
            directives = {item.strip().lower() for item in response.headers.get('Cache-Control', '').split(',')}
            if response.status != 200 or 'no-store' not in directives:
                raise ValueError('manifest는 HTTP 200과 Cache-Control: no-store가 필요합니다.')
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('manifest 크기 제한을 초과했습니다.')
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError('manifest 필드가 중복되었습니다.')
                value[key] = item
            return value
        return Deployment.from_dict(json.loads(raw, object_pairs_hook=unique))
    except (OSError, ValueError, TypeError, UnicodeError):
        raise ValueError('배포 manifest 조회·형식·캐시 정책을 확인하지 못했습니다.') from None


def _target(target_id):
    if not isinstance(target_id, str) or not TARGET.fullmatch(target_id):
        raise ValueError('유효한 화면 대상 ID가 필요합니다.')
    return target_id


def control_path(home, target_id='default'):
    return Path(home) / '.local/state/ala/control' / _target(target_id) / 'request.json'


def _safe_directory(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('제어 경로의 심볼릭 링크를 허용하지 않습니다.')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
        raise ValueError('제어 디렉터리 소유자·권한을 확인하지 못했습니다.')


def _read_owned(path, *, max_bytes=MAX_BYTES):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('제어 기록의 심볼릭 링크를 허용하지 않습니다.')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > max_bytes:
            raise ValueError('제어 기록의 소유자·권한·크기가 올바르지 않습니다.')
        value = stream.read(max_bytes + 1)
        if len(value) > max_bytes:
            raise ValueError('제어 기록 크기 제한을 초과했습니다.')
        return json.loads(value)


def valid_request(value, target_id, config_revision, run_nonce, *, now=None):
    keys = {'schema_version', 'nonce', 'target_id', 'config_revision', 'run_nonce', 'created_at',
            'expires_at', 'expected_build_id', 'deployment_seq', 'source', 'operation_token'}
    if not isinstance(value, dict) or set(value) != keys or type(value.get('schema_version')) is not int or value.get('schema_version') != 1:
        return False
    if value['target_id'] != target_id or value['config_revision'] != config_revision or value['run_nonce'] != run_nonce:
        return False
    if not isinstance(value['nonce'], str) or not re.fullmatch('[a-f0-9]{32}', value['nonce']):
        return False
    created, expires = value['created_at'], value['expires_at']
    now = time.time() if now is None else now
    if any(type(item) not in (int, float) for item in (created, expires)) or not created <= now <= expires <= created + CONTROL_TIMEOUT + 5:
        return False
    build, seq = value['expected_build_id'], value['deployment_seq']
    if build is not None and (not isinstance(build, str) or not TOKEN.fullmatch(build)):
        return False
    if seq is not None and (type(seq) is not int or not 0 < seq < 2**63):
        return False
    operation = value['operation_token']
    if operation is not None and (not isinstance(operation, dict) or
            set(operation) != {'target_id', 'operation_id', 'generation', 'revision'} or
            operation.get('target_id') != target_id or operation.get('revision') != config_revision or
            type(operation.get('generation')) is not int or operation['generation'] < 0 or
            not isinstance(operation.get('operation_id'), str) or not re.fullmatch('[a-f0-9]{32}', operation['operation_id'])):
        return False
    return (isinstance(value['source'], str) and value['source'] in {'manual', 'frontend'} and
            (value['source'] != 'frontend' or (build is not None and seq is not None)))


def request_reload(target_id, config_revision, home, paths=None, *, expected_build_id=None,
                   deployment_seq=None, source='manual', wait=False, timeout=CONTROL_TIMEOUT,
                   stop_requested=None, operation_token=None):
    from .display_health import state_path
    target_id = _target(target_id)
    if os.getuid() == 0:
        return {'exit_code': 1, 'state': 'blocked', 'reason': '화면 제어 요청은 지정한 일반 사용자로 실행하세요.'}
    try:
        health = _read_owned(state_path(home, target_id))
        if not isinstance(health, dict) or health.get('target_id', 'default') != target_id or health.get('config_revision', '') != config_revision:
            raise ValueError('현재 화면 구성과 제어 요청이 다릅니다.')
        run_nonce = health.get('run_nonce')
        if not isinstance(run_nonce, str) or not re.fullmatch('[a-f0-9]{32}', run_nonce):
            raise ValueError('실행 중인 감시가 제어 요청을 지원하지 않습니다.')
        age = time.time() - datetime.fromisoformat(health['updated_at']).timestamp()
        if not 0 <= age <= 90:
            raise ValueError('현재 감시 상태가 오래되어 요청하지 않았습니다.')
        now = time.time()
        request = {'schema_version': 1, 'nonce': uuid.uuid4().hex, 'target_id': target_id,
                   'config_revision': config_revision, 'run_nonce': run_nonce,
                   'created_at': now, 'expires_at': now + CONTROL_TIMEOUT,
                   'expected_build_id': expected_build_id, 'deployment_seq': deployment_seq, 'source': source,
                   'operation_token': operation_token}
        if not valid_request(request, target_id, config_revision, run_nonce, now=now):
            raise ValueError('제어 요청 형식이 올바르지 않습니다.')
        path = control_path(home, target_id)
        _safe_directory(path.parent)
        lock = os.open(path.parent / 'request.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(lock)
            if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError('제어 잠금 소유자·권한이 올바르지 않습니다.')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if path.exists():
                previous = _read_owned(path)
                ack = health.get('control', {})
                if (isinstance(previous, dict) and previous.get('run_nonce') == run_nonce and
                        isinstance(previous.get('expires_at'), (int, float)) and previous['expires_at'] > now and
                        not (isinstance(ack, dict) and ack.get('nonce') == previous.get('nonce') and ack.get('state') in {'completed', 'failed', 'rejected'})):
                    raise ValueError('다른 화면 제어 요청이 진행 중입니다.')
            atomic_json(path, request, 0o600)
        finally:
            os.close(lock)
        result = {'exit_code': 2, 'state': 'pending', 'nonce': request['nonce'], 'reason': '새로고침 요청 접수. 실제 문서 확인은 대기 중입니다.'}
        deadline = time.monotonic() + max(0, min(timeout, CONTROL_TIMEOUT))
        while wait and time.monotonic() < deadline:
            if stop_requested and stop_requested():
                return result | {'exit_code': 1, 'state': 'canceled', 'reason': '화면 작업 예약이 취소되었습니다.'}
            observed = _read_owned(state_path(home, target_id))
            if observed.get('run_nonce') != run_nonce:
                return result | {'exit_code': 1, 'state': 'failed', 'reason': '요청 중 감시 프로세스가 변경되었습니다.'}
            ack = observed.get('control', {})
            if isinstance(ack, dict) and ack.get('nonce') == request['nonce'] and ack.get('state') in {'completed', 'failed', 'rejected'}:
                return result | {'exit_code': 0 if ack['state'] == 'completed' else 1, 'state': ack['state'],
                                 'reason': '문서·요청한 빌드 준비를 확인했습니다.' if ack['state'] == 'completed' else '문서·요청한 빌드 준비를 확인하지 못했습니다.'}
            time.sleep(0.2)
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return {'exit_code': 1, 'state': 'unknown', 'reason': '실행 중 감시·구성·제어 경로를 확인하지 못했습니다.'}


def control_request(payload):
    allowed = {'target_id', 'config_revision', 'expected_build_id', 'deployment_seq', 'source', 'operation_token'}
    if not isinstance(payload, dict) or set(payload) - allowed or not {'target_id', 'config_revision'} <= set(payload):
        return {'exit_code': 1, 'state': 'error', 'reason': '허용되지 않은 화면 제어 요청입니다.'}
    return request_reload(home=Path(pwd.getpwuid(os.getuid()).pw_dir), wait=True, **payload)


class Coordinator:
    """Persist deployment attempts; callbacks own runtime reservations and IPC."""
    def __init__(self, state, *, save, fetch=fetch_deployment):
        self.state, self.save, self.fetch = state, save, fetch

    def poll(self, manifest_url, targets, *, observe, reserve, check, reload, finish, retry=False):
        if not isinstance(targets, (list, tuple)) or len(targets) > 16 or any(_target(target) != target for target in targets):
            raise ValueError('프론트 갱신 대상 목록이 올바르지 않습니다.')
        key = hashlib.sha256(manifest_url.encode()).hexdigest()
        group = self.state.setdefault(key, {'seen_seq': 0, 'candidate': None, 'attempts': {}, 'failed_seq': None})
        group['targets'] = list(dict.fromkeys([*targets, *group.get('targets', [])]))[:16] if retry else list(targets)
        group['observed_at'] = time.time()
        try:
            deployment = self.fetch(manifest_url)
        except (OSError, ValueError):
            if group.get('candidate'):
                group['candidate']['confirmations'] = 0
            group['status'] = 'manifest_unknown'
            self.save(self.state)
            return {'state': 'unknown', 'reason': '배포 조회 실패. 현재 화면을 유지합니다.'}
        seq, build = deployment.deployment_seq, deployment.build_id
        if seq < group['seen_seq']:
            return {'state': 'kept', 'reason': '이전 배포 순번은 적용하지 않습니다.'}
        candidate = group.get('candidate')
        if seq == group['seen_seq'] and group.get('seen_build') not in {None, build}:
            group['status'] = 'manifest_conflict'
            self.save(self.state)
            return {'state': 'blocked', 'reason': '같은 배포 순번의 build ID가 변경되었습니다.'}
        if seq > group['seen_seq']:
            group.update(seen_seq=seq, seen_build=build, candidate=None, attempts={}, failed_seq=None)
        if not deployment.ready:
            group['candidate'] = None
            group['status'] = 'waiting_ready'
            self.save(self.state)
            return {'state': 'pending', 'reason': '배포 준비 신호를 기다립니다.'}
        candidate = group.get('candidate')
        count = candidate['confirmations'] + 1 if candidate and candidate['deployment_seq'] == seq and candidate['build_id'] == build else 1
        group['candidate'] = {'deployment_seq': seq, 'build_id': build, 'confirmations': min(2, count)}
        self.save(self.state)
        if count < 2:
            return {'state': 'pending', 'reason': '같은 준비 배포를 다음 조회에서 다시 확인합니다.'}
        if group.get('failed_seq') == seq and not retry:
            return {'state': 'blocked', 'reason': '이 배포의 자동 갱신이 실패하여 나머지 대상을 보류합니다. 명시적으로 재시도하세요.'}
        if retry:
            group['failed_seq'] = None
            group['attempts'] = {target: item for target, item in group['attempts'].items()
                                 if target not in targets or item.get('state') == 'completed'}
        for target in targets:
            if target in group['attempts']:
                if group['attempts'][target].get('state') != 'completed':
                    group['failed_seq'] = seq
                    self.save(self.state)
                    return {'state': 'blocked', 'reason': '이전 배포 작업이 완료되지 않아 자동 재실행하지 않습니다.'}
                continue
            actual = observe(target)
            if actual.get('deferred'):
                return {'state': 'pending', 'reason': '현재 화면 감시·적용 구성·제어 지원을 확인한 뒤 갱신합니다.'}
            if not actual.get('running'):
                continue
            if actual.get('build_id') == build and actual.get('app_ready') is True:
                group['attempts'][target] = {'state': 'completed', 'already_current': True}
                self.save(self.state)
                continue
            token = reserve(target, actual)
            if token is None:
                return {'state': 'pending', 'reason': '다른 화면 작업 완료 후 갱신합니다.'}
            success = False
            try:
                if not check(token):
                    return {'state': 'pending', 'reason': '화면 작업 예약이 취소되었습니다.'}
                # Persist BEFORE navigation: crash/reentry cannot repeat an
                # automatic attempt for this target and deployment.
                group['attempts'][target] = {'state': 'started', 'revision': token.get('revision', '')}
                group['status'] = 'applying'
                self.save(self.state)
                try:
                    result = reload(target, deployment, token)
                except (OSError, ValueError, RuntimeError, TypeError):
                    result = {'exit_code': 1, 'state': 'failed'}
                success = result.get('exit_code') == 0 and result.get('state') == 'completed' and check(token)
                group['attempts'][target]['state'] = 'completed' if success else 'failed'
                if not success:
                    group.update(failed_seq=seq, status='failed')
                self.save(self.state)
            finally:
                try:
                    finish(token, success)
                except (OSError, ValueError, RuntimeError):
                    success = False
                    if target in group['attempts']:
                        group['attempts'][target]['state'] = 'failed'
                        group.update(failed_seq=seq, status='canceled')
                        self.save(self.state)
            if not success:
                return {'state': 'failed', 'reason': '문서·배포 빌드·준비 신호를 확인하지 못해 나머지 대상을 보류합니다.'}
        group['status'] = 'completed'
        self.save(self.state)
        return {'state': 'completed', 'reason': '실행 중인 대상의 배포 갱신을 확인했습니다.'}


def read_status(paths=None):
    """Read deployment outcomes without creating state or retrying a target."""
    paths = paths or Paths()
    path = paths.at('/var/lib/ala/frontend/state.json')
    try:
        if not path.exists() and not path.is_symlink():
            return {'state': 'ok', 'targets': {}, 'reason': '아직 관측한 프론트 배포 기록이 없습니다.'}
        value = _read_owned(path, max_bytes=65536)
        if not isinstance(value, dict) or len(value) > 64:
            raise ValueError('invalid state')
        targets, observed_at = {}, {}
        for key, group in value.items():
            if not re.fullmatch('[a-f0-9]{64}', key) or not isinstance(group, dict):
                raise ValueError('invalid group')
            seq, build, failed = group.get('seen_seq', 0), group.get('seen_build'), group.get('failed_seq')
            if type(seq) is not int or not 0 <= seq < 2**63 or (build is not None and (not isinstance(build, str) or not TOKEN.fullmatch(build))):
                raise ValueError('invalid deployment')
            if failed is not None and (type(failed) is not int or not 0 < failed < 2**63):
                raise ValueError('invalid failure')
            known, attempts = group.get('targets', []), group.get('attempts', {})
            observed = group.get('observed_at', 0)
            status = group.get('status')
            if (type(observed) not in (int, float) or not math.isfinite(observed) or observed < 0 or
                    (status is not None and (not isinstance(status, str) or status not in
                     {'manifest_unknown', 'manifest_conflict', 'waiting_ready', 'applying', 'failed', 'canceled', 'completed'}))):
                raise ValueError('invalid observation')
            if not isinstance(known, list) or len(known) > 16 or not isinstance(attempts, dict) or len(attempts) > 16:
                raise ValueError('invalid targets')
            for target in dict.fromkeys([*known, *attempts]):
                _target(target)
                if observed < observed_at.get(target, -1):
                    continue
                attempt = attempts.get(target, {})
                if not isinstance(attempt, dict) or attempt.get('state') not in {None, 'started', 'completed', 'failed'}:
                    raise ValueError('invalid attempt')
                state, reason, action = 'pending', '새 배포의 준비·순차 갱신을 기다립니다.', '다음 배포 조회와 현재 작업 완료를 확인하세요.'
                if group.get('status') == 'manifest_unknown':
                    state, reason = 'unknown', '배포 조회를 확인하지 못해 현재 화면을 유지합니다.'
                elif group.get('status') == 'manifest_conflict':
                    state, reason, action = 'blocked', '같은 배포 순번의 build ID가 변경되었습니다.', '더 큰 새 배포 순번으로 manifest를 게시하세요.'
                elif attempt.get('state') == 'completed':
                    state, reason, action = 'completed', '이 대상의 배포 빌드와 준비 상태를 확인했습니다.', ''
                elif failed is not None or attempt.get('state') == 'failed':
                    state, reason, action = 'blocked', '이 배포의 갱신 실패·취소로 남은 대상을 보류합니다.', '배포와 현재 화면을 확인한 뒤 이 대상을 명시적으로 재시도하세요.'
                elif attempt.get('state') == 'started':
                    state, reason = 'pending', '문서·배포 빌드·앱 준비를 확인 중입니다.'
                elif group.get('status') == 'completed':
                    state, reason, action = 'not_applicable', '중지된 대상은 배포 갱신으로 켜지 않습니다.', ''
                elif group.get('status') == 'waiting_ready':
                    reason = '배포 manifest의 ready 신호를 기다립니다.'
                targets[target] = {'state': state, 'deployment_seq': seq, 'build_id': build,
                                   'failed_seq': failed, 'reason': reason, 'next_action': action}
                observed_at[target] = observed
        return {'state': 'ok', 'targets': targets, 'reason': ''}
    except (OSError, ValueError, TypeError, KeyError):
        return {'state': 'unknown', 'targets': {}, 'reason': '프론트 배포 기록의 범위·형식·권한을 확인하지 못했습니다.'}


def run_worker(*, config=None, host=None, paths=None, retry=False, target_id=None, manifest_url=None, fetch=None):
    """One root timer invocation. The timer owns 60s plus jitter scheduling."""
    from .config import Config
    from .system import Host
    from . import display, runtime
    paths, host = paths or Paths(), host or Host()
    if os.geteuid() != 0:
        return 1
    if type(retry) is not bool or (retry and target_id is None and manifest_url is None):
        return 1
    if target_id is not None:
        _target(target_id)
    if manifest_url is not None:
        _url(manifest_url)
    # The expected /etc configuration can already describe tomorrow's code or
    # a new site. Only immutable committed snapshots describe today's windows.
    contexts = {}
    try:
        for snapshot in runtime.RuntimeStore(paths, host).selections():
            target = snapshot['target_id']
            selection = snapshot.get('committed')
            if not selection or (target_id is not None and target != target_id):
                continue
            applied = Config.load(Path(selection['config_path']))
            context = applied.for_screen(target, revision=selection['revision'])
            if context.frontend.enabled and (manifest_url is None or context.frontend.manifest_url == manifest_url):
                contexts[target] = context
    except (OSError, ValueError, KeyError, TypeError):
        return 1
    if not contexts:
        return 1 if retry else 0
    path = paths.at('/var/lib/ala/frontend/state.json')
    _safe_directory(path.parent)
    lock = os.open(path.parent / 'worker.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(lock)
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            return 1
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = _read_owned(path, max_bytes=65536) if path.exists() else {}
        if not isinstance(state, dict):
            return 1
        coordinator = Coordinator(state, save=lambda value: atomic_json(path, value, 0o600), fetch=fetch or fetch_deployment)
        groups = {}
        for target, context in contexts.items():
            groups.setdefault(context.frontend.manifest_url, []).append(target)
        def observe(target):
            snapshot = runtime.snapshot(target, paths=paths, host=host)
            value = display.chrome_action('status', contexts[target], host=host, paths=paths)
            health = value.get('health') or {}
            observed = health.get('frontend') or {}
            try:
                fresh = 0 <= time.time() - datetime.fromisoformat(observed['observed_at']).timestamp() <= 15
            except (KeyError, ValueError, TypeError):
                fresh = False
            active = (value.get('service') or {}).get('ActiveState')
            running = bool(snapshot and not snapshot.get('stopped') and active == 'active')
            supported = (isinstance(health.get('run_nonce'), str) and re.fullmatch('[a-f0-9]{32}', health['run_nonce']) and
                         snapshot and contexts[target].config_revision == snapshot['committed']['revision'] and
                         health.get('config_revision') == snapshot['committed']['revision'])
            return {'running': running, 'deferred': (running and not supported) or active not in {'active', 'inactive', 'failed'},
                    'snapshot': snapshot, 'build_id': observed.get('build_id') if fresh else None,
                    'app_ready': observed.get('app_ready') if fresh else None}
        def reserve(target, actual):
            snapshot = actual['snapshot']
            try:
                return runtime.reserve(target, kind='frontend', expected_generation=snapshot['generation'],
                                       expected_revision=snapshot['committed']['revision'], ttl=240, paths=paths, host=host)
            except (OSError, ValueError, RuntimeError):
                return None
        def reload(target, deployment, token):
            context = contexts[target]
            _, _, home = display._account(context, host)
            payload = {'target_id': target, 'config_revision': token['revision'], 'expected_build_id': deployment.build_id,
                       'deployment_seq': deployment.deployment_seq, 'source': 'frontend',
                       'operation_token': {key: token[key] for key in ('target_id', 'operation_id', 'generation', 'revision')}}
            release = Path(__file__).resolve().parents[1]
            result = host.run(['runuser', '-u', context.user, '--', 'env', '-i', f'HOME={home}',
                               'PATH=/usr/sbin:/usr/bin:/sbin:/bin',
                               str(release / '.venv/bin/python'), '-I', str(release / 'entry.py'),
                               '_frontend-control', json.dumps(payload)], timeout=CONTROL_TIMEOUT + 5)
            try:
                value = json.loads(result.stdout)
                return value if isinstance(value, dict) and not result.returncode else {'exit_code': 1, 'state': 'unknown'}
            except (TypeError, ValueError):
                return {'exit_code': 1, 'state': 'unknown'}
        failed = False
        for url, targets in groups.items():
            value = coordinator.poll(url, targets, observe=observe, reserve=reserve,
                check=lambda token: runtime.check(token, paths=paths, host=host), reload=reload,
                finish=lambda token, success: runtime.finish(token, success=success, paths=paths, host=host), retry=retry)
            failed |= value['state'] in {'unknown', 'failed', 'blocked'}
        return 1 if failed else 0
    except (OSError, ValueError, KeyError, TypeError):
        return 1
    finally:
        os.close(lock)
