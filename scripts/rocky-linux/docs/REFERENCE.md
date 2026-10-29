# Rocky Linux 설정 상세 안내

[빠른 설치 안내로 돌아가기](../README.md)

기본 구성과 일상 점검·업데이트 절차는 [기본 환경과 운영 관리](ENVIRONMENT.md)를 참고하세요.
이 문서는 파일별 옵션·변경 사항·복원 방법을 확인할 때 사용합니다.
모든 실행 예시는 **Rocky 서버의 스크립트 폴더** 안에서 실행하는 기준입니다.
SCP로 전달했다면 `cd ~/rocky-linux`, 저장소가 있다면 그 안의 `scripts/rocky-linux`로 이동하세요.
`common.sh`는 공통 함수 라이브러리이므로 직접 실행하지 않습니다.
대상은 CPU/GPU 자원이 필요한 무거운 작업을 실행하면서 Chrome 전체화면으로 DID 웹 화면을 띄우는
학습분석실의 물리 서버 두 대입니다. 현재 운영 구성에는 두 서버 모두 GNOME과 `--with-display`를 사용합니다.
DID 제어 인스턴스는 별도 담당자의 관리 범위이므로 패널 배치·영상 입력·장비 설정은 다루지 않습니다.
단일 앱 강제 실행이나 사용자 입력 제한 같은 전용 키오스크 구성을 추가하지 않습니다.
내부 dconf DB 이름은 `rocky_setup`입니다. 이전 `rocky-kiosk`는 하이픈 때문에 D-Bus 변경 알림 경로가
유효하지 않았으므로 기존 정책을 보존해 자동 이전합니다. logind의 기존 `60-rocky-kiosk.conf` 파일명은 유지합니다.
OS 업데이트 정책 검사는 `--update-policy`, 서버 자체의 화면 정책까지 적용했다면 `--display-policy`를 사용합니다.
이전 `--kiosk`도 `--display-policy`의 호환 별칭으로 지원합니다.


| 파일 | 내용 |
|---|---|
| `setup-base.sh` | 기본 패키지·Chrome·안내 정책·SSH 실행 명령, chrony, 시간대 |
| `setup-display.sh` | 화면 자동 꺼짐·자동 잠금·시스템 절전 방지 |
| `setup-update-policy.sh` | 자동 OS 업데이트 차단, 선택 GNOME 업데이트 정책 |
| `setup-ssh.sh` | SSH·방화벽 설정 (선택) |
| `setup-dev-tools.sh` | Neovim·btop·tmux 설치 (선택) |
| `setup-bash.sh` | Oh My Bash와 사용자/root 테마 |
| `setup-docker.sh` | Docker Engine, Compose, 자동 시작, 기본 로그 제한 |
| `setup-nvidia.sh` | NVIDIA 자동 감지·지원 드라이버 준비·재부팅 대기 |
| `setup-nvidia-container.sh` | 드라이버 준비 후 자동 호출하는 Container Toolkit 설정 |
| `lib/nvidia_gpu.py` | PCI 장치와 공식 open 모듈 지원 표를 읽는 내부 검사 |
| `lib/chrome_policy.py` | 로컬 Chrome 정책 적용·검사·백업 |
| `bin/chrome-did` | 설치 후 사용하는 SSH용 Chrome 실행·종료·상태·로그 명령 |
| `verify.sh` | 서비스, 포트, Docker와 선택 GPU 검사 |

### 기본 환경

```bash
sudo bash ./setup-base.sh
```

Git, curl, CA 인증서, bash-completion, Python 3, 네트워크 도구, firewalld와 Google Chrome을 설치합니다.
시간대는 Asia/Seoul이며 `--timezone`으로 변경할 수 있습니다. chronyd를 부팅 자동 시작합니다.
기존 SSH와 방화벽 서비스 상태·규칙은 변경하지 않습니다.
전체 패키지 업데이트도 요청할 경우에는 위 명령 대신 유지보수 시간에 다음 명령을 사용하세요.

```bash
sudo bash ./setup-base.sh --upgrade
```

