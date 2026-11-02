"""Task-oriented entry screens using the existing configuration/job contracts."""
from __future__ import annotations

import getpass
from rich.text import Text
from textual.containers import Vertical, Horizontal, VerticalScroll
from textual.widgets import Static, Button, Input, Label, Checkbox, ContentSwitcher, TextArea, OptionList

from .config import Config

HOME_MENU = (("wizard", "처음 설정하기"), ("check", "서버 상태 확인"),
             ("chrome", "화면 관리"), ("advanced", "고급 설정"))


def item_name(identifier):
    groups = {"base": "기본 패키지·Chrome", "bash": "터미널 환경", "tools": "선택 도구",
              "ssh": "SSH·방화벽", "docker": "Docker", "hardware": "장치 드라이버",
              "nvidia": "NVIDIA GPU", "gpu": "GPU", "display": "자동 로그인·화면",
              "chrome": "Chrome 화면", "updates": "자동 업데이트 차단", "update": "자동 업데이트 차단",
              "network": "네트워크", "platform": "운영체제", "config": "사용자 설정",
              "time": "시간 동기화", "shell": "터미널 환경", "user": "화면 계정", "host": "서버", "prepare": "초기 OS 준비"}
    return groups.get(str(identifier).split('.')[0], str(identifier))


def review_text(report):
    rows = report.get("results", [])
    lines = []
    changed = {}
    for r in rows:
        if r.get('state') == 'change_required':
            changed.setdefault(item_name(r.get('id')), []).append(r)
    if changed:
        lines.append('설치·설정할 항목')
        for name, items in changed.items():
            packages = [str(r['id']).split('.package.', 1)[1] for r in items if '.package.' in str(r.get('id'))]
            lines.append(f"• {name}: {len(items)}개 항목")
            if packages:
                lines.append('  패키지: ' + ', '.join(packages))
        lines.append('')
    waiting = []
    for r in rows:
        if r.get('state') in {'pending', 'blocked', 'unknown', 'error'}:
            action = r.get('next_action') or r.get('detail') or '현재 상태 확인'
            line = f"• {item_name(r.get('id'))}: {action}"
            if line not in waiting:
                waiting.append(line)
    if waiting:
        lines.extend(['추가 확인 / 적용 대기', *waiting, ''])
    unchanged = sum(r.get('state') == 'ok' for r in rows)
    irrelevant = sum(r.get('state') == 'not_applicable' for r in rows)
    lines.append(f"이미 정상: {unchanged}개 유지 · 해당 없음: {irrelevant}개")
    if not rows:
        lines.append('검사 결과가 없습니다. 다시 확인하세요.')
    lines.append('자동 재부팅하지 않습니다. 적용 후 재부팅이 필요할 수 있습니다.')
    return '\n'.join(lines)


