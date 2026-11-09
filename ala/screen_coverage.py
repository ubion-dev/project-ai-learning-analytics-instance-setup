"""Session-owned waiting windows for connected outputs without a managed page.

Never edits DID configuration or starts/stops a regular DID. The root-installed
user service owns only its own child supervisors and their private profiles.
"""
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import time

from .config import Config, ScreenConfig
from .runtime import RuntimeStore
from .system import Host, Paths, atomic_write

UNIT = 'ala-screen-coverage.service'
MARKER = '# ala managed output waiting windows v1'


class ChangePending(RuntimeError):
    pass


def identifier(output):
    if output is not None and (not isinstance(output, str) or not output or len(output) > 128
                               or any(ord(c) < 32 or ord(c) == 127 for c in output)):
        raise ValueError('대기 화면의 출력 식별자가 올바르지 않습니다.')
    ScreenConfig(id='default', output=output)
    return 'ala-wait-' + hashlib.sha256((output or 'session').encode()).hexdigest()[:20]


def policy_path(paths):
    return paths.at('/var/lib/ala-runtime/screen-coverage.json')


def enabled(config, paths):
    path = policy_path(paths)
    if not path.exists() and not path.is_symlink(): return False
    value = RuntimeStore(paths)._read(path)
    if not isinstance(value, dict) or type(value.get('protocol')) is not int or value.get('protocol') != 1 or type(value.get('enabled')) is not bool:
        raise ValueError('출력별 대기 화면의 관리 정책을 확인하세요.')
    return value['enabled'] and config.display and value.get('user') == config.user


def prepare(config, release, paths, host):
    """Root provisioning, under setup's shared mutation lock."""
    store = RuntimeStore(paths, host)
    metadata = Path(release) / 'COMPATIBILITY.json'
    if not metadata.is_file() or store._read(metadata).get('screen_coverage_protocol') != 1:
        return False
    from .display import _quote
    command = ' '.join(_quote(v) for v in (Path(release)/'.venv/bin/python', '-I', Path(release)/'entry.py', '_screen-coverage'))
    content = f'''{MARKER}
[Unit]
Description=ALA waiting image on each connected output
PartOf=graphical-session.target
After=graphical-session.target
StartLimitIntervalSec=300
StartLimitBurst=3
[Service]
Type=exec
ExecStart={command}
Restart=on-failure
RestartSec=10
SuccessExitStatus=78
RestartPreventExitStatus=78
TimeoutStopSec=60
KillMode=control-group
'''
    path = paths.at('/etc/systemd/user/' + UNIT)
    store._safe(path)
    if path.exists() and (not path.is_file() or MARKER not in path.read_text()):
        raise ValueError('관리되지 않은 출력별 대기 서비스를 변경하지 않습니다.')
    if not path.exists() or path.read_text() != content: atomic_write(path, content)
    store._write(policy_path(paths), {'protocol': 1, 'enabled': config.display, 'user': config.user})
    return True


def ensure(config, home, host, paths):
    from . import display
    if not enabled(config, paths): return {'state': 'not_applicable', 'exit_code': 0}
    path = paths.at('/etc/systemd/user/' + UNIT)
    RuntimeStore(paths, host)._safe(path)
    if not path.is_file() or path.stat().st_size > 16384 or MARKER not in path.read_text():
        raise ValueError('출력별 대기 서비스 등록을 확인하세요.')
    current = display._unit_state(host, UNIT)
    if not display._confirmed_unit_state(current): return {'state': 'unknown', 'exit_code': 2}
    running = current.get('ActiveState') in {'active', 'activating'}
    if not running and (current.get('ActiveState') != 'inactive' or current.get('Result') == 'start-limit-hit'):
        return {'state': 'pending', 'exit_code': 2}
    if not running and host.run(['systemctl', '--user', 'daemon-reload']).returncode:
        return {'state': 'pending', 'exit_code': 2}
    loaded = host.run(['systemctl', '--user', 'show', UNIT, '--property=FragmentPath', '--property=LoadState'])
    info = display._properties(loaded.stdout)
    if loaded.returncode or info.get('LoadState') != 'loaded' or info.get('FragmentPath') != '/etc/systemd/user/' + UNIT:
        return {'state': 'blocked', 'exit_code': 2, 'reason': '대기 서비스가 root 관리 등록 경로와 다릅니다.'}
    if not running:
        result = host.run(['systemctl', '--user', 'start', '--no-block', UNIT])
        if result.returncode: return {'state': 'pending', 'exit_code': 2, 'reason': '출력별 대기 서비스 시작을 확인하세요.'}
    from .monitors import bridge
    state = bridge('GetState', host=host)
    if state.get('state') != 'ok' or 'output-waiting-windows' not in state.get('capabilities', []):
        return {'state': 'pending', 'exit_code': 2,
                'reason': '대기 서비스 시작은 요청했습니다. 확장 파일 준비 후 GNOME에서 로그아웃·로그인해 출력별 안내 배치를 반영하세요.'}
    return {'state': 'kept' if running else 'starting', 'exit_code': 0}


