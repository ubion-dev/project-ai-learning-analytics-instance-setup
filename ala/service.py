from __future__ import annotations

from pathlib import Path
import asyncio
import json
import os
import pwd
import subprocess
import sys
from time import perf_counter

from .config import Config
from .jobs import JobStore, change_lock, protected_directory, trusted_release
from .system import Host, InspectionHost, Paths, atomic_json, platform_error

class Operations:
    """Fixed privileged API. Tests inject paths/host, never environment overrides."""
    def __init__(self, paths=None, host=None, release=None):
        self.paths, self.host = paths or Paths(), host or Host()
        self.release = release or Path(__file__).resolve().parents[1]

    def config(self):
        if self.paths.config.is_symlink():
            raise PermissionError('구성 파일에 심볼릭 링크를 사용하지 않습니다.')
        if not self.paths.config.exists():
            user = os.environ.get('SUDO_USER', '')
            return Config(user=user if user != 'root' else '', autostart=False)
        info = self.paths.config.lstat()
        if self.paths.config.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise PermissionError('구성 파일 소유자/쓰기 권한을 확인하세요.')
        return Config.load(self.paths.config)

    def dispatch(self, action, payload):
        if action == 'update-submit':
            from .update import submit
            if type(payload.get('prerelease', False)) is not bool:
                raise ValueError('시험판 선택은 boolean이어야 합니다.')
            return submit(self.release, payload.get('prerelease', False), paths=self.paths, host=self.host)
        if action == 'update-status':
            if payload.get('run_id') is not None and not isinstance(payload['run_id'], str):
                raise ValueError('업데이트 실행 ID는 문자열이어야 합니다.')
            from .update import status
            return status(paths=self.paths, host=self.host, run_id=payload.get('run_id'))
        if action == 'config-show':
            return {'config': self.config().to_dict(), 'exists': self.paths.config.exists()}
        if action == 'config-save':
            config = Config.from_dict(payload)
            if config.user:
                record = pwd.getpwnam(config.user)
                if record.pw_uid == 0 or not Path(record.pw_dir).is_dir():
                    raise ValueError('존재하는 일반 사용자와 홈이 필요합니다.')
            with change_lock(self.paths):
                if any(r['state'] in {'queued', 'running'} for r in JobStore(self.paths, self.host).list()):
                    raise RuntimeError('설치 작업 중에는 구성을 변경할 수 없습니다.')
                protected_directory(self.paths.config.parent, 0o755)
                if not self.paths.config.exists() or self.config() != config or self.paths.config.stat().st_mode & 0o777 != 0o644:
                    atomic_json(self.paths.config, config.to_dict(), 0o644)
            return {'config': config.to_dict(), 'saved': True}
        if action == 'doctor-network':
            from .network import inspect_network
            return inspect_network(host=self.host, paths=self.paths, probe_url=payload.get('probe_url')).to_dict()
        if action == 'prepare-plan':
            from .prepare import Prepare
            return Prepare(self.config(), host=self.host, paths=self.paths).plan().to_dict()
        if action == 'prepare-submit':
            return JobStore(self.paths, self.host).submit(self.config, self.release, self.release / '.venv/bin/python',
                expected_config=payload.get('expected_config'), operation='prepare')
        if action in {'inspect', 'plan', 'status'}:
            from .engine import Engine
            started = perf_counter()
            config = self.config()
            # Scope query sharing to this read-only response. Workers and all
            # mutation/preparation paths continue using the original Host.
            host = InspectionHost(self.host)
            engine = Engine(config, host=host, paths=self.paths)
            report = (engine.plan(workers=4) if action == 'plan' else engine.inspect(workers=4)).to_dict()
            inspection = engine.inspection
            if action == 'status':
                import socket
                from .prepare import Prepare
                report.update(hostname=socket.gethostname(), config=config.to_dict(), config_exists=self.paths.config.exists())
                for name, query in (('runs', lambda: JobStore(self.paths, host).list()),
                                    ('preparation', lambda: Prepare(config, host=host, paths=self.paths).inspect().to_dict())):
                    since = perf_counter()
                    report[name] = query()
                    inspection['stages'].append({'id': name, 'duration_ms': round(max(0.0, (perf_counter() - since) * 1000), 3)})
            inspection['duration_ms'] = round(max(0.0, (perf_counter() - started) * 1000), 3)
            report['inspection'] = inspection
            return report
        if action == 'submit':
            def current_config():
                config = self.config()
                if not config.user:
                    raise ValueError('기존 일반 사용자로 SSH 접속한 뒤 ala를 실행하세요. root로 직접 접속했다면 먼저 화면 계정을 지정하세요.')
                record = pwd.getpwnam(config.user)
                if record.pw_uid == 0 or not Path(record.pw_dir).is_dir() or record.pw_shell.endswith(('nologin','false')):
                    raise ValueError('사용 가능한 일반 사용자가 아닙니다.')
                return config
            return JobStore(self.paths, self.host).submit(current_config, self.release, self.release / '.venv/bin/python',
                                                         expected_config=payload.get('expected_config'), persist_config=True)
        if action == 'runs-list':
            return {'runs': JobStore(self.paths, self.host).list()}
        if action == 'runs-show':
            return JobStore(self.paths, self.host).load(payload['run_id'])
        if action == 'logs':
            store = JobStore(self.paths, self.host)
            directory = store.directory(payload['run_id'])
            # Read a bounded tail; do not load an unbounded compiler/package log into TUI memory.
            with (directory / 'output.log').open('rb') as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - 128 * 1024))
                content = stream.read().decode(errors='replace')
            return {'run_id': payload['run_id'], 'log': content}
        if action == 'chrome':
            from .display import chrome_action
            if payload.get('action') not in {'start', 'stop', 'restart', 'status', 'logs', 'check'}:
                raise ValueError('허용되지 않은 Chrome 작업입니다.')
            if payload['action'] in {'start','stop','restart'}:
                with change_lock(self.paths):
                    if any(r['state'] in {'queued','running'} for r in JobStore(self.paths, self.host).list()):
                        raise RuntimeError('설치 중에는 Chrome 변경 작업을 하지 않습니다.')
                    return chrome_action(payload['action'], self.config(), host=self.host, paths=self.paths)
            return chrome_action(payload['action'], self.config(), host=self.host, paths=self.paths)
        raise ValueError('허용되지 않은 관리자 작업입니다.')

