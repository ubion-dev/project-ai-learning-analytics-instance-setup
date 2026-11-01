#!/usr/bin/env python3
"""Prepare or inspect local Chrome policies without launching the browser."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import time


POLICIES = ('TranslateEnabled', 'DefaultBrowserSettingEnabled', 'PromotionsEnabled')
FILENAME = 'did-display.json'


def read_policy(path):
    # Chrome accepts JSON comments and trailing commas, and reads every filename.
    text = path.read_text(encoding='utf-8')
    text = re.sub(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/',
                  lambda match: match[1] if match[1] is not None else ' ', text, flags=re.S)
    text = re.sub(r'("(?:\\.|[^"\\])*")|,(\s*[}\]])',
                  lambda match: match[1] if match[1] is not None else match[2], text)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError('Chrome 정책은 JSON object여야 합니다.')
    return data


def read_directory(directory):
    if directory.is_symlink():
        raise ValueError(f'{directory}: 정책 디렉터리의 심볼릭 링크를 먼저 확인하세요.')
    if not directory.exists():
        return {}
    result = {}
    for path in sorted(directory.iterdir()):
        if path.is_symlink():
            raise ValueError(f'{path}: 정책 심볼릭 링크를 먼저 확인하세요.')
        if path.is_file():
            try:
                result[path] = read_policy(path)
            except (OSError, ValueError) as error:
                raise ValueError(f'{path}: {error}') from error
    target = directory / FILENAME
    if target.exists() and not target.is_file():
        raise ValueError(f'{target}: 정책 파일 경로가 일반 파일이 아닙니다.')
    return result


def readable_policy(path):
    value = path.stat()
    mode = stat.S_IMODE(value.st_mode)
    return (value.st_uid == os.geteuid() and
            bool(mode & stat.S_IROTH) and not mode & (stat.S_IWGRP | stat.S_IWOTH))


def ensure_directory(path, mode):
    if path.is_symlink():
        raise ValueError(f'{path}: 심볼릭 링크를 먼저 확인하세요.')
    path.mkdir(parents=True, exist_ok=True)
    if path.stat().st_uid != os.geteuid():
        os.chown(path, os.geteuid(), -1)
    if stat.S_IMODE(path.stat().st_mode) != mode:
        path.chmod(mode)


def apply(directory, backups):
    files = read_directory(directory)
    target = directory / FILENAME
    original = files.get(target, {})
    data = dict(original)
    for key in POLICIES:
        external = [path for path, values in files.items() if path != target and key in values]
        for path in external:
            if files[path][key] is not False or not readable_policy(path):
                raise ValueError(f'{path}: {key} 정책 값·권한이 충돌합니다. 기존 파일을 변경하지 않았습니다.')
        if external:
            data.pop(key, None)  # Keep an already configured policy in its original file.
        else:
            data[key] = False

    ensure_directory(directory.parent, 0o755)
    ensure_directory(directory, 0o755)
    same = json.dumps(original, sort_keys=True) == json.dumps(data, sort_keys=True)
    if target.exists() and same and readable_policy(target) and stat.S_IMODE(target.stat().st_mode) == 0o644:
        print(f'[유지] Chrome 안내 정책: {target}')
        return
    if target.exists():
        # Chrome reads hidden files and backups in managed/, regardless of extension.
        ensure_directory(backups, 0o700)
        shutil.copy2(target, backups / f'did-display.{time.time_ns()}.{os.getpid()}.json')
    temporary = None
    try:
        # Stage outside managed/ so Chrome never reads an incomplete candidate.
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory.parent,
                                         prefix='.did-display.', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f'[적용] Chrome 안내 정책: {target}')


def check(directory):
    files = read_directory(directory)
    for key in POLICIES:
        declarations = [path for path, values in files.items() if key in values]
        if not declarations or any(files[path][key] is not False or not readable_policy(path)
                                   for path in declarations):
            raise ValueError(f'{key}: false 정책의 존재·값·소유자·읽기 권한을 확인하세요.')
        print(f'[OK] Chrome {key}=false')
    for path in (directory.parent, directory):
        value = path.stat()
        mode = stat.S_IMODE(value.st_mode)
        if (value.st_uid != os.geteuid() or mode & (stat.S_IWGRP | stat.S_IWOTH) or
                mode & (stat.S_IROTH | stat.S_IXOTH) != (stat.S_IROTH | stat.S_IXOTH)):
            raise ValueError(f'{path}: Chrome이 읽을 수 있는 관리자 소유 디렉터리가 필요합니다.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument('--apply', action='store_true')
    actions.add_argument('--check', action='store_true')
    parser.add_argument('--directory', type=Path, default=Path('/etc/opt/chrome/policies/managed'))
    parser.add_argument('--backups', type=Path, default=Path('/var/lib/rocky-setup/chrome-policy-backups'))
    args = parser.parse_args()
    try:
        if args.apply:
            apply(args.directory, args.backups)
        check(args.directory)
    except (OSError, ValueError) as error:
        print(f'Chrome 정책 검사 실패: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
