"""GNOME session-bound Chrome display; root provisioning and user execution stay separate."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import os
import pwd
import re
import stat
import time
import urllib.error
import urllib.request

from .config import Config
from .model import CheckResult, Report
from .system import Host, Paths, atomic_json, atomic_write

UNIT = 'ala-chrome.service'
LEGACY = 'did-chrome.service'
RELEASE = Path(__file__).resolve().parents[1]
EXTENSION_SOURCE = Path(__file__).resolve().parents[1] / 'assets/gnome-extension'
DISPLAY_LAUNCHER = Path('/usr/local/bin/ala')
MARKER = '# ala managed automatic login'
DESKTOP_MARKER = '# ala managed display autostart'
UNIT_MARKER = '# ala managed display service'
ENV_NAMES = ('DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'XDG_CURRENT_DESKTOP', 'XDG_SESSION_TYPE', 'XDG_SESSION_ID')
SETTINGS = (
    ('org.gnome.desktop.session', 'idle-delay', 'uint32 0'),
    ('org.gnome.desktop.screensaver', 'lock-enabled', 'false'),
    ('org.gnome.settings-daemon.plugins.power', 'idle-dim', 'false'),
    ('org.gnome.settings-daemon.plugins.power', 'sleep-inactive-ac-type', "'nothing'"),
    ('org.gnome.settings-daemon.plugins.power', 'sleep-inactive-battery-type', "'nothing'"),
    ('org.gnome.software', 'allow-updates', 'false'),
    ('org.gnome.software', 'download-updates', 'false'),
    ('org.gnome.software', 'download-updates-notify', 'false'),
)
WORKSPACE_SETTINGS = (
    ('org.gnome.mutter', 'dynamic-workspaces', 'false'),
    ('org.gnome.desktop.wm.preferences', 'num-workspaces', '1'),
)


def _result(id, state, expected=None, actual=None, action='', detail=''):
    return CheckResult(id, state, expected, actual, action, detail)


def _properties(text):
    return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)


def _account(config, host):
    if not config.user:
        raise ValueError('화면 사용자를 지정하세요.')
    found = host.run(['getent', 'passwd', config.user])
    if found.returncode:
        raise ValueError('지정한 화면 사용자가 없습니다.')
    values = found.stdout.strip().split(':')
    if len(values) != 7 or values[0] != config.user:
        raise ValueError('화면 사용자 정보를 확인할 수 없습니다.')
    uid, gid = int(values[2]), int(values[3])
    home = Path(values[5])
    if uid == 0 or not home.is_absolute() or home == Path('/'):
        raise ValueError('일반 사용자와 유효한 홈 디렉터리가 필요합니다.')
    return uid, gid, home


def _quote(value):
    # systemd and desktop Exec parse their own quoting; do not use shell quoting.
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def unit_name(target='default'):
    from .config import ScreenConfig
    ScreenConfig(id=target)
    return UNIT if target=='default' else f'ala-chrome@{target}.service'


def _context(config,target=None):
    target=config.target_id if target is None else target
    return config if config.target_id==target and hasattr(config,'_target_id') else config.for_screen(target,revision=config.config_revision)


def _committed_screen(config, target, store):
    """Resolve the protected login selection, or the unregistered legacy target."""
    state = store.load(target)
    if state is None:
        return config.for_screen(target), None
    from .runtime import committed_selection
    selected = committed_selection(target, paths=store.paths, host=store.host)
    snapshot = Config.from_dict(store._validate_selection(selected))
    if snapshot.user != config.user:
        raise PermissionError('확정된 화면 설정의 사용자 계정이 다릅니다.')
    if not (Path(selected['release']) / 'COMPATIBILITY.json').is_file():
        if target != 'default' or snapshot != config:
            raise ValueError('구버전 화면 코드의 최초 이전이 필요합니다.')
    return snapshot.for_screen(target, revision=selected['revision']), state


def _autostart_enabled(config, paths):
    if not config.display:
        return False
    targets = config.screen_targets()
    if any(target.autostart for target in targets):
        return True
    # Provision the shared login entry for either configuration. A saved
    # change must not remove the entry still used by a committed target.
    from .runtime import RuntimeStore
    store = RuntimeStore(paths)
    for target in targets:
        context, _ = _committed_screen(config, target.id, store)
        if context.display and context.autostart:
            return True
    return False


def unit_text(target='default'):
    command = ' '.join(_quote(v) for v in (DISPLAY_LAUNCHER, '_runtime-dispatch',target))
    return f'''{UNIT_MARKER}
[Unit]
Description=ala DID Chrome display
PartOf=graphical-session.target
After=graphical-session.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=exec
ExecStart={command}
Restart=always
RestartSec=5
SuccessExitStatus=78
RestartPreventExitStatus=78
TimeoutStartSec=infinity
TimeoutStopSec=30
KillMode=control-group
'''


def desktop_text():
    command = ' '.join(_quote(v) for v in (DISPLAY_LAUNCHER, '_session'))
    return f'''{DESKTOP_MARKER}
[Desktop Entry]
Type=Application
Name=ala DID display
Exec={command}
OnlyShowIn=GNOME;
NoDisplay=true
X-GNOME-Autostart-enabled=true
'''


def gdm_candidate(text: str, config: Config) -> str:
    """Edit only our two keys, preserving comments, unrelated sections and values."""
    enabled = config.display and config.autologin
    lines = text.splitlines(keepends=True)
    sections = [(i, re.fullmatch(r'\s*\[([^]]+)\]\s*(?:[#;].*)?\n?', line)) for i, line in enumerate(lines)]
    starts = [i for i, match in sections if match and match.group(1).strip() == 'daemon']
    if len(starts) > 1:
        raise ValueError('GDM daemon section이 중복되어 있습니다.')
    if not enabled and MARKER not in text:
        return text
    if not starts:
        if not enabled:
            return text
        if text and not text.endswith('\n'):
            lines.append('\n')
        lines += ['[daemon]\n']
        start, end = len(lines) - 1, len(lines)
    else:
        start = starts[0]
        end = next((i for i, match in sections if match and i > start), len(lines))
    keys = {'AutomaticLoginEnable': 'True' if enabled else 'False'}
    if enabled:
        keys['AutomaticLogin'] = config.user
    seen = set()
    segment = []
    for line in lines[start + 1:end]:
        match = re.match(r'\s*(AutomaticLoginEnable|AutomaticLogin)\s*=', line)
        if match:
            key = match.group(1)
            if key in seen:
                raise ValueError(f'GDM {key} 값이 중복되어 있습니다.')
            seen.add(key)
            if key in keys:
                segment.append(f'{key}={keys[key]}\n')
            else:
                segment.append(line)
        else:
            segment.append(line)
    if MARKER not in ''.join(segment):
        segment.insert(0, MARKER + '\n')
    for key, value in keys.items():
        if key not in seen:
            if segment and not segment[-1].endswith('\n'):
                segment[-1] += '\n'
            segment.append(f'{key}={value}\n')
    return ''.join(lines[:start + 1] + segment + lines[end:])


def _file_result(id, path, expected, uid):
    if path.is_symlink():
        return _result(id, 'blocked', '일반 파일', '심볼릭 링크', '경로를 직접 확인하세요.')
    if not path.exists():
        return _result(id, 'change_required', expected, '없음', 'ala setup --apply')
    if not path.is_file():
        return _result(id, 'blocked', '일반 파일', '다른 파일 형식')
    info = path.stat()
    valid = path.read_text() == expected and info.st_uid == uid and stat.S_IMODE(info.st_mode) == 0o644
    return _result(id, 'ok' if valid else 'change_required', '내용·소유자·0644 일치',
                   '일치' if valid else '차이 있음', 'ala setup --apply' if not valid else '')


def _graphical_sessions(host, uid, *, greeters=None):
    listed = host.run(['loginctl', 'list-sessions', '--no-legend', '--no-pager'])
    if listed.returncode:
        return None, listed.stderr or '로그인 세션 조회 실패'
    sessions = []
    for line in listed.stdout.splitlines():
        fields = line.split()
        if not fields:
            continue
        session_id = fields[0]
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', session_id):
            continue
        result = host.run(['loginctl', 'show-session', session_id, '--property=User', '--property=Type',
                           '--property=Class', '--property=Remote', '--property=Active', '--property=State', '--property=LockedHint'])
        if result.returncode:
            return None, '로그인 세션 상세 조회 실패'
        value = _properties(result.stdout)
        if (greeters is not None and value.get('Class') == 'greeter' and value.get('Active') == 'yes'
                and value.get('Remote') == 'no' and value.get('Type') in {'x11', 'wayland'}):
            greeters.append(session_id)
        if value.get('User') == str(uid) and value.get('Type') in {'x11', 'wayland'} and value.get('Class') in {'user', 'user-early'} and value.get('Remote') == 'no' and value.get('State') != 'closing':
            sessions.append((session_id, value))
    return sessions, ''


def _user_call(config, host, paths, action, *, request=None):
    uid, _, home = _account(config, host)
    # GSettings reads the selected user's database without a live session bus.
    # Configuration inspections must not depend on graphical/session readiness.
    path, has_runtime = '', False
    if action != 'workspace-settings':
        runtime = host.run(['loginctl', 'show-user', str(uid), '--property=RuntimePath', '--value'])
        path = runtime.stdout.strip()
        has_runtime = runtime.returncode == 0 and path.startswith('/') and paths.at(path).is_dir()
    if not has_runtime and action not in {'prepare', 'autostart-sync', 'workspace-settings'}:
        return {'exit_code': 2, 'state': 'pending', 'reason': '사용자 runtime이 없습니다.',
                'next_action':'다음 GNOME 로그인 또는 유지보수 재부팅 후 화면 환경을 다시 확인하세요.'}
    environment = [f'HOME={home}', 'PATH=/usr/sbin:/usr/bin:/sbin:/bin', 'LANG=C.UTF-8']
    if has_runtime:
        environment += [f'XDG_RUNTIME_DIR={path}', f'DBUS_SESSION_BUS_ADDRESS=unix:path={path}/bus']
    if config.runtime_pin:
        pin=config.runtime_pin.get('config_path')
        if not isinstance(pin,str) or not pin.startswith('/'):
            return {'exit_code':1,'state':'unknown','reason':'root 실행 스냅샷 경로를 확인하지 못했습니다.'}
        environment += ['ALA_RUNTIME_CONFIG='+pin,'ALA_RUNTIME_TARGET='+config.target_id,'ALA_RUNTIME_ACTION='+action]
    target_args=[] if config.target_id=='default' else ['--target',config.target_id]
    if request is not None:
        if action != 'monitor-layout':
            raise ValueError('이 사용자 작업은 추가 입력을 받지 않습니다.')
        target_args = ['--request', json.dumps(request, separators=(',', ':'), allow_nan=False)]
    result = host.run(['runuser', '-u', config.user, '--', 'env', '-i', *environment, str(RELEASE / '.venv/bin/python'),
                       '-I', str(RELEASE / 'entry.py'), '_chrome-user', action,*target_args], timeout=30)
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError):
        return {'exit_code': 1, 'state': 'unknown', 'reason': result.stderr.strip() or '사용자 실행 응답을 읽을 수 없습니다.'}
    if not isinstance(value, dict):
        return {'exit_code': 1, 'state': 'unknown', 'reason': '잘못된 사용자 응답입니다.'}
    if result.returncode and 'exit_code' not in value:
        value.update(exit_code=1, state='error')
    return value



class BudgetHost:
    """Clamp every external call to one monotonic operation budget."""
    def __init__(self,host,timeout):
        if not isinstance(timeout,(int,float)) or isinstance(timeout,bool) or timeout<=0:
            raise ValueError('양수의 작업 제한 시간이 필요합니다.')
        self.host,self.deadline=host,time.monotonic()+timeout
    def run(self,argv,**kwargs):
        from .system import Command
        remaining=self.deadline-time.monotonic()
        if remaining<=0:return Command(124,'','작업 제한 시간 초과')
        kwargs['timeout']=min(kwargs.get('timeout',10),remaining)
        return self.host.run(argv,**kwargs)


def _install_extension(paths):
    from .monitors import UUID
    source=EXTENSION_SOURCE/UUID
    destination=paths.at('/usr/share/gnome-shell/extensions/'+UUID)
    for name in ('metadata.json','extension.js'):
        _write_managed(destination/name,(source/name).read_text(),paths.backups)


def _extension_activation(host,*,apply=False):
    import ast
    from .monitors import UUID
    disabled=host.run(['gsettings','get','org.gnome.shell','disable-user-extensions'])
    enabled=host.run(['gsettings','get','org.gnome.shell','enabled-extensions'])
    if disabled.returncode or enabled.returncode:
        return {'state':'pending','reason':'GNOME 확장 설정을 아직 조회하지 못했습니다.'}
    if disabled.stdout.strip()=='true':
        return {'state':'blocked','reason':'외부 GNOME 확장 차단 정책이 활성화되어 있습니다.'}
    try:
        text=enabled.stdout.strip()
        if text.startswith('@as '):text=text[4:]
        values=ast.literal_eval(text)
        if not isinstance(values,list) or any(not isinstance(item,str) for item in values):raise ValueError()
    except (ValueError,SyntaxError,TypeError):
        return {'state':'unknown','reason':'GNOME 확장 목록의 형식을 확인하지 못했습니다.'}
    if UUID not in values:
        if not apply:return {'state':'change_required','reason':'화면 관리 확장 활성화 등록이 필요합니다.'}
        response=host.run(['gsettings','set','org.gnome.shell','enabled-extensions',repr(values+[UUID])])
        if response.returncode:return {'state':'unknown','reason':'GNOME 확장 활성화 등록을 확인하지 못했습니다.'}
        confirmed=_extension_activation(host)
        if confirmed['state']!='ok':return {'state':'pending','reason':'GNOME 확장 목록 저장을 재확인하지 못했습니다. 사용자 bus 연결 후 다시 설정하세요.'}
    return {'state':'ok','reason':'GNOME 확장 활성화가 등록되었습니다. 새 설치는 재로그인 후 로드됩니다.'}


def _workspace_policy(config, host, *, apply=False, environment=None):
    """Only the display user's two workspace keys; never manage other windows."""
    if not config.display:
        return {'state':'not_applicable','exit_code':0,'settings':{},'changed':False}

    def read():
        settings = {}
        for schema, key, _ in WORKSPACE_SETTINGS:
            value = host.run(['gsettings', 'get', schema, key])
            if value.returncode == 0:
                settings[schema + '.' + key] = value.stdout.strip()
        return settings

    settings = read()
    response = {'settings':settings,'changed':False,
                'next_action':'GNOME 로그인 후 간편 설정을 다시 실행하고 작업공간 상태를 확인하세요.'}
    if len(settings) != len(WORKSPACE_SETTINGS):
        return response | {'state':'unknown','exit_code':1,'reason':'DID 계정의 작업공간 설정을 조회하지 못했습니다.'}
    changes = [(schema,key,expected) for schema,key,expected in WORKSPACE_SETTINGS
               if settings[schema + '.' + key] != expected]
    if not changes:
        return response | {'state':'ok','exit_code':0,'reason':'DID 계정의 작업공간 하나가 설정되었습니다.','next_action':''}
    if not apply:
        return response | {'state':'change_required','exit_code':1,'reason':'DID 계정의 작업공간을 하나로 설정해야 합니다.',
                           'next_action':'간편 설정을 실행해 DID 계정의 작업공간 설정을 적용하세요.'}
    environment = os.environ if environment is None else environment
    if not environment.get('DBUS_SESSION_BUS_ADDRESS'):
        return response | {'state':'pending','exit_code':2,'reason':'DID 계정의 실제 사용자 bus가 없어 작업공간 설정을 보류했습니다.',
                           'next_action':'다음 정상 GNOME 로그인에서 작업공간 설정을 다시 적용합니다. 이후 상태를 확인하세요.'}
    # Preflight both keys before either write, so an external lock cannot cause
    # a known partial policy change. Do not override or add dconf locks.
    for schema, key, _ in WORKSPACE_SETTINGS:
        writable = host.run(['gsettings', 'writable', schema, key])
        if writable.returncode or writable.stdout.strip() not in {'true','false'}:
            return response | {'state':'unknown','exit_code':1,'reason':'작업공간 설정의 쓰기 가능 여부를 확인하지 못했습니다.'}
        if writable.stdout.strip() != 'true':
            return response | {'state':'blocked','exit_code':1,'reason':'외부 정책이 DID 계정의 작업공간 설정 변경을 잠갔습니다.',
                               'next_action':'관리자가 기존 작업공간 정책을 확인한 뒤 간편 설정을 다시 실행하세요.'}
    failed = False
    for schema, key, expected in changes:
        changed = host.run(['gsettings', 'set', schema, key, expected])
        if changed.returncode:
            failed = True
            break
        response['changed'] = True
    response['settings'] = settings = read()
    if failed:
        return response | {'state':'error','exit_code':1,'reason':'DID 계정의 작업공간 설정 저장을 완료하지 못했습니다.'}
    if len(settings) != len(WORKSPACE_SETTINGS):
        return response | {'state':'unknown','exit_code':1,'reason':'저장 후 작업공간 설정을 재조회하지 못했습니다.'}
    if any(settings[schema + '.' + key] != expected for schema,key,expected in WORKSPACE_SETTINGS):
        return response | {'state':'pending','exit_code':2,'reason':'저장 후 작업공간 하나의 설정을 확인하지 못했습니다.'}
    return response | {'state':'ok','exit_code':0,'reason':'DID 계정의 작업공간 하나가 설정되었습니다.','next_action':''}


