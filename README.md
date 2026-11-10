# ala

Rocky Linux 10 x86_64에서 서버 환경과 DID 화면을 SSH로 설정합니다. DID는 GNOME Shell 47·49/GDM을 지원합니다.

## 1. Rocky Linux 설치

DVD ISO로 부팅하고 **서버 GUI 사용**을 선택합니다. 일반 계정을 만들고 **관리자로 지정**합니다. [1TB·2TB 파티션과 부트로더 설정](docs/INSTALL.md#rocky-linux-설치)

## 2. 최초 SSH 준비

Rocky의 로컬 콘솔에서 SSH 서비스를 켜고 서버 IP를 확인합니다.

```bash
sudo systemctl enable --now sshd
ip -br -4 address
```

[SSH 패키지·방화벽·sudo 설정](docs/INSTALL.md#최초-ssh-준비)

## 3. Mac에서 SSH 접속

Mac 터미널에서 `user`와 `서버IP`를 실제 값으로 바꿉니다.

```bash
ssh user@서버IP
```

## 4. ala 설치

SSH로 접속한 **Rocky에서** 실행합니다. 아래는 공개 시험판 RC37의 설치 명령입니다. [Mac에서 받아 SCP로 전달](docs/INSTALL.md#mac에서-받아-scp로-전달)

```bash
(
  set -e
  version='0.1.0rc37'
  tag='v0.1.0-rc.37'
  archive="ala-${version}.tar.gz"
  url="https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/releases/download/${tag}"
  mkdir -p "$HOME/ala-downloads/$version"
  cd "$HOME/ala-downloads/$version"
  curl -fLO --proto '=https' --retry 3 "$url/$archive"
  curl -fLO --proto '=https' --retry 3 "$url/$archive.sha256"
  sha256sum -c "$archive.sha256"
  tar -xzf "$archive"
  cd "ala-$version"
  sudo bash ./install.sh
  ala --version
)
```

## 5. 간편 설정과 DID 등록

`ala`는 간편 설정·상태 확인·화면 관리 TUI를 엽니다. 상태 카드와 표, 선택한 항목의 원인과 다음 조치를 함께 표시합니다.
DID 전용 계정을 쓸 경우 `ala config edit`에서 기존 일반 사용자를 먼저 지정합니다.
간편 설정을 실행한 뒤 화면 관리에서 모니터를 선택해 사이트를 저장하고 설정을 적용합니다.

`ala screen` / `ala chrome`은 화면 관리 TUI, `ala display edit`는 모니터 배치·해상도 편집 TUI를 바로 엽니다.
지원 해상도·화면 배율과 좌표·위치를 편집하고 검토·적용 뒤 30초 안에 실제 화면을 확인해 유지합니다.
확인하지 않으면 기존 서버 복원 절차가 동작합니다. 기존 사이트·프로필·코드 선택은 보존합니다.

텍스트 메뉴가 필요한 경우에만 `ala console`을 명시 실행합니다. 이번 기본 TUI 복원 때문에
GNOME 재로그인·재부팅·간편 설정 재실행은 필요하지 않습니다.
[화면 관리와 복구](docs/DISPLAY.md) · [선택 텍스트 메뉴](docs/TERMINAL.md)

## 6. 동작 확인

`ala check`로 설정 상태를 보고, 재부팅 후 SSH 재접속과 Chrome 자동 표시를 확인합니다. [실기기 확인 항목](docs/ACCEPTANCE.md)

[최초 OS 업데이트](docs/PREPARATION.md) · [ala 업데이트](docs/UPDATES.md) · [운영과 문제 해결](docs/OPERATIONS.md)
