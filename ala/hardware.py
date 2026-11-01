"""Conservative preparation of packaged support for the hardware that is present.

Binding proves only that the kernel attached a driver. It does not prove network
connectivity, device performance, or firmware health. Never detach working devices
or guess a vendor-specific USB interface's purpose here.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .model import CheckResult
from .system import Host, Paths, atomic_json

MARKER = "/var/lib/ala/hardware-reboot-required.json"
DNF_OPTIONS = (
    "--disablerepo=*", "--enablerepo=baseos", "--enablerepo=appstream",
    "--setopt=install_weak_deps=False", "--exclude=kernel", "--exclude=kernel-core",
    "--exclude=kernel-debug*", "--exclude=kernel-uki*", "--exclude=rocky-release*",
    "--exclude=rocky-repos*", "--exclude=rocky-gpg-keys*",
)
_KERNEL = re.compile(r"[0-9][A-Za-z0-9._+~-]{1,180}\.x86_64\Z")
_PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+~-]{0,250}\Z")
_DRIVER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*\Z")
_NETWORK_DRIVERS = ("rtl", "rtw", "r815", "ath9k", "ath10k", "ath11k", "ath12k",
                    "mt7", "mt76", "brcmfmac", "cdc_ether", "cdc_ncm", "cdc_mbim",
                    "cdc_eem", "cdc_subset", "rndis_host", "asix", "ax88179")


@dataclass
class Device:
    bus: str
    name: str
    vendor: str
    product: str
    kind: str
    driver: str | None
    supported_scope: bool = True
    override: str = ""
    subclass: str = ""
    protocol: str = ""
    usb_network: bool = False

    @property
    def id(self):
        return f"hardware.device.{self.bus}.{self.name}"

    @property
    def value(self):
        return {"bus": self.bus, "address": self.name, "vendor": self.vendor,
                "device": self.product, "class": self.kind, "driver": self.driver,
                "driver_override": self.override or None, "subclass": self.subclass or None,
                "protocol": self.protocol or None}

    @property
    def automatic(self):
        if self.bus == "pci":
            return self.kind.startswith(("02", "03")) and not (self.vendor == "10de" and self.kind.startswith("03"))
        return self.usb_network


def _hex(path, digits):
    value = path.read_text().strip().lower().removeprefix("0x")
    if not re.fullmatch(r"[0-9a-f]{" + str(digits) + r"}", value):
        raise ValueError(f"잘못된 sysfs 식별자: {path.name}")
    return value


def _driver(path):
    file = path / "driver"
    try:
        info = file.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        value = Path(os.readlink(file)).name
        if not _DRIVER.fullmatch(value):
            raise ValueError("잘못된 sysfs driver 이름")
        return value
    raise ValueError("sysfs driver가 심볼릭 링크가 아닙니다.")


def _devices(paths):
    devices, results = [], []
    for bus in ("pci", "usb"):
        root = paths.at(f"/sys/bus/{bus}/devices")
        try:
            entries = sorted(root.iterdir())
        except OSError as exc:
            results.append(CheckResult(f"hardware.inventory.{bus}", "unknown", "읽을 수 있는 sysfs", str(exc), "sysfs 마운트·접근 권한 확인"))
            continue
        for entry in entries:
            try:
                if bus == "pci":
                    kind = _hex(entry / "class", 6)
                    if kind[:2] not in {"01", "02", "03", "04"}:
                        continue
                    vendor, product = _hex(entry / "vendor", 4), _hex(entry / "device", 4)
                    driver = _driver(entry)
                    try:
                        override = (entry / "driver_override").read_text().strip()
                    except FileNotFoundError:
                        override = ""
                    if override in {"(null)", "(none)"}:
                        override = ""
                    devices.append(Device(bus, entry.name, vendor, product, kind, driver, override=override))
                elif ":" in entry.name:  # USB interfaces bind drivers; whole devices also represent hubs.
                    kind = _hex(entry / "bInterfaceClass", 2)
                    subclass = _hex(entry / "bInterfaceSubClass", 2)
                    protocol = _hex(entry / "bInterfaceProtocol", 2)
                    address = entry.name.split(":", 1)[0]
                    root_hub = re.fullmatch(r"([0-9]+)-0", address)
                    device = root / ("usb" + root_hub.group(1) if root_hub else address)
                    vendor, product = _hex(device / "idVendor", 4), _hex(device / "idProduct", 4)
                    driver = _driver(entry)
                    scoped = kind in {"01", "02", "03", "08", "0a", "0e", "e0"} or bool(driver and driver.startswith(_NETWORK_DRIVERS))
                    network = ((kind == "02" and subclass in {"06", "0d", "0e"}) or
                               (kind == "e0" and subclass == "01" and protocol == "03") or
                               bool(driver and driver.startswith(_NETWORK_DRIVERS)))
                    devices.append(Device(bus, entry.name, vendor, product, kind, driver, scoped,
                                          subclass=subclass, protocol=protocol, usb_network=network))
            except (OSError, ValueError) as exc:
                results.append(CheckResult(f"hardware.inventory.{bus}.{entry.name}", "unknown", "유효한 장치 정보", str(exc), "장치 제거 여부·sysfs 접근 권한 확인"))
    # A composite device may expose both Ethernet and serial CDC data. Without
    # parsing union/IAD descriptors, another interface cannot establish its role.
    # Only a data interface's own recognized network driver is evidence here.
    return devices, results


def _firmware(device):
    """Only confirmed Rocky package families; this is not a chipset support table."""
    if device.override or device.driver in {"vfio-pci", "vfio_pci", "pci-stub", "pci_stub"}:
        return ()
    if device.driver == "brcmfmac":
        return ("brcmfmac-firmware",)
    if device.bus == "pci":
        if device.kind.startswith("0280") and device.vendor == "8086":
            return ("iwlwifi-dvm-firmware", "iwlwifi-mvm-firmware")
        if not device.kind.startswith("02"):
            return ()
        return {"10ec": ("realtek-firmware",), "168c": ("atheros-firmware",),
                "14c3": ("mt7xxx-firmware",)}.get(device.vendor, ())
    if not device.usb_network:
        return ()
    return {"0bda": ("realtek-firmware",), "0cf3": ("atheros-firmware",),
            "0e8d": ("mt7xxx-firmware",)}.get(device.vendor, ())


def _read_marker(paths):
    path = paths.at(MARKER)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise ValueError("드라이버 재부팅 기록이 심볼릭 링크입니다.")
    owner = 0 if paths.root == Path("/") else os.geteuid()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("드라이버 재부팅 기록 소유자·권한이 잘못되었습니다.")
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or value.get("schema_version") != 1 or
            not isinstance(value.get("boot_id"), str) or not isinstance(value.get("kernel_release"), str) or
            not _KERNEL.fullmatch(value["kernel_release"]) or
            not isinstance(value.get("packages"), list) or not all(isinstance(p, str) and _PACKAGE.fullmatch(p) for p in value["packages"]) or
            not isinstance(value.get("devices"), list) or not all(isinstance(p, str) and p.startswith("hardware.device.") for p in value["devices"])):
        raise ValueError("드라이버 재부팅 기록 형식이 잘못되었습니다.")
    uuid.UUID(value["boot_id"])
    return value


def _inspect(host, paths):
    devices, results = _devices(paths)
    relevant = [d for d in devices if d.automatic]
    marker = None
    try:
        marker = _read_marker(paths)
    except (OSError, ValueError, TypeError) as exc:
        results.append(CheckResult("hardware.reboot", "blocked", "유효한 재부팅 기록", str(exc), "기록 파일을 직접 검토하세요."))
    if not devices and not results and not marker:
        return [CheckResult("hardware", "not_applicable", "관심 장치 조사", "해당 장치 없음")], [], [], None, None, None

    unbound = [d for d in relevant if not d.driver and not d.override]
    packages = sorted({p for device in relevant for p in _firmware(device)})
    kernel, boot = None, None
    if unbound or packages or marker:
        command = host.run(["uname", "-r"])
        kernel = command.stdout.strip()
        if command.returncode or not _KERNEL.fullmatch(kernel):
            results.append(CheckResult("hardware.kernel", "unknown", "실행 커널의 유효한 x86_64 버전", command.stderr.strip() or kernel, "uname -r과 실행 커널 패키지 확인"))
            kernel = None
        else:
            results.append(CheckResult("hardware.kernel", "ok", "실행 커널 유지", kernel))
        try:
            boot = paths.at("/proc/sys/kernel/random/boot_id").read_text().strip()
            uuid.UUID(boot)
        except (OSError, ValueError) as exc:
            results.append(CheckResult("hardware.boot", "unknown", "현재 부팅 ID", str(exc), "procfs 접근 상태 확인"))
            boot = None
    if unbound and kernel:
        packages += [f"kernel-modules-{kernel}", f"kernel-modules-extra-{kernel}"]
    missing = []
    for package in packages:
        command = host.run(["rpm", "-q", package])
        state = "ok" if command.returncode == 0 else "change_required" if command.returncode == 1 and not command.stderr.strip() else "unknown"
        if state == "change_required":
            missing.append(package)
        results.append(CheckResult("hardware.package." + package, state, "설치됨", command.stdout.strip() or command.stderr.strip(), "공식 Rocky 저장소에서 누락 패키지만 준비"))
    same_boot = bool(marker and boot and marker["boot_id"] == boot)
    for device in devices:
        if not device.supported_scope:
            state, action = "not_applicable", "용도를 추정하지 않습니다. 필요하면 장치 ID로 별도 지원 확인"
        elif device.bus == "pci" and device.vendor == "10de" and device.kind.startswith("03"):
            state, action = "not_applicable", "NVIDIA 전용 자동 감지 단계에서 검사"
        elif device.driver:
            state, action = "ok", "현재 드라이버 유지; 연결·성능은 별도 확인"
        elif not device.automatic:
            state, action = "not_applicable", "사용 용도가 미확정된 장치입니다. 자동 모듈 설치·바인딩 대상에서 제외"
        elif device.override:
            state, action = "blocked", "기존 driver_override 설정과 장치 용도를 확인하세요. 자동 변경하지 않습니다."
        elif same_boot and device.id in marker["devices"]:
            state, action = "pending", "새 모듈·펌웨어 준비 후 재부팅 대기; 현재 연결은 유지"
        elif missing and kernel:
            state, action = "change_required", "공식 패키지 보완 후 재부팅·재검사"
        else:
            state, action = "blocked", "장치 용도·커널 지원·blacklist·vfio 설정을 확인하세요. 드라이버 고장으로 단정하지 않습니다."
        results.append(CheckResult(device.id, state, "기존 드라이버 유지 또는 공식 지원 준비", device.value, action,
                                   "driver binding은 실제 장치 동작이나 인터넷 연결 성공을 검증하지 않습니다."))
    if marker and boot:
        results.append(CheckResult("hardware.reboot", "pending" if same_boot else "ok", "준비 후 새로운 부팅에서 재검사",
                                   {"prepared_boot": marker["boot_id"], "current_boot": boot, "packages": marker["packages"]},
                                   "유지보수 시간에 재부팅 후 ala check" if same_boot else "현재 장치 검사 결과 확인"))
    return results, missing, [d.id for d in relevant], kernel, boot, marker


def inspect_hardware(host: Host, paths: Paths) -> list[CheckResult]:
    """Read-only inventory and package checks; never refresh DNF metadata."""
    return _inspect(host, paths)[0]


def apply_hardware(host: Host, paths: Paths) -> list[CheckResult]:
    """Recheck immediately before installing; do not reload active drivers."""
    if (paths.root != Path("/") and type(host) is Host) or (paths.root == Path("/") and os.geteuid() != 0):
        return [CheckResult("hardware.apply", "blocked", "관리자 실행기 또는 모의 Host", "허용되지 않는 적용 경로")]
    results, missing, devices, kernel, boot, marker = _inspect(host, paths)
    if not missing or any(r.state in {"blocked", "unknown", "error"} for r in results):
        return results
    if not kernel or not boot:
        return results + [CheckResult("hardware.apply", "blocked", "실행 커널·부팅 ID 확인", "조회 실패")]
    try:
        code = host.execute(["dnf", "install", "-y", *DNF_OPTIONS, *missing])
    except OSError as exc:
        return inspect_hardware(host, paths) + [CheckResult("hardware.apply", "error", "공식 패키지 설치 성공", str(exc), "dnf 실행·권한·네트워크 확인")]
    # Record even a partial RPM transaction if any requested missing package was installed.
    installed = [p for p in missing if host.run(["rpm", "-q", p]).returncode == 0]
    if installed:
        try:
            previous = marker["packages"] if marker and marker["boot_id"] == boot else []
            atomic_json(paths.at(MARKER), {"schema_version": 1, "boot_id": boot, "kernel_release": kernel,
                        "packages": sorted(set(previous + installed)), "devices": devices}, 0o600)
        except OSError as exc:
            return inspect_hardware(host, paths) + [CheckResult("hardware.apply", "error", "설치 후 재부팅 기록 저장", str(exc), "재부팅 기록 권한 확인 후 재검사")]
    after = inspect_hardware(host, paths)
    if code or len(installed) != len(missing):
        after.append(CheckResult("hardware.apply", "error", "요청한 누락 패키지 설치 성공", code,
                                 "공식 저장소·현재 커널 버전의 패키지 가용성을 확인하세요. 커널 업그레이드로 우회하지 않습니다."))
    return after
