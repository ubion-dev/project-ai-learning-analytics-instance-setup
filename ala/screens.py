"""Per-target screen inventory and short, observable lifecycle requests.

The service owns root locks/generations. These leaf operations do not cancel a
runtime lease or acquire that lock, including calls from the nightly worker.
"""
from __future__ import annotations

from dataclasses import replace
import os

from .config import Config
from .system import Host, Paths
from . import display
from .monitors import hardware_topology, resolve_output


def _context(config,target):
    return config if getattr(config,'target_id',None)==target and hasattr(config,'_target_id') else config.for_screen(target,revision=config.config_revision)


def _setup_runtime_guard(config, host, paths):
    if config.url is not None or not config.runtime_pin:
        return None
    from pathlib import Path
    from .runtime import RuntimeStore
    release = config.runtime_pin.get('release')
    try:
        if isinstance(release, str):
            metadata = RuntimeStore(paths, host)._read(Path(release) / 'COMPATIBILITY.json')
            if metadata.get('setup_page_protocol') == 1:
                return None
    except (OSError, ValueError, RuntimeError):
        pass
    return {'state': 'pending', 'exit_code': 2,
            'reason': '선택한 화면 코드가 URL 없는 설정 안내를 지원하지 않습니다.',
            'next_action': '업데이트 관리에서 이 DID의 선택 화면 코드를 먼저 적용하세요.'}


def _applied_context(config, target, host, paths):
    """Control the selected snapshot while keeping the desired URL in the UI."""
    if config.runtime_pin:
        return _context(config, target)
    from .runtime import RuntimeStore, load_user_selection
    from .screen_swap import effective_selection
    assignment = effective_selection(target, paths=paths, host=host)
    if assignment is not None:
        store = RuntimeStore(paths, host)
        selected = assignment | {'config': store._validate_selection(assignment)}
    else:
        selected = load_user_selection(target, paths=paths, host=host)
    if selected is None:
        return _context(config, target)
    store = RuntimeStore(paths, host)
    pin = {'config_path': str(store.base / 'configs' / (selected['revision'] + '.json')),
           'revision': selected['revision'], 'release': selected['release'], 'version': selected['version']}
    model = replace(Config.from_dict(selected['config']), runtime_pin=pin)
    return model.for_screen(target, revision=selected['revision'])


def _workspace_runtime_guard(config, inventory, host, paths):
    """Check a candidate's additive capability before replacing a working DID."""
    from pathlib import Path
    from .runtime import RuntimeStore
    release=(config.runtime_pin or {}).get('release') or str(Path(__file__).resolve().parents[1])
    metadata=Path(release)/'COMPATIBILITY.json'
    if not metadata.exists():
        return None
    supported=RuntimeStore(paths,host)._read(metadata)
    if supported.get('workspace_protocol') != 1:
        return None
    if 'workspace-placement' not in inventory.get('capabilities', []):
        return {'state':'pending','exit_code':2,
                'reason':'후보 화면 코드에 필요한 작업 공간 배치 확장이 아직 로드되지 않았습니다.',
                'next_action':'간편 설정으로 확장 파일을 반영하고 GNOME 재로그인 후 화면 코드를 다시 적용하세요.'}
    return None


def _inventory(config,host,paths):
    first=config.screen_targets()[0]
    # Topology is session-wide, not a target runtime operation. Carrying a
    # second target's pin into the first target's RPC fails snapshot fencing.
    return display._user_call(_context(replace(config,runtime_pin=None),first.id),host,paths,'outputs')


def _conflict(config,target,inventory,*,host=None,paths=None,selected=None):
    selected=selected or _applied_context(config,target,host,paths)
    found=resolve_output(inventory,selected.output,auto=target=='default')
    if found['state']!='ok':return found
    # A pinned snapshot can predate other targets. Use the protected current
    # list for membership, then resolve each other target's applied settings.
    if selected.runtime_pin and paths and (paths.config.exists() or paths.config.is_symlink()):
        from .runtime import RuntimeStore
        config=Config.from_dict(RuntimeStore(paths,host)._read(paths.config))
    logical=found['output']['id']
    for other in config.screen_targets():
        if other.id==target:continue
        from .screen_swap import conflict_selection
        peer = conflict_selection(other.id, target, selected.runtime_pin, host=host, paths=paths)
        if peer is not None:
            from .runtime import RuntimeStore
            actual_other=Config.from_dict(RuntimeStore(paths,host)._validate_selection(peer)).for_screen(other.id)
        else:
            actual_other=_applied_context(replace(config,runtime_pin=None),other.id,host,paths)
        resolved=resolve_output(inventory,actual_other.output,auto=other.id=='default')
        if resolved.get('state')!='ok' or resolved['output']['id']!=logical:continue
        if selected.layout=='fullscreen' or actual_other.layout=='fullscreen' or selected.layout==actual_other.layout:
            return {'state':'blocked','reason':'복제 출력 또는 같은 논리 화면의 대상 배치가 겹칩니다.',
                    'next_action':'논리 화면당 전체화면 하나 또는 좌우 한 쌍으로 구성하세요.'}
    return found


