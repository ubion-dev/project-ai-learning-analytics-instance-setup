"""Preview and apply an explicitly approved DNF removal using system Python."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys


class InspectionError(RuntimeError):
    pass


def query_json(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15,
                                env=dict(os.environ, CONTAINER_HOST='', CONTAINER_CONNECTION=''))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InspectionError('Podman 조회를 완료하지 못했습니다.') from exc
    if result.returncode:
        raise InspectionError('Podman 사용 여부 조회 실패. 계정과 rootless 실행 환경을 확인하세요.')
    try:
        value = json.loads(result.stdout)
    except ValueError as exc:
        raise InspectionError('Podman 조회 결과를 해석하지 못했습니다.') from exc
    if not isinstance(value, list):
        raise InspectionError('Podman 조회 결과가 목록이 아닙니다.')
    return value


def local_accounts():
    accounts = []
    for account in pwd.getpwall():
        home = Path(account.pw_dir)
        storage = home / '.local/share/containers'
        settings = home / '.config/containers'
        if (account.pw_uid == 0 or
                (account.pw_uid >= 1000 and account.pw_uid != 65534 and home.is_dir()) or
                storage.exists() or settings.exists()):
            accounts.append(account)
    return sorted(accounts, key=lambda account: (account.pw_uid, account.pw_name))


def inspect_usage():
    checks = []
    system_units = (Path('/etc/containers/systemd'), Path('/usr/share/containers/systemd'))
    for directory in system_units:
        if directory.exists() and any(p.suffix in {'.container', '.volume', '.network', '.kube', '.pod', '.build', '.image'}
                                      for p in directory.rglob('*')):
            raise InspectionError('Podman 자동 실행 설정이 있습니다. 기존 환경을 확인하세요.')
    for account in local_accounts():
        quadlets = Path(account.pw_dir) / '.config/containers/systemd'
        if quadlets.exists() and any(quadlets.iterdir()):
            raise InspectionError('사용자 Podman 자동 실행 설정이 있습니다. 기존 환경을 확인하세요.')
        command = ['/usr/bin/podman', '--remote=false']
        if account.pw_uid:
            command = ['/usr/sbin/runuser', '-u', account.pw_name, '--',
                       '/usr/bin/env', '-i', 'PATH=/usr/bin:/bin',
                       'HOME=' + account.pw_dir, 'USER=' + account.pw_name,
                       'LOGNAME=' + account.pw_name,
                       'XDG_RUNTIME_DIR=/run/user/' + str(account.pw_uid), *command]
        for args in (['ps', '-a'], ['volume', 'ls'], ['images'], ['pod', 'ps']):
            if query_json([*command, *args, '--format', 'json']):
                raise InspectionError('Podman 컨테이너·볼륨·이미지·pod가 있습니다. 기존 환경을 확인하세요.')
        networks = query_json([*command, 'network', 'ls', '--format', 'json'])
        if any(network.get('name', network.get('Name')) != 'podman' for network in networks):
            raise InspectionError('사용자 정의 Podman 네트워크가 있습니다. 기존 환경을 확인하세요.')
        checks.append({'user': account.pw_name, 'uid': account.pw_uid, 'home': account.pw_dir})
    return checks


def removal_plan(base):
    installed = list(base.sack.query().installed())
    names = {package.name for package in installed}
    targets = sorted(names & {'podman', 'podman-docker'})
    if not targets:
        return None
    if 'podman' not in names:
        raise InspectionError('podman-docker만 남아 있어 사용 여부를 확인할 수 없습니다.')
    accounts = inspect_usage()
    for name in targets:
        base.remove(name)
    base.resolve(allow_erasing=True)
    if base.transaction.install_set:
        raise InspectionError('제거 계획에 패키지 설치가 포함되어 있습니다.')
    packages = sorted(str(package) for package in base.transaction.remove_set)
    if not packages:
        raise InspectionError('Podman 제거 목록이 비어 있습니다.')
    snapshot = {'packages': packages, 'accounts': accounts}
    token = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    return {'state': 'ready', 'packages': packages, 'accounts': accounts, 'token': token}


def run(expected_token=None):
    import dnf
    with dnf.Base() as base:
        base.conf.read()
        base.conf.clean_requirements_on_remove = False
        base.conf.assumeyes = True
        base.conf.exit_on_lock = True
        base.fill_sack(load_available_repos=False)
        plan = removal_plan(base)
        if plan is None:
            return {'state': 'not_required', 'packages': []}
        if expected_token is not None:
            if plan['token'] != expected_token:
                raise InspectionError('승인 후 제거 목록 또는 계정 구성이 바뀌었습니다. 제거 목록을 다시 확인하세요.')
            # Probe again immediately before the RPM transaction.
            if inspect_usage() != plan['accounts']:
                raise InspectionError('승인 후 조회 대상 계정이 바뀌었습니다.')
            base.do_transaction()
            return dict(plan, state='completed')
        return plan


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--expected-token')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('관리자 실행기가 필요합니다.')
    try:
        # DNF callbacks must not contaminate the structured response.
        with contextlib.redirect_stdout(sys.stderr):
            value = run(args.expected_token)
    except Exception as exc:
        value = {'state': 'blocked', 'reason': str(exc)}
    print(json.dumps(value, ensure_ascii=False))
    return 0 if value['state'] in {'ready', 'completed', 'not_required'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
