"""Read-only boot triage and explicitly confirmed per-DID controls."""
import asyncio
from time import monotonic

from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Select, Static, TextArea

from .chrome_tui import safe_logs, safe_text
from .screens_tui import ScreenPanel


SESSION_ISSUES = {
    'disabled': ('DID 관리가 꺼져 있습니다', '간편 설정에서 DID 사용 여부를 확인하세요.'),
    'multiple': ('그래픽 세션이 여러 개입니다', '화면 계정의 그래픽 세션을 확인하세요.'),
    'pending': ('활성 GNOME 세션이 없습니다', '이번 부팅의 GNOME 로그인 로그를 확인하세요.'),
    'locked': ('GNOME 세션이 잠겨 있습니다', '현장에서 화면 잠금 상태를 확인하세요.'),
    'lock_unknown': ('GNOME 잠금 상태를 확인할 수 없습니다', '화면 계정의 세션과 잠금 상태를 확인하세요.'),
    'unknown': ('GNOME 세션을 확인할 수 없습니다', '이번 부팅의 GNOME 로그인 로그를 확인하세요.'),
}


def diagnosis(report, target):
    session = report.get('session', 'unknown')
    service = target.get('service') or {}
    running = service.get('ActiveState')
    placement = (target.get('placement') or {}).get('state')
    health = target.get('health') or {}
    settings = target.get('settings') or {}
    revision = ((target.get('runtime') or {}).get('committed') or {}).get('revision')
    stages = ['GNOME 세션', 'Chrome 실행', '창 배치', '사이트 로드']
    confirmed = 0
    logs = 'logs'
    next_button = 'logs'
    if session != 'ok':
        reason, action = SESSION_ISSUES.get(session, SESSION_ISSUES['unknown'])
        logs = 'login-logs'
        if session in {'disabled', 'locked', 'lock_unknown', 'multiple'}:
            next_button = None
    else:
        confirmed = 1
        if not target.get('applied_url', target.get('url')):
            reason, action = '적용된 DID 주소가 없습니다', '화면 관리에서 주소를 저장하고 적용하세요.'
            next_button = 'manage'
        elif (target.get('runtime') or {}).get('stopped') and running != 'active':
            reason, action = '운영자가 이 DID를 중지했습니다', '표시를 다시 시작하려면 시작을 선택하세요.'
            next_button = 'control'
        elif running == 'failed':
            reason, action = 'Chrome 서비스 시작 실패', '이번 부팅의 Chrome 로그를 확인하세요.'
        elif running == 'inactive':
            reason = 'Chrome이 중지되어 있습니다'
            action = '이번 부팅의 Chrome 로그를 확인하세요.' if target.get('autostart') else '자동 시작이 꺼져 있습니다. 필요하면 시작을 선택하세요.'
            next_button = 'logs' if target.get('autostart') else 'control'
        elif running == 'activating':
            reason, action = 'Chrome이 시작 중입니다', '잠시 후 다시 점검하세요.'
            next_button = 'refresh'
        elif running != 'active' or target.get('state') in {'unknown', 'error', 'blocked'}:
            reason, action = '관리 Chrome 실행을 확인할 수 없습니다', '이번 부팅의 Chrome 로그를 확인하세요.'
        else:
            confirmed = 2
            if placement not in {'ok', 'not_applicable'}:
                reason, action = 'DID 창 배치 확인 필요', '화면 관리에서 이 DID의 출력과 창 배치를 확인하세요.'
                next_button = 'manage'
            else:
                confirmed = 3
                if placement == 'not_applicable':
                    stages[2] = '창 배치 (기존 기본 화면 · 별도 확인)'
                if (health.get('state') != 'healthy' or not health.get('target_matches_config')
                        or settings.get('state') in {'queued', 'running'}
                        or (revision and health.get('config_revision') != revision)):
                    reason, action = 'DID 사이트 로드 확인 필요', '이번 부팅의 Chrome 로그와 적용 주소를 확인하세요.'
                else:
                    confirmed = 4
                    reason, action = '서버 점검 통과', '실제 DID 화면과 패널 입력을 현장에서 확인하세요.'
                    next_button = None
    lines = []
    for index, name in enumerate(stages):
        status = ('별도 확인' if index == 2 and placement == 'not_applicable' else '확인') if index < confirmed else '점검 필요' if index == confirmed else '확인 대기'
        lines.append(f'{name} · {status}')
    tone = '#f1d5a3'
    if confirmed == 4:
        tone = '#b5e9d9'
    elif confirmed == 1 and running == 'failed':
        tone = '#f3b8c5'
    elif confirmed == 1 and running == 'activating':
        tone = '#cbc2ef'
    return reason, action, '\n'.join(lines), logs, next_button, tone


