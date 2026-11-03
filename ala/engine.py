"""Read-only expectations and idempotent execution shared by CLI and TUI.

The shell modules remain the mutation boundary. Inspections never execute those
modules, and their managed heredocs are also the source of expected file bytes.
"""
from __future__ import annotations

import configparser
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
from time import perf_counter

from .config import Config
from .model import CheckResult, Report
from .system import Host, Paths, platform_error


SCRIPTS = Path(__file__).resolve().parent.parent / "scripts" / "rocky-linux"
BASE_PACKAGES = ("dnf-plugins-core", "git", "curl", "ca-certificates", "bash-completion", "firewalld", "chrony", "python3", "iproute", "kmod")
DOCKER_PACKAGES = ("docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin")
CONFLICT_PACKAGES = ("podman", "podman-docker", "runc", "docker", "docker-common", "docker-engine")
UPDATE_UNITS = tuple(f"dnf-automatic{suffix}.{kind}" for kind in ("timer", "service")
                     for suffix in ("", "-download", "-install", "-notifyonly")) + ("packagekit.service", "packagekit-offline-update.service")
SLEEP_UNITS = ("sleep.target", "suspend.target", "hibernate.target", "hybrid-sleep.target", "suspend-then-hibernate.target")
SUPPORT_URL = "https://raw.githubusercontent.com/NVIDIA/open-gpu-kernel-modules/main/README.md"


def _inaccessible(command):
    text = command.stderr.lower()
    return command.returncode in (124, 126, 127) or any(value in text for value in
        ("permission denied", "access denied", "not permitted", "failed to connect to bus", "not been booted with systemd"))