def _workspace_checks(value):
    settings = value.get('settings') if isinstance(value.get('settings'), dict) else {}
    results = []
    for schema, key, expected in WORKSPACE_SETTINGS:
        actual = settings.get(schema + '.' + key)
        state = 'unknown' if actual is None else 'ok' if actual == expected else 'change_required'
        if actual is None and value.get('state') == 'pending':
            state = 'pending'
        results.append(_result('display.workspace.' + key, state, expected, actual,
                               value.get('next_action') or ('간편 설정을 실행해 DID 계정의 작업공간 설정을 적용하세요.' if state != 'ok' else ''),
                               '지정된 DID 계정의 설정값 검사이며 현재 창의 작업공간 이동 완료를 보증하지 않습니다.'))
    return results

def _gdm_diagnostics(host):
    """Summarize current-boot evidence without exporting raw authentication logs."""
    status = host.run(['systemctl', 'show', 'gdm.service', '--property=LoadState',
                       '--property=ActiveState', '--property=SubState', '--property=Result'])
    values = _properties(status.stdout)
    if status.returncode or not values.get('ActiveState'):
        state, actual = 'unknown', 'GDM 실행 상태 조회 불가'
    elif values.get('LoadState') == 'not-found':
        state, actual = 'blocked', 'GDM 서비스 없음'
    elif values['ActiveState'] == 'failed':
        state, actual = 'error', 'GDM 서비스 실패'
    elif values['ActiveState'] == 'active':
        state, actual = 'pending', 'GDM 실행 중, 대상 사용자 바탕화면 세션 없음'
    else:
        state, actual = 'pending', 'GDM 서비스 비활성 또는 시작 중'
    results = [_result('display.gdm_runtime', state, '활성 GDM과 사용자 그래픽 세션', actual,
                       '재부팅 후 재검사; 계속되면 journalctl -b -u gdm.service 확인')]
    logs = host.run(['journalctl', '-b', '-u', 'gdm.service', '-n', '80',
                     '--no-pager', '--output=cat'], timeout=10)
    if logs.returncode:
        results.append(_result('display.gdm_diagnostics', 'unknown', '현재 부팅 GDM 로그',
                               '로그 조회 불가', '관리자 권한과 journal 접근 권한 확인'))
        return results
    # Categories only: logs can contain account names or authentication metadata.
    text = logs.stdout[-16384:].lower()
    categories = []
    for name, pattern in (
        ('그래픽 초기화 오류 단서', r'no screens found|failed to (?:start|initialize).*(?:display|wayland|x server)|failed to open.*drm'),
        ('로그인·세션 시작 오류 단서', r'authentication failure|pam.*(?:error|failed)|session.*(?:failed|error)'),
    ):
        if re.search(pattern, text):
            categories.append(name)
    results.append(_result('display.gdm_diagnostics', 'pending', '사용자 세션 시작',
                           categories or ['최근 GDM 로그에서 분류 가능한 원인 미확인'],
                           '현재 부팅 GDM 로그와 GPU 상태 확인; 로그 단서만으로 원인을 확정하지 않습니다.'))
    return results


def _configuration_readiness(results):
    ids = {'display.gdm', 'display.autologin', 'display.gdm_enabled',
           'display.default_target', 'display.initial_setup'}
    relevant = [r for r in results if r.id in ids]
    state = next((candidate for candidate in ('error', 'blocked', 'unknown', 'change_required', 'pending')
                  if any(r.state == candidate for r in relevant)), 'ok')
    return _result('display.configuration', state, '자동 로그인·그래픽 부팅 설정 준비',
                   {r.id: r.state for r in relevant},
                   '설정 준비와 실제 세션·화면 표시는 별도로 검사합니다.')


