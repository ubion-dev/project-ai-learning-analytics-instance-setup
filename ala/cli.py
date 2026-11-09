from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

from . import __version__
from .config import Config
from .service import Service, privileged

class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(64, f'{self.prog}: {message}\n')

def parser():
    p = Parser(prog='ala', description='AI 학습 분석실 DID 인스턴스 환경 관리')
    p.add_argument('--version', action='version', version=__version__)
    sub = p.add_subparsers(dest='command')
    sub.add_parser('tui', help='간편 설정·상태 확인·화면 관리')
    sub.add_parser('console', help='텍스트 콘솔 (선택)')
    config = sub.add_parser('config', help='기대 구성').add_subparsers(dest='action', required=True)
    config.add_parser('edit')
    config.add_parser('show').add_argument('--json', action='store_true')
    config.add_parser('import').add_argument('file', type=Path)
    for name, description in (('setup', '환경 설치·설정'), ('prepare', '최초 OS 업데이트·재부팅 확인')):
        setup = sub.add_parser(name, help=description)
        mode = setup.add_mutually_exclusive_group()
        mode.add_argument('--plan', action='store_true')
        mode.add_argument('--apply', action='store_true')
        setup.add_argument('--detach', action='store_true')
        setup.add_argument('--json', action='store_true')
    update = sub.add_parser('update', help='ala 새 버전 확인·갱신')
    update_mode = update.add_mutually_exclusive_group()
    update_mode.add_argument('--check', action='store_true', help='설치 없이 새 버전 확인')
    update_mode.add_argument('--status', action='store_true', help='마지막 갱신의 완료·실패 확인')
    update_mode.add_argument('--policy', action='store_true', help='자동 운영 준비·현재 정책 확인')
    update_mode.add_argument('--bootstrap', action='store_true', help='서명 검증기·현재 배포·자동 운영 등록 준비')
    update_mode.add_argument('--enable-auto', action='store_true', help='수용 확인 후 정식판 자동 운영 활성화')
    update_mode.add_argument('--disable-auto', action='store_true', help='실행 중 화면을 유지하며 자동 운영 중지')
    update_mode.add_argument('--apply-runtime', metavar='TARGET', help='지정 화면의 새 관리 코드를 명시적으로 적용')
    update.add_argument('--confirm-acceptance', action='store_true', help='실제 장비의 화면·야간 적용 수용 확인 (--enable-auto)')
    update.add_argument('--run', dest='run_id', help='조회할 갱신 실행 ID (--status)')
    update.add_argument('--detach', action='store_true', help='갱신 시작 후 완료를 기다리지 않고 반환')
    update.add_argument('--prerelease', action='store_true', help='시험판 포함')
    update.add_argument('--json', action='store_true')
    doctor = sub.add_parser('doctor', help='읽기 전용 진단').add_subparsers(dest='action', required=True)
    network = doctor.add_parser('network', help='Wi-Fi·네트워크 계층 진단')
    network.add_argument('--probe-url', help='명시한 HTTP/HTTPS 주소만 통신 확인')
    network.add_argument('--json', action='store_true')
    for name in ('status', 'check'):
        sub.add_parser(name).add_argument('--json', action='store_true')
    chrome = sub.add_parser('chrome')
    chrome.add_argument('action', nargs='?', choices=['start','stop','restart','reload','status','logs','check'])
    chrome.add_argument('--target', help='표시 대상 ID (생략하면 기존 기본 대상)')
    chrome.add_argument('--json', action='store_true')
    display = sub.add_parser('display', help='연결된 출력·관리 창·사이트 상태')
    display.add_argument('action', nargs='?', choices=['list', 'status', 'modes', 'plan', 'trial', 'apply',
                         'trial-status', 'confirm', 'revert', 'recover'], default='status')
    display.add_argument('file', nargs='?', type=Path, help='plan/trial의 배치 JSON 파일')
    display.add_argument('--operation-id', help='confirm/revert/recover의 배치 시험 작업 ID')
    display.add_argument('--json', action='store_true')
    runs = sub.add_parser('runs').add_subparsers(dest='action', required=True)
    runs.add_parser('list').add_argument('--json', action='store_true')
    show = runs.add_parser('show')
    show.add_argument('run_id'); show.add_argument('--json', action='store_true')
    logs = sub.add_parser('logs')
    logs.add_argument('--run', required=True, dest='run_id')
    logs.add_argument('--follow', action='store_true')
    return p

