"""Explicit self-update controls; closing this view never stops the system job."""
from __future__ import annotations

import asyncio

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, Switch, TextArea

from . import __version__
from .model import redact
from .update import check as check_release, version_key


ACTIVE = {'queued', 'running'}
STATES = {'idle', 'queued', 'running', 'completed', 'failed', 'interrupted', 'unknown'}
LABELS = {'idle': '진행 중인 업데이트 없음', 'queued': '업데이트 시작 대기',
          'running': '업데이트 진행 중', 'completed': '업데이트 설치 완료',
          'failed': '업데이트 실패', 'interrupted': '업데이트 중단됨', 'unknown': '업데이트 상태 확인 불가'}
PHASES = {'queued': '작업 접수', 'checking': '업데이트 조건 확인', 'downloading': '배포 파일 다운로드', 'signature': '배포 서명·발행자 검증',
          'checksum': '다운로드 해시 검사', 'manifest': '배포 파일 검증', 'installing': '새 버전 설치',
          'rechecking': '설치 버전·실행 재검사', 'completed': '완료', 'failed': '실패'}


def status_text(value):
    """Render only the status contract, never raw RPC/log dictionaries."""
    value = value if isinstance(value, dict) else {}
    state = value.get('state', 'unknown')
    lines = [LABELS.get(state, LABELS['unknown'])]
    for label, key in (('단계', 'phase'), ('대상 버전', 'target_version'),
                       ('원인', 'reason'), ('다음', 'next_action')):
        item = value.get(key)
        if isinstance(item, str) and item:
            if key == 'phase':
                item = PHASES.get(item, '확인 불가')
            lines.append(f'{label}: {redact(item)}')
    installed, runtime = value.get('installed_version'), value.get('runtime_version', __version__)
    if installed and runtime and installed != runtime:
        lines.append('새 설치 버전을 사용하려면 ala를 종료하고 다시 실행하세요.')
    return '\n'.join(lines)


def newer_than_installed(release, status):
    if not release.get('available') or not isinstance(release.get('latest'), str):
        return False
    installed = status.get('installed_version')
    if not installed:
        return True
    try:
        return version_key(release['latest']) > version_key(installed)
    except (ValueError, TypeError):
        return False