def inspect_display_parts(config: Config, host=None, paths=None, *, include_runtime=True):
    host, paths = host or Host(), paths or Paths()
    results = []
    try:
        uid, _, home = _account(config, host)
    except (ValueError, OSError) as exc:
        return [_result('chrome.account', 'pending' if not config.user else 'blocked', '일반 화면 계정', str(exc), 'ala config edit')], []
    autostart = paths.at(str(home / '.config/autostart/ala-display.desktop'))
    gdm = paths.at('/etc/gdm/custom.conf')
    try:
        if config.display:
            if not gdm.parent.is_dir():
                results.append(_result('display.gdm', 'blocked', 'GNOME/GDM 설치', 'GDM 설정 디렉터리 없음'))
            else:
                original = gdm.read_text() if gdm.exists() and not gdm.is_symlink() else ''
                candidate = gdm_candidate(original, config)
                if config.autologin or MARKER in original:
                    results.append(_file_result('display.autologin', gdm, candidate, os.geteuid()))
                else:
                    results.append(_result('display.autologin', 'not_applicable', False, '자동 로그인 미관리'))
            for target in config.screen_targets():
                unit_path=paths.at('/etc/systemd/user/' + unit_name(target.id))
                id='chrome.unit' if target.id=='default' else 'chrome.unit.'+target.id
                if unit_path.is_file() and not unit_path.is_symlink() and UNIT_MARKER not in unit_path.read_text():
                    results.append(_result(id,'blocked','ala 관리 unit','관리되지 않은 동명 unit','기존 파일을 직접 확인하세요.'))
                else:
                    results.append(_file_result(id, unit_path, unit_text(target.id), os.geteuid()))
            if config.display:
                from .monitors import UUID
                for name in ('metadata.json','extension.js'):
                    results.append(_file_result('display.extension.'+name,
                        paths.at('/usr/share/gnome-shell/extensions/'+UUID+'/'+name),
                        (EXTENSION_SOURCE/UUID/name).read_text(),os.geteuid()))
            marker = paths.at(str(home / '.config/gnome-initial-setup-done'))
            good = marker.is_file() and not marker.is_symlink() and marker.stat().st_uid == uid
            results.append(_result('display.initial_setup', 'ok' if good else 'change_required', '첫 실행 완료 마커', '있음' if good else '없음/소유자 차이', 'ala setup --apply' if not good else ''))
            if config.autologin:
                for id, argv, expected in [('display.gdm_enabled', ['systemctl','is-enabled','gdm.service'], 'enabled'),
                                           ('display.default_target', ['systemctl','get-default'], 'graphical.target')]:
                    value = host.run(argv)
                    state = 'unknown' if value.returncode in {124, 127} else 'ok' if value.stdout.strip() == expected and value.returncode == 0 else 'change_required'
                    results.append(_result(id, state, expected, value.stdout.strip() or value.stderr.strip(), 'ala setup --apply' if state != 'ok' else ''))
        if config.display:
            results.append(_configuration_readiness(results))
        if _autostart_enabled(config, paths):
            results.append(_file_result('chrome.autostart', autostart, desktop_text(), uid))
        elif autostart.exists() or autostart.is_symlink():
            state = 'blocked' if autostart.is_symlink() or DESKTOP_MARKER not in autostart.read_text() else 'change_required'
            results.append(_result('chrome.autostart', state, '자동 시작 등록 없음', '등록 있음', 'ala setup --apply'))
        else:
            results.append(_result('chrome.autostart', 'ok', False, False))
        for target in config.screen_targets():
            if config.display and target.autostart and not target.url:
                results.append(_result('chrome.url' if target.id=='default' else 'chrome.url.'+target.id, 'ok', '사이트 미설정 시 내부 안내', '설정 안내 모드', '화면 관리에서 사이트 주소를 저장하고 적용하세요.'))
    except (OSError, ValueError) as exc:
        results.append(_result('display.inspect', 'blocked' if isinstance(exc, ValueError) else 'unknown', '표시 구성 조회', str(exc)))
        return results, []
    try:
        schedule=_frontend_schedule(config,host,paths)
        if schedule:results.append(schedule)
    except (OSError,ValueError,RuntimeError):
        results.append(_result('frontend.timer','unknown','배포 감시 timer 조회','조회 실패'))
    if config.display:
        try:
            workspace = _user_call(config, host, paths, 'workspace-settings')
        except (OSError, ValueError, RuntimeError):
            workspace = {'state':'unknown','reason':'DID 계정의 작업공간 설정 조회 실패'}
        results.extend(_workspace_checks(workspace))
    if not include_runtime:
        return results, []
    configuration, results = results, []
    try:
        if not config.display:
            return configuration, [_result('chrome.runtime', 'not_applicable', False, False)]
        greeters = []
        sessions, reason = _graphical_sessions(host, uid, greeters=greeters)
        if sessions is None:
            results.append(_result('display.session', 'unknown', '그래픽 세션 하나', reason))
        elif len(sessions) > 1:
            results.append(_result('display.session', 'blocked', '그래픽 세션 하나', len(sessions), '중복 그래픽 세션을 정리하세요. 임의 선택하지 않습니다.'))
        elif not sessions or sessions[0][1].get('Active') != 'yes':
            results.append(_result('display.session', 'pending', '활성 로컬 그래픽 세션', '없음/비활성', '다음 부팅 후 자동 로그인 결과를 재검사하세요.'))
            if greeters:
                results.append(_result('display.login_screen', 'pending', '대상 사용자 바탕화면 세션',
                                       '활성 로그인 화면(greeter) 세션 감지',
                                       '자동 로그인 설정 적용 후 재부팅; 계속 로그인 화면이면 GDM 로그 확인',
                                       '다른 seat의 로그인 화면일 수 있으므로 모니터 표시를 확정하지 않습니다.'))
            results.extend(_gdm_diagnostics(host))
        else:
            results.append(_result('display.session', 'ok', '그래픽 세션 하나', sessions[0][0]))
            locked = sessions[0][1].get('LockedHint')
            results.append(_result('display.session_lock',
                                   'pending' if locked == 'yes' else 'ok' if locked == 'no' else 'unknown',
                                   '잠금 해제된 세션', locked or '잠금 상태 조회 불가',
                                   '잠금 정책 적용 후 재부팅하여 자동 로그인 확인' if locked == 'yes' else '',
                                   '잠금 힌트는 실제 화면 표시를 검증하지 않습니다.'))
            for target in config.screen_targets():
                if target.autostart:
                    runtime = _user_call(config.for_screen(target.id), host, paths, 'check')
                    if config.schema_version==2 and (runtime.get('service') or {}).get('ActiveState') in {'active','activating'}:
                        window=_user_call(config.for_screen(target.id),host,paths,'windows')
                        results.append(_result('chrome.window.'+target.id,window.get('state','unknown'),'실제 관리 창·출력 배치',window,window.get('next_action','화면 관리에서 실제 배치를 확인하세요.')))
                    if isinstance(runtime.get('results'), list):
                        for item in runtime['results']:
                            if target.id!='default':item=item | {'id':item['id']+'.'+target.id}
                            results.append(CheckResult(**item))
                    else:
                        results.append(_result('chrome.runtime' if target.id=='default' else 'chrome.runtime.'+target.id, runtime.get('state', 'unknown'), 'Chrome 실행 확인',
                                               runtime.get('reason', '응답 없음'), runtime.get('next_action', '')))
                else:
                    results.append(_result('chrome.runtime' if target.id=='default' else 'chrome.runtime.'+target.id, 'not_applicable', '자동 시작 요구 없음',
                                           '자동 시작 꺼짐' if not target.autostart else '주소 입력 대기'))
            settings = _user_call(config, host, paths, 'settings')
            for schema, key, expected in SETTINGS:
                name = schema + '.' + key
                actual = settings.get('settings', {}).get(name)
                state = 'unknown' if actual is None else 'ok' if actual == expected else 'pending'
                results.append(_result('display.live.' + key, state, expected, actual, 'GNOME 재로그인 후 재검사' if state != 'ok' else ''))
        results.append(_result('display.visual_acceptance', 'not_applicable', '실제 화면 관찰', '자동 검사 범위 밖',
                               '실기기 재부팅 후 바탕화면·DID 표시를 별도로 확인하고 수용 기록에 남기세요.',
                               '설정·세션·프로세스 검사는 모니터의 실제 표시나 팝업 부재를 보증하지 않습니다.'))
    except (OSError, ValueError) as exc:
        results.append(_result('display.inspect', 'blocked' if isinstance(exc, ValueError) else 'unknown', '표시 구성 조회', str(exc)))
    return configuration, results


def inspect_display(config: Config, host=None, paths=None, *, configuration_only=False):
    configuration, runtime = inspect_display_parts(config, host, paths, include_runtime=not configuration_only)
    return configuration + runtime


def _safe_parents(path):
    for value in [path, *path.parents]:
        if value.is_symlink():
            raise ValueError(f'심볼릭 링크 경로를 변경하지 않습니다: {value}')


def _backup_file(path, backups):
    _safe_parents(path)
    if not path.is_file():
        raise ValueError(f'일반 파일이 아닙니다: {path}')
    _safe_parents(backups)
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = backups / (path.name + '.' + str(time.time_ns()) + '.backup')
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(path.read_bytes())


def _write_managed(path, content, backups=None, mode=0o644):
    _safe_parents(path)
    if path.exists():
        if not path.is_file():
            raise ValueError(f'일반 파일이 아닙니다: {path}')
        info = path.stat()
        if path.read_text() == content and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == mode:
            return False
        if backups is not None:
            _backup_file(path, backups)
    atomic_write(path, content, mode)
    return True


def _prepare_autostart_file(config, home: Path, paths):
    """Only the shared login entry; called with the target UID."""
    desktop = home / '.config/autostart/ala-display.desktop'
    changed = False
    if _autostart_enabled(config, paths):
        if desktop.exists() and DESKTOP_MARKER not in desktop.read_text():
            raise ValueError('관리되지 않은 ala-display.desktop이 있습니다.')
        # Root provisioning backed up any previous managed file before delegating.
        changed |= _write_managed(desktop, desktop_text())
    elif desktop.exists() or desktop.is_symlink():
        _safe_parents(desktop)
        if not desktop.is_file() or DESKTOP_MARKER not in desktop.read_text():
            raise ValueError('관리되지 않은 자동 실행 파일을 제거하지 않습니다.')
        desktop.unlink()
        changed = True
    return changed


def _prepare_user_files(config, home: Path, paths):
    """Called with the target UID, so user-controlled home paths never grant root writes."""
    changed = _prepare_autostart_file(config, home, paths)
    if config.display:
        marker = home / '.config/gnome-initial-setup-done'
        _safe_parents(marker)
        if not marker.exists():
            atomic_write(marker, 'yes\n', 0o644)
            changed = True
        elif not marker.is_file():
            raise ValueError('GNOME 초기 설정 마커 경로를 확인하세요.')
        elif marker.stat().st_uid != os.geteuid():
            changed |= _write_managed(marker,marker.read_text())
    return changed


