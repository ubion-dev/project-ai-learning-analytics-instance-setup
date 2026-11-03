#!/usr/bin/env bash
# 역할: 호스트 GPU를 Docker에 연결. 드라이버 정상 동작 후 별도 실행.
set -Eeuo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"
restart=false
while (($#)); do
  case "$1" in
    --restart-docker) restart=true; shift ;;
    -h|--help) echo 'sudo bash setup-nvidia-container.sh [--restart-docker]'; exit 0 ;;
    *) die "알 수 없는 옵션: $1" ;;
  esac
done
require_rocky
ala_substep host_check started
command -v nvidia-smi >/dev/null || die '먼저 NVIDIA 드라이버를 설치하고 재부팅하세요.'
nvidia-smi
command -v dockerd >/dev/null || die '먼저 setup-docker.sh를 실행하세요.'
ala_substep host_check completed
ala_substep repository started
install_missing_packages curl ca-certificates
add_repo https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo \
  /etc/yum.repos.d/nvidia-container-toolkit.repo
ala_substep repository completed
ala_substep packages started
install_missing_packages nvidia-container-toolkit
ala_substep packages completed
ala_substep runtime started
mkdir -p /etc/docker
candidate=$(mktemp /etc/docker/.nvidia-daemon.json.XXXXXX)
trap 'rm -f "$candidate"' EXIT
if [[ -f /etc/docker/daemon.json ]]; then cp /etc/docker/daemon.json "$candidate"
else printf '{}\n' > "$candidate"; fi
nvidia-ctk runtime configure --runtime=docker --config="$candidate"
dockerd --validate --config-file="$candidate"
install_json_if_changed "$candidate" /etc/docker/daemon.json
changed=$FILE_CHANGED
ala_substep runtime completed
ala_substep verify started
if systemctl is-active --quiet docker; then
  if $restart; then
    systemctl restart docker
  else
    # The file can be unchanged while the running daemon still uses old settings.
    running_runtimes=$(docker info --format '{{json .Runtimes}}')
    if $changed || ! grep -Eq '"nvidia"[[:space:]]*:' <<< "$running_runtimes"; then
      ala_substep verify pending
      echo 'GPU runtime 설정 저장됨. 적용에는 sudo systemctl restart docker가 필요합니다.'
      echo '운영 중인 컨테이너 영향을 확인한 뒤 재시작하고 verify.sh --gpu로 검사하세요.'
      exit 0
    fi
  fi
else
  systemctl start docker
fi
docker info --format '{{json .Runtimes}}' | grep -q '"nvidia"'
ala_substep verify completed
echo 'NVIDIA runtime 등록 확인 완료. 실제 GPU 컨테이너 실행 검증은 별도로 필요합니다.'