def bound_context(config, inventory):
    """Use the primary output for an unassigned legacy default, without saving."""
    if config.target_id == 'default' and config.output is None:
        primary = [o for o in inventory.get('logical_outputs', []) if o.get('confirmed') and o.get('primary')]
        if len(primary) == 1: object.__setattr__(config, '_output', primary[0]['id'])
    return config


def waiting_config(config, output):
    target = identifier(output)
    return replace(config, schema_version=2, url=None, runtime_pin=None,
                   screens=(ScreenConfig(id=target, output=output, autostart=True),)).for_screen(target)


def desired(config, inventory, *, states, observations):
    """Pure coverage policy. Configured sites never exclude other outputs."""
    from .monitors import resolve_output
    if not config.display: return set()
    if inventory.get('state') != 'ok':
        return set() if any(s and s.get('suppressed') for c, s in states.values()) else {None}
    outputs = {o['id'] for o in inventory.get('logical_outputs', []) if o.get('confirmed')}
    if not outputs: return {None}
    if len(outputs) > 16: raise ValueError('출력별 안내는 최대 16개 논리 출력을 지원합니다.')
    for screen in config.screen_targets():
        context, state = states[screen.id]
        resolved = resolve_output(inventory, context.output, auto=screen.id == 'default')
        if resolved.get('state') != 'ok': continue
        output = resolved['output']['id']
        if not context.display or (state and state.get('suppressed') is True):
            outputs.discard(output)
            continue
        observed = observations.get(screen.id) or {}
        placement = observed.get('placement') or {}
        if observed.get('process_verified') is True and placement.get('state') == 'ok' and placement.get('output') in {output, resolved['selector']}:
            outputs.discard(output)
    return outputs


def frame(config, home, host, paths):
    from . import display, monitors
    from .screen_swap import guard
    from .display_reset import guard as reset_guard
    try:
        guard(paths=paths, host=host)
        reset_guard(paths=paths, host=host)
    except (ValueError, RuntimeError) as exc:
        raise ChangePending('화면 변경·복구가 끝난 뒤 대기 안내를 재개합니다.') from exc
    inventory = monitors.topology(host=host)
    store = RuntimeStore(paths, host)
    states, observations = {}, {}
    bounded = display.BudgetHost(host, 8)
    for screen in config.screen_targets():
        context, state = display._committed_screen(config, screen.id, store)
        context = bound_context(context, inventory)
        suppressed = bool(state and (store.is_stopped(state) or store.active(state)))
        states[screen.id] = (context, {'suppressed': suppressed})
        if suppressed: continue
        try:
            checked = display._check_user(context, Path(home), bounded, paths)
            if checked.get('process_verified') is True:
                checked['placement'] = monitors.placement(context, checked.get('health') or {}, host=bounded, inventory=inventory)
            observations[screen.id] = checked
        except (OSError, ValueError, RuntimeError, KeyError): pass
    wanted = desired(config, inventory, states=states, observations=observations)
    return None if inventory.get('state') != 'ok' and wanted else wanted


