"""A durable, two-target output exchange. Reads never resume a transaction."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import re
import os
import time
import uuid

from .config import Config
from .jobs import JobStore, change_lock
from .runtime import RuntimeStore, target_id, code_ready
from .system import Host, Paths, atomic_json, atomic_write

ID = re.compile(r'[a-f0-9]{32}\Z')
TERMINAL = {'completed', 'rolled_back'}
MARKER = '# ala managed screen swap recovery v1'
LEASE = 900


def _now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, paths=None, host=None, *, monotonic=None):
        self.paths, self.host = paths or Paths(), host or Host()
        self.runtime = RuntimeStore(self.paths, self.host, monotonic=monotonic)
        self.base = self.runtime.base / 'swaps'

    def path(self, operation_id):
        if not isinstance(operation_id, str) or not ID.fullmatch(operation_id):
            raise ValueError('유효한 화면 교환 작업 ID가 필요합니다.')
        return self.base / (operation_id + '.json')

    def load(self, operation_id):
        value = self.runtime._read(self.path(operation_id))
        if (not isinstance(value, dict) or value.get('schema_version') != 1
                or value.get('operation_id') != operation_id
                or not isinstance(value.get('targets'), list) or len(value['targets']) != 2
                or len(set(value['targets'])) != 2
                or value.get('decision') not in {None, 'commit', 'rollback'}
                or value.get('state') not in {'queued', 'running', 'unknown', 'recovery_required', *TERMINAL}):
            raise ValueError('화면 교환 기록 형식이 올바르지 않습니다.')
        for target in value['targets']:
            target_id(target)
            part = value['participants'][target]
            for key in ('before', 'after', 'desired_before', 'desired_after'):
                self.runtime._validate_selection(part[key])
        if value.get('unit') != 'ala-runtime-swap-' + operation_id + '.service':
            raise ValueError('화면 교환 서비스가 기록과 다릅니다.')
        return value

    def save(self, value):
        self.runtime._write(self.path(value['operation_id']), value)

    def records(self):
        if not self.base.exists():
            return []
        self.runtime._safe(self.base)
        return [self.load(path.stem) for path in sorted(self.base.iterdir())
                if path.suffix == '.json' and ID.fullmatch(path.stem)]

    def pending(self, target=None):
        records = [v for v in self.records() if v['state'] not in TERMINAL
                   and (target is None or target in v['targets'])]
        if len(records) > 1 and target is not None:
            raise RuntimeError('대상의 화면 교환 기록이 겹칩니다. 관리자 복구가 필요합니다.')
        return records

    def owner_state(self, value):
        recovery = (value.get('recovery_unit') == 'ala-screen-swap-recover.service'
                    and value['phase'] in {'rollback', 'committing'})
        if not recovery and value['boot_id'] != self.runtime.boot_id():
            return 'inactive'
        unit = value['recovery_unit'] if recovery else value['unit']
        result = self.host.run(['systemctl', 'show', unit, '--property=ActiveState', '--property=InvocationID'], timeout=5)
        properties = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if result.returncode or properties.get('ActiveState') not in {'active', 'activating', 'deactivating', 'reloading', 'inactive', 'failed'}:
            return 'unknown'
        if recovery and value.get('recovery_invocation') and properties['ActiveState'] not in {'inactive', 'failed'}:
            if not properties.get('InvocationID'):
                return 'unknown'
            if properties['InvocationID'] != value['recovery_invocation']:
                return 'inactive'
        return 'inactive' if properties['ActiveState'] in {'inactive', 'failed'} else 'active'

    def owner_active(self, value):
        return self.owner_state(value) == 'active'


def guard(*, paths=None, host=None, target=None, operation_id=None):
    """An unfinished journal fences changes even after its lease/boot expired."""
    pending = Store(paths, host).pending(target)
    if any(v['operation_id'] != operation_id for v in pending):
        raise RuntimeError('두 화면 교환 또는 복구가 진행 중입니다. 교환 상태를 확인하세요.')


def note_stop(target, *, paths=None, host=None):
    """Called under the common lock before a public stop invalidates a lease."""
    store = Store(paths, host)
    for value in store.pending(target):
        stopped = value.setdefault('stop_requests', [])
        if target not in stopped:
            stopped.append(target)
        value['cancel_requested'] = True
        if value['decision'] != 'commit':
            value['decision'] = 'rollback'
        store.save(value)


def effective_selection(target, *, paths=None, host=None, committed=False):
    """Read the pair's assignment view; this function never authorizes starts."""
    pending = Store(paths, host).pending(target)
    if not pending:
        return None
    value = pending[0]
    after = value['decision'] == 'commit' or (not committed and value['decision'] is None
            and value['phase'] in {'trial', 'observing', 'committing'})
    return deepcopy(value['participants'][target]['after' if after else 'before'])


