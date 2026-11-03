from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
import re

STATES = {"ok", "change_required", "pending", "not_applicable", "blocked", "unknown", "error"}

def redact(value):
    """Don't echo daemon proxy credentials or secret-shaped keys into reports."""
    if isinstance(value, dict):
        return {key: '[redacted]' if re.search(r'password|token|secret|credential|authorization|api.?key', str(key), re.I)
                else redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r'(?i)(https?://)[^/@\s]+@', r'\1[redacted]@', value)
        return re.sub(r'(?i)([?&](?:access_token|token|password|api_key|secret)=)[^&\s]+', r'\1[redacted]', value)
    return value

@dataclass
class CheckResult:
    id: str
    state: str
    expected: Any = None
    actual: Any = None
    next_action: str = ""
    detail: str = ""

    def __post_init__(self):
        if self.state not in STATES:
            raise ValueError(f"잘못된 검사 상태: {self.state}")

    def to_dict(self):
        return redact(asdict(self))

@dataclass
class Report:
    results: list[CheckResult] = field(default_factory=list)
    scope: str | None = None

    @property
    def exit_code(self) -> int:
        if any(r.state in {"change_required", "blocked", "unknown", "error"} for r in self.results):
            return 1
        return 2 if any(r.state == "pending" for r in self.results) else 0

    def to_dict(self):
        result = {"schema_version": 1, "exit_code": self.exit_code,
                "summary": {s: sum(r.state == s for r in self.results) for s in sorted(STATES)},
                "results": [r.to_dict() for r in self.results]}
        if self.scope is not None:
            result['scope'] = self.scope
        return result