class QuickPanel(Vertical):
    DEFAULT_CSS = """
    QuickPanel { height: 1fr; padding: 0 1; }
    #quick-target { height: 3; }
    #quick-reason { height: auto; padding: 0 1; color: #b5e9d9; text-style: bold; background: #2a2634; }
    #quick-next { height: auto; padding: 0 1; color: #e9f0f7; }
    #quick-note { height: auto; max-height: 2; color: #ffe0af; }
    #quick-detail { height: 1fr; min-height: 3; border: round #50475f; }
    #quick-actions { height: 3; }
    #quick-actions Button { width: 1fr; min-width: 8; padding: 0; }
    Screen.compact #quick-actions { height: 2; }
    Screen.compact #quick-actions Button { height: 2; border: none; }
    """

    def __init__(self, owner, **kwargs):
        super().__init__(**kwargs)
        self.owner = owner
        self.report = {}
        self.selected = None
        self.busy = False
        self.confirm = None
        self.log_text = None
        self.log_action = 'logs'
        self.launch_pending = False

    def compose(self):
        yield Static('DID 빠른 점검 · 이번 부팅', id='quick-reason', markup=False)
        yield Static('', id='quick-next', markup=False)
        yield Static('', id='quick-note', markup=False)
        yield Select([], prompt='DID 선택', id='quick-target')
        with Horizontal(id='quick-actions'):
            yield Button('이번 부팅 로그', id='quick-logs', disabled=True)
            yield Button('다시 점검', id='quick-refresh')
            yield Button('화면 관리', id='quick-manage', disabled=True)
            yield Button('시작', id='quick-control', disabled=True)
        yield TextArea('', read_only=True, show_cursor=False, id='quick-detail')

    def activate(self):
        if not self.busy:
            self.run_worker(self.refresh_status(), group='quick', exit_on_error=False)

    def target(self):
        return next((target for target in self.report.get('targets', []) if target['id'] == self.selected), None)

    def control(self, target):
        state = (target.get('service') or {}).get('ActiveState')
        if (self.report.get('session') != 'ok' or not target.get('applied_url', target.get('url'))
                or (target.get('settings') or {}).get('state') in {'queued', 'running'}):
            return None
        return {'inactive': 'start', 'failed': 'start', 'active': 'restart'}.get(state)

    async def inspect(self):
        value = await self.owner.service.call('screens-inspect', {'quick': True})
        self.report = value
        ids = [item['id'] for item in value['targets']]
        if self.selected not in ids:
            self.selected = ids[0] if len(ids) == 1 else None
        picker = self.query_one(Select)
        with picker.prevent(Select.Changed):
            picker.set_options((key, key) for key in ids)
            if self.selected is None:
                picker.clear()
            else:
                picker.value = self.selected
        picker.display = len(ids) > 1

    async def refresh_status(self):
        self.log_text = self.confirm = None
        self.launch_pending = False
        await self.run_task(self.inspect, '이번 부팅 상태를 확인하는 중입니다.')

    async def run_task(self, task, message, *, control=False):
        if self.busy:
            return
        self.busy = True
        if control:
            self.owner._chrome_mutating = True
            self.owner.update_button()
        self.note(message)
        self.render_status()
        try:
            await task()
            if not control:
                self.note('')
        except Exception as exc:
            if task == self.inspect or control:
                self.report = {}
                self.launch_pending = False
            self.note('확인 실패 · '+safe_text(str(exc)))
        finally:
            self.busy = False
            if control:
                self.owner._chrome_mutating = False
                self.owner.update_button()
            self.render_status()

    def note(self, text):
        self.query_one('#quick-note', Static).update(text)

    def render_status(self):
        target = self.target()
        if target:
            reason, action, stages, self.log_action, next_button, tone = diagnosis(self.report, target)
        else:
            reason = 'DID를 선택하세요' if self.report.get('targets') else 'DID 상태 조회 대기'
            action, stages = '', ''
            next_button = None if self.report.get('targets') else 'refresh'
            tone = '#cbc2ef' if self.busy else '#f1d5a3'
        if self.launch_pending and target:
            reason = 'Chrome 요청 후 실행 결과 확인 대기'
            action = '새 실행의 창 배치와 사이트 로드가 확인될 때까지 기다리세요.'
            tone = '#cbc2ef'
        heading = f'이번 부팅 · {self.selected}   {reason}' if target else reason
        self.query_one('#quick-reason', Static).update(Text(heading, style=tone))
        self.query_one('#quick-next', Static).update('다음 조치   '+action if action else '')
        detail = self.query_one('#quick-detail', TextArea)
        detail.border_title = '이번 부팅 로그' if self.log_text is not None else '점검 단계'
        detail.load_text(self.log_text if self.log_text is not None else stages)
        action = self.control(target) if target else None
        button = self.query_one('#quick-control', Button)
        label = '재시작' if action == 'restart' else '시작'
        button.label = label+' 확인' if self.confirm else label
        button.disabled = not action or self.busy or self.launch_pending or self.owner._chrome_mutating or bool(self.owner._active_job)
        self.query_one(Select).disabled = self.busy
        self.query_one('#quick-refresh', Button).disabled = self.busy
        self.query_one('#quick-manage', Button).disabled = not target or self.busy
        logs = self.query_one('#quick-logs', Button)
        logs.label = ('점검 결과' if self.log_text is not None else
                      'GNOME 로그인 로그' if self.log_action == 'login-logs' else '이번 부팅 로그')
        logs.disabled = not target or self.busy
        if self.confirm:
            next_button = 'control'
        elif self.launch_pending:
            next_button = 'refresh'
        for candidate in self.query('#quick-actions Button').results(Button):
            candidate.variant = 'primary' if candidate.id == f'quick-{next_button}' and not candidate.disabled else 'default'

    @on(Select.Changed, '#quick-target')
    def choose(self, event):
        self.selected = event.value if isinstance(event.value, str) else None
        self.confirm = self.log_text = None
        self.launch_pending = False
        self.note('')
        self.render_status()

    @on(Button.Pressed, '#quick-refresh')
    def refresh_pressed(self):
        self.activate()

    @on(Button.Pressed, '#quick-manage')
    def manage_pressed(self):
        if self.target():
            self.owner.query_one(ScreenPanel).selected = self.selected
            self.owner.action_chrome()

    @on(Button.Pressed, '#quick-logs')
    def logs_pressed(self):
        self.confirm = None
        if self.log_text is not None:
            self.log_text = None
            self.render_status()
            return
        self.run_worker(self.run_task(self.load_logs, '이번 부팅 로그를 읽는 중입니다.'), group='quick', exit_on_error=False)

    async def load_logs(self):
        if self.log_action == 'login-logs':
            value = await self.owner.service.call('screens-login-logs', {'target': self.selected})
        else:
            value = await self.owner.service.call('screens-action', {'target': self.selected, 'action': 'logs'})
        self.log_text = safe_logs(value.get('log', '')) or safe_text(value.get('reason') or '이번 부팅의 로그가 없습니다.')

    @on(Button.Pressed, '#quick-control')
    def control_pressed(self):
        target = self.target()
        action = self.control(target) if target else None
        if not action or self.busy or self.launch_pending or self.owner._chrome_mutating or self.owner._active_job:
            return
        if self.confirm != (self.selected, action):
            self.confirm = (self.selected, action)
            self.note(f'{self.selected} · 같은 버튼을 한 번 더 눌러 Chrome 작업을 확인하세요.')
            self.render_status()
            return
        self.confirm = self.log_text = None
        self.run_worker(self.run_task(
            lambda: self.perform(target, action), 'Chrome 작업을 요청하는 중입니다.', control=True),
            group='quick', exit_on_error=False)

    async def perform(self, target, action):
        target_id = target['id']
        old_pid = (target.get('service') or {}).get('MainPID')
        value = await self.owner.service.call('screens-action', {'target': target_id, 'action': action})
        submitted = bool(value.get('submitted'))
        self.launch_pending = submitted
        deadline = monotonic() + 30
        self.note(ScreenPanel.result_message('Chrome', value, state='접수 · 재점검 중' if submitted else None))
        while True:
            await self.inspect()
            current = self.target()
            if not submitted or not current or current['id'] != target_id:
                self.launch_pending = False
                return
            service = current.get('service') or {}
            if service.get('ActiveState') == 'failed':
                self.launch_pending = False
                self.note('Chrome 작업 후 서비스 실패가 확인됐습니다.')
                return
            if (service.get('ActiveState') == 'active' and service.get('MainPID')
                    and service['MainPID'] != old_pid and diagnosis(self.report, current)[0] == '서버 점검 통과'):
                self.launch_pending = False
                self.note('Chrome 작업 후 서버 점검 완료')
                return
            if monotonic() >= deadline:
                break
            self.render_status()
            await asyncio.sleep(2)
        self.note('Chrome 요청 후 확인 시간이 지났습니다. 다시 점검하거나 이번 부팅 로그를 보세요.')
