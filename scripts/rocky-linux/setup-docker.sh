#!/usr/bin/env bash
# 역할: Docker·Compose 설치, 자동 시작·로그 정책 설정. 재시작은 명시 옵션.
set -Eeuo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"
restart=false
while (($#)); do
  case "$1" in
    --restart-docker) restart=true; shift ;;
    -h|--help) echo 'sudo bash setup-docker.sh [--restart-docker]'; exit 0 ;;
    *) die "알 수 없는 옵션: $1" ;;
  esac
done
require_rocky
ala_substep conflicts started
# Do not silently uninstall a working Podman/container runtime environment.
conflicts=()
for package in podman podman-docker runc docker docker-common docker-engine; do
  if rpm -q "$package" >/dev/null 2>&1; then
    conflicts+=("$package")
  fi
done
if ((${#conflicts[@]})); then
  printf 'Docker 설치 전 확인할 충돌 후보 패키지: %s\n' "${conflicts[*]}" >&2
  if [[ " ${conflicts[*]} " == *' podman '* || " ${conflicts[*]} " == *' podman-docker '* ]]; then
    echo '기존 컨테이너 확인: podman ps -a / sudo podman ps -a' >&2
    echo '기존 볼륨 확인: podman volume ls / sudo podman volume ls' >&2
  fi
  printf '사용하지 않는 패키지임을 확인한 경우: sudo dnf remove %s\n' "${conflicts[*]}" >&2
  die '패키지를 자동 삭제하지 않았습니다. 제거 목록을 확인해 충돌을 해결한 뒤 같은 설치 명령을 다시 실행하세요.'
fi
ala_substep conflicts completed
ala_substep repository started
install_missing_packages curl ca-certificates dnf-plugins-core python3
add_repo https://download.docker.com/linux/rhel/docker-ce.repo /etc/yum.repos.d/docker-ce.repo
ala_substep repository completed
ala_substep packages started
install_missing_packages docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
ala_substep packages completed
ala_substep logging started
mkdir -p /etc/docker
candidate=$(mktemp /etc/docker/.daemon.json.XXXXXX)
trap 'rm -f "$candidate"' EXIT
# Preserve all existing settings. Configure rotation only if no logging policy exists.
python3 - /etc/docker/daemon.json "$candidate" <<'PY'
import json, pathlib, sys
source, target = map(pathlib.Path, sys.argv[1:])
data = json.loads(source.read_text()) if source.exists() else {}
if not isinstance(data, dict):
    raise SystemExit('daemon.json은 JSON object여야 합니다.')
if 'log-driver' not in data and 'log-opts' not in data:
    data.update({'log-driver': 'local', 'log-opts': {'max-size': '20m', 'max-file': '5'}})
else:
    print('기존 Docker 로그 정책을 유지합니다. 크기 제한을 별도로 확인하세요.')
target.write_text(json.dumps(data, indent=2) + '\n')
PY
dockerd --validate --config-file="$candidate"
install_json_if_changed "$candidate" /etc/docker/daemon.json
changed=$FILE_CHANGED
ala_substep logging completed
ala_substep services started
for service in docker containerd; do
  if [[ $(systemctl is-enabled "$service" 2>/dev/null || true) != enabled ]]; then systemctl enable "$service"; fi
done
if systemctl is-active --quiet docker; then
  if $restart; then systemctl restart docker
  elif $changed; then echo '설정 저장됨. 적용하려면 유지보수 시간에 sudo systemctl restart docker가 필요합니다.'; fi
else
  systemctl start docker
fi
ensure_service containerd
ala_substep services completed
ala_substep verify started
systemctl is-active --quiet docker
docker info >/dev/null
docker compose version
ala_substep verify completed
echo 'Docker 설치/실행 확인 완료. 기존 컨테이너는 새 로그 설정으로 자동 변경되지 않습니다.'
echo 'docker 그룹에 사용자를 자동 추가하지 않습니다. sudo docker로 사용하세요.'
echo '컨테이너 부팅 복구에는 Compose restart 정책이 별도로 필요합니다.'