def conflict_selection(target, selected_target, pin, *, paths=None, host=None):
    """Use the full pair map during preflight, including a stopped partner."""
    if not pin:
        return None
    for value in Store(paths, host).pending(selected_target):
        if target not in value['targets']:
            return None
        own = value['participants'][selected_target]
        for side in ('after', 'before'):
            if pin == own[side]:
                return deepcopy(value['participants'][target][side])
    return None


def user_selection(target, *, paths=None, host=None, observation=False):
    """Fence login/restart on a dead worker, partial reservation, or recovery."""
    store = Store(paths, host)
    pending = store.pending(target)
    if not pending:
        return None
    value = pending[0]
    if observation and os.environ.get('ALA_RUNTIME_ACTION') in {'stop', 'status', 'check', 'logs', 'windows'}:
        pin = os.environ.get('ALA_RUNTIME_CONFIG')
        if os.environ.get('ALA_RUNTIME_TARGET') == target:
            for side in ('before', 'after'):
                selected = value['participants'][target][side]
                if pin == selected['config_path']:
                    return deepcopy(selected)
    if value['decision'] == 'commit':
        # The decision is durable, but starts still wait for complete materialization.
        raise RuntimeError('화면 교환 확정 기록 정리를 기다립니다.')
    internal_before = (observation and os.environ.get('ALA_RUNTIME_CONFIG') == value['participants'][target]['before']['config_path']
                       and os.environ.get('ALA_RUNTIME_TARGET') == target)
    if (value['state'] != 'running' or not store.owner_active(value)
            or (value['phase'] not in {'trial', 'observing', 'rollback'} and not internal_before)):
        raise RuntimeError('미완료 화면 교환의 복구가 필요합니다. 새 시작을 보류했습니다.')
    if value.get('cancel_requested') and value['phase'] != 'rollback':
        raise RuntimeError('화면 교환 취소 후 복구를 기다립니다.')
    if value['phase'] in {'trial', 'observing'} and not _valid(store, value):
        raise RuntimeError('교환 예약이 만료되어 복구 전 새 시작을 보류했습니다.')
    return effective_selection(target, paths=store.paths, host=store.host)


def topology_snapshot(value):
    if isinstance(value, list):
        value = {'state': 'ok', 'logical_outputs': value}
    if not isinstance(value, dict):
        raise ValueError('검토한 출력 배치가 필요합니다.')
    fields = ('id', 'connectors', 'x', 'y', 'width', 'height', 'scale', 'transform',
              'primary', 'mirrored', 'confirmed')
    logical = value.get('logical_outputs')
    if not isinstance(logical, list):
        raise ValueError('검토한 논리 출력 목록이 필요합니다.')
    outputs = []
    for output in logical:
        if not isinstance(output, dict) or not isinstance(output.get('id'), str):
            raise ValueError('출력 식별자가 올바르지 않습니다.')
        geometry = output.get('geometry', output)
        normalized = {key: deepcopy(geometry.get(key) if key in {'x', 'y', 'width', 'height'}
                                   else output.get(key)) for key in fields}
        normalized['connectors'] = sorted(normalized['connectors'] or [])
        outputs.append(normalized)
    return {'state': value.get('output_state', value.get('state')),
            'logical_outputs': sorted(outputs, key=lambda output: output['id'])}


def _capable(store, selection, target):
    path = Path(selection['release']) / 'COMPATIBILITY.json'
    if not path.is_file() or store.runtime._read(path).get('screen_swap_protocol') != 1:
        raise RuntimeError('선택한 화면 코드가 두 화면 교환을 지원하지 않습니다. 두 DID의 화면 런타임을 먼저 업데이트하세요.')
    model = Config.from_dict(store.runtime._validate_selection(selection)).for_screen(target)
    if model.url is None and store.runtime._read(path).get('setup_page_protocol') != 1:
        raise RuntimeError('선택한 화면 코드가 설정 안내를 지원하지 않습니다. 화면 런타임을 먼저 업데이트하세요.')


