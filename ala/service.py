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
            return Config(user=user if user != 'root' else '')
        info = self.paths.config.lstat()
        if self.paths.config.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise PermissionError('구성 파일 소유자/쓰기 권한을 확인하세요.')
        return Config.load(self.paths.config)

    def _enroll_before_change(self, config):
        """Observe outside the mutation lock; never guess an old running release."""
        from .runtime import RuntimeStore, bootstrap_from_observation
        from .screens import screen_action
        store = RuntimeStore(self.paths, self.host)
        if not self.paths.config.exists():
            return
        missing = [s for s in config.screen_targets() if store.load(s.id) is None]
        if not missing or self.paths.root != Path('/'):
            return
        reports = {s.id: screen_action('status', config, target=s.id, host=self.host, paths=self.paths)
                   for s in missing}
        with change_lock(self.paths):
            if self.config() != config:
                raise RuntimeError('구성이 변경됐습니다. 다시 읽은 뒤 작업하세요.')
            result = bootstrap_from_observation(config, reports, release=self.release,
                       paths=self.paths, host=self.host, locked=True, skip_inactive=True)
            if result.get('state') != 'completed':
                raise RuntimeError('기존 화면 관리 버전을 확인하지 못해 현재 표시를 유지했습니다. 점검 시간에 해당 창을 중지한 뒤 선택 화면 코드 적용을 다시 실행하세요.')

    def _check_runtime_change(self, targets):
        from .runtime import RuntimeStore
        store = RuntimeStore(self.paths, self.host)
        for target in targets:
            state = store.load(target)
            if state is not None and store.active(state):
                raise RuntimeError('선택 대상의 화면 작업이 진행 중입니다. 완료 후 구성을 저장하세요.')

    def _check_target_preservation(self, current, new):
        from .runtime import RuntimeStore
        from .display_reset import read_fence
        retired=set((read_fence(self.paths) or {}).get('retired',[]))
        if retired & {screen.id for screen in new.screen_targets()}:
            raise ValueError('초기화에서 제외된 ID는 복구용으로 보존합니다. 다른 ID로 화면을 추가하세요.')
        states = RuntimeStore(self.paths, self.host).selections()
        if states and current.user != new.user:
            raise ValueError('등록된 화면 계정 변경은 별도 이전이 필요합니다. 현재 계정과 표시를 유지했습니다.')
        if {s['target_id'] for s in states} - {s.id for s in new.screen_targets()}:
            raise ValueError('등록된 표시 대상을 구성에서 삭제할 수 없습니다. 자동 시작을 끄고 기존 대상을 보존하세요.')

    def _sync_runtime_configuration(self, config, targets=None):
        """Called inside the common lock after an explicit configuration save."""
        from .runtime import RuntimeStore
        store = RuntimeStore(self.paths, self.host)
        # Source-checkout Operations in tests are not an installed product.
        # Installed temporary release fixtures exercise the full same path.
        release = self.release.resolve()
        if not release.is_relative_to(self.paths.at('/opt/ala/releases').resolve()):
            return
        selected = set(targets) if targets is not None else {s.id for s in config.screen_targets()}
        for screen in config.screen_targets():
            if screen.id not in selected:
                continue
            if store.load(screen.id) is None:
                store.bootstrap(screen.id, release, config.to_dict())
            else:
                store.stage(screen.id, release, config.to_dict())

    def dispatch(self, action, payload):
        if action == 'monitor-extension-refresh':
            from .display import _install_extension, _user_call
            from .screens import inspect_session
            from .display_reset import guard as reset_guard
            from .screen_swap import guard as swap_guard
            with change_lock(self.paths):
                reset_guard(paths=self.paths, host=self.host)
                swap_guard(paths=self.paths, host=self.host)
                config = self.config()
                if inspect_session(config, self.host) != 'ok':
                    raise RuntimeError('활성·잠금 해제 GNOME 화면 계정을 확인하세요.')
                _install_extension(self.paths)
                result = _user_call(config, self.host, self.paths, 'extension-enable')
            return result | {'reason': '확장 파일을 갱신했습니다. GNOME에서 한 번 로그아웃한 뒤 다시 로그인하세요. SSH를 끊는 것과 다릅니다.'}
        if action in {'monitor-profile-status', 'monitor-profile-disable', 'monitor-profile-retry'}:
            from . import monitor_profiles
            if action == 'monitor-profile-status':
                return monitor_profiles.status(paths=self.paths, host=self.host)
            if action == 'monitor-profile-disable':
                return monitor_profiles.disable(paths=self.paths, host=self.host)
            return monitor_profiles.restore(release=self.release, paths=self.paths, host=self.host, retry=True)
        if action in {'monitor-layout-submit', 'monitor-layout-status', 'monitor-layout-confirm',
                      'monitor-layout-revert', 'monitor-layout-recover'}:
            from . import monitor_change
            if action == 'monitor-layout-submit':
                return monitor_change.submit(payload.get('layout'), persistent=payload.get('persistent', False),
                                             release=self.release, paths=self.paths, host=self.host)
            if action == 'monitor-layout-status':
                return monitor_change.status(paths=self.paths, host=self.host)
            if action == 'monitor-layout-recover':
                return monitor_change.recover(payload.get('operation_id'), paths=self.paths, host=self.host)
            return monitor_change.decide(payload.get('operation_id'),
                'confirm' if action == 'monitor-layout-confirm' else 'revert', paths=self.paths, host=self.host)
        if action in {'screens-reset-preview','screens-reset-submit','screens-reset-status','screens-reset-recover'}:
            from . import display_reset
            if action == 'screens-reset-preview':
                return display_reset.preview(self.config(),release=self.release,paths=self.paths,host=self.host)
            if action == 'screens-reset-submit':
                return display_reset.submit(payload.get('review'),release=self.release,paths=self.paths,host=self.host)
            if action == 'screens-reset-recover':
                return display_reset.recover(payload.get('operation_id'),release=self.release,paths=self.paths,host=self.host)
            return display_reset.status(paths=self.paths,host=self.host,operation_id=payload.get('operation_id'))
        if action in {'screens-swap-submit', 'screens-swap-status', 'screens-swap-retry'}:
            from . import screen_swap
            if action == 'screens-swap-status':
                return screen_swap.status(paths=self.paths, host=self.host,
                                          target=payload.get('target'), operation_id=payload.get('operation_id'))
            if action == 'screens-swap-retry':
                return screen_swap.retry(payload.get('operation_id'), release=self.release, paths=self.paths, host=self.host)
            return screen_swap.submit(payload.get('source'), payload.get('destination'),
                                      payload.get('expected_config'), payload.get('expected_topology'),
                                      release=self.release, paths=self.paths, host=self.host)
        if action in {'display-initialization-preview', 'display-initialization-status'}:
            from . import display_onboarding
            if action.endswith('preview'):
                return display_onboarding.preview(self.config(), paths=self.paths, host=self.host)
            return display_onboarding.status(paths=self.paths, host=self.host)
        if action == 'screens-identify':
            from .screens import inspect_session
            from .display import _user_call
            config = self.config()
            session = inspect_session(config, self.host)
            if session != 'ok':
                return {'state': 'pending', 'exit_code': 2, 'reason': '단일 활성·잠금 해제 GNOME 세션이 필요합니다.',
                        'next_action': '화면 계정의 GNOME 로그인과 잠금 상태를 확인하세요.'}
            return _user_call(config, self.host, self.paths, 'identify-outputs')
        if action in {'auto-update-status', 'auto-update-bootstrap', 'auto-update-enable', 'auto-update-disable'}:
            from . import auto_update
            if action == 'auto-update-status':
                return auto_update.status(paths=self.paths, host=self.host)
            if action == 'auto-update-bootstrap':
                return auto_update.submit_bootstrap(self.release, paths=self.paths, host=self.host)
            if action == 'auto-update-enable':
                if type(payload.get('accepted', False)) is not bool:
                    raise ValueError('장비 수용 확인은 boolean이어야 합니다.')
                return auto_update.enable(accepted=payload.get('accepted', False), paths=self.paths, host=self.host)
            return auto_update.disable(paths=self.paths, host=self.host)
        if action == 'runtime-status':
            from .runtime import RuntimeStore, snapshot
            if payload.get('target') is not None:
                return snapshot(payload['target'], paths=self.paths, host=self.host) or {'state': 'pending', 'reason': '최초 화면 관리 버전 등록 대기'}
            return {'targets': RuntimeStore(self.paths, self.host).selections()}
        if action in {'runtime-stage', 'runtime-apply'}:
            from .config import ScreenConfig
            from .runtime import RuntimeStore, submit_apply
            target = payload.get('target', 'default')
            ScreenConfig(id=target)
            config = self.config()
            config.for_screen(target)
            if RuntimeStore(self.paths, self.host).load(target) is None:
                from .screens import screen_action
                from .runtime import bootstrap_from_observation
                reports = {s.id: screen_action('status', config, target=s.id, host=self.host, paths=self.paths)
                           for s in config.screen_targets()}
                enrolled = bootstrap_from_observation(config, reports, release=self.release, paths=self.paths, host=self.host)
                if enrolled.get('state') != 'completed':
                    raise RuntimeError('기존 실행 버전을 확인하지 못해 표시를 유지했습니다. 점검 시간에 해당 창을 중지한 뒤 다시 적용하세요.')
            with change_lock(self.paths):
                if self.config() != config:
                    raise RuntimeError('구성이 변경됐습니다. 다시 확인하세요.')
                store = RuntimeStore(self.paths, self.host)
                if store.load(target) is None:
                    raise RuntimeError('최초 화면 관리 버전을 먼저 등록하세요.')
                result = store.stage(target, self.release, config.to_dict())
            if action == 'runtime-stage':
                return result
            return submit_apply(target, release=self.release, paths=self.paths, host=self.host)
        if action in {'monitor-layout-state', 'monitor-layout-plan'}:
            from .screens import _inventory, inspect_session
            from .monitor_layout import build_plan
            config = self.config()
            session = inspect_session(config, self.host)
            if session != 'ok':
                return {'state': 'pending', 'session': session, 'exit_code': 2,
                        'apply_supported': False,
                        'reason': '화면 계정의 활성 GNOME 세션 하나와 잠금 해제를 확인하세요.'}
            inventory = _inventory(config, self.host, self.paths)
            settings = inventory.get('layout_settings', {'state': 'unknown'})
            if inventory.get('state') != 'ok' or settings.get('state') != 'ok':
                return {'state': 'unknown', 'exit_code': 2, 'apply_supported': False,
                        'reason': settings.get('reason') or inventory.get('reason') or
                                  '모니터별 지원 모드·배율을 확인하지 못했습니다.'}
            if action == 'monitor-layout-state':
                return settings | {'trial_supported': 'monitor-layout-trial' in inventory.get('capabilities', [])}
            return build_plan(settings, payload.get('layout'))
        if action == 'screens-inspect':
            from .screens import inspect_screens, inspect_session
            from .runtime import settings_status, snapshot
            from .frontend import read_status
            from .screen_control import read_status as reload_status
            started = perf_counter()
            host = InspectionHost(self.host)
            config = self.config()
            value = inspect_screens(config, host=host, paths=self.paths, batch=True)
            observed_at = perf_counter()
            if payload.get('quick') is True:
                value['session'] = inspect_session(config, host)
            frontend = read_status(paths=self.paths)
            from . import display_onboarding, screen_swap
            value['initialization'] = display_onboarding.status(paths=self.paths, host=host)
            from .display_reset import status as reset_status
            value['reset'] = reset_status(paths=self.paths,host=host)
            def enrich(target):
                target['swap'] = screen_swap.status(paths=self.paths, host=host, target=target['id'])
                target['reload'] = reload_status(target['id'], paths=self.paths, host=host)
                target['frontend_status'] = frontend.get('targets', {}).get(target['id'], {
                    'state': 'unknown' if frontend.get('state') == 'unknown' else 'pending',
                    'reason': frontend.get('reason') or '배포 연동 미관측'})
                try:
                    state = snapshot(target['id'], host=host, paths=self.paths)
                    target['runtime'] = state or {
                        'state': 'pending', 'reason': '관리 버전 최초 등록 대기'}
                    settings = settings_status(config, target['id'], host=host, paths=self.paths, state=state)
                    for key in ('applied_url', 'applied_frontend'):
                        if key in settings:
                            target[key] = settings.pop(key)
                    target['settings'] = settings
                except (OSError, ValueError, RuntimeError) as exc:
                    target['runtime'] = {'state': 'unknown', 'reason': str(exc)}
                    target['settings'] = {'state': 'unknown', 'pending': False,
                                          'reason': '화면의 확정 설정을 확인하지 못했습니다.'}
                return target
            from concurrent.futures import ThreadPoolExecutor
            targets = value.get('targets', [])
            if targets:
                with ThreadPoolExecutor(max_workers=min(4, len(targets))) as pool:
                    value['targets'] = list(pool.map(enrich, targets))
            finished = perf_counter()
            value['query_timing'] = {'observation_seconds': observed_at-started,
                                     'metadata_seconds': finished-observed_at,
                                     'total_seconds': finished-started}
            return value
        if action == 'screens-apply':
            from .runtime import submit_apply
            expected = payload.get('expected_config')
            if 'expected_config' in payload and not isinstance(expected, dict):
                raise ValueError('검토한 화면 구성은 object여야 합니다.')
            def current_settings():
                config = self.config()
                if expected is not None and config.to_dict() != expected:
                    raise RuntimeError('검토 후 구성이 변경되었습니다. 화면을 다시 확인한 뒤 적용하세요.')
                return config
            return submit_apply(payload.get('target', 'default'), release=self.release, paths=self.paths,
                                host=self.host, settings_config=current_settings)
        if action == 'frontend-retry':
            from .config import ScreenConfig
            target = payload.get('target')
            ScreenConfig(id=target)
            self.config().for_screen(target)
            release = trusted_release(self.release)
            with change_lock(self.paths):
                if any(r['state'] in {'queued', 'running'} for r in JobStore(self.paths, self.host).list()):
                    raise RuntimeError('설치 중에는 프론트 갱신을 시작하지 않습니다.')
                response = self.host.run(['systemd-run', '--quiet', '--collect',
                    '--unit=ala-frontend-retry-'+target, '--property=Type=oneshot',
                    str(release / '.venv/bin/python'), '-I', str(release / 'entry.py'),
                    '_frontend-worker', json.dumps({'retry': True, 'target_id': target})])
            return {'state': 'pending' if response.returncode == 0 else 'error',
                    'exit_code': 2 if response.returncode == 0 else 1,
                    'reason': '선택 대상의 배포 재시도를 접수했습니다. 실제 적용 상태를 다시 확인합니다.'
                              if response.returncode == 0 else '배포 재시도 작업을 시작하지 못했습니다. 실행 중 작업과 서비스를 확인하세요.'}
        if action == 'screens-save':
            from .config import ScreenConfig
            target = ScreenConfig.from_dict(payload.get('target'))
            before = self.config()
            if payload.get('expected_config') != before.to_dict():
                raise RuntimeError('다른 작업에서 구성이 변경됐습니다. 다시 읽은 뒤 저장하세요.')
            self._enroll_before_change(before)
            with change_lock(self.paths):
                from .screen_swap import guard
                guard(paths=self.paths, host=self.host)
                current = self.config()
                if payload.get('expected_config') != current.to_dict():
                    raise RuntimeError('다른 작업에서 구성이 변경됐습니다. 다시 읽은 뒤 저장하세요.')
                if any(r['state'] in {'queued', 'running'} for r in JobStore(self.paths, self.host).list()):
                    raise RuntimeError('설치 작업 중에는 구성을 변경할 수 없습니다.')
                value = current.to_dict()
                targets = [s.to_dict() for s in current.screen_targets()]
                existing = next((i for i, s in enumerate(targets) if s['id'] == target.id), None)
                if existing is None:
                    targets.append(target.to_dict())
                else:
                    targets[existing] = target.to_dict()
                value.update(schema_version=2, screens=targets)
                config = Config.from_dict(value)
                from .runtime import guard_config_change, RuntimeStore
                guard_config_change(current, config, paths=self.paths, host=self.host)
                self._check_target_preservation(current, config)
                self._check_runtime_change([target.id])
                protected_directory(self.paths.config.parent, 0o755)
                store = RuntimeStore(self.paths, self.host)
                registered = store.load(target.id) is not None
                if config != current:
                    if registered:
                        store.settings_saved(target.id)
                    atomic_json(self.paths.config, config.to_dict(), 0o644)
                if not registered:
                    self._sync_runtime_configuration(config, [target.id])
            return {'saved': True, 'config': config.to_dict(), 'state': 'pending', 'exit_code': 2,
                    'reason': ('새 화면을 저장했습니다. 간편 설정에서 화면을 등록한 뒤 시작하세요.' if existing is None
                               else '구성을 저장했습니다. 화면 관리에서 설정 적용을 선택하세요.')}
        if action == 'screens-login-logs':
            from .display import _gdm_diagnostics
            self.config().for_screen(payload.get('target', 'default'))
            status, journal = _gdm_diagnostics(self.host)
            lines = [status.actual, *(journal.actual if isinstance(journal.actual, list) else [journal.actual])]
            return {'log': '\n'.join(lines), 'exit_code': int(journal.state == 'unknown')}
        if action == 'screens-action':
            from .screens import screen_action
            name = payload.get('action')
            if name not in {'start', 'stop', 'restart', 'reload', 'status', 'logs', 'check'}:
                raise ValueError('허용되지 않은 화면 작업입니다.')
            target = payload.get('target', 'default')
            if name == 'reload':
                from .screen_swap import guard
                guard(paths=self.paths, host=self.host, target=target)
                from .screen_control import submit_reload
                return submit_reload(self.config(), target=target, release=self.release, paths=self.paths, host=self.host)
            if name in {'start', 'stop', 'restart'}:
                with change_lock(self.paths, blocking=name == 'stop'):
                    from .screen_swap import guard, note_stop
                    if name == 'stop':
                        note_stop(target, paths=self.paths, host=self.host)
                    else:
                        guard(paths=self.paths, host=self.host, target=target)
                    if any(r['state'] in {'queued', 'running'} for r in JobStore(self.paths, self.host).list()):
                        raise RuntimeError('설치 중에는 화면 변경 작업을 하지 않습니다.')
                    if name in {'start', 'stop', 'restart'}:
                        from .runtime import RuntimeStore
                        store = RuntimeStore(self.paths, self.host)
                        if store.load(target) is not None:
                            store.cancel(target, stopped=name == 'stop')
                    return screen_action(name, self.config(), target=target, host=self.host, paths=self.paths)
            return screen_action(name, self.config(), target=target, host=self.host, paths=self.paths)
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
            from .display_onboarding import status
            return {'config': self.config().to_dict(), 'exists': self.paths.config.exists(),
                    'display_initialization': status(paths=self.paths, host=self.host)}
        if action in {'config-save', 'config-save-reviewed'}:
            reviewed = action == 'config-save-reviewed'
            config = Config.from_dict(payload.get('config') if reviewed else payload)
            if config.user:
                record = pwd.getpwnam(config.user)
                if record.pw_uid == 0 or not Path(record.pw_dir).is_dir():
                    raise ValueError('존재하는 일반 사용자와 홈이 필요합니다.')
            before = self.config()
            if reviewed and payload.get('expected_config') != before.to_dict():
                raise RuntimeError('검토 후 구성이 변경됐습니다. 다시 읽은 뒤 저장하세요.')
            self._enroll_before_change(before)
            with change_lock(self.paths):
                from .screen_swap import guard
                guard(paths=self.paths, host=self.host)
                if self.config() != before:
                    raise RuntimeError('다른 작업에서 구성이 변경됐습니다. 다시 읽은 뒤 저장하세요.')
                if any(r['state'] in {'queued', 'running'} for r in JobStore(self.paths, self.host).list()):
                    raise RuntimeError('설치 작업 중에는 구성을 변경할 수 없습니다.')
                from .runtime import guard_config_change
                guard_config_change(before, config, paths=self.paths, host=self.host)
                self._check_target_preservation(before, config)
                self._check_runtime_change([s.id for s in config.screen_targets()])
                protected_directory(self.paths.config.parent, 0o755)
                if not self.paths.config.exists() or self.config() != config or self.paths.config.stat().st_mode & 0o777 != 0o644:
                    atomic_json(self.paths.config, config.to_dict(), 0o644)
                    self._sync_runtime_configuration(config)
                else:
                    from .runtime import RuntimeStore
                    store = RuntimeStore(self.paths, self.host)
                    missing = [s.id for s in config.screen_targets() if store.load(s.id) is None]
                    if missing:
                        self._sync_runtime_configuration(config, missing)
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
                report['configuration_report'] = engine.configuration_report.to_dict()
                for name, query in (('runs', lambda: JobStore(self.paths, host).list()),
                                    ('preparation', lambda: Prepare(config, host=host, paths=self.paths).inspect().to_dict())):
                    since = perf_counter()
                    report[name] = query()
                    inspection['stages'].append({'id': name, 'duration_ms': round(max(0.0, (perf_counter() - since) * 1000), 3)})
            inspection['duration_ms'] = round(max(0.0, (perf_counter() - started) * 1000), 3)
            report['inspection'] = inspection
            return report
        if action == 'podman-plan':
            from .podman import inspect_cleanup
            with change_lock(self.paths):
                if any(r['state'] in {'queued', 'running'} for r in JobStore(self.paths, self.host).list()):
                    raise RuntimeError('설치 작업이 끝난 뒤 Podman 사용 여부를 확인하세요.')
                return inspect_cleanup(self.host, self.release)
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
                                                         expected_config=payload.get('expected_config'), persist_config=True,
                                                         initialize_displays=payload.get('initialize_displays', not self.paths.config.exists() and self.config().display),
                                                         bash_assist=payload.get('bash_assist', False),
                                                         podman_cleanup=payload.get('podman_cleanup'))
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
                if payload.get('action') != 'reload':
                    raise ValueError('허용되지 않은 Chrome 작업입니다.')
            return self.dispatch('screens-action', payload)
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
