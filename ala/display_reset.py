"""Explicit DID reset transaction. Public fences outlive leases and boots.

Private journals contain backups; user-readable fences contain no site data.
Reads never repair state. Only the fixed privileged worker may resume a reset.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from .config import Config, ScreenConfig
from .jobs import JobStore, change_lock, protected_directory
from .runtime import RuntimeStore, code_ready, target_id
from .system import Host, Paths, atomic_json, atomic_write

TERMINAL = {'completed', 'rolled_back'}
MARKER = '# ala managed DID reset recovery v1'
_CONTEXT = ContextVar('ala_reset_operation', default=None)
ID = re.compile(r'[0-9a-f]{32}\Z')


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


class Store:
    def __init__(self, paths=None, host=None):
        self.paths, self.host = paths or Paths(), host or Host()
        self.runtime = RuntimeStore(self.paths, self.host)
        self.base = self.paths.at('/var/lib/ala/display-resets')
        self.fence = self.runtime.base / 'display-reset.json'

    def path(self, operation_id):
        if not isinstance(operation_id, str) or not ID.fullmatch(operation_id):
            raise ValueError('유효한 DID 초기화 작업 ID가 필요합니다.')
        return self.base / (operation_id + '.json')

    def save(self, value):
        path = self.path(value['operation_id'])
        self.runtime._safe(path)
        protected_directory(self.base)
        os.chmod(self.base, 0o700)
        if len((json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode())>1024*1024:
            raise ValueError('초기화 기록의 크기 제한을 초과했습니다.')
        atomic_json(path, value, 0o600)

    def load(self, operation_id):
        value = self.runtime._read(self.path(operation_id))
        if (not isinstance(value, dict) or (type(value.get('schema_version')) is not int or value['schema_version'] != 1)
                or value.get('operation_id') != operation_id
                or value.get('state') not in {'queued','running','recovery_required',*TERMINAL}
                or value.get('decision') not in {None,'commit','rollback'}):
            raise ValueError('DID 초기화 기록이 손상됐습니다.')
        backup = value.get('backup')
        if not isinstance(backup, dict) or _hash(backup) != value.get('backup_sha256'):
            raise ValueError('DID 초기화 백업 검증에 실패했습니다.')
        if (not isinstance(backup.get('states'),dict) or not 1<=len(backup['states'])<=16
                or not isinstance(backup.get('running'),dict) or set(backup['running'])!=set(backup['states'])
                or any(type(v) is not bool for v in backup['running'].values())
                or not isinstance(value.get('after'),dict) or type(value.get('lifecycle_started')) is not bool
                or not isinstance(value.get('stop_requests'),list) or not isinstance(value.get('targets'),list)
                or not isinstance(value.get('boot_id'),str) or not value['boot_id']
                or value.get('unit')!='ala-display-reset-'+operation_id+'.service'
                or value.get('owner_unit') not in {None,'ala-display-reset-recover.service'}):
            raise ValueError('DID 초기화 실행·백업 구조가 올바르지 않습니다.')
        before=Config.from_dict(backup['config'])
        after=Config.from_dict(value['candidate'])
        expected=set(backup['states'])|{t.id for t in after.screen_targets()}
        if (len(set(value['targets']))!=len(value['targets']) or set(value['targets'])!=expected
                or set(value['stop_requests'])-expected or set(backup['states'])!={t.id for t in before.screen_targets()}
                or (value['lifecycle_started'] and set(value['after'])!={t.id for t in after.screen_targets()})
                or (not value['lifecycle_started'] and value['after'])):
            raise ValueError('DID 초기화 대상·예약 구성이 일치하지 않습니다.')
        for target, state in backup['states'].items():
            target_id(target)
            for name in ('committed','desired'):
                self.runtime._validate_selection(state[name])
        for target, selections in value.get('after', {}).items():
            target_id(target)
            if not isinstance(selections,dict) or set(selections)!={'committed','desired'}:
                raise ValueError('DID 초기화 후보 선택이 올바르지 않습니다.')
            for selection in selections.values():
                self.runtime._validate_selection(selection)
        return value

    def records(self):
        self.runtime._safe(self.base)
        if not self.base.exists():
            return []
        records = []
        for path in sorted(self.base.iterdir()):
            if path.suffix != '.json' or not ID.fullmatch(path.stem):
                raise ValueError('알 수 없는 초기화 기록이 있어 변경을 보류했습니다.')
            records.append(self.load(path.stem))
        return sorted(records,key=lambda v:v.get('created_ns',0))

    def pending(self):
        return [v for v in self.records() if v['state'] not in TERMINAL]

    def publish(self, value, *, starting=False, terminal=False):
        old = read_fence(self.paths) or {'retired': []}
        before = set(value['backup']['states'])
        after = {s.id for s in Config.from_dict(value['candidate']).screen_targets()}
        retired = set(old['retired'])
        if value['decision'] == 'commit':
            retired = (retired | before) - after
        self.runtime._write(self.fence, {
            'schema_version':1, 'operation_id':value['operation_id'],
            'pending':not terminal, 'starting':starting,
            'retired':sorted(retired), 'targets':sorted(before | after),
            'revision':_hash(value['candidate'] if value['decision']=='commit' else value['backup']['config']),
        })


def read_fence(paths=None):
    store = RuntimeStore(paths or Paths())
    path = store.base / 'display-reset.json'
    if not path.exists() and not path.is_symlink():
        return None
    value = store._read(path)
    if (not isinstance(value,dict) or (type(value.get('schema_version')) is not int or value['schema_version']!=1)
            or type(value.get('pending')) is not bool or type(value.get('starting')) is not bool
            or not isinstance(value.get('operation_id'),str) or not ID.fullmatch(value['operation_id'])
            or not isinstance(value.get('revision'),str) or not re.fullmatch('[0-9a-f]{64}',value['revision'])):
        raise ValueError('DID 초기화 보호 기록을 확인할 수 없습니다.')
    for key in ('targets','retired'):
        if not isinstance(value.get(key),list) or len(value[key])>256:
            raise ValueError('DID 초기화 대상 기록을 확인할 수 없습니다.')
        for target in value[key]:
            target_id(target)
    return value


def user_guard(target, *, paths=None, observation=False):
    paths = paths or Paths()
    fence = read_fence(paths)
    if not fence or observation:
        return
    if target in fence['retired']:
        raise RuntimeError('초기화에서 제외된 DID는 시작하지 않습니다.')
    if fence['pending']:
        if not fence['starting']:
            raise RuntimeError('DID 전체 초기화 복구·확정을 기다립니다.')
        model = Config.from_dict(RuntimeStore(paths)._read(paths.config))
        if _hash(model.to_dict()) != fence['revision'] or target not in {s.id for s in model.screen_targets()}:
            raise RuntimeError('초기화의 일관된 화면 구성을 확인하지 못했습니다.')


def guard(*, paths=None, host=None):
    store = Store(paths, host)
    if os.geteuid() != store.runtime.owner:
        fence=read_fence(store.paths)
        if fence and fence['pending'] and not fence['starting']:
            raise RuntimeError('DID 초기화 복구를 기다립니다.')
        return
    for value in store.pending():
        if _CONTEXT.get() != value['operation_id']:
            raise RuntimeError('DID 전체 초기화 또는 복구 중입니다. 초기화 상태를 확인하세요.')


def note_stop(target, *, paths=None, host=None):
    store = Store(paths, host)
    for value in store.pending():
        if target not in value['targets']:
            continue
        if target not in value['stop_requests']:
            value['stop_requests'].append(target)
        store.save(value)


@contextmanager
def _scope(operation_id):
    token = _CONTEXT.set(operation_id)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def blank_candidate(config, inventory):
    """Same canonical output/ID algorithm as first-install onboarding."""
    from .display_onboarding import _candidate
    seed = replace(config, schema_version=2, url=None, autostart=True,
                   screens=(ScreenConfig(id='default'),))
    result = _candidate(seed, inventory)
    if result['state'] != 'ready':
        raise RuntimeError(result['reason'])
    return Config.from_dict(result['config'])


def _capable(store, selection):
    release, _ = store.runtime.release(selection['release'])
    metadata = store.runtime._read(release/'COMPATIBILITY.json')
    if any(type(metadata.get(key)) is not int or metadata[key]!=1 for key in ('setup_page_protocol','workspace_protocol','display_reset_protocol')):
        raise RuntimeError('DID 전체 초기화를 지원하는 화면 코드를 먼저 적용하세요.')


def _observe(config, store, release):
    from .screens import _inventory, inspect_session, screen_action
    from .screen_swap import guard as swap_guard, topology_snapshot
    swap_guard(paths=store.paths,host=store.host)
    if not config.display or inspect_session(config,store.host)!='ok':
        raise RuntimeError('단일 활성·잠금 해제 GNOME 화면 세션이 필요합니다.')
    if any(v.get('state') in {'queued','running'} for v in JobStore(store.paths,store.host).list()):
        raise RuntimeError('설치 작업 완료 후 초기화하세요.')
    from .update import status as update_status
    if update_status(paths=store.paths,host=store.host).get('state') in {'queued','running','unknown'}:
        raise RuntimeError('CLI 갱신 상태를 먼저 확인하세요.')
    from .display_onboarding import status as initial_status
    if initial_status(paths=store.paths,host=store.host).get('state')=='pending':
        raise RuntimeError('초기 안내 준비 작업을 먼저 완료하세요.')
    inventory = _inventory(config,store.host,store.paths)
    if inventory.get('state')!='ok' or 'workspace-placement' not in inventory.get('capabilities',[]):
        raise RuntimeError('화면 확장과 현재 출력 연결을 먼저 확인하세요.')
    candidate = blank_candidate(config,inventory)
    states, running = {}, {}
    for screen in config.screen_targets():
        from .screen_control import read_status as reload_status
        if reload_status(screen.id,paths=store.paths,host=store.host).get('state') in {'queued','running','unknown'}:
            raise RuntimeError('화면 새로고침 작업을 먼저 확인하세요.')
        state = store.runtime.load(screen.id)
        if state is None or store.runtime.active(state) or state.get('operation'):
            raise RuntimeError('모든 기존 DID의 화면 코드 등록·진행 상태를 먼저 확인하세요.')
        for key in ('committed','desired'):
            _capable(store,state[key])
        report = screen_action('status',config,target=screen.id,host=store.host,paths=store.paths)
        service = report.get('service') or {}
        if service.get('QueryError') or service.get('ActiveState') not in {'active','inactive'}:
            raise RuntimeError('기존 DID 서비스 실행 상태를 확인하지 못했습니다.')
        if service['ActiveState']=='active':
            from .runtime import _observed_release_matches
            if not _observed_release_matches(store.runtime,report,state['committed']['version'],screen.id,config):
                raise RuntimeError('관리 Chrome의 실행 코드·소유권을 확인하지 못했습니다.')
        states[screen.id],running[screen.id]=state,service['ActiveState']=='active'
    for screen in candidate.screen_targets():
        if screen.id not in states and store.runtime.load(screen.id) is not None:
            raise RuntimeError('자동 생성 ID와 보존된 런타임 기록이 겹칩니다. 기존 기록을 먼저 확인하세요.')
    pinned,version=store.runtime.release(release)
    if {s.id for s in candidate.screen_targets()}-states.keys():
        _capable(store,{'release':str(pinned)})
    # Orphan runtime selections must not be silently discarded or reactivated.
    fence=read_fence(store.paths) or {'retired':[]}
    if {s['target_id'] for s in store.runtime.selections()}-states.keys()-set(fence['retired']):
        raise RuntimeError('구성에 없는 기존 런타임을 먼저 확인하세요.')
    if len((set(fence['retired']) | states.keys()) - {t.id for t in candidate.screen_targets()})>256:
        raise RuntimeError('보존된 제외 대상 기록의 수를 먼저 확인하세요.')
    snapshot={'config':config.to_dict(),'states':states,'running':running,
              'topology':topology_snapshot(inventory),'release':str(pinned),'version':version,
              'registrations':registration_snapshot(config,candidate,store)}
    return snapshot,candidate


def preview(config, *, release, paths=None, host=None):
    store=Store(paths,host)
    try:
        snapshot,candidate=_observe(config,store,release)
        targets={s.id:s.to_dict() for s in candidate.screen_targets()}
        return {'state':'ready','review':{'fingerprint':_hash(snapshot),'expires_at':time.time()+300},
                'before':[s.to_dict() for s in config.screen_targets()], 'after':list(targets.values()),
                'removed':sorted(snapshot['states'].keys()-targets.keys()),
                'manual_stops':[t for t,s in snapshot['states'].items() if store.runtime.is_stopped(s)],
                'reason':'DID 전체 설정을 백업한 뒤 모니터별 안내 화면으로 초기화합니다.'}
    except RuntimeError as exc:
        return {'state':'blocked','reason':str(exc),'next_action':'해당 전제를 확인한 뒤 초기화를 다시 검토하세요.'}
    except (OSError,ValueError,KeyError,TypeError):
        return {'state':'blocked','reason':'초기화 전제 확인 필요 · 화면 코드·세션·출력·진행 작업을 확인하세요.',
                'next_action':'화면 코드 적용과 진행 작업 완료 후 다시 검토하세요.'}


def _public(value):
    return {k:deepcopy(value[k]) for k in ('operation_id','state','phase','decision','targets','results','reason','next_action','current_target','execution_verified','schedule_warning') if k in value} | {'display_verified':False}


def status(*, paths=None, host=None, operation_id=None):
    store=Store(paths,host)
    values=[store.load(operation_id)] if operation_id is not None else store.records()
    if not values:
        return {'state':'idle'}
    value=next((v for v in reversed(values) if v['state'] not in TERMINAL),values[-1])
    result=_public(value)
    if value['state'] not in TERMINAL:
        owner=_owner(store,value)
        if owner=='inactive' and value['phase']!='waiting_session':
            result.update(state='recovery_required',reason='초기화 작업이 중단됐습니다. 복구를 요청하세요.')
        elif owner=='unknown':
            result.update(state='unknown',reason='초기화 서비스의 실행 여부를 확인하지 못했습니다.')
    return result


def _owner(store,value):
    if value.get('owner_boot_id',value['boot_id'])!=store.runtime.boot_id():
        return 'inactive'
    result=store.host.run(['systemctl','show',value.get('owner_unit',value['unit']),'--property=ActiveState','--value'],timeout=5)
    if result.returncode or result.stdout.strip() not in {'active','activating','reloading','deactivating','inactive','failed'}:
        return 'unknown'
    return 'inactive' if result.stdout.strip() in {'inactive','failed'} else 'active'


def _register(store,release):
    from .display import _quote
    command=' '.join(_quote(v) for v in (release/'.venv/bin/python','-I',release/'entry.py','_reset-recover'))
    units={'ala-display-reset-recover.service':f'''{MARKER}
[Unit]
Description=Recover interrupted ALA DID resets
Before=display-manager.service
[Service]
Type=oneshot
ExecStart={command}
UMask=0077
TimeoutStartSec=3600
[Install]
WantedBy=graphical.target
''', 'ala-display-reset-recover.timer':f'''{MARKER}
[Unit]
Description=Resume ALA DID reset after graphical login
[Timer]
OnBootSec=3
OnUnitInactiveSec=15
Unit=ala-display-reset-recover.service
[Install]
WantedBy=timers.target
'''}
    for name,content in units.items():
        path=store.paths.at('/etc/systemd/system/'+name)
        store.runtime._safe(path)
        if path.exists() and (not path.is_file() or MARKER not in path.read_text()):
            raise RuntimeError('관리되지 않은 복구 서비스를 변경하지 않습니다.')
        atomic_write(path,content)
    for command in (['systemctl','daemon-reload'],['systemctl','enable','ala-display-reset-recover.service'],
                    ['systemctl','enable','--now','ala-display-reset-recover.timer']):
        if store.host.run(command).returncode:
            raise RuntimeError('초기화 부팅 복구 등록을 확인하지 못했습니다.')


def submit(review, *, release, paths=None, host=None):
    store=Store(paths,host)
    if store.paths.root!=Path('/') and type(store.host) is Host:
        raise RuntimeError('대체 경로에서 실제 서비스 변경을 허용하지 않습니다.')
    with change_lock(store.paths):
        guard(paths=store.paths,host=store.host)
        if (not isinstance(review,dict) or type(review.get('expires_at')) not in (float,int)
                or not time.time()<=review['expires_at']<=time.time()+301):
            raise ValueError('초기화 검토가 만료됐습니다. 다시 확인하세요.')
        config=Config.from_dict(store.runtime._read(store.paths.config))
        snapshot,candidate=_observe(config,store,release)
        if review.get('fingerprint')!=_hash(snapshot):
            raise RuntimeError('검토 후 구성·출력·실행 상태가 변경됐습니다. 다시 확인하세요.')
        operation_id=uuid.uuid4().hex
        value={'schema_version':1,'operation_id':operation_id,'state':'queued','phase':'prepared','decision':None,
               'created_ns':time.time_ns(),'boot_id':store.runtime.boot_id(),'unit':'ala-display-reset-'+operation_id+'.service',
               'backup':snapshot,'backup_sha256':_hash(snapshot),'candidate':candidate.to_dict(),
               'targets':sorted(set(snapshot['states'])|{s.id for s in candidate.screen_targets()}),
               'after':{},'stop_requests':[],'results':{},'lifecycle_started':False,'execution_verified':False}
        # Immutable backup is read back before any process/registration change.
        store.save(value)
        value=store.load(operation_id)
        store.publish(value)
        try:
            _register(store,Path(snapshot['release']))
            result=store.host.run(['systemd-run','--unit='+value['unit'],'--collect','--expand-environment=no',
                '--property=Type=exec','--property=UMask=0077','--property=TimeoutStartSec=3600',
                str(Path(snapshot['release'])/'.venv/bin/python'),'-I',str(Path(snapshot['release'])/'entry.py'),
                '_reset-worker',operation_id],timeout=30)
            if result.returncode:
                raise RuntimeError('초기화 실행기 접수를 확인하지 못했습니다.')
        except (OSError,ValueError,RuntimeError):
            value.update(state='recovery_required',reason='초기화 접수 확인 불가 · 복구를 요청하세요.')
            store.save(value)
        return _public(value)


def _control(store,target,action,selection,*,timeout=5):
    from .screen_swap import _control as pinned_control
    return pinned_control(store,target,action,selection,timeout=timeout)


def _check_config(store,value):
    current=store.runtime._read(store.paths.config)
    if current not in (value['backup']['config'],value['candidate']):
        raise RuntimeError('초기화 이후의 다른 구성을 덮어쓰지 않습니다.')


def _materialize(store,value,after):
    """Redo either decision without rewinding a generation or a newer stop."""
    _check_config(store,value)
    model=Config.from_dict(value['candidate'] if after else value['backup']['config'])
    old=value['backup']['states']
    # Validate every target before the first write; partial writes remain fenced.
    for target in value['targets']:
        state=store.runtime.load(target)
        original=old.get(target)
        candidate=value['after'].get(target)
        if state is None:
            if original is not None:
                raise RuntimeError('복구할 기존 런타임 기록이 없습니다.')
            continue
        for key in ('committed','desired'):
            allowed=[s[key] for s in (original,candidate) if s is not None]
            if state[key] not in allowed:
                raise RuntimeError('다른 작업이 선택한 화면 코드를 덮어쓰지 않습니다.')
        if state.get('operation'):
            raise RuntimeError('다른 화면 작업과 겹쳐 복구를 보류했습니다.')
    for target in value['targets']:
        original=old.get(target)
        selected=value['after'].get(target) if after else original
        if selected is None:
            # Keep old/new tombstones; public retired fence prevents starts.
            continue
        current=store.runtime.load(target)
        state=deepcopy(current or original or {'schema_version':1,'target_id':target,'generation':0,'operation':None})
        state.update(committed=deepcopy(selected['committed']),desired=deepcopy(selected['desired']),operation=None)
        state['generation']+=1
        stopped=target in value['stop_requests'] or (not after and original['stopped'])
        state.update(stopped=stopped,stop_boot_id=store.runtime.boot_id() if stopped else None,
                     stop_session_id=None)
        store.runtime._write(store.runtime.state_path(target),state)
    atomic_json(store.paths.config,model.to_dict(),0o644)
    if after:
        _registration(store,model,reloading=value['boot_id']==store.runtime.boot_id())
    # Before commit no registration was changed, so rollback preserves its exact bytes.


def _registration(store,model,*,reloading=True):
    from . import display
    from .runtime import _sync_autostart
    for screen in model.screen_targets():
        path=store.paths.at('/etc/systemd/user/'+display.unit_name(screen.id))
        store.runtime._safe(path)
        if path.exists() and (not path.is_file() or display.UNIT_MARKER not in path.read_text()):
            raise RuntimeError('관리되지 않은 Chrome unit을 변경하지 않습니다.')
        display._write_managed(path,display.unit_text(screen.id),store.paths.backups)
    if reloading and store.paths.root==Path('/'):
        result=display._user_call(model,store.host,store.paths,'reload-units')
        if result.get('exit_code')!=0:
            raise RuntimeError('사용자 unit 재조회가 필요합니다.')
    _sync_autostart(model,store.paths,store.host)


def _finish(store,value,state,reason):
    # Terminal private record last: interrupted fence publication stays recoverable.
    value.update(phase=state,reason=reason)
    store.publish(value,terminal=True)
    value['state']=state
    store.save(value)
    if not _disable_retry(store):
        value['schedule_warning']='초기화 배정은 확정됐지만 복구 타이머 해제를 확인하세요.'
        store.save(value)
    return _public(value)


def _checkpoint(store,operation_id,phase,*,check_stop=True):
    with change_lock(store.paths,blocking=True):
        value=store.load(operation_id)
        if check_stop and value['stop_requests'] and value['decision'] is None:
            raise RuntimeError('초기화 중 수동 중지 요청으로 취소했습니다.')
        value.update(state='running',phase=phase)
        store.save(value)
        return value


def _stop_all(store,value,control,clock,pause):
    deadline=clock()+90
    for target,state in value['backup']['states'].items():
        with change_lock(store.paths,blocking=True):
            latest=store.load(value['operation_id'])
            latest['current_target']=target
            latest['results'][target]={'state':'stopping'}
            store.save(latest)
        while clock()<deadline:
            response=control(store,target,'stop',state['committed'],timeout=min(5,deadline-clock()))
            if response.get('exit_code')==0 and response.get('state')=='stopped':
                break
            if response.get('exit_code',1) not in {0,2}:
                raise RuntimeError('관리 Chrome 종료 확인 실패')
            pause(min(1,max(0,deadline-clock())))
        else:
            raise RuntimeError('관리 Chrome 종료 확인 시간 초과')


def _ready(store,value,selections,targets,control,clock,pause,*,stable=False):
    deadline=clock()+(60 if stable else 90)
    while clock()<deadline:
        latest=store.load(value['operation_id'])
        ready=True
        for target in targets:
            if target in latest['stop_requests']:
                continue
            selection=selections[target]['committed']
            report=control(store,target,'check',selection,timeout=min(5,max(.1,deadline-clock())))
            model=Config.from_dict(store.runtime._validate_selection(selection)).for_screen(target)
            ready &= code_ready(report,selection['version'],selection['revision'],setup=model.url is None)
        if ready and not stable:
            return
        if not ready and stable:
            raise RuntimeError('안내 화면 안정 관측 실패')
        pause(min(1,max(0,deadline-clock())))
    if not stable:
        raise RuntimeError('화면 실행·문서·배치 후조건 확인 시간 초과')


def _start(store,value,selections,targets,control,clock,pause):
    # The shared lock serializes the short start request against manual stop.
    for target in targets:
        with change_lock(store.paths,blocking=True):
            latest=store.load(value['operation_id'])
            if target in latest['stop_requests']:
                continue
            latest['results'][target]={'state':'starting'}
            latest['current_target']=target
            store.save(latest)
            response=control(store,target,'start',selections[target]['committed'],timeout=5)
            if response.get('exit_code',1) not in {0,2}:
                raise RuntimeError('안내 화면 시작 요청 실패')
    _ready(store,value,selections,targets,control,clock,pause)
    _ready(store,value,selections,targets,control,clock,pause,stable=True)


def execute(operation_id,*,paths=None,host=None,recovering=False,control=None,monotonic=None,sleep=None):
    store=Store(paths,host)
    control,clock,pause=control or _control,monotonic or time.monotonic,sleep or time.sleep
    with _scope(operation_id):
        with change_lock(store.paths,blocking=True):
            value=store.load(operation_id)
            if value['state'] in TERMINAL:
                return _public(value)
            if recovering:
                if _owner(store,value)!='inactive':
                    raise RuntimeError('기존 초기화 실행기 종료를 확인하지 못했습니다.')
                value.update(owner_unit='ala-display-reset-recover.service',owner_boot_id=store.runtime.boot_id(),automatic_recovery_attempted=True)
                store.save(value)
            elif value['state']!='queued' or value['boot_id']!=store.runtime.boot_id():
                raise RuntimeError('신규 실행 대신 초기화 복구를 요청하세요.')
            else:
                value.update(state='running',phase='claimed')
                store.save(value)
            store.publish(value)
        try:
            if not recovering:
                value=_checkpoint(store,operation_id,'preparing')
                from .screens import _inventory,inspect_session
                from .screen_swap import topology_snapshot
                model=Config.from_dict(value['backup']['config'])
                if (inspect_session(model,store.host)!='ok' or
                        topology_snapshot(_inventory(model,store.host,store.paths))!=value['backup']['topology']):
                    raise RuntimeError('검토 후 화면 세션·출력이 변경됐습니다.')
                with change_lock(store.paths,blocking=True):
                    value=store.load(operation_id)
                    _check_config(store,value)
                    for target,state in value['backup']['states'].items():
                        if store.runtime.load(target)!=state:
                            raise RuntimeError('검토 후 런타임 상태가 변경됐습니다.')
                    candidate=Config.from_dict(value['candidate'])
                    if registration_snapshot(model,candidate,store)!=value['backup']['registrations']:
                        raise RuntimeError('검토 후 자동 시작 등록이 변경됐습니다.')
                    for screen in candidate.screen_targets():
                        old=value['backup']['states'].get(screen.id)
                        value['after'][screen.id]={key:store.runtime.selection(
                            old[key]['release'] if old else value['backup']['release'],candidate)
                            for key in ('committed','desired')}
                    value['lifecycle_started']=True
                    value['phase']='stopping'
                    store.save(value)
                _stop_all(store,value,control,clock,pause)
                value=_checkpoint(store,operation_id,'committing')
                with change_lock(store.paths,blocking=True):
                    value=store.load(operation_id)
                    if value['stop_requests']:
                        raise RuntimeError('수동 중지로 초기화를 취소했습니다.')
                    _check_config(store,value)
                    from .screen_swap import topology_snapshot
                    if topology_snapshot(_inventory(model,store.host,store.paths))!=value['backup']['topology']:
                        raise RuntimeError('종료 확인 중 출력 연결이 변경됐습니다.')
                    if registration_snapshot(model,candidate,store)!=value['backup']['registrations']:
                        raise RuntimeError('종료 확인 중 자동 시작 등록이 변경됐습니다.')
                    for target,before in value['backup']['states'].items():
                        if store.runtime.load(target)!=before:
                            raise RuntimeError('종료 확인 중 런타임 선택이 변경됐습니다.')
                    # Durable decision precedes configuration/selection writes.
                    value['decision']='commit'
                    store.save(value)
            value=store.load(operation_id)
            after=value['decision']=='commit'
            if not after:
                return _rollback(store,value,control,clock,pause)
            with change_lock(store.paths,blocking=True):
                value=store.load(operation_id)
                _materialize(store,value,True)
                value['phase']='starting'
                store.save(value)
                store.publish(value,starting=True)
            if value['boot_id']!=store.runtime.boot_id():
                waiting=_wait_session(store,value,clock)
                if waiting is not None:
                    return waiting
            targets=list(value['after'])
            _start(store,value,value['after'],targets,control,clock,pause)
            with change_lock(store.paths,blocking=True):
                value=store.load(operation_id)
                value['execution_verified']=True
                value['results']={t:{'state':'stopped' if t in value['stop_requests'] else 'waiting_configuration'} for t in targets}
                return _finish(store,value,'completed','모니터별 안내 화면을 확인했습니다. 실제 패널에서 확인하세요.')
        except (OSError,ValueError,RuntimeError):
            value=store.load(operation_id)
            if value['decision'] is None:
                try:
                    return _rollback(store,value,control,clock,pause)
                except (OSError,ValueError,RuntimeError):
                    pass
            with change_lock(store.paths,blocking=True):
                value=store.load(operation_id)
                if value.get('current_target'):
                    value['results'][value['current_target']]={'state':'unknown'}
                value.update(state='recovery_required',automatic_recovery_attempted=True,
                    reason='초기화 실행·복구 확인 필요',next_action=(value.get('current_target','')+' · 로그·연결 상태 확인 후 초기화 작업 복구를 요청하세요.'))
                store.save(value)
                _disable_retry(store)
                return _public(value)


def _rollback(store,value,control,clock,pause):
    with change_lock(store.paths,blocking=True):
        value=store.load(value['operation_id'])
        value.update(decision='rollback',phase='rollback')
        store.save(value)
    if not value['lifecycle_started'] and value['boot_id']==store.runtime.boot_id():
        with change_lock(store.paths,blocking=True):
            return _finish(store,store.load(value['operation_id']),'rolled_back','화면 제어 전에 초기화를 취소했습니다.')
    if value['boot_id']==store.runtime.boot_id():
        _stop_all(store,value,control,clock,pause)
    with change_lock(store.paths,blocking=True):
        value=store.load(value['operation_id'])
        _materialize(store,value,False)
        store.publish(value,starting=True)
    if value['boot_id']!=store.runtime.boot_id():
        waiting=_wait_session(store,value,clock)
        if waiting is not None:
            return waiting
    targets=[t for t,running in value['backup']['running'].items() if running and t not in value['stop_requests']]
    _start(store,value,value['backup']['states'],targets,control,clock,pause)
    with change_lock(store.paths,blocking=True):
        return _finish(store,store.load(value['operation_id']),'rolled_back','초기화를 취소하고 이전 배정을 복구했습니다.')


def run_worker(operation_id):
    result=execute(operation_id)
    return 0 if result['state'] in TERMINAL or result.get('phase')=='waiting_session' else 1


def recover_all(*,paths=None,host=None):
    store=Store(paths,host)
    failed=False
    try:
        values=store.pending()
    except (OSError,ValueError,RuntimeError,KeyError,TypeError):
        _disable_retry(store)
        return 1
    if not values:
        _disable_retry(store)
        return 0
    for value in values:
        if value.get('automatic_recovery_attempted'):
            failed=True
            continue
        owner=_owner(store,value)
        if owner=='active':
            continue
        if owner=='unknown':
            failed=True
            continue
        try:
            result=execute(value['operation_id'],paths=store.paths,host=store.host,recovering=True)
            failed |= result['state'] not in TERMINAL and result.get('phase')!='waiting_session'
        except (OSError,ValueError,RuntimeError):
            failed=True
    if failed:
        _disable_retry(store)
    return 1 if failed else 0


def recover(operation_id,*,release,paths=None,host=None):
    store=Store(paths,host)
    with change_lock(store.paths):
        value=store.load(operation_id)
        if value['state'] in TERMINAL:
            return _public(value)
        if _owner(store,value)!='inactive':
            raise RuntimeError('기존 초기화 작업 종료를 확인한 뒤 복구하세요.')
        pinned,_=store.runtime.release(release)
        _register(store,pinned)
        value.pop('automatic_recovery_attempted',None)
        value.pop('owner_unit',None)
        value.pop('owner_boot_id',None)
        store.save(value)
        if store.host.run(['systemctl','start','--no-block','ala-display-reset-recover.service']).returncode:
            raise RuntimeError('초기화 복구 접수를 확인하지 못했습니다.')
        return _public(value)|{'state':'queued'}


def registration_snapshot(config,candidate,store):
    """Back up only fixed managed unit names and the display account login entry."""
    from . import display
    names={display.unit_name(s.id) for model in (config,candidate) for s in model.screen_targets()}
    files={name:store.paths.at('/etc/systemd/user/'+name) for name in names}
    if store.paths.root==Path('/'):
        uid,_,home=display._account(config,store.host)
        files['autostart']=home/'.config/autostart/ala-display.desktop'
    else:
        uid=os.geteuid()
        files['autostart']=store.paths.at('/home/user/.config/autostart/ala-display.desktop')
    result={}
    for name,path in files.items():
        display._safe_parents(path)
        if not path.exists() and not path.is_symlink():
            result[name]=None
            continue
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            info=os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {store.runtime.owner,uid}
                    or info.st_mode & 0o022 or info.st_size>65536):
                raise ValueError('자동 시작 등록의 형식·권한·크기 확인 필요')
            content=stream.read(65537)
        marker=display.DESKTOP_MARKER if name=='autostart' else display.UNIT_MARKER
        if marker not in content:
            raise ValueError('관리되지 않은 자동 시작 등록이 있어 초기화를 보류했습니다.')
        result[name]=content
    return result


def _wait_session(store,value,clock):
    """Repair the selection before GDM, then let a bounded timer resume starts."""
    from .screens import inspect_session
    model=Config.from_dict(value['candidate'] if value['decision']=='commit' else value['backup']['config'])
    if inspect_session(model,store.host)=='ok':
        return None
    with change_lock(store.paths,blocking=True):
        latest=store.load(value['operation_id'])
        boot=store.runtime.boot_id()
        if latest.get('waiting_boot_id')!=boot:
            latest.update(waiting_boot_id=boot,session_deadline=clock()+600)
        if clock()>=latest['session_deadline']:
            latest.update(state='recovery_required',phase='waiting_session_expired',
                          automatic_recovery_attempted=True,reason='GNOME 로그인 준비 대기 시간이 끝났습니다.',
                          next_action='GNOME 로그인 후 초기화 작업 복구를 요청하세요.')
        else:
            latest.update(state='queued',phase='waiting_session',execution_verified=False,
                          reason='일관된 배정을 복원했습니다. GNOME 로그인 준비를 기다립니다.',
                          next_action='화면 계정으로 GNOME에 로그인하세요. 준비되면 작업을 재개합니다.')
            latest.pop('automatic_recovery_attempted',None)
            latest.pop('owner_unit',None)
            latest.pop('owner_boot_id',None)
        store.save(latest)
        if latest['state']=='recovery_required':
            _disable_retry(store)
        return _public(latest)


def _disable_retry(store):
    """Stop idle/failing polling; keep the boot service and durable journal."""
    try:
        return store.host.run(['systemctl','disable','--now','ala-display-reset-recover.timer'],timeout=5).returncode==0
    except (OSError,RuntimeError):
        return False
