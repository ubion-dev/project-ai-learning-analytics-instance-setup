"""Root-owned runtime selections and short, generation-fenced target operations.

Reading selections never writes or resumes an operation. The CLI pointer is not
the runtime pointer: an old committed release remains selected across a crash or
boot until a separately observed trial is committed.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from .jobs import change_lock
from .system import Paths, Host, atomic_json

TARGET = re.compile(r'[a-z][a-z0-9_-]{0,31}\Z')
KINDS = {'runtime', 'frontend'}
MAX_BYTES = 1024 * 1024


def target_id(value):
    if not isinstance(value, str) or not TARGET.fullmatch(value):
        raise ValueError('유효한 화면 대상 ID가 필요합니다.')
    return value


def _stamp():
    return datetime.now(timezone.utc).isoformat()


class RuntimeStore:
    def __init__(self, paths=None, host=None, *, monotonic=None):
        self.paths, self.host = paths or Paths(), host or Host()
        # Existing /var/lib/ala is intentionally 0700 for root job records. A
        # separate root-owned 0755 tree exposes only the same non-secret config
        # already readable in /etc/ala/config.json to the display user.
        self.base = self.paths.at('/var/lib/ala-runtime')
        self.owner = 0 if self.paths.root == Path('/') else os.geteuid()
        self.monotonic = monotonic or time.monotonic

    def _safe(self, path):
        for item in (path, *path.parents):
            if item.is_symlink():
                raise PermissionError('런타임 상태 경로의 심볼릭 링크를 허용하지 않습니다.')
            if item.exists():
                info = item.stat()
                if info.st_uid != self.owner or info.st_mode & 0o022:
                    raise PermissionError('런타임 상태 소유자·쓰기 권한을 확인하세요.')
            if item == self.paths.root:
                break

    def _read(self, path):
        self._safe(path)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError('런타임 상태 파일의 형식·크기가 올바르지 않습니다.')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            current = os.fstat(stream.fileno())
            if (current.st_ino, current.st_dev) != (info.st_ino, info.st_dev):
                raise ValueError('런타임 상태가 조회 중 변경되었습니다.')
            content = stream.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise ValueError('런타임 상태 파일의 크기 제한을 초과했습니다.')
        return json.loads(content)

    def _write(self, path, value):
        if os.geteuid() != self.owner:
            raise PermissionError('런타임 선택 변경은 관리자 작업입니다.')
        self._safe(path)
        # A systemd worker uses UMask=0077. Only this dedicated non-secret tree
        # is deliberately searchable by the display user, never /var/lib/ala.
        ancestors = []
        for directory in self.base.parents:
            if directory == self.paths.root:
                break
            ancestors.append(directory)
        for directory in reversed(ancestors):
            self._safe(directory)
            if not directory.exists():
                directory.mkdir(mode=0o755)
                os.chmod(directory, 0o755)
        for directory in [self.base, *[self.base / part for part in reversed(path.parent.relative_to(self.base).parents)], path.parent]:
            if directory == self.base / '.':
                directory = self.base
            self._safe(directory)
            if not directory.exists():
                directory.mkdir(mode=0o755)
            os.chmod(directory, 0o755)
        self._safe(path.parent)
        atomic_json(path, value, 0o644)

    def boot_id(self):
        value = self.paths.at('/proc/sys/kernel/random/boot_id').read_text().strip()
        if not value or len(value) > 128:
            raise ValueError('런타임 작업의 현재 부팅 정보를 확인하지 못했습니다.')
        return value

    def state_path(self, target):
        return self.base / 'targets' / target_id(target) / 'state.json'

    def load(self, target):
        path = self.state_path(target)
        if not path.exists() and not path.is_symlink():
            return None
        value = self._read(path)
        if (not isinstance(value, dict) or value.get('schema_version') != 1
                or value.get('target_id') != target or type(value.get('generation')) is not int
                or value['generation'] < 0 or type(value.get('stopped')) is not bool):
            raise ValueError('런타임 대상 상태 형식이 올바르지 않습니다.')
        for name in ('committed', 'desired'):
            if value.get(name) is not None:
                self._validate_selection(value[name])
        operation = value.get('operation')
        if operation is not None:
            if (not isinstance(operation, dict) or operation.get('kind') not in KINDS
                    or not re.fullmatch(r'[a-f0-9]{32}', str(operation.get('operation_id', '')))
                    or type(operation.get('generation')) is not int
                    or not isinstance(operation.get('boot_id'), str)
                    or type(operation.get('expires_monotonic')) not in (int, float)):
                raise ValueError('런타임 대상 작업 형식이 올바르지 않습니다.')
        return value

    def release(self, path):
        path = Path(path).resolve(strict=True)
        base = self.paths.at('/opt/ala/releases').resolve(strict=True)
        if path.parent != base:
            raise PermissionError('검증된 ala release 디렉터리가 필요합니다.')
        self._safe(path)
        version = self._read_version(path)
        if path.name != version or not (path / 'entry.py').is_file() or not (path / '.venv/bin/python').is_file():
            raise ValueError('완료된 ala release와 고정 실행기가 필요합니다.')
        return path, version

    def _read_version(self, release):
        path = release / 'VERSION'
        self._safe(path)
        value = path.read_text().strip()
        if not re.fullmatch(r'\d+\.\d+\.\d+(?:rc\d+)?', value):
            raise ValueError('런타임 버전 형식이 올바르지 않습니다.')
        return value

    def selection(self, release, config):
        from .config import Config
        config = config.to_dict() if hasattr(config, 'to_dict') else config
        Config.from_dict(config)
        release, version = self.release(release)
        compatibility = release / 'COMPATIBILITY.json'
        if compatibility.exists():
            supported = self._read(compatibility)
            if (not isinstance(supported, dict) or supported.get('schema_version') != 1
                    or supported.get('runtime_protocol') != 1
                    or supported.get('extension_protocol', 1) != 1 or supported.get('frontend_protocol', 1) != 1
                    or not isinstance(supported.get('config_schemas'), list)
                    or config.get('schema_version', 1) not in supported.get('config_schemas', [])):
                raise ValueError('후보 런타임과 구성·관리 프로토콜이 호환되지 않습니다.')
        elif config.get('schema_version', 1) != 1:
            raise ValueError('구버전 런타임에 새 구성 schema를 적용하지 않습니다.')
        encoded = json.dumps(config, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
        revision = hashlib.sha256(encoded).hexdigest()
        path = self.base / 'configs' / (revision + '.json')
        if path.exists():
            if self._read(path) != config:
                raise ValueError('동일 구성 revision의 내용이 다릅니다.')
        else:
            self._write(path, config)
        return {'release': str(release), 'version': version, 'revision': revision, 'config_path': str(path)}

    def _validate_selection(self, selection):
        if (not isinstance(selection, dict) or not isinstance(selection.get('release'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', str(selection.get('revision', '')))):
            raise ValueError('런타임 선택 형식이 올바르지 않습니다.')
        config = self.base / 'configs' / (selection['revision'] + '.json')
        if selection.get('config_path') != str(config):
            raise PermissionError('런타임 구성은 root 스냅샷이어야 합니다.')
        release, version = self.release(selection['release'])
        if selection.get('version') != version:
            raise ValueError('런타임 선택 버전이 release와 다릅니다.')
        value = self._read(config)
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
        if hashlib.sha256(encoded).hexdigest() != selection['revision']:
            raise ValueError('런타임 구성 revision 검증 실패')
        return value

    def bootstrap(self, target, release, config):
        """Explicit enrollment only; existing committed selections are never moved."""
        state = self.load(target)
        if state is not None:
            return state
        selection = self.selection(release, config)
        state = {'schema_version': 1, 'target_id': target_id(target), 'generation': 0,
                 'stopped': False, 'committed': selection, 'desired': selection,
                 'operation': None, 'last_result': None}
        self._write(self.state_path(target), state)
        return state

    def stage(self, target, release, config, *, expected_generation=None, expected_revision=None):
        state = self.load(target)
        if state is None:
            raise ValueError('화면 런타임의 최초 등록이 필요합니다.')
        self._expect(state, expected_generation, expected_revision)
        if self.active(state):
            raise RuntimeError('이 대상의 화면 작업이 진행 중입니다.')
        state['desired'] = self.selection(release, config)
        state['generation'] += 1
        self._write(self.state_path(target), state)
        return state

    def settings_saved(self, target):
        """Invalidate old results without staging an unapplied edit for nightly rollout."""
        state = self.load(target)
        if state is None or self.active(state):
            raise RuntimeError('저장한 화면 설정의 변경을 기록할 수 없습니다.')
        state['generation'] += 1
        state['operation'] = None
        self._write(self.state_path(target), state)

    @staticmethod
    def _expect(state, generation=None, revision=None):
        if generation is not None and generation != state['generation']:
            raise RuntimeError('화면 선택이 변경되었습니다. 새 상태를 확인하세요.')
        if revision is not None and revision != (state.get('committed') or {}).get('revision'):
            raise RuntimeError('적용 구성 revision이 변경되었습니다. 새 상태를 확인하세요.')

    def active(self, state):
        operation = state.get('operation')
        return bool(operation and operation['boot_id'] == self.boot_id()
                    and operation['generation'] == state['generation']
                    and operation['expires_monotonic'] > self.monotonic())

    def trial_owner_active(self, state):
        operation = state.get('operation') or {}
        unit = operation.get('unit')
        if not unit:
            return True  # in-process reservation; production workers name a unit
        if not isinstance(unit, str) or not re.fullmatch(r'ala-runtime-(?:nightly|apply-[a-z][a-z0-9_-]{0,31})\.service', unit):
            return False
        response = self.host.run(['systemctl', 'show', unit, '--property=ActiveState'], timeout=5)
        properties = dict(line.split('=', 1) for line in response.stdout.splitlines() if '=' in line)
        return response.returncode == 0 and properties.get('ActiveState') in {'active', 'activating'}

    def _session(self, state):
        try:
            from .config import Config
            from .display import _account, _graphical_sessions
            config = Config.from_dict(self._validate_selection(state['committed']))
            uid, _, _ = _account(config, self.host)
            sessions, _ = _graphical_sessions(self.host, uid)
            if sessions is not None and len(sessions) == 1:
                return sessions[0][0]
        except (OSError, ValueError, RuntimeError):
            pass
        return None

    def is_stopped(self, state):
        if not state.get('stopped') or state.get('stop_boot_id') != self.boot_id():
            return False
        previous = state.get('stop_session_id')
        current = self._session(state)
        # A new GNOME login restores autostart; an unavailable session query
        # cannot prove that an operator's stop intent has expired.
        return not (previous is not None and current is not None and previous != current)

    def reserve(self, target, kind, *, expected_generation=None, expected_revision=None, ttl=240, unit=None,
                settings=None):
        if kind not in KINDS or type(ttl) not in (int, float) or not 0 < ttl <= 900:
            raise ValueError('허용된 화면 작업과 제한 시간이 필요합니다.')
        if settings is not None and kind != 'runtime':
            raise ValueError('화면 설정 후보에는 런타임 전환이 필요합니다.')
        from .jobs import JobStore
        if any(row.get('state') in {'queued', 'running'} for row in JobStore(self.paths, self.host).list()):
            raise RuntimeError('설치 작업 완료 후 화면 작업을 시작하세요.')
        state = self.load(target)
        if state is None:
            raise ValueError('화면 런타임의 최초 등록이 필요합니다.')
        self._expect(state, expected_generation, expected_revision)
        if self.is_stopped(state) and settings is None:
            raise RuntimeError('수동 중지한 화면은 자동 작업으로 켜지 않습니다.')
        if self.active(state):
            raise RuntimeError('이 대상의 화면 작업이 진행 중입니다.')
        revision = (state.get('committed') or {}).get('revision')
        operation = {'operation_id': uuid.uuid4().hex, 'kind': kind, 'generation': state['generation'],
                     'revision': revision, 'boot_id': self.boot_id(), 'started_at': _stamp(),
                     'expires_monotonic': self.monotonic() + ttl, 'unit': unit,
                     'candidate': state.get('desired') if kind == 'runtime' else None,
                     'phase': 'reserved'}
        if settings is not None:
            for key in ('committed', 'desired'):
                self._validate_selection(settings[key])
                if settings[key]['release'] != state[key]['release']:
                    raise ValueError('설정 적용은 기존 화면 코드 선택을 유지해야 합니다.')
            operation.update(settings_only=True, candidate=settings['committed'], settings_desired=settings['desired'])
        state['operation'] = operation
        self._write(self.state_path(target), state)
        return {'target_id': target, **operation}

    def valid(self, token):
        state = self.load(token['target_id'])
        operation = (state or {}).get('operation') or {}
        return bool(state and self.active(state) and (operation.get('settings_only') or not self.is_stopped(state))
                    and operation.get('operation_id') == token.get('operation_id')
                    and operation.get('generation') == token.get('generation')
                    and operation.get('revision') == token.get('revision'))

    def finish(self, token, *, success=True, reason='', outcome=None, next_action=''):
        state = self.load(token['target_id'])
        if state is None or not self.valid(token):
            raise RuntimeError('이미 취소·교체·중단된 화면 작업입니다.')
        operation = state['operation']
        if success and operation['kind'] == 'runtime':
            state['committed'] = operation['candidate']
            if operation.get('settings_only'):
                state['desired'] = operation['settings_desired']
        self._record_result(state, operation, outcome or ('completed' if success else 'failed'), reason, next_action)
        state['operation'] = None
        self._write(self.state_path(token['target_id']), state)
        return state

    @staticmethod
    def _record_result(state, operation, outcome, reason='', next_action=''):
        result = {'operation_id': operation['operation_id'], 'kind': operation['kind'],
                  'state': outcome, 'ended_at': _stamp(), 'reason': reason}
        if next_action:
            result['next_action'] = next_action
        if operation.get('settings_only'):
            state['last_settings_result'] = result | {'generation': state['generation']}
        else:
            state['last_result'] = result

    def phase(self, token, phase):
        if phase not in {'preflight', 'trial', 'observing', 'rollback'} or token.get('kind') != 'runtime':
            raise ValueError('허용된 런타임 전환 단계가 필요합니다.')
        if not self.valid(token):
            raise RuntimeError('이미 취소·교체·중단된 화면 작업입니다.')
        state = self.load(token['target_id'])
        state['operation']['phase'] = phase
        self._write(self.state_path(token['target_id']), state)
        return state

    def cancel(self, target, *, stopped=True):
        state = self.load(target)
        if state is None:
            return None
        state['generation'] += 1
        state['stopped'] = bool(stopped)
        state['stop_boot_id'] = self.boot_id() if stopped else None
        state['stop_session_id'] = self._session(state) if stopped else None
        if state.get('operation'):
            self._record_result(state, state['operation'], 'cancelled', '다른 화면 제어 요청으로 작업이 취소됐습니다.')
        state['operation'] = None
        self._write(self.state_path(target), state)
        return state

    def selections(self):
        directory = self.base / 'targets'
        if not directory.exists():
            return []
        self._safe(directory)
        return [self.load(path.name) for path in sorted(directory.iterdir()) if path.is_dir() and TARGET.fullmatch(path.name)]


def snapshot(target='default', *, paths=None, host=None):
    store = RuntimeStore(paths, host)
    state = store.load(target)
    return state | {'stopped': store.is_stopped(state)} if state is not None else None


def _target_config(config, target):
    return next((screen for screen in config.screen_targets() if screen.id == target), None)


def _with_screen(config, screen, *, saved_schema):
    if config.schema_version == saved_schema == 1:
        return replace(config, url=screen.url, autostart=screen.autostart)
    screens = {item.id: item for item in config.screen_targets()}
    screens[screen.id] = screen
    return replace(config, schema_version=2, screens=tuple(screens.values()))


def settings_status(config, target, *, paths=None, host=None, state=None):
    """Expose the confirmed configuration, even while a trial window is running."""
    from .config import Config
    store = RuntimeStore(paths, host)
    committed = (state or {}).get('committed')
    if committed is None:
        return {'state': 'unknown', 'pending': False, 'reason': '화면 관리 버전 최초 등록이 필요합니다.',
                'next_action': '업데이트 관리에서 선택 화면 코드 적용을 확인하세요.'}
    applied = _target_config(Config.from_dict(store._validate_selection(committed)), target)
    saved = _target_config(config, target)
    pending = applied != saved
    value = {'state': 'pending' if pending else 'applied', 'pending': pending,
             'applied_url': applied.url if applied else None,
             'applied_frontend': applied.frontend.to_dict() if applied else None}
    operation = state.get('operation') or {}
    if operation.get('settings_only'):
        value['operation_id'] = operation['operation_id']
        if store.active(state) and store.trial_owner_active(state):
            return value | {'state': 'queued' if operation.get('phase') == 'reserved' else 'running',
                            'reason': '저장한 설정을 적용하는 중입니다.'}
        return value | {'state': 'interrupted', 'reason': '화면 설정 작업의 실행이 중단됐습니다.',
                        'next_action': '현재 화면을 확인한 뒤 설정 적용을 다시 실행하세요.'}
    last = state.get('last_settings_result') or {}
    if last.get('generation') == state['generation']:
        value.update({key: last[key] for key in ('operation_id', 'state', 'reason', 'next_action') if key in last})
    return value


def reserve(target, kind='frontend', *, paths=None, host=None, **kwargs):
    store = RuntimeStore(paths, host)
    with change_lock(store.paths):
        return store.reserve(target, kind, **kwargs)


def check(token, *, paths=None, host=None):
    return RuntimeStore(paths, host).valid(token)


def finish(token, *, success=True, reason='', paths=None, host=None):
    store = RuntimeStore(paths, host)
    with change_lock(store.paths):
        return store.finish(token, success=success, reason=reason)


def cancel(target, *, stopped=True, paths=None, host=None):
    store = RuntimeStore(paths, host)
    with change_lock(store.paths):
        return store.cancel(target, stopped=stopped)


def stage(target, release, config, *, paths=None, host=None, **kwargs):
    store = RuntimeStore(paths, host)
    with change_lock(store.paths):
        return store.stage(target, release, config, **kwargs)


def bootstrap(target, release, config, *, paths=None, host=None):
    store = RuntimeStore(paths, host)
    with change_lock(store.paths):
        return store.bootstrap(target, release, config)


def load_user_selection(target='default', *, paths=None, host=None):
    """A user may read, never create, a root-owned committed/trial snapshot."""
    store = RuntimeStore(paths, host)
    state = store.load(target)
    if state is None:
        if os.environ.get('ALA_RUNTIME_CONFIG'):
            raise PermissionError('런타임 pin에 대응하는 root 선택이 없습니다.')
        return None
    selection = state['committed']
    # Trials never survive a boot, lease expiry, or manual stop. The selected
    # trial is only used while its root-owned operation remains current.
    if (store.active(state) and state['operation']['kind'] == 'runtime'
            and state['operation'].get('phase') in {'trial', 'observing'} and not store.is_stopped(state)
            and store.trial_owner_active(state)):
        selection = state['operation']['candidate']
    expected = os.environ.get('ALA_RUNTIME_CONFIG')
    if expected and (expected != selection['config_path'] or os.environ.get('ALA_RUNTIME_TARGET') != target):
        raise PermissionError('런타임 실행 pin과 현재 root 선택이 다릅니다.')
    config = store._validate_selection(selection)
    return {'config': config, 'revision': selection['revision'], 'release': selection['release'],
            'version': selection['version'], 'generation': state['generation']}


def dispatch(target='default', *, fallback=None, paths=None, host=None, execute=None):
    if os.getuid() == 0:
        raise PermissionError('화면 런타임은 일반 사용자로 실행하세요.')
    selected = load_user_selection(target, paths=paths, host=host)
    if selected is None:
        if fallback is None:
            raise ValueError('화면 런타임의 최초 등록이 필요합니다.')
        return fallback(target=target)
    store = RuntimeStore(paths, host)
    state = store.load(target)
    if store.is_stopped(state):
        return 78
    release = Path(selected['release'])
    legacy = not (release / 'COMPATIBILITY.json').is_file()
    if legacy and (target != 'default' or RuntimeStore(paths, host)._read((paths or Paths()).config) != selected['config']):
        raise ValueError('구버전 기본 런타임의 schema1 구성을 먼저 야간 이전해야 합니다.')
    environment = dict(os.environ, ALA_RUNTIME_CONFIG=str(store.base / 'configs' / (selected['revision'] + '.json')),
                       ALA_RUNTIME_TARGET=target)
    argv = [str(release / '.venv/bin/python'), '-I', str(release / 'entry.py'), '_chrome-run']
    if not legacy:
        argv += ['--target', target]
    (execute or os.execve)(argv[0], argv, environment)
    return 1  # execve must replace the process.


def bootstrap_from_observation(config, reports, *, release=None, paths=None, host=None, locked=False,
                               skip_inactive=False):
    """Preserve the old expected config before a schema/URL save changes it.

    Reports must be collected by the trusted caller, never supplied as an
    untrusted RPC payload. An active display needs an independently checked
    supervisor version and matching old URL fingerprint.
    """
    store = RuntimeStore(paths, host)
    config_data = config.to_dict() if hasattr(config, 'to_dict') else config
    from .config import Config
    model = Config.from_dict(config_data)
    targets = model.screen_targets() if hasattr(model, 'screen_targets') else [type('Default', (), {'id': 'default', 'url': model.url})()]
    results = []
    def enroll():
        for target in targets:
            existing = store.load(target.id)
            if existing is not None:
                results.append({'target_id': target.id, 'state': 'kept'})
                continue
            report = reports.get(target.id, {})
            service = report.get('service') or {}
            selected = release
            if service.get('ActiveState') in {'active', 'activating'}:
                health = report.get('health') or {}
                version = health.get('supervisor_version')
                fingerprint = hashlib.sha256((target.url or '').encode()).hexdigest()
                valid = (isinstance(version, str) and re.fullmatch(r'\d+\.\d+\.\d+(?:rc\d+)?', version)
                         and health.get('config_fingerprint') == fingerprint
                         and health.get('state') not in {'unknown', 'failed'}
                         and not any(row.get('state') in {'unknown', 'error', 'blocked'}
                                     for row in report.get('results', [])
                                     if row.get('id', '').startswith(('chrome.process', 'chrome.option.', 'chrome.forbidden'))))
                if valid:
                    valid = _observed_release_matches(store, report, version, target.id, model)
                if not valid:
                    results.append({'target_id': target.id, 'state': 'pending', 'reason': '현재 감독 버전·기존 구성의 실행 근거가 필요합니다.'})
                    continue
                selected = store.paths.at('/opt/ala/releases') / version
            elif service.get('ActiveState') != 'inactive' or service.get('QueryError'):
                results.append({'target_id': target.id, 'state': 'pending', 'reason': '현재 화면 실행 여부를 확인하지 못했습니다.'})
                continue
            elif skip_inactive:
                results.append({'target_id': target.id, 'state': 'not_applicable',
                                'reason': '실행하지 않는 최초 화면은 새 구성으로 등록합니다.'})
                continue
            if selected is None:
                selected = store.paths.at('/opt/ala/current').resolve(strict=True)
            store.bootstrap(target.id, selected, config_data)
            results.append({'target_id': target.id, 'state': 'completed'})
    if locked:
        enroll()
    else:
        with change_lock(store.paths):
            enroll()
    return {'results': results, 'state': 'pending' if any(item['state'] == 'pending' for item in results) else 'completed'}


def _observed_release_matches(store, report, version, target, config):
    """A user-writable health version is not a root runtime selection proof."""
    try:
        from .display import _account
        uid, _, _ = _account(config, store.host)
        health = report['health']
        pid = int(report['service']['MainPID'])
        if pid < 1 or health.get('supervisor_pid') != pid:
            return False
        process = store.paths.at('/proc') / str(pid)
        def identity():
            actual = next(line.split()[1] for line in (process / 'status').read_text().splitlines() if line.startswith('Uid:'))
            if int(actual) != uid:
                raise ValueError('감독 사용자 신원이 일치하지 않습니다.')
            ticks = (process / 'stat').read_text().rsplit(') ', 1)[1].split()[19]
            if not ticks.isdecimal():
                raise ValueError('감독 시작 신원을 확인하지 못했습니다.')
            return ticks
        before = identity()
        if before != health.get('supervisor_start_ticks'):
            return False
        args = (process / 'cmdline').read_bytes().decode().rstrip('\0').split('\0')
        if len(args) not in {4, 6} or args[1] != '-I':
            return False
        allowed = [['_chrome-run', '--target', target]]
        if target == 'default':
            allowed.append(['_chrome-run'])
        if args[3:] not in allowed or Path(args[2]).name != 'entry.py':
            return False
        release, actual = store.release(Path(args[2]).parent)
        # A mutable current alias describes today's pointer, not necessarily
        # the Python code loaded when this old process started. Do not infer
        # its running version from a user-writable health claim or file times.
        if Path(args[2]) != release / 'entry.py':
            return False
        return actual == version and identity() == before
    except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration):
        return False


def guard_config_change(current, new, *, paths=None, host=None):
    """Read only; legacy code cannot consume pinned snapshots or schema 2."""
    data = new.to_dict() if hasattr(new, 'to_dict') else new
    store = RuntimeStore(paths, host)
    for state in store.selections():
        selected = state['committed']
        if not (Path(selected['release']) / 'COMPATIBILITY.json').is_file():
            if store._validate_selection(selected) != data:
                raise ValueError('기존 화면 설정을 유지한 채 업데이트 관리의 선택 화면 코드 적용으로 최초 이전을 완료하세요.')


def code_ready(report, version, revision=None):
    """A site's HTTP outage is not evidence that a runtime release is broken."""
    service = report.get('service') or {}
    if service.get('ActiveState') != 'active' or service.get('QueryError'):
        return False
    health = report.get('health') or {}
    if health.get('supervisor_version') != version or health.get('state') in {'failed', 'unknown'}:
        return False
    if revision is not None and health.get('config_revision') != revision:
        return False
    if type(health.get('browser_pid')) is not int or health['browser_pid'] < 1:
        return False
    placement = report.get('placement') or {}
    if placement.get('state') not in {'ok', 'not_applicable'}:
        return False
    return not any(row.get('state') in {'error', 'unknown', 'blocked'}
                   for row in report.get('results', [])
                   if row.get('id', '').startswith(('chrome.process', 'chrome.option.', 'chrome.forbidden', 'chrome.window')))


