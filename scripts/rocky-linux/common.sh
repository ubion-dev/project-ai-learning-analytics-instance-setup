#!/usr/bin/env bash
# Library; source from the setup scripts.
set -Eeuo pipefail
ROCKY_SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
trap 'printf "실패: %s:%s. 자동 재부팅하지 않습니다.\n" "${BASH_SOURCE[0]:-shell}" "$LINENO" >&2' ERR
die() { printf '%s\n' "$*" >&2; exit 1; }
# Machine-readable progress is opt-in; ordinary script output stays unchanged.
ala_substep() {
  [[ ${ALA_REPORT_PROGRESS:-0} == 1 ]] || return 0
  [[ $# == 2 && $1 =~ ^[a-z0-9_]+$ ]] || die '잘못된 세부 진행 식별자입니다.'
  case "$2" in
    started|completed|pending|kept|failed) ;;
    *) die '잘못된 세부 진행 상태입니다.' ;;
  esac
  printf '__ALA_SUBSTEP__\t%s\t%s\n' "$1" "$2"
}

require_rocky() {
  [[ $EUID == 0 ]] || die 'sudo bash로 실행하세요.'
  [[ -r /etc/os-release ]] || die 'Rocky Linux 10에서 실행하세요.'
  . /etc/os-release
  [[ ${ID:-} == rocky && ${VERSION_ID%%.*} == 10 ]] || die 'Rocky Linux 10 전용입니다.'
}
backup_file() {
  # Hidden backups are not parsed as dconf policy or lock files.
  [[ ! -e $1 ]] || cp -a -- "$1" "$(dirname -- "$1")/.$(basename -- "$1").backup.$(date +%Y%m%d-%H%M%S).$$"
}
dnf_install() {
  # Keep the boot kernel/release unchanged, including dependencies. Exact running-
  # kernel modules/devel remain installable; do not replace this with kernel*.
  local result=0
  dnf install -y --setopt=install_weak_deps=False \
    --exclude='kernel,kernel-core,kernel-debug*,kernel-uki*,rocky-release*,rocky-repos*,rocky-gpg-keys*' \
    "$@" || result=$?
  if ((result)); then
    printf '패키지 설치 실패: 기본 설치는 부팅 커널과 Rocky release 변경을 허용하지 않습니다.\n' >&2
    printf '실행 커널 %s에 맞는 kernel-devel/modules가 현재 저장소에 있는지 확인하세요.\n' "$(uname -r)" >&2
    printf 'OS/커널 변경이 필요하면 별도 명시적 유지보수 작업으로 진행하세요. 자동 업그레이드·삭제로 우회하지 않습니다.\n' >&2
  fi
  return "$result"
}
install_missing_packages() {
  local package query_status missing=()
  for package in "$@"; do
    if rpm -q "$package" >/dev/null 2>&1; then
      printf '[유지] 패키지: %s\n' "$package"
    else
      query_status=$?
      if ((query_status != 1)); then
        printf '패키지 설치 여부 조회 실패 (rpm 종료 %s): %s. 설치를 중단합니다.\n' "$query_status" "$package" >&2
        return "$query_status"
      fi
      missing+=("$package")
    fi
  done
  if ((${#missing[@]})); then dnf_install "${missing[@]}"; fi
}
ensure_service() {
  local service=$1
  if [[ $(systemctl is-enabled "$service" 2>/dev/null || true) != enabled ]]; then systemctl enable "$service"; fi
  if ! systemctl is-active --quiet "$service"; then systemctl start "$service"; fi
}
ensure_masked() {
  local unit
  for unit in "$@"; do
    if [[ $(systemctl is-enabled "$unit" 2>/dev/null || true) != masked ]]; then
      systemctl mask "$unit"
    fi
  done
}
ensure_repo_enabled() {
  if ! dnf -q repolist --enabled | awk '{print $1}' | grep -Fxq "$1"; then
    dnf config-manager --set-enabled "$1"
  fi
}
nvidia_gpu_present() {
  local status=0
  python3 "$ROCKY_SCRIPT_DIR/lib/nvidia_gpu.py" --present || status=$?
  case "$status" in
    0|1) return "$status" ;;
    *) die 'NVIDIA GPU 감지 실패. PCI/sysfs 상태를 확인하세요.' ;;
  esac
}
install_if_changed() {
  local source=$1 destination=$2 mode=${3:-0644}
  FILE_CHANGED=false
  if cmp -s "$source" "$destination" && python3 - "$destination" "$mode" <<'PY'
import os, pathlib, stat, sys
value = pathlib.Path(sys.argv[1]).stat()
sys.exit(0 if stat.S_IMODE(value.st_mode) == int(sys.argv[2], 8) and value.st_uid == os.geteuid() else 1)
PY
  then
    printf '[유지] 설정: %s\n' "$destination"
    return 0
  fi
  backup_file "$destination"
  install -m "$mode" "$source" "$destination"
  FILE_CHANGED=true
  printf '[적용] 설정: %s\n' "$destination"
}
write_config() {
  local destination=$1 mode=${2:-0644} candidate
  candidate=$(mktemp "$(dirname -- "$destination")/.rocky-config.XXXXXX")
  cat > "$candidate"
  install_if_changed "$candidate" "$destination" "$mode"
  rm -f "$candidate"
}
install_json_if_changed() {
  # Preserve the original formatting when a generated candidate means the same JSON.
  if python3 - "$1" "$2" <<'PY'
import json, pathlib, sys
try:
    left, right = (json.loads(pathlib.Path(name).read_text()) for name in sys.argv[1:])
except (OSError, ValueError):
    sys.exit(1)
sys.exit(0 if left == right else 1)
PY
  then
    cp -- "$2" "$1"
  fi
  install_if_changed "$1" "$2"
}
migrate_kiosk_dconf() {
  local legacy=/etc/dconf/db/rocky-kiosk.d current=/etc/dconf/db/rocky_setup.d
  [[ ! -L $legacy && ! -L $current ]] || die 'dconf 정책 경로가 심볼릭 링크입니다. 이전 전에 직접 확인하세요.'
  [[ ! -e $legacy || -d $legacy ]] || die '기존 dconf 정책 경로가 디렉터리가 아닙니다.'
  [[ ! -e $current || -d $current ]] || die '새 dconf 정책 경로가 디렉터리가 아닙니다.'
  [[ -d $legacy ]] || return 0
  if [[ ! -e $current ]]; then
    mv -- "$legacy" "$current"
  else
    # Preserve both namespaces if custom policies conflict; never guess which policy wins.
    python3 - "$legacy" "$current" <<'PY'
from pathlib import Path
import sys
legacy, current = map(Path, sys.argv[1:])
for source in legacy.rglob('*'):
    target = current / source.relative_to(legacy)
    if (source.is_symlink() or target.is_symlink() or not (source.is_file() or source.is_dir()) or
            (target.exists() and not (target.is_file() or target.is_dir()))):
        raise SystemExit(f'dconf 정책 이전 전 직접 확인이 필요한 파일: {source}')
    if target.exists() and (source.is_dir() != target.is_dir() or
                            (source.is_file() and source.read_bytes() != target.read_bytes())):
        raise SystemExit(f'dconf 정책 충돌: {source.relative_to(legacy)}. 기존 파일을 덮어쓰지 않았습니다.')
PY
    cp -a -n -- "$legacy/." "$current/"
    mv -- "$legacy" "/etc/dconf/db/.rocky-kiosk.d.migrated.$(date +%Y%m%d-%H%M%S).$$"
  fi
  DCONF_CHANGED=true
  echo '[이전] dconf 정책 DB: rocky-kiosk -> rocky_setup'
}
prepare_kiosk_dconf() {
  command -v dconf >/dev/null || die 'GNOME/dconf가 필요합니다. 먼저 GNOME 데스크톱을 설치하세요.'
  install -d -m 0755 /etc/dconf/profile /etc/dconf/db
  local candidate
  candidate=$(mktemp /etc/dconf/profile/.user.XXXXXX)
  python3 - "$candidate" <<'PY'
from pathlib import Path
import sys
profile = Path('/etc/dconf/profile/user')
lines = profile.read_text().splitlines() if profile.exists() else ['user-db:user']
lines = [line for line in lines if line.strip() not in ('system-db:rocky-kiosk', 'system-db:rocky_setup')]
indices = [i for i, line in enumerate(lines) if line.strip().startswith('user-db:')]
if not indices:
    raise SystemExit('기존 dconf profile에 user-db가 없습니다. 수동 검토가 필요합니다.')
lines.insert(indices[0] + 1, 'system-db:rocky_setup')
Path(sys.argv[1]).write_text('\n'.join(lines) + '\n')
PY
  DCONF_CHANGED=false
  migrate_kiosk_dconf
  install -d -m 0755 /etc/dconf/db/rocky_setup.d/locks
  install_if_changed "$candidate" /etc/dconf/profile/user
  if $FILE_CHANGED; then DCONF_CHANGED=true; fi
  rm -f "$candidate"
}
update_dconf_if_changed() {
  if $DCONF_CHANGED || [[ ! -f /etc/dconf/db/rocky_setup ]] || ! python3 - <<'PY'
from pathlib import Path
import sys
database = Path('/etc/dconf/db/rocky_setup')
source = Path('/etc/dconf/db/rocky_setup.d')
files = [p for p in source.rglob('*') if p.is_file() and not any(part.startswith('.') for part in p.relative_to(source).parts)]
files.append(Path('/etc/dconf/profile/user'))
sys.exit(0 if all(p.stat().st_mtime_ns <= database.stat().st_mtime_ns for p in files) else 1)
PY
  then
    dconf update
    chmod 0644 /etc/dconf/db/rocky_setup
  fi
}
add_repo() {
  local url=$1 destination=$2 temporary
  [[ ! -e $destination ]] || return 0
  temporary=$(mktemp)
  if ! curl --fail --silent --show-error --location "$url" -o "$temporary"; then
    rm -f "$temporary"; die "저장소 다운로드 실패: $url"
  fi
  grep -q '^\[' "$temporary" || { rm -f "$temporary"; die '유효한 repo 파일이 아닙니다.'; }
  install -m 0644 "$temporary" "$destination"
  rm -f "$temporary"
}
report_reboot() {
  local result=0
  dnf needs-restarting -r || result=$?
  case "$result" in
    0) echo '일반 시스템 검사상 재부팅 불필요. GPU 드라이버 설치는 별도 안내를 따르세요.' ;;
    1) echo '재부팅 필요: 작업을 마친 뒤 유지보수 시간에 sudo reboot를 실행하세요.' ;;
    *) echo '재부팅 필요 여부 검사 실패. 설치 로그와 커널 버전을 확인하세요.' >&2; return "$result" ;;
  esac
}