def _legacy_module(name):
    spec = importlib.util.spec_from_file_location(f"ala_legacy_{name}", SCRIPTS / "lib" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _template(script, label):
    source = (SCRIPTS / script).read_text()
    found = re.search(r"<<'?" + re.escape(label) + r"'?\n(.*?)\n" + re.escape(label) + r"(?:\n|$)", source, re.S)
    if found is None:
        raise ValueError(f"설정 템플릿이 없습니다: {script}/{label}")
    return found.group(1) + "\n"


class Engine:
    supports_substeps = True
    def __init__(self, config: Config, host=None, paths=None):
        self.config = config
        self.host = host if host is not None else Host()
        self.paths = paths if paths is not None else Paths()
        self.owner = 0 if self.paths.root == Path("/") else os.geteuid()
        self.inspection = None

    def _run(self, argv, **kwargs):
        return self.host.run(argv, **kwargs)

    def _result(self, id, state, expected=None, actual=None, action="", detail=""):
        return CheckResult(id, state, expected, actual, action, detail)

    def _command(self, id, argv, expected=0, *, missing="unknown", failed="change_required"):
        command = self._run(argv)
        if _inaccessible(command):
            state = missing
        else:
            state = "ok" if command.returncode == expected else failed
        return self._result(id, state, "명령 성공", command.stdout.strip() or command.stderr.strip(),
                            "필요한 권한·선행 도구를 확인하세요." if state == "unknown" else "ala setup --apply")

    def _package(self, package, prefix):
        result = self._run(["rpm", "-q", package])
        if result.returncode == 0:
            state = "ok"
        elif result.returncode == 1:
            state = "change_required"
        else:
            state = "unknown"
        return self._result(f"{prefix}.package.{package}", state, "설치됨", result.stdout.strip() or result.stderr.strip(), "누락 패키지만 설치")

    def _service(self, name, prefix):
        active = self._run(["systemctl", "is-active", name])
        enabled = self._run(["systemctl", "is-enabled", name])
        output = []
        for kind, result, expected in (("active", active, "active"), ("enabled", enabled, "enabled")):
            if _inaccessible(result):
                state = "unknown"
            else:
                state = "ok" if result.returncode == 0 and result.stdout.strip() == expected else "change_required"
            output.append(self._result(f"{prefix}.service.{name}.{kind}", state, expected,
                                      result.stdout.strip() or result.stderr.strip(), "서비스 시작·영구 부팅 자동 시작 확인"))
        return output

    def _masked(self, unit, prefix, *, inactive=True):
        enabled = self._run(["systemctl", "is-enabled", unit])
        active = self._run(["systemctl", "is-active", unit]) if inactive else None
        if _inaccessible(enabled):
            state = "unknown"
        elif enabled.stdout.strip() != "masked":
            state = "change_required"
        elif active is not None and _inaccessible(active):
            state = "unknown"
        elif active is not None and active.stdout.strip() in ("active", "activating", "reloading"):
            state = "pending"
        else:
            state = "ok"
        return self._result(f"{prefix}.mask.{unit}", state, "masked" + (", inactive" if inactive else ""),
                            {"enabled": enabled.stdout.strip(), "active": active.stdout.strip() if active else None},
                            "진행 중 트랜잭션은 중단하지 말고 유지보수 후 재검사" if state == "pending" else "영구 차단 적용")

    def _file(self, id, filename, expected, mode=0o644, uid=None):
        path = self.paths.at(filename)
        uid = self.owner if uid is None else uid
        try:
            if path.is_symlink():
                return self._result(id, "blocked", "일반 파일", filename, "심볼릭 링크를 직접 검토하세요.")
            if not path.exists():
                return self._result(id, "change_required", expected, None, "관리 설정 파일 준비")
            value = path.stat()
            content = path.read_text()
            correct = content == expected and stat.S_IMODE(value.st_mode) == mode and value.st_uid == uid
            return self._result(id, "ok" if correct else "change_required", expected, content,
                                "내용·소유자·권한 비교 후 필요한 부분만 적용")
        except (OSError, UnicodeError) as exc:
            return self._result(id, "unknown", "읽을 수 있는 설정", str(exc), "파일 읽기 권한·형식 확인")

    def _restart_pending(self, id, service, filename):
        """Compare persisted config to the running service's start, without restarting it."""
        path = self.paths.at(filename)
        if not path.is_file():
            return None
        started = self._run(["systemctl", "show", service, "--property=ExecMainStartTimestampMonotonic", "--value"])
        try:
            if started.returncode != 0 or not started.stdout.strip().isdigit() or int(started.stdout.strip()) == 0:
                raise ValueError("실행 시각 조회 실패")
            boot = next(int(line.split()[1]) for line in self.paths.at("/proc/stat").read_text().splitlines() if line.startswith("btime "))
            seconds = boot + int(started.stdout.strip()) / 1_000_000
            pending = path.stat().st_mtime > seconds + 1
            return self._result(id, "pending" if pending else "ok", "저장 설정 이후 서비스 기동", "재시작 필요" if pending else "기동 시각 확인", "유지보수 시간에 재시작 또는 재부팅 후 확인")
        except (OSError, ValueError, StopIteration) as exc:
            return self._result(id, "unknown", "저장 설정의 실행 반영 시점", str(exc), "서비스 시작 시각과 파일 변경 시각을 확인하세요.")

    def _account(self, name):
        if not name:
            return None, self._result("user", "blocked", "기존 일반 사용자", "미지정", "ala config edit에서 사용자를 지정하세요.")
        value = self._run(["getent", "passwd", name])
        if value.returncode != 0:
            state = "blocked" if value.returncode == 2 else "unknown"
            return None, self._result("user", state, "존재하는 사용자", value.stderr.strip() or name, "기존 일반 사용자 지정")
        try:
            fields = value.stdout.strip().split(":")
            uid = int(fields[2])
            if len(fields) != 7 or fields[0] != name or uid == 0 or not fields[5].startswith("/") or fields[5] == "/":
                raise ValueError("계정/홈/UID 조건 불일치")
            if not fields[6].endswith(("/bash", "/zsh", "/fish", "/sh")):
                raise ValueError("서비스 계정 또는 지원하지 않는 로그인 셸")
        except (ValueError, IndexError) as exc:
            return None, self._result("user", "blocked", "사용 가능한 일반 계정", str(exc), "getent passwd 결과 확인")
        return fields, self._result("user", "ok", name, {"uid": uid, "home": fields[5], "shell": fields[6]})

    def _base(self):
        results = [self._package(name, "base") for name in BASE_PACKAGES]
        chrome = self._package("google-chrome-stable", "base")
        results.append(chrome)
        if chrome.state == "ok":
            results.append(self._command("base.chrome.version", ["google-chrome-stable", "--version"], failed="blocked"))
        else:
            results.append(self._result("base.chrome.version", "pending", "실행 가능", "패키지 준비 후 확인", "Chrome 설치 후 검사"))
        try:
            module = _legacy_module("chrome_policy")
            directory = self.paths.at("/etc/opt/chrome/policies/managed")
            files = module.read_directory(directory)
            for key in module.POLICIES:
                declarations = [(path, data[key]) for path, data in files.items() if key in data]
                wrong_external = [str(path) for path, value in declarations if path.name != module.FILENAME and
                                  (value is not False or not module.readable_policy(path))]
                good = bool(declarations) and all(value is False and module.readable_policy(path) for path, value in declarations)
                state = "blocked" if wrong_external else "ok" if good else "change_required"
                results.append(self._result(f"base.chrome.policy.{key}", state, False,
                                            wrong_external or {str(path): value for path, value in declarations}, "Chrome 정책 값·소유자·권한 확인"))
            for path in (directory.parent, directory):
                if not path.exists():
                    state = "change_required"
                else:
                    info = path.stat()
                    state = "ok" if info.st_uid == self.owner and stat.S_IMODE(info.st_mode) == 0o755 else "change_required"
                results.append(self._result("base.chrome.directory." + path.name, state, "관리자 소유 0755", str(path)))
        except (OSError, ValueError) as exc:
            results.append(self._result("base.chrome.policy", "blocked", "유효한 정책", str(exc), "정책 충돌/잘못된 JSON을 확인하세요."))
        results.append(self._file("base.chrome.launcher", "/usr/local/bin/chrome-did", (SCRIPTS / "bin/chrome-did").read_text(), 0o755))
        timezone = self._run(["timedatectl", "show", "--property=Timezone", "--value"])
        state = "unknown" if timezone.returncode != 0 else "ok" if timezone.stdout.strip() == self.config.timezone else "change_required"
        results.append(self._result("base.timezone", state, self.config.timezone, timezone.stdout.strip(), "시간대 설정"))
        results.extend(self._service("chronyd", "base"))
        return results

    def _shell(self):
        results = [self._package(name, "shell") for name in ("bash", "git", "bash-completion")]
        names = [self.config.user] + (["root"] if self.config.root_bash else [])
        for name in names:
            if name == "root":
                command = self._run(["getent", "passwd", "root"])
                fields = command.stdout.strip().split(":") if command.returncode == 0 else []
                if len(fields) != 7:
                    results.append(self._result("shell.root", "unknown", "root 계정 정보", command.stderr)); continue
            else:
                fields, account = self._account(name)
                if fields is None:
                    results.append(account); continue
            home, uid, shell = fields[5], int(fields[2]), fields[6]
            prefix = f"shell.{name}"
            results.append(self._result(prefix + ".login_shell", "ok" if shell.endswith("/bash") else "change_required" if shell.endswith(("/zsh", "/fish", "/sh")) else "blocked", "/bin/bash", shell))
            folder = self.paths.at(home) / ".config/server-bash"
            omb = self.paths.at(home) / ".oh-my-bash"
            if omb.exists() and not (folder / "managed").is_file():
                results.append(self._result(prefix + ".ownership", "blocked", "도구가 관리하는 Oh My Bash", str(omb), "기존 사용자 셸 구성을 검토하세요.")); continue
            source = omb / "oh-my-bash.sh"
            results.append(self._result(prefix + ".ohmybash", "ok" if source.is_file() and (folder / "managed").is_file() else "change_required", "Oh My Bash와 관리 표시", str(omb)))
            for suffix, filename, label in (("init", home + "/.config/server-bash/init.bash", "INIT"), ("theme", home + "/.config/server-bash/server.theme.sh", "THEME"),
                                             ("custom_theme", home + "/.oh-my-bash/custom/themes/server/server.theme.sh", "THEME")):
                results.append(self._file(prefix + "." + suffix, filename, _template("setup-bash.sh", label), 0o600, uid))
            bashrc = self.paths.at(home) / ".bashrc"
            try:
                content = bashrc.read_text() if bashrc.exists() else ""
                hook = _template("setup-bash.sh", "HOOK").strip()
                state = "ok" if hook in content else "blocked" if re.search(r"^[ \t]*[^#\n].*oh-my-bash\.sh", content, re.M) else "change_required"
                results.append(self._result(prefix + ".bashrc", state, "관리 hook", hook in content, "기존 .bashrc 보존 후 관리 hook 추가"))
                if bashrc.exists():
                    results.append(self._command(prefix + ".syntax", ["bash", "-n", str(bashrc)], failed="blocked"))
                profile = next((self.paths.at(home) / p for p in (".bash_profile", ".bash_login", ".profile") if (self.paths.at(home) / p).exists()), None)
                if profile and ".bashrc" not in profile.read_text():
                    results.append(self._result(prefix + ".login_profile", "blocked", ".bashrc 로딩", str(profile), "기존 로그인 프로필을 보존하며 .bashrc 로딩을 추가해야 합니다."))
                commit = folder / "oh-my-bash.commit"
                git = ["git", "-C", str(omb), "rev-parse", "HEAD"]
                # Query as the repository owner; never weaken Git's safe.directory protections.
                git_command = git if name == "root" else ["runuser", "-u", name, "--", "env", "HOME=" + home, *git]
                revision = self._run(git_command) if source.is_file() else None
                match = revision is not None and revision.returncode == 0 and commit.is_file() and commit.read_text().strip() == revision.stdout.strip()
                results.append(self._result(prefix + ".commit", "ok" if match else "change_required", "설치 OMB commit 기록", commit.read_text().strip() if commit.exists() else None))
            except (OSError, UnicodeError) as exc:
                results.append(self._result(prefix + ".files", "unknown", "읽을 수 있는 셸 설정", str(exc)))
        return results

    def _updates(self):
        results = [self._masked(unit, "update") for unit in UPDATE_UNITS]
        for filename in ("/system-update", "/etc/system-update"):
            path = self.paths.at(filename)
            if path.exists() or path.is_symlink():
                results.append(self._result("update.offline." + filename, "blocked", "예약된 업데이트 없음", filename, "예약된 트랜잭션을 검토하세요. 자동 삭제하지 않습니다."))
        for unit in UPDATE_UNITS:
            if unit.endswith(".service") and unit != "packagekit.service":
                active = self._run(["systemctl", "is-active", unit])
                if active.stdout.strip() in {"active", "activating", "reloading"}:
                    results.append(self._result("update.transaction." + unit, "blocked", "트랜잭션 종료", active.stdout.strip(), "업데이트 완료 후 재실행. 강제 종료하지 않습니다."))
        if self.config.display:
            results.extend(self._dconf("update", "10-updates", "updates", "setup-update-policy.sh"))
        return results

    def _dconf(self, prefix, name, lock, script):
        results = [self._file(f"{prefix}.dconf.source", f"/etc/dconf/db/rocky_setup.d/{name}", _template(script, "POLICY")),
                   self._file(f"{prefix}.dconf.locks", f"/etc/dconf/db/rocky_setup.d/locks/{lock}", _template(script, "LOCKS"))]
        profile = self.paths.at("/etc/dconf/profile/user")
        database = self.paths.at("/etc/dconf/db/rocky_setup")
        try:
            lines = profile.read_text().splitlines() if profile.exists() else []
            selected = "system-db:rocky_setup" in lines and any(line.startswith("user-db:") for line in lines)
            results.append(self._result(prefix + ".dconf.profile", "ok" if selected else "change_required", "system-db:rocky_setup", lines))
            source = self.paths.at("/etc/dconf/db/rocky_setup.d")
            files = [p for p in source.rglob("*") if p.is_file() and not any(part.startswith(".") for part in p.relative_to(source).parts)]
            if profile.exists():
                files.append(profile)
            compiled = database.is_file() and all(p.stat().st_mtime_ns <= database.stat().st_mtime_ns for p in files)
            results.append(self._result(prefix + ".dconf.compiled", "ok" if compiled else "change_required", "최신 정책 DB", compiled, "dconf DB 재컴파일"))
        except OSError as exc:
            results.append(self._result(prefix + ".dconf", "unknown", "정책 읽기", str(exc)))
        return results

    def _display_policy(self):
        if not self.config.display:
            return [self._result("display", "not_applicable", False, False)]
        schemas = self._run(["gsettings", "list-schemas"])
        required = {"org.gnome.desktop.session", "org.gnome.desktop.screensaver", "org.gnome.settings-daemon.plugins.power"}
        present = set(schemas.stdout.splitlines())
        results = [self._result("display.gnome", "ok" if schemas.returncode == 0 and required <= present else "blocked", sorted(required), sorted(present), "GNOME/GDM이 설치된 Rocky 서버가 필요합니다.")]
        results.extend(self._dconf("display", "00-display", "display", "setup-display.sh"))
        results.append(self._file("display.logind", "/etc/systemd/logind.conf.d/60-rocky-kiosk.conf", _template("setup-display.sh", "LOGIND")))
        results.extend(self._masked(unit, "display", inactive=False) for unit in SLEEP_UNITS)
        runtime = self._restart_pending("display.logind.runtime", "systemd-logind.service", "/etc/systemd/logind.conf.d/60-rocky-kiosk.conf")
        if runtime:
            results.append(runtime)
        return results

    def _ssh(self):
        if not self.config.ssh:
            return [self._result("ssh", "not_applicable", False, False)]
        results = [self._package(name, "ssh") for name in ("openssh-server", "firewalld", "iproute")]
        for service in ("sshd", "firewalld"):
            results.extend(self._service(service, "ssh"))
        if any(r.state != "ok" for r in results if ".package." in r.id):
            results.append(self._result("ssh.rules", "pending", "SSH 설정·방화벽 규칙", "패키지 준비 후 확인")); return results
        if any(r.id == "ssh.service.firewalld.active" and r.state == "change_required" for r in results):
            results.append(self._result("ssh.rules", "pending", "SSH 방화벽 규칙", "방화벽 서비스 시작 후 확인")); return results
        results.append(self._command("ssh.syntax", ["/usr/sbin/sshd", "-t"], failed="blocked"))
        config = self._run(["/usr/sbin/sshd", "-T"])
        ports = [line.split()[1] for line in config.stdout.splitlines() if line.startswith("port ")]
        if config.returncode != 0 or not ports:
            results.append(self._result("ssh.ports", "unknown", "현재 SSH 수신 포트", config.stderr, "sshd -T 결과 확인")); return results
        zone = self.config.ssh_zone
        if zone is None:
            route = self._run(["ip", "route", "show", "default"])
            words = route.stdout.split()
            if "dev" in words:
                interface = words[words.index("dev") + 1]
                guess = self._run(["firewall-cmd", "--get-zone-of-interface=" + interface])
                if guess.returncode == 0 and guess.stdout.strip() not in {"", "no zone"}:
                    zone = guess.stdout.strip()
            if not zone:
                default = self._run(["firewall-cmd", "--get-default-zone"])
                zone = default.stdout.strip() if default.returncode == 0 else None
        if not zone or not re.fullmatch(r"[a-zA-Z0-9_-]+", zone):
            results.append(self._result("ssh.zone", "unknown", "허용할 zone", zone, "구성에서 ssh_zone을 지정하세요.")); return results
        for port in ports:
            if not port.isdigit() or not 0 < int(port) < 65536:
                results.append(self._result("ssh.ports", "blocked", "유효한 포트", port)); continue
            for permanent in (False, True):
                argv = ["firewall-cmd"] + (["--permanent"] if permanent else []) + ["--zone=" + zone, "--query-port=" + port + "/tcp"]
                result = self._run(argv)
                state = "ok" if result.returncode == 0 else "change_required" if result.returncode == 1 else "unknown"
                results.append(self._result(f"ssh.firewall.{zone}.{port}.{'permanent' if permanent else 'runtime'}", state, "허용", result.stdout.strip() or result.stderr.strip(), "현재 SSH 포트의 누락 규칙만 추가"))
        return results

    def _docker(self):
        results = [self._package(name, "docker") for name in DOCKER_PACKAGES]
        for name in CONFLICT_PACKAGES:
            conflict = self._run(["rpm", "-q", name])
            if conflict.returncode == 0:
                action = "기존 컨테이너/볼륨 사용 여부 확인 후 충돌을 직접 해결하세요."
                if name.startswith("podman"):
                    action = ("일반 사용자와 sudo로 podman ps -a / podman volume ls를 확인하세요. "
                              f"미사용 장비이면 sudo dnf remove {name} 의 제거 목록을 검토한 뒤 정리하고 "
                              "간편 설정을 다시 실행하세요. 자동 삭제하지 않습니다.")
                results.append(self._result("docker.conflict." + name, "blocked", "충돌 패키지 없음", name, action))
            elif conflict.returncode not in (0, 1):
                results.append(self._result("docker.conflict." + name, "unknown", "패키지 조회", conflict.stderr))
        for service in ("docker", "containerd"):
            results.extend(self._service(service, "docker"))
        path = self.paths.at("/etc/docker/daemon.json")
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
            if not isinstance(data, dict):
                raise ValueError("daemon.json은 JSON object여야 합니다.")
            has_policy = "log-driver" in data or "log-opts" in data
            correct = path.is_file() and has_policy and not path.is_symlink() and path.stat().st_uid == self.owner and stat.S_IMODE(path.stat().st_mode) == 0o644
            results.append(self._result("docker.config", "ok" if correct else "change_required", "기존 로그 정책 보존 또는 local/20m/5", data, "기존 JSON을 보존해 누락 로그 정책만 추가"))
            if path.is_symlink():
                results.append(self._result("docker.config.symlink", "blocked", "일반 파일", str(path)))
        except (OSError, ValueError) as exc:
            data = None
            results.append(self._result("docker.config", "blocked", "유효한 Docker JSON", str(exc), "기존 설정을 검토하세요. 자동 덮어쓰지 않습니다."))
        daemon_active = next(r for r in results if r.id == "docker.service.docker.active")
        if daemon_active.state == "change_required":
            results.append(self._result("docker.runtime", "pending", "Docker 실행 상태", "서비스 시작 후 확인"))
        elif all(r.state == "ok" for r in results if ".package." in r.id):
            results.append(self._command("docker.info", ["docker", "info"], failed="error"))
            results.append(self._command("docker.compose", ["docker", "compose", "version"], failed="error"))
            if data and "log-driver" in data:
                runtime = self._run(["docker", "info", "--format", "{{json .LoggingDriver}}"])
                try:
                    actual = json.loads(runtime.stdout) if runtime.returncode == 0 else None
                    state = "unknown" if actual is None else "ok" if actual == data["log-driver"] else "pending"
                    results.append(self._result("docker.runtime.logging", state, data["log-driver"], actual, "유지보수 시간에 Docker 재시작 후 확인"))
                except ValueError:
                    results.append(self._result("docker.runtime.logging", "unknown", data["log-driver"], runtime.stderr or "응답 JSON 해석 실패"))
            runtime_config = self._restart_pending("docker.runtime.config_age", "docker.service", "/etc/docker/daemon.json")
            if runtime_config:
                results.append(runtime_config)
        return results

    def _gpu(self):
        results = []
        try:
            module = _legacy_module("nvidia_gpu")
            devices = module.gpu_devices(self.paths.at("/sys/bus/pci/devices"))
        except (OSError, ValueError) as exc:
            return [self._result("gpu.detection", "unknown", "PCI 장치 조회", str(exc), "sysfs 접근 확인")]
        if not devices:
            return [self._result("gpu", "not_applicable", "NVIDIA GPU 감지 시 자동 구성", "GPU 없음")]
        results.append(self._result("gpu.detection", "ok", "NVIDIA graphics device", [name for name, ids in devices]))
        smi = self._run(["nvidia-smi"])
        if smi.returncode == 0:
            results.append(self._result("gpu.driver", "ok", "nvidia-smi 성공", smi.stdout.strip()))
            package = self._package("nvidia-container-toolkit", "gpu")
            results.append(package)
            path = self.paths.at("/etc/docker/daemon.json")
            try:
                saved = json.loads(path.read_text()) if path.exists() else {}
                configured = isinstance(saved, dict) and isinstance(saved.get("runtimes"), dict) and "nvidia" in saved["runtimes"]
                results.append(self._result("gpu.runtime.config", "ok" if configured else "change_required", "nvidia runtime 저장", configured))
                running = self._run(["docker", "info", "--format", "{{json .Runtimes}}"])
                runtimes = json.loads(running.stdout) if running.returncode == 0 else None
                state = "unknown" if not isinstance(runtimes, dict) else "ok" if "nvidia" in runtimes else "pending" if configured else "change_required"
                results.append(self._result("gpu.runtime.live", state, "실행 중 NVIDIA runtime", runtimes, "유지보수 시간에 Docker 재시작 후 검사"))
            except (OSError, ValueError) as exc:
                results.append(self._result("gpu.runtime.config", "blocked", "유효한 Docker JSON", str(exc)))
            return results
        pending = self.paths.at("/var/lib/rocky-setup/nvidia-reboot-required")
        package = self._run(["rpm", "-q", "nvidia-open"])
        if pending.is_file() and package.returncode == 0:
            try:
                boot = self.paths.at("/proc/sys/kernel/random/boot_id").read_text().strip()
                same = pending.read_text().strip() == boot
                results.append(self._result("gpu.driver", "pending" if same else "blocked", "nvidia-smi 성공", "설치 후 재부팅 대기" if same else "재부팅 후에도 드라이버 동작 실패", "유지보수 재부팅 후 재검사" if same else "DKMS/커널/서명 상태 점검"))
            except OSError as exc:
                results.append(self._result("gpu.driver", "unknown", "부팅 대기 판정", str(exc)))
            return results
        packages = self._run(["rpm", "-qa", "--qf", "%{NAME}\n"])
        if packages.returncode != 0:
            return results + [self._result("gpu.driver", "unknown", "NVIDIA 패키지 조회", packages.stderr)]
        broken = re.search(r"^(?:nvidia-driver(?:$|-cuda(?:$|-)|-libs(?:$|-))|kmod-nvidia|nvidia-open|cuda-drivers)", packages.stdout, re.M)
        if self.paths.at("/var/log/nvidia-installer.log").exists() or broken:
            return results + [self._result("gpu.driver", "blocked", "기존 정상 드라이버 또는 미설치", "기존 설치가 있으나 nvidia-smi 실패", "기존 드라이버/커널 상태 확인. 자동 교체하지 않습니다.")]
        if self.paths.at("/sys/firmware/efi").is_dir():
            secure = self._run(["mokutil", "--sb-state"])
            if secure.returncode == 0 and "SecureBoot disabled" not in secure.stdout:
                return results + [self._result("gpu.secure_boot", "blocked", "SecureBoot disabled", secure.stdout.strip(), "서명/MOK 등록 절차 필요")]
            if secure.returncode != 0:
                results.append(self._result("gpu.secure_boot", "unknown", "Secure Boot 상태 확인", secure.stderr, "GPU 설치 모듈의 mokutil 준비 후 재확인"))
        table = self._run(["curl", "--fail", "--silent", "--show-error", "--location", "--max-time", "10", SUPPORT_URL], timeout=15)
        if table.returncode != 0:
            return results + [self._result("gpu.support", "unknown", "공식 open 모듈 지원 표", table.stderr, "네트워크/선행 curl 설치 후 재검사")]
        try:
            entries = module.supported_ids(table.stdout)
            unsupported = [name for name, ids in devices if (ids[0],) not in entries and ids not in entries]
            results.append(self._result("gpu.support", "blocked" if unsupported else "ok", "모든 GPU의 open 모듈 지원", unsupported or "지원 확인", "미지원·미확인 GPU는 별도 드라이버 검토"))
            if not unsupported:
                results.append(self._result("gpu.driver", "change_required", "정상 드라이버", "드라이버 미설치", "지원 드라이버 준비 후 재부팅"))
        except ValueError as exc:
            results.append(self._result("gpu.support", "unknown", "유효한 지원 표", str(exc)))
        return results

    def _dev_tools(self):
        return ([self._package(name, "dev_tools") for name in ("neovim", "btop", "tmux")]
                if self.config.dev_tools else [self._result("dev_tools", "not_applicable", False, False)])

    def _network(self):
        from .network import inspect_network
        return inspect_network(host=self.host, paths=self.paths).results

    def _hardware(self, apply=False):
        from .hardware import apply_hardware, inspect_hardware
        return (apply_hardware if apply else inspect_hardware)(self.host, self.paths)

    def _extra_display(self, apply=False):
        try:
            from .display import apply_display, inspect_display
        except ImportError as exc:
            if exc.name not in {"ala.display", "display"}:
                raise
            return [self._result("chrome.integration", "pending", "표시 복구 모듈", "ALA-04 구현 대기")]
        return (apply_display if apply else inspect_display)(self.config, self.host, self.paths)

    def inspect(self, *, workers=1) -> Report:
        if type(workers) is not int or workers < 1:
            raise ValueError('검사 작업 수는 양의 정수여야 합니다.')
        started = perf_counter()
        self.inspection = {'duration_ms': 0, 'stages': []}

        def elapsed(since):
            return round(max(0.0, (perf_counter() - since) * 1000), 3)

        def stage(spec):
            name, inspect = spec
            since = perf_counter()
            try:
                found = inspect()
            except (OSError, ValueError, configparser.Error) as exc:
                found = [self._result(name, 'unknown', '상태 조회', str(exc))]
            return found, {'id': name, 'duration_ms': elapsed(since)}

        try:
            since = perf_counter()
            error = platform_error(self.paths)
            self.inspection['stages'].append({'id': 'platform', 'duration_ms': elapsed(since)})
            if error:
                return Report([self._result('platform', 'blocked', 'Rocky Linux 10 x86_64', error)])
            results = [self._result('platform', 'ok', 'Rocky Linux 10 x86_64', '지원 환경')]
            since = perf_counter()
            _, account = self._account(self.config.user)
            self.inspection['stages'].append({'id': 'user', 'duration_ms': elapsed(since)})
            results.append(account)
            stages = [(name, getattr(self, '_' + name)) for name in
                      ('base', 'updates', 'hardware', 'network', 'shell', 'ssh', 'docker',
                       'gpu', 'dev_tools', 'display_policy', 'extra_display')]
            if workers == 1:
                inspected = map(stage, stages)
                for found, timing in inspected:
                    results.extend(found)
                    self.inspection['stages'].append(timing)
            else:
                with ThreadPoolExecutor(max_workers=min(workers, 4)) as pool:
                    # Submission is concurrent; consumption keeps the old report order.
                    pending = [pool.submit(stage, inspect) for inspect in stages]
                    for future in pending:
                        found, timing = future.result()
                        results.extend(found)
                        self.inspection['stages'].append(timing)
            return Report(results)
        finally:
            self.inspection['duration_ms'] = elapsed(started)

    def plan(self, *, workers=1) -> Report:
        return self.inspect(workers=workers)

    def apply(self, on_stage=None, on_substep=None) -> Report:
        extra = []

        def event(stage, state):
            if on_stage:
                on_stage(stage, state)

        def finish(additional=()):
            report = self.inspect()
            known = {r.id for r in report.results}
            report.results.extend(r for r in extra if r.id not in known)
            report.results.extend(additional)
            return report

        def run_module(stage, command):
            from .substeps import step_ids
            allowed = step_ids(stage)
            def output(line):
                if len(line) > 256:
                    return
                fields = line.rstrip('\r\n').split('\t')
                if (len(fields) == 3 and fields[0] == '__ALA_SUBSTEP__'
                        and fields[1] in allowed
                        and fields[2] in {'started', 'completed', 'pending', 'kept', 'failed'}
                        and on_substep is not None):
                    on_substep(stage, fields[1], fields[2])
            reported = getattr(self.host, 'execute_reported', None)
            if reported is not None:
                return reported(command, env=dict(os.environ, ALA_REPORT_PROGRESS='1'), on_line=output)
            return self.host.execute(command)

        error = platform_error(self.paths)
        if error:
            return Report([self._result("platform", "blocked", "Rocky Linux 10 x86_64", error)])
        _, account = self._account(self.config.user)
        if account.state != "ok":
            return Report([account])
        if self.paths.root != Path("/") and type(self.host) is Host:
            return Report([self._result("execution", "blocked", "실제 root 또는 모의 Host", "대체 경로에서 실제 시스템 설치 금지")])
        if self.paths.root == Path("/") and os.geteuid() != 0:
            return Report([self._result("execution", "blocked", "관리자 권한", "일반 사용자", "관리자 실행기를 통해 적용하세요.")])
        specs = [
            ("base", self._base, "setup-base.sh", ["--timezone", self.config.timezone]),
            ("update", self._updates, "setup-update-policy.sh", ["--with-display"] if self.config.display else []),
            ("hardware", self._hardware, None, []),
            ("shell", self._shell, "setup-bash.sh", ["--user", self.config.user] + (["--with-root"] if self.config.root_bash else [])),
            ("ssh", self._ssh, "setup-ssh.sh", ["--ssh-zone", self.config.ssh_zone] if self.config.ssh_zone else []),
            ("display", self._display_policy, "setup-display.sh", []),
            ("docker", self._docker, "setup-docker.sh", []),
            ("gpu", self._gpu, "setup-nvidia.sh", ["--auto"]),
            ("dev_tools", self._dev_tools, "setup-dev-tools.sh", []),
        ]
        for stage, inspect, script, args in specs:
            current = inspect()
            blocking = [r for r in current if r.state in {"blocked", "error", "unknown"} and not
                        (stage == "gpu" and r.id == "gpu.secure_boot" and "mokutil" in r.next_action)]
            if blocking:
                event(stage, "blocked")
                if stage == "hardware":
                    # Ambiguous device support does not make unrelated shell,
                    # Docker, or display policy changes dependent on that device.
                    # The final report still retains the blocked/unknown checks.
                    continue
                return finish()
            changed = any(r.state == "change_required" for r in current)
            if changed:
                event(stage, "started")
                if stage == "hardware":
                    outcome = self._hardware(apply=True)
                    if any(r.state == "error" for r in outcome):
                        event(stage, "failed")
                        report = self.inspect()
                        observed = {r.id for r in outcome}
                        report.results = [r for r in report.results if r.id not in observed] + outcome
                        return report
                    if any(r.state in {"blocked", "unknown", "change_required"} for r in outcome):
                        event(stage, "blocked")
                        continue
                    code = 10 if any(r.state == "pending" for r in outcome) else 0
                else:
                    code = run_module(stage, ["bash", str(SCRIPTS / script), *args])
                if stage in {"gpu", "hardware"} and code == 10:
                    event(stage, "pending")
                elif code != 0:
                    event(stage, "failed")
                    return finish([self._result(stage + ".apply", "error", "설치 모듈 성공", code, "단계 로그 확인 후 재실행")])
                else:
                    event(stage, "completed")
            else:
                event(stage, "kept" if all(r.state in {"ok", "not_applicable"} for r in current) else "pending")
            after = inspect()
            if stage == "gpu" and any(r.id == "gpu.driver" and r.state == "ok" for r in after):
                if any(r.state == "change_required" for r in after if r.id.startswith("gpu.runtime") or r.id.startswith("gpu.package")):
                    event("gpu_container", "started")
                    code = run_module("gpu_container", ["bash", str(SCRIPTS / "setup-nvidia-container.sh")])
                    if code != 0:
                        return finish([self._result("gpu_container.apply", "error", 0, code)])
                    event("gpu_container", "completed")
                    after = inspect()
            if any(r.state in {"error", "blocked", "unknown", "change_required"} for r in after):
                if stage == "hardware" and not any(r.state == "error" for r in after):
                    continue
                return finish()
            if stage == "display":
                # Registration needs the display prerequisites, not Docker/GPU.
                # Keep it in this same serial worker and before unrelated blockers.
                event("chrome", "started")
                extra = self._extra_display(apply=True)
                if any(r.state == "error" for r in extra):
                    state = "failed"
                elif any(r.state in {"blocked", "unknown", "change_required"} for r in extra):
                    state = "blocked"
                elif any(r.state == "pending" for r in extra):
                    state = "pending"
                else:
                    state = "completed"
                event("chrome", state)
        return finish()
