#!/usr/bin/env bash
# ala deployment bundle installer. Runtime system configuration is a separate ala setup action.
set -Eeuo pipefail
if (($#)); then echo '사용법: sudo bash ./install.sh (배포 묶음 내부에서 실행)' >&2; exit 64; fi
if [[ $EUID != 0 ]]; then echo 'sudo bash ./install.sh로 실행하세요.' >&2; exit 1; fi
OS_RELEASE=/etc/os-release
[[ -r $OS_RELEASE ]] || { echo 'Rocky Linux 10이 필요합니다.' >&2; exit 1; }
os_id=$(awk -F= '$1 == "ID" {gsub(/"/, "", $2); print $2}' "$OS_RELEASE")
os_version=$(awk -F= '$1 == "VERSION_ID" {gsub(/"/, "", $2); print $2}' "$OS_RELEASE")
[[ $os_id == rocky && ${os_version%%.*} == 10 && $(uname -m) == x86_64 ]] || {
  echo 'Rocky Linux 10 x86_64 전용입니다.' >&2; exit 1;
}
if ! command -v python3 >/dev/null; then
  # Do not source the bundle's helpers before its manifest is verified.
  if dnf install -y --setopt=install_weak_deps=False \
    --exclude='kernel,kernel-core,kernel-debug*,kernel-uki*,rocky-release*,rocky-repos*,rocky-gpg-keys*' python3; then
    :
  else
    status=$?
    printf 'Python 설치 실패: 부팅 커널과 Rocky release 변경은 허용하지 않습니다.\n' >&2
    printf '실행 커널 %s의 저장소/의존성을 확인하고, OS 변경은 별도 명시적 유지보수 작업으로 진행하세요.\n' "$(uname -r)" >&2
    exit "$status"
  fi
fi
python3 -I -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)' || {
  echo '시스템 python3는 3.12 이상이어야 합니다.' >&2; exit 1;
}
bundle=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec python3 -I - "$bundle" <<'PY'
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path("/")
SOURCE = Path(sys.argv[1])
OWNER = os.geteuid()
os.umask(0o022)

def fail(message):
    raise RuntimeError(message)

def dnf_install(*packages):
    # Keep this bootstrap policy in sync with scripts/rocky-linux/common.sh.
    result = subprocess.run(['dnf', 'install', '-y', '--setopt=install_weak_deps=False',
                             '--exclude=kernel,kernel-core,kernel-debug*,kernel-uki*,rocky-release*,rocky-repos*,rocky-gpg-keys*',
                             *packages])
    if result.returncode:
        fail(f'패키지 설치 실패: 부팅 커널과 Rocky release 변경은 허용하지 않습니다. '
             f'실행 커널 {os.uname().release}의 저장소/의존성을 확인하고, '
             'OS 변경은 별도 명시적 유지보수 작업으로 진행하세요. 자동 업그레이드·삭제로 우회하지 않습니다.')

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def secure_directory(path, mode=0o755):
    if path == ROOT:
        return
    secure_directory(path.parent)
    if path.is_symlink():
        fail(f'심볼릭 링크 설치 경로를 사용하지 않습니다: {path}')
    path.mkdir(exist_ok=True, mode=mode)
    value = path.stat()
    sticky_lock_parent = path == ROOT / 'run/lock' and bool(value.st_mode & stat.S_ISVTX)
    if not stat.S_ISDIR(value.st_mode) or value.st_uid != OWNER or (value.st_mode & 0o022 and not sticky_lock_parent):
        fail(f'설치 디렉터리 소유자/쓰기 권한을 확인하세요: {path}')

