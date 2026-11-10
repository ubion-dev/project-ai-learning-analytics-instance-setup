"""Ordinary-user Chrome supervision; HTTP reachability is not app health."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import stat
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request

from .cdp import CDPError, CDPResponseTimeout, PipeBrowser
from . import __version__
from .system import Paths, atomic_json

INTERVAL = 30
TIMEOUT = 5
FAILURES = 3
SUCCESSES = 2
NAVIGATION_TIMEOUT = 20
STARTUP_TIMEOUT = 30
FRESHNESS = 90
RECOVERY_HTML = '''<!doctype html><html lang="ko"><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>연결 복구 중</title><style>html{background:#101923;color:#e8eff8;font-family:sans-serif}
body{height:90vh;display:grid;place-content:center;text-align:center}h1{font-size:3rem}
p{font-size:1.3rem;color:#b7c9dc}</style><h1>연결 복구 중</h1>
<p>화면 서비스를 확인하고 있습니다.<br>연결이 복구되면 자동으로 돌아갑니다.</p></html>'''
RECOVERY_URL = 'data:text/html;charset=utf-8,' + urllib.parse.quote(RECOVERY_HTML)
STATES = {'waiting_site', 'waiting_configuration', 'healthy', 'recovering', 'failed', 'unknown'}


def setup_page(*, output=None, display_number=None, target_id='default', preparing=False):
    """An internal document, never a user-configurable URL or network endpoint."""
    number = str(display_number) if type(display_number) is int and 1 <= display_number <= 16 else '확인 필요'
    label = html.escape(str(output or '출력 확인 대기'))
    target = html.escape(str(target_id))
    heading = '기기 설정 중' if preparing else '화면 설정 중'
    context = '모니터 연결과 표시 환경을 준비하고 있습니다.' if preparing else f'화면 {number} · {label} · DID {target}'
    document = f'''<!doctype html><html lang="ko"><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>ALA 화면 설정 안내</title><style>
html{{background:#17161d;color:#e9f0f7;font-family:sans-serif}}
body{{margin:0;min-height:100vh;display:grid;place-content:center;text-align:center;padding:0 5vw;box-sizing:border-box}}
.number{{font-size:clamp(4rem,15vw,12rem);color:#b5e9d9;font-weight:bold}}
h1{{font-size:clamp(1.5rem,3vw,3rem)}}p{{font-size:clamp(1rem,2vw,2rem);color:#cbc2ef}}
.setup-image{{width:min(42vw,480px);height:24vh;margin:auto;color:#b5e9d9}}
.pulse{{animation:pulse 2s ease-in-out infinite}}
@keyframes pulse{{50%{{opacity:.35}}}}
@media(prefers-reduced-motion:reduce){{.pulse{{animation:none}}}}
</style><svg class="setup-image" viewBox="0 0 320 180" role="img" aria-label="모니터 설정 안내">
<rect x="15" y="16" width="210" height="130" rx="12" fill="#242735" stroke="currentColor" stroke-width="5"/>
<path d="M98 147v18m-30 0h60" fill="none" stroke="currentColor" stroke-width="5" stroke-linecap="round"/>
<rect x="206" y="57" width="100" height="98" rx="10" fill="#242735" stroke="#cbc2ef" stroke-width="5"/>
<g fill="currentColor" class="pulse"><circle cx="78" cy="80" r="7"/><circle cx="120" cy="80" r="7"/><circle cx="162" cy="80" r="7"/></g>
</svg><h1>{heading}</h1><p>{context}</p>
<p>준비가 끝나면 이 안내가 모니터별 화면으로 이어집니다.<br>화면 관리에서 모니터 배치와 사이트를 설정해 주세요.</p>
<p>SSH에서 ala 실행 → 화면 관리 → 사이트 주소 저장 → 설정 적용</p></html>'''
    return 'data:text/html;charset=utf-8,' + urllib.parse.quote(document)


@dataclass(frozen=True)
class Probe:
    ok: bool
    status: int | None
    reason: str


def probe_url(url, *, opener=None):
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'ala-display-readiness/1',
                                                     'Cache-Control': 'no-cache'})
        with (opener or urllib.request.urlopen)(request, timeout=TIMEOUT) as response:
            code = response.status
            return Probe(200 <= code < 400, code, 'HTTP 응답 확인' if 200 <= code < 400 else 'HTTP 오류 응답')
    except urllib.error.HTTPError as exc:
        return Probe(False, exc.code, 'HTTP 오류 응답')
    except (TimeoutError, socket.timeout):
        return Probe(False, None, 'HTTP 연결 제한 시간 초과')
    except urllib.error.URLError as exc:
        reason = ('TLS 인증서 검증 또는 연결 실패' if isinstance(exc.reason, ssl.SSLError)
                  else 'HTTP 연결 제한 시간 초과' if isinstance(exc.reason, TimeoutError)
                  else 'HTTP 연결 실패')
        return Probe(False, None, reason)
    except (OSError, ValueError):
        return Probe(False, None, 'HTTP 연결 확인 실패')


def state_path(home, target_id='default'):
    from .frontend import _target
    _target(target_id)
    base = Path(home) / '.local/state/ala'
    return base / 'chrome-health.json' if target_id == 'default' else base / 'screens' / target_id / 'chrome-health.json'


def _safe_path(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('감시 상태 경로의 심볼릭 링크를 허용하지 않습니다.')


def boot_id(paths):
    value = paths.at('/proc/sys/kernel/random/boot_id').read_text().strip()
    if not value or len(value) > 128:
        raise ValueError('현재 부팅 ID를 확인하지 못했습니다.')
    return value


def process_identity(paths, pid):
    if type(pid) is not int or pid < 1:
        raise ValueError('유효한 프로세스 ID가 필요합니다.')
    process = paths.at('/proc') / str(pid)
    uid = next(line.split()[1] for line in (process / 'status').read_text().splitlines() if line.startswith('Uid:'))
    if int(uid) != os.getuid():
        raise ValueError('표시 사용자와 프로세스 소유자가 다릅니다.')
    # comm may contain spaces or parentheses; fields after its final ')' start at 3.
    ticks = (process / 'stat').read_text().rsplit(') ', 1)[1].split()[19]
    if not ticks.isdecimal():
        raise ValueError('프로세스 시작 식별자를 읽지 못했습니다.')
    return ticks


def verify_direct_child(paths, supervisor_pid, browser_pid, record):
    """Verify lineage independently of a browser's self-selected systemd scope."""
    if record.get('supervisor_pid') != supervisor_pid or record.get('browser_pid') != browser_pid:
        raise ValueError('Chrome 감시 기록과 현재 서비스·자식 PID가 다릅니다.')
    if record.get('boot_id') != boot_id(paths):
        raise ValueError('Chrome 감시 기록의 부팅 정보가 변경되었습니다.')
    status = (paths.at('/proc') / str(browser_pid) / 'status').read_text()
    fields = dict(line.split(':', 1) for line in status.splitlines() if ':' in line)
    uids = fields.get('Uid', '').split()
    if not uids or any(not value.isdecimal() or int(value) != os.getuid() for value in uids):
        raise ValueError('Chrome 자식의 사용자 소유권을 확인할 수 없습니다.')
    parent = fields.get('PPid', '').strip()
    if not parent.isdecimal() or int(parent) != supervisor_pid:
        raise ValueError('Chrome 프로세스가 현재 감독 서비스의 직접 자식이 아닙니다.')
    # Re-read identities after the caller inspected argv: an old record or PID
    # reuse must not authorize an unrelated process with a similar command line.
    if record.get('supervisor_start_ticks') != process_identity(paths, supervisor_pid):
        raise ValueError('Chrome 감독 프로세스의 시작 식별자가 변경되었습니다.')
    if record.get('browser_start_ticks') != process_identity(paths, browser_pid):
        raise ValueError('Chrome 자식 프로세스의 시작 식별자가 변경되었습니다.')


