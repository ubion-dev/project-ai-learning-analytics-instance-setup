"""DRM connector observation and the GNOME 47 display bridge."""
from __future__ import annotations

import ast
import json
import re

from .system import Host, Paths

UUID = 'ala-display@example.com'
BUS = 'org.ala.Display1'
OBJECT = '/org/ala/Display1'
PROTOCOL = 1
SUPPORTED_GNOME = ('47',)
DRM_CONNECTOR = re.compile(r'card[0-9]+-([A-Za-z0-9]+(?:-[A-Za-z0-9]+)*-[0-9]+)')


def hardware_topology(paths=None):
    """Read kernel connector status independently of the GNOME session."""
    root = (paths or Paths()).at('/sys/class/drm')
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return {'hardware_outputs': [], 'hardware_output_state': 'unknown',
                'hardware_output_reason': 'Linux DRM 커넥터를 조회하지 못했습니다.'}
    outputs = []
    for entry in entries:
        match = DRM_CONNECTOR.fullmatch(entry.name)
        if not match:
            continue
        try:
            status = (entry / 'status').read_text(encoding='ascii').strip()
        except (OSError, UnicodeError):
            # A connector may disappear between directory enumeration and read.
            status = 'unknown'
        if status not in {'connected', 'disconnected'}:
            status = 'unknown'
        outputs.append({'id': entry.name, 'name': match.group(1), 'status': status})
    return {'hardware_outputs': outputs, 'hardware_output_state': 'ok', 'hardware_output_reason': ''}


def bridge(method, request=None, *, host=None):
    if method not in {'GetState', 'Inspect', 'Place', 'Closed', 'DismissStartupOverview'}:
        raise ValueError('허용되지 않은 화면 bridge 작업입니다.')
    host = host or Host()
    argv = ['gdbus','call','--session','--dest',BUS,'--object-path',OBJECT,
            '--method',BUS+'.'+method]
    if request is not None:
        argv.append(json.dumps(request,separators=(',',':')))
    response = host.run(argv, timeout=5)
    if response.returncode:
        return {'state':'pending','reason':'GNOME 화면 확장에 연결되지 않았습니다.',
                'next_action':'간편 설정 후 GNOME 재로그인과 GNOME 47 확장 활성화를 확인하세요.'}
    try:
        if len(response.stdout.encode()) > 1048576:
            raise ValueError()
        wrapped = ast.literal_eval(response.stdout.strip())
        if not isinstance(wrapped,tuple) or len(wrapped)!=1 or not isinstance(wrapped[0],str):
            raise ValueError()
        value=json.loads(wrapped[0])
        if not isinstance(value,dict) or value.get('protocol')!=PROTOCOL:
            raise ValueError()
        return value
    except (SyntaxError,ValueError,TypeError,MemoryError,RecursionError):
        return {'state':'unknown','reason':'GNOME 화면 확장 응답의 버전·형식을 확인하지 못했습니다.',
                'next_action':'설치된 ala 버전과 GNOME 화면 확장의 등록·활성화를 확인하세요.'}


def dismiss_startup_overview(host):
    """Wait briefly for the user's extension after queuing the display service."""
    try:
        ready = host.run(['gdbus', 'wait', '--session', '--timeout', '5', BUS], timeout=6)
        if ready.returncode == 0:
            result = bridge('DismissStartupOverview', host=host)
            if result.get('state') == 'ok' and result.get('overview') in (
                'dismissed', 'already_hidden', 'scheduled', 'kept',
            ):
                return result['overview']
    except (OSError, RuntimeError, ValueError):
        pass
    return 'unavailable'


def _number(value):
    return type(value) in (int,float) and -1000000 <= value <= 1000000


def topology(*,host=None):
    value=bridge('GetState',host=host)
    if value.get('state')!='ok':
        return value | {'outputs':[],'logical_outputs':[]}
    try:
        if str(value.get('gnome_version','')).split('.')[0] not in SUPPORTED_GNOME:
            return {'state':'pending','outputs':[],'logical_outputs':[],
                    'reason':'현재 GNOME 버전의 정식 배치를 지원하지 않습니다.',
                    'next_action':'관리 화면을 실행하는 GNOME 47 세션을 확인하세요.'}
        raw=value['display_config']
        if not isinstance(raw,list) or len(raw)!=4:
            raise ValueError()
        serial, physical, logical, properties=raw
        if type(serial) is not int or not isinstance(physical,list) or not isinstance(logical,list):
            raise ValueError()
        if len(physical)>64 or len(logical)>64:
            raise ValueError()
        outputs=[]
        for monitor in physical:
            spec,modes,_=monitor
            connector=spec[0]
            if not isinstance(connector,str) or not connector or len(connector)>128:
                raise ValueError()
            current=next((mode for mode in modes if mode[-1].get('is-current') is True),None)
            outputs.append({'id':connector,'connector':connector,
                            'width':current[1] if current else None,'height':current[2] if current else None,
                            'active':False})
        # Main.layoutManager supplies actual compositor coordinates. Compare
        # connector-derived logical origins instead of estimating fractional
        # scaling, transformed physical pixels, or external panel geometry.
        rectangles=value.get('compositor_monitors',[])
        normalized=[]
        for item in logical:
            x,y,scale,transform,primary,specs,_=item
            connectors=sorted(spec[0] for spec in specs)
            if not connectors or not all(any(o['id']==c for o in outputs) for c in connectors):
                raise ValueError()
            matches=[r for r in rectangles if r.get('x')==x and r.get('y')==y]
            rect=matches[0] if len(matches)==1 else None
            confirmed=bool(rect and all(_number(rect.get(k)) for k in ('x','y','width','height'))
                           and rect['width']>0 and rect['height']>0)
            normalized.append({'id':'+'.join(connectors),'connectors':connectors,
                               'x':x,'y':y,'width':rect['width'] if confirmed else None,
                               'height':rect['height'] if confirmed else None,'scale':scale,
                               'transform':transform,'primary':bool(primary),'mirrored':len(connectors)>1,
                               'confirmed':confirmed})
            for output in outputs:
                if output['id'] in connectors:output['active']=True
        return {'state':'ok','protocol':PROTOCOL,'gnome_version':value['gnome_version'],
                'serial':serial,'outputs':outputs,'logical_outputs':normalized,
                'reason':'OS가 인식한 출력입니다. 외부 제어 장비의 패널 수는 확인하지 않습니다.'}
    except (ValueError,KeyError,TypeError,IndexError,StopIteration,AttributeError):
        return {'state':'unknown','outputs':[],'logical_outputs':[],
                'reason':'Mutter 출력과 compositor 좌표를 연결하지 못했습니다.',
                'next_action':'GNOME 디스플레이 설정에서 활성 출력과 배치를 확인하세요.'}