def inspect_session(config, host):
    if not config.display:
        return 'disabled'
    try:
        uid, _, _ = display._account(config, host)
        sessions, _ = display._graphical_sessions(host, uid)
    except (OSError, ValueError, RuntimeError):
        return 'unknown'
    if sessions is None:
        return 'unknown'
    if len(sessions) > 1:
        return 'multiple'
    if not sessions or sessions[0][1].get('Active') != 'yes':
        return 'pending'
    locked = sessions[0][1].get('LockedHint')
    return {'no': 'ok', 'yes': 'locked'}.get(locked, 'lock_unknown')


def inspect_screens(config:Config,host=None,paths=None):
    host,paths=host or Host(),paths or Paths()
    hardware=hardware_topology(paths)
    try:
        inventory=_inventory(config,host,paths) if config.display else {'state':'not_applicable'}
    except (OSError,ValueError,RuntimeError):
        inventory={'state':'unknown','reason':'사용자 화면 출력 조회를 완료하지 못했습니다.',
                   'next_action':'화면 계정과 활성 GNOME 로그인 세션을 확인하세요.'}
    targets=[]
    for screen in config.screen_targets():
        try:
            context=_applied_context(config,screen.id,host,paths)
            observed=display.chrome_action('status',context,host=host,paths=paths)
            service=observed.get('service') or {}
            health=observed.get('health') or {'state':'unknown','reason':observed.get('reason','현재 감시 기록 없음')}
            if observed.get('state') in {'running','pending'} and service.get('ActiveState') in {'active','activating'}:
                placement=display._user_call(context,host,paths,'windows')
            else:
                placement={'state':'pending','reason':'관리 Chrome의 실행·창 증거를 기다립니다.','confirmed_window_count':None}
            if context.schema_version==1 and context.output is None:
                placement={'state':'not_applicable','reason':'기존 기본 화면의 배치를 유지합니다.','confirmed_window_count':None}
            targets.append({'id':screen.id,'url':screen.url,'applied_url':context.url,'actual_url':None,'output':screen.output,
                            'layout':screen.layout,'applied_output':context.output,'applied_layout':context.layout,
                            'autostart':screen.autostart,'frontend':screen.frontend.to_dict(),
                            'service':service,'health':health,'placement':placement,'state':observed.get('state','unknown'),
                            'results':observed.get('results',[]),'next_action':observed.get('next_action',''),
                            'reason':observed.get('reason','')})
        except (OSError,ValueError,RuntimeError):
            targets.append({'id':screen.id,'url':screen.url,'actual_url':None,'output':screen.output,
                            'layout':screen.layout,'autostart':screen.autostart,'frontend':screen.frontend.to_dict(),
                            'service':{},'health':{'state':'unknown'},'placement':{'state':'unknown'},
                            'state':'unknown','reason':'화면 대상의 관측을 완료하지 못했습니다.'})
    failures=any(t['state'] in {'unknown','error','blocked'} or t['placement']['state'] in {'unknown','error','blocked'} for t in targets)
    pending=inventory.get('state') not in {'ok','not_applicable'} or any(t['state'] not in {'running','stopped','not_applicable'} or t['placement']['state']=='pending' for t in targets)
    code=1 if failures else 2 if pending else 0
    return {**hardware,'outputs':inventory.get('outputs',[]),'logical_outputs':inventory.get('logical_outputs',[]),
            'output_state':inventory.get('state','unknown'),'reason':inventory.get('reason',''),
            'output_next_action':inventory.get('next_action',''),
            'targets':targets,'exit_code':code,'state':'unknown' if failures else 'pending' if pending else 'ok',
            'limits':'OS 출력·프로세스·관리 창 관측입니다. 외부 패널 수와 실제 표시 내용은 별도 확인이 필요합니다.'}