def fingerprint(url):
    return hashlib.sha256((url or '').encode()).hexdigest()


def stamp(now):
    return datetime.fromtimestamp(now, timezone.utc).isoformat()


def _read_json(path):
    _safe_path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 16384:
        raise ValueError('감시 상태 파일의 소유자·권한·크기가 올바르지 않습니다.')
    # O_NOFOLLOW also closes the final-component replacement race.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        current = os.fstat(stream.fileno())
        if current.st_ino != info.st_ino or current.st_dev != info.st_dev:
            raise ValueError('감시 상태 파일이 조회 중 변경되었습니다.')
        content = stream.read(16385)
        if len(content) > 16384:
            raise ValueError('감시 상태 파일 크기 제한을 초과했습니다.')
        return json.loads(content)


def read_health(home, paths, supervisor_pid, url, *, now=None, target_id='default'):
    """Read only; never create a state file or infer health from an old record."""
    loaded = False
    try:
        value = _read_json(state_path(home, target_id))
        loaded = True
        if not isinstance(value, dict) or value.get('state') not in STATES:
            raise ValueError('감시 상태 형식이 올바르지 않습니다.')
        if value.get('target_id', 'default') != target_id:
            raise ValueError('다른 화면 대상의 감시 기록입니다.')
        if value.get('supervisor_pid') != supervisor_pid or value.get('boot_id') != boot_id(paths):
            raise ValueError('현재 서비스·부팅의 감시 기록이 아닙니다.')
        if value.get('supervisor_start_ticks') != process_identity(paths, supervisor_pid):
            raise ValueError('감독 프로세스가 변경되었습니다.')
        browser_pid = value.get('browser_pid')
        if value.get('browser_start_ticks') != process_identity(paths, browser_pid):
            raise ValueError('Chrome 프로세스가 변경되었습니다.')
        age = (time.time() if now is None else now) - datetime.fromisoformat(value['updated_at']).timestamp()
        if age < -5 or age > FRESHNESS:
            raise ValueError('Chrome 감시 기록이 오래되었거나 시간이 다릅니다.')
        if value.get('config_fingerprint') != fingerprint(url):
            return value | {'state': 'unknown', 'target_matches_config': False,
                            'reason': '실행 중인 감시 구성과 저장된 DID 주소가 다릅니다.',
                            'next_action': '화면 관리에서 명시적으로 다시 시작해 새 구성을 적용하세요.'}
        for key in ('target_matches_config',):
            if type(value.get(key)) is not bool:
                raise ValueError('감시 상태 판정 값이 올바르지 않습니다.')
        if url is None and (value['state'] == 'healthy' or value['target_matches_config']):
            raise ValueError('사이트 주소가 없는 안내 화면은 사이트 정상으로 판정하지 않습니다.')
        if value['state'] == 'waiting_configuration' and (url is not None or type(value.get('local_page_ready')) is not bool):
            raise ValueError('설정 안내 상태와 적용 구성이 다릅니다.')
        return value
    except FileNotFoundError:
        if loaded:
            return {'state': 'unknown', 'reason': '감시 기록에 대응하는 현재 프로세스·부팅 정보를 찾지 못했습니다.',
                    'next_action': 'Chrome 프로세스와 서비스 상태를 다시 확인하세요.'}
        return {'state': 'waiting_site', 'reason': '실행 중인 감독 프로세스의 감시 기록을 아직 받지 못했습니다.',
                'next_action': '잠시 후 다시 확인하세요. 대기가 계속되면 Chrome 로그를 확인하세요.'}
    except (OSError, ValueError, KeyError, IndexError, StopIteration, TypeError):
        return {'state': 'unknown', 'reason': '현재 Chrome의 유효하고 최신인 감시 기록을 확인하지 못했습니다.',
                'next_action': 'Chrome 로그와 서비스 상태를 확인하세요.'}