class GuidedSetup:
    def init_guided(self):
        self._wizard_stage = 'account'
        self._wizard_base = None
        self._wizard_busy = False
        self._wizard_started = False

    def guided_panels(self):
        with Vertical(id='home', classes='panel'):
            yield Static('서버 환경을 준비하세요', classes='home-title', markup=False)
            yield Static('', id='update-notice', markup=False)
            yield Static('현재 상태를 확인하고 있습니다.', id='home-status', markup=False)
            yield Static('왼쪽에서 할 일을 선택하고 Enter를 누르세요.\n\n처음에는 첫 메뉴만 따라가면 됩니다.\n① 계정 확인  ② 화면 선택  ③ 변경 확인\n\n이미 정상인 설치·설정은 유지합니다.\n초기 OS 업데이트와 상세 옵션은 고급 설정에 있습니다.', markup=False)
        with Vertical(id='wizard', classes='panel'):
            yield Static('처음 설정하기', id='wizard-title', classes='home-title', markup=False)
            with ContentSwitcher(initial='wizard-account', id='wizard-pages'):
                with VerticalScroll(id='wizard-account'):
                    yield Label('화면에 로그인할 기존 일반 계정')
                    yield Input(id='wizard-user', placeholder='user')
                    yield Static('현재 접속 계정을 기본으로 사용합니다.\n재부팅 후 이 계정으로 바탕화면에 자동 로그인합니다.\n계정 암호와 SSH 인증은 바꾸지 않습니다.', markup=False)
                with VerticalScroll(id='wizard-display'):
                    yield Checkbox('DID 웹페이지를 전체화면으로 표시', id='wizard-web')
                    yield Label('체크 해제: 바탕화면만 표시')
                    yield Label('로그인 없이 열리는 DID 주소')
                    yield Input(id='wizard-url', placeholder='https://example.com')
                    yield Static('주소가 없으면 바탕화면부터 준비하세요.\n잠금·유휴 화면 꺼짐을 방지합니다. 상세 옵션은 고급 설정에 있습니다.', markup=False)
                with Vertical(id='wizard-review'):
                    yield Static('선택한 구성을 저장했습니다. 아직 설치·설정은 시작하지 않았습니다.', id='wizard-choice', markup=False)
                    yield TextArea('', id='wizard-summary', read_only=True)
                with Vertical(id='wizard-running'):
                    yield Static('설정 작업을 시작합니다.', id='wizard-progress', markup=False)
                    yield TextArea('', id='wizard-result', read_only=True)
            with Horizontal(classes='actions'):
                yield Button('홈', id='wizard-back')
                yield Button('다음', id='wizard-next', variant='primary')
        with VerticalScroll(id='advanced', classes='panel'):
            yield Static('필요할 때만 상세 설정을 사용하세요.', classes='home-title', markup=False)
            yield Button('세부 구성 변경', id='advanced-config')
            yield Button('초기 OS 업데이트·상세 적용', id='advanced-setup')
            yield Button('실행 기록·로그', id='advanced-runs')
            yield Button('상세 상태', id='advanced-dashboard')

    def show_section(self, section):
        self.section = section
        self.query_one('#content', ContentSwitcher).current = section
        self.query_one('#toolbar').display = section not in {'home', 'wizard'}
        self.refresh_section()

    async def load_home(self):
        value = await self.api('status')
        if value is None:
            self.query_one('#home-status', Static).update('확인하려면 왼쪽의 처음 설정하기를 선택하세요. 필요한 권한은 순서대로 안내합니다.')
            self.notice('첫 메뉴에서 시작하세요. 설정 확인에 필요한 권한을 요청합니다.')
            return
        self.SUB_TITLE = str(value.get('hostname', '서버 환경 설정'))
        self.sub_title = self.SUB_TITLE
        configured = value.get('config_exists', bool(value.get('config', {}).get('user')))
        label = '현재 설정 점검·보완' if configured else '처음 설정하기'
        self.query_one('#navigation', OptionList).replace_option_prompt('wizard', label)
        rows = value.get('results', [])
        issues = sum(r.get('state') not in {'ok', 'not_applicable'} for r in rows)
        self.query_one('#home-status', Static).update(Text(
            f"서버: {value.get('hostname', '-')}\n" +
            (f'확인할 항목: {issues}개 · {label}에서 안내합니다.' if configured else '초기 설정이 필요합니다. 처음 설정하기를 선택하세요.')))
        active = next((r for r in value.get('runs', []) if r.get('state') in {'queued', 'running'}), None)
        if active:
            self._active_job = self._selected_job = active['run_id']
            self.render_job(active)
            self.query_one('#home-status', Static).update('설정 작업이 실행 중입니다. 첫 메뉴에서 진행 상황을 확인하세요.')
        self.notice('↑/↓ 선택 · Enter 실행 · Tab 이동 · Esc 홈 · q 종료')

    def wizard_page(self, stage):
        self._wizard_stage = stage
        titles = {'account': '1 / 3 · 사용할 계정 확인', 'display': '2 / 3 · 화면 용도 선택',
                  'review': '3 / 3 · 필요한 변경 확인', 'running': '설정 진행 상황'}
        self.query_one('#wizard-title', Static).update(titles[stage])
        self.query_one('#wizard-pages', ContentSwitcher).current = 'wizard-' + stage
        next_button = self.query_one('#wizard-next', Button)
        next_button.label = {'account': '다음', 'display': '변경 확인', 'review': '설정 시작', 'running': '홈으로'}[stage]
        next_button.disabled = stage == 'review' and self._plan_config is None
        self.query_one('#wizard-back', Button).label = '홈' if stage in {'account', 'running'} else '이전'
        focus = '#wizard-user' if stage == 'account' else '#wizard-web' if stage == 'display' else '#wizard-next'
        self.query_one(focus).focus()

    async def begin_wizard(self):
        if self._active_job:
            self.wizard_page('running')
            return
        self._wizard_base = None
        self.query_one('#wizard-next', Button).disabled = True
        self.notice('설정 확인을 준비합니다. 필요하면 터미널에서 sudo 암호를 입력하세요.')
        try:
            if self.is_headless:
                self.service.authenticate()
            else:
                with self.suspend():
                    self.service.authenticate()
        except Exception as exc:
            self.notice(f'권한 확인 실패: {exc} · 첫 메뉴를 다시 선택해 재시도하세요.')
            return
        await self.load_config()
        if self._stored_config is None:
            return
        self._wizard_base = dict(self._stored_config)
        user = self._wizard_base.get('user') or getpass.getuser()
        self.query_one('#wizard-user', Input).value = '' if user == 'root' else user
        self.query_one('#wizard-url', Input).value = self._wizard_base.get('url') or ''
        self.query_one('#wizard-web', Checkbox).value = bool(self._wizard_base.get('url') and self._wizard_base.get('autostart'))
        self.query_one('#wizard-url', Input).disabled = not self.query_one('#wizard-web', Checkbox).value
        self.wizard_page('account')
        self.notice('기존 일반 계정을 확인하고 다음을 누르세요.')

    async def wizard_advance(self):
        if self._wizard_busy:
            return
        if self._wizard_stage == 'running':
            self.show_section('home')
            return
        if self._wizard_base is None:
            return
        self._wizard_busy = True
        self.query_one('#wizard-next', Button).disabled = True
        try:
            if self._wizard_stage == 'account':
                user = self.query_one('#wizard-user', Input).value.strip()
                if not user:
                    raise ValueError('기존 일반 계정을 입력하세요.')
                Config.from_dict(dict(self._wizard_base, user=user))
                self.wizard_page('display')
            elif self._wizard_stage == 'display':
                web = self.query_one('#wizard-web', Checkbox).value
                url = self.query_one('#wizard-url', Input).value.strip()
                if web and not url:
                    raise ValueError('DID 주소를 입력하거나 바탕화면만 표시를 선택하세요.')
                data = dict(self._wizard_base, user=self.query_one('#wizard-user', Input).value.strip(),
                            display=True, autologin=True, autostart=web)
                if web:
                    data['url'] = url
                config = Config.from_dict(data)
                current = await self.api('config-show')
                if current is None or current['config'] != self._wizard_base:
                    self.notice('다른 곳에서 구성이 바뀌었습니다. 홈으로 돌아가 다시 시작하세요.')
                    return
                value = await self.api('config-save', config.to_dict())
                if value is None:
                    return
                await self.load_config()
                self._wizard_base = dict(self._stored_config)
                await self.make_plan()
                self.query_one('#wizard-choice', Static).update(Text(
                    f"계정: {config.user} · {'DID 전체화면' if web else '바탕화면만 표시'}\n선택을 저장했습니다. 설정 시작 전에는 설치하지 않습니다."))
                report = self._last_plan or {'results': self._reports.get('plan', [])}
                self.query_one('#wizard-summary', TextArea).load_text(review_text(report))
                self.wizard_page('review')
                if self._plan_config is not None:
                    self.notice('변경 내용을 읽고 설정 시작을 누르세요. 이전으로 돌아가 수정할 수 있습니다.')
            elif self._wizard_stage == 'review':
                await self.submit_plan()
                if self._active_job:
                    self._wizard_started = True
                    self.wizard_page('running')
        except (ValueError, TypeError) as exc:
            self.notice(str(exc))
        finally:
            self._wizard_busy = False
            self.query_one('#wizard-next', Button).disabled = self._wizard_stage == 'review' and self._plan_config is None

    def wizard_back(self):
        if self._wizard_busy:
            return
        stage = self._wizard_stage
        if stage in {'account', 'running'}:
            self.show_section('home')
            self.query_one('#navigation', OptionList).focus()
        else:
            self.invalidate_plan()
            self.wizard_page('account' if stage == 'display' else 'display')

    def render_guided_job(self, job):
        state = job.get('state')
        if state in {'queued', 'running'}:
            text = '설정 작업이 진행 중입니다. SSH를 종료해도 작업은 계속됩니다.'
        elif state == 'completed':
            text = '설정을 마쳤습니다. 서버 상태 확인과 실제 모니터에서 화면을 확인하세요.'
        elif state == 'pending':
            text = '추가 적용이 필요합니다. 아래 다음 행동을 확인하세요. 자동 재부팅하지 않습니다.'
        else:
            text = '설정을 마치지 못했습니다. 아래 조치를 확인한 뒤 다시 실행하세요.'
        self.query_one('#wizard-progress', Static).update(Text(text))
        lines = []
        for stage in job.get('stages', []):
            state_label = {'started': '진행 중', 'completed': '완료', 'failed': '실패', 'pending': '대기'}.get(stage.get('state'), stage.get('state'))
            lines.append(f"{item_name(stage.get('id'))}: {state_label}")
        if job.get('reason'):
            lines.append(str(job['reason']))
        for r in (job.get('report') or {}).get('results', []):
            if r.get('state') not in {'ok', 'not_applicable'}:
                lines.append(f"{item_name(r.get('id'))}: {r.get('next_action') or r.get('detail') or '상태 확인 필요'}")
        lines.append('상세 로그: 고급 설정 → 실행 기록·로그')
        self.query_one('#wizard-result', TextArea).load_text('\n'.join(lines))