def emit(value, as_json=False):
    if not as_json and 'log' in value:
        print(value['log'], end='' if value['log'].endswith('\n') else '\n')
    elif as_json or 'results' not in value:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        if 'hostname' in value:
            print(f'호스트: {value["hostname"]} | 화면 사용자: {value.get("config",{}).get("user", "미지정")}')
            for run in value.get('runs', [])[:3]:
                print(f'실행 {run["run_id"]}: {run["state"]}')
        for result in value['results']:
            print(f'[{result["state"]}] {result["id"]}: 기대={result.get("expected")} / 현재={result.get("actual")}')
            if result.get('next_action'):
                print(f'  → {result["next_action"]}')
        print('결과: ' + ' / '.join(f'{key} {count}' for key,count in value.get('summary', {}).items() if count))


def update_result(result):
    labels = {'queued':'시작 대기', 'running':'진행 중', 'completed':'완료',
              'failed':'실패', 'interrupted':'중단됨', 'unknown':'확인 불가', 'idle':'실행 기록 없음'}
    print('ala 갱신: ' + labels.get(result.get('state'), '확인 불가'))
    if result.get('phase') and result.get('state') not in {'idle', 'completed'}:
        print('단계: ' + update_phase(result['phase']))
    if result.get('run_id'):
        print('실행 ID: ' + result['run_id'])
    print('설치된 버전: ' + str(result.get('installed_version') or '확인 불가'))
    if result.get('target_version'):
        print('대상 버전: ' + result['target_version'])
    if result.get('reason'):
        print(result['reason'])
    if result.get('next_action'):
        print(result['next_action'])


def update_phase(phase):
    return {'queued':'작업 접수', 'checking':'업데이트 조건 확인', 'signature':'배포 서명·발행자 검증',
            'downloading':'배포 파일 다운로드', 'checksum':'다운로드 해시 검사',
            'manifest':'배포 파일 검증', 'installing':'새 버전 설치',
            'rechecking':'설치 버전·실행 재검사', 'completed':'완료', 'failed':'실패'}.get(phase, str(phase))