def sync_autostart_registration(config, host=None, paths=None):
    """Sync the login entry without provisioning GNOME, units, or restarting Chrome."""
    host, paths = host or Host(), paths or Paths()
    try:
        _, _, home = _account(config, host)
        desktop = paths.at(str(home / '.config/autostart/ala-display.desktop'))
        _safe_parents(desktop)
        if desktop.exists() or desktop.is_symlink():
            if not desktop.is_file() or desktop.is_symlink() or DESKTOP_MARKER not in desktop.read_text():
                raise ValueError('관리되지 않은 자동 실행 파일을 변경하지 않습니다.')
            desired = desktop_text() if _autostart_enabled(config, paths) else None
            if desktop.read_text() != desired:
                _backup_file(desktop, paths.backups)
        return _user_call(config, host, paths, 'autostart-sync')
    except (OSError, ValueError, RuntimeError) as exc:
        return {'state': 'blocked', 'exit_code': 1, 'reason': str(exc),
                'next_action': '화면 계정의 자동 시작 등록을 확인한 뒤 다시 적용하세요.'}



def _frontend_schedule(config,host,paths,*,apply=False):
    enabled=any(target.frontend.enabled for target in config.screen_targets())
    timer='ala-frontend.timer'
    path=paths.at('/etc/systemd/system/'+timer)
    if not enabled and not path.exists() and not path.is_symlink():return None
    if path.exists() or path.is_symlink():
        from .auto_update import MARKER as schedule_marker
        if path.is_symlink() or not path.is_file() or schedule_marker not in path.read_text():
            return _result('frontend.timer','blocked','ala 관리 timer','외부 동명 timer','기존 timer를 직접 확인하세요.')
    if apply and enabled:
        from .auto_update import register_units
        register_units(paths=paths,host=host)
    registered=host.run(['systemctl','is-enabled',timer])
    active=host.run(['systemctl','is-active',timer])
    if registered.returncode in {124,127} or active.returncode in {124,127}:
        return _result('frontend.timer','unknown','배포 감시 timer 상태','조회 실패','systemd 연결 확인')
    current=registered.returncode==0 and registered.stdout.strip()=='enabled'
    running=active.returncode==0 and active.stdout.strip()=='active'
    changed=(enabled and not (current and running)) or (not enabled and (current or running))
    if changed and apply:
        command=['systemctl','enable','--now',timer] if enabled else ['systemctl','disable','--now',timer]
        response=host.run(command)
        if response.returncode:return _result('frontend.timer','unknown',enabled,'등록 변경 확인 실패','systemd timer 상태 확인')
        return _frontend_schedule(config,host,paths)
    return _result('frontend.timer','change_required' if changed else 'ok',enabled,
                   {'enabled':current,'active':running},'ala setup --apply' if changed else '')

def apply_display(config: Config, host=None, paths=None):
    host, paths = host or Host(), paths or Paths()
    if paths.root != Path('/') and type(host) is Host:
        return [_result('display.apply', 'blocked', '모의 Host 또는 실제 root', '대체 경로에서 실제 실행 금지')]
    try:
        uid, _, home = _account(config, host)
        gdm = paths.at('/etc/gdm/custom.conf')
        desktop=paths.at(str(home / '.config/autostart/ala-display.desktop'))
        unit_path=paths.at('/etc/systemd/user/' + UNIT)
        if config.display and not gdm.parent.is_dir():
            return [_result('display.gdm', 'blocked', 'GNOME/GDM 설치', 'GDM 설정 디렉터리 없음')]
        _safe_parents(gdm)
        _safe_parents(unit_path)
        _safe_parents(desktop)
        if config.display and unit_path.exists() and (not unit_path.is_file() or UNIT_MARKER not in unit_path.read_text()):
            raise ValueError('관리되지 않은 ala-chrome.service를 덮어쓰지 않습니다.')
        if desktop.exists() and (not desktop.is_file() or DESKTOP_MARKER not in desktop.read_text()):
            raise ValueError('관리되지 않은 자동 실행 파일을 변경하지 않습니다.')
        if gdm.exists() or config.display:
            original = gdm.read_text() if gdm.exists() else ''
            candidate = gdm_candidate(original, config)
            if candidate != original or (config.display and config.autologin):
                _write_managed(gdm, candidate, paths.backups)
        if config.display:
            for target in config.screen_targets():
                path=paths.at('/etc/systemd/user/'+unit_name(target.id))
                _safe_parents(path)
                if path.exists() and (not path.is_file() or UNIT_MARKER not in path.read_text()):
                    raise ValueError('관리되지 않은 대상 unit을 덮어쓰지 않습니다.')
                _write_managed(path, unit_text(target.id), paths.backups)
            _install_extension(paths)
            if config.autologin:
                current = host.run(['systemctl','is-enabled','gdm.service'])
                if current.stdout.strip() != 'enabled':
                    if host.execute(['systemctl','enable','gdm.service']):
                        raise RuntimeError('GDM 부팅 등록 실패')
                current = host.run(['systemctl','get-default'])
                if current.stdout.strip() != 'graphical.target':
                    if host.execute(['systemctl','set-default','graphical.target']):
                        raise RuntimeError('그래픽 부팅 target 설정 실패')
        if desktop.exists() or desktop.is_symlink():
            _safe_parents(desktop)
            if not desktop.is_file() or DESKTOP_MARKER not in desktop.read_text():
                raise ValueError('관리되지 않은 자동 실행 파일을 변경하지 않습니다.')
            desired=desktop_text() if _autostart_enabled(config, paths) else None
            info=desktop.stat()
            if desktop.read_text()!=desired or info.st_uid!=uid or stat.S_IMODE(info.st_mode)!=0o644:
                _backup_file(desktop,paths.backups)
        marker=paths.at(str(home / '.config/gnome-initial-setup-done'))
        if config.display and marker.exists():
            _safe_parents(marker)
            if marker.is_file() and marker.stat().st_uid!=uid:
                _backup_file(marker,paths.backups)
        prepared = _user_call(config, host, paths, 'prepare')
        workspace = prepared.get('workspace_policy')
        issues = []
        if isinstance(workspace, dict) and workspace.get('state') not in {'ok','not_applicable'}:
            issues.append(_result('display.workspace.apply', workspace.get('state','unknown'),
                                  'DID 계정의 작업공간 하나', workspace.get('reason','작업공간 설정 확인 필요'),
                                  workspace.get('next_action','GNOME 로그인 후 간편 설정을 다시 실행하세요.')))
        if prepared.get('exit_code', 1):
            return issues + [_result('display.prepare', prepared.get('state','pending'), '사용자 자동 시작 준비', prepared.get('reason','사용자 runtime 필요'), 'GNOME 로그인 후 재실행')]
        schedule=_frontend_schedule(config,host,paths,apply=True)
        if schedule and schedule.state not in {'ok','not_applicable'}:return issues + [schedule]
        # Setup only provisions registration. Boot autostart and explicit screen
        # management own the Chrome lifecycle, independently of installation.
        return issues + inspect_display(config, host, paths, configuration_only=True)
    except (OSError, ValueError, RuntimeError) as exc:
        return [_result('display.apply', 'blocked', '표시 구성 준비', str(exc), '로그와 경로 확인 후 재실행')]


def chrome_action(action, config: Config, host=None, paths=None):
    host, paths = host or Host(), paths or Paths()
    config=_context(config)
    if action not in {'start','stop','restart','status','logs','check','outputs','windows','place','reload'}:
        raise ValueError('허용되지 않은 Chrome 명령입니다.')
    if action in {'start','restart'} and not config.display:
        return {'exit_code': 2, 'state':'pending', 'reason':'화면 사용 설정을 먼저 켜세요.'}
    uid, _, _ = _account(config, host)
    if action in {'start','restart','check'}:
        sessions, reason = _graphical_sessions(host, uid)
        if sessions is None:
            return {'exit_code':1,'state':'unknown','reason':reason}
        if len(sessions) > 1:
            return {'exit_code':1,'state':'blocked','reason':'그래픽 세션이 여러 개입니다. 임의로 선택하지 않습니다.'}
        if not sessions or sessions[0][1].get('Active') != 'yes':
            return {'exit_code':2,'state':'pending','reason':'활성 그래픽 세션이 없습니다. 자동 로그인 적용 후 재검사하세요.'}
    result = _user_call(config, host, paths, action)
    if action == 'logs':
        log = result.get('log')
        empty_markers = {'-- No entries --', 'No journal files were found.'}
        has_entries = (isinstance(log, str) and
                       any(line.strip() and line.strip() not in empty_markers for line in log.splitlines()))
        if result.get('exit_code') != 0 or not has_entries:
            from .display_journal import read_display_journal
            return read_display_journal(host, uid) if config.target_id=='default' else read_display_journal(host, uid,unit=unit_name(config.target_id))
    return result


def _load_user_config(paths=Paths(),target='default',*,runtime=False):
    if os.getuid() == 0:
        raise PermissionError('Chrome은 일반 사용자로 실행하세요.')
    p = paths.config
    info = p.lstat()
    if p.is_symlink() or not p.is_file() or info.st_uid != 0 or info.st_mode & 0o022:
        raise PermissionError('root 소유의 쓰기 보호된 ala 구성이 필요합니다.')
    config = None
    revision=''
    if runtime or os.environ.get('ALA_RUNTIME_CONFIG'):
        from .runtime import load_user_selection
        selected=load_user_selection(target or os.environ.get('ALA_RUNTIME_TARGET','default'),paths=paths,observation=not runtime)
        if selected is not None:
            # Desired config may already use a future schema. A protected,
            # selected snapshot is the only config this runtime must parse.
            config=Config.from_dict(selected['config'])
            revision=selected['revision']
    if config is None:
        config = Config.load(p)
    identity = pwd.getpwuid(os.getuid())
    if config.user != identity.pw_name:
        raise PermissionError('지정한 화면 사용자로 실행하세요.')
    return (config if target is None else config.for_screen(target,revision=revision)), Path(identity.pw_dir)


def _manager(host):
    response = host.run(['systemctl','--user','show-environment'])
    if response.returncode:
        raise RuntimeError('사용자 systemd 환경을 읽을 수 없습니다.')
    value = _properties(response.stdout)
    if not value.get('DISPLAY') and not value.get('WAYLAND_DISPLAY'):
        raise RuntimeError('사용자 systemd에 로그인한 화면 환경이 없습니다.')
    return value


def _unit_state(host, unit=UNIT):
    response = host.run(['systemctl','--user','show',unit,'--property=ActiveState','--property=SubState',
                         '--property=Result','--property=MainPID','--property=LoadState'])
    value = _properties(response.stdout)
    if response.returncode and not value:
        return {'ActiveState':'inactive','LoadState':'not-found','MainPID':'0','Result':'unknown',
                'QueryError':response.stderr.strip() or '서비스 상태 조회 실패'}
    if response.returncode and value.get('LoadState') != 'not-found':
        value['QueryError']=response.stderr.strip() or '서비스 상태 조회 실패'
    return value


def _confirmed_unit_state(value):
    return not value.get('QueryError') and value.get('ActiveState') in {
        'active','activating','inactive','failed','deactivating','reloading'}


def _profile(home,target='default'):
    unit_name(target)
    return home / ('.config/ala/chrome-display' if target=='default' else '.config/ala/chrome-displays/'+target)