def _prepare_nightly_selection(store, target):
    """Rebase RC21's previously staged edits before automatic code rollout."""
    from .config import Config
    from .signature import verified_release

    with change_lock(store.paths):
        state = store.load(target)
        if state is None or state['desired'] == state['committed']:
            return state
        if store.active(state):
            raise RuntimeError('이 대상의 화면 작업이 진행 중입니다.')
        desired = state['desired']
        if not verified_release(desired['version'], paths=store.paths):
            raise RuntimeError('실제 서명 검증된 런타임 후보가 필요합니다.')
        current = Config.from_dict(store._validate_selection(state['committed']))
        candidate = Config.from_dict(store._validate_selection(desired))
        applied = _target_config(current, target)
        if applied is None:
            raise ValueError('확정된 화면 설정을 확인하지 못했습니다.')
        normalized = _with_screen(candidate, applied, saved_schema=current.schema_version)
        if current.schema_version == 1 and len(normalized.screen_targets()) == 1:
            # RC21 screen edits upgraded the container to schema 2. Keep an
            # otherwise unchanged legacy default from restarting just for that.
            normalized = replace(normalized, schema_version=1, screens=(), url=applied.url, autostart=applied.autostart)
        if normalized != candidate:
            state = store.stage(target, desired['release'], normalized, expected_generation=state['generation'])
        return state


