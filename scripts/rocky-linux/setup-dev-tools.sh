#!/usr/bin/env bash
# 역할: Neovim·btop·tmux 설치. 기본 제외, 필요할 때만 실행.
set -Eeuo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"
if (($#)); then
  case "$1" in
    -h|--help) echo 'sudo bash setup-dev-tools.sh'; exit 0 ;;
    *) die "알 수 없는 옵션: $1" ;;
  esac
fi
require_rocky
ala_substep repository started
install_missing_packages dnf-plugins-core
ensure_repo_enabled crb
install_missing_packages epel-release
ala_substep repository completed
ala_substep packages started
install_missing_packages neovim btop tmux
ala_substep packages completed
