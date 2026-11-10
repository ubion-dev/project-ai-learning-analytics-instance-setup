"""Line-oriented management. No screen clearing, raw input, timers or UI imports."""
from __future__ import annotations

from copy import deepcopy
import json
import re
import select
import sys
import time

from . import __version__
from .model import redact


class Cancelled(Exception):
    """Return to the calling menu without applying a change."""


def clean(value):
    # Escape terminal controls in remote data, including OSC/CSI introducers.
    return re.sub(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]', '?', str(redact(value)))


class Terminal:
    def __init__(self, service, *, read_line=None, stream=None, sleep=time.sleep):
        self.service = service
        self.read_line = read_line
        self.stream = stream or sys.stdout
        self.sleep = sleep

    def write(self, value=''):
        print(clean(value), file=self.stream, flush=True)

    def ask(self, prompt, *, default=None, deadline=None):
        suffix = f' [{default}]' if default is not None else ''
        prompt = clean(prompt + suffix + ': ')
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Cancelled('확인 시간이 지났습니다. 복원 상태를 조회하세요.')
            if self.read_line is None:
                print(prompt, end='', file=self.stream, flush=True)
                if not select.select([sys.stdin], [], [], remaining)[0]:
                    self.write('\n확인 시간이 지났습니다. 서버가 원래 배치로 복원을 요청합니다.')
                    raise Cancelled()
                line = sys.stdin.readline()
                if not line:
                    raise EOFError
                value = line.strip()
            else:
                value = self.read_line(prompt).strip()
        else:
            value = (self.read_line or input)(prompt).strip()
        if value.lower() in {'b', 'back', '취소'}:
            raise Cancelled()
        return default if value == '' and default is not None else value

    def yes(self, prompt, **kwargs):
        return self.ask(prompt + ' (yes 입력 시 실행, Enter 취소)', **kwargs).lower() == 'yes'

    def pick(self, prompt, items, label=str):
        if not items:
            raise ValueError('선택할 항목이 없습니다. 상태를 다시 조회하세요.')
        page = 0
        while True:
            start = page * 12
            for i, item in enumerate(items[start:start + 12], start + 1):
                self.write(f'  {i}. {label(item)}')
            self.write('  n 다음 쪽 · p 이전 쪽 · b 돌아가기' if len(items) > 12 else '  b 돌아가기')
            value = self.ask(prompt)
            if value.isdecimal() and 1 <= int(value) <= len(items):
                return items[int(value) - 1]
            if value == 'n' and start + 12 < len(items): page += 1
            elif value == 'p' and page: page -= 1
            else: self.write('목록 번호를 입력하세요.')

    def call(self, action, payload=None):
        since = time.perf_counter()
        result = self.service.request(action, payload)
        self.write(f'요청 {time.perf_counter() - since:.2f}초')
        return result

    def result(self, value):
        if value.get('state'):
            self.write('상태: ' + str(value['state']))
        for key in ('reason', 'next_action', 'error', 'query_warning'):
            if value.get(key):
                self.write(value[key])
        for key in ('run_id', 'operation_id'):
            if value.get(key):
                self.write(f'{key}: {value[key]}')
        if 'log' in value:
            self.write(value['log'])

    def detail(self, value):
        self.write(json.dumps(redact(value), ensure_ascii=False, indent=2))

    def guarded(self, fn):
        try:
            return fn()
        except Cancelled as exc:
            if str(exc): self.write(exc)
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            self.write('실패: ' + str(exc))

    def setup(self, *, prepare=False, cleanup=None):
        config = self.call('config-show')['config']
        if not config.get('user'):
            raise ValueError('먼저 기본 설정에서 기존 일반 사용자 계정을 지정하세요.')
        self.write(f"화면 계정: {config['user']} · 기존 설정과 사이트를 보존합니다.")
        if prepare:
            self.detail(self.call('prepare-plan'))
            self.write('OS 패키지 업데이트 작업입니다. 재부팅은 자동 실행하지 않습니다.')
        if not self.yes('OS 준비 시작' if prepare else '간편 설정 시작'):
            return
        payload = {'expected_config': config}
        if cleanup is not None: payload['podman_cleanup'] = cleanup
        job = self.call('prepare-submit' if prepare else 'submit', payload)
        self.write('SSH가 끊겨도 작업은 계속됩니다. Ctrl+C는 진행 표시만 종료합니다.')
        seen = set()
        while True:
            for stage in job.get('stages', []):
                marker = (stage.get('id'), stage.get('state'), stage.get('time'))
                if marker not in seen:
                    self.write(f"{stage.get('id')} · {stage.get('state')}")
                    seen.add(marker)
            if job.get('query_warning') or job.get('state') not in {'queued', 'running'}:
                self.result(job)
                if job.get('report'): self.status(job['report'])
                return
            self.sleep(2)
            # Only active jobs poll; unchanged progress never redraws a screen.
            job = self.service.request('runs-show', {'run_id': job['run_id']})

    def status(self, report=None):
        report = self.call('status') if report is None else report
        rows = report.get('results', []) + report.get('preparation', {}).get('results', [])
        if not rows:
            self.write('검사 결과가 없습니다. 정상으로 판단할 수 없습니다.')
        for row in rows:
            if row.get('state') not in {'ok', 'not_applicable'}:
                self.write(f"{row.get('id')} · {row.get('state')} · {clean(row.get('actual'))}")
                if row.get('next_action'): self.write('  → ' + row['next_action'])
        self.write(f"검사 {len(rows)}개 · 정상 {sum(r.get('state') == 'ok' for r in rows)}개")
        for job in report.get('runs', []):
            if job.get('state') in {'queued', 'running'}: self.result(job)

    def config(self):
        from .config import Config
        before = self.call('config-show')['config']
        edited = deepcopy(before)
        while True:
            self.write('\n기본 설정 · 편집 중에는 저장하지 않습니다.')
            self.write('화면 계정: ' + str(edited.get('user') or '미지정'))
            flags = [key for key in ('display', 'autologin', 'dev_tools', 'root_bash', 'ssh') if key in edited]
            for i, key in enumerate(flags, 1): self.write(f"{i}. {key}: {'켜짐' if edited[key] else '꺼짐'}")
            self.write('u 계정 변경 · z 시간대 · f SSH 방화벽 zone · s 저장 · b 취소')
            choice = self.ask('선택')
            if choice == 'u': edited['user'] = self.ask('기존 일반 사용자', default=edited.get('user') or '')
            elif choice == 'z': edited['timezone'] = self.ask('시간대', default=edited['timezone'])
            elif choice == 'f':
                zone = self.ask('SSH zone (- 기본)', default=edited.get('ssh_zone') or '-')
                edited['ssh_zone'] = None if zone == '-' else zone
            elif choice.isdecimal() and 1 <= int(choice) <= len(flags):
                key = flags[int(choice) - 1]; edited[key] = not edited[key]
            elif choice == 's':
                Config.from_dict(edited)
                self.write('변경 항목: ' + ', '.join(k for k in edited if edited[k] != before.get(k)))
                if edited == before or not self.yes('기본 설정 저장'): return
                self.result(self.call('config-save-reviewed', {'config': edited, 'expected_config': before}))
                self.write('저장했습니다. 실제 환경 변경은 간편 설정에서 실행하세요.')
                return

    def updates(self):
        self.write('1 새 버전 확인 · 2 업데이트 시작 · 3 작업 상태 · 4 자동 운영 정책 · b 돌아가기')
        choice = self.ask('선택')
        if choice == '1':
            from .update import check
            self.detail(check(prerelease=True))
        elif choice == '2':
            prerelease = self.yes('시험판도 포함')
            if self.yes('ala 업데이트 시작'):
                self.result(self.call('update-submit', {'prerelease': prerelease}))
                self.write('진행 확인: ala update --status · 완료 후 ala를 다시 실행하세요.')
        elif choice == '3': self.result(self.call('update-status'))
        elif choice == '4': self.detail(self.call('auto-update-status'))

    def advanced(self):
        self.write('1 기본 설정 · 2 실행 기록·로그 · 3 네트워크 진단 · 4 OS 준비 · 5 모니터 확장 갱신 · 6 로그인 진단 · 7 DID 전체 초기화·복구 · 8 자동 운영 · 9 미사용 Podman 정리 검토 · b 돌아가기')
        choice = self.ask('선택')
        if choice == '1': self.config()
        elif choice == '2':
            job = self.pick('실행 기록', self.call('runs-list').get('runs', []),
                            lambda r: f"{r['run_id']} · {r['state']}")
            self.detail(self.call('runs-show', {'run_id': job['run_id']}))
            if self.yes('선택 실행의 로그 조회'): self.result(self.call('logs', {'run_id': job['run_id']}))
        elif choice == '3': self.detail(self.call('doctor-network'))
        elif choice == '4': self.setup(prepare=True)
        elif choice == '5':
            self.write('기존 GNOME 확장 파일을 갱신합니다. 반영에는 GNOME 재로그인이 필요할 수 있습니다.')
            if self.yes('확장 갱신'): self.result(self.call('monitor-extension-refresh'))
        elif choice == '6': self.result(self.call('screens-login-logs', {'target': 'default'}))
        elif choice == '7':
            self.write('1 초기화 계획 조회 · 2 기록 조회 · 3 복구 요청 · b 돌아가기')
            action = self.ask('선택')
            if action == '1':
                preview = self.call('screens-reset-preview'); self.detail(preview)
                if self.yes('DID 전체 설정 초기화'):
                    self.result(self.call('screens-reset-submit', {'review': preview.get('review')}))
            elif action == '2': self.detail(self.call('screens-reset-status'))
            elif action == '3':
                status = self.call('screens-reset-status'); self.detail(status)
                if self.yes('기록된 초기화 작업 복구'): self.result(self.call('screens-reset-recover', {'operation_id': status.get('operation_id')}))
        elif choice == '8':
            self.detail(self.call('auto-update-status'))
            self.write('1 서명 검증·자동 운영 준비 · 2 수용 확인 후 활성화 · 3 자동 운영 중지 · b 돌아가기')
            action = self.ask('선택')
            names = {'1': 'auto-update-bootstrap', '2': 'auto-update-enable', '3': 'auto-update-disable'}
            if action == '2': self.write('실제 장비의 화면·야간 적용 수용을 완료한 경우에만 활성화하세요.')
            if action in names and self.yes(names[action]):
                self.result(self.call(names[action], {'accepted': True} if action == '2' else {}))
        elif choice == '9':
            plan = self.call('podman-plan')
            self.result(plan)
            if plan.get('state') == 'ready':
                self.write('제거 예정 패키지: ' + ', '.join(plan.get('packages', [])))
                self.write('조회 계정: ' + ', '.join(a['user'] for a in plan.get('accounts', [])))
                self.write('컨테이너·볼륨·이미지·pod·사용자 정의 네트워크가 없는 경우만 제거합니다.')
                if self.yes('표시한 Podman 패키지 제거 후 간편 설정'):
                    self.setup(cleanup=plan['token'])