class Service:
    """CLI and TUI use this exact API; no localized log parsing."""
    def __init__(self, release=None):
        self.release = release or Path(__file__).resolve().parents[1]

    def enter_admin(self, argv):
        """Hand the real terminal to sudo before opening the management UI.

        Noninteractive callers retain the fixed sudo -n RPC, so JSON pipelines
        never wait for a password. Private graphical-session entrypoints bypass
        this public CLI path entirely.
        """
        if os.geteuid() == 0 or not (sys.stdin.isatty() and sys.stdout.isatty()):
            return
        release = trusted_release(self.release)
        command = ['sudo', '--', str(release / '.venv/bin/python'), '-I',
                   str(release / 'entry.py'), *argv]
        try:
            os.execvp('sudo', command)
        except FileNotFoundError:
            raise RuntimeError('sudo 명령을 찾을 수 없습니다. sudo가 설치된 일반 관리자 계정으로 실행하세요.') from None

    def authenticate(self):
        if os.geteuid() != 0:
            if not (sys.stdin.isatty() and sys.stdout.isatty()):
                result = subprocess.run(['sudo', '-n', '-v'], check=False)
            else:
                result = subprocess.run(['sudo', '-v'], check=False)
            if result.returncode:
                raise PermissionError('sudo 인증이 필요합니다.')

    def command(self, action):
        release = trusted_release(self.release)
        args = [str(release / '.venv/bin/python'), '-I', str(release / 'entry.py'), '_privileged', action]
        if os.geteuid() != 0:
            args = ['sudo', '-n', '--', *args]
        return args

    @staticmethod
    def decode(returncode, stdout, stderr):
        try:
            value = json.loads(stdout)
        except ValueError:
            raise RuntimeError(stderr.strip() or '관리자 응답을 읽을 수 없습니다.') from None
        if not isinstance(value, dict):
            raise RuntimeError('잘못된 관리자 응답입니다.')
        if returncode or 'error' in value:
            raise RuntimeError(value.get('error', stderr.strip() or '관리자 작업 실패'))
        return value

    def request(self, action: str, payload=None):
        try:
            result = subprocess.run(self.command(action), input=json.dumps(payload or {}), capture_output=True,
                                    text=True, check=False, timeout=180)
        except subprocess.TimeoutExpired:
            raise RuntimeError('관리자 조회 제한 시간 초과. 시작한 작업은 ala runs list로 확인하세요.') from None
        return self.decode(result.returncode, result.stdout, result.stderr)

    async def call(self, action: str, payload=None):
        process = await asyncio.create_subprocess_exec(*self.command(action), stdin=asyncio.subprocess.PIPE,
                                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps(payload or {}).encode()), timeout=180)
            return self.decode(process.returncode, stdout.decode(), stderr.decode())
        except (asyncio.CancelledError, asyncio.TimeoutError) as exc:
            # This is only the RPC client, never the detached installation unit.
            if process.returncode is None:
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=2)
                except (ProcessLookupError, asyncio.TimeoutError):
                    if process.returncode is None:
                        process.kill()
            if isinstance(exc, asyncio.TimeoutError):
                raise RuntimeError('관리자 조회 제한 시간 초과. 진행 중 설치는 실행 기록으로 확인하세요.') from None
            raise

def privileged(action):
    if os.geteuid() != 0:
        raise PermissionError('관리자 실행기는 root 전용입니다.')
    release = trusted_release(Path(__file__).resolve().parents[1])
    error = platform_error()
    if error:
        raise RuntimeError(error)
    raw = sys.stdin.buffer.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError('요청이 너무 큽니다.')
    payload = json.loads(raw or b'{}')
    if not isinstance(payload, dict):
        raise ValueError('요청은 object여야 합니다.')
    return Operations(release=release).dispatch(action, payload)