def chrome_arguments(config, home, manager, *, supervised=False):
    args = ['/usr/bin/google-chrome-stable', '--user-data-dir=' + str(_profile(home,config.target_id)),
            '--no-first-run', '--no-default-browser-check',
            '--hide-crash-restore-bubble', '--password-store=basic']
    if config.layout=='fullscreen':args.insert(2,'--start-fullscreen')
    if not manager.get('DISPLAY') and manager.get('WAYLAND_DISPLAY'):
        args.append('--ozone-platform=wayland')
    if supervised:
        from .display_health import RECOVERY_URL
        return args + ['--remote-debugging-pipe', RECOVERY_URL]
    return args + [config.url]


def _foreign_profile_pid(paths, profile, allowed=()):
    from .chrome_process import AmbiguousCommandLine, profile_evidence
    proc = paths.at('/proc')
    if not proc.is_dir():
        return None
    for process in proc.iterdir():
        if not process.name.isdigit() or int(process.name) in allowed:
            continue
        try:
            uid_line = next(line for line in (process/'status').read_text().splitlines() if line.startswith('Uid:'))
            if int(uid_line.split()[1]) != os.getuid():
                continue
            raw = (process/'cmdline').read_bytes()
            if not profile_evidence(raw, profile):
                continue
            try:
                args = _observe_chrome_process(process, ['--user-data-dir=' + str(profile)], raw=raw).arguments
            except AmbiguousCommandLine:
                # Only an exact requested-profile observation reaches here. Do
                # not block every unrelated process with an unsupported title.
                return int(process.name)
            if not any(arg.startswith('--type=') for arg in args):
                return int(process.name)
        except (OSError, ValueError, StopIteration):
            continue
    return None


def _observe_chrome_process(process, expected_arguments, *, raw=None):
    from .chrome_process import AmbiguousCommandLine, observe_chrome_cmdline
    try:
        raw = (process/'cmdline').read_bytes() if raw is None else raw
    except OSError:
        raise AmbiguousCommandLine('Chrome 인자 관측을 읽을 수 없습니다.') from None
    executable = None
    if len(raw.rstrip(b'\0').split(b'\0')) == 1:
        try:
            executable = (process/'exe').readlink()
        except OSError:
            raise AmbiguousCommandLine('실제 Chrome 실행 파일을 조회하지 못해 Linux 제목 형식을 확인할 수 없습니다.') from None
    return observe_chrome_cmdline(raw, executable=executable,
                                  expected_arguments=[arg for arg in expected_arguments if arg is not None])


def _check_user(config, home, host, paths):
    config=_context(config)
    unit=unit_name(config.target_id)
    results = []
    try:
        _manager(host)
        results.append(_result('chrome.environment','ok','로그인 화면 환경','있음'))
    except RuntimeError as exc:
        return {'exit_code':2, 'state':'pending', 'reason':str(exc),
                'next_action':'다음 GNOME 로그인 또는 유지보수 재부팅 후 화면 환경을 다시 확인하세요.'}
    legacy = _unit_state(host, LEGACY) if config.target_id=='default' else {'ActiveState':'inactive'}
    state = _unit_state(host,unit)
    if legacy.get('QueryError') or state.get('QueryError'):
        results.append(_result('chrome.runtime','unknown','기존·현재 표시 서비스 상태 조회',
                               {'legacy':legacy.get('QueryError'),'current':state.get('QueryError')},
                               '사용자 systemd 연결·권한을 확인한 뒤 다시 조회하세요.'))
        results.append(_result('chrome.health','unknown','서비스와 연결된 유효한 감시 상태',
                               '서비스 상태 조회가 실패하여 정상 판정을 보류했습니다.',
                               'Chrome 로그와 사용자 서비스 연결을 확인하세요.'))
        return Report(results).to_dict() | {'state':'unknown','service':state}
    if legacy.get('ActiveState') in {'active','activating'}:
        results.append(_result('chrome.legacy','pending','새 표시 서비스','기존 did-chrome 실행 중','ala chrome restart 또는 다음 GNOME 로그인에서 전환'))
    if state.get('ActiveState') == 'failed':
        results.append(_result('chrome.runtime','error','Chrome 실행',state.get('Result'),'ala chrome logs 확인 후 ala chrome restart'))
        return Report(results).to_dict() | {'state':'error', 'service':state}
    if state.get('ActiveState') not in {'active','activating'}:
        results.append(_result('chrome.runtime','pending' if config.autostart else 'not_applicable','Chrome 실행' if config.autostart else False,'중지됨','ala chrome start' if config.autostart else ''))
        return Report(results).to_dict() | {'state':'pending' if config.autostart else 'stopped', 'service':state}
    pid = state.get('MainPID','0')
    if not re.fullmatch(r'[1-9][0-9]*', pid):
        results.append(_result('chrome.runtime','pending','유효한 Chrome PID','시작 중'))
        return Report(results).to_dict() | {'state':'pending', 'service':state}
    process = paths.at('/proc')/pid
    health = None
    health_result = None
    supervised = False
    try:
        uid_line = next(line for line in (process/'status').read_text().splitlines() if line.startswith('Uid:'))
        if int(uid_line.split()[1]) != os.getuid():
            raise ValueError('다른 사용자 소유 프로세스입니다.')
        arguments = (process/'cmdline').read_bytes().decode().rstrip('\0').split('\0')
        supervised = '_chrome-run' in arguments
        if supervised:
            from .display_health import read_health
            entry = Path(arguments[2]) if len(arguments) in {4,6} else None
            suffix=arguments[3:]
            allowed=[['_chrome-run','--target',config.target_id]]
            if config.target_id=='default':allowed.append(['_chrome-run'])
            if (entry is None or arguments[1] != '-I' or suffix not in allowed
                    or entry.name != 'entry.py'
                    or not (entry.parent in {RELEASE, Path('/opt/ala/current')}
                            or entry.parent.is_relative_to('/opt/ala/releases'))):
                raise ValueError('예상한 ala 감독 프로세스의 실행 인자가 아닙니다.')
            health = read_health(home, paths, int(pid), config.url,target_id=config.target_id)
            health_state = health['state']
            if health_state == 'healthy' and not health.get('target_matches_config'):
                health_state = 'unknown'
                health = health | {'state':'unknown','reason':'관리 탭과 현재 구성의 연결 근거를 확인하지 못했습니다.',
                                   'next_action':'Chrome 로그와 화면 구성을 확인하세요.'}
            check_state = {'healthy':'ok','waiting_site':'pending','waiting_configuration':'pending','recovering':'pending',
                           'failed':'error','unknown':'unknown'}[health_state]
            health_result = _result('chrome.health',check_state,'관리 탭 주 문서·HTTP 지속 감시',health,
                                    health.get('next_action',''),
                                    'HTTP 응답·관리 탭의 문서 관측입니다. 앱 내부 데이터·실제 모니터 표시는 보증하지 않습니다.')
            browser_pid = health.get('browser_pid')
            if type(browser_pid) is not int or browser_pid < 1 or health_state in {'unknown','failed'}:
                results.append(health_result)
                return Report(results).to_dict() | {'state':health_state, 'service':state, 'health':health}
            process = paths.at('/proc') / str(browser_pid)
        else:
            results.append(_result('chrome.health','pending','사이트 지속 감시','기존 단독 Chrome 실행을 유지했습니다.',
                                   '화면 관리에서 명시적으로 다시 시작하거나 다음 GNOME 로그인에서 지속 감시로 전환하세요.'))
        required = chrome_arguments(config, home, _manager(host),supervised=supervised)[1:]
        arguments = list(_observe_chrome_process(process, required).arguments)
        from .display_health import RECOVERY_URL
        launch_url = RECOVERY_URL if supervised else config.url
        for expected in required:
            if expected is None:
                results.append(_result('chrome.url','pending','DID URL','입력 대기','ala config edit'))
            else:
                label = 'chrome.url' if expected == launch_url else 'chrome.option.' + expected.split('=',1)[0].lstrip('-')
                if expected == launch_url:
                    valid=arguments.count(expected)==1
                else:
                    key=expected.split('=',1)[0]
                    valid=[arg for arg in arguments if arg.split('=',1)[0]==key]==[expected]
                results.append(_result(label,'ok' if valid else 'error', '로컬 복구 안내로 시작' if expected == RECOVERY_URL else expected,
                                       '일치' if valid else '없음/중복/다른 값','ala chrome restart' if not valid else ''))
        forbidden = ('--kiosk','--no-sandbox','--remote-debugging-port','--remote-debugging-address')
        if not supervised:
            forbidden += ('--remote-debugging-pipe',)
        for bad in forbidden:
            if any(arg.split('=',1)[0]==bad for arg in arguments):
                results.append(_result('chrome.forbidden','error','허용된 실행 옵션',bad,'ala chrome restart'))
        if supervised:
            from .display_health import verify_direct_child
            verify_direct_child(paths,int(pid),browser_pid,health)
    except (OSError, ValueError, StopIteration, IndexError, KeyError) as exc:
        results.append(_result('chrome.process','unknown','프로세스 확인',str(exc),
                               '화면 관리에서 상태·로그와 Chrome 실행 파일·인자 형식을 확인하세요.'))
    if supervised:
        if any(result.state in {'error','unknown','blocked'} for result in results):
            health = (health or {}) | {'state':'unknown','target_matches_config':False,
                                      'reason':'Chrome 자식의 소유권·부모 관계·시작 식별자·실행 인자를 모두 확인하지 못했습니다.',
                                      'next_action':'Chrome 프로세스와 로그를 확인한 뒤 화면 관리에서 필요한 조치를 선택하세요.'}
            health_result = _result('chrome.health','unknown','검증된 Chrome 자식의 사이트 감시',health,
                                    health['next_action'])
        if health_result is not None:
            results.append(health_result)
    process_verified=not any(item.state in {'error','unknown','blocked'} for item in results)
    report=Report(results)
    return report.to_dict() | {'process_verified':process_verified,'state':{0:'running',1:'error',2:'pending'}[report.exit_code], 'service':state,
                                        'health':health,
                                        'limits':'프로세스·감시 기록 검사입니다. 실제 창·팝업·앱 내용은 별도 확인합니다.'}