def verify(directory, manifest, *, installed=False):
    if manifest.get('schema_version') != 1 or not isinstance(manifest.get('files'), dict):
        fail('잘못된 배포 manifest입니다.')
    for name, checksum in manifest['files'].items():
        relative = PurePosixPath(name)
        if (not name or relative.is_absolute() or '..' in relative.parts or '\\' in name or
                str(relative) != name or not re.fullmatch(r'[0-9a-f]{64}', str(checksum))):
            fail('잘못된 배포 manifest 경로/해시입니다.')
        path = directory / name
        for ancestor in [path, *path.parents]:
            if ancestor == directory:
                break
            if ancestor.is_symlink():
                fail(f'배포 파일에 심볼릭 링크가 있습니다: {name}')
        if not path.is_file() or path.stat().st_nlink != 1 or sha(path) != checksum:
            fail(f'배포 파일 검증 실패: {name}')
        if installed and (path.stat().st_uid != OWNER or path.stat().st_mode & 0o022):
            fail(f'설치 파일 소유자/쓰기 권한을 확인하세요: {name}')
    if not installed:
        actual = set()
        for p in directory.rglob('*'):
            if p.is_symlink() or not (p.is_file() or p.is_dir()):
                fail(f'일반 파일이 아닌 배포 항목: {p}')
            if p.is_file():
                actual.add(str(p.relative_to(directory)))
        if actual != set(manifest['files']) | {'MANIFEST.json'}:
            fail('manifest에 없는 파일 또는 누락된 파일이 있습니다.')
    else:
        for parent in directory.rglob('*'):
            if parent.is_dir() and not parent.is_symlink():
                value = parent.stat()
                if value.st_uid != OWNER or value.st_mode & 0o022:
                    fail(f'설치 하위 디렉터리 소유자/쓰기 권한을 확인하세요: {parent}')

def busy_jobs():
    runs = ROOT / 'var/lib/ala/runs'
    if not runs.exists():
        return
    boot = (ROOT / 'proc/sys/kernel/random/boot_id').read_text().strip()
    for path in runs.iterdir():
        if path.is_symlink():
            fail('설치 작업 기록 경로가 심볼릭 링크입니다.')
        if not path.is_dir():
            continue
        value = json.loads((path / 'result.json').read_text())
        if value.get('state') not in {'queued', 'running'} or value.get('boot_id') != boot:
            continue
        if not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}', path.name):
            fail('잘못된 설치 작업 ID입니다.')
        unit = f'ala-setup-{path.name}.service'
        active = subprocess.run(['systemctl', 'show', unit, '--property=ActiveState', '--value'], capture_output=True, text=True)
        if active.returncode == 0 and active.stdout.strip() in {'active', 'activating', 'reloading'}:
            fail('설치 작업이 진행 중입니다. 완료 후 도구를 갱신하세요.')
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(value['started_at'])).total_seconds()
        if age <= 15:
            fail('설치 작업 시작 중입니다. 잠시 후 상태를 확인하세요.')

def busy_screen_swaps():
    # The durable pair fence outlives per-target leases and the previous boot.
    # Check it under the same writer lock as the final current switch, including
    # direct installation and a swap accepted while an updater was downloading.
    swaps = ROOT / 'var/lib/ala-runtime/swaps'
    for parent in swaps.parents:
        if parent == ROOT:
            break
        if parent.is_symlink():
            fail('화면 교환 기록의 상위 경로가 심볼릭 링크입니다.')
        if parent.exists():
            info = parent.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != OWNER or info.st_mode & 0o022:
                fail('화면 교환 기록 디렉터리의 소유자·권한을 확인하세요.')
    if not swaps.exists() and not swaps.is_symlink():
        return
    secure_directory(swaps)
    for path in sorted(swaps.iterdir()):
        if not re.fullmatch(r'[a-f0-9]{32}\.json', path.name) or path.is_symlink():
            fail('화면 교환 기록의 경로를 확인하세요. 기존 설치를 유지합니다.')
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != OWNER or info.st_mode & 0o022 or not info.st_mode & 0o400
                or info.st_nlink != 1 or info.st_size > 1024 * 1024):
            fail('화면 교환 기록의 소유자·권한·형식을 확인하세요. 기존 설치를 유지합니다.')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            current = os.fstat(stream.fileno())
            if (current.st_ino, current.st_dev) != (info.st_ino, info.st_dev):
                fail('화면 교환 기록이 조회 중 변경됐습니다. 기존 설치를 유지합니다.')
            text = stream.read(1024 * 1024 + 1)
        if len(text) > 1024 * 1024:
            fail('화면 교환 기록의 크기 제한을 초과했습니다.')
        try:
            value = json.loads(text)
            identifier = path.stem
            targets = value.get('targets') if isinstance(value, dict) else None
            if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
                    or value['schema_version'] != 1 or value.get('operation_id') != identifier
                    or not isinstance(targets, list) or len(targets) != 2
                    or any(not isinstance(target, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', target)
                           for target in targets) or len(set(targets)) != 2
                    or value.get('unit') != 'ala-runtime-swap-' + identifier + '.service'
                    or value.get('decision') not in {None, 'commit', 'rollback'}
                    or value.get('state') not in {'queued', 'running', 'unknown', 'recovery_required', 'completed', 'rolled_back'}
                    or not isinstance(value.get('participants'), dict) or set(value['participants']) != set(targets)):
                raise ValueError()
            for target in targets:
                part = value['participants'][target]
                if not isinstance(part, dict):
                    raise ValueError()
                for key in ('before', 'after', 'desired_before', 'desired_after'):
                    selected = part.get(key)
                    if (not isinstance(selected, dict)
                            or any(not isinstance(selected.get(field), str) for field in ('config_path', 'release', 'version', 'revision'))
                            or not Path(selected['config_path']).is_absolute() or not Path(selected['release']).is_absolute()
                            or not re.fullmatch(r'[0-9a-f]{64}', selected['revision'])
                            or not re.fullmatch(r'\d+\.\d+\.\d+(?:rc\d+)?', selected['version'])):
                        raise ValueError()
            terminal = {'completed': 'commit', 'rolled_back': 'rollback'}
            if value['state'] in terminal:
                if value.get('phase') != value['state'] or value['decision'] != terminal[value['state']]:
                    raise ValueError()
            else:
                fail('두 화면 교환 또는 복구 완료 후 CLI를 갱신하세요. 현재 화면과 설치를 유지합니다.')
        except (ValueError, TypeError, KeyError):
            fail('화면 교환 기록이 손상됐습니다. 복구 상태를 확인한 뒤 CLI를 갱신하세요.')