def _config(store):
    return Config.from_dict(store.runtime._read(store.paths.config))


def _candidate(store, selection, pair):
    base = Config.from_dict(store.runtime._validate_selection(selection))
    # Every snapshot validates the whole topology. Replace the list together;
    # applying one swapped assignment would create a temporary duplicate.
    screens = {screen.id: screen for screen in base.screen_targets()}
    for reviewed in pair:
        screens[reviewed.id] = replace(screens.get(reviewed.id, reviewed), output=reviewed.output)
    return store.runtime.selection(selection['release'], replace(base, schema_version=2, screens=tuple(screens.values())))


def _inventory(config, store):
    from .screens import _inventory as inspect_outputs
    return topology_snapshot(inspect_outputs(config, store.host, store.paths))


def _control(store, target, action, selection, *, timeout):
    from .screens import screen_action
    model = replace(Config.from_dict(store.runtime._validate_selection(selection)),
                    runtime_pin=selection).for_screen(target)
    return screen_action(action, model, target=target, host=store.host, paths=store.paths,
                         timeout=timeout, internal=True)


def _public(value):
    return {key: deepcopy(value[key]) for key in ('operation_id', 'source', 'destination', 'targets',
            'state', 'phase', 'decision', 'results', 'reason', 'next_action', 'started_at', 'ended_at') if key in value} | {
            'exit_code': 0 if value['state'] == 'completed' else 1 if value['state'] in {'rolled_back', 'recovery_required', 'unknown'} else 2}


def status(*, paths=None, host=None, target=None, operation_id=None):
    store = Store(paths, host)
    if operation_id is not None:
        value = store.load(operation_id)
    else:
        values = [v for v in store.records() if target is None or target in v['targets']]
        if not values:
            return {'state': 'idle', 'phase': 'idle', 'targets': [], 'exit_code': 0}
        values.sort(key=lambda value: value['started_at'])
        value = next((v for v in reversed(values) if v['state'] not in TERMINAL), values[-1])
    result = _public(value)
    expired = value['boot_id'] != store.runtime.boot_id() or store.runtime.monotonic() >= value['expires_monotonic']
    if value['state'] in {'queued', 'running'} and (not store.owner_active(value)
            or (value['phase'] != 'rollback' and expired)):
        result.update(state='recovery_required', reason='교환 작업의 실행을 확인하지 못했습니다.',
                      next_action='두 화면 교환 복구를 다시 요청하세요.', exit_code=1)
    return result


