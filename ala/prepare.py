"""One-time OS preparation, separate from ordinary environment reconciliation.

The caller holds ala's operation lock. No boot service resumes this operation:
following a reboot the operator invokes apply again to verify the checkpoint.
"""
from __future__ import annotations

import configparser
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import uuid
from urllib.parse import urlsplit

from .engine import Engine, SCRIPTS
from .model import CheckResult, Report
from .system import Host, Paths, atomic_json, platform_error

MARKER = '/var/lib/ala/prepare.json'
OFFICIAL_REPOS = ('baseos', 'appstream', 'extras', 'crb')
# No replacement/removal or retention pruning during this initial operation.
DNF_OPTIONS = ('--noplugins', '--releasever=10', '--disablerepo=*',
               '--setopt=install_weak_deps=False', '--setopt=obsoletes=False',
               '--setopt=installonly_limit=0', '--setopt=gpgcheck=True',
               '--exclude=google-chrome*,docker*,containerd*,nvidia*,cuda*,dkms,akmod*,kmod-*')
RPM_QUERY = ('rpm', '-qa', '--qf', '%{NAME}\t%{EPOCHNUM}\t%{VERSION}\t%{RELEASE}\t%{ARCH}\n')
KERNEL = re.compile(r'[0-9][A-Za-z0-9._+~-]{1,180}\.x86_64\Z')
PACKAGE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._+~:-]{0,250}\Z')
BAD = {'blocked', 'unknown', 'error'}
STATES = {'upgrading', 'failed', 'verifying', 'reboot_required', 'complete'}


def _result(name, state, expected=None, actual=None, action='', detail=''):
    return CheckResult('prepare.' + name, state, expected, actual, action, detail)


def _now():
    return datetime.now(timezone.utc).isoformat()


