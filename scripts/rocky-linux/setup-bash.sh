#!/usr/bin/env bash
# 역할: 지정한 사용자에게 Bash·Oh My Bash 테마 적용. root는 --with-root 선택.
# Rocky Linux 10: Bash + Oh My Bash + bash-completion.
# Usage: sudo bash setup-bash.sh --user user [--with-root]
set -Eeuo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$script_dir/common.sh"

target_user=${SUDO_USER:-}
with_root=false
while (($#)); do
  case "$1" in
    --user) [[ $# -ge 2 ]] || { echo '--user 뒤에 사용자 이름이 필요합니다.' >&2; exit 2; }; target_user=$2; shift 2 ;;
    --with-root) with_root=true; shift ;;
    -h|--help) echo 'sudo bash setup-bash.sh --user user [--with-root]'; exit 0 ;;
    *) printf '알 수 없는 옵션: %s\n' "$1" >&2; exit 2 ;;
  esac
done

require_rocky
[[ -n $target_user ]] || { echo '--user로 대상 계정을 지정하세요.' >&2; exit 2; }
getent passwd "$target_user" >/dev/null || { echo '존재하지 않는 사용자입니다.' >&2; exit 1; }

ala_substep packages started
install_missing_packages bash git bash-completion
ala_substep packages completed

configure_user() {
  local account=$1 account_home current_shell
  account_home=$(getent passwd "$account" | cut -d: -f6)
  current_shell=$(getent passwd "$account" | cut -d: -f7)
  [[ $account_home == /* && $account_home != / && -d $account_home ]] || {
    printf '사용 가능한 홈 디렉터리가 아닙니다: %s\n' "$account_home" >&2; return 1;
  }
  # Send only the required functions over stdin: the user may not be able to read /root's script directory.
  {
  declare -f backup_file install_if_changed write_config
  cat <<'USER_SETUP'
set -Eeuo pipefail
umask 077
omb="$HOME/.oh-my-bash"
config_dir="$HOME/.config/server-bash"
mkdir -p "$config_dir"

# Refuse to overwrite a pre-existing Oh My Bash setup or unrelated directory.
if [[ -e $omb ]]; then
  [[ -f $config_dir/managed && -f $omb/oh-my-bash.sh ]] || {
    echo "기존 $omb 설정이 있습니다. 덮어쓰지 않았습니다." >&2; exit 1;
  }
else
  # Clone only: do not run the upstream installer that replaces .bashrc.
  # Existing installations are not pulled/updated on repeat runs.
  git clone --depth 1 https://github.com/ohmybash/oh-my-bash.git "$omb"
  touch "$config_dir/managed"
fi

mkdir -p "$omb/custom/themes/server"
write_config "$config_dir/server.theme.sh" 0600 <<'THEME'
# ASCII prompt: no Powerline/Nerd Font dependency.
# __git_ps1 is provided by Oh My Bash's bundled git-prompt.sh.
GIT_PS1_SHOWDIRTYSTATE=1
GIT_PS1_SHOWUNTRACKEDFILES=1
if ((EUID == 0)); then
  PS1='\[\e[1;31m\][ROOT \u@\h]\[\e[0m\] \[\e[1;34m\]\w\[\e[0m\]\[\e[33m\]$(__git_ps1 " (%s)")\[\e[0m\]\n\$ '
else
  PS1='\[\e[1;32m\][\u@\h]\[\e[0m\] \[\e[1;34m\]\w\[\e[0m\]\[\e[33m\]$(__git_ps1 " (%s)")\[\e[0m\]\n\$ '
fi
THEME
install_if_changed "$config_dir/server.theme.sh" "$omb/custom/themes/server/server.theme.sh" 0600

write_config "$config_dir/init.bash" 0600 <<'INIT'
# Interactive Bash only; keep scp, scripts, and non-interactive SSH quiet.
[[ $- == *i* ]] || return
[[ -z ${SERVER_BASH_LOADED:-} ]] || return
SERVER_BASH_LOADED=1
export OSH="$HOME/.oh-my-bash"
OSH_THEME="server"
DISABLE_AUTO_UPDATE=true
OMB_USE_SUDO=false
plugins=()
aliases=()
completions=()
if ! declare -F _completion_loader >/dev/null && [[ -r /usr/share/bash-completion/bash_completion ]]; then
  source /usr/share/bash-completion/bash_completion
fi
source "$OSH/oh-my-bash.sh"
INIT

if [[ ! -f $HOME/.bashrc ]] || ! grep -Fqx '# server-bash managed hook' "$HOME/.bashrc"; then
  if [[ -f $HOME/.bashrc ]] && grep -Eq '^[[:space:]]*[^#].*oh-my-bash\.sh' "$HOME/.bashrc"; then
    echo '.bashrc에 기존 Oh My Bash 로딩 설정이 있습니다. 중복 적용하지 않았습니다.' >&2
    exit 1
  fi
  candidate=$(mktemp "$config_dir/.bashrc.XXXXXX")
  if [[ -f $HOME/.bashrc ]]; then cat "$HOME/.bashrc" > "$candidate"; fi
  cat >> "$candidate" <<'HOOK'

# server-bash managed hook
if [ -n "${BASH_VERSION:-}" ] && [ -r "$HOME/.config/server-bash/init.bash" ]; then
  . "$HOME/.config/server-bash/init.bash"
fi
HOOK
  install_if_changed "$candidate" "$HOME/.bashrc" 0600
  rm -f "$candidate"
fi

# Do not rewrite an existing login profile: it may contain site-specific logic.
if [[ ! -e $HOME/.bash_profile ]]; then
  if [[ -e $HOME/.bash_login || -e $HOME/.profile ]]; then
    echo '주의: 기존 .bash_login/.profile에서 .bashrc를 읽는지 확인하세요.'
  else
    printf '%s\n' 'if [ -f "$HOME/.bashrc" ]; then . "$HOME/.bashrc"; fi' > "$HOME/.bash_profile"
  fi
elif ! grep -q '\.bashrc' "$HOME/.bash_profile"; then
  echo '주의: .bash_profile에서 .bashrc를 읽는지 확인하세요.'
fi

for file in "$HOME/.bashrc" "$config_dir/init.bash" "$config_dir/server.theme.sh"; do
  bash -n "$file"
done
git -C "$omb" rev-parse HEAD | write_config "$config_dir/oh-my-bash.commit" 0600
printf '사용자 %s 설정 완료. Oh My Bash commit: ' "$(id -un)"
cat "$config_dir/oh-my-bash.commit"
USER_SETUP
  } | runuser -u "$account" -- env HOME="$account_home" bash --noprofile --norc -s

  # Switch only interactive shells; never turn a service account into a login account.
  case "$current_shell" in
    */bash) ;;
    */zsh|*/fish|*/sh) usermod --shell /bin/bash "$account" ;;
    *) printf '로그인 셸을 바꾸지 않았습니다: %s (%s)\n' "$account" "$current_shell" >&2; return 1 ;;
  esac
}

ala_substep user_shell started
configure_user "$target_user"
ala_substep user_shell completed
if $with_root && [[ $target_user != root ]]; then
  ala_substep root_shell started
  configure_user root
  ala_substep root_shell completed
fi
printf '\n완료: SSH로 다시 접속하세요. 재부팅은 필요 없습니다.\n'
printf 'root에도 적용했다면 sudo -i로 확인하세요. root SSH 설정은 변경하지 않습니다.\n'
