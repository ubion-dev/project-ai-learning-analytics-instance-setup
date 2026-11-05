"""Single managed display controls with separate configuration/run/site evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import re

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea

from .config import Config
from .model import redact

PREPARATION_IDS = {'display.configuration', 'display.autologin', 'display.gdm_enabled',
                   'display.default_target', 'display.initial_setup', 'chrome.unit', 'chrome.autostart'}
HEALTH_LABELS = {'healthy': '주 문서·HTTP 연결 확인', 'waiting_site': '사이트 응답 대기',
                 'waiting_configuration': '설정 안내 표시 중',
                 'recovering': '연결 복구 중', 'failed': '감시 실패', 'unknown': '감시 확인 불가'}
NAVIGATION_LABELS = {
    'last_command': ('마지막 ala 명령', {'display': '표시 주소 요청', 'recovery': '복구 안내 요청', 'setup': '설정 안내 요청'}),
    'last_browser_navigation': ('마지막 브라우저 이동', {
        'script': '스크립트 관련 관측', 'parser': '문서 파서 관련 관측',
        'other': '기타 관측', 'unknown': '구분 불가'}),
    'last_error': ('마지막 탐색 오류', {
        'aborted': '탐색 취소', 'download': '다운로드 전환', 'navigation_error': '탐색 오류'}),
}


def safe_text(value):
    return str(redact(value)) if value is not None else '확인 불가'


def navigation_details(health, *, running=False):
    """Present only bounded diagnostic fields; never infer a missing counter."""
    if not isinstance(health, dict):
        health = {}
    version = health.get('supervisor_version')
    valid_version = (isinstance(version, str) and len(version) <= 64 and
                     re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,3}(?:[-.]?(?:a|b|rc|dev|post)[0-9]+)?', version))
    lines = [('실행 중 감시 버전 · ' if running else '감시 기록 버전 · ') +
             (version if valid_version else '확인 불가')]
    if 'navigation' not in health:
        lines.append('실행 중 감시에 탐색 기록 없음 · 화면 관리에서 명시적 재시작 후 진단'
                     if running else '탐색 진단 · 실행 후 기록 확인')
        return lines
    navigation = health['navigation']
    if not isinstance(navigation, dict):
        lines.append('탐색 진단 · 기록 형식 확인 필요')
        return lines

    def count(key):
        value = navigation.get(key)
        return str(value) + '회' if type(value) is int and 0 <= value <= 2**63 - 1 else '확인 불가'

    lines.append('탐색 · ala 표시 요청 ' + count('display_requests') +
                 ' / 복구 안내 요청 ' + count('recovery_requests') +
                 ' / 브라우저 문서 이동 ' + count('browser_navigations'))
    for key, (label, choices) in NAVIGATION_LABELS.items():
        if key not in navigation:
            description = '확인 불가'
        elif navigation[key] is None:
            description = '기록 없음'
        elif isinstance(navigation[key], dict):
            item = navigation[key]
            category = item.get('initiator' if key == 'last_browser_navigation' else 'kind')
            description = choices.get(category, '범주 확인 불가') if isinstance(category, str) else '범주 확인 불가'
            when = item.get('at')
            try:
                if not isinstance(when, str) or len(when) > 64:
                    raise ValueError('invalid time')
                moment = datetime.fromisoformat(when)
                if moment.tzinfo is None:
                    raise ValueError('missing timezone')
                when = moment.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            except (ValueError, OverflowError):
                when = '시각 확인 불가'
            description += ' · ' + when
        else:
            description = '확인 불가'
        lines.append(label + ' · ' + description)
    lines.append('브라우저 관측 범주는 사용자 입력·브라우저 동작도 포함하며 원인 확정이 아닙니다.')
    return lines


def safe_logs(value):
    """Bounded display only; do not expose credential-shaped journal lines."""
    if not isinstance(value, str):
        return '로그 응답을 확인하지 못했습니다.'
    lines = []
    start = max(0, len(value) - 65536)
    tail = value[start:]
    if start and value[start - 1] != '\n':
        # Never interpret a credential value as an ordinary line after its key
        # was removed by the display bound. Resume only at a complete line.
        tail = tail.partition('\n')[2]
        lines.append('[잘린 로그 첫 행 숨김]')
    for line in tail.splitlines():
        if re.search(r'''(?i)(?:authorization|cookie|password|passwd|token|secret|api[_-]?key)["']?\s*[:=]|\bbearer\s+\S+''', line):
            lines.append('[인증정보가 포함된 로그 행 숨김]')
        else:
            lines.append(safe_text(line))
    return '\n'.join(lines)


class ChromeScreen(ModalScreen):
    BINDINGS = [('escape', 'close', '닫기'), ('q', 'close', '닫기')]
    CSS = """
    ChromeScreen { align: center middle; background: #000000 65%; }
    #chrome-dialog { width: 94%; max-width: 100; height: auto; max-height: 95%; padding: 1 2; background: #17212d; border: round #83e3c4; }
    #chrome-title { height: 1; text-style: bold; color: #b5e9d9; }
    #chrome-config { height: 4; }
    #chrome-registration, #chrome-runtime, #chrome-health { height: 1; }
    #chrome-detail { height: 6; min-height: 4; padding: 0 1; background: #0e151e; }
    #chrome-controls { height: 3; }
    #chrome-controls Button { width: 1fr; min-width: 8; padding: 0; }
    #chrome-help { height: 1; color: #aabdd0; }
    """

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self._view_closed = False
        self._reading = False
        self._result = None
        self._message = ''
        self._showing_logs = False
        self._logs = ''

    def compose(self) -> ComposeResult:
        with Vertical(id='chrome-dialog'):
            yield Static('화면 관리', id='chrome-title')
            yield Static('', id='chrome-config', markup=False)
            yield Static('', id='chrome-registration', markup=False)
            yield Static('', id='chrome-runtime', markup=False)
            yield Static('', id='chrome-health', markup=False)
            yield TextArea('', id='chrome-detail', read_only=True, show_cursor=False, highlight_cursor_line=False)
            with Horizontal(id='chrome-controls'):
                yield Button('시작', id='chrome-start', variant='primary', disabled=True)
                yield Button('중지', id='chrome-stop', disabled=True)
                yield Button('재시작', id='chrome-restart', disabled=True)
                yield Button('상태', id='chrome-status')
                yield Button('로그', id='chrome-logs')
                yield Button('닫기', id='chrome-close')
            yield Static('단일 관리 Chrome · 모니터별 배치는 미지원 · Esc 닫기', id='chrome-help')

    def alive(self):
        return self.is_mounted and not self._view_closed and self.owner.is_running

    def on_mount(self):
        self.render_state()
        self.set_interval(2, self.refresh_status)
        self.run_worker(self.refresh_status(), exit_on_error=False)

    def on_unmount(self):
        self._view_closed = True

    def action_close(self):
        self._view_closed = True
        self.dismiss()

    def prepared(self):
        config = self.owner._config or {}
        report = self.owner._setup_report or {}
        nested = report.get('configuration_report')
        rows = (nested if isinstance(nested, dict) else report).get('results', [])
        rows = rows if isinstance(rows, list) else []
        checks = {row.get('id'): row.get('state') for row in rows if isinstance(row, dict)
                  and row.get('id') in PREPARATION_IDS}
        required = ['display.configuration', 'chrome.unit']
        if config.get('autostart'):
            required.append('chrome.autostart')
        return bool(self.owner._status_known and all(checks.get(key) == 'ok' for key in required))

    def has_valid_target(self):
        try:
            config = Config.from_dict(self.owner._config or {})
            return bool(config.user and config.display)
        except (ValueError, TypeError):
            return False

    def render_state(self):
        if not self.alive():
            return
        config, value = self.owner._config or {}, self._result or {}
        enabled = lambda key: '켜짐' if config.get(key) else '꺼짐'
        text = (f"관리 계정 · {safe_text(config.get('user') or '미지정')}\n"
                f"표시 URL · {safe_text(config.get('url') or '입력 필요')}\n"
                f"부팅 자동 로그인 · {enabled('autologin')} / Chrome 자동 시작 · {enabled('autostart')}")
        self.query_one('#chrome-config', Static).update(Text(text))
        ready = self.prepared()
        self.query_one('#chrome-registration', Static).update(Text(
            '설정 준비 · ' + ('등록 준비 완료' if ready else '등록·설정 검사 필요'), style='green' if ready else 'yellow'))
        service = value.get('service') if isinstance(value.get('service'), dict) else {}
        active = service.get('ActiveState') if not service.get('QueryError') else None
        active = active if isinstance(active, str) else None
        runtime = {'active': '실행 중', 'activating': '시작 중', 'inactive': '현재 중지됨',
                   'failed': '실행 실패', 'deactivating': '중지 중'}.get(active, '실행 상태 확인 불가')
        self.query_one('#chrome-runtime', Static).update(Text('현재 실행 · ' + runtime,
            style='cyan' if active == 'active' else 'red' if active == 'failed' else 'yellow' if active is None else ''))
        health = value.get('health') if isinstance(value.get('health'), dict) else {}
        health_state = health.get('state', 'unknown')
        health_state = health_state if isinstance(health_state, str) else 'unknown'
        if value.get('state') in {'unknown', 'error', 'failed'} or active != 'active':
            health_state = 'unknown'
        health_label = '중지 상태 · 실행 후 감시' if active == 'inactive' else HEALTH_LABELS.get(health_state, '감시 확인 불가')
        self.query_one('#chrome-health', Static).update(Text('사이트 감시 · ' + health_label,
            style='' if active == 'inactive' else 'green' if health_state == 'healthy' else 'yellow'))
        details = [self._message] if self._message else []
        if not ready:
            details.append('간편 설정에서 자동 시작 등록을 준비한 뒤 다시 상태를 확인하세요.')
        if self.owner._chrome_mutating:
            details.append('화면 제어 요청을 처리 중입니다. 완료 후 실행 상태를 다시 확인합니다.')
        if self.owner._active_job or (self.owner._update_status or {}).get('state') in {'queued', 'running', 'unknown'}:
            details.append('설치·갱신 작업의 종료와 상태를 확인한 뒤 화면을 제어하세요.')
        if active == 'inactive' and config.get('autostart'):
            details.append('현재는 중지 상태입니다. 다음 그래픽 로그인에는 자동 시작합니다.')
        if active == 'active' or health:
            details.extend(navigation_details(health, running=active == 'active'))
        for label, item in (('원인', value.get('reason')), ('다음', value.get('next_action')),
                            ('감시 원인', health.get('reason')), ('감시 다음', health.get('next_action')),
                            ('마지막 검사', health.get('last_checked'))):
            if item:
                details.append(f'{label}: {safe_text(item)}')
        for row in value.get('results', []):
            if isinstance(row, dict) and row.get('state') not in {'ok', 'not_applicable'}:
                if row.get('next_action'):
                    details.append('다음: ' + safe_text(row['next_action']))
                if not isinstance(row.get('actual'), dict):
                    details.append(safe_text(row.get('id')) + ': ' + safe_text(row.get('actual')))
        details.append('문서·HTTP 관측과 실제 모니터 표시·앱 내부 데이터는 별도입니다.')
        detail = self.query_one('#chrome-detail', TextArea)
        detail.load_text(self._logs if self._showing_logs else '\n'.join(details))
        detail.refresh()
        busy = self._reading or self.owner._chrome_mutating
        locked = busy or bool(self.owner._active_job) or (self.owner._update_status or {}).get('state') in {'queued', 'running', 'unknown'}
        can_start = ready and self.has_valid_target() and active == 'inactive' and value.get('state') != 'unknown'
        self.query_one('#chrome-start', Button).disabled = locked or not can_start
        self.query_one('#chrome-restart', Button).disabled = locked or not (ready and self.has_valid_target() and active in {'active', 'inactive', 'failed'})
        self.query_one('#chrome-stop', Button).disabled = locked or active not in {'active', 'activating', 'failed'}
        self.query_one('#chrome-status', Button).disabled = busy
        self.query_one('#chrome-logs', Button).disabled = busy

    async def refresh_status(self):
        if not self.alive() or self._reading or self.owner._chrome_mutating:
            return
        self._reading = True
        self.render_state()
        try:
            value = await self.owner.service.call('chrome', {'action': 'status'})
            if not isinstance(value, dict) or not isinstance(value.get('state'), str):
                raise ValueError('invalid response')
        except Exception:
            value = {'state': 'unknown', 'reason': '화면 상태 조회 실패 · 연결과 권한을 확인하세요.'}
        finally:
            self._reading = False
        self._result = value
        if self.alive():
            self.render_state()

    @on(Button.Pressed)
    def button_pressed(self, event):
        action = (event.button.id or '').removeprefix('chrome-')
        if action == 'close':
            self.action_close()
        elif action == 'status':
            self._showing_logs = False
            self.run_worker(self.refresh_status(), exit_on_error=False)
        elif action == 'logs':
            self.run_worker(self.fetch_logs(), exit_on_error=False)
        elif action in {'start', 'stop', 'restart'} and not event.button.disabled:
            self.owner.run_worker(self.perform_action(action), group='chrome-control', exit_on_error=False)

    async def fetch_logs(self):
        if not self.alive() or self._reading or self.owner._chrome_mutating:
            return
        self._reading = True
        self.render_state()
        try:
            value = await self.owner.service.call('chrome', {'action': 'logs'})
            self._logs = safe_logs(value.get('log')) if isinstance(value, dict) and not value.get('exit_code') else '로그 조회 실패 · 연결과 권한을 확인하세요.'
        except Exception:
            self._logs = '로그 조회 실패 · 연결과 권한을 확인하세요.'
        finally:
            self._reading = False
        self._showing_logs = True
        if self.alive():
            self.render_state()

    async def perform_action(self, action):
        if not self.alive() or self.owner._chrome_mutating or self.query_one('#chrome-' + action, Button).disabled:
            return
        self.owner._chrome_mutating = True
        self.owner.update_button()
        self._showing_logs = False
        self._message = {'start': '시작', 'stop': '중지', 'restart': '재시작'}[action] + ' 요청 중…'
        self.render_state()
        try:
            value = await self.owner.service.call('chrome', {'action': action})
            if not isinstance(value, dict):
                raise ValueError('invalid response')
            if value.get('exit_code') not in {0, 2}:
                self._message = '요청 실패 · ' + safe_text(value.get('reason') or value.get('error') or '원인 확인 필요')
            else:
                self._message = '요청 결과 · ' + safe_text(value.get('reason') or value.get('state')) + '\n실제 실행·감시 상태는 별도 조회 결과를 확인하세요.'
            status = await self.owner.service.call('chrome', {'action': 'status'})
            self._result = status if isinstance(status, dict) else {'state': 'unknown'}
        except Exception:
            self._message = '요청 또는 결과 조회를 확인하지 못했습니다. 상태를 다시 확인하세요.'
            self._result = {'state': 'unknown'}
        finally:
            self.owner._chrome_mutating = False
            if self.owner.is_running:
                self.owner.update_button()
            if self.alive():
                self.render_state()
