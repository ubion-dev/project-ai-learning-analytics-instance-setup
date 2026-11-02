#!/usr/bin/env bash
# 역할: 이 서버 자체의 GNOME 화면·잠금 및 시스템 절전 방지. --with-display 선택 단계.
set -Eeuo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"
if (($#)); then
  case "$1" in
    -h|--help) echo 'sudo bash setup-display.sh'; exit 0 ;;
    *) die "알 수 없는 옵션: $1" ;;
  esac
fi
require_rocky
ala_substep schema_check started
command -v gsettings >/dev/null || die 'GNOME 데스크톱이 필요합니다.'
schemas=$(gsettings list-schemas)
for schema in org.gnome.desktop.session org.gnome.desktop.screensaver org.gnome.settings-daemon.plugins.power; do
  grep -Fxq "$schema" <<< "$schemas" || die "GNOME schema가 없습니다: $schema"
done
ala_substep schema_check completed
ala_substep desktop_policy started
prepare_kiosk_dconf
policy=/etc/dconf/db/rocky_setup.d/00-display
locks=/etc/dconf/db/rocky_setup.d/locks/display
write_config "$policy" <<'POLICY'
[org/gnome/desktop/session]
idle-delay=uint32 0

[org/gnome/desktop/screensaver]
lock-enabled=false

[org/gnome/settings-daemon/plugins/power]
idle-dim=false
sleep-inactive-ac-type='nothing'
sleep-inactive-battery-type='nothing'
POLICY
if $FILE_CHANGED; then DCONF_CHANGED=true; fi
write_config "$locks" <<'LOCKS'
/org/gnome/desktop/session/idle-delay
/org/gnome/desktop/screensaver/lock-enabled
/org/gnome/settings-daemon/plugins/power/idle-dim
/org/gnome/settings-daemon/plugins/power/sleep-inactive-ac-type
/org/gnome/settings-daemon/plugins/power/sleep-inactive-battery-type
LOCKS
if $FILE_CHANGED; then DCONF_CHANGED=true; fi
update_dconf_if_changed
ala_substep desktop_policy completed
ala_substep system_sleep started
# Do not stop logind or the current graphical session.
mkdir -p /etc/systemd/logind.conf.d
write_config /etc/systemd/logind.conf.d/60-rocky-kiosk.conf <<'LOGIND'
[Login]
IdleAction=ignore
LOGIND
ensure_masked sleep.target suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target
ala_substep system_sleep completed
echo '화면 자동 꺼짐/자동 잠금/자동 절전 방지 정책 저장 완료.'
echo 'GNOME 프로필 적용은 로그아웃 후 로그인하세요. logind 설정은 다음 재부팅부터 적용됩니다.'
echo '절전 target은 즉시 차단됩니다. 수동 절전/최대절전도 차단하며, 수동 화면 잠금은 유지합니다.'