def busy_display_operations():
    layout = ROOT / 'var/lib/ala/monitor-layout/current.json'
    if layout.exists() or layout.is_symlink():
        for item in [layout, *layout.parents]:
            if item.is_symlink():
                fail('모니터 배치 복구 경로의 심볼릭 링크를 허용하지 않습니다.')
            if item == ROOT:
                break
            if item.exists():
                info = item.stat()
                if info.st_uid != OWNER or info.st_mode & 0o022:
                    fail('모니터 배치 복구 기록 권한 확인 필요')
        info = layout.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            fail('모니터 배치 복구 기록 형식 확인 필요')
        value = json.loads(layout.read_text())
        backup = value.get('backup')
        if (type(value.get('schema_version')) is not int or value['schema_version'] != 1
                or type(value.get('persistent')) is not bool or not isinstance(backup, dict)
                or hashlib.sha256(json.dumps(backup, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest() != value.get('backup_sha256')
                or value.get('state') not in {'confirmed', 'rolled_back', 'not_applied'}):
            fail('모니터 배치 확인 또는 복구 완료 후 CLI를 갱신하세요.')
    fence = ROOT / 'var/lib/ala-runtime/display-reset.json'
    if fence.exists() or fence.is_symlink():
        for parent in [fence, *fence.parents]:
            if parent.is_symlink():
                fail('DID 초기화 보호 경로의 심볼릭 링크를 허용하지 않습니다.')
            if parent == ROOT:
                break
            if parent.exists():
                info=parent.stat()
                if info.st_uid != OWNER or info.st_mode & 0o022:
                    fail('DID 초기화 보호 기록 권한 확인 필요')
        value=json.loads(fence.read_text())
        if type(value.get('pending')) is not bool or value['pending']:
            fail('DID 초기화 또는 복구 완료 후 CLI를 갱신하세요.')
    busy_screen_swaps()
    targets = ROOT / 'var/lib/ala-runtime/targets'
    if not targets.exists():
        return
    secure_directory(targets)
    boot = (ROOT / 'proc/sys/kernel/random/boot_id').read_text().strip()
    for target in targets.iterdir():
        if not target.is_dir() or target.is_symlink():
            fail('화면 작업 기록 경로를 확인하세요.')
        secure_directory(target)
        path = target / 'state.json'
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != OWNER
                or info.st_mode & 0o022 or info.st_size > 1024 * 1024):
            fail('화면 작업 기록의 소유자·권한을 확인하세요.')
        value = json.loads(path.read_text())
        operation = value.get('operation') or {}
        if (operation.get('boot_id') == boot and operation.get('generation') == value.get('generation')
                and operation.get('expires_monotonic', 0) > time.monotonic()):
            fail('화면 코드 전환 또는 프론트 갱신 완료 후 CLI를 갱신하세요. 현재 화면은 유지합니다.')

def automatic_compatibility():
    # Recheck while holding the configuration writer's lock: a schema-2 save
    # between the download preflight and this installer must not be missed.
    if os.environ.get('ALA_REQUIRE_COMPATIBILITY') != '1':
        return
    metadata = json.loads((SOURCE / 'COMPATIBILITY.json').read_text())
    protocols = ('schema_version', 'runtime_protocol', 'frontend_protocol', 'extension_protocol')
    supported = metadata.get('config_schemas') if isinstance(metadata, dict) else None
    if (not isinstance(metadata, dict) or any(type(metadata.get(key)) is not int or metadata[key] != 1 for key in protocols)
            or not isinstance(supported, list) or not supported
            or any(type(schema) is not int or schema not in {1, 2} for schema in supported)
            or len(set(supported)) != len(supported)):
        fail('자동 갱신 관리 프로토콜이 호환되지 않습니다.')
    configs = [ROOT / 'etc/ala/config.json']
    snapshots = ROOT / 'var/lib/ala-runtime/configs'
    if snapshots.exists():
        secure_directory(snapshots)
        configs.extend(snapshots.iterdir())
    required = {1}
    for path in configs:
        if not path.exists() and not path.is_symlink():
            continue
        secure_directory(path.parent)
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != OWNER
                or info.st_mode & 0o022 or info.st_size > 1024 * 1024):
            fail('자동 갱신 전 구성 소유자·권한·크기를 확인하세요.')
        value = json.loads(path.read_text())
        schema = value.get('schema_version', 1) if isinstance(value, dict) else None
        if type(schema) is not int or schema not in {1, 2}:
            fail('자동 갱신 전 구성 schema를 확인하세요.')
        required.add(schema)
    if not required.issubset(supported):
        fail('자동 갱신 후보가 현재·보관된 구성 schema를 모두 지원하지 않습니다.')