class Prepare:
    def __init__(self, config, host=None, paths=None):
        self.config = config
        self.host = host if host is not None else Host()
        self.paths = paths if paths is not None else Paths()
        self.env = dict(os.environ, LC_ALL='C', LANG='C')

    def _run(self, argv, **kwargs):
        return self.host.run(argv, env=self.env, **kwargs)

    def _safe_marker(self):
        marker = self.paths.at(MARKER)
        for p in (marker, *marker.parents):
            if p.is_symlink():
                raise ValueError('초기 준비 기록 경로에 심볼릭 링크가 있습니다.')
        if marker.exists():
            info = marker.stat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 8 * 1024 * 1024):
                raise ValueError('초기 준비 기록 소유자·권한·형식을 확인하세요.')
        return marker

    def _load(self):
        marker = self._safe_marker()
        if not marker.exists():
            return None
        value = json.loads(marker.read_text())
        if (not isinstance(value, dict) or value.get('schema_version') != 1
                or value.get('state') not in STATES
                or not isinstance(value.get('before'), list)
                or not isinstance(value.get('boot_id'), str)
                or not isinstance(value.get('initial_kernel'), str)
                or not KERNEL.fullmatch(value['initial_kernel'])):
            raise ValueError('초기 준비 기록이 유효하지 않습니다.')
        uuid.UUID(value['boot_id'])
        self._validate_snapshot(value['before'])
        if value.get('expected_kernel') and not KERNEL.fullmatch(value['expected_kernel']):
            raise ValueError('기록된 부팅 커널이 유효하지 않습니다.')
        if value['state'] in {'reboot_required', 'complete'} and not value.get('expected_kernel'):
            raise ValueError('기록에 예상 부팅 커널이 없습니다.')
        return value

    def _save(self, value):
        marker = self._safe_marker()
        marker.parent.mkdir(parents=True, exist_ok=True)
        info = marker.parent.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError('초기 준비 기록 디렉터리 소유자·권한이 올바르지 않습니다.')
        atomic_json(marker, value)

    def _boot(self):
        boot = self.paths.at('/proc/sys/kernel/random/boot_id').read_text().strip()
        uuid.UUID(boot)
        result = self._run(['uname', '-r'])
        kernel = result.stdout.strip()
        if result.returncode or not KERNEL.fullmatch(kernel):
            raise ValueError('실행 커널을 확인할 수 없습니다.')
        return boot, kernel

    @staticmethod
    def _validate_snapshot(rows):
        if len(rows) > 100000:
            raise ValueError('RPM 목록이 너무 큽니다.')
        for row in rows:
            if (not isinstance(row, str) or len(row.split('\t')) != 5
                    or not all(PACKAGE.fullmatch(field) for field in row.split('\t'))):
                raise ValueError('RPM 목록 형식을 확인할 수 없습니다.')
        return sorted(set(rows))

    def _snapshot(self):
        result = self._run(RPM_QUERY, timeout=30)
        if result.returncode or not result.stdout.strip():
            raise ValueError('설치된 RPM 목록을 읽을 수 없습니다.')
        return self._validate_snapshot(result.stdout.splitlines())

    def _repos(self):
        directory = self.paths.at('/etc/yum.repos.d')
        if not directory.is_dir():
            raise ValueError('Rocky 공식 저장소 설정이 없습니다.')
        found = {}
        for path in sorted(directory.glob('*.repo')):
            parser = configparser.ConfigParser(interpolation=None, strict=True)
            parser.read_string(path.read_text())
            for name in parser.sections():
                if name not in OFFICIAL_REPOS:
                    continue
                if name in found:
                    raise ValueError('공식 저장소 ID가 중복되어 있습니다: ' + name)
                section = parser[name]
                enabled = section.getboolean('enabled', fallback=True)
                if not enabled:
                    found[name] = False
                    continue
                # The official mirrorlist delegates mirrors; arbitrary repository
                # definitions reusing a Rocky ID are not trusted by their name.
                urls = [section.get(key, '').strip() for key in ('mirrorlist', 'metalink', 'baseurl')]
                urls = [url for values in urls for url in values.split()]
                if not urls or not all(urlsplit(url).scheme == 'https' and
                    (urlsplit(url).hostname or '').endswith('.rockylinux.org') for url in urls):
                    raise ValueError('공식 Rocky HTTPS 저장소 출처를 확인하세요: ' + name)
                if not section.getboolean('gpgcheck', fallback=False):
                    raise ValueError('저장소 GPG 검증을 활성화하세요: ' + name)
                owner = self._run(['rpm', '-qf', '--qf', '%{NAME}\n', str(path)])
                if owner.returncode or owner.stdout.strip() != 'rocky-repos':
                    raise ValueError('rocky-repos 소유 설정이 아닙니다: ' + name)
                keys = section.get('gpgkey', '').split()
                if not keys:
                    raise ValueError('공식 패키지 서명 키가 없습니다: ' + name)
                for key in keys:
                    parsed = urlsplit(key)
                    if parsed.scheme != 'file' or parsed.netloc or not parsed.path.startswith('/etc/pki/rpm-gpg/'):
                        raise ValueError('로컬 Rocky 패키지 서명 키를 확인하세요: ' + name)
                    key_path = self.paths.at(parsed.path)
                    key_owner = self._run(['rpm', '-qf', '--qf', '%{NAME}\n', str(key_path)])
                    verified = self._run(['rpm', '-Vf', str(key_path)])
                    if key_owner.returncode or key_owner.stdout.strip() != 'rocky-gpg-keys' or verified.returncode:
                        raise ValueError('공식 서명 키 패키지 무결성을 확인하세요: ' + name)
                found[name] = True
        if not all(found.get(name) for name in ('baseos', 'appstream')):
            raise ValueError('활성화된 baseos/appstream 저장소가 필요합니다.')
        return [name for name in OFFICIAL_REPOS if found.get(name)]

    def _external_modules(self, snapshot):
        names = [row.split('\t')[0] for row in snapshot]
        external = [name for name in names if re.match(
            r'^(dkms$|akmods$|akmod-|kmod-(?!libs$)|nvidia-(open|driver)|cuda-drivers)', name)]
        dkms = self.paths.at('/var/lib/dkms')
        if dkms.is_dir() and any(p.is_dir() for p in dkms.iterdir()):
            external.append('DKMS 등록 모듈')
        if self.paths.at('/usr/bin/nvidia-uninstall').exists():
            external.append('NVIDIA .run 설치')
        # Distribution kernel packages keep their modules under kernel/. Extra
        # module trees also reveal unloaded DKMS/kABI/vendor modules, without
        # loading them or invoking modinfo thousands of times.
        for root in ('/usr/lib/modules', '/lib/modules'):
            directory = self.paths.at(root)
            if directory.is_dir():
                for release in directory.iterdir():
                    if not release.is_dir():
                        continue
                    for location in ('extra', 'updates', 'weak-updates'):
                        tree = release / location
                        if tree.is_dir():
                            for module in tree.rglob('*'):
                                if re.search(r'\.ko(?:\.(?:xz|gz|zst))?$', module.name):
                                    external.append(str(module.relative_to(directory)))
        modules = self.paths.at('/sys/module')
        if not modules.is_dir():
            raise ValueError('실행 중 커널 모듈 정보를 읽을 수 없습니다.')
        for path in modules.iterdir():
            taint = path / 'taint'
            if taint.exists() and any(flag in taint.read_text() for flag in ('O', 'P', 'E')):
                external.append(path.name)
        return sorted(set(external))

    def _preflight(self):
        results = []
        error = platform_error(self.paths)
        if error:
            return [_result('platform', 'blocked', 'Rocky Linux 10 x86_64', error)], None, None
        try:
            snapshot = self._snapshot()
            external = self._external_modules(snapshot)
            results.append(_result('external_modules', 'blocked' if external else 'ok',
                '외부 커널 모듈 호환성 검토 불필요', external,
                '기존 NVIDIA/DKMS 드라이버의 커널 호환성을 별도 검토하세요.' if external else ''))
        except (OSError, ValueError) as exc:
            snapshot = None
            results.append(_result('inventory', 'unknown', 'RPM/모듈 조회', str(exc)))
        for name, minimum in (('/var', 2 * 1024**3), ('/boot', 256 * 1024**2)):
            try:
                free = shutil.disk_usage(self.paths.at(name)).free
                results.append(_result('space.' + name.strip('/'), 'ok' if free >= minimum else 'blocked',
                    {'minimum_free_bytes': minimum}, {'free_bytes': free}, '공간 확보 후 재시도' if free < minimum else '',
                    '최소 여유 공간 검사이며 실제 트랜잭션 공간은 DNF가 다시 검사합니다.'))
            except OSError as exc:
                results.append(_result('space.' + name.strip('/'), 'unknown', '공간 조회', str(exc)))
        processes = self._run(['pgrep', '-x', 'dnf|dnf5|yum|rpm|packagekitd'])
        if processes.returncode not in (0, 1):
            results.append(_result('transaction', 'unknown', '패키지 작업 없음', processes.stderr))
        else:
            results.append(_result('transaction', 'blocked' if processes.returncode == 0 else 'ok',
                '경합 가능한 패키지 프로세스 없음', '패키지 관리자/PackageKit 데몬 존재' if processes.returncode == 0 else '없음',
                'PackageKit은 유휴 데몬일 수도 있습니다. 진행 중 작업 여부를 확인하고 해당 프로세스 종료 후 다시 실행하세요. 강제로 종료하지 않습니다.' if processes.returncode == 0 else ''))
        # No dconf requirement at this phase: GUI policy belongs to setup. The
        # service/timer masks are checked before and after the OS transaction.
        results.extend(Engine(replace(self.config, display=False), host=self.host, paths=self.paths)._updates())
        try:
            repos = self._repos()
            results.append(_result('repositories', 'ok', '활성 공식 저장소', repos))
        except (OSError, ValueError, configparser.Error) as exc:
            repos = None
            results.append(_result('repositories', 'blocked', '활성 공식 저장소', str(exc)))
        return results, snapshot, repos

    def _dnf(self, repos):
        return ['dnf', *DNF_OPTIONS, *(f'--enablerepo={repo}' for repo in repos)]

    def _checkpoint_report(self, value):
        boot, kernel = self._boot()
        state = value['state']
        if state == 'complete':
            return Report([_result('state', 'ok', '최초 OS 준비 완료', '완료 기록 유지',
                                   'ala setup --apply', '이 명령은 정기 업데이트를 반복하지 않습니다.')])
        if state == 'reboot_required':
            if boot == value['boot_id']:
                return Report([_result('reboot', 'pending', value['expected_kernel'], kernel,
                    '직접 재부팅한 뒤 ala prepare --apply를 다시 실행하세요.')])
            if kernel != value['expected_kernel']:
                return Report([_result('boot_kernel', 'blocked', value['expected_kernel'], kernel,
                    '예상 커널로 부팅했는지 GRUB/부팅 결과를 확인하세요.')])
            return Report([_result('verification', 'pending', '재부팅 후 검증 기록', kernel,
                                   'ala prepare --apply로 검증을 완료하세요.')])
        return Report([_result('recovery', 'blocked', '성공한 초기 준비 트랜잭션', state,
                    'ala prepare --apply로 현재 RPM 상태를 재조사하고 명시적으로 이어서 진행하세요.',
                    '전원 중단/실패 기록을 자동으로 성공 처리하거나 부팅 후 자동 재개하지 않습니다.')])

    def inspect(self):
        try:
            value = self._load()
            if value:
                return self._checkpoint_report(value)
            return Report([_result('state', 'not_applicable', '선택적인 최초 OS 준비', '아직 실행하지 않음',
                                   'ala prepare --plan; 일반 setup은 초기 준비 없이도 사용할 수 있습니다.')])
        except (OSError, ValueError, TypeError) as exc:
            return Report([_result('record', 'blocked', '유효한 초기 준비 기록', str(exc))])

    def plan(self):
        try:
            value = self._load()
            if value and value['state'] in {'complete', 'reboot_required'}:
                return self._checkpoint_report(value)
            if value and value['state'] == 'verifying':
                return Report([_result('recovery', 'change_required', '성공한 업데이트의 후속 검증',
                    '패키지·커널·이력·정책 검증 필요', 'ala prepare --apply',
                    '업데이트 트랜잭션은 다시 실행하지 않습니다.')])
            results, _, repos = self._preflight()
            if any(r.state in BAD for r in results):
                return Report(results)
            # DNF needs metadata to compute updates. All plan-specific writes are
            # temporary, with plugins disabled (no subscription/plugin hooks).
            with tempfile.TemporaryDirectory(prefix='ala-prepare-plan-') as directory:
                command = self._dnf(repos) + [f'--setopt={key}={directory}/{key}'
                           for key in ('cachedir', 'persistdir', 'logdir')]
                result = self._run(command + ['--refresh', 'check-update'], timeout=120)
            if result.returncode not in (0, 100):
                results.append(_result('updates', 'unknown', '저장소 업데이트 조회', result.stderr or result.stdout,
                                       'DNS·인터넷·저장소·서명 설정을 확인하세요.'))
            else:
                results.append(_result('updates', 'change_required' if result.returncode == 100 else 'ok',
                    '공식 Rocky 10 업데이트', result.stdout.strip()[-24000:], 'ala prepare --apply',
                    '후보 목록이며 최종 의존성 변경은 적용 시 DNF가 결정합니다. OS 패키지 스크립트가 서비스를 재시작할 수 있습니다.'))
            if value:
                results.append(_result('recovery', 'change_required', '중단된 초기 준비의 명시적 재시도', value['state']))
            return Report(results)
        except (OSError, ValueError, TypeError) as exc:
            return Report([_result('record', 'blocked', '초기 준비 조사', str(exc))])

    def _expected_kernel(self, before, after):
        kernels = lambda rows: {f'{v}-{r}.{a}' for n, e, v, r, a in (row.split('\t') for row in rows) if n == 'kernel-core'}
        current = kernels(after)
        response = self._run(['grubby', '--default-kernel'])
        path = response.stdout.strip()
        expected = path.removeprefix('/boot/vmlinuz-')
        if response.returncode or not path.startswith('/boot/vmlinuz-') or not KERNEL.fullmatch(expected) or expected not in current:
            raise ValueError('설치된 기본 부팅 커널을 확인할 수 없습니다. grubby --default-kernel을 확인하세요.')
        added = current - kernels(before)
        if added and expected not in added:
            raise ValueError('새 커널이 설치됐지만 기본 부팅 항목은 이전 커널입니다. GRUB 설정을 확인하세요.')
        return expected

    def _history(self, require_success=True):
        with tempfile.TemporaryDirectory(prefix='ala-prepare-history-') as directory:
            response = self._run(['dnf', '--noplugins', f'--setopt=logdir={directory}',
                                  f'--setopt=cachedir={directory}/cache', 'history', 'info', 'last'], timeout=30)
        if response.returncode:
            raise ValueError('DNF 트랜잭션 이력을 확인할 수 없습니다.')
        match = re.search(r'^Transaction ID\s*:\s*(\d+)\s*$', response.stdout, re.M)
        if not match:
            raise ValueError('DNF 트랜잭션 ID를 읽을 수 없습니다.')
        if require_success and not re.search(r'^Return-Code\s*:\s*Success\s*$', response.stdout, re.M):
            raise ValueError('마지막 DNF 트랜잭션이 성공으로 기록되지 않았습니다. 부분 설치/스크립트 오류를 직접 검토하세요.')
        return int(match.group(1))

    def _ensure_policy(self, event):
        current = Engine(replace(self.config, display=False), host=self.host, paths=self.paths)._updates()
        if any(r.state in BAD or r.state == 'pending' for r in current):
            return current
        if any(r.state == 'change_required' for r in current):
            event('prepare.update_policy', 'started')
            code = self.host.execute(['bash', str(SCRIPTS / 'setup-update-policy.sh')], env=self.env)
            if code:
                return [_result('update_policy', 'error', '자동 업데이트 차단', code)]
        current = Engine(replace(self.config, display=False), host=self.host, paths=self.paths)._updates()
        if any(r.state not in {'ok', 'not_applicable'} for r in current):
            return current
        event('prepare.update_policy', 'completed')
        return []

    def _finish_transaction(self, value, event):
        error = platform_error(self.paths)
        if error:
            return Report([_result('platform', 'blocked', 'Rocky Linux 10 x86_64 유지', error)])
        policy = self._ensure_policy(event)
        if policy:
            return Report(policy)
        after = self._snapshot()
        removed = {row.split('\t')[0] for row in value['before']} - {row.split('\t')[0] for row in after}
        if removed:
            return Report([_result('removed_packages', 'blocked', '기존 패키지 이름 유지', sorted(removed),
                                   '예상하지 않은 패키지 삭제를 DNF 이력에서 확인하세요. 자동 복원하지 않습니다.')])
        expected = self._expected_kernel(value['before'], after)
        changed = sorted(set(value['before']) ^ set(after))
        transaction = self._history() if changed else None
        boot, kernel = self._boot()
        value.update(after=after, changed_packages=changed, transaction_id=transaction,
                     expected_kernel=expected, updated_at=_now())
        if changed or kernel != expected:
            # A retry after a power interruption still compares against the
            # original baseline, so a no-op DNF retry never loses reboot need.
            value.update(state='reboot_required', boot_id=boot)
        else:
            value.update(state='complete', completed_at=_now())
        self._save(value)
        return self._checkpoint_report(value)

    def apply(self, on_stage=None):
        event = on_stage or (lambda *_: None)
        if (self.paths.root != Path('/') and type(self.host) is Host) or (self.paths.root == Path('/') and os.geteuid() != 0):
            return Report([_result('execution', 'blocked', 'root 실행기 또는 모의 Host', '잘못된 실행 환경')])
        try:
            error = platform_error(self.paths)
            if error:
                return Report([_result('platform', 'blocked', 'Rocky Linux 10 x86_64', error)])
            value = self._load()
            if value and value['state'] == 'complete':
                return self._checkpoint_report(value)
            if value and value['state'] == 'reboot_required':
                report = self._checkpoint_report(value)
                if not any(r.id == 'prepare.verification' for r in report.results):
                    return report
                # Ensure the recorded kernel still exists and defaults correctly.
                current = self._snapshot()
                expected = self._expected_kernel(value['before'], current)
                if expected != value['expected_kernel']:
                    return Report([_result('boot_kernel', 'blocked', value['expected_kernel'], expected,
                                          '초기 준비 이후 변경된 커널을 검토하세요.')])
                policy = self._ensure_policy(event)
                if policy:
                    return Report(policy)
                value.update(state='complete', completed_at=_now(), verified_boot_id=self._boot()[0])
                self._save(value)
                event('prepare.verify', 'completed')
                return self._checkpoint_report(value)
            if value and value['state'] == 'verifying':
                return self._finish_transaction(value, event)
            results, snapshot, repos = self._preflight()
            if any(r.state in BAD for r in results):
                event('prepare.preflight', 'blocked')
                return Report(results)
            event('prepare.preflight', 'completed')
            policy = self._ensure_policy(event)
            if policy:
                return Report(policy)
            boot, kernel = self._boot()
            if not value:
                value = dict(schema_version=1, state='upgrading', boot_id=boot,
                             initial_kernel=kernel, before=snapshot, started_at=_now(), attempts=0)
            value.update(state='upgrading', attempts=value.get('attempts', 0) + 1,
                         last_attempt_boot_id=boot, updated_at=_now(), repositories=repos)
            self._save(value)
            event('prepare.upgrade', 'started')
            code = self.host.execute(self._dnf(repos) + ['upgrade', '--refresh', '-y'], env=self.env)
            value.update(dnf_exit_code=code, state='failed' if code else 'verifying', updated_at=_now())
            self._save(value)
            if code:
                event('prepare.upgrade', 'failed')
                # Preserve the original baseline and capture partial mutation.
                try:
                    value['after_failure'] = self._snapshot()
                    value['changed_packages'] = sorted(set(value['before']) ^ set(value['after_failure']))
                    self._save(value)
                    value['transaction_id'] = self._history(require_success=False)
                    self._save(value)
                except (OSError, ValueError):
                    pass
                return Report([_result('upgrade', 'error', '성공한 OS 업데이트', code,
                                       '실행 로그 확인 후 ala prepare --apply로 명시적 재시도')])
            event('prepare.upgrade', 'completed')
            report = self._finish_transaction(value, event)
            event('prepare.verify', 'pending' if report.exit_code == 2 else 'completed' if report.exit_code == 0 else 'failed')
            return report
        except (OSError, ValueError, TypeError) as exc:
            return Report([_result('apply', 'error', '초기 준비 적용·검증', str(exc),
                                   '실행 기록과 현재 상태를 확인한 뒤 ala prepare --apply로 재시도')])
