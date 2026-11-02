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
    doctor = sub.add_parser('doctor', help='읽기 전용 진단').add_subparsers(dest='action', required=True)
    network = doctor.add_parser('network', help='Wi-Fi·네트워크 계층 진단')
    network.add_argument('--probe-url', help='명시한 HTTP/HTTPS 주소만 통신 확인')
    network.add_argument('--json', action='store_true')
    for name in ('status', 'check'):
        sub.add_parser(name).add_argument('--json', action='store_true')
    chrome = sub.add_parser('chrome')
    chrome.add_argument('action', nargs='?', choices=['start','stop','restart','status','logs','check'])
    chrome.add_argument('--json', action='store_true')
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

def main(argv=None, *, service=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Not part of the public command grammar. Root-owned installed entry only.
    if argv and argv[0] in {'_privileged','_worker','_session','_chrome-run','_chrome-user'}:
        try:
            if argv[0] == '_privileged' and len(argv) == 2:
                print(json.dumps(privileged(argv[1]), ensure_ascii=False))
                return 0
            if argv[0] == '_worker' and len(argv) == 2:
                from .jobs import trusted_release, run_worker
                from .system import platform_error
                if os.geteuid() != 0 or platform_error():
                    raise PermissionError('Rocky Linux root 작업입니다.')
                trusted_release(Path(__file__).resolve().parents[1])
                return run_worker(argv[1])
            if argv[0] in {'_session','_chrome-run'} and len(argv) == 1:
                from .display import session_start, run_chrome
                return session_start() if argv[0] == '_session' else run_chrome()
            if argv[0] == '_chrome-user' and len(argv) == 2:
                from .display import user_action
                value = user_action(argv[1])
                print(json.dumps(value, ensure_ascii=False))
                return value.get('exit_code', 0)
            return 64
        except Exception as exc:
            print(json.dumps({'error': str(exc)}, ensure_ascii=False))
            return 1
    p = parser()
    args = p.parse_args(argv)
    if getattr(args, 'detach', False) and not getattr(args, 'apply', False):
        p.error('--detach는 --apply와 함께 사용하세요.')
    svc = service or Service()
    try:
        if args.command is None or (args.command == 'chrome' and not args.action) or (args.command == 'config' and args.action == 'edit'):
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                p.error('메뉴는 SSH 터미널에서 실행하세요. 비대화형 환경에는 하위 명령을 지정하세요.')
            from .tui import AlaApp
            section = 'config' if args.command == 'config' else 'chrome' if args.command == 'chrome' else 'home'
            AlaApp(service=svc, initial_section=section).run()
            return 0
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
            emit(job, as_json)
            return job.get('exit_code', 1)
        if args.command in {'check','status'}:
            result = svc.request('inspect' if args.command == 'check' else 'status')
            emit(result, as_json)
            return result.get('exit_code', 1) if args.command == 'check' else 0
        if args.command == 'chrome':
            result = svc.request('chrome', {'action': args.action}); emit(result, as_json)
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
        print('표시를 종료했습니다. 시작한 설치 작업은 계속 실행됩니다.', file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        if getattr(args, 'json', False):
            print(json.dumps({'error': str(exc), 'exit_code': 1}, ensure_ascii=False))
        else:
            print(f'실패: {exc}', file=sys.stderr)
        return 1
    return 0