def submit(source, destination, expected_config, expected_topology, *, release=None, paths=None, host=None):
    store = Store(paths, host)
    source, destination = target_id(source), target_id(destination)
    if source == destination:
        raise ValueError('서로 다른 두 DID를 선택하세요.')
    expected = Config.from_dict(expected_config)
    topology = topology_snapshot(expected_topology)
    with change_lock(store.paths):
        guard(paths=store.paths, host=store.host)
        current = _config(store)
        if current != expected:
            raise RuntimeError('검토 후 구성이 변경되었습니다. 다시 조회하고 교환하세요.')
        if not current.display or current.schema_version != 2:
            raise ValueError('명시적 출력이 있는 다중 화면 구성이 필요합니다.')
        if any(v['state'] in {'queued', 'running'} for v in JobStore(store.paths, store.host).list()):
            raise RuntimeError('설치 작업이 끝난 뒤 교환하세요.')
        if topology['state'] != 'ok' or _inventory(current, store) != topology:
            raise RuntimeError('검토 후 출력 배치가 변경되었습니다. 다시 조회하고 교환하세요.')
        from .monitors import resolve_output
        from .screens import screen_action
        targets = {screen.id: screen for screen in current.screen_targets()}
        if any(target not in targets for target in (source, destination)):
            raise ValueError('구성에 있는 두 DID를 선택하세요.')
        pair = [targets[source], targets[destination]]
        resolved = [resolve_output(topology, screen.output, auto=screen.id == 'default') for screen in pair]
        if (any(screen.layout != 'fullscreen' for screen in pair)
                or any(value.get('state') != 'ok' for value in resolved)
                or resolved[0]['output']['id'] == resolved[1]['output']['id']):
            raise RuntimeError('서로 다른 두 논리 출력의 전체화면 DID만 교환할 수 있습니다.')
        occupied = {value['output']['id'] for value in resolved}
        for other in current.screen_targets():
            if other.id not in {source, destination}:
                found = resolve_output(topology, other.output, auto=other.id == 'default')
                if found.get('state') == 'ok' and found['output']['id'] in occupied:
                    raise RuntimeError('교환 출력에 다른 DID가 배정돼 있습니다.')
        after = replace(current, screens=tuple(replace(screen, output=pair[1].output if screen.id == source else pair[0].output)
                        if screen.id in {source, destination} else screen for screen in current.screens))
        operation_id = uuid.uuid4().hex
        pinned, _ = store.runtime.release(release or store.paths.at('/opt/ala/current'))
        value = {'schema_version': 1, 'operation_id': operation_id, 'source': source, 'destination': destination,
                 'targets': [source, destination], 'unit': 'ala-runtime-swap-' + operation_id + '.service',
                 'release': str(pinned), 'boot_id': store.runtime.boot_id(), 'started_at': _now(), 'ended_at': None,
                 'expires_monotonic': store.runtime.monotonic() + LEASE,
                 'state': 'queued', 'phase': 'prepared', 'decision': None, 'participants': {},
                 'before_config': current.to_dict(), 'after_config': after.to_dict(), 'topology': topology,
                 'results': {}, 'stop_requests': [], 'cancel_requested': False,
                 'reason': '두 화면 교환을 접수했습니다.', 'next_action': '교환 진행 상태를 확인하세요.'}
        for target in value['targets']:
            state = store.runtime.load(target)
            if state is None or store.runtime.active(state):
                raise RuntimeError('두 DID의 관리 버전과 진행 중인 작업을 확인하세요.')
            for selection in (state['committed'], state['desired']):
                _capable(store, selection, target)
            applied = Config.from_dict(store.runtime._validate_selection(state['committed'])).for_screen(target)
            if next(screen for screen in applied.screen_targets() if screen.id == target) != targets[target]:
                raise RuntimeError('기존 설정 적용을 마친 뒤 교환하세요.')
            observed = screen_action('status', current, target=target, host=store.host, paths=store.paths)
            service = observed.get('service') or {}
            active = service.get('ActiveState')
            if service.get('QueryError') or active not in {'active', 'inactive', 'failed'}:
                raise RuntimeError('두 DID의 실행 상태를 확실히 확인한 뒤 교환하세요.')
            stopped = store.runtime.is_stopped(state)
            if stopped and active == 'active':
                raise RuntimeError('수동 중지와 실제 실행 상태가 다릅니다. 먼저 점검하세요.')
            value['participants'][target] = {'before': state['committed'], 'desired_before': state['desired'],
                'after': _candidate(store, state['committed'], [screen for screen in after.screens if screen.id in {source, destination}]),
                'desired_after': _candidate(store, state['desired'], [screen for screen in after.screens if screen.id in {source, destination}]),
                'generation': state['generation'], 'was_running': active == 'active', 'was_stopped': stopped,
                'token': None}
        register_recovery(release=pinned, paths=store.paths, host=store.host)
        store.save(value)  # The fence exists before the first reservation can be written.
        try:
            for target, part in value['participants'].items():
                part['token'] = store.runtime.reserve(target, 'runtime', ttl=LEASE, unit=value['unit'],
                    expected_generation=part['generation'], expected_revision=part['before']['revision'],
                    settings={'committed': part['after'], 'desired': part['desired_after']},
                    swap_operation_id=operation_id)
                store.save(value)
            response = store.host.run(['systemd-run', '--unit=' + value['unit'], '--collect',
                '--expand-environment=no', '--property=Type=exec', '--property=TimeoutStartSec=infinity',
                '--property=OnFailure=ala-screen-swap-recover.service',
                str(pinned / '.venv/bin/python'), '-I', str(pinned / 'entry.py'), '_swap-worker', operation_id], timeout=30)
            if response.returncode:
                value.update(state='unknown' if response.returncode == 124 else 'recovery_required',
                             reason='교환 서비스 접수 여부를 확인하지 못했습니다.', next_action='교환 복구를 요청하세요.')
                store.save(value)
        except Exception:
            value.update(state='recovery_required', decision='rollback', reason='교환 예약을 완료하지 못했습니다.')
            store.save(value)
            raise
    return _public(value)