class Supervisor:
    def __init__(self, config, home, argv, *, paths=None, opener=None,
                 transport_factory=None, clock=None, monotonic=None, stop_requested=None,
                 target_id='default', config_revision='', frontend=None, placement_callback=None,
                 setup_context_callback=None):
        from .frontend import FrontendConfig, _target
        self.config, self.home, self.argv = config, Path(home), list(argv)
        self.target_id = _target(target_id)
        if not isinstance(config_revision, str) or len(config_revision) > 128:
            raise ValueError('화면 구성 revision이 올바르지 않습니다.')
        self.config_revision = config_revision
        self.frontend = FrontendConfig.from_dict(frontend) if isinstance(frontend, dict) else frontend or FrontendConfig()
        self.run_nonce = uuid.uuid4().hex
        self._control = None
        self._last_control = None
        self.placement_callback = placement_callback
        self.setup_context_callback = setup_context_callback
        self._setup_url = None
        self._next_placement = 0
        self._next_frontend_observation = 0
        self.paths = paths or Paths()
        self.opener = opener
        self.transport_factory = transport_factory or PipeBrowser
        self.clock, self.monotonic = clock or time.time, monotonic or time.monotonic
        self.stop_requested = stop_requested or (lambda: False)
        self.stopping = False
        self.browser = None
        self.target = self.session = self.frame = self.loader = self.request = None
        self._bootstrap_loader = None
        self._bootstrap_pending = False
        self.navigating = False
        self.document_ok = self.loaded = False
        self.deadline = None
        self.successes = 0
        self.had_outage = False
        # At most the configured URL and one verified final redirect URL. These
        # stay in memory; health JSON never contains browsing destinations.
        self.allowed_reload_urls = {config.url}
        self.managed_navigation = False
        self.observed_document_url = None
        self.value = {'state': 'waiting_site', 'browser_pid': None, 'supervisor_pid': os.getpid(),
                      'target_id': self.target_id, 'config_revision': config_revision, 'run_nonce': self.run_nonce,
                      'control': None,
                      'supervisor_version': __version__,
                      'navigation': {'display_requests': 0, 'recovery_requests': 0, 'browser_navigations': 0,
                                     'last_command': None, 'last_browser_navigation': None, 'last_error': None},
                      'last_checked': None, 'last_success': None, 'http_status': None,
                      'target_matches_config': False, 'consecutive_failures': 0,
                      'reason': '사이트 응답 확인 대기', 'next_action': 'HTTP 연결을 자동으로 재확인합니다.',
                      'config_fingerprint': fingerprint(config.url)}
        if self.config.url is None:
            self.value.update(state='waiting_configuration', local_page_ready=False,
                              reason='설정 안내 문서 준비 중', next_action='화면 관리에서 사이트 주소를 저장하고 적용하세요.')

    def _save(self):
        path = state_path(self.home, self.target_id)
        _safe_path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = path.parent.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError('감시 상태 디렉터리의 소유자·권한이 안전하지 않습니다.')
        if path.exists():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError('관리 밖 감시 상태 파일을 변경하지 않습니다.')
        self.value['updated_at'] = stamp(self.clock())
        atomic_json(path, self.value, 0o600)

    def _call(self, method, params=None):
        return self.browser.call(method, params, session_id=self.session, timeout=TIMEOUT)

    def start(self):
        if os.getuid() == 0:
            raise PermissionError('Chrome 감시는 일반 사용자로 실행하세요.')
        self.value['boot_id'] = boot_id(self.paths)
        self.value['supervisor_start_ticks'] = process_identity(self.paths, os.getpid())
        startup_deadline = self.monotonic() + STARTUP_TIMEOUT
        self.browser = self.transport_factory(self.argv)
        self.value['browser_pid'] = self.browser.pid
        self.value['browser_start_ticks'] = process_identity(self.paths, self.browser.pid)

        def startup_call(method, params=None, *, session_id=None):
            if self.browser.poll() is not None:
                raise CDPError('표시용 Chrome 프로세스가 종료되었습니다.')
            remaining = startup_deadline - self.monotonic()
            if remaining <= 0:
                raise CDPError('표시용 Chrome 제어 연결 준비 시간이 초과되었습니다.')
            options = {'timeout': min(TIMEOUT, remaining)}
            if session_id is not None:
                options['session_id'] = session_id
            try:
                result = self.browser.call(method, params, **options)
            except CDPResponseTimeout:
                if self.browser.poll() is not None:
                    raise CDPError('표시용 Chrome 프로세스가 종료되었습니다.') from None
                if self.monotonic() >= startup_deadline:
                    raise CDPError('표시용 Chrome 제어 연결 준비 시간이 초과되었습니다.') from None
                raise
            if self.browser.poll() is not None:
                raise CDPError('표시용 Chrome 프로세스가 종료되었습니다.')
            if self.monotonic() >= startup_deadline:
                raise CDPError('표시용 Chrome 제어 연결 준비 시간이 초과되었습니다.')
            return result

        # Only the target opened by this launch is eligible. Never attach to a
        # matching URL in somebody else's browser or guess an arbitrary tab.
        while True:
            if self.stopping or self.stop_requested():
                return
            try:
                targets = startup_call('Target.getTargets').get('targetInfos', [])
            except CDPResponseTimeout:
                # Only this read-only query is safe to retry after the complete
                # request was sent. Late replies keep their old CDP request ID.
                continue
            matches = [item for item in targets if item.get('type') == 'page' and item.get('url') == RECOVERY_URL]
            if len(matches) == 1:
                self.target = matches[0].get('targetId')
                break
            if len(matches) > 1:
                raise CDPError('표시용 관리 탭이 여러 개라 선택하지 않았습니다.')
            remaining = startup_deadline - self.monotonic()
            if remaining <= 0:
                raise CDPError('표시용 Chrome 제어 연결 준비 시간이 초과되었습니다.')
            self.browser.next_event(timeout=min(0.1, remaining))
        if not self.target:
            raise CDPError('이번 실행에서 연 표시용 탭을 확인하지 못했습니다.')
        if self.stopping or self.stop_requested():
            return
        self.session = startup_call('Target.attachToTarget', {'targetId': self.target, 'flatten': True}).get('sessionId')
        if not isinstance(self.session, str) or not self.session:
            raise CDPError('표시용 탭 제어 연결을 확인하지 못했습니다.')
        for method, params in (('Page.enable', None), ('Network.enable', None),
                               ('Page.setLifecycleEventsEnabled', {'enabled': True})):
            if self.stopping or self.stop_requested():
                return
            startup_call(method, params, session_id=self.session)
        if self.stopping or self.stop_requested():
            return
        initial_frame = startup_call('Page.getFrameTree', session_id=self.session).get('frameTree', {}).get('frame', {})
        self.frame = initial_frame.get('id')
        if (initial_frame.get('url') == RECOVERY_URL and
                isinstance(initial_frame.get('loaderId'), str) and initial_frame['loaderId']):
            self._bootstrap_loader = initial_frame['loaderId']
            self._bootstrap_pending = True
        if not self.frame:
            raise CDPError('표시용 탭의 주 문서를 확인하지 못했습니다.')
        self._save()

        if self.config.url is None:
            self.setup_tick()

    def _navigate(self, url, *, ensure_document=False):
        kind = 'setup' if self.config.url is None and url == self._setup_url else 'recovery' if url == RECOVERY_URL else 'display'
        navigation = self.value['navigation']
        navigation[kind + '_requests'] = navigation.get(kind + '_requests', 0) + 1
        navigation['last_command'] = {'kind': kind, 'at': stamp(self.clock())}
        if kind == 'setup':
            self.value['local_page_ready'] = False
        if kind == 'display':
            # A request may start changing the document before CDP replies.
            # Record that fact before error handling so an initial aborted
            # navigation also requests the real recovery page.
            self.navigating = True
        try:
            result = self._call('Page.navigate', {'url': url})
        except CDPError:
            navigation['last_error'] = {'kind': 'navigation_error', 'at': stamp(self.clock())}
            raise
        if result.get('errorText') or result.get('isDownload'):
            error_text = result.get('errorText')
            error_kind = ('download' if result.get('isDownload') else
                          'aborted' if isinstance(error_text, str) and error_text in {'net::ERR_ABORTED', 'ERR_ABORTED'} else 'navigation_error')
            navigation['last_error'] = {'kind': error_kind, 'at': stamp(self.clock())}
            if kind == 'setup':
                raise CDPError('로컬 설정 안내를 표시하지 못했습니다.')
            if url == RECOVERY_URL:
                raise CDPError('로컬 복구 안내를 표시하지 못했습니다.')
            self._fallback('표시 주소로 이동하는 중 네트워크 오류가 발생했습니다.')
            return
        if result.get('frameId') == self.frame and not result.get('loaderId') and ensure_document:
            # Removing/changing a SPA fragment may be a same-document move.
            # An explicit reload still needs one real document load. Do not
            # infer a deployment from that fragment or repeat this operation.
            operation = (self._control or {}).get('operation_token')
            if self.stopping or self.stop_requested() or (operation is not None and not self._valid_operation(operation)):
                self.navigating = False
                self._finish_control(False)
                return
            self.loader = self.request = None
            self.document_ok = self.loaded = False
            self.navigating = self.managed_navigation = True
            self.observed_document_url = self.config.url
            self.deadline = self.monotonic() + NAVIGATION_TIMEOUT

            self._call('Page.reload', {'ignoreCache': True})
            return
        if result.get('frameId') != self.frame or not result.get('loaderId'):
            navigation['last_error'] = {'kind': 'navigation_error', 'at': stamp(self.clock())}
            raise CDPError('표시용 탭의 문서 이동 식별자를 확인하지 못했습니다.')
        if url != RECOVERY_URL:
            self.loader = result['loaderId']
            self.request = None
            self.document_ok = self.loaded = False
            self.navigating = True
            self.managed_navigation = True
            self.observed_document_url = self.config.url
            self.deadline = self.monotonic() + NAVIGATION_TIMEOUT

    def setup_tick(self):
        """Keep only our local document; no HTTP or frontend readiness checks."""
        context = {}
        if self.setup_context_callback is not None:
            try:
                context = self.setup_context_callback()
                if not isinstance(context, dict):
                    context = {}
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                context = {}
        url = setup_page(output=context.get('output', self.config.output),
                         display_number=context.get('display_number'), target_id=self.target_id,
                         preparing=context.get('preparing') is True)
        self.value.update(last_checked=stamp(self.clock()), state='waiting_configuration',
                          http_status=None, target_matches_config=False,
                          reason='설정 안내 표시 중' if self.value.get('local_page_ready') else '설정 안내 문서 확인 중',
                          next_action='화면 관리에서 사이트 주소를 저장하고 적용하세요.')
        if self._setup_url != url:
            self._setup_url = url
            self.value['local_page_ready'] = False
            self._navigate(url)
        self._save()

    def _fallback(self, reason):
        if self._control is not None:
            self._finish_control(False)
        self.had_outage = True
        was_displaying = self.value['target_matches_config'] or self.navigating
        self.navigating = False
        self.managed_navigation = False
        self.observed_document_url = None
        self.loader = self.request = self.deadline = None
        self.document_ok = self.loaded = False
        self.successes = 0
        self.value.update(state='recovering', target_matches_config=False, reason=reason,
                          next_action='30초 간격으로 재확인하며 정상 응답 2회 후 원래 화면으로 복귀합니다.')
        if was_displaying:
            self._navigate(RECOVERY_URL)
        self._save()

    def probe(self):
        if self.config.url is None:
            self.setup_tick()
            return
        result = probe_url(self.config.url, opener=self.opener)
        self.value.update(last_checked=stamp(self.clock()), http_status=result.status)
        if result.ok:
            self.value['last_success'] = stamp(self.clock())
            self.value['consecutive_failures'] = 0
            self.successes += 1
            required = SUCCESSES if self.had_outage else 1
            if self.value['state'] != 'healthy' and not self.navigating and self.successes >= required:
                self.value.update(state='recovering', reason='HTTP 연결 복구 후 실제 탭의 문서 응답을 확인 중입니다.',
                                  next_action='표시용 탭의 주 문서 응답과 로드를 확인합니다.')
                self._navigate(self.config.url)
        else:
            self.had_outage = True
            self.successes = 0
            self.value['consecutive_failures'] += 1
            if self.value['state'] == 'healthy' and self.value['consecutive_failures'] >= FAILURES:
                self._fallback(result.reason)
            elif self.value['state'] != 'healthy' and not self.navigating:
                self.value.update(reason=result.reason, next_action='HTTP 연결을 자동으로 재확인합니다.')
        self._save()

    def _healthy_if_complete(self):
        if self.document_ok and self.loaded:
            if self.config.url is None:
                self.navigating = False
                self.deadline = None
                self.value.update(state='waiting_configuration', local_page_ready=True,
                                  target_matches_config=False, reason='설정 안내 표시 중',
                                  next_action='화면 관리에서 사이트 주소를 저장하고 적용하세요.')
                self._finish_control(True)
                self._save()
                return
            if self.managed_navigation and self.observed_document_url:
                self.allowed_reload_urls = {self.config.url, self.observed_document_url}
            self.managed_navigation = False
            self.navigating = False
            self.deadline = None
            self.value.update(state='healthy', target_matches_config=True,
                              reason='관리 탭의 주 문서 HTTP 응답과 로드를 확인했습니다.',
                              next_action='앱 내부 데이터·실제 모니터 표시는 별도 수용 범위입니다.')
            self._save()

    def _finish_control(self, success):
        request = self._control
        if request is None:
            return
        self.value['control'] = {'nonce': request['nonce'], 'state': 'completed' if success else 'failed',
                                 'deployment_seq': request['deployment_seq'],
                                 'build_id': request['expected_build_id'] if success else None,
                                 'app_ready': success if request['expected_build_id'] is not None else None}
        self._control = None
        self._save()

    def observe_frontend(self):
        from .frontend import TOKEN
        expression = "(()=>({build_id:document.querySelector('meta[name=\"ala-build-id\"]')?.content??null,ready:document.querySelector('meta[name=\"ala-app-ready\"]')?.content==='true'}))()"
        response = self._call('Runtime.evaluate', {'expression': expression, 'returnByValue': True})
        value = response.get('result', {}).get('value')
        build = value.get('build_id') if isinstance(value, dict) else None
        ready = value.get('ready') if isinstance(value, dict) else None
        result = {'build_id': build if isinstance(build, str) and TOKEN.fullmatch(build) else None,
                  'app_ready': ready if type(ready) is bool else None, 'observed_at': stamp(self.clock())}
        self.value['frontend'] = result
        self._save()
        return result

    def frontend_tick(self):
        if not self.frontend.enabled or self.value['state'] != 'healthy' or self.navigating or self.monotonic() < self._next_frontend_observation:
            return
        self._next_frontend_observation = self.monotonic() + 5
        try:
            self.observe_frontend()
        except CDPError:
            self.value['frontend'] = {'build_id': None, 'app_ready': None, 'observed_at': stamp(self.clock())}
            self._save()

    def control_tick(self):
        """Only this supervisor can use its own CDP session for a fixed request."""
        from .frontend import _read_owned, control_path, valid_request
        if self.stopping or self.stop_requested() or self.session is None:
            return
        if self._control is not None:
            operation = self._control.get('operation_token')
            if operation is not None and not self._valid_operation(operation):
                self._finish_control(False)
                return
            if self.clock() > self._control['expires_at']:
                self._finish_control(False)
            elif self.value['state'] == 'healthy' and self.document_ok and self.loaded and not self.navigating:
                expected = self._control['expected_build_id']
                if expected is None:
                    self._finish_control(True)
                else:
                    # Fixed read-only expression, never interpolate URL/build ID
                    # or evaluate script text from configuration or a request.
                    try:
                        value = self.observe_frontend()
                        if value['build_id'] == expected and value['app_ready'] is True:
                            self._finish_control(True)
                    except CDPError:
                        self._finish_control(False)
            return
        try:
            request = _read_owned(control_path(self.home, self.target_id))
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(request, dict) or request.get('nonce') == self._last_control:
            return
        if not valid_request(request, self.target_id, self.config_revision, self.run_nonce, now=self.clock()):
            return
        if self.navigating:
            return
        self._last_control = request['nonce']
        if request.get('operation_token') is not None and not self._valid_operation(request['operation_token']):
            self.value['control'] = {'nonce': request['nonce'], 'state': 'rejected'}
            self._save()
            return
        if request['source'] == 'frontend' and (not self.frontend.enabled or self.config.url is None):
            self.value['control'] = {'nonce': request['nonce'], 'state': 'rejected'}
            self._save()
            return
        self._control = request
        self.value['control'] = {'nonce': request['nonce'], 'state': 'pending'}
        self._save()
        self._navigate(self._setup_url if self.config.url is None else self.config.url, ensure_document=True)

    def _valid_operation(self, token):
        from . import runtime
        try:
            return runtime.check(token, paths=self.paths)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            return False

    def placement_tick(self):
        if self.placement_callback is None or self.monotonic() < self._next_placement or self.stopping or self.stop_requested():
            return
        self._next_placement = self.monotonic() + 5
        try:
            value = self.placement_callback(dict(self.value))
            if not isinstance(value, dict):
                raise ValueError('invalid placement result')
        except (OSError, ValueError, RuntimeError, TypeError, KeyError, AttributeError):
            value = {'state': 'unknown', 'reason': '관리 창 배치를 확인하지 못했습니다.'}
        self.value['placement'] = value
        self._save()

    def event(self, event):
        method, params = event.get('method'), event.get('params', {})
        if not isinstance(method, str) or not isinstance(params, dict):
            raise CDPError('브라우저 제어 이벤트 형식이 올바르지 않습니다.')
        if method == 'Target.targetDestroyed' and params.get('targetId') == self.target:
            raise CDPError('관리 중인 표시용 탭이 종료되었습니다.')
        if event.get('sessionId') != self.session:
            return
        if method in {'Inspector.detached', 'Inspector.targetCrashed'}:
            raise CDPError('표시용 탭 제어가 종료되었습니다.')
        if self.config.url is None:
            if method == 'Page.frameNavigated':
                frame = params.get('frame', {})
                if frame.get('id') == self.frame and not frame.get('parentId'):
                    if (self._bootstrap_pending and self.navigating and not self.document_ok and
                            frame.get('url') == RECOVERY_URL and
                            frame.get('loaderId') == self._bootstrap_loader and self.loader != self._bootstrap_loader):
                        # Page.enable can queue the verified launch document's
                        # commit before our setup navigation. It is not evidence
                        # that the new setup document loaded or failed.
                        return
                    if (self.navigating and self.loader is None and frame.get('url') == self._setup_url
                            and isinstance(frame.get('loaderId'), str) and frame['loaderId']):
                        self.loader = frame['loaderId']
                    if frame.get('url') != self._setup_url or frame.get('loaderId') != self.loader:
                        raise CDPError('설정 안내 문서의 실제 표시를 확인하지 못했습니다.')
                    self.document_ok = True
                    self._bootstrap_pending = False
                    self._healthy_if_complete()
            elif (method == 'Page.lifecycleEvent' and params.get('frameId') == self.frame
                  and params.get('loaderId') == self.loader and params.get('name') == 'load'):
                self.loaded = True
                self._healthy_if_complete()
            return
        if method == 'Network.requestWillBeSent' and params.get('type') == 'Document' and params.get('frameId') == self.frame:
            if not self.loader and self.navigating and self.managed_navigation:
                requested = params.get('request', {}).get('url')
                loader = params.get('loaderId')
                if (not isinstance(requested, str) or urllib.parse.urldefrag(requested)[0] != urllib.parse.urldefrag(self.config.url)[0]
                        or not isinstance(loader, str) or not loader):
                    raise CDPError('명시적 새로고침의 실제 주 문서를 확인하지 못했습니다.')
                self.loader = loader
            if self.loader and params.get('loaderId') != self.loader:
                if params.get('request', {}).get('url') not in self.allowed_reload_urls:
                    raise CDPError('관리 탭이 설정한 주소와 다른 새 주 문서로 이동했습니다.')
                if not isinstance(params.get('loaderId'), str) or not params['loaderId']:
                    raise CDPError('새 주 문서 이동의 식별자를 확인하지 못했습니다.')
                navigation = self.value['navigation']
                navigation['browser_navigations'] += 1
                initiator = params.get('initiator')
                initiator_type = initiator.get('type') if isinstance(initiator, dict) else None
                navigation['last_browser_navigation'] = {
                    'initiator': initiator_type if isinstance(initiator_type, str) and initiator_type in {'script', 'parser', 'other'} else 'unknown',
                    'at': stamp(self.clock()),
                }
                self._save()
                self.loader = params.get('loaderId')
                self.document_ok = self.loaded = False
                self.navigating = True
                self.managed_navigation = False
                self.deadline = self.monotonic() + NAVIGATION_TIMEOUT
            if self.loader and params.get('loaderId') == self.loader:
                self.request = params.get('requestId')
                if self.managed_navigation and isinstance(params.get('request', {}).get('url'), str):
                    self.observed_document_url = params['request']['url']
        elif method == 'Network.responseReceived' and params.get('type') == 'Document' and params.get('frameId') == self.frame:
            if self.loader and params.get('loaderId') == self.loader:
                if self.managed_navigation and isinstance(params.get('response', {}).get('url'), str):
                    self.observed_document_url = params['response']['url']
                code = params.get('response', {}).get('status')
                if isinstance(code, (int, float)) and code >= 400:
                    self.value['http_status'] = int(code)
                    self._fallback('관리 탭의 주 문서에서 HTTP 오류 응답을 확인했습니다.')
                elif isinstance(code, (int, float)) and 200 <= code < 400:
                    self.document_ok = True
                    self._healthy_if_complete()
        elif method == 'Network.loadingFailed' and self.request and params.get('requestId') == self.request:
            self.value['navigation']['last_error'] = {
                'kind': 'aborted' if params.get('errorText') == 'net::ERR_ABORTED' or params.get('canceled') is True else 'navigation_error',
                'at': stamp(self.clock()),
            }
            self._fallback('관리 탭의 주 문서에서 네트워크 오류를 확인했습니다.')
        elif method == 'Page.lifecycleEvent' and params.get('frameId') == self.frame and params.get('loaderId') == self.loader:
            if params.get('name') == 'load':
                self.loaded = True
                self._healthy_if_complete()
        elif method == 'Page.frameNavigated':
            frame = params.get('frame', {})
            if frame.get('id') == self.frame and not frame.get('parentId') and frame.get('url') != RECOVERY_URL:
                if self.loader and frame.get('loaderId') != self.loader:
                    raise CDPError('관리 탭이 예상한 문서 이동과 달라 제어 상태를 확인할 수 없습니다.')
                if self.managed_navigation and self.loader == frame.get('loaderId') and isinstance(frame.get('url'), str):
                    self.observed_document_url = frame['url']

    def run(self):
        previous = {}
        try:
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous[signum] = signal.signal(signum, lambda *_: setattr(self, 'stopping', True))
            self.start()
            next_probe = self.monotonic()
            while not self.stopping and not self.stop_requested():
                if self.browser.poll() is not None:
                    raise CDPError('표시용 Chrome 프로세스가 종료되었습니다.')
                now = self.monotonic()
                if now >= next_probe:
                    self.probe()
                    next_probe = self.monotonic() + INTERVAL
                event = self.browser.next_event(timeout=min(1, max(0, next_probe - self.monotonic())))
                if event is not None:
                    self.event(event)
                if self.deadline is not None and self.monotonic() > self.deadline:
                    if self.config.url is None:
                        raise CDPError('설정 안내 문서의 로드 확인 시간이 초과되었습니다.')
                    self._fallback('표시용 주 문서 로드 확인 시간이 초과되었습니다.')
                self.control_tick()
                self.frontend_tick()
                self.placement_tick()
            self.value.update(state='unknown', reason='표시 서비스가 중지되었습니다.', next_action='다음 시작 시 감시를 다시 시작합니다.')
            self._save()
            return 0
        except (CDPError, OSError, ValueError, KeyError, IndexError, StopIteration, TypeError, AttributeError) as exc:
            self.value.update(state='failed' if isinstance(exc, CDPError) else 'unknown',
                              reason=str(exc) if isinstance(exc, CDPError) else '표시 감시 실행 또는 기록을 확인하지 못했습니다.',
                              next_action='Chrome 로그와 서비스 상태를 확인하세요. 반복 실패는 기존 systemd 시작 제한을 유지합니다.')
            print(self.value['reason'], flush=True)
            try:
                self._save()
            except (OSError, ValueError):
                pass
            return 1
        finally:
            if self.browser is not None:
                self.browser.close()
            for signum, handler in previous.items():
                signal.signal(signum, handler)