def apply_target(target, *, paths=None, host=None, control=None, monotonic=None, sleep=None,
                 unit='ala-runtime-nightly.service', settings_operation_id=None, preserve_screen_settings=False):
    """Observe outside the root lock; commit only the same live generation.

    The caller enforces the maintenance window. Each phase has a separate
    deadline: start 90s, stable observation 60s, failed-trial restoration 90s.
    """
    store = RuntimeStore(paths, host, monotonic=monotonic)
    clock, pause = store.monotonic, sleep or time.sleep
    if preserve_screen_settings:
        try:
            state = _prepare_nightly_selection(store, target)
        except (OSError, ValueError, RuntimeError) as exc:
            return {'state': 'pending', 'target_id': target, 'reason': str(exc)}
    else:
        state = store.load(target)
    if state is None:
        return {'state': 'pending', 'reason': '화면 런타임의 최초 등록이 필요합니다.'}
    token = None
    if settings_operation_id is not None:
        # systemd-run returns after exec. The worker may start while submit still
        # holds the reservation lock, so wait to claim the exact queued operation.
        with change_lock(store.paths, blocking=True):
            state = store.load(target)
            operation = (state or {}).get('operation') or {}
            if (not operation.get('settings_only') or operation['operation_id'] != settings_operation_id
                    or operation.get('phase') != 'reserved' or operation.get('unit') != unit):
                return {'state': 'cancelled', 'target_id': target}
            token = {'target_id': target, **operation}
            if not store.valid(token):
                return {'state': 'cancelled', 'target_id': target}
            store.phase(token, 'preflight')
    previous = state['committed']
    candidate = token['candidate'] if token is not None else state['desired']
    if previous == candidate and token is None:
        return {'state': 'kept', 'reason': '중지된 화면 또는 적용할 후보가 없는 화면입니다.'}
    if control is None:
        from .config import Config
        from .screens import screen_action
        def control(action, selection, *, timeout):
            config = Config.from_dict(store._validate_selection(selection)).for_screen(target)
            config = replace(config, runtime_pin=selection)
            return screen_action(action, config, target=target, paths=store.paths, host=store.host, timeout=timeout, internal=True)

    def invoke(action, selection, deadline):
        def run():
            remaining = deadline - clock()
            if remaining <= 0:
                raise RuntimeError('화면 전환 단계의 제한 시간이 초과됐습니다.')
            return control(action, selection, timeout=min(30, remaining))

        if action in {'start', 'stop'}:
            # Fence a short lifecycle request against manual stop/restart. Polling,
            # preflight, and the stable observation window stay outside this lock.
            with change_lock(store.paths, blocking=True):
                if not store.valid(token):
                    raise RuntimeError('화면 작업이 취소됐습니다.')
                result = run()
        else:
            result = run()
        if clock() >= deadline:
            raise RuntimeError('화면 전환 단계의 제한 시간이 초과됐습니다.')
        return result

    def conclude(outcome, reason, next_action='', *, commit=False):
        result = {'state': outcome, 'target_id': target, 'reason': reason,
                  'next_action': next_action, 'exit_code': 1 if outcome == 'failed' else 2}
        if settings_operation_id is not None:
            with change_lock(store.paths, blocking=True):
                if not store.valid(token):
                    return {'state': 'cancelled', 'target_id': target}
                store.finish(token, success=commit, reason=reason, outcome=outcome, next_action=next_action)
        return result | ({'selection': candidate} if commit else {})

    start_deadline = clock() + 90
    try:
        before = invoke('status', previous, start_deadline)
        service = before.get('service') or {}
        if service.get('QueryError') or service.get('ActiveState') not in {'inactive', 'failed', 'active', 'activating'}:
            return conclude('pending', before.get('reason') or '화면 실행 상태를 확인하지 못해 선택을 유지합니다.',
                            before.get('next_action') or '사용자 서비스와 GNOME 로그인 상태를 확인하세요.')
        if service['ActiveState'] in {'inactive', 'failed'}:
            # Inactive selection is independent of whether a window can be started.
            tested = store.host.run([str(Path(candidate['release']) / '.venv/bin/python'), '-I', '-B',
                                     str(Path(candidate['release']) / 'entry.py'), '--version'])
            if tested.returncode or tested.stdout.strip() != candidate['version']:
                return conclude('failed', '후보의 고정 실행 버전을 확인하지 못했습니다.', '화면 코드 설치 상태를 확인하세요.')
            if settings_operation_id is not None:
                return conclude('selected_unverified', '설정을 적용했습니다. 화면은 중지 상태입니다.', commit=True)
            with change_lock(store.paths):
                latest = store.load(target)
                store._expect(latest, state['generation'], previous['revision'])
                if store.active(latest):
                    raise RuntimeError('이 대상의 화면 작업이 진행 중입니다.')
                latest['committed'] = candidate
                latest['generation'] += 1
                latest['operation'] = None
                latest['last_result'] = {'kind': 'runtime', 'state': 'selected_unverified', 'ended_at': _stamp(),
                                         'reason': '미실행 화면의 코드 선택만 완료했습니다. 실제 화면 수용은 대기 중입니다.'}
                store._write(store.state_path(target), latest)
            return conclude('selected_unverified', '코드 선택 완료·실행 미검증. 중지된 화면을 켜지 않았습니다.', commit=True)
        if store.is_stopped(state):
            return conclude('pending', '수동 중지 이후 화면이 실행 중인 상태입니다.', '현재 화면의 중지 상태를 확인하세요.')
        preflight = invoke('preflight', candidate, start_deadline)
        if preflight.get('exit_code', 1) != 0:
            return conclude('pending', preflight.get('reason') or '로그인 세션·출력·실행 준비 확인이 필요합니다.',
                            preflight.get('next_action') or '화면 준비 상태를 확인한 뒤 다시 적용하세요.')
    except (OSError, RuntimeError, ValueError):
        if settings_operation_id is None:
            raise
        return conclude('failed', '화면 적용 사전 확인을 완료하지 못했습니다.', '상태와 로그를 확인한 뒤 다시 적용하세요.')
    if token is None:
        with change_lock(store.paths):
            token = store.reserve(target, 'runtime', expected_generation=state['generation'],
                                  expected_revision=previous['revision'], ttl=300, unit=unit)

    def valid():
        return store.valid(token)

    def phase(name):
        with change_lock(store.paths):
            store.phase(token, name)

    def wait_ready(selection, deadline, *, stable=False):
        healthy_since = None
        while clock() < deadline:
            if not valid():
                return False
            revision = selection['revision'] if (Path(selection['release']) / 'COMPATIBILITY.json').is_file() else None
            ready = code_ready(invoke('check', selection, deadline), selection['version'], revision)
            if ready:
                if not stable:
                    return True
                healthy_since = clock() if healthy_since is None else healthy_since
            else:
                if stable:
                    return False
                healthy_since = None
            pause(min(1, max(0, deadline - clock())))
        return bool(stable and healthy_since is not None and valid())

    def stop_confirmed(selection, deadline):
        while clock() < deadline and valid():
            result = invoke('stop', selection, deadline)
            if result.get('exit_code', 1) not in {0, 2}:
                return False
            # Exit 2 can mean the unit is inactive but its browser/window still
            # exists. Recheck the leaf's complete stop postcondition.
            if result.get('exit_code') == 0 and result.get('state') == 'stopped':
                return True
            pause(min(1, max(0, deadline - clock())))
        return False

    try:
        if not stop_confirmed(previous, start_deadline) or not valid():
            raise RuntimeError('이전 화면의 종료를 확인하지 못했거나 작업이 취소됐습니다.')
        phase('trial')
        if not valid():
            return {'state': 'cancelled'}
        started = invoke('start', candidate, start_deadline)
        if started.get('exit_code', 1) not in {0, 2} or not wait_ready(candidate, start_deadline):
            raise RuntimeError('후보 화면 코드의 시작 후조건을 확인하지 못했습니다.')
        phase('observing')
        if not wait_ready(candidate, clock() + 60, stable=True):
            raise RuntimeError('후보 화면 코드의 안정 관측에 실패했습니다.')
        with change_lock(store.paths):
            result = store.finish(token, success=True, reason='저장한 설정을 적용했습니다.' if settings_operation_id else '')
        return {'state': 'completed', 'target_id': target, 'selection': result['committed']}
    except (OSError, RuntimeError, ValueError):
        if not valid():
            # A user's stop beats rollback. Never revive a cancelled target.
            return {'state': 'cancelled', 'target_id': target}
        rollback_deadline = clock() + 90
        restored = False
        try:
            selected = candidate if (store.load(target)['operation'] or {}).get('phase') in {'trial', 'observing'} else previous
            if stop_confirmed(selected, rollback_deadline) and valid():
                phase('rollback')
                result = invoke('start', previous, rollback_deadline)
                restored = result.get('exit_code', 1) in {0, 2} and wait_ready(previous, rollback_deadline)
        except (OSError, RuntimeError, ValueError):
            restored = False
        if not valid():
            return {'state': 'cancelled', 'target_id': target}
        try:
            with change_lock(store.paths):
                store.finish(token, success=False, reason='후보 적용 실패 · 이전 화면 복원 ' + ('완료' if restored else '확인 불가'),
                             next_action='선택한 화면의 출력과 로그를 확인하세요.' if settings_operation_id else '')
        except RuntimeError:
            return {'state': 'cancelled', 'target_id': target}
        return {'state': 'failed', 'target_id': target, 'restored': restored,
                'reason': '후보 적용에 실패해 나머지 대상의 적용을 보류합니다.'}


