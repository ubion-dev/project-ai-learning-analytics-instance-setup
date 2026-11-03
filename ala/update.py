"""Public release discovery and explicitly requested, isolated self-update."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tarfile
import tempfile
import os
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.request import Request, urlopen

from . import __version__
from .jobs import RUN_ID, JobStore, boot_id, change_lock, protected_directory
from .model import redact
from .system import Host, Paths, atomic_json

REPO = 'ubion-dev/project-ai-learning-analytics-instance-setup'
API = f'https://api.github.com/repos/{REPO}/releases?per_page=100'
TAG = re.compile(r'v(\d+)\.(\d+)\.(\d+)(?:-rc\.(\d+))?\Z')
MAX_ARCHIVE = 128 * 1024 * 1024
UNIT = 'ala-update.service'
ACTIVE = {'queued', 'running'}
STATES = ACTIVE | {'completed', 'failed', 'interrupted', 'unknown', 'idle'}
PHASES = {'queued', 'checking', 'downloading', 'checksum', 'manifest', 'installing',
          'rechecking', 'completed', 'failed'}


def now():
    return datetime.now(timezone.utc).isoformat()


def installed_version(paths=None):
    """Read the installed pointer, independently of this process's code version."""
    paths = paths or Paths()
    owner = 0 if paths.root == Path('/') else os.geteuid()
    current = paths.at('/opt/ala/current')
    if not current.exists() and not current.is_symlink():
        return None
    if not current.is_symlink():
        raise ValueError('설치 current가 심볼릭 링크가 아닙니다.')
    base = paths.at('/opt/ala/releases')
    for directory in (base, current.parent, *current.parent.parents):
        if directory == paths.root:
            break
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
            raise PermissionError('설치 디렉터리 소유자·권한을 확인하세요.')
    release = current.resolve(strict=True)
    if release.parent != base.resolve(strict=True):
        raise ValueError('설치 current가 releases 밖을 가리킵니다.')
    info = release.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
        raise PermissionError('설치 버전 디렉터리 소유자·권한을 확인하세요.')
    version = release / 'VERSION'
    info = version.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022 or info.st_size > 128:
        raise PermissionError('설치 버전 파일 소유자·권한을 확인하세요.')
    value = version.read_text().strip()
    version_key(value)
    if release.name != value:
        raise ValueError('설치 경로와 버전 파일이 일치하지 않습니다.')
    return value


class UpdateStore:
    """Root-owned update evidence, kept separate from environment setup runs."""
    def __init__(self, paths=None, host=None, release=None):
        self.paths = paths or Paths()
        self.host = host or Host()
        self.release = release or Path(__file__).resolve().parents[1]
        self.root = self.paths.at('/var/lib/ala/updates')
        self.owner = 0 if self.paths.root == Path('/') else os.geteuid()

    def _directory(self, run_id=None):
        if run_id is not None and (not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id)):
            raise ValueError('유효하지 않은 갱신 실행 ID입니다.')
        target = self.root / run_id if run_id is not None else self.root
        for directory in [target, *target.parents]:
            if directory == self.paths.root:
                break
            if not directory.exists() and not directory.is_symlink():
                continue
            info = directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != self.owner
                    or info.st_mode & 0o022):
                raise PermissionError('갱신 기록 디렉터리 소유자·권한을 확인하세요.')
        return target

    def load(self, run_id):
        path = self._directory(run_id) / 'state.json'
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 64 * 1024):
            raise PermissionError('갱신 기록 파일 소유자·권한을 확인하세요.')
        value = json.loads(path.read_text())
        self.validate(value, run_id)
        return value

    @staticmethod
    def validate(value, run_id):
        if (not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != 1
                or value.get('run_id') != run_id or value.get('unit') != UNIT
                or value.get('state') not in STATES - {'idle'} or value.get('phase') not in PHASES
                or not isinstance(value.get('boot_id'), str)
                or not isinstance(value.get('release'), str)):
            raise ValueError('갱신 기록 형식이 올바르지 않습니다.')
        for key in ('before_version', 'installed_version', 'target_version', 'runtime_version'):
            if value.get(key) is not None:
                version_key(value[key])
        if value.get('tag') is not None and tag_version(value['tag']) != value.get('target_version'):
            raise ValueError('갱신 대상 태그·버전이 일치하지 않습니다.')
        if value['state'] in ACTIVE | {'completed'} and (value.get('tag') is None or value.get('target_version') is None):
            raise ValueError('갱신 대상 정보가 없습니다.')
        for key in ('started_at', 'ended_at'):
            if value.get(key) is not None:
                stamp = datetime.fromisoformat(value[key])
                if stamp.tzinfo is None:
                    raise ValueError('갱신 시각에 시간대가 없습니다.')
        if value.get('started_at') is None:
            raise ValueError('갱신 시작 시각이 없습니다.')
        for key in ('reason', 'next_action'):
            if not isinstance(value.get(key), str):
                raise ValueError('갱신 안내 형식이 올바르지 않습니다.')

    def save(self, value):
        run_id = value['run_id']
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise ValueError('유효하지 않은 갱신 실행 ID입니다.')
        self.validate(value, run_id)
        directory = self._directory(run_id)
        protected_directory(self.root)
        protected_directory(directory)
        destination = directory / 'state.json'
        if destination.is_symlink():
            raise PermissionError('갱신 기록에 심볼릭 링크를 사용하지 않습니다.')
        atomic_json(destination, value)

    def latest(self):
        directory = self._directory()
        if not directory.exists():
            return None
        records = [self.load(path.name) for path in directory.iterdir() if RUN_ID.fullmatch(path.name)]
        return max(records, key=lambda value: (datetime.fromisoformat(value['started_at']), value['run_id']), default=None)