def _valid(store, value):
    latest = store.load(value['operation_id'])
    if latest.get('cancel_requested') or latest['boot_id'] != store.runtime.boot_id():
        return False
    for target, part in value['participants'].items():
        token = part.get('token')
        if token is None or not store.runtime.valid(token):
            return False
    return True


def _phase(store, value, phase):
    with change_lock(store.paths, blocking=True):
        current = store.load(value['operation_id'])
        if current.get('cancel_requested'):
            raise RuntimeError('운영자 요청으로 교환을 취소했습니다.')
        if not _valid(store, current):
            raise RuntimeError('교환 예약이 변경됐습니다.')
        current.update(state='running', phase=phase)
        if phase == 'stopping':
            current['lifecycle_started'] = True
        for part in current['participants'].values():
            store.runtime.phase(part['token'], 'observing' if phase == 'observing' else 'trial' if phase == 'trial' else 'preflight')
        store.save(current)
    value.update(current)


def _sync(store):
    from .runtime import _sync_autostart
    _sync_autostart(_config(store), store.paths, store.host)


def _materialize(store, value, after):
    """Idempotent redo/undo. Keep newer stop intent and never rewind generations."""
    current = _config(store).to_dict()
    if current not in (value['before_config'], value['after_config']):
        raise RuntimeError('전역 구성이 변경되어 자동으로 덮어쓰지 않았습니다.')
    for target, part in value['participants'].items():
        state = store.runtime.load(target)
        if state is None:
            raise RuntimeError('복구할 화면 런타임이 없습니다.')
        operation = state.get('operation') or {}
        if operation and operation.get('swap_operation_id') != value['operation_id']:
            raise RuntimeError('다른 작업이 화면 선택을 변경해 자동 복구하지 않았습니다.')
        if state['committed'] not in (part['before'], part['after']) or state['desired'] not in (part['desired_before'], part['desired_after']):
            raise RuntimeError('복구 이후의 다른 화면 선택을 덮어쓰지 않았습니다.')
        committed, desired = part['after' if after else 'before'], part['desired_after' if after else 'desired_before']
        if state['committed'] != committed or state['desired'] != desired or operation:
            state['committed'], state['desired'] = deepcopy(committed), deepcopy(desired)
            state['generation'] += 1
            if operation:
                store.runtime._record_result(state, operation, 'completed' if after else 'failed',
                                             '두 화면 교환 완료' if after else '두 화면 교환 이전 배정 복구')
            state['operation'] = None
            store.runtime._write(store.runtime.state_path(target), state)
    atomic_json(store.paths.config, value['after_config' if after else 'before_config'], 0o644)
    _sync(store)