def authorized(config, output, paths, host=None):
    target = identifier(output)
    if not enabled(config, paths) or any(s.id == target for s in config.screen_targets()): return False
    store = RuntimeStore(paths, host)
    if store.load(target) is not None: return False
    for screen in config.screen_targets():
        if screen.output == output:
            state = store.load(screen.id)
            if state and store.is_stopped(state): return False
    return True


def run_wait(output, *, config=None, home=None, host=None, paths=None, runner=None):
    from . import display
    host, paths = host or Host(), paths or Paths()
    if config is None: config, home = display._load_user_config(paths, None)
    elif os.getuid() == 0: raise PermissionError('대기 Chrome은 일반 사용자로 실행하세요.')
    if not authorized(config, output, paths, host): return 78
    def stopped():
        try:
            current = Config.from_dict(RuntimeStore(paths, host)._read(paths.config))
            return current.user != config.user or not authorized(current, output, paths, host)
        except (OSError, ValueError, RuntimeError, KeyError): return True
    return (runner or display.run_chrome)(config=waiting_config(config, output), home=home, host=host, paths=paths,
                                         screen_wait=True, stop_requested=stopped)


class Children:
    """Only child handles created here can ever receive terminate()."""
    def __init__(self, spawn, clock=time.monotonic):
        self.spawn, self.clock, self.running, self.attempts = spawn, clock, {}, {}

    def reconcile(self, wanted):
        now = self.clock()
        for output, child in list(self.running.items()):
            if child.poll() is not None: del self.running[output]
            elif output not in wanted: child.terminate()
        for output in wanted:
            if output in self.running: continue
            attempts = [t for t in self.attempts.get(output, []) if now-t < 300]
            self.attempts[output] = attempts
            if len(attempts) >= 3: continue
            attempts.append(now)
            try:
                self.running[output] = self.spawn(output)
            except OSError:
                # A failed output must not close the other waiting windows.
                continue


def run(*, host=None, paths=None, sleeper=time.sleep):
    from . import display
    host, paths = host or Host(), paths or Paths()
    config, home = display._load_user_config(paths, None)
    if not enabled(config, paths): return 78
    stopped = False
    def stop(*_):
        nonlocal stopped
        stopped = True
    previous = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT)}
    def spawn(output):
        argv = [str(display.RELEASE/'.venv/bin/python'), '-I', str(display.RELEASE/'entry.py'), '_screen-wait']
        if output is not None: argv.append(output)
        environment = {key: value for key, value in os.environ.items()
                       if key not in {'ALA_RUNTIME_CONFIG', 'ALA_RUNTIME_TARGET', 'ALA_RUNTIME_ACTION'}}
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, close_fds=True, env=environment)
    children = Children(spawn)
    try:
        while not stopped:
            current, _ = display._load_user_config(paths, None)
            if current.user != config.user or not enabled(current, paths): break
            try:
                wanted = frame(current, home, host, paths)
                if wanted is None: wanted = set(children.running) or {None}
                wanted = {o for o in wanted if authorized(current, o, paths, host)}
                if None in children.running and wanted and None not in wanted:
                    from .display_health import read_health
                    from .monitors import placement
                    for output in wanted:
                        child = children.running.get(output)
                        if child is None or child.poll() is not None:
                            wanted.add(None)
                            break
                        health = read_health(home, paths, child.pid, None, target_id=identifier(output))
                        if (health.get('local_page_ready') is not True or
                                placement(waiting_config(current, output), health, host=host).get('state') != 'ok'):
                            wanted.add(None)
                            break
            except ChangePending:
                wanted = set()
            except (OSError, ValueError, RuntimeError, KeyError):
                # Never create unplaced duplicates on an ambiguous observation.
                wanted = set(children.running)
            children.reconcile(wanted)
            for _ in range(5):
                if stopped: break
                sleeper(1)
        return 0
    finally:
        children.reconcile(set())
        for child in children.running.values():
            try: child.wait(timeout=10)
            except subprocess.TimeoutExpired: pass  # systemd owns the entire service cgroup.
        for signum, handler in previous.items(): signal.signal(signum, handler)
