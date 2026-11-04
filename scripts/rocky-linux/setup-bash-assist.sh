#!/usr/bin/env bash
# Opt-in interactive Bash support for the configured general user.
set -Eeuo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$script_dir/common.sh"

if [[ $# == 1 && ( $1 == --help || $1 == -h ) ]]; then
  printf '%s\n' '사용법: sudo bash setup-bash-assist.sh --user user'
  exit 0
fi
[[ $# == 2 && $1 == --user && $2 =~ ^[a-zA-Z_][a-zA-Z0-9_.-]{0,63}$ ]] ||
  die '사용법: sudo bash setup-bash-assist.sh --user user'
require_rocky
record=$(getent passwd "$2") || die '지정한 일반 사용자 계정을 찾지 못했습니다.'
IFS=: read -r account _ uid _ _ account_home login_shell <<< "$record"
[[ $account == "$2" && $uid =~ ^[1-9][0-9]*$ && $login_shell == */bash &&
   $account_home == /* && $account_home != / && -d $account_home ]] ||
  die 'Bash 로그인과 홈 디렉터리가 준비된 일반 사용자만 지정하세요.'

install_missing_packages curl tar xz procps-ng

runuser -u "$account" -- env -i HOME="$account_home" USER="$account" LOGNAME="$account" \
  PATH=/usr/bin:/bin bash --noprofile --norc -s <<'USER_SETUP'
set -Eeuo pipefail
umask 077
config_dir="$HOME/.config/server-bash"
bashrc="$HOME/.bashrc"
version=0.4.0-devel3
checksum=c8612ee612bc6b10dbfd6e85c6cbdfd7caf152a12d1f9de22ea0a9d735b3080c
destination="$config_dir/ble-$version"

[[ ! -L $HOME && ! -L $HOME/.config && ! -L $config_dir && ! -L $bashrc &&
   -f $config_dir/managed && -O $config_dir && -f $bashrc && -O $bashrc ]] || {
  echo '관리 중인 Bash 설정의 소유자와 심볼릭 링크 여부를 확인하세요.' >&2
  exit 1
}

load_block=$(cat <<'BLE_LOAD'
# ala bash-assist load
if [[ $- == *i* && -t 0 && -t 1 && -t 2 && ${BASH_VERSINFO[0]} -ge 4 && ! ${BLE_VERSION-} &&
      -r "$HOME/.config/server-bash/ble-0.4.0-devel3/ble.sh" ]]; then
  if source -- "$HOME/.config/server-bash/ble-0.4.0-devel3/ble.sh" --attach=none; then
    __ala_blesh_attach=1
  fi
fi
BLE_LOAD
)
attach_block=$(cat <<'BLE_ATTACH'
# ala bash-assist attach
if [[ ${__ala_blesh_attach-} == 1 ]]; then
  unset __ala_blesh_attach
  if ! ble-attach; then
    printf '%s\n' 'Bash 자동 제안·구문 강조를 활성화하지 못했습니다.' >&2
  fi
fi
BLE_ATTACH
)

candidate=
stage=$(mktemp -d -- "$config_dir/.ble.XXXXXX")
cleanup() {
  if [[ -n $candidate ]]; then
    rm -f -- "$candidate"
  fi
  rm -rf -- "$stage"
}
trap cleanup EXIT
cp -p -- "$bashrc" "$stage/original"
original=$(cat -- "$stage/original")
managed=false
if [[ $original == *"$load_block"* && $original == *"$attach_block"* &&
      $(grep -Fxc '# ala bash-assist load' "$stage/original") == 1 &&
      $(grep -Fxc '# ala bash-assist attach' "$stage/original") == 1 ]]; then
  managed=true
elif grep -Eq '^# ala bash-assist (load|attach)$' "$stage/original"; then
  echo 'Bash 편의 기능 로딩 구문이 일부 변경되었습니다. 기존 설정을 확인하세요.' >&2
  exit 1
fi

# Recognize explicit user-managed loads without interpreting shell contents.
# Existing custom ble.sh settings are preserved.
if ! $managed; then
  for rc in "$stage/original" "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile"; do
    if [[ -f $rc ]] && grep -Ev '^[[:space:]]*(#|$)' "$rc" |
        grep -E 'ble\.sh|(^|[[:space:]])ble-attach([[:space:];]|$)' >/dev/null; then
      echo '기존 ble.sh 로딩 구문이 있습니다. 중복 등록하지 않습니다.' >&2
      exit 1
    fi
  done
fi

if [[ -e $destination || -L $destination ]]; then
  [[ ! -L $destination && -d $destination && -O $destination &&
     ! -L $destination/ble.sh && -f $destination/ble.sh &&
     ! -L $destination/.ala-release && -f $destination/.ala-release &&
     $(cat -- "$destination/.ala-release") == "$checksum" ]] || {
    echo '기존 ble.sh 설치 경로의 관리 여부를 확인하세요.' >&2
    exit 1
  }
else
  archive="$stage/release.tar.xz"
  curl --fail --silent --show-error --location --max-time 90 --proto '=https' --proto-redir '=https' \
    "https://github.com/akinomyoga/ble.sh/releases/download/v$version/ble-$version.tar.xz" -o "$archive"
  if ! printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check --status; then
    echo 'ble.sh 릴리스의 SHA-256이 일치하지 않습니다.' >&2
    exit 1
  fi
  tar --extract --xz --no-same-owner --no-same-permissions --file "$archive" --directory "$stage"
  [[ ! -L $stage/ble-$version/ble.sh && -f $stage/ble-$version/ble.sh ]]
  bash -n -- "$stage/ble-$version/ble.sh"
  printf '%s\n' "$checksum" > "$stage/ble-$version/.ala-release"
  mv -T -- "$stage/ble-$version" "$destination"
fi

if ! $managed; then
  candidate=$(mktemp -- "$HOME/.bashrc.ala.XXXXXX")
  cp -p -- "$stage/original" "$candidate"
  {
    printf '%s\n\n' "$load_block"
    cat -- "$stage/original"
    printf '\n%s\n' "$attach_block"
  } > "$candidate"
  bash -n -- "$candidate"
  if [[ -L $bashrc || ! -O $bashrc ]] || ! cmp -s -- "$stage/original" "$bashrc"; then
    echo '작업 중 .bashrc가 바뀌었습니다. 현재 내용을 보존했습니다.' >&2
    exit 1
  fi
  backup=$(mktemp -- "$HOME/.bashrc.ala-backup.XXXXXX")
  cp -p -- "$stage/original" "$backup"
  mv -fT -- "$candidate" "$bashrc"
fi
printf '%s\n' 'Bash 자동 제안·구문 강조 준비 완료. 새 SSH 로그인에서 확인하세요.'
USER_SETUP