def _execute(operation_id, *, paths=None, host=None, control=None, monotonic=None, sleep=None, recovering=False):
    store = Store(paths, host, monotonic=monotonic)
    clock, pause = monotonic or time.monotonic, sleep or time.sleep
    value = store.load(operation_id)
    if value['state'] in TERMINAL:
        return _public(value)
    control = control or (lambda target, action, selection, **kwargs: _control(store, target, action, selection, **kwargs))

    def invoke(target, action, selection, deadline, *, recovery=False):
        remaining = deadline - clock()
        if remaining <= 0:
            raise RuntimeError('두 화면 교환 단계의 제한 시간이 초과됐습니다.')
        if action in {'start', 'stop'}:
            with change_lock(store.paths, blocking=True):
                latest = store.load(operation_id)
                if action == 'start' and (target in latest.get('stop_requests', [])
                        or store.runtime.is_stopped(store.runtime.load(target))):
                    raise RuntimeError('수동 중지한 DID는 다시 켜지 않습니다.')
                if not recovery and not _valid(store, latest):
                    raise RuntimeError('화면 교환이 취소됐습니다.')
                return control(target, action, selection, timeout=min(30, remaining))
        return control(target, action, selection, timeout=min(30, remaining))

    def stop(target, selection, deadline, *, recovery=False):
        while clock() < deadline:
            response = invoke(target, 'stop', selection, deadline, recovery=recovery)
            if response.get('exit_code') == 0 and response.get('state') == 'stopped':
                return
            if response.get('exit_code', 1) not in {0, 2}:
                break
            pause(min(1, max(0, deadline - clock())))
        raise RuntimeError('관리 창과 Chrome의 종료를 확인하지 못했습니다.')

    def ready_all(deadline, *, stable=False, before=False, recovery=False):
        since = None
        while clock() < deadline:
            latest = store.load(operation_id)
            if not recovery and not _valid(store, latest):
                raise RuntimeError('화면 교환이 취소됐습니다.')
            ready = True
            for target, part in value['participants'].items():
                if not part['was_running'] or target in latest.get('stop_requests', []):
                    continue
                selected = part['before' if before else 'after']
                report = invoke(target, 'check', selected, deadline, recovery=recovery)
                model = Config.from_dict(store.runtime._validate_selection(selected)).for_screen(target)
                ready &= code_ready(report, selected['version'], selected['revision'], setup=model.url is None)
            if ready:
                if not stable:
                    return
                since = clock() if since is None else since
            elif stable:
                raise RuntimeError('두 화면의 안정 관측에 실패했습니다.')
            pause(min(1, max(0, deadline - clock())))
        if stable and since is not None:
            return
        raise RuntimeError('두 화면의 시작 후조건을 확인하지 못했습니다.')

    def recover():
        nonlocal value
        with change_lock(store.paths, blocking=True):
            value = store.load(operation_id)
            if value['state'] in TERMINAL:
                return _public(value)
            if recovering:
                owner = store.owner_state(value)
                if owner != 'inactive':
                    return _public(value) | {'state': 'unknown', 'exit_code': 1,
                        'reason': '기존 교환 작업의 종료를 확인하지 못해 병행 복구하지 않았습니다.'}
            value['automatic_recovery_attempted'] = True
            if recovering:
                value['recovery_unit'] = 'ala-screen-swap-recover.service'
                value['recovery_invocation'] = os.environ.get('INVOCATION_ID')
            if value['decision'] == 'commit':
                store.save(value)
                _materialize(store, value, True)
                value.update(state='completed', phase='completed', ended_at=_now(), reason='두 화면 교환을 확정했습니다.')
                store.save(value)
                return _public(value)
            value.update(state='running', phase='rollback', decision='rollback')
            store.save(value)
            if not value.get('lifecycle_started'):
                _materialize(store, value, False)
                value.update(state='rolled_back', phase='rolled_back', ended_at=_now(),
                    reason='화면 제어 전에 교환을 취소했습니다. 기존 실행을 유지했습니다.',
                    results={target: {'state': 'kept'} for target in value['targets']})
                store.save(value)
                return _public(value)
            if value['boot_id'] != store.runtime.boot_id():
                # Processes from the old boot cannot still be alive. Repair the
                # selections before login; do not require a not-yet-created GUI.
                _materialize(store, value, False)
                value.update(state='rolled_back', phase='rolled_back', ended_at=_now(),
                    reason='부팅 전 교환을 취소하고 원래 배정을 선택했습니다. 실제 표시는 다음 로그인에서 확인하세요.',
                    results={target: {'state': 'selected_unverified'} for target in value['targets']})
                store.save(value)
                return _public(value)
        deadline = clock() + 90
        errors = []
        # Both candidate windows must be gone before either old assignment starts.
        for target, part in value['participants'].items():
            try:
                # Recovery's selected view is before; explicit pins must use the
                # actual selection that was launched, not a guessed live revision.
                stop(target, part['before'], deadline, recovery=True)
                value['results'][target] = {'state': 'stopped'}
            except (OSError, ValueError, RuntimeError) as exc:
                errors.append(str(exc))
                value['results'][target] = {'state': 'unknown', 'reason': str(exc)}
        if errors:
            with change_lock(store.paths, blocking=True):
                value = store.load(operation_id) | {'results': value['results'], 'state': 'recovery_required',
                    'phase': 'rollback', 'reason': '교환 중지·복구 확인 필요', 'next_action': '각 DID의 로그와 연결을 확인하고 복구를 다시 요청하세요.'}
                store.save(value)
            return _public(value)
        try:
            with change_lock(store.paths, blocking=True):
                value = store.load(operation_id)
                _materialize(store, value, False)
            for target, part in value['participants'].items():
                current = store.load(operation_id)
                if part['was_running'] and target not in current.get('stop_requests', []) and not store.runtime.is_stopped(store.runtime.load(target)):
                    invoke(target, 'start', part['before'], deadline, recovery=True)
                value['results'][target] = {'state': 'restored' if part['was_running'] and target not in current.get('stop_requests', []) else 'stopped'}
            ready_all(deadline, before=True, recovery=True)
        except (OSError, ValueError, RuntimeError) as exc:
            with change_lock(store.paths, blocking=True):
                latest = store.load(operation_id)
                latest.update(state='recovery_required', phase='rollback', results=value['results'],
                              reason='원래 배정의 실제 복구를 확인하지 못했습니다.', next_action=str(exc))
                store.save(latest)
            return _public(latest)
        with change_lock(store.paths, blocking=True):
            latest = store.load(operation_id)
            latest.update(state='rolled_back', phase='rolled_back', ended_at=_now(), results=value['results'],
                          reason='교환을 완료하지 못해 원래 배정을 복구했습니다.', next_action='두 DID의 출력과 로그를 확인하세요.')
            store.save(latest)
        return _public(latest)

    if recovering or value['decision'] is not None or value['boot_id'] != store.runtime.boot_id() or value['phase'] != 'prepared':
        return recover()
    try:
        _phase(store, value, 'preflight')
        deadline = clock() + 90
        before = Config.from_dict(value['before_config'])
        if _inventory(before, store) != value['topology']:
            raise RuntimeError('출력 연결이 변경되어 교환하지 않았습니다.')
        # Pure preflight uses the complete after topology (including a stopped
        # partner), never one partner's old committed output.
        for target, part in value['participants'].items():
            if part['was_running']:
                response = invoke(target, 'preflight', part['after'], deadline)
                if response.get('exit_code', 1):
                    raise RuntimeError(response.get('reason') or '후보 화면 사전 확인에 실패했습니다.')
        _phase(store, value, 'stopping')
        for target, part in value['participants'].items():
            stop(target, part['before'], deadline)
        with change_lock(store.paths, blocking=True):
            value = store.load(operation_id)
            value['trial_started'] = True
            store.save(value)
        _phase(store, value, 'trial')
        for target, part in value['participants'].items():
            if part['was_running']:
                response = invoke(target, 'start', part['after'], deadline)
                if response.get('exit_code', 1) not in {0, 2}:
                    raise RuntimeError('후보 화면을 시작하지 못했습니다.')
        ready_all(deadline)
        _phase(store, value, 'observing')
        ready_all(clock() + 60, stable=True)
        with change_lock(store.paths, blocking=True):
            latest = store.load(operation_id)
            if not _valid(store, latest) or _config(store).to_dict() != latest['before_config']:
                raise RuntimeError('교환 중 구성이 변경됐습니다.')
            if _inventory(before, store) != latest['topology']:
                raise RuntimeError('교환 검증 중 출력 배치가 변경됐습니다.')
            latest.update(phase='committing', decision='commit')
            store.save(latest)  # Atomic durable decision; recovery must now redo both.
            _materialize(store, latest, True)
            latest.update(state='completed', phase='completed', ended_at=_now(), reason='두 화면 교환을 완료했습니다.',
                          next_action='실제 두 DID 화면을 확인하세요.',
                          results={target: {'state': 'applied' if part['was_running'] else 'stopped'}
                                   for target, part in latest['participants'].items()})
            store.save(latest)
        return _public(latest)
    except (OSError, ValueError, RuntimeError) as exc:
        with change_lock(store.paths, blocking=True):
            value = store.load(operation_id)
            value['reason'] = str(exc)
            store.save(value)
        return recover()