def main(argv=None, *, service=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Not part of the public command grammar. Root-owned installed entry only.
    if argv and argv[0] in {'_privileged','_worker','_session','_chrome-run','_chrome-user','_update-worker', '_runtime-dispatch', '_auto-update', '_frontend-worker', '_frontend-control', '_auto-bootstrap', '_runtime-worker', '_screen-worker', '_display-onboarding-worker', '_swap-worker', '_swap-recover', '_reset-worker', '_reset-recover', '_layout-worker', '_layout-recover', '_layout-restore'}:
        try:
            if argv == ['_layout-restore']:
                from .jobs import trusted_release
                from .system import platform_error
                from .monitor_profiles import restore
                if os.geteuid() != 0 or platform_error():
                    raise PermissionError('Rocky Linux root 작업입니다.')
                release = trusted_release(Path(__file__).resolve().parents[1])
                print(json.dumps(restore(release=release), ensure_ascii=False))
                return 0
            if argv[0] in {'_layout-worker', '_layout-recover'} and len(argv) == 2:
                from .jobs import trusted_release
                from .system import platform_error
                from .monitor_change import run_worker, recover
                if os.geteuid() != 0 or platform_error():
                    raise PermissionError('Rocky Linux root 작업입니다.')
                trusted_release(Path(__file__).resolve().parents[1])
                result = (run_worker if argv[0] == '_layout-worker' else recover)(argv[1])
                print(json.dumps(result, ensure_ascii=False))
                return 0 if result.get('state') in {'confirmed', 'rolled_back', 'not_applied'} else 1
            if argv[0] in {'_display-onboarding-worker', '_swap-worker', '_swap-recover', '_reset-worker', '_reset-recover'}:
                from .jobs import trusted_release
                from .system import platform_error
                if os.geteuid() != 0 or platform_error():
                    raise PermissionError('Rocky Linux root 작업입니다.')
                trusted_release(Path(__file__).resolve().parents[1])
                if argv[0] == '_reset-worker' and len(argv) == 2:
                    from .display_reset import run_worker
                    result = run_worker(argv[1])
                elif argv[0] == '_reset-recover' and len(argv) == 1:
                    from .display_reset import recover_all
                    result = recover_all()
                elif argv[0] == '_display-onboarding-worker' and len(argv) == 1:
                    from .display_onboarding import run_pending
                    result = run_pending()
                elif argv[0] == '_swap-worker' and len(argv) == 2:
                    from .screen_swap import run_worker
                    result = run_worker(argv[1])
                elif argv[0] == '_swap-recover' and len(argv) == 1:
                    from .screen_swap import recover_all
                    result = recover_all()
                else:
                    return 64
                if isinstance(result, dict):
                    print(json.dumps(result, ensure_ascii=False))
                    return result.get('exit_code', 0)
                return result if isinstance(result, int) else 0
            if argv[0] == '_screen-worker' and len(argv) == 2:
                from .screen_control import run_worker
                from .jobs import trusted_release
                from .system import platform_error
                if os.geteuid() != 0 or platform_error():
                    raise PermissionError('Rocky Linux root 작업입니다.')
                trusted_release(Path(__file__).resolve().parents[1])
                return run_worker(argv[1])
            if argv[0] in {'_auto-bootstrap', '_runtime-worker'}:
                from .jobs import trusted_release
                from .system import platform_error
                if os.geteuid() != 0 or platform_error():
                    raise PermissionError('Rocky Linux root 작업입니다.')
                trusted_release(Path(__file__).resolve().parents[1])
                if argv[0] == '_auto-bootstrap' and len(argv) == 2:
                    from .auto_update import run_bootstrap
                    return run_bootstrap(argv[1])
                if argv[0] == '_runtime-worker' and len(argv) in {2, 3}:
                    from .runtime import run_worker
                    if len(argv) == 2:
                        return run_worker(argv[1])
                    return run_worker(argv[1], settings_operation_id=argv[2])
                return 64
            if argv[0] == '_frontend-control' and len(argv) == 2:
                from .frontend import control_request
                value = control_request(json.loads(argv[1]))
                print(json.dumps(value, ensure_ascii=False))
                return value.get('exit_code', 1)
            if argv[0] == '_frontend-worker' and len(argv) in {1, 2}:
                from .frontend import run_worker
                from .jobs import trusted_release
                from .system import platform_error
                if os.geteuid() != 0 or platform_error():
                    raise PermissionError('Rocky Linux root 작업입니다.')
                trusted_release(Path(__file__).resolve().parents[1])
                payload = {} if len(argv) == 1 else json.loads(argv[1])
                if not isinstance(payload, dict) or set(payload) - {'retry', 'target_id', 'manifest_url'}:
                    raise ValueError('허용되지 않은 프론트 작업입니다.')
                return run_worker(**payload)
            if argv[0] == '_auto-update' and len(argv) == 2:
                from .auto_update import run_scheduled
                from .jobs import trusted_release
                from .system import platform_error
                if os.geteuid() != 0 or platform_error():
                    raise PermissionError('Rocky Linux root 작업입니다.')
                trusted_release(Path(__file__).resolve().parents[1])
                return run_scheduled(argv[1])
            if argv[0] == '_runtime-dispatch' and len(argv) in {1, 2}:
                from .runtime import dispatch
                from .display import run_chrome
                return dispatch(argv[1] if len(argv) == 2 else 'default', fallback=run_chrome)
            if argv[0] == '_privileged' and len(argv) == 2:
                print(json.dumps(privileged(argv[1]), ensure_ascii=False))
                return 0
            if argv[0] == '_update-worker' and len(argv) == 2:
                from .jobs import trusted_release
                from .system import platform_error
                from .update import run_worker
                if os.geteuid() != 0 or platform_error():
                    raise PermissionError('Rocky Linux root 작업입니다.')
                trusted_release(Path(__file__).resolve().parents[1])
                return run_worker(argv[1])
            if argv[0] == '_worker' and len(argv) == 2:
                from .jobs import trusted_release, run_worker
                from .system import platform_error
                if os.geteuid() != 0 or platform_error():
                    raise PermissionError('Rocky Linux root 작업입니다.')
                trusted_release(Path(__file__).resolve().parents[1])
                return run_worker(argv[1])
            if argv[0] in {'_session','_chrome-run'} and len(argv) in {1, 2, 3}:
                from .display import session_start, run_chrome
                if argv[0] == '_session':
                    return session_start() if len(argv) == 1 else 64
                target = 'default' if len(argv) == 1 else argv[1] if len(argv) == 2 else argv[2] if argv[1] == '--target' else None
                if target is None:
                    return 64
                return run_chrome() if target == 'default' else run_chrome(target=target)
            if argv[0] == '_chrome-user' and len(argv) in {2, 3, 4}:
                from .display import user_action
                if len(argv) == 4 and argv[1:3] == ['monitor-layout', '--request']:
                    value = user_action('monitor-layout', request=json.loads(argv[3]))
                    print(json.dumps(value, ensure_ascii=False))
                    return value.get('exit_code', 0)
                target = None if len(argv) == 2 else argv[2] if len(argv) == 3 else argv[3] if argv[2] == '--target' else None
                if len(argv) > 2 and target is None:
                    return 64
                value = user_action(argv[1]) if target is None or target == 'default' else user_action(argv[1], target=target)
                print(json.dumps(value, ensure_ascii=False))
                return value.get('exit_code', 0)
            return 64
        except Exception as exc:
            print(json.dumps({'error': str(exc)}, ensure_ascii=False))
            return 1
    p = parser()
    args = p.parse_args(argv)
    if args.command == 'display':
        if (args.action in {'plan', 'trial', 'apply'}) != (args.file is not None):
            p.error('display plan/trial에는 배치 파일 하나가 필요합니다. 다른 명령에는 파일을 사용하지 않습니다.')
        if (args.action in {'confirm', 'revert', 'recover'}) != (args.operation_id is not None):
            p.error('display confirm/revert/recover에는 --operation-id가 필요합니다.')
    if args.command in {'setup', 'prepare'} and args.detach and not args.apply:
        p.error('--detach는 --apply와 함께 사용하세요.')
    if args.command == 'update':
        policy_mode = args.policy or args.bootstrap or args.enable_auto or args.disable_auto or args.apply_runtime is not None
        if args.confirm_acceptance and not args.enable_auto:
            p.error('--confirm-acceptance는 --enable-auto와 함께 사용하세요.')
        if policy_mode and (args.prerelease or args.run_id or args.detach):
            p.error('자동 운영 정책·전환 명령에는 --prerelease/--run/--detach를 사용하지 않습니다.')
        if args.detach and (args.check or args.status):
            p.error('--detach는 갱신 실행에만 사용하세요.')
        if args.run_id and not args.status:
            p.error('--run은 --status와 함께 사용하세요.')
        if args.status and args.prerelease:
            p.error('--status에는 시험판 채널 선택이 필요하지 않습니다.')
    svc = service or Service()
    try:
        if args.command is None or args.command in {'tui', 'console'} or (args.command == 'chrome' and not args.action) or (args.command == 'config' and args.action == 'edit'):
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                p.error('메뉴는 SSH 터미널에서 실행하세요. 비대화형 환경에는 하위 명령을 지정하세요.')
            if service is None:
                svc.enter_admin(argv)
            if args.command == 'console':
                from .console import run_console
                return run_console(svc)
            if args.command is None or args.command == 'tui' or args.command == 'chrome':
                from .simple_tui import SimpleApp
                if args.command == 'chrome':
                    SimpleApp(service=svc, initial_section='screens').run()
                else:
                    SimpleApp(service=svc).run()
                return 0
            from .tui import AlaApp
            section = 'config' if args.command == 'config' else 'chrome' if args.command == 'chrome' else 'home'
            AlaApp(service=svc, initial_section=section).run()
            return 0
        if service is None and not (args.command == 'update' and args.check):
            svc.enter_admin(argv)
        if args.command == 'update':
            if args.policy or args.bootstrap or args.enable_auto or args.disable_auto or args.apply_runtime is not None:
                svc.authenticate()
                action, payload = ('auto-update-status', {}) if args.policy else ('auto-update-bootstrap', {}) if args.bootstrap else (
                    'auto-update-enable', {'accepted': args.confirm_acceptance}) if args.enable_auto else (
                    'auto-update-disable', {}) if args.disable_auto else ('runtime-apply', {'target': args.apply_runtime})
                result = svc.request(action, payload)
                emit(result, args.json)
                return result.get('exit_code', 0)
            from .update import check
            if args.check:
                result = check(args.prerelease)
            elif args.status:
                svc.authenticate()
                result = svc.request('update-status', {'run_id': args.run_id})
            else:
                svc.authenticate()
                result = svc.request('update-submit', {'prerelease': args.prerelease})
                if result.get('started') and not args.detach:
                    run_id = result.get('run_id')
                    if not run_id:
                        raise RuntimeError('업데이트 실행 ID를 확인하지 못했습니다. ala update --status로 확인하세요.')
                    previous = None
                    if not args.json:
                        print(f"ala {result.get('latest', result.get('target_version', '?'))} 갱신 · 실행 ID {run_id}")
                        print('SSH·이 표시를 종료해도 갱신 작업은 계속됩니다.')
                    while True:
                        result = svc.request('update-status', {'run_id': run_id})
                        if result.get('state') not in {'queued', 'running'}:
                            break
                        phase = result.get('phase')
                        if not args.json and phase != previous:
                            print('진행 중: ' + update_phase(phase or '상태 확인'))
                        previous = phase
                        time.sleep(1)
            if args.json:
                emit(result, True)
            elif args.status or result.get('state'):
                update_result(result)
            elif result.get('started'):
                print(f"ala {result['latest']} 갱신 시작. SSH 종료 후에도 계속됩니다.")
                print(result['next_action'])
            elif result.get('error'):
                print(result['error'], file=sys.stderr)
            elif result['available']:
                print(f"업데이트 가능: {result['current']} → {result['latest']}. ala update" + (' --prerelease' if args.prerelease else ''))
            else:
                print('선택한 채널에 더 새로운 설치 가능한 릴리스가 없습니다.')
            return result.get('exit_code', 0)
        svc.authenticate()
        as_json = getattr(args, 'json', False)
        if args.command == 'config':
            if args.action == 'show':
                emit(svc.request('config-show'), as_json)
            else:
                config = Config.load(args.file)
                emit(svc.request('config-save', config.to_dict()))
            return 0
        if args.command == 'doctor':
            if args.probe_url:
                Config(url=args.probe_url)  # Same credential-free HTTP(S) validation as DID URLs.
            result = svc.request('doctor-network', {'probe_url': args.probe_url})
            emit(result, as_json)
            return result.get('exit_code', 1)
        if args.command in {'setup', 'prepare'}:
            prefix = 'prepare-' if args.command == 'prepare' else ''
            if not args.apply:
                result = svc.request(prefix + 'plan'); emit(result, as_json)
                return 1 if any(r['state'] in {'blocked','unknown','error'} for r in result['results']) else 0
            job = svc.request(prefix + 'submit')
            if args.detach:
                emit(job, as_json); return 0
            if not as_json:
                print(f'실행 ID: {job["run_id"]}. SSH/TUI 종료 후에도 계속 실행합니다.')
            while job['state'] in {'queued','running'}:
                time.sleep(1)
                job = svc.request('runs-show', {'run_id': job['run_id']})
                if job.get('query_warning'):
                    emit(job, as_json)
                    return 1
            if as_json:
                emit(job, True)
            else:
                from .console import show_job
                show_job(job)
            return job.get('exit_code', 1)
        if args.command in {'check','status'}:
            result = svc.request('inspect' if args.command == 'check' else 'status')
            if as_json:
                emit(result, True)
            else:
                from .console import summarize
                summarize(result)
            return result.get('exit_code', 1) if args.command == 'check' else 0
        if args.command == 'chrome':
            payload = {'action': args.action}
            if args.target is not None:
                payload['target'] = args.target
            result = svc.request('chrome', payload); emit(result, as_json)
            return result.get('exit_code', 0)
        if args.command == 'display':
            if args.action in {'plan', 'trial', 'apply'}:
                from .monitor_layout import load_request
                payload = {'layout': load_request(args.file)}
                if args.action == 'apply': payload['persistent'] = True
                result = svc.request('monitor-layout-plan' if args.action == 'plan' else 'monitor-layout-submit', payload)
            elif args.action in {'trial-status', 'confirm', 'revert', 'recover'}:
                result = svc.request('monitor-layout-status' if args.action == 'trial-status' else 'monitor-layout-' + args.action,
                                     {} if args.operation_id is None else {'operation_id': args.operation_id})
            else:
                result = svc.request('monitor-layout-state' if args.action == 'modes' else 'screens-inspect')
            emit(result, as_json)
            return result.get('exit_code', 0)
        if args.command == 'runs':
            result = svc.request('runs-list' if args.action == 'list' else 'runs-show', {} if args.action == 'list' else {'run_id': args.run_id})
            emit(result, as_json); return 0
        if args.command == 'logs':
            previous = ''
            while True:
                result = svc.request('logs', {'run_id': args.run_id})['log']
                print(result[len(previous):] if result.startswith(previous) else result, end='', flush=True)
                previous = result
                if not args.follow or svc.request('runs-show', {'run_id': args.run_id})['state'] not in {'queued','running'}:
                    return 0
                time.sleep(1)
    except KeyboardInterrupt:
        print('표시를 종료했습니다. 시작한 작업은 계속 실행됩니다.', file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        if getattr(args, 'json', False):
            print(json.dumps({'error': str(exc), 'exit_code': 1}, ensure_ascii=False))
        else:
            print(f'실패: {exc}', file=sys.stderr)
        return 1
    return 0
