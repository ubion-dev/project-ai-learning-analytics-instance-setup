"""Public release discovery and explicitly requested, isolated self-update."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tarfile
import tempfile
import os
from pathlib import Path, PurePosixPath
from urllib.request import Request, urlopen

from . import __version__

REPO = 'ubion-dev/project-ai-learning-analytics-instance-setup'
API = f'https://api.github.com/repos/{REPO}/releases?per_page=100'
TAG = re.compile(r'v(\d+)\.(\d+)\.(\d+)(?:-rc\.(\d+))?\Z')
MAX_ARCHIVE = 128 * 1024 * 1024


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


def check(prerelease=False):
    try:
        releases = json.loads(fetch(API, 4 * 1024 * 1024))
        if not isinstance(releases, list):
            raise ValueError('릴리스 목록 형식 오류')
        return select_release(releases, prerelease)
    except (OSError, ValueError, TypeError, AttributeError):
        return {'current': __version__, 'available': False, 'exit_code': 1,
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


def install_release(tag):
    version = tag_version(tag)
    if version_key(version) <= version_key(__version__):
        raise ValueError('같거나 이전 버전으로 갱신하지 않습니다.')
    # Root-private temporary files prevent unprivileged replacement before installation.
    with tempfile.TemporaryDirectory(prefix='ala-update-') as temporary:
        directory = Path(temporary)
        name = f'ala-{version}.tar.gz'
        base = f'https://github.com/{REPO}/releases/download/{tag}/'
        checksum = fetch(base + name + '.sha256', 4096).decode('ascii').strip()
        match = re.fullmatch(r'([0-9a-f]{64})  ' + re.escape(name), checksum)
        if not match:
            raise ValueError('잘못된 체크섬 파일입니다.')
        data = fetch(base + name, MAX_ARCHIVE, timeout=60)
        if hashlib.sha256(data).hexdigest() != match.group(1):
            raise ValueError('다운로드 SHA-256 불일치')
        archive = directory / name
        archive.write_bytes(data)
        bundle = unpack(archive, directory, version)
        # The existing installer owns the common lock and atomic current switch.
        subprocess.run(['bash', str(bundle / 'install.sh')], check=True,
                       env=dict(os.environ, ALA_UPDATE_ONLY='1'))


def submit(release, prerelease=False):
    result = check(prerelease)
    if result['exit_code'] or not result['available']:
        return result
    from .jobs import change_lock, JobStore
    from .system import Paths, Host
    with change_lock():
        if any(r['state'] in {'queued', 'running'} for r in JobStore(Paths(), Host()).list()):
            raise RuntimeError('설치 작업 완료 후 갱신하세요.')
        # Fixed unit name rejects concurrent update workers, including downloads.
        subprocess.run(['systemd-run', '--unit=ala-update', '--collect',
                        '--property=Type=exec', '--property=UMask=0077',
                        str(release / '.venv/bin/python'), '-I', str(release / 'entry.py'),
                        '_update-worker', result['tag']], check=True, capture_output=True)
    return dict(result, unit='ala-update.service', started=True,
                next_action='sudo journalctl -u ala-update -f 로 결과 확인 후 ala --version을 실행하세요.')
