#!/usr/bin/env bash
# 역할: NVIDIA GPU 자동 감지·드라이버 준비. 종료 코드 10은 설치 후 재부팅 대기.
set -Eeuo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"
install_driver=false
while (($#)); do
  case "$1" in
    --check) shift ;;
    --auto|--install-open) install_driver=true; shift ;;
    --confirm-open-compatible) shift ;; # Compatibility with older documented commands.
    -h|--help)
      echo 'sudo bash setup-nvidia.sh --check'
      echo 'sudo bash setup-nvidia.sh --auto'
      exit 0 ;;
    *) die "알 수 없는 옵션: $1" ;;
  esac
done
require_rocky
if ! nvidia_gpu_present; then
  echo '[건너뜀] NVIDIA GPU가 없습니다. GPU 패키지를 설치하지 않습니다.'
  exit 0
fi
[[ $(uname -m) == x86_64 ]] || die '현재 GPU 설치 스크립트는 x86_64 전용입니다.'
printf '현재 커널: %s\n' "$(uname -r)"
pending=/var/lib/rocky-setup/nvidia-reboot-required
if command -v nvidia-smi >/dev/null && nvidia-smi; then
  if $install_driver && [[ -f $pending ]]; then rm -f "$pending"; fi
  echo '[유지] 현재 NVIDIA 드라이버가 정상 동작합니다.'
  exit 0
fi
if ! $install_driver; then
  echo 'NVIDIA GPU가 감지되었고 드라이버는 아직 정상 동작하지 않습니다. --auto로 자동 준비할 수 있습니다.'
  exit 0
fi
boot_id=$(cat /proc/sys/kernel/random/boot_id)
if [[ -f $pending ]] && ! rpm -q nvidia-open >/dev/null 2>&1; then
  rm -f "$pending"
fi
if [[ -f $pending ]]; then
  if [[ $(cat "$pending") == "$boot_id" ]]; then
    echo '[대기] NVIDIA 패키지는 이미 설치되었습니다. 재부팅 후 같은 setup.sh 명령을 다시 실행하세요.'
    exit 10
  fi
  die '재부팅 후에도 NVIDIA 드라이버가 동작하지 않습니다. DKMS/커널/서명 상태를 확인하세요.'
fi
if [[ -f /var/log/nvidia-installer.log ]]; then
  die '.run 방식 설치 흔적이 있습니다. 기존 설치 상태를 확인한 뒤 패키지 방식으로 전환하세요.'
fi
installed_packages=$(rpm -qa --qf '%{NAME}\n')
if grep -Eq '^(nvidia-driver($|-cuda($|-)|-libs($|-))|kmod-nvidia|nvidia-open|cuda-drivers)' <<< "$installed_packages"; then
  die '기존 NVIDIA 패키지가 있지만 드라이버가 동작하지 않습니다. 재부팅/커널 상태를 먼저 점검하세요.'
fi
install_missing_packages dnf-plugins-core curl ca-certificates mokutil
if [[ -d /sys/firmware/efi ]]; then
  secure_boot=$(LC_ALL=C mokutil --sb-state 2>&1) || die 'Secure Boot 상태를 확인할 수 없습니다.'
  [[ $secure_boot == *'SecureBoot disabled'* ]] || \
    die 'Secure Boot가 켜져 있습니다. DKMS 서명/MOK 등록 절차가 필요합니다.'
fi
# Data only: never execute downloaded code or assume that every NVIDIA PCI device supports open modules.
support_table=$(mktemp)
trap 'rm -f "$support_table"' EXIT
curl --fail --silent --show-error --location \
  https://raw.githubusercontent.com/NVIDIA/open-gpu-kernel-modules/main/README.md -o "$support_table"
if ! python3 "$ROCKY_SCRIPT_DIR/lib/nvidia_gpu.py" --support-table "$support_table"; then
  die 'NVIDIA open 드라이버 지원이 확인되지 않았습니다. 레거시/미확인 GPU는 별도 드라이버가 필요합니다.'
fi
ensure_repo_enabled crb
install_missing_packages epel-release
install_missing_packages gcc make dkms kernel-headers
if [[ ! -d /usr/src/kernels/$(uname -r) ]]; then
  install_missing_packages "kernel-devel-$(uname -r)"
fi
[[ -d /usr/src/kernels/$(uname -r) ]] || \
  die '실행 커널용 개발 파일이 없습니다. kernel-devel 패키지의 파일과 저장소 지원 버전을 확인하세요.'
add_repo https://developer.download.nvidia.com/compute/cuda/repos/rhel10/x86_64/cuda-rhel10.repo \
  /etc/yum.repos.d/cuda-rhel10.repo
# Full desktop + compute driver for workloads alongside Chrome/GNOME.
install_missing_packages nvidia-open
rpm -q nvidia-open >/dev/null || die 'NVIDIA 드라이버 패키지 설치를 확인할 수 없습니다.'
install -d -m 0755 /var/lib/rocky-setup
printf '%s\n' "$boot_id" > "$pending"
chmod 0600 "$pending"
echo '[대기] NVIDIA 드라이버 설치 완료. 자동 재부팅하지 않습니다.'
echo '유지보수 시간에 재부팅 후 같은 setup.sh 명령을 다시 실행하면 컨테이너 GPU 연결을 이어서 검사합니다.'
exit 10