def submit_apply(target='default', *, release=None, paths=None, host=None, settings_config=None):
    """An explicit operator transition may run outside the automatic window."""
    paths, host = paths or Paths(), host or Host()
    target_id(target)
    store = RuntimeStore(paths, host)
    with change_lock(paths):
        state = store.load(target)
        if state is None:
            if settings_config is not None:
                return {'state': 'pending', 'exit_code': 2, 'reason': '확정된 화면 관리 버전이 필요합니다.',
                        'next_action': '업데이트 관리에서 선택 화면 코드 적용으로 최초 이전을 완료하세요.'}
            raise ValueError('화면 런타임의 최초 등록이 필요합니다.')
        if store.active(state):
            raise RuntimeError('이 대상의 화면 작업이 진행 중입니다.')
        pinned, _ = store.release(release or paths.at('/opt/ala/current'))
        unit = 'ala-runtime-apply-' + target + '.service'
        active = host.run(['systemctl', 'show', unit, '--property=ActiveState'])
        properties = dict(line.split('=', 1) for line in active.stdout.splitlines() if '=' in line)
        if properties.get('ActiveState') in {'active', 'activating', 'deactivating', 'reloading'}:
            raise RuntimeError('이 대상의 명시적 런타임 적용이 진행 중입니다.')
        token = None
        if settings_config is not None:
            from .config import Config
            saved = settings_config()
            screen = _target_config(saved, target)
            if screen is None:
                raise ValueError('해당 화면 ID가 구성에 없습니다.')
            committed = state['committed']
            config = Config.from_dict(store._validate_selection(committed))
            if screen == _target_config(config, target):
                return {'state': 'kept', 'exit_code': 0, 'reason': '저장한 설정이 이미 적용돼 있습니다.'}
            if not (Path(committed['release']) / 'COMPATIBILITY.json').is_file():
                return {'state': 'pending', 'exit_code': 2, 'reason': '기존 화면 코드가 적용 스냅샷을 지원하지 않습니다.',
                        'next_action': '업데이트 관리에서 선택 화면 코드 적용으로 최초 이전을 완료하세요.'}
            # Rebase the pending code snapshot too. A later code rollout must
            # retain the applied edit while preserving its own version and other settings.
            desired = state['desired']
            desired_config = Config.from_dict(store._validate_selection(desired))
            try:
                candidate = store.selection(committed['release'], _with_screen(config, screen, saved_schema=saved.schema_version))
                desired_candidate = store.selection(desired['release'], _with_screen(desired_config, screen, saved_schema=saved.schema_version))
            except ValueError:
                return {'state': 'pending', 'exit_code': 2, 'reason': '저장한 설정을 기존 화면 선택과 함께 적용할 수 없습니다.',
                        'next_action': '겹치는 출력 설정과 업데이트 관리의 선택 화면 코드 적용을 확인하세요.'}
            token = store.reserve(target, 'runtime', ttl=300, unit=unit,
                                  settings={'committed': candidate, 'desired': desired_candidate})
        worker_args = ['_runtime-worker', target]
        if token is not None:
            worker_args.append(token['operation_id'])
        result = host.run(['systemd-run', '--unit=' + unit, '--collect', '--expand-environment=no',
                           '--property=Type=exec', '--property=TimeoutStartSec=infinity',
                           str(pinned / '.venv/bin/python'), '-I', str(pinned / 'entry.py'),
                           *worker_args], timeout=30)
        if result.returncode:
            if token is not None:
                reason = '화면 설정 적용 작업을 시작하지 못했습니다.'
                next_action = '사용자 서비스와 시스템 작업 상태를 확인한 뒤 다시 적용하세요.'
                store.finish(token, success=False, reason=reason, next_action=next_action)
                return {'state': 'failed', 'exit_code': 1, 'reason': reason, 'next_action': next_action,
                        'operation_id': token['operation_id']}
            raise RuntimeError('화면 런타임 적용 서비스의 시작을 확인하지 못했습니다.')
    return {'state': 'queued', 'target_id': target, 'unit': unit, 'exit_code': 2,
            'reason': '화면 설정 적용을 접수했습니다.' if settings_config is not None else '화면 코드 적용을 접수했습니다.',
            'next_action': '화면 관리에서 적용 상태를 확인하세요. 현재 화면은 실제 전환 단계까지 유지됩니다.'} | (
                {'operation_id': token['operation_id']} if token is not None else {})


def run_worker(target='default', *, paths=None, host=None, settings_operation_id=None):
    result = apply_target(target, paths=paths, host=host,
                          unit='ala-runtime-apply-' + target_id(target) + '.service',
                          settings_operation_id=settings_operation_id)
    return 1 if result['state'] in {'failed', 'unknown', 'cancelled'} else 2 if result['state'] in {'pending', 'selected_unverified'} else 0