def _public_state(value=None):
    value = value or {}
    result = {name: value.get(name) for name in (
        'run_id', 'phase', 'target_version', 'before_version', 'installed_version',
        'reason', 'next_action', 'unit', 'started_at', 'ended_at')}
    result.update(state=value.get('state', 'idle'), runtime_version=__version__)
    result['exit_code'] = 2 if result['state'] in ACTIVE else 0 if result['state'] in {'idle', 'completed'} else 1
    return result


def status(paths=None, host=None, run_id=None):
    """Compute current update state without creating or changing any evidence."""
    store = UpdateStore(paths, host)
    result = _public_state()
    try:
        value = store.load(run_id) if run_id is not None else store.latest()
        result = _public_state(value)
        if value and value['state'] in ACTIVE:
            latest = store.latest() if run_id is not None else value
            if latest and latest['run_id'] != value['run_id']:
                # The fixed unit name belongs to a later attempt now. Its active
                # state must not resurrect an old, unfinalized record.
                result.update(state='interrupted', reason='이 기록 이후 새 갱신 시도가 있어 현재 서비스와 연결하지 않습니다.',
                              next_action='ala update --status로 최신 갱신 결과를 확인하세요.')
            elif value['boot_id'] != boot_id(store.paths):
                result.update(state='interrupted', reason='부팅이 바뀌어 갱신이 중단됐습니다.',
                              next_action='현재 설치 버전을 확인한 뒤 ala update를 다시 실행하세요.')
            else:
                command = store.host.run(['systemctl', 'show', UNIT, '--property=ActiveState', '--property=LoadState'])
                properties = dict(line.split('=', 1) for line in command.stdout.splitlines() if '=' in line)
                stopped = properties.get('LoadState') == 'not-found' or (
                    command.returncode == 0 and properties.get('ActiveState') in {'inactive', 'failed'})
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(value['started_at'])).total_seconds()
                if stopped and age > 15:
                    # The worker may have finished between the record and unit reads.
                    latest = store.load(value['run_id'])
                    if latest['state'] not in ACTIVE:
                        result = _public_state(latest)
                    else:
                        result.update(state='interrupted', reason='완료 기록 없이 갱신 서비스가 종료됐습니다.',
                                      next_action='현재 버전과 journalctl -u ala-update 로그를 확인하세요.')
                elif not stopped and (command.returncode or not properties.get('ActiveState')):
                    result.update(state='unknown', reason='갱신 서비스 상태를 조회하지 못했습니다.',
                                  next_action='권한과 systemd 상태를 확인한 뒤 다시 조회하세요.')
        result['installed_version'] = installed_version(store.paths)
        if result['state'] == 'completed' and result['installed_version'] != result['target_version']:
            result.update(state='unknown', reason='완료 기록의 대상과 현재 설치 버전이 다릅니다.',
                          next_action='현재 설치 버전과 최근 갱신 기록을 확인하세요.')
        result['exit_code'] = 2 if result['state'] in ACTIVE else 0 if result['state'] in {'idle', 'completed'} else 1
        if not value:
            result.update(reason='기록된 갱신 작업이 없습니다.', next_action='ala update --check로 새 버전을 확인하세요.')
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result.update(state='unknown', reason='갱신 상태 확인 실패: ' + str(redact(str(exc))),
                      next_action='설치 경로·기록 소유자·권한을 확인한 뒤 다시 조회하세요.',
                      installed_version=None, exit_code=1)
    return result


