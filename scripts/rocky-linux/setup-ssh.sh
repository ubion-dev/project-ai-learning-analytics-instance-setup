#!/usr/bin/env bash
# 역할: 기존 SSH 인증 설정을 유지하며 서버·자동 시작·방화벽 포트 구성. 선택 단계.
set -Eeuo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"
zone=''
while (($#)); do
  case "$1" in
    --ssh-zone) [[ $# -ge 2 && -n $2 ]] || die '--ssh-zone 값이 필요합니다.'; zone=$2; shift 2 ;;
    -h|--help) echo 'sudo bash setup-ssh.sh [--ssh-zone ZONE]'; exit 0 ;;
    *) die "알 수 없는 옵션: $1" ;;
  esac
done
require_rocky
install_missing_packages openssh-server firewalld iproute
# Generate missing host keys before validation; preserve all existing keys.
if ! /usr/sbin/sshd -t; then systemctl start sshd-keygen.target; fi
# Check the existing SSH configuration and permit ALL configured listening ports.
/usr/sbin/sshd -t
ports=$(/usr/sbin/sshd -T | awk '$1 == "port" { print $2 }')
[[ -n $ports ]] || die 'SSH 수신 포트를 확인할 수 없습니다.'
firewall_running=false
if firewall-cmd --state >/dev/null 2>&1; then firewall_running=true; fi
if [[ -z $zone ]]; then
  # For remote runs, locate the interface used to return traffic to the SSH client.
  iface=''
  if [[ -n ${SSH_CONNECTION:-} ]]; then
    client=${SSH_CONNECTION%% *}
    iface=$(ip route get "$client" | awk '{for(i=1;i<NF;i++) if($i=="dev") {print $(i+1); exit}}')
  fi
  # sudo may not preserve SSH_CONNECTION; use the default-route NIC in that case.
  if [[ -z $iface ]]; then
    iface=$(ip route show default | awk '{for(i=1;i<NF;i++) if($i=="dev") {print $(i+1); exit}}')
  fi
  if [[ -n $iface ]]; then
    if $firewall_running; then
      zone=$(firewall-cmd --get-zone-of-interface="$iface" 2>/dev/null || true)
      [[ $zone != 'no zone' ]] || zone=''
    elif command -v nmcli >/dev/null; then
      connection=$(nmcli -g GENERAL.CONNECTION device show "$iface" 2>/dev/null || true)
      if [[ -n $connection && $connection != '--' ]]; then
        zone=$(nmcli -g connection.zone connection show "$connection" 2>/dev/null || true)
        [[ $zone != '--' ]] || zone=''
      fi
    fi
  fi
  if [[ -z $zone ]]; then
    if $firewall_running; then zone=$(firewall-cmd --get-default-zone)
    else zone=$(firewall-offline-cmd --get-default-zone); fi
  fi
fi
printf 'SSH 허용 zone: %s, 포트: %s\n' "$zone" "$ports"
for port in $ports; do
  if $firewall_running; then
    if ! firewall-cmd --zone="$zone" --query-port="$port/tcp" >/dev/null; then
      firewall-cmd --zone="$zone" --add-port="$port/tcp"
    fi
    if ! firewall-cmd --permanent --zone="$zone" --query-port="$port/tcp" >/dev/null; then
      firewall-cmd --permanent --zone="$zone" --add-port="$port/tcp"
    fi
  else
    if ! firewall-offline-cmd --zone="$zone" --query-port="$port/tcp" >/dev/null; then
      firewall-offline-cmd --zone="$zone" --add-port="$port/tcp"
    fi
  fi
done
# No reload of existing firewall rules or restart of the current SSH daemon.
ensure_service sshd
ensure_service firewalld
systemctl is-active --quiet sshd firewalld
systemctl is-enabled sshd firewalld
echo '기존 SSH 인증 설정과 SELinux 설정을 유지했습니다. 새 SSH 연결로 접속을 확인하세요.'