def _stop_evidence(config,home,host,paths,state):
    pid=state.get('MainPID','0')
    if not re.fullmatch(r'[1-9][0-9]*',pid):
        # A stopped supervisor can leave a browser in a separate GNOME scope.
        # Repeat stop must not turn that pending result into a false success.
        if _foreign_profile_pid(paths,_profile(home,config.target_id)):
            return {'state':'unknown','reason':'중지된 서비스와 별도로 표시 프로필을 사용하는 프로세스가 남아 있습니다.'}
        from .display_health import state_path,_read_json,boot_id
        path=state_path(home,config.target_id)
        if not path.exists() and not path.is_symlink():return None
        try:
            record=_read_json(path)
            if record.get('boot_id')!=boot_id(paths):return None
            if record.get('target_id','default')!=config.target_id:raise ValueError()
            windows=(record.get('placement') or {}).get('windows')
            if (not isinstance(windows,list) or len(windows)>32
                    or any(not isinstance(w,dict) or type(w.get('id')) is not int or w['id']<1 for w in windows)):
                raise ValueError()
            return {'state':'previous_windows','window_ids':[w['id'] for w in windows]}
        except (OSError,ValueError,TypeError,AttributeError):
            return {'state':'unknown','reason':'중지된 서비스의 마지막 관리 창 소멸을 확인할 증거가 없습니다.'}
    checked=_check_user(config,home,host,paths)
    from .display_health import process_identity,verify_direct_child
    from .monitors import ownership,bridge
    health=checked.get('health') or {}
    evidence=ownership(health)
    if not checked.get('process_verified'):
        try:
            if not evidence:raise ValueError()
            arguments=(paths.at('/proc')/pid/'cmdline').read_bytes().decode().rstrip('\0').split('\0')
            suffix=[['_chrome-run','--target',config.target_id]]
            if config.target_id=='default':suffix.append(['_chrome-run'])
            entry=Path(arguments[2])
            if (arguments[1]!='-I' or arguments[3:] not in suffix or entry.name!='entry.py'
                    or not (entry.parent in {RELEASE,Path('/opt/ala/current')} or entry.parent.is_relative_to('/opt/ala/releases'))):
                raise ValueError()
            verify_direct_child(paths,int(pid),health['browser_pid'],health)
            # Profile/private control ownership is independent of the desired
            # site's URL and of a requested fullscreen/half-screen change.
            expected=[arg for arg in chrome_arguments(config,home,{},supervised=True)[1:]
                      if arg!='--start-fullscreen']
            observed=list(_observe_chrome_process(paths.at('/proc')/str(health['browser_pid']),expected).arguments)
            for arg in expected:
                key=arg.split('=',1)[0]
                if [item for item in observed if item.split('=',1)[0]==key]!=[arg]:raise ValueError()
            if any(arg.split('=',1)[0] in {'--no-sandbox','--kiosk','--remote-debugging-port','--remote-debugging-address'} for arg in observed):
                raise ValueError()
        except (OSError,ValueError,IndexError,KeyError,StopIteration):
            return {'state':'unknown','reason':'중지 전 Chrome 자식의 소유권·시작 식별자를 확인하지 못했습니다.'}

    browser_pid=health.get('browser_pid') if evidence else int(pid)
    try:
        ticks=process_identity(paths,browser_pid)
    except (OSError,ValueError,IndexError,KeyError,StopIteration):
        return {'state':'unknown','reason':'중지 전 Chrome의 시작 식별자를 확인하지 못했습니다.'}
    window_ids=None
    if evidence:
        observed=bridge('Inspect',evidence | {'target':config.target_id,'layout':config.layout},host=host)
        values=observed.get('windows')
        if (observed.get('state')=='ok' and isinstance(values,list) and len(values)<=32
                and all(isinstance(w,dict) and type(w.get('id')) is int and w['id']>0 for w in values)):
            window_ids=[w['id'] for w in values]
    return {'state':'verified','browser_pid':browser_pid,'ticks':ticks,'window_ids':window_ids}


def _stop_postcondition(config,home,host,paths,evidence):
    if evidence is None:return None
    if evidence.get('state') not in {'verified','previous_windows'}:
        return {'state':'unknown','exit_code':1,'reason':evidence['reason'],
                'service_stopped':True,'next_action':'서비스는 중지했습니다. Chrome 프로세스와 실제 창의 종료는 다시 확인하세요.'}
    if evidence['state']=='verified':
        from .display_health import process_identity
        process=paths.at('/proc')/str(evidence['browser_pid'])
        try:
            ticks=process_identity(paths,evidence['browser_pid'])
            if ticks==evidence['ticks']:
                return {'state':'pending','exit_code':2,'service_stopped':True,
                        'reason':'서비스는 중지됐지만 같은 Chrome 자식이 아직 살아 있습니다. 외부 PID를 종료하지 않았습니다.',
                        'next_action':'잠시 후 프로세스와 창을 다시 확인하세요.'}
        except FileNotFoundError:
            pass
        except (OSError,ValueError,IndexError,KeyError,StopIteration):
            return {'state':'unknown','exit_code':1,'service_stopped':True,
                    'reason':'서비스 중지 후 Chrome 프로세스 소멸을 확인하지 못했습니다.'}
    if _foreign_profile_pid(paths,_profile(home,config.target_id)):
        return {'state':'unknown','exit_code':1,'service_stopped':True,
                'reason':'서비스 중지 후에도 같은 표시 프로필을 사용하는 프로세스가 있습니다. 자동 종료하지 않았습니다.'}
    ids=evidence['window_ids']
    if ids is None:
        return {'state':'unknown','exit_code':1,'service_stopped':True,'browser_stopped':True,
                'reason':'Chrome 프로세스는 종료됐으나 실제 관리 창의 소멸은 확인하지 못했습니다.',
                'next_action':'GNOME 화면 확장과 실제 창 상태를 확인하세요.'}
    if ids:
        from .monitors import bridge
        observed=bridge('Closed',{'target':config.target_id,'windows':ids},host=host)
        count=observed.get('remaining_window_count')
        if observed.get('state')!='ok' or type(count) is not int or count<0:
            return {'state':'unknown','exit_code':1,'service_stopped':True,'browser_stopped':True,
                    'reason':'Chrome 종료 후 실제 창의 소멸 관측을 완료하지 못했습니다.'}
        if count:
            return {'state':'pending','exit_code':2,'service_stopped':True,'browser_stopped':True,
                    'reason':'Chrome 프로세스는 종료됐으며 GNOME 관리 창이 사라지기를 기다립니다.'}
    return None

