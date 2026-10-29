#!/usr/bin/env bash
# 역할: 기본 패키지·Chrome 안내 정책·SSH 실행 도구·시간 동기화. 전체 업데이트는 --upgrade 선택.
set -Eeuo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"
upgrade=false; timezone='Asia/Seoul'
while (($#)); do
  case "$1" in
    --upgrade) upgrade=true; shift ;;
    --timezone) [[ $# -ge 2 && -n $2 ]] || die '--timezone 값이 필요합니다.'; timezone=$2; shift 2 ;;
    -h|--help) echo 'sudo bash setup-base.sh [--upgrade] [--timezone Asia/Seoul]'; exit 0 ;;
    *) die "알 수 없는 옵션: $1" ;;
  esac
done
require_rocky
install_missing_packages dnf-plugins-core git curl ca-certificates bash-completion \
  firewalld chrony python3 iproute
if rpm -q google-chrome-stable >/dev/null 2>&1; then
  echo '[유지] 패키지: google-chrome-stable'
else
  [[ $(uname -m) == x86_64 ]] || die 'Chrome 신규 설치는 x86_64 공식 RPM을 사용합니다. 이 아키텍처는 별도 설치가 필요합니다.'
  rpm --import https://dl.google.com/linux/linux_signing_key.pub
  dnf install -y --setopt=localpkg_gpgcheck=1 --setopt=gpgcheck=1 \
    https://dl.google.com/linux/direct/google-chrome-stable_current_x86_64.rpm
fi
command -v google-chrome-stable >/dev/null || die 'Chrome 패키지의 실행 파일이 없습니다. 기존 설치 상태를 확인하세요.'
google-chrome-stable --version
python3 "$ROCKY_SCRIPT_DIR/lib/chrome_policy.py" --apply
mkdir -p /usr/local/bin
install_if_changed "$ROCKY_SCRIPT_DIR/bin/chrome-did" /usr/local/bin/chrome-did 0755
echo 'Chrome 안내 정책과 SSH용 chrome-did 명령 준비 완료. 실행 중 Chrome은 자동 재시작하지 않습니다.'
if $upgrade; then dnf upgrade --refresh -y; fi
if [[ $(timedatectl show --property=Timezone --value) != "$timezone" ]]; then timedatectl set-timezone "$timezone"; fi
ensure_service chronyd
systemctl is-active --quiet chronyd
echo '기본 환경 설정 완료. SSH와 기존 방화벽 서비스 상태/규칙은 변경하지 않았습니다.'
report_reboot