def version_key(value):
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:rc(\d+))?', value)
    if not match:
        raise ValueError('지원하지 않는 버전 형식입니다.')
    a, b, c, rc = match.groups()
    return int(a), int(b), int(c), rc is None, int(rc or 0)


def tag_version(tag):
    match = TAG.fullmatch(tag)
    if not match:
        raise ValueError('지원하지 않는 릴리스 태그입니다.')
    a, b, c, rc = match.groups()
    return f'{a}.{b}.{c}' + (f'rc{rc}' if rc else '')


def fetch(url, limit, timeout=5):
    # No credentials, environment tokens, or GitHub login are used.
    request = Request(url, headers={'User-Agent': 'ala-update', 'Accept': 'application/vnd.github+json'})
    with urlopen(request, timeout=timeout) as response:
        if not response.url.startswith('https://'):
            raise ValueError('HTTPS 다운로드만 허용합니다.')
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('릴리스 응답/파일 크기 제한을 초과했습니다.')
    return data


def select_release(releases, prerelease=False, current=__version__):
    candidates = []
    for release in releases:
        if release.get('draft'):
            continue
        tag = release.get('tag_name', '')
        if not TAG.fullmatch(tag):
            continue
        version = tag_version(tag)
        is_rc = 'rc' in version
        if (release.get('prerelease') or is_rc) and not prerelease:
            continue
        filename = f'ala-{version}.tar.gz'
        names = [asset.get('name') for asset in release.get('assets', []) if asset.get('state') == 'uploaded']
        if names.count(filename) != 1 or names.count(filename + '.sha256') != 1:
            continue
        candidates.append((version_key(version), tag, version))
    latest = max(candidates, default=None)
    available = bool(latest and latest[0] > version_key(current))
    return {'current': current, 'latest': latest[2] if latest else None,
            'tag': latest[1] if latest else None, 'available': available,
            'channel': 'prerelease' if prerelease else 'stable', 'exit_code': 0}


def check(prerelease=False, *, paths=None):
    installed = None
    try:
        installed = installed_version(paths)
    except (OSError, ValueError, TypeError):
        return {'current': __version__, 'runtime_version': __version__, 'installed_version': None,
                'available': False, 'exit_code': 1,
                'error': '현재 설치 버전을 확인하지 못했습니다. 설치 경로·소유자·권한을 확인하세요.'}
    try:
        releases = json.loads(fetch(API, 4 * 1024 * 1024))
        if not isinstance(releases, list):
            raise ValueError('릴리스 목록 형식 오류')
        result = select_release(releases, prerelease, installed or __version__)
        return dict(result, current=__version__, runtime_version=__version__, installed_version=installed)
    except (OSError, ValueError, TypeError, AttributeError):
        return {'current': __version__, 'runtime_version': __version__, 'installed_version': installed,
                'available': False, 'exit_code': 1,
                'error': '새 버전을 확인하지 못했습니다. 네트워크 또는 GitHub API 제한을 확인하세요.'}


def unpack(archive, destination, version):
    root = f'ala-{version}'
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        if len(members) > 4096 or sum(m.size for m in members) > 512 * 1024 * 1024:
            raise ValueError('압축 해제 크기 제한을 초과했습니다.')
        names = set()
        for member in members:
            p = PurePosixPath(member.name)
            if (not member.isfile() or p.is_absolute() or '..' in p.parts or '\\' in member.name
                    or str(p) != member.name or len(p.parts) < 2 or p.parts[0] != root
                    or member.name in names):
                raise ValueError('안전하지 않은 배포 파일 경로/유형입니다.')
            names.add(member.name)
        for member in members:
            path = destination / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as source, path.open('xb') as target:
                target.write(source.read())
    bundle = destination / root
    manifest = json.loads((bundle / 'MANIFEST.json').read_text())
    if manifest.get('version') != version or manifest.get('schema_version') != 1:
        raise ValueError('manifest 버전 불일치')
    actual = {str(p.relative_to(bundle)) for p in bundle.rglob('*') if p.is_file()}
    files = manifest['files']
    if actual != set(files) | {'MANIFEST.json'} or not {'install.sh', 'VERSION'} <= set(files):
        raise ValueError('manifest 파일 목록 불일치')
    for name, digest in files.items():
        if hashlib.sha256((bundle / name).read_bytes()).hexdigest() != digest:
            raise ValueError('manifest 파일 해시 불일치')
    if (bundle / 'VERSION').read_text().strip() != version:
        raise ValueError('배포 버전 불일치')
    return bundle