def user_action(action, *, target='default', config=None, home=None, host=None, paths=None, request=None):
    host, paths = host or Host(), paths or Paths()
    if config is None:
        config, home = _load_user_config(paths,None if action in {'prepare','autostart-sync','reload-units','settings','workspace-settings','outputs','identify-outputs','extension-status','environment','monitor-layout','extension-enable'} else target)
    elif os.getuid() == 0:
        raise PermissionError('Chrome은 일반 사용자로 실행하세요.')
    if action not in {'prepare','autostart-sync','reload-units','settings','workspace-settings','outputs','identify-outputs','extension-status','environment','monitor-layout','extension-enable'}:
        config=_context(config,target if not hasattr(config,'_target_id') else config.target_id)
    unit=unit_name(target if action=='environment' else config.target_id)
    home = Path(home)
    if action == 'extension-enable':
        return _extension_activation(host, apply=True)
    if action == 'monitor-layout':
        from .monitors import bridge
        if not isinstance(request, dict):
            raise ValueError('모니터 배치 입력이 필요합니다.')
        value = bridge('ApplyLayout', request, host=host)
        return value | {'exit_code': 0 if value.get('state') == 'ok' else 2}
    if action == 'reload-units':
        result=host.run(['systemctl','--user','daemon-reload'])
        return {'exit_code':result.returncode,'state':'ok' if result.returncode==0 else 'unknown'}
    if action == 'autostart-sync':
        return {'exit_code': 0, 'state': 'ok', 'changed': _prepare_autostart_file(config, home, paths)}
    if action == 'prepare':
        changed = _prepare_user_files(config, home, paths)
        try:
            workspace = _workspace_policy(config, host, apply=True)
        except (OSError, ValueError, RuntimeError):
            workspace = {'state':'unknown','exit_code':1,'reason':'DID 계정의 작업공간 설정을 완료하지 못했습니다.',
                         'next_action':'GNOME 로그인 후 간편 설정을 다시 실행하고 작업공간 상태를 확인하세요.'}
        extension=_extension_activation(host,apply=True) if config.display else None
        if extension and extension['state']!='ok':
            return extension | {'exit_code':2 if extension['state']=='pending' else 1,'workspace_policy':workspace}
        result = host.run(['systemctl','--user','daemon-reload'])
        return {'exit_code':0, 'state':'ok', 'changed':changed,'workspace_policy':workspace,
                'reason':result.stderr.strip() if result.returncode else '',
                'reload_pending':result.returncode != 0}
    if action == 'workspace-settings':
        return _workspace_policy(config, host)
    if action == 'settings':
        settings = {}
        for schema,key,_ in SETTINGS:
            value=host.run(['gsettings','get',schema,key])
            if value.returncode == 0:
                settings[schema+'.'+key] = value.stdout.strip()
        return {'exit_code':0 if len(settings)==len(SETTINGS) else 1,'settings':settings}
    if action == 'environment':
        try:
            _manager(host)
        except RuntimeError:
            return {'exit_code':2,'state':'pending','reason':'사용자 manager의 실제 화면 환경을 기다립니다.'}
        response=host.run(['systemctl','--user','show',unit,'--property=FragmentPath',
                           '--property=LoadState','--property=NeedDaemonReload'])
        values=_properties(response.stdout)
        if response.returncode or values.get('LoadState')!='loaded':
            return {'exit_code':2,'state':'pending','reason':'표시 서비스의 로드된 등록을 확인하지 못했습니다.'}
        if values.get('FragmentPath')!='/etc/systemd/user/'+unit:
            return {'exit_code':1,'state':'blocked','reason':'사용자 서비스가 예상한 root 관리 등록과 다릅니다.'}
        if values.get('NeedDaemonReload')!='no':
            return {'exit_code':2,'state':'pending','reason':'서비스 등록 재읽기가 필요합니다. 간편 설정을 확인하세요.'}
        return {'exit_code':0,'state':'ready','environment_ready':True,'registration_ready':True}
    if action == 'extension-status':
        value=_extension_activation(host)
        return value | {'exit_code':0 if value['state']=='ok' else 2}
    if action == 'outputs':
        from .monitors import topology
        value=topology(host=host)
        return value | {'exit_code':0 if value.get('state')=='ok' else 2}
    if action == 'identify-outputs':
        from .monitors import identify_outputs
        value = identify_outputs(host=host)
        return value | {'exit_code': 0 if value.get('state') == 'ok' else 2}
    if action == 'startup-status':
        current = _unit_state(host, unit)
        legacy = _unit_state(host, LEGACY) if config.target_id == 'default' else {'ActiveState': 'inactive'}
        confirmed = _confirmed_unit_state(current) and _confirmed_unit_state(legacy)
        return {'exit_code': 0 if confirmed else 1, 'state': 'ok' if confirmed else 'unknown',
                'service': current, 'legacy_service': legacy,
                'reason': '' if confirmed else '기존 표시 서비스의 상태를 확실히 조회하지 못했습니다.'}
    if action in {'windows','place'}:
        from .monitors import placement
        value=_check_user(config,home,host,paths)
        if (not value.get('process_verified') or (value.get('service') or {}).get('ActiveState') not in {'active','activating'}
                or (value.get('health') or {}).get('state') in {'unknown','failed'}):
            return {'exit_code':value.get('exit_code',2),'state':'unknown' if value.get('state') in {'unknown','error'} else 'pending',
                    'reason':'관리 Chrome의 프로세스·감시 소유권 검증을 먼저 완료해야 합니다.','confirmed_window_count':None}
        health=value.get('health') or {}
        observed=placement(config,health,host=host,apply=action=='place')
        return observed | {'exit_code':0 if observed.get('state') in {'ok','not_applicable'} else 2}
    if action == 'reload':
        from .frontend import request_reload
        checked=_check_user(config,home,host,paths)
        if checked.get('state')!='running':return checked
        health=checked.get('health') or {}
        revision=health.get('config_revision','')
        if not revision:return {'state':'pending','exit_code':2,'reason':'현재 화면의 적용 구성 식별자를 확인하지 못했습니다.'}
        return request_reload(config.target_id,revision,home,paths=paths)
    if action == 'logs':
        value=host.run(['journalctl','--user','--boot','-u',unit,'-n','100','--no-pager'])
        return {'exit_code':0 if value.returncode==0 else 1,'log':value.stdout,'reason':value.stderr.strip()}
    if action in {'check','status'}:
        value = _check_user(config,home,host,paths)
        if action == 'status':
            value['check_exit_code'] = value['exit_code']
            value['exit_code'] = 0
        return value
    if action not in {'start','stop','restart','ensure-start'}:
        raise ValueError('허용되지 않은 사용자 Chrome 작업입니다.')
    if action == 'stop':
        state = _unit_state(host,unit)
        if not _confirmed_unit_state(state):
            return {'exit_code':1,'state':'unknown','reason':'표시 서비스 상태를 확실히 조회하지 못해 중지하지 않았습니다.',
                    'next_action':'사용자 서비스 연결·권한을 확인한 뒤 다시 요청하세요.'}
        evidence=_stop_evidence(config,home,host,paths,state)
        if state.get('ActiveState') != 'inactive':
            result=host.run(['systemctl','--user','stop',unit],timeout=35)
            if result.returncode:
                return {'exit_code':1,'state':'error','reason':result.stderr.strip()}
            after = _unit_state(host,unit)
            if not _confirmed_unit_state(after):
                return {'exit_code':1,'state':'unknown','reason':'중지 요청 후 서비스 상태를 조회하지 못해 종료를 확인할 수 없습니다.',
                        'next_action':'사용자 서비스 연결과 Chrome 상태를 다시 확인하세요.'}
            if after.get('ActiveState') != 'inactive':
                return {'exit_code':1,'state':'blocked','reason':'중지 요청 후 서비스가 중지 상태로 전환되지 않았습니다.',
                        'next_action':'Chrome 로그와 서비스 상태를 확인하세요.'}
        postcondition=_stop_postcondition(config,home,host,paths,evidence)
        if postcondition:return postcondition
        reason = '현재 실행을 중지했습니다.'
        if config.display and config.autostart:
            reason += ' 다음 그래픽 로그인에는 자동 실행됩니다.'
        return {'exit_code':0,'state':'stopped','reason':reason}
    if action == 'ensure-start' and (not config.display or not config.autostart):
        return {'exit_code':0,'state':'not_applicable','reason':'Chrome 자동 시작이 꺼져 있습니다.'}
    if not config.display:
        return {'exit_code':2,'state':'pending','reason':'화면 사용 설정이 꺼져 있습니다.'}
    try:
        _manager(host)
    except RuntimeError as exc:
        if action != 'ensure-start':
            raise
        return {'exit_code':2,'state':'pending','reason':str(exc),
                'next_action':'다음 GNOME 로그인 또는 유지보수 재부팅 후 화면 환경을 다시 확인하세요.'}
    sessions, reason = _graphical_sessions(host,os.getuid())
    if sessions is None:
        return {'exit_code':1,'state':'unknown','reason':reason}
    if len(sessions)>1:
        return {'exit_code':1,'state':'blocked','reason':'그래픽 세션이 여러 개입니다.'}
    if not sessions or sessions[0][1].get('Active')!='yes':
        return {'exit_code':2,'state':'pending','reason':'활성 그래픽 세션이 없습니다.'}
    if action == 'ensure-start' and sessions[0][1].get('LockedHint') == 'yes':
        return {'exit_code':2,'state':'pending','reason':'현재 그래픽 세션이 잠겨 있습니다.',
                'next_action':'잠금 정책 적용 후 다음 GNOME 로그인 또는 유지보수 재부팅으로 확인하세요.'}
    old = _unit_state(host,LEGACY) if config.target_id=='default' else {'ActiveState':'inactive'}
    current = _unit_state(host,unit)
    if not _confirmed_unit_state(old) or not _confirmed_unit_state(current):
        return {'exit_code':1,'state':'unknown','reason':'기존 Chrome 서비스 상태를 확실히 조회하지 못해 실행 요청을 진행하지 않았습니다.',
                'next_action':'사용자 서비스 연결·권한을 확인한 뒤 다시 요청하세요.'}
    if action in {'start','ensure-start'} and old.get('ActiveState') in {'active','activating','deactivating','reloading'}:
        return {'exit_code':2,'state':'pending','reason':'기존 did-chrome 실행을 유지했습니다.',
                'next_action':'기존 표시를 유지하려면 그대로 두세요. 전환이 필요하면 명시적으로 ala chrome restart를 실행하세요.'}
    if action in {'start','ensure-start'} and current.get('ActiveState') in {'active','activating'}:
        return {'exit_code':0,'state':'kept','reason':'표시용 Chrome이 이미 실행 중입니다.'}
    if action == 'ensure-start' and current.get('ActiveState') == 'failed':
        return {'exit_code':1,'state':'error','reason':'표시용 Chrome의 실패 상태를 유지했습니다: ' + current.get('Result','unknown'),
                'next_action':'ala chrome logs로 원인을 확인한 뒤 필요한 경우 명시적으로 ala chrome restart를 실행하세요.'}
    if action == 'ensure-start' and current.get('ActiveState') != 'inactive':
        return {'exit_code':2,'state':'pending','reason':'표시용 Chrome 서비스 상태 변경을 기다립니다: ' + current.get('ActiveState','unknown')}
    if action == 'restart':
        for unit, state in ((LEGACY,old),(unit,current)):
            if state.get('ActiveState') != 'inactive':
                stopped=host.run(['systemctl','--user','stop',unit],timeout=35)
                if stopped.returncode:
                    return {'exit_code':1,'state':'blocked','reason':f'{unit} 종료를 확인할 수 없습니다.'}
                after = _unit_state(host,unit)
                if not _confirmed_unit_state(after):
                    return {'exit_code':1,'state':'unknown','reason':f'{unit} 중지 요청 후 상태를 조회하지 못했습니다.',
                            'next_action':'사용자 서비스 연결과 Chrome 상태를 확인하세요. 새 실행을 시작하지 않았습니다.'}
                if after.get('ActiveState') not in {'inactive','failed'}:
                    return {'exit_code':1,'state':'blocked','reason':f'{unit} 종료를 확인할 수 없습니다.'}
    foreign = _foreign_profile_pid(paths,_profile(home,config.target_id))
    if foreign:
        return {'exit_code':1,'state':'blocked','reason':f'관리 밖 프로세스 {foreign}가 표시 프로필을 사용 중입니다. 잠금 파일을 삭제하지 않았습니다.',
                'next_action':'해당 PID와 프로필 사용 여부를 직접 확인하세요. 외부 Chrome을 자동 종료하지 않습니다.'}
    if action == 'restart' and (current.get('ActiveState') == 'failed' or current.get('Result') == 'start-limit-hit'):
        reset=host.run(['systemctl','--user','reset-failed',unit])
        if reset.returncode:
            after = _unit_state(host,unit)
            cleared = (not after.get('QueryError') and after.get('LoadState') == 'loaded'
                       and after.get('ActiveState') == 'inactive' and after.get('Result') == 'success')
            if not cleared:
                return {'exit_code':1,'state':'error','reason':reset.stderr.strip()}
    reloaded=host.run(['systemctl','--user','daemon-reload'])
    if reloaded.returncode:
        return {'exit_code':1,'state':'error','reason':reloaded.stderr.strip()}
    start=host.run(['systemctl','--user','start','--no-block',unit])
    return {'exit_code':0 if start.returncode==0 else 1, 'state':'starting' if start.returncode==0 else 'error',
            'reason':'비동기 실행 요청 완료. status/logs로 사이트 준비 상태를 확인하세요.' if start.returncode==0 else start.stderr.strip()}


def _autostart_log(phase, outcome, rc=0, *, journal=False):
    message = f'[ala-autostart] phase={phase} outcome={outcome} rc={int(rc)}'
    print(message, flush=True)
    if journal:
        try:
            import syslog
            syslog.openlog('ala-autostart')
            syslog.syslog(syslog.LOG_INFO,message)
        except (ImportError,OSError):
            print('[ala-autostart] phase=journal outcome=unavailable rc=1', flush=True)


class _SessionHost:
    def __init__(self, host, environment):
        self.host, self.environment = host, environment

    def run(self, argv, **kwargs):
        environment = self.environment | (kwargs.get('env') or {})
        for key in ('XDG_RUNTIME_DIR','DBUS_SESSION_BUS_ADDRESS'):
            environment[key] = self.environment[key]
        kwargs['env'] = environment
        return self.host.run(argv, **kwargs)


def _session_connection(host, paths):
    response = host.run(['systemctl','--user','show-environment'])
    if response.returncode == 0:
        return host
    # This derives a bus address from logind's actual current-user runtime, not
    # an assumed UID path or a guessed graphical DISPLAY.
    runtime = host.run(['loginctl','show-user',str(os.getuid()),'--property=RuntimePath','--value'])
    if runtime.returncode:
        raise RuntimeError('현재 사용자 runtime을 확인할 수 없습니다.')
    value = runtime.stdout.strip()
    if not value.startswith('/') or any(c.isspace() or c in '\0,;=' for c in value):
        raise RuntimeError('현재 사용자 runtime 형식을 확인할 수 없습니다.')
    directory = paths.at(value)
    _safe_parents(directory)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError('현재 사용자 runtime 소유자·권한을 확인할 수 없습니다.')
    bus = directory/'bus'
    info = bus.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError('현재 사용자 bus socket을 확인할 수 없습니다.')
    connected = _SessionHost(host, dict(os.environ, XDG_RUNTIME_DIR=value,
                                      DBUS_SESSION_BUS_ADDRESS='unix:path='+value+'/bus'))
    if connected.run(['systemctl','--user','show-environment']).returncode:
        raise RuntimeError('현재 사용자 서비스 연결을 확인할 수 없습니다.')
    return connected


