"""A local setup image while the explicitly requested onboarding is pending.

This separate user service/profile never takes over a configured DID. It needs
a graphical session, but neither monitor coordinates nor an Internet endpoint.
"""
from dataclasses import replace
import os
from pathlib import Path

from .config import Config, ScreenConfig
from .runtime import RuntimeStore
from .system import Host, Paths, atomic_write

TARGET = 'ala-initial-setup'
UNIT = 'ala-setup-preview.service'
MARKER = '# ala managed local setup preview v1'


def unit_text(release):
    from .display import _quote
    command = ' '.join(_quote(v) for v in (release / '.venv/bin/python', '-I', release / 'entry.py', '_setup-preview'))
    return f'''{MARKER}
[Unit]
Description=ALA local setup image before monitor readiness
PartOf=graphical-session.target
After=graphical-session.target
StartLimitIntervalSec=300
StartLimitBurst=3
[Service]
Type=exec
ExecStart={command}
Restart=no
SuccessExitStatus=78
TimeoutStartSec=infinity
TimeoutStopSec=30
KillMode=control-group
'''


def authorized(config, paths, host=None):
    from .display_onboarding import preview_for_user, _eligible
    if (_eligible(config) or any(screen.id == TARGET for screen in config.screen_targets())
            or not preview_for_user(config, paths)):
        return False
    store = RuntimeStore(paths, host)
    if store.load(TARGET) is not None:
        return False
    current = Config.from_dict(store._read(paths.config))
    for screen in current.screen_targets():
        state = store.load(screen.id)
        if state and store.is_stopped(state): return False
    return True


def prepare(config, release, paths, host):
    """Called only by the root onboarding owner with its mutation lock held."""
    store = RuntimeStore(paths, host)
    metadata = store._read(release / 'COMPATIBILITY.json')
    if metadata.get('setup_preview_protocol') != 1:
        return False
    if any(screen.id == TARGET for screen in config.screen_targets()):
        raise ValueError('설정 안내의 예약 ID와 기존 DID가 겹쳐 안내 실행을 보류했습니다.')
    if store.load(TARGET) is not None:
        raise ValueError('설정 안내의 예약 ID가 기존 관리 대상과 겹칩니다.')
    path = paths.at('/etc/systemd/user/' + UNIT)
    store._safe(path)
    content = unit_text(release)
    if path.exists() and (not path.is_file() or MARKER not in path.read_text()):
        raise ValueError('관리되지 않은 설정 안내 서비스를 변경하지 않습니다.')
    if not path.exists() or path.read_text() != content:
        atomic_write(path, content)
    return True


def ensure(config, home, host, paths):
    """Run as the display user; no sudo, no global config changes, no restarts."""
    from . import display
    from .screen_coverage import enabled, ensure as ensure_coverage
    if enabled(config, paths): return ensure_coverage(config, home, host, paths)
    if not authorized(config, paths, host):
        return {'state': 'not_applicable', 'exit_code': 0, 'reason': '설정 중 안내 실행 요청이 없습니다.'}
    if host.run(['test', '-x', '/usr/bin/google-chrome-stable']).returncode:
        return {'state': 'pending', 'exit_code': 2, 'reason': 'Chrome 설치 완료를 기다립니다.'}
    try:
        display._manager(host)
    except RuntimeError:
        return {'state': 'pending', 'exit_code': 2, 'reason': '안내를 표시할 그래픽 환경을 기다립니다.'}
    sessions, _ = display._graphical_sessions(host, os.getuid())
    if (sessions is None or len(sessions) != 1 or sessions[0][1].get('Active') != 'yes'
            or sessions[0][1].get('LockedHint') != 'no'):
        return {'state': 'pending', 'exit_code': 2, 'reason': '단일 활성·잠금 해제 GNOME 세션을 기다립니다.'}
    path = paths.at('/etc/systemd/user/' + UNIT)
    store = RuntimeStore(paths, host)
    store._safe(path)
    if (not path.is_file() or path.stat().st_size > 16384 or MARKER not in path.read_text()
            or not path.read_text().endswith('KillMode=control-group\n')):
        raise ValueError('설정 안내 서비스 등록을 확인하지 못했습니다.')
    # Preserve existing regular or legacy Chrome and every manual stop.
    for screen in config.screen_targets():
        state = store.load(screen.id)
        if state and (store.is_stopped(state) or store.active(state)):
            return {'state': 'blocked', 'exit_code': 2, 'reason': '기존 DID의 수동 중지·작업 상태를 유지합니다.'}
        current = display._unit_state(host, display.unit_name(screen.id))
        if not display._confirmed_unit_state(current) or current.get('ActiveState') != 'inactive':
            return {'state': 'pending', 'exit_code': 2, 'reason': '기존 Chrome의 상태를 유지합니다.'}
    legacy = display._unit_state(host, display.LEGACY)
    if not display._confirmed_unit_state(legacy) or legacy.get('ActiveState') != 'inactive':
        return {'state': 'pending', 'exit_code': 2, 'reason': '기존 표시용 Chrome을 유지합니다.'}
    current = display._unit_state(host, UNIT)
    if not display._confirmed_unit_state(current):
        return {'state': 'unknown', 'exit_code': 2, 'reason': '설정 안내 서비스 상태를 확인하지 못했습니다.'}
    if current.get('ActiveState') in {'active', 'activating'}:
        return {'state': 'kept', 'exit_code': 0, 'reason': '설정 중 안내 실행을 유지합니다.'}
    if current.get('ActiveState') != 'inactive' or current.get('Result') == 'start-limit-hit':
        return {'state': 'pending', 'exit_code': 2, 'reason': '설정 안내의 기존 실패·전환 상태를 확인하세요.'}
    if host.run(['systemctl', '--user', 'daemon-reload']).returncode:
        return {'state': 'pending', 'exit_code': 2, 'reason': '설정 안내 등록 재읽기를 확인하지 못했습니다.'}
    loaded = host.run(['systemctl', '--user', 'show', UNIT, '--property=FragmentPath', '--property=LoadState'])
    info = display._properties(loaded.stdout)
    if loaded.returncode or info.get('LoadState') != 'loaded' or info.get('FragmentPath') != '/etc/systemd/user/' + UNIT:
        return {'state': 'blocked', 'exit_code': 2, 'reason': '설정 안내 서비스의 관리 등록 경로를 확인하세요.'}
    result = host.run(['systemctl', '--user', 'start', '--no-block', UNIT])
    return {'state': 'starting' if result.returncode == 0 else 'pending', 'exit_code': 0 if result.returncode == 0 else 2,
            'reason': '로컬 설정 중 안내의 시작을 요청했습니다. 실제 화면은 별도 확인합니다.'}


def run(*, config=None, home=None, host=None, paths=None, runner=None):
    from . import display
    paths, host = paths or Paths(), host or Host()
    if config is None:
        config, home = display._load_user_config(paths, None)
    elif os.getuid() == 0:
        raise PermissionError('설정 안내 Chrome은 일반 사용자로 실행하세요.')
    if not authorized(config, paths, host):
        return 78
    root_config = config
    preview = replace(config, schema_version=2, url=None,
                      screens=(ScreenConfig(id=TARGET, url=None, autostart=True),)).for_screen(TARGET)
    def stopped():
        try: return not authorized(root_config, paths, host)
        except (OSError, ValueError, RuntimeError): return True
    return (runner or display.run_chrome)(target=TARGET, config=preview, home=home, host=host, paths=paths,
                                         setup_preview=True, stop_requested=stopped)
