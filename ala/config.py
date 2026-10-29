from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import json
import re

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

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
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

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise ValueError("구성은 JSON object여야 합니다.")
        extra = set(value) - {f.name for f in fields(cls)}
        if extra:
            raise ValueError(f"알 수 없는 구성 필드: {', '.join(sorted(extra))}")
        return cls(**value)

    @classmethod
    def load(cls, path: Path):
        return cls.from_dict(json.loads(path.read_text()))

    def to_dict(self):
        return asdict(self)