def session_start(*, config=None, home=None, host=None, paths=None, environment=None):
    host, paths = host or Host(), paths or Paths()
    journal = type(host) is Host and paths.root == Path('/')
    def log(phase,outcome,rc=0):
        _autostart_log(phase,outcome,rc,journal=journal)
    log('entry','entered')
    try:
        if config is None:
            config,home=_load_user_config(paths,None)
        elif os.getuid()==0:
            raise PermissionError('일반 사용자 세션에서 실행하세요.')
    except Exception:
        log('configuration','rejected',1)
        raise
    from .runtime import RuntimeStore
    from .display_onboarding import pending_for_user
    initializing = pending_for_user(config, paths)
    store = RuntimeStore(paths, host)
    targets = []
    codes = []
    if config.display:
        for target in config.screen_targets():
            try:
                from .screen_swap import guard
                guard(paths=paths, host=host, target=target.id)
                context, state = _committed_screen(config, target.id, store)
                if not (context.display and context.autostart):
                    continue
                if state is not None and store.is_stopped(state):
                    log('configuration', 'manual_stop')
                    continue
                targets.append(context)
            except (OSError, ValueError, RuntimeError, KeyError):
                log('configuration', 'selection_rejected', 1)
                codes.append(1)
    if not targets and not initializing:
        if not codes:
            log('configuration','disabled')
        return 1 if codes else 0
    log('configuration','accepted')
    try:
        host = _session_connection(host, paths)
    except (OSError, ValueError, RuntimeError):
        log('connection','unavailable',1)
        return 1
    log('connection','ready')
    environment = os.environ if environment is None else environment
    if environment.get('DISPLAY') or environment.get('WAYLAND_DISPLAY'):
        env = dict(os.environ)
        for name in ENV_NAMES:
            env.pop(name,None)
            if environment.get(name):
                env[name]=environment[name]
        cleared=host.run(['systemctl','--user','unset-environment','DISPLAY','WAYLAND_DISPLAY','XAUTHORITY'])
        if cleared.returncode:
            log('environment','clear_failed',cleared.returncode)
            return 1
        names=[name for name in ENV_NAMES if environment.get(name)]
        imported=host.run(['systemctl','--user','import-environment',*names],env=env)
        if imported.returncode:
            log('environment','import_failed',imported.returncode)
            return 1
        log('environment','imported')
    else:
        log('environment','manager_retained')
    try:
        workspace_environment = host.environment if isinstance(host, _SessionHost) else environment
        workspace = _workspace_policy(config, host, apply=True, environment=workspace_environment)
        outcome = ('applied' if workspace.get('changed') else 'ready') if workspace['state']=='ok' else workspace['state']
        log('workspace', outcome, workspace['exit_code'])
    except (OSError, ValueError, RuntimeError):
        # A workspace policy failure must not stop or replace existing Chrome.
        # The read-only workspace checks retain its unresolved settings.
        log('workspace', 'unknown', 1)
    if initializing:
        log('configuration', 'monitor_initialization_pending')
        return 78
    for context in targets:
        codes.append(_session_target_start(context,home,host,paths,log))
    if 0 in codes:
        from .monitors import dismiss_startup_overview
        try:
            sessions, _ = _graphical_sessions(BudgetHost(host, 3), os.getuid())
        except (OSError, RuntimeError, ValueError):
            sessions = None
        if (sessions and len(sessions) == 1 and sessions[0][1].get('Active') == 'yes'
                and sessions[0][1].get('LockedHint') == 'no'
                and environment.get('XDG_SESSION_ID', sessions[0][0]) == sessions[0][0]):
            log('overview', dismiss_startup_overview(host))
        else:
            log('overview', 'session_unavailable')
    return 1 if 1 in codes else 2 if 2 in codes else 0


def _session_target_start(config,home,host,paths,log):
    unit=unit_name(config.target_id)
    old = _unit_state(host,LEGACY) if config.target_id=='default' else {'ActiveState':'inactive'}
    current = _unit_state(host,unit)
    if not _confirmed_unit_state(old) or not _confirmed_unit_state(current):
        log('unit_state','unknown',1)
        return 1
    if (current.get('ActiveState') in {'active','activating'}
            and old.get('ActiveState') in {'active','activating','deactivating','reloading'}):
        log('unit_state','legacy_conflict',1)
        return 1
    if current.get('ActiveState') in {'active','activating'}:
        log('start','already_running')
        return 0
    if current.get('ActiveState') == 'failed' or current.get('Result') == 'start-limit-hit':
        log('start','failed_preserved',1)
        return 1
    if current.get('ActiveState') != 'inactive':
        log('start','transition_pending',2)
        return 2
    # Migration is authorized at an actual GNOME login. Duplicate login entry
    # calls preserve the new running unit and never restart it or reset limits.
    if old.get('ActiveState') in {'active','activating','deactivating','reloading'}:
        stopped = host.run(['systemctl','--user','stop',LEGACY],timeout=35)
        if stopped.returncode:
            log('legacy_stop','failed',stopped.returncode)
            return 1
        after = _unit_state(host,LEGACY)
        if not _confirmed_unit_state(after) or after.get('ActiveState') not in {'inactive','failed'}:
            log('legacy_stop','unconfirmed',1)
            return 1
        log('legacy_stop','confirmed')
    if _foreign_profile_pid(paths,_profile(Path(home),config.target_id)):
        log('profile','in_use',1)
        return 1
    reloaded = host.run(['systemctl','--user','daemon-reload'])
    if reloaded.returncode:
        log('reload','failed',reloaded.returncode)
        return 1
    started = host.run(['systemctl','--user','start','--no-block',unit])
    log('start','requested' if started.returncode == 0 else 'request_failed',started.returncode)
    return 0 if started.returncode == 0 else 1


class _ReadinessCanceled(RuntimeError):
    pass


def _wait_for_graphical_ready(host, paths, *, clock, sleeper, stop_requested):
    deadline = clock() + 45
    identified = None
    announced = False
    class DeadlineHost:
        def run(self, argv, **kwargs):
            remaining = deadline-clock()
            if remaining <= 0:
                raise TimeoutError('그래픽 준비 확인 제한 시간 초과')
            kwargs['timeout'] = min(kwargs.get('timeout',10),remaining)
            return host.run(argv,**kwargs)
    bounded = DeadlineHost()
    while True:
        if stop_requested():
            raise _ReadinessCanceled('화면 시작 대기가 중지되었습니다.')
        try:
            sessions, reason = _graphical_sessions(bounded,os.getuid())
            if sessions is None:
                raise RuntimeError('그래픽 로그인 세션을 확실히 조회하지 못했습니다.')
            if len(sessions) > 1:
                raise RuntimeError('그래픽 세션이 여러 개입니다. 임의로 선택하지 않습니다.')
            if not sessions and identified is not None:
                raise _ReadinessCanceled('대기하던 그래픽 로그인이 종료되었습니다.')
            if sessions:
                session, properties = sessions[0]
                if identified is not None and session != identified:
                    raise _ReadinessCanceled('대기하던 그래픽 로그인이 변경되었습니다.')
                identified = session
                manager = bounded.run(['systemctl','--user','show-environment'])
                if manager.returncode:
                    raise RuntimeError('사용자 서비스 환경을 확실히 조회하지 못했습니다.')
                values = _properties(manager.stdout)
                if properties.get('Active') == 'yes' and (values.get('DISPLAY') or values.get('WAYLAND_DISPLAY')):
                    if stop_requested():
                        raise _ReadinessCanceled('화면 시작 대기가 중지되었습니다.')
                    if clock() > deadline:
                        raise TimeoutError('그래픽 준비 확인 제한 시간 초과')
                    _autostart_log('readiness','ready')
                    return values
            if clock() >= deadline:
                raise TimeoutError('그래픽 준비 확인 제한 시간 초과')
            if not announced:
                _autostart_log('readiness','waiting',2)
                announced = True
            sleeper(max(0,min(1,deadline-clock())))
        except TimeoutError:
            _autostart_log('readiness','timed_out',1)
            raise RuntimeError('45초 안에 단일 활성 그래픽 세션과 화면 환경이 준비되지 않았습니다.') from None
        except _ReadinessCanceled:
            raise
        except RuntimeError:
            _autostart_log('readiness','blocked',1)
            raise


def url_ready(url, *, opener=None):
    from .display_health import probe_url
    return probe_url(url,opener=opener).ok


def run_chrome(*, target='default', config=None, home=None, host=None, paths=None, opener=None, supervisor_factory=None,
               clock=None, sleeper=None, stop_requested=None):
    host, paths = host or Host(), paths or Paths()
    if config is None:
        config,home=_load_user_config(paths,target,runtime=True)
    elif os.getuid()==0:
        raise PermissionError('Chrome은 일반 사용자로 실행하세요.')
    config=_context(config,target if not hasattr(config,'_target_id') else config.target_id)
    if not config.display:
        return 2
    from .display_onboarding import pending_for_user
    if pending_for_user(config, paths):
        return 78
    home=Path(home)
    try:
        manager = _wait_for_graphical_ready(host,paths,clock=clock or time.monotonic,
                                           sleeper=sleeper or time.sleep,stop_requested=stop_requested or (lambda:False))
    except _ReadinessCanceled:
        _autostart_log('readiness','canceled',78)
        return 78
    if config.schema_version==2:
        from .monitors import topology,resolve_output
        resolved=resolve_output(topology(host=host),config.output,auto=config.target_id=='default')
        if resolved['state']!='ok':
            _autostart_log('output','unresolved',78)
            return 78
        if config.output is None:
            object.__setattr__(config,'_output',resolved['selector'])
    profile=_profile(home,config.target_id)
    _safe_parents(profile)
    if _foreign_profile_pid(paths,profile,allowed=(os.getpid(),)):
        raise RuntimeError('관리 밖 Chrome이 표시 프로필을 사용 중입니다.')
    marker=profile/'.ala-display-profile'
    if profile.is_dir() and not marker.exists() and any(profile.iterdir()):
        raise RuntimeError('관리되지 않은 기존 표시 프로필을 자동 전환하지 않습니다.')
    profile.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(profile,0o700)
    if not marker.exists():
        atomic_write(marker,'ala display profile v1\n',0o600)
    from .display_health import Supervisor
    args=chrome_arguments(config,home,manager,supervised=True)
    options={}
    if config.target_id!='default' or config.config_revision or config.frontend.enabled or config.schema_version==2:
        options={'target_id':config.target_id,'config_revision':config.config_revision,'frontend':config.frontend}
    if config.schema_version==2 or config.output is not None:
        from .monitors import placement
        options['placement_callback']=lambda value:placement(config,value,host=host,apply=True)
    if config.url is None:
        def setup_context():
            from .monitors import topology, resolve_output
            selected = resolve_output(topology(host=host), config.output, auto=config.target_id == 'default')
            output = selected.get('output') or {}
            return {'output': output.get('id', config.output), 'display_number': output.get('display_number')}
        options['setup_context_callback'] = setup_context
    return (supervisor_factory or Supervisor)(config,home,args,paths=paths,opener=opener,**options).run()
