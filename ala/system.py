from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import subprocess
import tempfile

@dataclass(frozen=True)
class Paths:
    root: Path = Path("/")

    def at(self, value: str) -> Path:
        return self.root / value.lstrip("/")

    @property
    def config(self):
        return self.at("/etc/ala/config.json")

    @property
    def runs(self):
        return self.at("/var/lib/ala/runs")

    @property
    def backups(self):
        return self.at("/var/lib/ala/backups")

@dataclass
class Command:
    returncode: int
    stdout: str = ""
    stderr: str = ""

class Host:
    def run(self, argv, *, timeout=10, env=None) -> Command:
        try:
            p = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, env=env)
            return Command(p.returncode, p.stdout, p.stderr)
        except FileNotFoundError as exc:
            return Command(127, "", str(exc))
        except subprocess.TimeoutExpired:
            return Command(124, "", "조회 제한 시간 초과")

    def execute(self, argv, *, env=None) -> int:
        return subprocess.run(list(argv), env=env, check=False).returncode

def atomic_json(path: Path, value, mode=0o600):
    import json
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n", mode)

def atomic_write(path: Path, data: str, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".ala-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def platform_error(paths=Paths()) -> str | None:
    import platform
    try:
        values = dict(line.split("=", 1) for line in paths.at("/etc/os-release").read_text().splitlines() if "=" in line)
        if values.get("ID", "").strip('"') != "rocky" or values.get("VERSION_ID", "").strip('"').split(".")[0] != "10":
            return "Rocky Linux 10 전용입니다."
    except OSError:
        return "Rocky Linux 10 정보를 읽을 수 없습니다."
    if platform.machine() != "x86_64":
        return "x86_64 전용입니다."
    return None
