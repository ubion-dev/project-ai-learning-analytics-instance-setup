"""Numeric monitor editing; draft changes never make device requests."""
from copy import deepcopy

from .monitor_draft import LayoutDraft
from .terminal import Cancelled


ACTIVE = {'queued', 'applying', 'awaiting_confirmation', 'rolling_back', 'recovery_required'}


def describe(model, item):
    mode = model.mode(item['id'])
    scale = '미선택' if item['scale'] is None else f"{item['scale'] * 100:g}%"
    return (f"{item['id']} · ({item['x']}, {item['y']}) · "
            f"{mode['width']}×{mode['height']} {mode['refresh_hz']:g}Hz · 배율 {scale}")


def position(t, model):
    t.write('1 좌표 입력 · 2 다른 모니터와 위치 교환 · 3 다른 모니터 오른쪽 · 4 왼쪽 · 5 아래 · 6 위 · b 취소')
    choice = t.ask('배치 방법')
    a = next(o for o in model.rects() if o['id'] == model.selected)
    if choice == '1':
        model.drop(model.selected, int(t.ask('가로 X', default=str(a['x']))),
                   int(t.ask('세로 Y', default=str(a['y']))), snap=0)
    elif choice in {'2', '3', '4', '5', '6'}:
        b = t.pick('기준 모니터', [o for o in model.rects() if o['id'] != model.selected], lambda o: o['id'])
        if choice == '2': model.drop(model.selected, a['x'], a['y'], b['id'])
        else:
            x, y = {'3': (b['x'] + b['width'], b['y']), '4': (b['x'] - a['width'], b['y']),
                    '5': (b['x'], b['y'] + b['height']), '6': (b['x'], b['y'] - a['height'])}[choice]
            model.drop(model.selected, x, y, snap=0)


def edit(t, model, output_id):
    model.selected = output_id
    while True:
        t.write('\n' + describe(model, model.output()))
        t.write('1 위치 · 2 해상도·주사율 · 3 화면 배율 · b 편집 목록으로')
        choice = t.ask('선택')
        if choice == '1': t.guarded(lambda: position(t, model))
        elif choice == '2':
            def change_mode():
                mode = t.pick('지원 해상도·주사율', model.modes(),
                    lambda m: f"{m['width']}×{m['height']} {m['refresh_hz']:g}Hz" + (' 권장' if m['preferred'] else ''))
                model.set_mode(mode['id'])
                if model.output()['scale'] is None: t.write('기존 배율은 이 모드에서 지원되지 않습니다. 배율을 다시 선택하세요.')
            t.guarded(change_mode)
        elif choice == '3':
            def change_scale():
                model.set_scale(t.pick('지원 화면 배율', model.mode()['supported_scales'], lambda s: f'{s * 100:g}%'))
            t.guarded(change_scale)


def operation(t, status):
    t.result(status)
    operation_id = status.get('operation_id')
    if not operation_id: return
    if status['state'] == 'awaiting_confirmation':
        t.write('실제 모니터를 확인하세요. 확인하지 않으면 서버가 원래 배치로 복원을 요청합니다.')
        if t.yes('이 배치 유지', deadline=status.get('deadline_monotonic')):
            t.result(t.call('monitor-layout-confirm', {'operation_id': operation_id}))
        else: t.result(t.call('monitor-layout-revert', {'operation_id': operation_id}))
    elif status['state'] == 'recovery_required':
        if t.yes('원래 배치 복구 재시도'):
            t.result(t.call('monitor-layout-recover', {'operation_id': operation_id}))
    elif status['state'] in ACTIVE:
        t.write('작업이 진행 중입니다. 모니터 메뉴를 다시 열어 상태를 확인하세요.')


def apply(t, model, settings):
    model.validate()
    request = deepcopy(model.request)
    plan = t.call('monitor-layout-plan', {'layout': request})
    t.result(plan)
    if plan.get('state') != 'validated':
        raise ValueError('현재 장비의 배치 검증을 통과하지 못했습니다. 다시 조회하세요.')
    t.write('변경 검토 · 사이트 주소와 배정은 유지합니다.')
    before = LayoutDraft(settings)
    for item in model.request['outputs']:
        t.write('이전: ' + describe(before, before.output(item['id'])))
        t.write('이후: ' + describe(model, item))
    persistent = t.ask('다음 로그인에도 유지 (on/off)', default='on')
    if persistent not in {'on', 'off'}: raise ValueError('on 또는 off를 입력하세요.')
    if not t.yes('배치 적용'): return False
    status = t.call('monitor-layout-submit', {'layout': request, 'persistent': persistent == 'on'})
    t.result(status)
    operation_id = status.get('operation_id')
    # Only track the submitted operation. The detached worker owns the deadline.
    for _ in range(30):
        if not operation_id or status.get('state') not in {'queued', 'applying'}: break
        t.sleep(1)
        status = t.service.request('monitor-layout-status')
        if status.get('operation_id') != operation_id:
            raise RuntimeError('배치 작업이 변경됐습니다. 다시 조회하세요.')
    try:
        operation(t, status)
    except Cancelled as exc:
        t.write(str(exc) or '배치 확인을 취소했습니다. 서버의 확인 기한과 복원 처리는 유지됩니다.')
    return True


def monitors(t):
    status = t.call('monitor-layout-status')
    if status.get('state') in ACTIVE:
        operation(t, status)
        return
    settings = t.call('monitor-layout-state')
    t.result(settings)
    model = LayoutDraft(settings)
    while True:
        t.write('\n모니터 편집 · 아직 OS에 적용하지 않은 초안')
        outputs = model.request['outputs']
        for i, item in enumerate(outputs, 1): t.write(f'{i}. {describe(model, item)}')
        t.write('a 변경 검토·적용 · r 다시 조회 (초안 폐기) · u 편집 취소 · i 모니터 식별 · p 로그인 복원 · b 돌아가기')
        choice = t.ask('모니터 번호 또는 메뉴')
        if choice.isdecimal() and 1 <= int(choice) <= len(outputs):
            t.guarded(lambda: edit(t, model, outputs[int(choice) - 1]['id']))
        elif choice == 'u': model = LayoutDraft(settings)
        elif choice == 'r':
            if model.dirty and not t.yes('미저장 초안을 버리고 다시 조회'): continue
            status = t.call('monitor-layout-status')
            if status.get('state') in ACTIVE:
                operation(t, status); return
            settings = t.call('monitor-layout-state'); model = LayoutDraft(settings)
        elif choice == 'i': t.result(t.call('screens-identify'))
        elif choice == 'p':
            t.detail(t.call('monitor-profile-status'))
            action = t.ask('1 로그인 복원 끄기 · 2 저장 배치 복원 재시도 · b 취소')
            if action in {'1', '2'} and t.yes('로그인 복원 정책 변경'):
                t.result(t.call('monitor-profile-disable' if action == '1' else 'monitor-profile-retry'))
                return
        elif choice == 'a':
            def submit():
                if not settings.get('trial_supported'):
                    raise ValueError('배치 시험 기능을 확인하지 못했습니다. 확장 갱신·GNOME 재로그인 후 다시 조회하세요.')
                return apply(t, model, settings)
            if t.guarded(submit): return