def run_worker(operation_id, *, paths=None, host=None):
    try:
        result = _execute(operation_id, paths=paths, host=host)
    except (OSError, ValueError, RuntimeError) as exc:
        _recovery_failure(Store(paths, host), operation_id, exc)
        return 1
    return 0 if result['state'] in TERMINAL else 1


def _recovery_failure(store, operation_id, exc):
    with change_lock(store.paths, blocking=True):
        value = store.load(operation_id)
        if value['state'] not in TERMINAL:
            value.update(state='recovery_required', automatic_recovery_attempted=True,
                         reason='두 화면 교환 복구를 완료하지 못했습니다.', next_action=str(exc))
            store.save(value)


def recover_all(*, paths=None, host=None):
    store = Store(paths, host)
    failed = False
    for value in store.pending():
        # An observed recovery failure needs an explicit retry. A dead process
        # in the middle of undo/redo has no such result and must be resumed.
        if value['state'] == 'recovery_required' and value.get('automatic_recovery_attempted'):
            continue
        recovery_owner = (value.get('recovery_unit') == 'ala-screen-swap-recover.service'
                          and value['phase'] in {'rollback', 'committing'})
        owner = store.owner_state(value)
        if owner == 'unknown':
            failed = True
            continue
        if owner == 'active':
            if recovery_owner:
                continue
            expired = value['boot_id'] != store.runtime.boot_id() or store.runtime.monotonic() >= value['expires_monotonic']
            if not expired:
                continue
            # A timed-out live worker must be stopped before recovery can own
            # the same pair. Never wait for it while holding the common lock.
            with change_lock(store.paths, blocking=True):
                current = store.load(value['operation_id'])
                if current['decision'] is None:
                    current['decision'] = 'rollback'
                current['cancel_requested'] = True
                store.save(current)
            stopped = store.host.run(['systemctl', 'stop', value['unit']], timeout=35)
            if stopped.returncode or store.owner_state(value) != 'inactive':
                failed = True
                continue
        try:
            result = _execute(value['operation_id'], paths=store.paths, host=store.host, recovering=True)
        except (OSError, ValueError, RuntimeError) as exc:
            _recovery_failure(store, value['operation_id'], exc)
            failed = True
            continue
        failed |= result['state'] not in TERMINAL
    return 1 if failed else 0