def atomic_text(path, value, mode):
    if path.is_symlink():
        fail(f'진입점이 심볼릭 링크입니다: {path}')
    if path.exists() and path.read_text() == value and path.stat().st_uid == OWNER and stat.S_IMODE(path.stat().st_mode) == mode:
        return
    fd, name = tempfile.mkstemp(prefix='.ala-launcher-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def sync_release(path):
    # Commit the prepared files before publishing the version pointer.
    for item in path.rglob('*'):
        if item.is_symlink():
            continue
        if item.is_file():
            with item.open('rb') as stream:
                os.fsync(stream.fileno())
        elif item.is_dir():
            sync_directory(item)
    sync_directory(path)

def install():
    manifest_path = SOURCE / 'MANIFEST.json'
    if manifest_path.is_symlink():
        fail('manifest는 일반 파일이어야 합니다.')
    manifest = json.loads(manifest_path.read_text())
    verify(SOURCE, manifest)
    version = (SOURCE / 'VERSION').read_text().strip()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', version) or manifest.get('version') != version:
        fail('잘못된 배포 버전입니다.')
    lock_dir = ROOT / 'run/lock/ala'
    secure_directory(lock_dir)
    lock_path = lock_dir / 'operation.lock'
    lock = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(lock).st_mode) or os.fstat(lock).st_uid != OWNER or os.fstat(lock).st_mode & 0o022:
            fail('설치 잠금 소유자/쓰기 권한이 올바르지 않습니다.')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail('다른 설치 또는 도구 변경 작업이 진행 중입니다.')
        busy_jobs()
        busy_display_operations()
        automatic_compatibility()
        destination = ROOT / 'opt/ala'
        releases = destination / 'releases'
        secure_directory(releases)
        current = destination / 'current'
        if current.exists() and not current.is_symlink():
            fail('current가 심볼릭 링크가 아닙니다. 기존 경로를 확인하세요.')
        if current.is_symlink() and not current.resolve().is_relative_to(releases):
            fail('current가 ala releases 밖을 가리킵니다.')
        if os.environ.get('ALA_UPDATE_ONLY') == '1' and current.is_symlink():
            def version_key(value):
                match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:rc(\d+))?', value)
                if not match:
                    fail('현재/대상 버전 형식을 확인하세요.')
                a, b, c, rc = match.groups()
                return int(a), int(b), int(c), rc is None, int(rc or 0)
            installed_version = (current / 'VERSION').read_text().strip()
            if version_key(version) <= version_key(installed_version):
                fail('이미 같거나 더 새로운 버전이 설치되어 있습니다.')
        launcher = ROOT / 'usr/local/bin/ala'
        secure_directory(launcher.parent)
        if launcher.exists() and (launcher.is_symlink() or '# ala managed launcher v1' not in launcher.read_text()):
            fail('다른 ala 명령이 존재합니다. 기존 명령을 확인하세요.')
        release = releases / version
        created = False
        stage = None
        try:
            if release.exists() or release.is_symlink():
                if release.is_symlink() or not (release / 'INSTALLATION.json').is_file():
                    fail('동일 버전의 불완전한 설치 경로가 있습니다. 직접 확인하세요.')
                secure_directory(release)
                existing_manifest = release / 'MANIFEST.json'
                if existing_manifest.is_symlink() or existing_manifest.stat().st_uid != OWNER or existing_manifest.stat().st_mode & 0o022:
                    fail('설치 manifest 소유자/쓰기 권한을 확인하세요.')
                if json.loads((release / 'MANIFEST.json').read_text()) != manifest:
                    fail('같은 버전에 다른 내용이 있습니다. 새 버전을 사용하세요.')
                verify(release, manifest, installed=True)
            else:
                stage = Path(tempfile.mkdtemp(prefix='.ala-stage-', dir=releases))
                os.chmod(stage, 0o755)
                for name in [*manifest['files'], 'MANIFEST.json']:
                    target = stage / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(SOURCE / name, target)
                    os.chmod(target, 0o755 if target.suffix == '.sh' or target.name == 'chrome-did' else 0o644)
                verify(stage, manifest, installed=True)
                os.rename(stage, release)
                stage = None
                created = True
                # Create venv at its final location: venv scripts have absolute shebangs.
                if subprocess.run([sys.executable, '-I', '-m', 'pip', '--version'], stdout=subprocess.DEVNULL).returncode:
                    dnf_install('python3-pip')
                subprocess.run([sys.executable, '-I', '-m', 'venv', str(release / '.venv')], check=True)
                interpreter = release / '.venv/bin/python'
                subprocess.run([str(interpreter), '-I', '-m', 'pip', '--isolated', 'install', '--no-index',
                                '--find-links', str(release / 'wheels'), '--require-hashes',
                                '-r', str(release / 'requirements.lock')], check=True)
                (release / 'INSTALLATION.json').write_text(json.dumps({'version': version, 'installed_at': datetime.now(timezone.utc).isoformat()}) + '\n')
                os.chmod(release / 'INSTALLATION.json', 0o644)
            interpreter = release / '.venv/bin/python'
            textual_pin = re.search(r'^textual==([^\s]+) --hash=sha256:', (release / 'requirements.lock').read_text(), re.MULTILINE)
            if textual_pin is None:
                fail('Textual 버전이 고정되지 않았습니다.')
            subprocess.run([str(interpreter), '-I', '-c',
                            "import importlib.metadata as m, sys; assert m.version('textual') == sys.argv[1]",
                            textual_pin.group(1)], check=True)
            subprocess.run([str(interpreter), '-I', str(release / 'entry.py'), '--version'], check=True)
            if created:
                sync_release(release)
                sync_directory(releases)
            # Resolve once: a simultaneous current switch must not pair one
            # release's interpreter/site-packages with another release's entry.
            wrapper = f'#!/usr/bin/env bash\n# ala managed launcher v1\nset -e\nALA_RELEASE_PATH=$(cd -- "{current}" && pwd -P)\nexec "$ALA_RELEASE_PATH/.venv/bin/python" -I "$ALA_RELEASE_PATH/entry.py" "$@"\n'
            atomic_text(launcher, wrapper, 0o755)
            if not current.is_symlink() or current.resolve() != release:
                link = destination / ('.current-' + uuid.uuid4().hex)
                try:
                    link.symlink_to(release)
                    os.replace(link, current)
                    sync_directory(destination)
                finally:
                    if link.is_symlink():
                        link.unlink()
            print(f'ala {version} 설치 완료. ala에서 간편 설정 실행을 누르거나 ala check로 상태를 확인하세요.')
        except Exception:
            if created and (not current.is_symlink() or current.resolve() != release):
                shutil.rmtree(release)
            raise
        finally:
            if stage is not None:
                shutil.rmtree(stage)
    finally:
        os.close(lock)

try:
    install()
except Exception as error:
    print(f'ala 설치 실패: {error}', file=sys.stderr)
    sys.exit(1)
PY
