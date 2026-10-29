#!/usr/bin/env bash
# 역할: 읽기 전용 기대 상태 검사. --chrome/--chrome-url로 실제 실행 인자까지 검사.
set -Eeuo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"
gpu=false; ssh=false; display_policy=false; update_policy=false; chrome=false
account=${SUDO_USER:-}; expected_url=''
while (($#)); do
  case "$1" in
    --ssh) ssh=true; shift ;;
    --update-policy) update_policy=true; shift ;;
    --display-policy|--kiosk) display_policy=true; update_policy=true; shift ;;
    --gpu) gpu=true; shift ;;
    --chrome) chrome=true; shift ;;
    --user|--chrome-url)
      [[ $# -ge 2 && -n $2 && $2 != --* ]] || die "$1 값이 필요합니다."
      if [[ $1 == --user ]]; then account=$2
      else expected_url=$2; chrome=true; fi
      shift 2 ;;
    -h|--help) echo 'sudo bash verify.sh [--gpu] [--ssh] [--update-policy] [--display-policy] [--chrome] [--chrome-url URL] [--user NAME]'; exit 0 ;;
    *) die "알 수 없는 옵션: $1" ;;
  esac
done
require_rocky
passed=0; failed=0; pending_count=0
ok() { passed=$((passed + 1)); printf '[OK] %s\n' "$1"; }
fail() { failed=$((failed + 1)); printf '[FAIL] %s\n' "$1" >&2; }
check() {
  local description=$1; shift
  if "$@"; then ok "$description"; else fail "$description"; fi
}
persistently_enabled() {
  local actual
  actual=$(systemctl is-enabled "$1" 2>/dev/null || true)
  [[ $actual == enabled ]] || { printf '기대: enabled / 현재: %s\n' "$actual"; return 1; }
}
verify_chrome_runtime() {
  local uid runtime_path
  [[ -n $account && $account != root ]] || { echo '화면 사용자 지정 필요: --user NAME' >&2; return 1; }
  uid=$(id -u "$account") || return 1
  runtime_path=$(loginctl show-user "$uid" --property=RuntimePath --value) || return 1
  [[ -n $runtime_path && -d $runtime_path ]] || { echo '로그인된 사용자의 runtime 디렉터리가 없습니다.' >&2; return 1; }
  [[ -x /usr/local/bin/chrome-did ]] && cmp -s "$ROCKY_SCRIPT_DIR/bin/chrome-did" /usr/local/bin/chrome-did || {
    echo '먼저 setup-base.sh로 최신 chrome-did 명령을 준비하세요.' >&2; return 1;
  }
  local runtime_args=(check)
  [[ -z $expected_url ]] || runtime_args+=("$expected_url")
  runuser -u "$account" -- env XDG_RUNTIME_DIR="$runtime_path" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime_path/bus" \
    /usr/local/bin/chrome-did "${runtime_args[@]}"
}
services=(chronyd docker containerd)
if $ssh; then services+=(sshd firewalld); fi
for service in "${services[@]}"; do
  check "$service 실행" systemctl is-active --quiet "$service"
  check "$service 영구 부팅 자동 시작" persistently_enabled "$service"
done
if $ssh; then check 'SSH 설정 문법' /usr/sbin/sshd -t; fi
check 'Docker 응답' docker info
check 'Compose plugin' docker compose version
check '커널 모듈 도구 패키지' rpm -q kmod
check 'Chrome stable 패키지' rpm -q google-chrome-stable
check 'Chrome 버전' google-chrome-stable --version
check 'Chrome 번역·기본 브라우저·소개 정책' python3 "$ROCKY_SCRIPT_DIR/lib/chrome_policy.py" --check
check 'SSH용 Chrome 실행 명령' test -x /usr/local/bin/chrome-did
check 'Chrome 실행 명령이 현재 스크립트와 일치' cmp -s "$ROCKY_SCRIPT_DIR/bin/chrome-did" /usr/local/bin/chrome-did
check '시간 동기화 현황 조회' timedatectl status
if firewall-cmd --state >/dev/null 2>&1; then check '방화벽 zone 현황 조회' firewall-cmd --get-active-zones
else echo '안내: firewalld 비활성 상태입니다. 서비스 공개 전 정책을 확인하세요.'; fi
check '수신 포트 현황 조회' ss -lntp
check '디스크 현황 조회' df -h / /var
if $gpu; then
  check '호스트 GPU' nvidia-smi
  check 'Docker NVIDIA runtime' bash -c 'docker info --format "{{json .Runtimes}}" | grep -q '\''"nvidia"'\'''
fi
if $update_policy; then
  for unit in dnf-automatic.timer dnf-automatic-download.timer dnf-automatic-install.timer dnf-automatic-notifyonly.timer \
    dnf-automatic.service dnf-automatic-download.service dnf-automatic-install.service dnf-automatic-notifyonly.service \
    packagekit.service packagekit-offline-update.service; do
    check "$unit 차단" bash -c '[[ $(systemctl is-enabled "$1" 2>/dev/null || true) == masked ]]' _ "$unit"
    check "$unit 비활성" bash -c '! systemctl is-active --quiet "$1"' _ "$unit"
  done
  for pending in /system-update /etc/system-update; do
    if [[ -e $pending || -L $pending ]]; then fail "예약된 업데이트: $pending"; fi
  done
fi
if $display_policy; then
  for unit in sleep.target suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target; do
    check "$unit 차단" bash -c '[[ $(systemctl is-enabled "$1" 2>/dev/null || true) == masked ]]' _ "$unit"
  done
  check 'GNOME 시스템 정책 DB' test -f /etc/dconf/db/rocky_setup
  check 'GNOME 시스템 정책 프로필' grep -Fqx 'system-db:rocky_setup' /etc/dconf/profile/user
  check '화면 자동 꺼짐 정책' grep -Fqx 'idle-delay=uint32 0' /etc/dconf/db/rocky_setup.d/00-display
  check 'GNOME 업데이트 차단 정책' grep -Fqx 'allow-updates=false' /etc/dconf/db/rocky_setup.d/10-updates
  echo '현재 GUI 세션은 재로그인 후 실제 gsettings 값과 화면 유지 여부를 확인하세요.'
fi
if $chrome; then
  runtime_status=0
  verify_chrome_runtime || runtime_status=$?
  case "$runtime_status" in
    0) ok '실행 중 DID Chrome의 기대 상태' ;;
    2) pending_count=$((pending_count + 1)); echo '[대기] DID Chrome 실행 후 재검사 필요' ;;
    *) fail '실행 중 DID Chrome의 기대 상태' ;;
  esac
else
  echo 'DID 실제 실행 검사 미선택. --chrome 또는 --chrome-url URL로 추가할 수 있습니다.'
fi
printf '\n검사 결과: 정상 %d / 실패 %d / 대기 %d\n' "$passed" "$failed" "$pending_count"
echo '이 검사는 외부 SSH/RDP 접속, 재부팅 후 앱 복구, GPU 컨테이너 실동작을 대신하지 않습니다.'
if ((failed)); then echo '실패한 항목을 확인하고 해당 설정 스크립트를 다시 실행하세요.'; exit 1; fi
if ((pending_count)); then exit 2; fi
exit 0
