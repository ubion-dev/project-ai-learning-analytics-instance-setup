#!/usr/bin/env bash
# Entry point: base -> optional display -> update policy -> Bash -> optional tools/SSH -> Docker -> GPU -> verify.
set -Eeuo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$script_dir/common.sh"

account=${SUDO_USER:-}
with_ssh=false
with_display=false
dev_tools=false
plan_only=false
gpu_pending=false
gpu_runtime_pending=false
ssh_zone=''
root_args=()
base_args=()
ssh_args=()
update_args=()
verify_args=(--update-policy)

show_help() {
  cat <<'HELP'
사용법:
  sudo bash ./setup.sh --user user --with-root --with-ssh
  bash ./setup.sh --user user --with-root --with-ssh --plan

기본 구성:
  필수 패키지/Chrome·안내 정책/시간 -> 자동 업데이트 차단 -> Bash -> Docker -> NVIDIA 자동 감지 -> 검사
  이 서버 자체의 GNOME 화면 유지가 필요하면 --with-display 추가
  Rocky Linux 10 필요. --with-display는 GNOME 필요. NVIDIA는 감지된 경우 자동 준비합니다.

선택 옵션:
  --user NAME       Bash 설정 대상 사용자 (기본: sudo 실행 사용자)
  --with-root       root에도 Bash 테마 적용
  --with-ssh        SSH 서버/방화벽 설정 포함
  --with-display    이 서버 자체의 GNOME 화면 유지/업데이트 정책 포함
  --ssh-zone ZONE   SSH 방화벽 zone 지정 (--with-ssh 필요)
  --dev-tools       Neovim/btop/tmux 설치
  --upgrade         이번 실행에서 전체 OS 패키지 업데이트
  --plan            계획만 표시. 설치/설정 변경 없음. sudo 불필요
  --help            이 도움말 표시

자동 재부팅은 하지 않습니다. 실패하면 중단하며 이전 단계는 자동 복원하지 않습니다.
HELP
}

while (($#)); do
  case "$1" in
    --user|--ssh-zone)
      [[ $# -ge 2 && -n $2 && $2 != --* ]] || die "$1 값이 필요합니다."
      if [[ $1 == --user ]]; then
        account=$2
      else
        ssh_zone=$2
        ssh_args=(--ssh-zone "$2")
      fi
      shift 2
      ;;
    --with-root) root_args=(--with-root); shift ;;
    --with-display) with_display=true; shift ;;
    --dev-tools) dev_tools=true; shift ;;
    --with-ssh) with_ssh=true; shift ;;
    --upgrade) base_args+=(--upgrade); shift ;;
    --plan) plan_only=true; shift ;;
    -h|--help) show_help; exit 0 ;;
    *) die "알 수 없는 옵션: $1" ;;
  esac
done
[[ -z $ssh_zone ]] || $with_ssh || die '--ssh-zone은 --with-ssh와 함께 사용하세요.'

stage_scripts=(setup-base.sh)
stage_labels=('기본 패키지·Chrome·안내 정책·시간 동기화')
if $with_display; then
  stage_scripts+=(setup-display.sh)
  stage_labels+=('이 서버의 GNOME 화면·절전 방지')
  update_args=(--with-display)
  verify_args+=(--display-policy)
fi
stage_scripts+=(setup-update-policy.sh setup-bash.sh)
stage_labels+=('자동 업데이트 차단' 'Bash·Oh My Bash')
if $dev_tools; then
  stage_scripts+=(setup-dev-tools.sh)
  stage_labels+=('선택: 관리 도구')
fi
if $with_ssh; then
  stage_scripts+=(setup-ssh.sh)
  stage_labels+=('선택: SSH·방화벽')
  verify_args+=(--ssh)
fi
stage_scripts+=(setup-docker.sh setup-nvidia.sh verify.sh)
stage_labels+=('Docker·Compose' 'NVIDIA 자동 감지·드라이버·컨테이너 연결' '설정·서비스 검사')

if $plan_only; then
  printf '실행 계획 (설치하지 않음) | 대상 사용자: %s\n' "${account:-미지정}"
  for index in "${!stage_scripts[@]}"; do
    printf '[%d/%d] %-32s %s\n' "$((index + 1))" "${#stage_scripts[@]}" \
      "${stage_scripts[index]}" "${stage_labels[index]}"
  done
  printf 'root Bash 적용: %s | 전체 패키지 업데이트: %s\n' \
    "${root_args[*]:-안 함}" "${base_args[*]:-안 함}"
  echo 'NVIDIA GPU: 있으면 자동 준비, 없으면 건너뜀. 자동 재부팅: 안 함.'
  exit 0
fi

require_rocky
[[ -n $account ]] || die '--user를 지정하세요.'
getent passwd "$account" >/dev/null || die '존재하지 않는 사용자입니다.'

for index in "${!stage_scripts[@]}"; do
  script=${stage_scripts[index]}
  printf '\n[%d/%d] %s — %s\n' "$((index + 1))" "${#stage_scripts[@]}" \
    "${stage_labels[index]}" "$script"
  case "$script" in
    setup-base.sh) bash "$script_dir/$script" ${base_args[@]+"${base_args[@]}"} ;;
    setup-update-policy.sh) bash "$script_dir/$script" ${update_args[@]+"${update_args[@]}"} ;;
    setup-bash.sh) bash "$script_dir/$script" --user "$account" ${root_args[@]+"${root_args[@]}"} ;;
    setup-ssh.sh) bash "$script_dir/$script" ${ssh_args[@]+"${ssh_args[@]}"} ;;
    setup-nvidia.sh)
      gpu_status=0
      bash "$script_dir/$script" --auto || gpu_status=$?
      case "$gpu_status" in
        10) gpu_pending=true ;;
        0)
          if nvidia_gpu_present; then
            bash "$script_dir/setup-nvidia-container.sh"
            if docker info --format '{{json .Runtimes}}' | grep -Eq '"nvidia"[[:space:]]*:'; then
              verify_args+=(--gpu)
            else
              gpu_runtime_pending=true
            fi
          fi
          ;;
        *) exit "$gpu_status" ;;
      esac
      ;;
    verify.sh) bash "$script_dir/$script" ${verify_args[@]+"${verify_args[@]}"} ;;
    *) bash "$script_dir/$script" ;;
  esac
done

printf '\n공통 환경 단계 완료. 새 터미널에서 Bash 설정을 확인하세요.\n'
if $with_display; then echo 'GUI 정책은 이 PC에서 재로그인 후 적용됩니다.'; fi
if $gpu_pending; then
  echo '[대기] NVIDIA 드라이버 설치 후 재부팅이 필요합니다. 재부팅 후 같은 setup.sh 명령을 다시 실행하세요.'
elif $gpu_runtime_pending; then
  echo '[대기] 컨테이너 GPU 연결 적용에는 Docker 재시작 또는 재부팅이 필요합니다. 적용 후 같은 setup.sh 명령으로 검사하세요.'
fi
echo '실제 앱 배포와 Chrome 자동 실행은 별도입니다.'
