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

SSH로 접속한 **Rocky에서** 실행합니다. 아래는 공개 시험판 RC29의 설치 명령입니다. [Mac에서 받아 SCP로 전달](docs/INSTALL.md#mac에서-받아-scp로-전달)

```bash
(
  set -e
  version='0.1.0rc29'
  tag='v0.1.0-rc.29'
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

DID 전용 계정을 쓸 경우 `ala config edit`에서 화면 사용자를 먼저 지정합니다. `ala`를 열어 **간편 설정 실행**을 누르고, **화면 관리**에서 사이트 주소를 저장한 뒤 **설정 적용**을 선택합니다. [설치 항목](docs/OPERATIONS.md#설치설정-항목-관리) · [화면 관리](docs/DISPLAY.md)

새 장비는 모니터별 안내 화면 준비를 확인하면 연결된 모든 논리 화면에 설정 안내를 띄웁니다.
화면 관리의 **모니터 식별**로 번호를 확인하고 모니터 그림을 클릭해 주소를 설정합니다.
방향키와 Enter로도 열 수 있으며 운영·로그 기능은 각 모니터의 설정 창에서 사용합니다. 전체화면 DID 두 개는
드래그 후 확인하여 출력 위치를 교환할 수 있습니다. 기존 장비의 사이트와 자동 시작 선택은 보존합니다.

## 6. 동작 확인

`ala check`로 설정 상태를 보고, 재부팅 후 SSH 재접속과 Chrome 자동 표시를 확인합니다. [실기기 확인 항목](docs/ACCEPTANCE.md)

[최초 OS 업데이트](docs/PREPARATION.md) · [ala 업데이트](docs/UPDATES.md) · [운영과 문제 해결](docs/OPERATIONS.md)
