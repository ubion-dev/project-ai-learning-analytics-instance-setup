#!/usr/bin/env bash
# 역할: 자동 OS 업데이트 차단. --with-display로 GNOME 정책 추가. 수동 dnf는 유지.
set -Eeuo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"
with_display=false
while (($#)); do
  case "$1" in
    --with-display) with_display=true; shift ;;
    -h|--help) echo 'sudo bash setup-update-policy.sh [--with-display]'; exit 0 ;;
    *) die "알 수 없는 옵션: $1" ;;
  esac
done
require_rocky
ala_substep offline_check started
# Never interrupt a running RPM transaction or delete an already scheduled upgrade.
for pending in /system-update /etc/system-update; do
  [[ ! -e $pending && ! -L $pending ]] || die "$pending 존재: 예약된 오프라인 업데이트를 먼저 검토/취소하세요."
done
for unit in dnf-automatic.service dnf-automatic-download.service dnf-automatic-install.service \
  dnf-automatic-notifyonly.service packagekit-offline-update.service; do
  if systemctl is-active --quiet "$unit"; then
    die "$unit 실행 중입니다. 트랜잭션이 끝난 뒤 다시 실행하세요. 강제 종료하지 않습니다."
  fi
done
ala_substep offline_check completed
if $with_display; then
  ala_substep desktop_policy started
  prepare_kiosk_dconf
  policy=/etc/dconf/db/rocky_setup.d/10-updates
  locks=/etc/dconf/db/rocky_setup.d/locks/updates
  write_config "$policy" <<'POLICY'
[org/gnome/software]
allow-updates=false
download-updates=false
download-updates-notify=false
POLICY
  if $FILE_CHANGED; then DCONF_CHANGED=true; fi
  write_config "$locks" <<'LOCKS'
/org/gnome/software/allow-updates
/org/gnome/software/download-updates
/org/gnome/software/download-updates-notify
LOCKS
  if $FILE_CHANGED; then DCONF_CHANGED=true; fi
  update_dconf_if_changed
  ala_substep desktop_policy completed
fi
ala_substep units started
# Mask even absent units, so installing dnf-automatic later cannot enable them.
for timer in dnf-automatic.timer dnf-automatic-download.timer dnf-automatic-install.timer dnf-automatic-notifyonly.timer; do
  if systemctl is-active --quiet "$timer"; then systemctl stop "$timer"; fi
  ensure_masked "$timer"
done
ensure_masked dnf-automatic.service dnf-automatic-download.service dnf-automatic-install.service \
  dnf-automatic-notifyonly.service packagekit.service packagekit-offline-update.service
if systemctl is-active --quiet packagekit.service; then
  echo 'PackageKit이 현재 실행 중입니다. 설치 중일 수 있어 강제 종료하지 않았습니다.'
  echo '유지보수 시간에 재부팅해 기존 데몬을 종료한 뒤 정책을 검증하세요.'
fi
ala_substep units completed
echo 'DNF Automatic과 PackageKit 자동 업데이트 경로를 차단했습니다.'
if $with_display; then echo 'GNOME 정책 적용은 이 PC에서 로그아웃/로그인 필요.'; fi
echo '수동 sudo dnf upgrade는 계속 사용할 수 있습니다.'
echo '별도 cron, 사내 관리 에이전트, 사용자 정의 업데이트 스크립트는 별도 점검 대상입니다.'