class UpdateScreen(ModalScreen):
    BINDINGS = [('escape', 'close', '닫기'), ('q', 'close', '닫기')]
    CSS = """
    UpdateScreen { align: center middle; background: #000000 65%; }
    #update-dialog { width: 92%; max-width: 88; height: auto; max-height: 95%; padding: 1 2; background: #211f29; border: round #b5e9d9; }
    #update-title { height: 1; text-style: bold; color: #b5e9d9; }
    #update-release { height: 1; padding: 0 1; background: #2a2634; color: #b5e9d9; text-style: bold; text-wrap: nowrap; text-overflow: ellipsis; }
    #update-next { height: 1; padding: 0 1; text-wrap: nowrap; text-overflow: ellipsis; }
    #update-versions { height: 2; margin-top: 1; }
    #update-channel { height: 3; }
    #update-channel-label { height: 3; width: 1fr; content-align: left middle; }
    #update-prerelease { width: auto; }
    #update-detail { height: 6; min-height: 4; padding: 0 1; background: #1b1922; }
    #update-controls { height: 3; margin-top: 1; }
    UpdateScreen Button { width: 1fr; min-width: 10; padding: 0 1; }
    #update-close { max-width: 12; }
    #update-help { height: 1; color: #aabdd0; }
    """

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self._view_closed = False
        self._checking = False
        self._submitting = False
        self._generation = 0
        self._release = None
        self._message = ''

    def compose(self) -> ComposeResult:
        with Vertical(id='update-dialog'):
            yield Static('ala 업데이트', id='update-title')
            yield Static('릴리스를 확인하고 있습니다.', id='update-release', markup=False)
            yield Static('', id='update-next', markup=False)
            with Horizontal(id='update-channel'):
                yield Static('시험판 포함 · 필요한 경우에만 켜세요', id='update-channel-label')
                yield Switch(False, id='update-prerelease')
            with Horizontal(id='update-controls'):
                yield Button('다시 확인', id='update-check')
                yield Button('업데이트 시작', id='update-start', disabled=True)
                yield Button('자동 운영', id='update-automatic')
                yield Button('닫기', id='update-close')
            yield Static('', id='update-versions', markup=False)
            yield TextArea('', id='update-detail', read_only=True, show_cursor=False, highlight_cursor_line=False)
            yield Static('Esc 닫기 · 창을 닫아도 시작한 업데이트는 계속됩니다.', id='update-help')

    def alive(self):
        return self.is_mounted and not self._view_closed and self.owner.is_running

    def on_mount(self):
        self.render_state()
        self.set_interval(1.5, self.refresh_status)
        self.run_worker(self.refresh_status(), exit_on_error=False)
        self.run_worker(self.refresh_release(), exit_on_error=False)

    def on_unmount(self):
        self._view_closed = True

    def action_close(self):
        self._view_closed = True
        self.dismiss()

    @on(Button.Pressed, '#update-close')
    def close_pressed(self):
        self.action_close()

    @on(Button.Pressed, '#update-automatic')
    def automatic_pressed(self):
        from .automatic_tui import AutomaticScreen
        self.owner.push_screen(AutomaticScreen(self.owner))

    @on(Button.Pressed, '#update-check')
    def check_pressed(self):
        self._message = ''
        self.run_worker(self.refresh_status(latest=True), exit_on_error=False)
        self.run_worker(self.refresh_release(), exit_on_error=False)

    @on(Switch.Changed, '#update-prerelease')
    def channel_changed(self):
        self.run_worker(self.refresh_release(), exit_on_error=False)

    async def refresh_release(self):
        if not self.alive():
            return
        self._generation += 1
        generation = self._generation
        prerelease = self.query_one('#update-prerelease', Switch).value
        self._checking = True
        self.render_state()
        try:
            value = await asyncio.to_thread(check_release, prerelease)
        except Exception:
            value = {'exit_code': 1, 'error': '릴리스 조회 실패 · 네트워크 연결을 확인하세요.'}
        if not self.alive() or generation != self._generation:
            return
        self._checking = False
        self._release = value if isinstance(value, dict) else {'exit_code': 1}
        self.render_state()

    async def refresh_status(self, latest=False):
        await self.owner.poll_update(latest=latest)
        if self.alive():
            self.render_state()

    def priority(self, status, release, available):
        state = status.get('state', 'unknown')
        if self._submitting:
            return '업데이트 시작 요청 중', '작업 접수 결과를 기다리세요.', None, '#cbc2ef'
        if state in ACTIVE:
            phase = PHASES.get(status.get('phase'))
            return ' · '.join(filter(None, (LABELS[state], phase))), '작업 상태를 확인하세요.', None, '#cbc2ef'
        if self.owner._active_job:
            return '환경 설정 작업 진행 중', '설정 작업이 끝난 뒤 업데이트를 시작하세요.', None, '#cbc2ef'
        if self._message:
            return self._message, '다시 확인으로 현재 상태를 조회하세요.', 'check', '#f1d5a3'
        if state in {'failed', 'interrupted', 'unknown'}:
            next_action = status.get('next_action') or '다시 확인으로 현재 상태를 조회하세요.'
            return LABELS[state], str(redact(next_action)), 'check', '#f3b8c5'
        if self._checking:
            return '릴리스 확인 중…', '조회 결과를 기다리세요.', None, '#cbc2ef'
        installed = status.get('installed_version')
        runtime = status.get('runtime_version') or __version__
        if installed and installed != runtime:
            return '설치 버전과 실행 버전이 다릅니다', 'ala를 종료하고 다시 실행하세요.', None, '#f1d5a3'
        if release.get('exit_code', 1) or release.get('error'):
            return '릴리스 조회 실패', '연결 상태를 확인하고 다시 확인을 선택하세요.', 'check', '#f3b8c5'
        if available:
            return f"새 버전 · {release.get('latest', '확인 불가')}", '업데이트 시작을 선택하면 설치를 요청합니다.', 'start', '#f1d5a3'
        return '이 채널에 새로 설치할 버전이 없습니다', '자동 운영 정책이나 설치·실행 버전을 확인하세요.', None, '#b5e9d9'

    def render_state(self):
        if not self.alive():
            return
        status = self.owner._update_status or {'state': 'unknown'}
        installed = status.get('installed_version') or '확인 불가'
        runtime = status.get('runtime_version') or __version__
        self.query_one('#update-versions', Static).update(Text(f'설치 버전 · {installed}\n현재 실행 버전 · {runtime}'))
        release = self._release or {}
        available = newer_than_installed(release, status)
        headline, next_action, primary, tone = self.priority(status, release, available)
        self.query_one('#update-release', Static).update(Text(headline, style=tone))
        self.query_one('#update-next', Static).update(Text('다음 · '+next_action))
        detail = self.query_one('#update-detail', TextArea)
        text = status_text(status) + ('\n' + self._message if self._message else '')
        if self.owner._active_job:
            text += '\n환경 설정 작업이 진행 중입니다. 완료 후 업데이트를 시작하세요.'
        if text != detail.text:
            detail.load_text(text)
            detail.refresh()
        active = status.get('state') in ACTIVE
        start = self.query_one('#update-start', Button)
        check = self.query_one('#update-check', Button)
        start.disabled = bool(
            self._checking or self._submitting or active or self.owner._active_job
            or status.get('state') == 'unknown' or not available
            or release.get('exit_code', 1) or release.get('error'))
        self.query_one('#update-prerelease', Switch).disabled = self._submitting or active
        check.disabled = self._checking or self._submitting
        start.variant = 'primary' if primary == 'start' and not start.disabled else 'default'
        check.variant = 'primary' if primary == 'check' and not check.disabled else 'default'

    @on(Button.Pressed, '#update-start')
    def start_pressed(self):
        # App-owned work survives dismissing this modal; the system worker also
        # remains independent of the whole TUI and SSH connection.
        if not self.query_one('#update-start', Button).disabled:
            self.owner.run_worker(self.start_update(), group='update-submit', exit_on_error=False)

    async def start_update(self):
        if not self.alive() or self._submitting or self.query_one('#update-start', Button).disabled:
            return
        prerelease = self.query_one('#update-prerelease', Switch).value
        self._submitting = True
        self._message = '업데이트 시작을 요청하고 있습니다.'
        self.render_state()
        try:
            value = await self.owner.service.call('update-submit', {'prerelease': prerelease})
            if not isinstance(value, dict):
                raise ValueError('invalid response')
            if value.get('run_id') and value.get('state') in STATES - {'idle'}:
                self.owner.accept_update_status(value)
                await self.owner.poll_update()
                self._message = ''
            elif value.get('exit_code', 0) or value.get('error'):
                self._message = '업데이트 시작 실패 · 진행 중인 설정 작업과 연결 상태를 확인하세요.'
            elif value.get('available') is False:
                self._release = value
                self._message = '새로 설치할 버전이 없습니다.'
            else:
                self._message = '업데이트 시작 여부를 확인하지 못했습니다. 상태를 다시 확인하세요.'
        except Exception:
            self._message = '업데이트 시작을 확인하지 못했습니다. 상태를 다시 확인하세요.'
            self.owner.accept_update_status({'state': 'unknown', 'reason': self._message})
        finally:
            self._submitting = False
            if self.alive():
                self.render_state()