def preflight(config,host=None,paths=None):
    """Read common startup prerequisites before stopping a working display."""
    host,paths=host or Host(),paths or Paths()
    if not config.display:
        return {'state':'pending','exit_code':2,'reason':'화면 관리가 꺼져 있습니다.'}
    unsupported = _setup_runtime_guard(config, host, paths)
    if unsupported:
        return unsupported
    try:
        uid,_,_=display._account(config,host)
        sessions,_=display._graphical_sessions(host,uid)
        if sessions is None:return {'state':'unknown','exit_code':1,'reason':'그래픽 세션을 확실히 조회하지 못했습니다.'}
        if len(sessions)>1:return {'state':'blocked','exit_code':1,'reason':'그래픽 세션이 여러 개여서 기존 화면을 유지합니다.'}
        if not sessions or sessions[0][1].get('Active')!='yes':
            return {'state':'pending','exit_code':2,'reason':'단일 활성 그래픽 세션을 기다립니다.'}
        if sessions[0][1].get('LockedHint')=='yes':
            return {'state':'pending','exit_code':2,'reason':'화면이 잠겨 있어 기존 표시를 유지합니다.'}
        unit=display.unit_name(config.target_id)
        path=paths.at('/etc/systemd/user/'+unit)
        display._safe_parents(path)
        if path.is_file() and display.UNIT_MARKER not in path.read_text():
            return {'state':'blocked','exit_code':1,'reason':'관리하지 않는 동명 서비스 등록이 있습니다.'}
        registered=display._file_result('chrome.unit',path,display.unit_text(config.target_id),os.geteuid())
        if registered.state!='ok':
            return {'state':'blocked' if registered.state=='blocked' else 'pending',
                    'exit_code':1 if registered.state=='blocked' else 2,
                    'reason':'후보 실행 전에 간편 설정에서 고정 런타임 서비스 등록을 준비하세요.'}
        # A trial is not selected yet. Only current protected global config is
        # loaded by this observation-only RPC; candidate data remains at root.
        observation=replace(config,runtime_pin=None).for_screen(config.target_id,revision=config.config_revision)
        environment=display._user_call(observation,host,paths,'environment')
        if environment.get('exit_code')!=0 or environment.get('state')!='ready':return environment
        if config.schema_version==2 or config.output is not None:
            inventory=_inventory(observation,host,paths)
            resolved=_conflict(config,config.target_id,inventory,host=host,paths=paths,selected=config)
            if resolved['state']!='ok':return resolved | {'exit_code':1 if resolved['state']=='blocked' else 2}
            unsupported=_workspace_runtime_guard(config,inventory,host,paths)
            if unsupported:return unsupported
        return {'state':'ready','exit_code':0,'reason':'단일 그래픽 세션·실제 화면 환경·서비스·대상 출력의 시작 전제를 확인했습니다.'}
    except (OSError,ValueError,RuntimeError):
        return {'state':'unknown','exit_code':1,'reason':'화면 전환의 시작 전제를 확인하지 못해 기존 표시를 유지합니다.'}

def screen_action(action,config:Config,target='default',host=None,paths=None,*,timeout=None,internal=False):
    if action not in {'start','stop','restart','status','check','logs','reload','outputs','windows','preflight'}:
        raise ValueError('허용되지 않은 화면 작업입니다.')
    host,paths=host or Host(),paths or Paths()
    if timeout is not None:host=display.BudgetHost(host,timeout)
    context=_applied_context(config,target,host,paths)
    if action in {'start', 'restart'}:
        unsupported = _setup_runtime_guard(context, host, paths)
        if unsupported:
            return unsupported
    if action=='preflight':return preflight(context,host=host,paths=paths)
    if action=='outputs':return _inventory(config,host,paths)
    if action in {'start','restart'} and context.schema_version==2:
        # Explicit start of an already running instance is idempotent, even
        # during a hotplug. Restart still requires a confirmed destination.
        current=display.chrome_action('status',context,host=host,paths=paths)
        active=(current.get('service') or {}).get('ActiveState') in {'active','activating'}
        if not (action=='start' and active):
            guard=_conflict(config,target,_inventory(config,host,paths),host=host,paths=paths,selected=context)
            if guard['state']!='ok':return guard | {'exit_code':1 if guard['state']=='blocked' else 2}
    result=display.chrome_action(action,context,host=host,paths=paths)
    if action in {'start','restart'} and result.get('state')=='starting':
        return result | {'exit_code':2,'state':'pending','submitted':True,
                         'reason':'실행 요청을 접수했습니다. 프로세스·창·배치·문서 확인을 기다립니다.'}
    if action in {'status','check'}:
        if context.schema_version==1 and context.output is None:
            result['placement']={'state':'not_applicable','reason':'기존 기본 화면의 배치를 유지합니다.'}
        elif (result.get('service') or {}).get('ActiveState') in {'active','activating'}:
            result['placement']=display._user_call(context,host,paths,'windows')
        else:
            result['placement']={'state':'pending','reason':'관리 창 생성·실행을 기다립니다.'}
    return result
