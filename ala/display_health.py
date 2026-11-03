"""Ordinary-user Chrome supervision; HTTP reachability is not app health."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import stat
import time
import urllib.error
import urllib.parse
import urllib.request

from .cdp import CDPError, PipeBrowser
from .system import Paths, atomic_json

INTERVAL = 30
TIMEOUT = 5
FAILURES = 3
SUCCESSES = 2
NAVIGATION_TIMEOUT = 20
FRESHNESS = 90
RECOVERY_HTML = '''<!doctype html><html lang="ko"><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>연결 복구 중</title><style>html{background:#101923;color:#e8eff8;font-family:sans-serif}
body{height:90vh;display:grid;place-content:center;text-align:center}h1{font-size:3rem}
p{font-size:1.3rem;color:#b7c9dc}</style><h1>연결 복구 중</h1>
<p>화면 서비스를 확인하고 있습니다.<br>연결이 복구되면 자동으로 돌아갑니다.</p></html>'''
RECOVERY_URL = 'data:text/html;charset=utf-8,' + urllib.parse.quote(RECOVERY_HTML)
STATES = {'waiting_site', 'healthy', 'recovering', 'failed', 'unknown'}


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


def state_path(home):
    return Path(home) / '.local/state/ala/chrome-health.json'


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


def read_health(home, paths, supervisor_pid, url, *, now=None):
    """Read only; never create a state file or infer health from an old record."""
    loaded = False
    try:
        value = _read_json(state_path(home))
        loaded = True
        if not isinstance(value, dict) or value.get('state') not in STATES:
            raise ValueError('감시 상태 형식이 올바르지 않습니다.')
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
                 transport_factory=None, clock=None, monotonic=None, stop_requested=None):
        self.config, self.home, self.argv = config, Path(home), list(argv)
        self.paths = paths or Paths()
        self.opener = opener
        self.transport_factory = transport_factory or PipeBrowser
        self.clock, self.monotonic = clock or time.time, monotonic or time.monotonic
        self.stop_requested = stop_requested or (lambda: False)
        self.stopping = False
        self.browser = None
        self.target = self.session = self.frame = self.loader = self.request = None
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
                      'last_checked': None, 'last_success': None, 'http_status': None,
                      'target_matches_config': False, 'consecutive_failures': 0,
                      'reason': '사이트 응답 확인 대기', 'next_action': 'HTTP 연결을 자동으로 재확인합니다.',
                      'config_fingerprint': fingerprint(config.url)}

    def _save(self):
        path = state_path(self.home)
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
        self.browser = self.transport_factory(self.argv)
        self.value['browser_pid'] = self.browser.pid
        self.value['browser_start_ticks'] = process_identity(self.paths, self.browser.pid)
        # Only the target opened by this launch is eligible. Never attach to a
        # matching URL in somebody else's browser or guess an arbitrary tab.
        for _ in range(10):
            targets = self.browser.call('Target.getTargets', timeout=TIMEOUT).get('targetInfos', [])
            matches = [item for item in targets if item.get('type') == 'page' and item.get('url') == RECOVERY_URL]
            if len(matches) == 1:
                self.target = matches[0].get('targetId')
                break
            if len(matches) > 1:
                raise CDPError('표시용 관리 탭이 여러 개라 선택하지 않았습니다.')
            self.browser.next_event(timeout=0.1)
        if not self.target:
            raise CDPError('이번 실행에서 연 표시용 탭을 확인하지 못했습니다.')
        self.session = self.browser.call('Target.attachToTarget', {'targetId': self.target, 'flatten': True}, timeout=TIMEOUT).get('sessionId')
        if not isinstance(self.session, str) or not self.session:
            raise CDPError('표시용 탭 제어 연결을 확인하지 못했습니다.')
        self._call('Page.enable')
        self._call('Network.enable')
        self._call('Page.setLifecycleEventsEnabled', {'enabled': True})
        self.frame = self._call('Page.getFrameTree').get('frameTree', {}).get('frame', {}).get('id')
        if not self.frame:
            raise CDPError('표시용 탭의 주 문서를 확인하지 못했습니다.')
        self._save()

    def _navigate(self, url):
        result = self._call('Page.navigate', {'url': url})
        if result.get('errorText') or result.get('isDownload'):
            if url == RECOVERY_URL:
                raise CDPError('로컬 복구 안내를 표시하지 못했습니다.')
            self._fallback('표시 주소로 이동하는 중 네트워크 오류가 발생했습니다.')
            return
        if result.get('frameId') != self.frame or not result.get('loaderId'):
            raise CDPError('표시용 탭의 문서 이동 식별자를 확인하지 못했습니다.')
        if url != RECOVERY_URL:
            self.loader = result['loaderId']
            self.request = None
            self.document_ok = self.loaded = False
            self.navigating = True
            self.managed_navigation = True
            self.observed_document_url = self.config.url
            self.deadline = self.monotonic() + NAVIGATION_TIMEOUT

    def _fallback(self, reason):
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
            if self.managed_navigation and self.observed_document_url:
                self.allowed_reload_urls = {self.config.url, self.observed_document_url}
            self.managed_navigation = False
            self.navigating = False
            self.deadline = None
            self.value.update(state='healthy', target_matches_config=True,
                              reason='관리 탭의 주 문서 HTTP 응답과 로드를 확인했습니다.',
                              next_action='앱 내부 데이터·실제 모니터 표시는 별도 수용 범위입니다.')
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
        if method == 'Network.requestWillBeSent' and params.get('type') == 'Document' and params.get('frameId') == self.frame:
            if self.loader and params.get('loaderId') != self.loader:
                if params.get('request', {}).get('url') not in self.allowed_reload_urls:
                    raise CDPError('관리 탭이 설정한 주소와 다른 새 주 문서로 이동했습니다.')
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
                    self._fallback('표시용 주 문서 로드 확인 시간이 초과되었습니다.')
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
