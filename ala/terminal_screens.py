"""Site configuration and operations through explicit, line-oriented requests."""
from copy import deepcopy
from urllib.parse import urlsplit

from .config import Config, ScreenConfig


def target_label(value):
    service = value.get('service') or {}
    health = value.get('health') or {}
    url = urlsplit(value.get('url') or '')
    site = (url.netloc + url.path)[:100] if url.netloc else '사이트 미설정'
    return (f"{value['id']} · 출력 {value.get('output') or '자동'} · "
            f"{service.get('ActiveState', '미조회')} · {health.get('state', '미조회')} · {site}")


def edit_target(t, target_id=None, *, report=None):
    before = t.call('config-show')['config']
    targets = [s.to_dict() for s in Config.from_dict(before).screen_targets()]
    target = deepcopy(next((s for s in targets if s['id'] == target_id), None))
    if target_id is not None and target is None:
        raise ValueError('저장된 대상이 없습니다. 목록을 다시 조회하세요.')
    if target is None:
        target = ScreenConfig(id=t.ask('새 화면 ID')).to_dict()
    t.write('Enter 기존값 유지 · 주소에 - 입력 시 안내 화면 · b 취소')
    target['url'] = t.ask('사이트 URL', default=target.get('url') or '-')
    if target['url'] == '-': target['url'] = None
    outputs = [o['id'] for o in (report or {}).get('logical_outputs', [])]
    t.write('확인한 출력: ' + ', '.join(outputs))
    output = t.ask('출력 ID (- 자동)', default=target.get('output') or '-')
    target['output'] = None if output == '-' else output
    if target['id'] != 'default' and target['output'] is None:
        raise ValueError('추가 화면은 출력 ID를 지정하세요.')
    target['layout'] = t.ask('창 배치 (fullscreen/left/right)', default=target.get('layout', 'fullscreen'))
    automatic = t.ask('로그인 자동 시작 (on/off)', default='on' if target.get('autostart') else 'off')
    if automatic not in {'on', 'off'}: raise ValueError('on 또는 off를 입력하세요.')
    target['autostart'] = automatic == 'on'
    if t.yes('프론트 배포 감지 설정도 편집'):
        frontend = deepcopy(target.get('frontend') or {})
        enabled = t.ask('배포 감지 (on/off)', default='on' if frontend.get('enabled') else 'off')
        if enabled not in {'on', 'off'}: raise ValueError('on 또는 off를 입력하세요.')
        frontend['enabled'] = enabled == 'on'
        manifest = t.ask('배포 정보 주소 (- 삭제)', default=frontend.get('manifest_url') or '-')
        frontend['manifest_url'] = None if manifest == '-' else manifest
        target['frontend'] = frontend
    ScreenConfig.from_dict(target)
    t.write('저장할 화면:')
    t.detail(target)
    if not t.yes('화면 설정 저장'): return
    saved = t.call('screens-save', {'target': target, 'expected_config': before})
    t.result(saved)
    if not saved.get('saved') or not isinstance(saved.get('config'), dict):
        raise RuntimeError('저장된 구성을 확인하지 못했습니다. 다시 조회하세요.')
    if target_id is None:
        t.write('새 대상은 간편 설정에서 등록한 뒤 시작하세요.')
    elif t.yes('방금 저장한 이 화면의 설정 적용'):
        t.result(t.call('screens-apply', {'target': target['id'], 'expected_config': saved['config']}))
        t.write('적용 접수와 실제 화면 정상은 다릅니다. 목록을 다시 조회하세요.')


def operate(t, target, report):
    target_id = target['id']
    t.write('\n' + target_label(target))
    t.write('1 설정 편집 · 2 설정 적용 · 3 시작 · 4 중지 · 5 재시작 · 6 페이지 새로고침 · 7 로그 · 8 상세 · 9 새 관리 코드 적용 · f 배포 재시도 · w 두 사이트 출력 교환 · x 교환 복구 · b 돌아가기')
    choice = t.ask('선택')
    if choice == '1': edit_target(t, target_id, report=report)
    elif choice == '8': t.detail(target)
    elif choice == '7': t.result(t.call('screens-action', {'target': target_id, 'action': 'logs'}))
    elif choice == '2':
        config = t.call('config-show')['config']
        t.write('선택 화면에 적용할 저장 설정:')
        t.detail(next(s.to_dict() for s in Config.from_dict(config).screen_targets() if s.id == target_id))
        if t.yes('선택 화면의 저장 설정 적용'):
            t.result(t.call('screens-apply', {'target': target_id, 'expected_config': config}))
    elif choice in {'3', '4', '5', '6'}:
        action, label = {'3': ('start', '시작'), '4': ('stop', '중지'),
                         '5': ('restart', '재시작'), '6': ('reload', '페이지 새로고침')}[choice]
        if t.yes(f'{target_id} {label}'):
            t.result(t.call('screens-action', {'target': target_id, 'action': action}))
    elif choice == '9':
        if t.yes(f'{target_id} 새 관리 코드 적용'):
            t.result(t.call('runtime-apply', {'target': target_id}))
    elif choice == 'f':
        if t.yes(f'{target_id} 프론트 배포 재시도'):
            t.result(t.call('frontend-retry', {'target': target_id}))
    elif choice == 'w':
        expected = t.call('config-show')['config']
        observed = t.call('screens-inspect')
        source = next((s for s in observed.get('targets', []) if s['id'] == target_id), None)
        if source is None: raise ValueError('대상을 다시 조회하세요.')
        other = t.pick('교환할 사이트', [s for s in observed['targets'] if s['id'] != target_id], target_label)
        t.write(f"{target_id}: {source.get('output')} → {other.get('output')}")
        t.write(f"{other['id']}: {other.get('output')} → {source.get('output')}")
        t.write('기존 사이트·코드와 수동 중지를 보존하며 두 출력 배정만 교환합니다.')
        if t.yes('두 사이트 출력 교환'):
            t.result(t.call('screens-swap-submit', {'source': target_id, 'destination': other['id'],
                'expected_config': expected, 'expected_topology': observed.get('logical_outputs', [])}))
    elif choice == 'x':
        status = t.call('screens-swap-status', {'target': target_id}); t.detail(status)
        if status.get('operation_id') and t.yes('기록된 두 화면 교환 복구'):
            t.result(t.call('screens-swap-retry', {'operation_id': status['operation_id']}))
    t.write('표시는 마지막 조회 시점의 상태입니다. 최신 상태는 다시 조회하세요.')


def screens(t):
    report = t.call('screens-inspect')
    while True:
        t.write('\n사이트·Chrome · 마지막 명시 조회 결과')
        for i, target in enumerate(report.get('targets', []), 1): t.write(f'{i}. {target_label(target)}')
        t.write('r 다시 조회 · n 화면 추가 · i 모니터 식별 · b 돌아가기')
        choice = t.ask('대상 번호 또는 메뉴')
        if choice == 'r': report = t.call('screens-inspect')
        elif choice == 'n': t.guarded(lambda: edit_target(t, report=report))
        elif choice == 'i': t.result(t.call('screens-identify'))
        elif choice.isdecimal() and 1 <= int(choice) <= len(report.get('targets', [])):
            t.guarded(lambda: operate(t, report['targets'][int(choice) - 1], report))
