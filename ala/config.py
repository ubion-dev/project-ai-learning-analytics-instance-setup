from __future__ import annotations

from dataclasses import asdict, dataclass, fields, field
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import json
import re

from .frontend import FrontendConfig


def _url(value):
    if value is None:
        return
    if not isinstance(value,str) or any(c.isspace() or ord(c)<32 or ord(c)==127 for c in value):
        raise ValueError('URL에 공백/제어 문자를 사용할 수 없습니다.')
    try:
        parts=urlsplit(value)
        valid=parts.scheme in ('http','https') and bool(parts.hostname) and parts.port!=0
    except ValueError:
        valid=False
    if not valid or parts.username is not None or parts.password is not None:
        raise ValueError('인증정보 없는 http/https URL을 지정하세요.')
    sensitive={'token','access_token','refresh_token','id_token','password','passwd','api_key','apikey',
               'authorization','auth','secret','client_secret','signature'}
    if any(key.lower().replace('-','_') in sensitive for key,_ in parse_qsl(parts.query,keep_blank_values=True)):
        raise ValueError('인증 토큰·암호가 포함된 URL은 저장하지 않습니다.')


@dataclass(frozen=True)
class ScreenConfig:
    id: str='default'
    url: str|None=None
    output: str|None=None
    layout: str='fullscreen'
    autostart: bool=True
    frontend: FrontendConfig=FrontendConfig()

    def __post_init__(self):
        if not isinstance(self.id,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}',self.id):
            raise ValueError('화면 ID는 영문 소문자로 시작하는 1~32자 식별자여야 합니다.')
        _url(self.url)
        if self.output is not None and (not isinstance(self.output,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:+-]{0,127}',self.output)):
            raise ValueError('유효한 출력 식별자를 지정하세요.')
        if not isinstance(self.layout,str) or self.layout not in {'fullscreen','left','right'}:
            raise ValueError('화면 배치는 fullscreen/left/right 중 하나여야 합니다.')
        if type(self.autostart) is not bool:
            raise ValueError('화면 자동 시작에는 true/false가 필요합니다.')
        if isinstance(self.frontend,dict):
            object.__setattr__(self,'frontend',FrontendConfig.from_dict(self.frontend))
        elif not isinstance(self.frontend,FrontendConfig):
            raise ValueError('프론트 갱신 구성이 올바르지 않습니다.')

    @classmethod
    def from_dict(cls,value):
        if not isinstance(value,dict) or set(value)-{f.name for f in fields(cls)}:
            raise ValueError('화면 구성은 알려진 필드를 가진 JSON object여야 합니다.')
        if 'id' not in value:
            raise ValueError('화면 ID가 필요합니다.')
        return cls(**value)

    def to_dict(self):
        return asdict(self)

@dataclass(frozen=True)
class Config:
    schema_version: int = 1
    user: str = ""
    url: str | None = None
    timezone: str = "Asia/Seoul"
    root_bash: bool = False
    dev_tools: bool = False
    ssh: bool = True
    display: bool = True
    autologin: bool = True
    autostart: bool = True
    ssh_zone: str | None = None
    screens: tuple[ScreenConfig,...] = ()
    runtime_pin: dict | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version not in {1,2}:
            raise ValueError("지원하지 않는 구성 버전입니다.")
        for key in ("root_bash", "dev_tools", "ssh", "display", "autologin", "autostart"):
            if type(getattr(self, key)) is not bool:
                raise ValueError(f"{key}: true/false가 필요합니다.")
        if not isinstance(self.user, str) or (self.user and (not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.-]{0,63}", self.user) or self.user == "root")):
            raise ValueError("user에는 기존 일반 사용자 이름을 지정하세요.")
        if self.url is not None:
            if not isinstance(self.url, str) or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in self.url):
                raise ValueError("URL에 공백/제어 문자를 사용할 수 없습니다.")
            try:
                parts = urlsplit(self.url)
                valid = parts.scheme in ("http", "https") and bool(parts.hostname) and parts.port != 0
            except ValueError:
                valid = False
            if not valid or parts.username is not None or parts.password is not None:
                raise ValueError("인증정보 없는 http/https URL을 지정하세요.")
            sensitive = {'token','access_token','refresh_token','id_token','password','passwd','api_key',
                         'apikey','authorization','auth','secret','client_secret','signature'}
            if any(key.lower().replace('-', '_') in sensitive for key, _ in parse_qsl(parts.query, keep_blank_values=True)):
                raise ValueError("인증 토큰·암호가 포함된 URL은 저장하지 않습니다. 로그인 없는 표시 URL을 사용하세요.")
        if not isinstance(self.timezone, str):
            raise ValueError("timezone에는 문자열이 필요합니다.")
        try:
            ZoneInfo(self.timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("유효하지 않은 시간대입니다.") from None
        if self.ssh_zone is not None and (not isinstance(self.ssh_zone, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", self.ssh_zone)):
            raise ValueError("ssh_zone이 올바르지 않습니다.")
        if not isinstance(self.screens,(list,tuple)) or len(self.screens)>16:
            raise ValueError('화면 구성은 최대 16개 목록이어야 합니다.')
        normalized=tuple(ScreenConfig.from_dict(item) if isinstance(item,dict) else item for item in self.screens)
        if any(not isinstance(item,ScreenConfig) for item in normalized):
            raise ValueError('잘못된 화면 구성이 있습니다.')
        if self.schema_version==1 and normalized:
            raise ValueError('다중 화면 구성은 schema_version 2로 명시 저장하세요.')
        if self.schema_version==2 and not normalized:
            raise ValueError('화면 구성 목록이 필요합니다.')
        if len({item.id for item in normalized})!=len(normalized):
            raise ValueError('화면 ID가 중복되었습니다.')
        mapped={}
        for item in normalized:
            if item.output is None:continue
            layouts=mapped.setdefault(item.output,[])
            if item.layout in layouts or ('fullscreen' in layouts) or (item.layout=='fullscreen' and layouts):
                raise ValueError('같은 출력의 화면 배치가 겹칩니다.')
            layouts.append(item.layout)
        object.__setattr__(self,'screens',normalized)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise ValueError("구성은 JSON object여야 합니다.")
        extra = set(value) - {f.name for f in fields(cls) if f.name != "runtime_pin"}
        if extra:
            raise ValueError(f"알 수 없는 구성 필드: {', '.join(sorted(extra))}")
        return cls(**value)

    @classmethod
    def load(cls, path: Path):
        return cls.from_dict(json.loads(path.read_text()))

    def to_dict(self):
        value=asdict(self)
        value.pop('runtime_pin')
        if self.schema_version==1:value.pop('screens')
        else:value['screens']=[item.to_dict() for item in self.screens]
        return value

    def screen_targets(self):
        if self.schema_version==1:
            return (ScreenConfig(id='default',url=self.url,autostart=self.autostart),)
        return self.screens

    def for_screen(self,target='default',*,revision=''):
        screen=next((item for item in self.screen_targets() if item.id==target),None)
        if screen is None:raise ValueError('해당 화면 ID가 구성에 없습니다.')
        context=replace(self,url=screen.url,autostart=screen.autostart)
        for key,value in (('_target_id',screen.id),('_output',screen.output),('_layout',screen.layout),
                          ('_frontend',screen.frontend),('_config_revision',revision)):
            object.__setattr__(context,key,value)
        return context

    @property
    def target_id(self):return getattr(self,'_target_id','default')
    @property
    def output(self):return getattr(self,'_output',None)
    @property
    def layout(self):return getattr(self,'_layout','fullscreen')
    @property
    def frontend(self):return getattr(self,'_frontend',FrontendConfig())
    @property
    def config_revision(self):return getattr(self,'_config_revision','')