def install_release(tag, *, on_phase=None):
    def phase(name):
        if on_phase:
            on_phase(name)

    version = tag_version(tag)
    if version_key(version) <= version_key(__version__):
        raise ValueError('같거나 이전 버전으로 갱신하지 않습니다.')
    # Root-private temporary files prevent unprivileged replacement before installation.
    with tempfile.TemporaryDirectory(prefix='ala-update-') as temporary:
        directory = Path(temporary)
        name = f'ala-{version}.tar.gz'
        base = f'https://github.com/{REPO}/releases/download/{tag}/'
        phase('downloading')
        checksum = fetch(base + name + '.sha256', 4096).decode('ascii').strip()
        match = re.fullmatch(r'([0-9a-f]{64})  ' + re.escape(name), checksum)
        if not match:
            raise ValueError('잘못된 체크섬 파일입니다.')
        data = fetch(base + name, MAX_ARCHIVE, timeout=60)
        phase('checksum')
        if hashlib.sha256(data).hexdigest() != match.group(1):
            raise ValueError('다운로드 SHA-256 불일치')
        archive = directory / name
        archive.write_bytes(data)
        phase('manifest')
        bundle = unpack(archive, directory, version)
        # The existing installer owns the common lock and atomic current switch.
        phase('installing')
        subprocess.run(['bash', str(bundle / 'install.sh')], check=True,
                       env=dict(os.environ, ALA_UPDATE_ONLY='1'))


def submit(release, prerelease=False, paths=None, host=None):
    store = UpdateStore(paths, host, release)
    result = check(prerelease, paths=store.paths)
    if not result['exit_code'] and not result['available']:
        return dict(result, state='idle', started=False,
                    reason='선택한 채널에 더 새로운 설치 가능한 릴리스가 없습니다.',
                    next_action='현재 설치 버전을 계속 사용합니다.')
    with change_lock(store.paths):
        if any(r['state'] in ACTIVE for r in JobStore(store.paths, store.host).list()):
            raise RuntimeError('설치 작업 완료 후 갱신하세요.')
        previous = store.latest()
        if previous and previous['state'] in ACTIVE:
            existing = status(store.paths, store.host, previous['run_id'])
            if existing['state'] in ACTIVE | {'unknown'}:
                raise RuntimeError('갱신 작업이 이미 진행 중이거나 실행 여부를 확인할 수 없습니다. ala update --status로 확인하세요.')
        before = installed_version(store.paths)
        run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]
        value = dict(schema_version=1, run_id=run_id, unit=UNIT, release=str(Path(release).resolve()),
                     boot_id=boot_id(store.paths), state='queued', phase='queued',
                     tag=result.get('tag'), target_version=result.get('latest'),
                     before_version=before, installed_version=before, runtime_version=__version__,
                     started_at=now(), ended_at=None, reason='갱신 서비스를 시작하고 있습니다.',
                     next_action='ala update --status로 진행 상태를 확인하세요.')
        if result['exit_code']:
            value.update(state='failed', phase='checking', ended_at=now(),
                         reason=result['error'] + (' 기존 설치 버전을 유지했습니다.' if before else ''),
                         next_action='네트워크 또는 GitHub 상태를 확인한 뒤 다시 실행하세요.')
            store.save(value)
            response = dict(result)
            # `error` is reserved for failed RPC envelopes, not recorded outcomes.
            response.pop('error', None)
            return dict(response, **_public_state(value), started=False)
        store.save(value)
        # Fixed unit name rejects concurrent update workers, including downloads.
        try:
            pinned = Path(release).resolve()
            launched = store.host.run(['systemd-run', '--unit=ala-update', '--collect',
                                      '--property=Type=exec', '--property=UMask=0077',
                                      str(pinned / '.venv/bin/python'), '-I', str(pinned / 'entry.py'),
                                      '_update-worker', run_id])
            if launched.returncode == 124:
                # A timed-out client does not prove that systemd rejected the unit.
                # Do not overwrite evidence a detached worker may already own.
                uncertain = _public_state(store.load(run_id))
                uncertain.update(state='unknown', exit_code=1,
                                 reason='서비스 시작 응답이 시간 초과되었습니다. 실제 갱신은 계속될 수 있습니다.',
                                 next_action='ala update --status --run ' + run_id + ' 로 확인하세요.')
                return dict(result, **uncertain, started=False)
            if launched.returncode:
                raise RuntimeError(f'갱신 서비스 시작 실패 (종료 {launched.returncode}).')
        except (OSError, RuntimeError) as exc:
            latest = store.load(run_id)
            if latest['state'] != 'queued':
                return dict(result, **_public_state(latest), started=True)
            value.update(state='failed', phase='queued', ended_at=now(), reason=str(redact(str(exc))),
                         next_action='현재 설치 버전과 갱신 서비스를 확인한 뒤 다시 실행하세요.')
            store.save(value)
            return dict(result, **_public_state(value), started=False)
    return dict(result, **_public_state(value), started=True)