def run_terminal(service, *, initial='home', read_line=None, stream=None, sleep=time.sleep):
    terminal = Terminal(service, read_line=read_line, stream=stream, sleep=sleep)
    terminal.write(f'ala {__version__} · 텍스트 메뉴 · b 돌아가기 · Ctrl+C 표시 종료')
    from .terminal_screens import screens
    from .terminal_monitors import monitors
    actions = {'1': terminal.setup, '2': terminal.status, '3': lambda: screens(terminal),
               '4': lambda: monitors(terminal), '5': terminal.updates, '6': terminal.advanced}
    try:
        if initial != 'home':
            terminal.guarded({'screens': lambda: screens(terminal), 'monitors': lambda: monitors(terminal), 'config': terminal.config}[initial])
            return 0
        while True:
            terminal.write('\n1 간편 설정 · 2 상태 확인 · 3 사이트·Chrome · 4 모니터 배치·해상도 · 5 업데이트 · 6 고급 · q 종료')
            try: choice = terminal.ask('선택')
            except Cancelled: continue
            if choice.lower() in {'q', 'quit', 'exit'}: return 0
            if choice in actions: terminal.guarded(actions[choice])
            else: terminal.write('메뉴 번호를 입력하세요.')
    except EOFError:
        terminal.write('입력이 종료됐습니다. 미저장 편집은 폐기합니다. 진행 중 작업은 서버에서 계속됩니다.')
        return 0
