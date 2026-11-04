"""Explicit enrollment and maintenance controls; status reads never enroll."""
from __future__ import annotations

from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Select, Static, Switch, TextArea

from .chrome_tui import safe_text


class AutomaticScreen(ModalScreen):
    BINDINGS = [('escape', 'close', '닫기'), ('q', 'close', '닫기')]
    CSS = """
    AutomaticScreen { align: center middle; background: #000000 65%; }
    #automatic-dialog { width: 88; max-width: 96%; height: 30; max-height: 95%; padding: 1 2; background: #17212d; border: round #83e3c4; }
    #automatic-title { height: 1; color: #b5e9d9; text-style: bold; }
    #automatic-state { height: 1; padding: 0 1; background: #172735; color: #b5e9d9; text-style: bold; text-wrap: nowrap; text-overflow: ellipsis; }
    #automatic-next { height: 1; padding: 0 1; text-wrap: nowrap; text-overflow: ellipsis; }
    #automatic-body { height: 1fr; min-height: 6; }
    #automatic-policy { height: 3; margin-top: 1; }
    #automatic-detail { height: 5; min-height: 4; }
    #automatic-target { height: 3; }
    #automatic-ack { height: 3; }
    #automatic-ack-label { width: 1fr; height: 3; content-align: left middle; }
    .automatic-controls { height: 3; }
    .automatic-controls Button { width: 1fr; min-width: 10; padding: 0; }
    """

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.value = {}
        self.message = ''
        self.request_failed = False
        self.reading = False
        self.working = False
        config = owner._config or {}
        self.targets = [v['id'] for v in config.get('screens', [])] or ['default']

    def compose(self):
        with Vertical(id='automatic-dialog'):
            yield Static('자동 운영 · 최초 준비와 야간 적용', id='automatic-title')
            yield Static('자동 운영 상태 확인 중', id='automatic-state', markup=False)
            yield Static('', id='automatic-next', markup=False)
            with VerticalScroll(id='automatic-body'):
                yield Select([(key, key) for key in self.targets], value=self.targets[0], allow_blank=False, id='automatic-target')
                with Horizontal(id='automatic-ack'):
                    yield Static('실제 모니터·재부팅·첫 화면 코드 적용을 확인했습니다.', id='automatic-ack-label')
                    yield Switch(False, id='automatic-accept')
                yield Static('정식 CLI · 6시간마다 분산 조회\n화면 코드 · 매일 03:00–04:00 한국 시간\n현재 화면 유지 · 자동 재부팅 없음', id='automatic-policy')
                yield TextArea('', id='automatic-detail', read_only=True, show_cursor=False, highlight_cursor_line=False)
            with Horizontal(classes='automatic-controls'):
                yield Button('최초 준비', id='automatic-prepare')
                yield Button('선택 화면 코드 적용', id='automatic-apply')
                yield Button('닫기', id='automatic-close')
            with Horizontal(classes='automatic-controls'):
                yield Button('자동 운영 켜기', id='automatic-enable', disabled=True)
                yield Button('자동 운영 끄기', id='automatic-disable')
                yield Button('상태 확인', id='automatic-refresh')

    def on_mount(self):
        self.set_interval(3, self.refresh_status)
        self.run_worker(self.refresh_status(), exit_on_error=False)

    def alive(self):
        return self.is_mounted and self.owner.is_running and self.is_current

    async def refresh_status(self):
        if not self.alive() or self.reading:
            return
        self.reading = True
        try:
            value = await self.owner.service.call('auto-update-status')
            self.value = value if isinstance(value, dict) else {'state': 'unknown'}
        except Exception as exc:
            self.value = {'state': 'unknown', 'reason': safe_text(str(exc))}
        finally:
            self.reading = False
        self.render_state()

    def priority(self, bootstrap, accepted):
        if self.working:
            return '자동 운영 작업 요청 중', '작업 접수 결과를 기다리세요.', None, 'cyan'
        if self.owner._active_job or (self.owner._update_status or {}).get('state') in {'queued', 'running'}:
            return '다른 설정·업데이트 작업 진행 중', '작업이 끝난 뒤 자동 운영을 설정하세요.', None, 'cyan'
        if self.request_failed:
            return '자동 운영 요청 실패', '세부 결과를 확인한 뒤 상태 확인을 선택하세요.', 'refresh', 'red'
        if not self.value:
            return '자동 운영 상태 확인 중', '조회 결과를 기다리세요.', None, 'cyan'
        if self.value.get('state') == 'unknown':
            return '자동 운영 상태 확인 불가', '상태 확인을 선택하세요.', 'refresh', 'yellow'
        state = bootstrap.get('state')
        if state in {'queued', 'running'}:
            return '최초 준비 진행 중', '서명 검증과 등록 결과를 확인하세요.', 'refresh', 'cyan'
        if state in {'failed', 'interrupted', 'unknown'}:
            label = {'failed': '최초 준비 실패', 'interrupted': '최초 준비 중단', 'unknown': '최초 준비 상태 확인 불가'}[state]
            return label, '최초 준비의 세부 결과를 확인하세요.', None, 'red'
        if self.value.get('verifier') != 'ready':
            return '서명 검증기 준비 필요', '최초 준비를 선택해 설치와 서명을 확인하세요.', 'prepare', 'yellow'
        if self.value.get('enabled'):
            return '자동 운영 켜짐', '정식 CLI 분산 조회와 03:00–04:00 화면 적용 정책을 확인하세요.', None, 'green'
        if state != 'completed':
            return '최초 준비 확인 필요', '최초 준비를 선택해 설치와 서명을 확인하세요.', 'prepare', 'yellow'
        if not accepted:
            return '자동 운영 꺼짐 · 실제 화면 확인 필요', '선택 화면 코드 적용 후 실제 표시와 재부팅을 확인하세요.', 'apply', 'yellow'
        return '실제 화면 확인 표시됨', '확인 결과를 검토하고 자동 운영 켜기를 선택하세요.', 'enable', 'yellow'

    def render_state(self):
        if not self.alive():
            return
        value = self.value
        lines = [self.message] if self.message else []
        if value and value.get('state') != 'unknown':
            lines.append('자동 운영 · '+('켜짐' if value.get('enabled') else '꺼짐 · 준비·수용 확인 필요'))
            lines.append('서명 검증기 · '+('준비됨' if value.get('verifier') == 'ready' else '준비·검증 필요'))
        bootstrap = value.get('bootstrap')
        bootstrap = bootstrap if isinstance(bootstrap, dict) else {}
        if bootstrap.get('state'):
            labels = {'queued': '시작 대기', 'running': '진행 중', 'completed': '완료', 'failed': '실패', 'interrupted': '중단됨'}
            lines.append('최초 준비 · '+labels.get(bootstrap['state'], '확인 필요'))
            if bootstrap.get('phase'):
                phases = {'verifier': '고정 서명 검증기 준비', 'installed_signature': '현재 설치 배포 서명·내용 대조', 'registering': '자동 운영 등록 준비'}
                lines.append(phases.get(bootstrap['phase'], '단계 확인 필요'))
            if bootstrap.get('reason'):
                lines.append(safe_text(bootstrap['reason']))
        for key in ('reason', 'next_action'):
            if value.get(key):
                lines.append(safe_text(value[key]))
        self.query_one('#automatic-detail', TextArea).load_text('\n'.join(lines))
        blocked = self.working or bool(self.owner._active_job) or (self.owner._update_status or {}).get('state') in {'queued', 'running'}
        for key in ('prepare', 'apply', 'disable'):
            self.query_one('#automatic-'+key, Button).disabled = blocked
        accepted = self.query_one('#automatic-accept', Switch).value
        self.query_one('#automatic-enable', Button).disabled = blocked or not accepted
        headline, next_action, primary, tone = self.priority(bootstrap, accepted)
        self.query_one('#automatic-state', Static).update(Text(headline, style=tone))
        self.query_one('#automatic-next', Static).update(Text('다음 · '+next_action))
        for key in ('prepare', 'apply', 'enable', 'refresh'):
            button = self.query_one('#automatic-'+key, Button)
            button.variant = 'primary' if key == primary and not button.disabled else 'default'

    @on(Switch.Changed, '#automatic-accept')
    def acceptance_changed(self):
        self.render_state()

    @on(Button.Pressed)
    def pressed(self, event):
        key = (event.button.id or '').removeprefix('automatic-')
        if key == 'close':
            self.action_close()
        elif key == 'refresh':
            self.request_failed = False
            self.run_worker(self.refresh_status(), exit_on_error=False)
        elif key in {'prepare', 'apply', 'enable', 'disable'} and not event.button.disabled:
            self.owner.run_worker(self.change(key), group='automatic-control', exit_on_error=False)

    async def change(self, key):
        if self.working:
            return
        target = self.query_one('#automatic-target', Select).value
        accepted = self.query_one('#automatic-accept', Switch).value
        if key == 'enable' and not accepted:
            return
        actions = {'prepare': ('auto-update-bootstrap', {}),
                   'apply': ('runtime-apply', {'target': target}),
                   'enable': ('auto-update-enable', {'accepted': accepted}),
                   'disable': ('auto-update-disable', {})}
        self.working = True
        self.request_failed = False
        self.render_state()
        try:
            action, payload = actions[key]
            result = await self.owner.service.call(action, payload)
            self.request_failed = result.get('state') in {'failed', 'interrupted', 'unknown', 'blocked'} or result.get('exit_code') == 1
            self.message = safe_text(result.get('reason') or result.get('state') or '결과 확인 필요')
            if result.get('next_action'):
                self.message += '\n'+safe_text(result['next_action'])
        except Exception as exc:
            self.request_failed = True
            self.message = '요청 실패 · '+safe_text(str(exc))
        finally:
            self.working = False
        await self.refresh_status()

    def action_close(self):
        self.dismiss()