Chrome은 기본 단계에 포함되어 단독 `sudo bash ./setup-base.sh`와 공통 `setup.sh` 모두에서 설치됩니다.
`google-chrome-stable` RPM이 있으면 서명 키를 다시 가져오거나 패키지를 재설치하지 않고 버전만 확인합니다.
없으면 Google 공식 x86_64 RPM을 설치하며 `localpkg_gpgcheck=1`과 `gpgcheck=1`로 서명 검증을 유지합니다.
신규 설치가 필요한 다른 아키텍처에서는 명확한 안내와 함께 중단합니다.
공식 패키지가 이후 업데이트용 저장소를 구성하므로 별도 임의 저장소 파일을 만들거나 덮어쓰지 않습니다.
이미 설치된 Chrome의 일반 재실행은 업그레이드하지 않으며, 명시적인 `--upgrade`는 전체 수동 업데이트입니다.
패키지가 설치되어도 실행 파일이 없거나 `--version` 검사가 실패하면 중단하며 자동 재설치하지 않습니다.
기존 Chrome 패키지는 유지하더라도 `/etc/opt/chrome/policies/managed/did-display.json`의 안내 정책과
`/usr/local/bin/chrome-did` 실행 명령은 준비합니다. 같은 JSON 값·권한과 같은 실행 도구이면 파일을 다시 쓰지 않습니다.
정책은 `TranslateEnabled=false`, `DefaultBrowserSettingEnabled=false`, `PromotionsEnabled=false`입니다.
기존 파일의 다른 키는 보존합니다. 다른 정책 파일이 이미 같은 값을 제공하면 중복 키를 만들지 않고,
반대 값이나 잘못된 읽기 권한을 발견하면 원본을 덮어쓰지 않고 경로와 이유를 알립니다.

Chrome은 정책 폴더의 파일을 확장자와 관계없이 읽으므로 정책 백업은
`/var/lib/rocky-setup/chrome-policy-backups/`에 보관합니다. 임시 파일도 `managed/` 밖에서 완성한 후 교체합니다.
이 경로의 파일을 `managed/`에 백업 용도로 복사해 두면 안 됩니다.
정책 검사기는 Chrome이 허용하는 JSON 주석·마지막 쉼표도 처리합니다.