def run_worker(run_id, paths=None, host=None, release=None):
    store = UpdateStore(paths, host, release)
    # Claim once, then release the lock before calling the installer which owns it.
    with change_lock(store.paths, blocking=True):
        value = store.load(run_id)
        if value['state'] != 'queued' or value['boot_id'] != boot_id(store.paths):
            raise RuntimeError('재실행할 수 없는 갱신 기록입니다.')
        value.update(state='running', phase='checking', reason='새 ala 버전을 준비하고 있습니다.')
        store.save(value)

    def phase(name):
        value.update(phase=name)
        store.save(value)

    try:
        before = installed_version(store.paths)
        value.update(before_version=before, installed_version=before)
        if before is not None and version_key(value['target_version']) <= version_key(before):
            raise ValueError('이미 같거나 더 새로운 버전이 설치되어 있습니다.')
        phase('checking')
        install_release(value['tag'], on_phase=phase)
        phase('rechecking')
        after = installed_version(store.paths)
        value['installed_version'] = after
        if after != value['target_version']:
            raise ValueError('설치된 current 버전이 갱신 대상과 일치하지 않습니다.')
        launcher = store.paths.at('/usr/local/bin/ala')
        info = launcher.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != store.owner or info.st_mode & 0o022):
            raise PermissionError('설치 진입점 소유자·권한을 확인하세요.')
        current = store.paths.at('/opt/ala/current')
        expected = f'#!/usr/bin/env bash\n# ala managed launcher v1\nset -e\nexec "{current}/.venv/bin/python" -I "{current}/entry.py" "$@"\n'
        if launcher.read_text() != expected:
            raise ValueError('고정 ala 진입점이 현재 설치 경로를 실행하지 않습니다.')
        actual = store.host.run([str(current / '.venv/bin/python'), '-I', '-B', str(current / 'entry.py'), '--version'])
        if actual.returncode or actual.stdout.strip() != value['target_version']:
            raise ValueError('고정 ala 진입점의 실제 실행 버전 검증에 실패했습니다.')
        value.update(state='completed', phase='completed', ended_at=now(), reason='ala 갱신을 완료했습니다.',
                     next_action='ala를 다시 실행하여 새 버전을 사용하세요.')
        store.save(value)
        return 0
    except Exception as exc:
        try:
            after = installed_version(store.paths)
            value['installed_version'] = after
            if after == value['before_version'] and after is not None:
                preserved = f'기존 설치 버전 {after}을 유지했습니다.'
            elif after is not None:
                preserved = f'현재 설치 버전은 {after}입니다. 기존 버전 유지로 판단하지 않습니다.'
            else:
                preserved = '설치 버전이 없어 기존 버전 유지 여부를 확인할 수 없습니다.'
        except (OSError, ValueError, TypeError):
            value['installed_version'] = None
            preserved = '현재 설치 버전과 기존 버전 유지 여부를 확인할 수 없습니다.'
        value.update(state='failed', ended_at=now(), reason=str(redact(str(exc))) + ' ' + preserved,
                     next_action='실패 원인과 현재 설치 버전을 확인한 뒤 다시 실행하세요.')
        store.save(value)
        return 1
