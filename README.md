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

SSH로 접속한 **Rocky에서** 실행합니다. 아래는 공개 시험판 RC34의 설치 명령입니다. [Mac에서 받아 SCP로 전달](docs/INSTALL.md#mac에서-받아-scp로-전달)

```bash
(
  set -e
  version='0.1.0rc36'
  tag='v0.1.0-rc.36'
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

`ala`는 번호를 입력하는 텍스트 메뉴를 엽니다. 화면 지우기·마우스·주기적인 전체 화면 갱신을 사용하지 않습니다.
DID 전용 계정을 쓸 경우 `ala config edit`에서 기존 일반 사용자를 먼저 지정합니다.

- `1 간편 설정`: 저장한 구성으로 설치하고 바뀐 진행 항목만 출력합니다.
- `3 사이트·Chrome` 또는 `ala screen`: 대상 번호로 사이트·출력·자동 시작을 편집하고 저장·적용합니다. 운영·로그·배포 재시도·두 사이트 교환도 제공합니다.
- `4 모니터 배치·해상도` 또는 `ala display edit`: 모니터 번호로 좌표·상대 위치·위치 교환·지원 모드·배율을 편집합니다. 세 대 이상·상하 배치를 지원합니다.
- `5 업데이트`: 새 버전·작업 상태를 명시적으로 조회합니다. `6 고급`에서 설정·실행 기록·진단·복구를 사용합니다.

Enter는 기존 값을 유지하고 `b`는 돌아가기입니다. 변경은 `yes`로 확인해야 실행합니다.
모니터 배치는 검토·적용 후 실제 화면을 확인해 30초 안에 유지합니다. 미응답 시 기존 서버 복원 절차가 동작합니다.
기존 GNOME 확장의 갱신·재로그인 요구는 그대로이며 이번 텍스트 메뉴 전환만을 위한 재부팅은 필요하지 않습니다.
[텍스트 메뉴 상세](docs/TERMINAL.md) · [화면 관리와 복구](docs/DISPLAY.md)

기존 전체 화면은 `ala tui`로 명시 실행할 때만 사용합니다. 기본 메뉴는 Rich/Textual을 불러오지 않으며 기존 설정과 Chrome 코드를 자동 교체하지 않습니다.

## 6. 동작 확인

`ala check`로 설정 상태를 보고, 재부팅 후 SSH 재접속과 Chrome 자동 표시를 확인합니다. [실기기 확인 항목](docs/ACCEPTANCE.md)

[최초 OS 업데이트](docs/PREPARATION.md) · [ala 업데이트](docs/UPDATES.md) · [운영과 문제 해결](docs/OPERATIONS.md)