def retry(operation_id, *, release=None, paths=None, host=None):
    store = Store(paths, host)
    with change_lock(store.paths):
        value = store.load(operation_id)
        if value['state'] in TERMINAL:
            return _public(value)
        owner = store.owner_state(value)
        if owner == 'unknown':
            raise RuntimeError('기존 교환 작업의 종료 여부를 확인하지 못했습니다. 상태를 다시 조회하세요.')
        if owner == 'active':
            return _public(value)
        pinned, _ = store.runtime.release(release or value['release'])
        value.update(state='queued', phase='recovery_requested')
        if value['decision'] is None:
            value['decision'] = 'rollback'
        value.pop('recovery_unit', None)
        value.pop('recovery_invocation', None)
        store.save(value)
        response = store.host.run(['systemd-run', '--unit=' + value['unit'], '--collect',
            '--expand-environment=no', '--property=Type=exec',
            str(pinned / '.venv/bin/python'), '-I', str(pinned / 'entry.py'), '_swap-worker', operation_id], timeout=30)
        if response.returncode:
            raise RuntimeError('화면 교환 복구 서비스를 시작하지 못했습니다.')
    return _public(value) | {'state': 'queued', 'exit_code': 2}


def register_recovery(*, release=None, paths=None, host=None):
    store = Store(paths, host)
    pinned, _ = store.runtime.release(release or store.paths.at('/opt/ala/current'))
    command = ' '.join('"' + str(item).replace('"', '\\"') + '"' for item in
                       (pinned / '.venv/bin/python', '-I', pinned / 'entry.py', '_swap-recover'))
    units = {'ala-screen-swap-recover.service': f'''{MARKER}
[Unit]
Description=Recover interrupted ALA screen exchanges
Before=display-manager.service
[Service]
Type=oneshot
ExecStart={command}
[Install]
WantedBy=graphical.target
''', 'ala-screen-swap-recover.timer': f'''{MARKER}
[Unit]
Description=Check interrupted ALA screen exchanges
[Timer]
OnBootSec=3
OnUnitActiveSec=15
Unit=ala-screen-swap-recover.service
[Install]
WantedBy=timers.target
'''}
    for name, content in units.items():
        path = store.paths.at('/etc/systemd/system/' + name)
        store.runtime._safe(path)
        if path.exists() and (not path.is_file() or MARKER not in path.read_text()):
            raise RuntimeError('관리되지 않은 화면 교환 복구 서비스를 덮어쓰지 않습니다.')
        atomic_write(path, content, 0o644)
    if store.host.run(['systemctl', 'daemon-reload']).returncode:
        raise RuntimeError('화면 교환 복구 서비스 등록을 확인하지 못했습니다.')
    if store.host.run(['systemctl', 'enable', 'ala-screen-swap-recover.service']).returncode:
        raise RuntimeError('로그인 전 화면 교환 복구 등록을 확인하지 못했습니다.')
    if store.host.run(['systemctl', 'enable', '--now', 'ala-screen-swap-recover.timer']).returncode:
        raise RuntimeError('화면 교환 복구 timer를 활성화하지 못했습니다.')
    return {'state': 'ready', 'exit_code': 0}