SSH에서 `chrome-did start 'https://example.com'`으로 실행하면 F11 전체화면과
`--no-first-run`, `--no-default-browser-check`, `--hide-crash-restore-bubble`을 포함합니다.
DID 전용 프로필은 `${XDG_CONFIG_HOME:-$HOME/.config}/chrome-did`이며 일반 Chrome의 세션과 분리합니다.
`stop`은 이 도구의 `did-chrome.service`만 종료하고, `status`와 `logs`로 같은 서비스를 조회합니다.
화면 환경이 없으면 실행 전에 중단합니다. Wayland만 등록된 환경에서는 native Wayland 옵션을 선택합니다.
관리자는 SSH에서 기존 GNOME 로그인 세션을 사용하며 별도 화면 접속 프로그램은 설치하지 않습니다.
로그인·부팅 자동 실행을 등록하지 않고 설치 중인 브라우저를 강제로 재시작하지 않습니다.
[SSH 실행 및 검사 예시](ENVIRONMENT.md#chrome-표시와-ssh-관리)를 참고하세요.

이 스크립트 단독 실행에는 자동 업데이트 차단이 포함되지 않습니다. 통합 실행의 업데이트 정책 단계 또는
`setup-update-policy.sh`를 별도로 적용해야 합니다.
[Google Linux 서명 키 안내](https://www.google.com/linuxrepositories/)와
[Chrome Linux 설치 안내](https://support.google.com/chrome/answer/95346?hl=en)를 따릅니다.
[Linux 정책 적용](https://support.google.com/chrome/a/answer/9027408?hl=en),
[번역 정책](https://support.google.com/chrome/a/answer/9036555?hl=en),
[Chrome 안내 옵션](https://github.com/chromium/chromium/blob/main/chrome/common/chrome_switches.h)을 참고합니다.

### 재실행 시 처리

완료 마커로 전체 설치를 생략하지 않고 매 실행마다 현재 상태를 검사합니다.

- RPM으로 설치 여부를 검사하여 누락 패키지만 `dnf install`에 전달합니다. 기존 패키지를 직접 업그레이드하지 않으며, 누락 패키지의 의존성 해결에 따른 변경은 발생할 수 있습니다.
- 설정의 내용·파일 권한·소유자가 같으면 파일 교체와 백업을 생략합니다. 관리하는 값이 달라졌으면 백업 후 복구합니다.
- Docker JSON은 의미가 같으면 들여쓰기·키 순서가 달라도 원본을 유지합니다.
- 서비스 실행·자동 시작, systemd mask, SSH 포트 허용 규칙도 변경 전에 검사합니다.
- dconf 소스가 같아도 컴파일된 DB가 없거나 더 오래되었으면 다시 컴파일합니다.
- 기존 Oh My Bash 등 스크립트가 관리하지 않는 환경이나 손상된 드라이버는 자동 덮어쓰기 대신 중단합니다.

### 선택: 서버 자체의 GNOME 화면 유지·절전 정책

DID 제어 장비의 패널 배치·입력 설정은 변경하지 않으며, 이 서버의 GNOME 화면을 유지하는 정책입니다.
현재 두 서버 모두 Chrome 웹 표시를 병행하므로 이 단계를 적용합니다.

```bash
sudo bash ./setup-display.sh
```

로그인한 GNOME 데스크톱의 화면 자동 꺼짐, 자동 밝기 감소, 자동 잠금,
AC/배터리 자동 절전을 비활성화합니다. 모든 사용자에 적용되는 dconf 정책과 잠금을 사용하여
기존 사용자 설정이 정책을 덮어쓰지 못하게 합니다. 수동 화면 잠금은 허용합니다.
sleep/suspend/hibernate target도 mask하므로 수동 절전·최대절전 요청도 차단합니다.
logind의 IdleAction은 ignore로 설정하며 실행 중인 로그인 세션을 강제 재시작하지 않습니다.

GNOME은 **로그아웃 후 재로그인**, logind 설정은 **다음 OS 재부팅** 후 적용됩니다.
절전 target 차단은 즉시 적용됩니다. 모니터 자체 OSD 절전·외부 전원 제어와 GDM 로그인 화면 정책은 별도입니다.
기본 `/etc/dconf/profile/user`를 사용하며 별도 DCONF_PROFILE을 사용하는 세션은 추가 설정이 필요합니다.

### 자동 업데이트 차단

화면 없는 서버는 `sudo bash ./setup-update-policy.sh`로 실행합니다.
아래 `--with-display` 예시는 GNOME 업데이트 알림 정책까지 추가합니다.

```bash
sudo bash ./setup-update-policy.sh --with-display
```

- DNF Automatic 타이머와 서비스를 mask하여 예약 다운로드·설치를 차단
- `--with-display` 지정 시 GNOME Software 업데이트 화면·자동 다운로드·알림을 dconf 정책으로 비활성화
- PackageKit과 PackageKit 오프라인 업데이트 서비스를 mask하여 GUI 업데이트 경로 차단
- `sudo dnf upgrade` 등 관리자의 수동 패키지 관리는 유지

예약된 오프라인 업데이트(`/system-update`, `/etc/system-update`) 또는 실행 중인
자동 업데이트 트랜잭션을 발견하면 중단하며, 임의 취소·프로세스 강제 종료는 하지 않습니다.
PackageKit 데몬이 이미 실행 중이면 강제 종료하지 않으므로 유지보수 시간에 재부팅한 뒤
`verify.sh --update-policy`로 검사하세요. GNOME 정책도 적용했다면 `--display-policy`로 검사하며 재로그인이 필요합니다.
이 정책은 GUI 소프트웨어 설치도 제한합니다. 관리자는 CLI로 패키지를 설치하세요.
DNF makecache는 메타데이터만 갱신하므로 차단하지 않습니다.
별도 cron, 사내 관리 에이전트, 사용자 정의 업데이트 작업과 펌웨어 정책은 별도 점검 대상입니다.

자동 업데이트 차단은 영구 무패치 운영을 뜻하지 않습니다. 서비스 미사용 시간에
보안 업데이트·커널/GPU 호환성 확인·재부팅 검증을 계획적으로 진행하세요.
통합 스크립트의 `--upgrade`는 사용자가 명시한 전체 수동 업데이트입니다.

Chrome 본체도 Linux 패키지 관리자를 통해 업데이트되므로 위 DNF Automatic·PackageKit 차단 범위에 포함됩니다.
별도 버전 고정은 적용하지 않으며 관리자의 수동 `dnf upgrade`는 Chrome도 업데이트할 수 있습니다.
Chrome 내부 구성요소·확장 프로그램 업데이트는 이 OS 정책의 차단 대상이 아닙니다.
[Chrome 업데이트 범위와 수동 관리 절차](ENVIRONMENT.md#chrome과-os-업데이트-관리)를 참고하세요.

시스템 설정 백업은 원본 디렉터리의 `.파일명.backup.<시각>.<PID>` 숨김 파일로 저장합니다.
Chrome 정책은 예외로 `/var/lib/rocky-setup/chrome-policy-backups/`에 저장합니다.
dconf가 백업까지 정책으로 읽지 않도록 숨김 파일을 사용합니다.
정책을 되돌릴 때는 생성한 `rocky_setup.d/00-display`, `10-updates`와 해당 locks 파일을
백업 확인 후 제거하고 `dconf update`를 실행하세요. `/etc/dconf/profile/user`는 백업을 참고하여
`system-db:rocky_setup` 항목을 정리합니다. logind의 `60-rocky-kiosk.conf`도 제거하고 재부팅합니다.
systemd mask는 되돌릴 unit별 `systemctl unmask UNIT`이 필요합니다.
기존부터 masked였는지, 어떤 타이머가 활성화돼 있었는지는 먼저 확인하여 선택적으로 복원하세요.

이전 버전의 `rocky-kiosk.d`가 있으면 `rocky_setup.d`로 이동하고 사용자 프로필의 DB 참조도 바꿉니다.
두 디렉터리가 모두 있으면 충돌 없는 파일만 합치고 옛 소스 디렉터리는 `.rocky-kiosk.d.migrated.*`로 보관하여
다음 컴파일 대상에서 제외합니다. 같은 이름의 파일 내용이 다르거나 예상 밖 경로이면 덮어쓰지 않고 중단합니다.
옛 컴파일 DB 파일은 복구 참고용으로 남기며 활성 프로필에서는 참조하지 않습니다.
적용 후 GNOME은 로그아웃·로그인이 필요합니다.
[D-Bus 객체 경로 규칙](https://dbus.freedesktop.org/doc/dbus-specification.html#message-protocol-marshaling-object-path)에 따라
DB 이름에는 하이픈을 사용하지 않습니다.

### 선택: SSH

```bash
sudo bash ./setup-ssh.sh
# 관리 인터페이스의 zone을 지정할 경우
sudo bash ./setup-ssh.sh --ssh-zone public
```

기존 sshd 설정을 검사하고 현재 설정된 포트를 허용한 뒤 SSH/firewalld를 자동 시작합니다.
첫 설치에서는 배포판의 호스트 키 생성 target을 먼저 실행하며 기존 키를 교체하지 않습니다.
기존 SSH 세션의 반환 인터페이스, 기본 경로 NIC, 기본 zone 순으로 zone을 찾습니다.
VPN·다중 NIC 환경이면 `--ssh-zone`으로 관리 인터페이스의 zone을 지정하세요.
기존 규칙을 초기화하거나 SELinux·SSH 인증 설정을 바꾸지 않습니다.
기존 SSH 세션을 닫기 전에 새 연결로 접속을 확인하세요.

### 선택: 관리 도구

```bash
sudo bash ./setup-dev-tools.sh
```

CRB·EPEL 활성화 후 Neovim·btop·tmux를 설치합니다. 기본 실행에는 포함하지 않습니다.
uv 설치, 원격 데스크톱 구성, 기존 설치 도구 제거는 하지 않습니다.

### Docker

```bash
sudo bash ./setup-docker.sh
```

RHEL용 Docker 공식 저장소에서 Engine, Buildx, Compose 플러그인을 설치합니다.
Podman/runc 등 충돌 후보가 있으면 자동 삭제하지 않고 중단합니다.
이미 사용 중인 런타임과 컨테이너가 없는지 확인한 뒤 충돌을 해소하세요.

Rocky 설치 과정에서 Podman이 함께 설치되어 이 단계에서 중단될 수 있습니다.
현재 사용자와 root의 컨테이너·볼륨을 확인합니다. 종료된 컨테이너와 볼륨도 기존 데이터일 수 있습니다.

```bash
podman ps -a
sudo podman ps -a
podman volume ls
sudo podman volume ls
```

Podman을 사용하지 않는 서버라면 표시된 충돌 패키지만 `sudo dnf remove 패키지명`으로 제거한 뒤
동일한 통합 설치 명령을 재실행합니다. `-y` 없이 제거 목록을 확인하고 진행하세요.
패키지를 지우기 위해 컨테이너 저장 디렉터리를 수동 삭제할 필요는 없습니다.

기존 `daemon.json`은 백업·보존하며 기존 로그 정책이 없을 때만
`local` 드라이버에 `20m × 5개` 제한을 추가합니다.
기존 로그 정책이 있으면 덮어쓰지 않고 안내합니다.
Docker/containerd는 부팅 자동 시작합니다. docker 그룹에는 사용자를 자동 추가하지 않습니다.

실행 중인 Docker에 새 설정을 저장한 경우 자동 재시작하지 않습니다.
다음 명령은 **실행 중 컨테이너에 영향을 줄 수 있으므로** 초기 설치나 유지보수 시간에 사용하세요.

```bash
sudo bash ./setup-docker.sh --restart-docker
```

새 로그 정책은 새로 생성/재생성하는 컨테이너부터 적용됩니다.
Docker `ports`는 호스트 방화벽과 별개로 외부에 노출될 수 있습니다.
DB는 ports를 생략하거나 `127.0.0.1`에 바인딩하고, 필요한 API 포트만 명시적으로 공개하세요.

### NVIDIA GPU 드라이버

공통 `setup.sh`에서 별도 옵션 없이 `setup-nvidia.sh --auto`를 실행합니다.
PCI 벤더와 장치 class를 검사하여 NVIDIA GPU가 없으면 패키지 설치 없이 건너뜁니다.
NVIDIA 오디오·USB 장치만 있는 경우도 GPU로 판단하지 않습니다.
정상 `nvidia-smi`가 있으면 현재 드라이버를 유지합니다.

조회만 필요할 때는 다음 명령을 사용할 수 있습니다. 조회는 패키지나 설정을 변경하지 않습니다.

```bash
sudo bash ./setup-nvidia.sh --check
```

이 스크립트는 **x86_64 + NVIDIA open 커널 모듈 지원 GPU**만 설치 대상으로 합니다.
새 설치가 필요하면 [NVIDIA 공식 지원 표](https://github.com/NVIDIA/open-gpu-kernel-modules#compatible-gpus)의
장치·서브시스템 ID를 검사합니다. 지원 표는 데이터로만 읽으며 다운로드한 코드를 실행하지 않습니다.
레거시·미확인 GPU, 혼합 장치 중 미지원 GPU, 지원 표 다운로드·해석 실패는 중단 사유입니다.
Secure Boot가 활성화되어 있으면 MOK/서명 절차를 안내하고 중단하며 임의로 끄지 않습니다.

```bash
# GPU 단계만 다시 실행할 때 (공통 설치에서는 자동 호출)
sudo bash ./setup-nvidia.sh --auto
```

화면 출력과 추론을 모두 지원하는 desktop+compute 패키지인 `nvidia-open`을 설치합니다.
화면 없는 NVIDIA 서버에서도 사용할 수 있습니다.
CUDA Toolkit 전체는 설치하지 않습니다. 기존 드라이버가 정상 동작하면 교체하지 않습니다.
기존 패키지가 있지만 nvidia-smi가 실패하면 자동 수리 대신 커널/설치 상태 점검을 요청합니다.
실행 커널의 개발 디렉터리가 없으면 해당 버전의 `kernel-devel-$(uname -r)` 설치를 시도합니다.
저장소에 현재 커널용 패키지가 없거나 설치 파일이 손상되었으면 커널·저장소 정합성을 확인해야 합니다.

새 드라이버 설치는 종료 코드 `10`으로 재부팅 대기를 알립니다. `setup.sh`는 이를 설치 실패와 구분하여
공통 검사를 마친 뒤 재부팅을 안내합니다. `/var/lib/rocky-setup/nvidia-reboot-required`에 설치 당시 boot ID를
기록하고 실제 `nvidia-open` 패키지도 검사하므로 같은 부팅에서 반복 설치하지 않습니다.
재부팅 후 동일한 공통 설치 명령을 실행하면 `nvidia-smi`를 다시 확인하고 컨테이너 연결을 이어서 설정합니다.
재부팅 후에도 드라이버가 실패하면 반복 설치하지 않고 DKMS·커널·서명 점검을 요청합니다.
이전 `--install-open --confirm-open-compatible` 명령도 호환하지만 지원 표 검사를 우회하지 않습니다.

### 컨테이너 GPU

호스트 드라이버가 정상 동작하면 공통 설치가 자동 호출합니다. 필요한 경우에만 개별 실행합니다.

```bash
sudo bash ./setup-nvidia-container.sh
```

공식 저장소에서 NVIDIA Container Toolkit을 설치하고 기존 Docker 설정에 runtime을 병합합니다.
패키지와 runtime 설정이 이미 있으면 유지하고 현재 Docker 프로세스에 runtime이 등록되었는지도 검사합니다.
실행 중 Docker는 자동 재시작하지 않습니다. 유지보수 시간에는 아래처럼 적용할 수 있습니다.

```bash
sudo bash ./setup-nvidia-container.sh --restart-docker
sudo bash ./verify.sh --gpu
```

실제 컨테이너 GPU 확인은 GPU 드라이버와 호환되는 CUDA 이미지 태그를 선택하여 실행하세요.
태그를 정하지 않은 예시 이미지를 자동 다운로드하지 않습니다.

```bash
sudo docker run --rm --gpus all nvidia/cuda:<호환되는-태그> nvidia-smi
```

애플리케이션 배포는 이 스크립트 묶음의 범위에 포함하지 않습니다.
Docker 자동 시작과 별도로 각 앱의 컨테이너 재시작 정책·데이터 볼륨·백업을 구성해야 합니다.

### 검사 및 재부팅

```bash
sudo bash ./verify.sh
# OS 자동 업데이트 정책 검사 (GNOME 불필요)
sudo bash ./verify.sh --update-policy
# 화면 유지·GNOME/OS 업데이트 정책도 확인
sudo bash ./verify.sh --display-policy
# SSH를 선택 설치했다면
sudo bash ./verify.sh --ssh
# GPU도 구성했다면
sudo bash ./verify.sh --gpu
# 실행 중 Chrome의 옵션도 확인 (미실행은 대기)
sudo bash ./verify.sh --chrome
# 기대한 시작 URL까지 비교 (미실행은 실패)
sudo bash ./verify.sh --display-policy --ssh --chrome-url 'https://example.com'
```

`--user NAME`은 Chrome 화면 사용자이며 생략하면 sudo 실행 계정을 사용합니다.
시스템 검사는 root로 하고, 실행 인자 검사는 logind에서 발견한 사용자 runtime 경로로 연결해
해당 계정의 `chrome-did check`를 실행합니다. `DISPLAY` 번호를 임의로 지정하지 않습니다.
`chrome-did check`만 일반 사용자로 직접 실행해도 됩니다. 인자에 URL을 주면 시작 주소도 비교합니다.
조회 결과가 틀려도 설정·서비스를 고치지 않고 나머지 검사를 계속합니다.
영구 자동 시작과 런타임 자동 시작을 구분하고, 업데이트 unit은 masked 여부와 현재 비활성 여부를 함께 검사합니다.
설치된 실행 도구가 현재 소스와 다르면 setup-base.sh로 갱신하라는 실패를 표시합니다.
최종 요약과 종료 코드는 정상 `0`, 실패 `1`, 실행 대기 `2`입니다. 실패가 대기보다 우선합니다.
브라우저 검사는 MainPID의 실제 실행 인자를 대상으로 하며 현재 탭 이동·실제 전체화면 상태·팝업 화면은 검사하지 않습니다.

셸 변경은 재접속, Docker daemon 설정은 Docker 재시작, 커널/GPU 드라이버는 OS 재부팅이 필요할 수 있습니다.
기본 설정 단계는 `dnf needs-restarting -r` 결과를 안내하며 자동 재부팅하지 않습니다.
이 결과는 GPU 설치 지침을 대신하지 않습니다.
유지보수 시간에 직접 `sudo reboot` 후 서비스와 애플리케이션을 다시 검증하세요.
이 구성은 꺼진 PC의 전원을 자동으로 켜는 BIOS/Wake-on-LAN 설정을 포함하지 않습니다.

Tailscale/GNOME Remote Login은 계정 인증 및 기존 설정 확인이 필요하므로 이번 스크립트 범위에 포함하지 않았습니다.
SSH에서 표시할 폰트는 접속하는 Mac의 터미널에 설치합니다. 서버 GUI 폰트 설치는 포함하지 않습니다.

### 설치 기준과 공식 문서

최초 실행은 저장소의 현재 패키지를 설치하고, Oh My Bash는 설치 커밋을 기록합니다. 운영 환경에서 정확한 패키지 버전을 재현하려면
승인한 버전 목록/저장소 스냅샷을 별도로 고정해야 합니다.

- [Docker RHEL 설치](https://docs.docker.com/engine/install/rhel/)
- [NVIDIA Rocky Linux 설치](https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/rocky-linux.html)
- [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
- [RHEL 10 자동 업데이트 타이머](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/managing_software_with_the_dnf_tool/automating-software-updates-in-rhel)
- [GNOME Software 업데이트 설정](https://github.com/GNOME/gnome-software/blob/main/data/org.gnome.software.gschema.xml)

## 두 물리 서버에 적용할 때

두 서버는 필요한 CPU/GPU 작업을 실행하면서 각각 Chrome 전체화면으로 DID 웹 화면을 표시합니다.
구체적인 작업 배치는 애플리케이션에서 정하며 이 스크립트가 앱이나 DB를 이동·배포하지 않습니다.

| 장비 | 현재 역할 |
|---|---|
| CPU 서버 | CPU 자원이 필요한 무거운 작업 + Chrome 전체화면 DID 웹 표시 |
| GPU 서버 | GPU 자원이 필요한 무거운 작업 + Chrome 전체화면 DID 웹 표시 |
| 별도 DID 제어 인스턴스 | 패널 배치·영상 입력·장비 제어, 별도 담당자 관리 |

| 단계 | 적용 대상 | 판단 기준 |
|---|---|---|
| 기본 패키지·시간 동기화 | 두 서버 | Rocky Linux 10 |
| SSH | 관리에 필요한 서버 | 실제 접속 경로와 방화벽 zone |
| Docker | 컨테이너를 실행하는 서버 | 앱 실행 방식 |
| NVIDIA 드라이버·Container Toolkit | NVIDIA GPU가 감지된 서버 | 자동 감지·지원 표 검사, 정상 드라이버 유지 |
| GNOME 화면 유지 | 현재 두 서버 모두 | `--with-display`, Chrome 화면 꺼짐·절전 방지 |
| 자동 OS 업데이트 정책 | 두 서버 | GNOME 없이 실행 가능 |
| GNOME 업데이트 알림 정책 | 현재 두 서버 모두 | `--with-display` |
| DID 제어·패널 배치·영상 입력 | 별도 담당자가 관리하는 DID 제어 인스턴스 | 이 스크립트의 관리 범위 밖 |

상시 수업 분석에서 영상 감지나 Whisper를 실행하는 작업은 일반적으로 이미 학습된 모델의 추론입니다.
모델 재학습은 수업 중 실시간 서비스와 자원을 경쟁하지 않도록 별도로 계획합니다.
두 서버 모두 `sudo bash ./setup.sh --user user --with-root --with-ssh --with-display`로 공통 설정을 적용합니다.
향후 웹 화면을 표시하지 않는 서버는 `--with-display`를 생략할 수 있습니다.
10개 패널의 배치나 DID 입력을 선택할 필요 없이 서버 환경 설정을 진행할 수 있습니다.
DID 제어 담당자와는 서비스 주소·출력 방식 등 연동에 필요한 정보를 별도 확인합니다.
두 서버의 실제 앱 배포는 이 스크립트에 포함하지 않습니다.

Chrome 설치는 기본 단계에 포함합니다. DID 웹 URL 지정·로그인 후 전체화면 자동 실행은 별도 구성입니다.
현재 화면 정책만으로 재부팅 후 Chrome 표시가 자동 복구되지는 않습니다.
계산 작업과 Chrome을 함께 실행하므로 작업의 CPU·메모리·GPU 점유와 화면 응답은 실제 앱 배포 단계에서 함께 확인합니다.

## Bash 환경 설정

앞서 진입한 **Rocky 서버의 `rocky-linux` 폴더 안에서** 실행합니다.
`$(id -un)`은 현재 로그인한 사용자 이름입니다.

```bash
# 일반 사용자에게 적용
sudo bash ./setup-bash.sh --user "$(id -un)"

# 일반 사용자와 root에게 적용하려면 위 명령 대신 실행
sudo bash ./setup-bash.sh --user "$(id -un)" --with-root
```

### 적용 내용

- `dnf`로 Bash, Git, bash-completion 설치
- 대상 계정에 Oh My Bash 설치 및 서버용 테마 적용
- 사용자·호스트·현재 경로·Git 브랜치와 작업 상태 표시
- 일반 사용자는 초록색, root는 빨간색 프롬프트
- 지원하는 기존 로그인 셸(Bash, Zsh, Fish, sh)을 Bash로 통일
- 기존 `.bashrc`는 교체하지 않고 로딩 구문 추가
- Oh My Bash 자동 업데이트 비활성화 및 설치 커밋 기록

GitHub에 접속할 수 있어야 합니다. 최초 설치는 당시 Oh My Bash 기본 브랜치를 사용하며,
설치한 커밋은 `~/.config/server-bash/oh-my-bash.commit`에 기록합니다.
재실행 시 기존 설치를 자동 업데이트하지 않습니다.

별도 Powerline/Nerd Font는 필요하지 않습니다.
입력 중 명령어 추천과 구문 강조용 ble.sh는 포함하지 않습니다.
root SSH 로그인, 방화벽, 애플리케이션 설정은 변경하지 않습니다.

### 적용 확인

재부팅 없이 SSH로 다시 로그인하세요. root에 적용했다면 `sudo -i`로 확인합니다.

```bash
echo "$SHELL"
echo "$BASH_VERSION"
```

기존 로그인 프로필이 `.bashrc`를 읽지 않는 경우 경고를 출력합니다.
이 경우 해당 계정의 로그인 프로필에 아래 구문이 필요합니다.

```bash
if [ -f "$HOME/.bashrc" ]; then
  . "$HOME/.bashrc"
fi
```

### 기존 설정과 백업

다른 방법으로 설치한 `~/.oh-my-bash`가 있으면 덮어쓰지 않고 중단합니다.
기존 `.bashrc`에 별도 Oh My Bash 로딩 구문이 있어도 중복 적용을 중단합니다.
중단 전에 패키지 설치나 일부 파일 생성은 진행됐을 수 있습니다.

내용이 바뀌는 기존 파일만 원본과 같은 디렉터리의 `.<원본 파일명>.backup.<시각>.<PID>`로 백업합니다.
예를 들어 `.bashrc`의 백업은 `..bashrc.backup.<시각>.<PID>`입니다.
동일한 설정을 재실행하면 파일 수정 시각을 유지하며 새 백업을 만들지 않습니다.
기존 `.bash_profile`은 다시 쓰지 않습니다. 이전 버전의 `*.server-bash-backup.*` 백업도 그대로 남습니다.

되돌릴 때는 복원할 백업의 내용을 확인한 뒤 해당 계정의 `.bashrc`를 복원하세요.
백업 이후 변경한 내용이 있다면 전체 복원 대신 `# server-bash managed hook`부터
이어지는 로딩 구문만 제거하세요. 원래 로그인 셸이 Bash가 아니었다면 별도로 복원해야 합니다.
설치한 패키지와 Oh My Bash 디렉터리는 자동 삭제하지 않습니다.

## 검증 범위

로컬 검사는 패키지 설치나 실제 서비스 조작 없이 실행합니다.
이 명령 역시 `rocky-linux` 폴더 안에서 실행합니다.

```bash
python3 -m unittest discover -s tests -v
```

스크립트·생성 Bash 코드 문법, 도움말·잘못된 인자 처리, Docker 기존 설정 보존,
통합 실행 순서·선택 옵션 전달·실패 시 중단, 반복 실행 시 파일·백업·패키지 유지,
NVIDIA 감지·지원 표·재부팅 대기와 오래된 dconf DB 복구를 검사합니다.
실제 Rocky Linux 10에서 패키지 설치, 방화벽 적용, SSH 로그인, GPU 및 재부팅 복구 검증은
아직 수행하지 않았습니다.