def resolve_output(inventory, selector, *, auto=False):
    if inventory.get('state')!='ok':
        return {'state':'pending','reason':inventory.get('reason','출력 확인 대기'),
                'next_action':inventory.get('next_action','')}
    logical_outputs=inventory.get('logical_outputs',[])
    choices=[item for item in logical_outputs if item.get('confirmed')]
    if selector is None:
        matches=choices if auto and len(logical_outputs)==1 else []
    else:
        matches=[item for item in choices if selector==item['id'] or selector in item['connectors']]
    if len(matches)!=1:
        return {'state':'pending','reason':'대상 출력을 하나로 확인하지 못했습니다. 연결과 출력 선택을 확인하세요.'}
    return {'state':'ok','output':matches[0], 'selector':selector or matches[0]['id']}


def desired_rect(output,layout):
    if layout not in {'fullscreen','left','right'}:raise ValueError('지원하지 않는 배치입니다.')
    x,y,w,h=(int(output[k]) for k in ('x','y','width','height'))
    if layout=='left':w=w//2
    elif layout=='right':x,w=x+w//2,w-w//2
    return {'x':x,'y':y,'width':w,'height':h}


def ownership(health):
    keys=('supervisor_pid','browser_pid','supervisor_start_ticks','browser_start_ticks','boot_id')
    result={key:health.get(key) for key in keys}
    if (any(type(result[k]) is not int or result[k]<1 for k in keys[:2])
            or any(not isinstance(result[k],str) or not result[k].isdecimal() for k in keys[2:4])
            or not isinstance(result['boot_id'],str)):
        return None
    return result


def placement(config,health,*,host=None,apply=False,inventory=None):
    if config.schema_version==1 and config.output is None:
        return {'state':'not_applicable','reason':'기존 기본 화면의 배치는 유지합니다.','windows':[],
                'confirmed_window_count':None}
    evidence=ownership(health)
    if evidence is None:
        return {'state':'pending','reason':'현재 관리 Chrome의 프로세스 증거를 기다립니다.',
                'windows':[],'confirmed_window_count':None}
    inventory=inventory if inventory is not None else topology(host=host)
    previous=health.get('placement')
    pinned=previous.get('output') if isinstance(previous,dict) and isinstance(previous.get('output'),str) else None
    selector=config.output or pinned
    resolved=resolve_output(inventory,selector,auto=config.target_id=='default')
    if resolved['state']!='ok':return resolved | {'windows':[],'confirmed_window_count':None}
    selected=resolved['output']
    request=evidence | {'target':config.target_id,'layout':config.layout,'output':resolved['selector'],
                        'rect':{k:selected[k] for k in ('x','y','width','height')}}
    observed=bridge('Inspect',request,host=host)
    if observed.get('state')!='ok':return observed | {'confirmed_window_count':None}
    windows=observed.get('windows')
    if not isinstance(windows,list):
        return {'state':'unknown','reason':'관리 창 조회 응답이 올바르지 않습니다.','confirmed_window_count':None}
    if len(windows)!=1:
        return {'state':'pending' if not windows else 'blocked','windows':windows,
                'confirmed_window_count':len(windows),'reason':'관리 창 하나의 생성·식별을 기다립니다.' if not windows else '관리 창이 여러 개여서 임의로 배치하지 않습니다.'}
    expected=desired_rect(selected,config.layout)
    actual=windows[0].get('rect',{})
    correct=(all(type(actual.get(k)) is int and abs(actual[k]-expected[k])<=2 for k in expected)
             and windows[0].get('fullscreen')==(config.layout=='fullscreen'))
    if not correct and apply:
        placed=bridge('Place',request,host=host)
        if placed.get('state') not in {'ok','pending'}:return placed | {'confirmed_window_count':1}
        # Placement completion requires a separate actual observation, never
        # the successful delivery of move/fullscreen API calls.
        return placement(config,health,host=host,apply=False,inventory=inventory)
    return {'state':'ok' if correct else 'pending','windows':windows,'confirmed_window_count':1,
            'output':resolved['selector'],'layout':config.layout,'expected':expected,
            'reason':'관리 창의 실제 배치를 확인했습니다.' if correct else '실제 창 배치 적용·확인을 기다립니다.'}
