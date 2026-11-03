"""Pinned external Sigstore verifier; no native dependencies in ala's venv.

The initial manual installer is the trust bootstrap. It pins the independent
verifier's bytes; subsequent automatic root execution additionally requires a
valid bundle issued to this repository's tag-only release workflow.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
import urllib.request

from .system import Host, Paths

COSIGN_VERSION = '3.1.4'
COSIGN_SHA256 = '424e2c97d0b45fd7a6b608a87f090f35633f4c23ece5f092d48bba11aaf4bbfa'
COSIGN_URL = f'https://github.com/sigstore/cosign/releases/download/v{COSIGN_VERSION}/cosign-linux-amd64'
MAX_VERIFIER = 160 * 1024 * 1024
REPOSITORY = 'ubion-dev/project-ai-learning-analytics-instance-setup'
WORKFLOW = '.github/workflows/release.yml'
ISSUER = 'https://token.actions.githubusercontent.com'


def verifier_path(paths=None):
    return (paths or Paths()).at('/opt/ala-verifier') / ('cosign-' + COSIGN_VERSION)


def ensure_verifier(paths=None):
    paths = paths or Paths()
    path = verifier_path(paths)
    owner = 0 if paths.root == Path('/') else os.geteuid()
    # In test roots only inspect ancestors through that root, not a shared /tmp.
    for item in (path, *path.parents):
        if item.is_symlink():
            raise PermissionError('서명 검증기 경로에 심볼릭 링크를 허용하지 않습니다.')
        if item.exists() and (item.stat().st_uid != owner or item.stat().st_mode & 0o022):
            raise PermissionError('서명 검증기 소유자·쓰기 권한을 확인하세요.')
        if item == paths.root:
            break
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111 or info.st_size > MAX_VERIFIER:
        raise PermissionError('고정된 독립 서명 검증기가 필요합니다.')
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest != COSIGN_SHA256:
        raise ValueError('독립 서명 검증기의 고정 해시가 다릅니다.')
    return path


def bootstrap(*, paths=None, opener=None):
    """Explicit bootstrap download: never execute bytes before their pinned hash."""
    paths = paths or Paths()
    owner = 0 if paths.root == Path('/') else os.geteuid()
    if os.geteuid() != owner:
        raise PermissionError('서명 검증기 최초 준비는 관리자 작업입니다.')
    path = verifier_path(paths)
    if path.exists() or path.is_symlink():
        ensure_verifier(paths)
        return {'state': 'kept', 'version': COSIGN_VERSION}
    parent = path.parent
    for item in (parent, *parent.parents):
        if item.is_symlink() or (item.exists() and (item.stat().st_uid != owner or item.stat().st_mode & 0o022)):
            raise PermissionError('검증기 설치 경로 소유자·쓰기 권한을 확인하세요.')
        if item == paths.root:
            break
    parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    fd, name = tempfile.mkstemp(prefix='.cosign-', dir=parent)
    try:
        digest, size = hashlib.sha256(), 0
        request = urllib.request.Request(COSIGN_URL, headers={'User-Agent': 'ala-verifier-bootstrap/1'})
        with os.fdopen(fd, 'wb') as stream, (opener or urllib.request.urlopen)(request, timeout=60) as response:
            if response.status != 200:
                raise ValueError('검증기 공식 다운로드 응답이 올바르지 않습니다.')
            while data := response.read(1024 * 1024):
                size += len(data)
                if size > MAX_VERIFIER:
                    raise ValueError('검증기 다운로드 크기 제한을 초과했습니다.')
                digest.update(data)
                stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if digest.hexdigest() != COSIGN_SHA256:
            raise ValueError('검증기 공식 다운로드의 고정 해시가 다릅니다.')
        os.chmod(name, 0o755)
        os.replace(name, path)
        ensure_verifier(paths)
        return {'state': 'completed', 'version': COSIGN_VERSION}
    finally:
        if os.path.exists(name):
            os.unlink(name)


def verify_blob(artifact, bundle, tag, *, paths=None, host=None):
    if not isinstance(tag, str) or not re.fullmatch(r'v\d+\.\d+\.\d+(?:-rc\.\d+)?', tag):
        raise ValueError('서명 검증 대상 release tag가 올바르지 않습니다.')
    paths, host = paths or Paths(), host or Host()
    verifier = ensure_verifier(paths)
    artifact, bundle = Path(artifact), Path(bundle)
    for item in (artifact, bundle):
        if item.is_symlink() or not item.is_file():
            raise ValueError('서명 검증 대상은 일반 파일이어야 합니다.')
    cache = paths.at('/var/lib/ala/verifier-cache')
    owner = 0 if paths.root == Path('/') else os.geteuid()
    for item in (cache, *cache.parents):
        if item.is_symlink() or (item.exists() and (item.stat().st_uid != owner or item.stat().st_mode & 0o022)):
            raise PermissionError('검증기 캐시 경로가 올바르지 않습니다.')
        if item == paths.root:
            break
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    if cache.stat().st_mode & 0o077:
        raise PermissionError('검증기 캐시는 관리자 전용이어야 합니다.')
    # Do not inherit SIGSTORE_ROOT_FILE, credential providers, or a user's HOME.
    environment = {'PATH': '/usr/bin:/bin', 'HOME': str(cache), 'LANG': 'C.UTF-8'}
    identity = f'https://github.com/{REPOSITORY}/{WORKFLOW}@refs/tags/{tag}'
    command = [str(verifier), 'verify-blob', str(artifact), '--bundle', str(bundle),
               '--certificate-identity', identity, '--certificate-oidc-issuer', ISSUER,
               '--certificate-github-workflow-repository', REPOSITORY,
               '--certificate-github-workflow-ref', 'refs/tags/' + tag,
               '--certificate-github-workflow-trigger', 'push']
    result = host.run(command, timeout=60, env=environment)
    if result.returncode:
        # A verifier's raw diagnostics may include certificate identities/URLs.
        raise ValueError('고정 release workflow의 Sigstore 서명 검증에 실패했습니다.')
    return {'state': 'verified', 'verifier_version': COSIGN_VERSION, 'tag': tag}


def verify_installed_release(*, paths=None, host=None):
    """Close the manual-install enrollment gap without trusting an operator flag."""
    paths, host = paths or Paths(), host or Host()
    from .update import installed_version, fetch, unpack, MAX_ARCHIVE
    version = installed_version(paths)
    if version is None:
        raise ValueError('서명과 대조할 설치된 ala 버전이 없습니다.')
    tag = 'v' + re.sub(r'rc(\d+)$', r'-rc.\1', version)
    name = f'ala-{version}.tar.gz'
    base = f'https://github.com/{REPOSITORY}/releases/download/{tag}/'
    installed = paths.at('/opt/ala/current').resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='ala-enrollment-') as directory:
        directory = Path(directory)
        archive = directory / name
        data = fetch(base + name, MAX_ARCHIVE, timeout=60)
        archive.write_bytes(data)
        bundle = directory / (name + '.sigstore.json')
        bundle.write_bytes(fetch(base + name + '.sigstore.json', 1024 * 1024))
        verified = verify_blob(archive, bundle, tag, paths=paths, host=host)
        source = unpack(archive, directory / 'verified', version)
        manifest = json_load(source / 'MANIFEST.json')
        if json_load(installed / 'MANIFEST.json') != manifest:
            raise ValueError('현재 설치 내용과 공식 서명 배포 manifest가 다릅니다.')
        owner = 0 if paths.root == Path('/') else os.geteuid()
        for relative, checksum in manifest['files'].items():
            item = installed / relative
            if (item.is_symlink() or not item.is_file() or item.stat().st_uid != owner
                    or item.stat().st_mode & 0o022):
                raise PermissionError('설치 payload 소유자·쓰기 권한을 확인하세요.')
            with item.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != checksum:
                    raise ValueError('설치 payload가 공식 서명 배포와 다릅니다.')
        if paths.at('/opt/ala/current').resolve(strict=True) != installed:
            raise RuntimeError('서명 대조 중 CLI 선택이 변경됐습니다. 다시 준비하세요.')
        record_verified(verified | {'version': version, 'sha256': hashlib.sha256(data).hexdigest()}, paths=paths)
    return verified | {'version': version}


def record_verified(value, *, paths=None):
    from .jobs import protected_directory
    from .system import atomic_json
    paths = paths or Paths()
    version = value.get('version')
    if (value.get('state') != 'verified' or not isinstance(version, str)
            or not re.fullmatch(r'\d+\.\d+\.\d+(?:rc\d+)?', version)):
        raise ValueError('실제 서명 검증 결과가 필요합니다.')
    directory = paths.at('/var/lib/ala/trust')
    protected_directory(directory)
    protected_directory(directory / 'releases')
    atomic_json(directory / 'releases' / (version + '.json'), value, 0o600)
    atomic_json(directory / 'last-verified.json', value, 0o600)


def verified_release(version, *, paths=None):
    """Only root-owned receipts produced by verify_blob authorize auto trials."""
    paths = paths or Paths()
    if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+(?:rc\d+)?', version):
        return False
    path = paths.at('/var/lib/ala/trust/releases') / (version + '.json')
    owner = 0 if paths.root == Path('/') else os.geteuid()
    for item in (path, *path.parents):
        if item.is_symlink() or (item.exists() and (item.stat().st_uid != owner or item.stat().st_mode & 0o022)):
            return False
        if item == paths.root:
            break
    if not path.is_file() or path.stat().st_size > 65536:
        return False
    value = json_load(path)
    return value.get('state') == 'verified' and value.get('version') == version and value.get('verifier_version') == COSIGN_VERSION


def json_load(path):
    import json
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('서명 대조 manifest가 올바르지 않습니다.')
    return json.loads(path.read_text())
