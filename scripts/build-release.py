#!/usr/bin/env python3
"""Build an allow-listed, hashed, offline-installable ala source/wheel bundle."""
from __future__ import annotations

import argparse
import email
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def locked_requirements(path: Path):
    result = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([^\s]+) --hash=sha256:([0-9a-f]{64})', line.strip())
        if not match:
            raise ValueError(f'지원하지 않는 lock 항목: {line}')
        name, version, checksum = match.groups()
        result[name.lower().replace('_', '-')] = (version, checksum)
    return result


def copy_wheels(source: Path, target: Path, locked):
    found = set()
    target.mkdir()
    for wheel in sorted(source.glob('*.whl')):
        if wheel.is_symlink():
            raise ValueError(f'심볼릭 링크 wheel은 사용할 수 없습니다: {wheel}')
        with zipfile.ZipFile(wheel) as archive:
            metadata = email.message_from_bytes(archive.read(next(n for n in archive.namelist() if n.endswith('.dist-info/METADATA'))))
        name = metadata['Name'].lower().replace('_', '-')
        if name not in locked:
            continue
        version, checksum = locked[name]
        if metadata['Version'] != version or digest(wheel) != checksum:
            raise ValueError(f'lock과 다른 wheel: {wheel.name}')
        if not wheel.name.endswith('-none-any.whl'):
            raise ValueError(f'현재 배포는 Python3.12+ universal wheel만 지원합니다: {wheel.name}')
        if name in found:
            raise ValueError(f'중복 wheel: {name}')
        found.add(name)
        shutil.copyfile(wheel, target / wheel.name)
    if found != locked.keys():
        raise ValueError('누락된 wheel: ' + ', '.join(sorted(locked.keys() - found)))


def source_files(root: Path):
    # A deny-list is insufficient: the original repository contains customer data.
    files = [root / name for name in ('entry.py', 'pyproject.toml', 'requirements.lock', 'install.sh')]
    files += list((root / 'ala').rglob('*.py'))
    legacy = root / 'scripts/rocky-linux'
    files += list(legacy.glob('*.sh')) + list((legacy / 'lib').glob('*.py')) + [legacy / 'bin/chrome-did']
    files += [root / name for name in ('README.md', 'docs/INSTALL.md', 'docs/OPERATIONS.md', 'docs/PREPARATION.md', 'docs/DRIVERS.md', 'docs/SOURCE.md',
                                      'docs/ACCEPTANCE.md', 'examples/instance.json') if (root / name).is_file()]
    for path in sorted(set(files)):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'배포 파일이 없거나 일반 파일이 아닙니다: {path}')
        if '__pycache__' not in path.parts:
            yield path


def build(output: Path, wheelhouse: Path | None = None, root=ROOT):
    version = tomllib.loads((root / 'pyproject.toml').read_text())['project']['version']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', version):
        raise ValueError('안전하지 않은 버전 문자열입니다.')
    locked = locked_requirements(root / 'requirements.lock')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ala-build-') as temporary:
        stage = Path(temporary) / f'ala-{version}'
        stage.mkdir()
        for path in source_files(root):
            target = stage / path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        if wheelhouse is None:
            wheelhouse = Path(temporary) / 'download'
            subprocess.run([sys.executable, '-m', 'pip', '--isolated', 'download', '--only-binary=:all:',
                            '--python-version', '3.12', '--require-hashes', '--dest', str(wheelhouse),
                            '-r', str(root / 'requirements.lock')], check=True)
        copy_wheels(wheelhouse, stage / 'wheels', locked)
        (stage / 'VERSION').write_text(version + '\n')
        files = {str(p.relative_to(stage)): digest(p) for p in sorted(stage.rglob('*')) if p.is_file()}
        (stage / 'MANIFEST.json').write_text(json.dumps({'schema_version': 1, 'version': version, 'files': files}, sort_keys=True, indent=2) + '\n')
        archive_path = output / f'ala-{version}.tar.gz'
        with archive_path.open('wb') as stream, gzip.GzipFile(filename='', mode='wb', fileobj=stream, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as archive:
                for p in sorted(stage.rglob('*')):
                    if not p.is_file():
                        continue
                    data = p.read_bytes()
                    info = tarfile.TarInfo(f'{stage.name}/{p.relative_to(stage)}')
                    info.size = len(data)
                    info.mode = 0o755 if p.suffix == '.sh' or p.name == 'chrome-did' else 0o644
                    info.uid = info.gid = info.mtime = 0
                    info.uname = info.gname = 'root'
                    archive.addfile(info, io.BytesIO(data))
        archive_path.with_suffix(archive_path.suffix + '.sha256').write_text(f'{digest(archive_path)}  {archive_path.name}\n')
        return archive_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    parser.add_argument('--wheelhouse', type=Path, help='기존 universal wheel 폴더; lock 해시는 여전히 검증')
    args = parser.parse_args()
    try:
        print(build(args.output, args.wheelhouse))
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'배포 묶음 생성 실패: {exc}\n')


if __name__ == '__main__':
    main()
