from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

STATES = {"ok", "change_required", "pending", "not_applicable", "blocked", "unknown", "error"}

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
        return asdict(self)

@dataclass
class Report:
    results: list[CheckResult] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if any(r.state in {"change_required", "blocked", "unknown", "error"} for r in self.results):
            return 1
        return 2 if any(r.state == "pending" for r in self.results) else 0

    def to_dict(self):
        return {"schema_version": 1, "exit_code": self.exit_code,
                "summary": {s: sum(r.state == s for r in self.results) for s in sorted(STATES)},
                "results": [r.to_dict() for r in self.results]}
