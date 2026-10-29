from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import fcntl
import json
import os
import re
import stat
import uuid

from . import __version__
from .config import Config
from .system import Host, Paths, atomic_json

RUN_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}\Z")

def now():
    return datetime.now(timezone.utc).isoformat()

def boot_id(paths: Paths):
    return paths.at('/proc/sys/kernel/random/boot_id').read_text().strip()

def trusted_release(release: Path, base=Path('/opt/ala/releases')):
    release = release.resolve(strict=True)
    if not release.is_relative_to(base.resolve(strict=True)) or release == base:
        raise PermissionError('먼저 배포 묶음의 install.sh로 ala를 설치하세요.')
    for p in [release, *release.parents]:
        info = p.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise PermissionError(f'신뢰할 수 없는 실행 경로: {p}')
    return release

def protected_directory(path: Path, mode=0o700):
    if path.is_symlink():
        raise PermissionError(f'심볼릭 링크 경로를 사용하지 않습니다: {path}')
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    info = path.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise PermissionError(f'소유자/쓰기 권한을 확인하세요: {path}')

@contextmanager
def change_lock(paths=Paths(), *, blocking=False):
    lock = paths.at('/run/lock/ala/operation.lock')
    protected_directory(lock.parent, 0o755)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise PermissionError('ala 잠금 소유자가 올바르지 않습니다.')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise RuntimeError('다른 설치 또는 도구 변경 작업이 진행 중입니다.') from None
        yield
    finally:
        os.close(fd)

class JobStore:
    def __init__(self, paths=None, host=None):
        self.paths, self.host = paths or Paths(), host or Host()

    def directory(self, run_id):
        if not RUN_ID.fullmatch(run_id):
            raise ValueError('유효하지 않은 실행 ID입니다.')
        return self.paths.runs / run_id

    def load(self, run_id):
        value = json.loads((self.directory(run_id) / 'result.json').read_text())
        if value.get('state') in {'queued', 'running'}:
            if value['boot_id'] != boot_id(self.paths):
                value = dict(value, state='interrupted', reason='부팅이 바뀌었습니다. 다시 적용하기 전에 계획을 확인하세요.')
            else:
                active = self.host.run(['systemctl', 'show', value['unit'], '--property=ActiveState', '--property=LoadState'])
                properties = dict(line.split('=', 1) for line in active.stdout.splitlines() if '=' in line)
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(value['started_at'])).total_seconds()
                confirmed_stopped = properties.get('LoadState') == 'not-found' or (
                    active.returncode == 0 and properties.get('ActiveState') in {'inactive', 'failed'})
                if age > 15 and confirmed_stopped:
                    value = dict(value, state='interrupted', reason='실행 unit이 없거나 종료 기록이 없습니다.')
                elif active.returncode or not properties.get('ActiveState'):
                    value = dict(value, query_warning='unit 상태 조회 불가. 실행 중단으로 단정하지 않습니다.')
        return value

    def list(self):
        if not self.paths.runs.exists():
            return []
        result = []
        for p in sorted(self.paths.runs.iterdir(), reverse=True):
            if RUN_ID.fullmatch(p.name) and p.is_dir() and not p.is_symlink():
                result.append(self.load(p.name))
        return result

    def save(self, value):
        atomic_json(self.directory(value['run_id']) / 'result.json', value)

    def submit(self, config, release: Path, python: Path, *, expected_config=None, operation="setup"):
        if operation not in {"setup", "prepare"}:
            raise ValueError("허용되지 않은 설치 작업입니다.")
        with change_lock(self.paths):
            config = config() if callable(config) else config
            if expected_config is not None and config != Config.from_dict(expected_config):
                raise RuntimeError('검토 후 구성이 변경되었습니다. 변경 계획을 다시 확인하세요.')
            if any(r['state'] in {'queued', 'running'} for r in self.list()):
                raise RuntimeError('설치 작업이 이미 진행 중입니다. ala status로 확인하세요.')
            protected_directory(self.paths.at('/var/lib/ala'))
            protected_directory(self.paths.runs)
            run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]
            directory = self.directory(run_id)
            protected_directory(directory)
            unit = f'ala-{operation}-{run_id}.service'
            value = dict(run_id=run_id, operation=operation, state='queued', version=__version__, release=str(release),
                         boot_id=boot_id(self.paths), unit=unit, started_at=now(), finished_at=None, stages=[])
            atomic_json(directory / 'config.json', config.to_dict())
            (directory / 'output.log').touch(mode=0o600, exist_ok=False)
            self.save(value)
            command = ['systemd-run', f'--unit={unit}', '--service-type=exec', '--collect',
                       '--expand-environment=no', '--property=TimeoutStartSec=infinity',
                       f'--property=StandardOutput=append:{directory / "output.log"}',
                       f'--property=StandardError=append:{directory / "output.log"}',
                       str(python), '-I', '-u', str(release / 'entry.py'), '_worker', run_id]
            started = self.host.run(command, timeout=30)
            if started.returncode:
                value.update(state='failed', exit_code=1, finished_at=now(), reason=started.stderr.strip())
                self.save(value)
                raise RuntimeError(f'설치 unit 시작 실패: {started.stderr.strip()}')
            return value

def run_worker(run_id, paths=None, host=None, engine_factory=None):
    paths, host = paths or Paths(), host or Host()
    store = JobStore(paths, host)
    with change_lock(paths, blocking=True):
        raw = json.loads((store.directory(run_id) / 'result.json').read_text())
        if raw['state'] != 'queued' or raw['boot_id'] != boot_id(paths):
            raise RuntimeError('재실행할 수 없는 설치 기록입니다.')
        raw['state'] = 'running'
        store.save(raw)
        def stage(stage_id, state):
            raw['stages'].append(dict(id=stage_id, state=state, time=now()))
            store.save(raw)
            print(f'[{state}] {stage_id}', flush=True)
        try:
            operation = raw.get('operation', 'setup')
            if operation not in {'setup', 'prepare'}:
                raise ValueError('허용되지 않은 설치 작업입니다.')
            if engine_factory is None:
                if operation == 'prepare':
                    from .prepare import Prepare
                    engine_factory = Prepare
                else:
                    from .engine import Engine
                    engine_factory = Engine
            config = Config.load(store.directory(run_id) / 'config.json')
            report = engine_factory(config, host=host, paths=paths).apply(on_stage=stage)
            raw.update(state={0:'completed', 1:'failed', 2:'pending'}[report.exit_code],
                       exit_code=report.exit_code, report=report.to_dict())
        except Exception as exc:
            raw.update(state='failed', exit_code=1, reason=str(exc))
            print(f'실패: {exc}', flush=True)
        raw['finished_at'] = now()
        store.save(raw)
        return raw.get('exit_code', 1)
