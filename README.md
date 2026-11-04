# ala — AI 학습 분석실 인스턴스 환경 관리

Rocky Linux의 기본 환경과 DID 화면을 SSH에서 설치·검사·관리합니다.
지원 환경은 **Rocky Linux 10 x86_64 + GNOME 47/GDM + 기존 sudo 사용자**입니다.

**0.1.0rc22 개발 후보**입니다. 자동 테스트와 실제 GNOME·모니터·재부팅 수용은 별도이며,
실기기 수용 전에는 전체 장비의 자동 운영을 켜지 않습니다. 기존 RC20 설치는 새 도구로 한 번 이전해야 합니다.

## 1. 설치

공개 GitHub 릴리스 묶음을 Rocky에서 받습니다. GitHub 로그인·토큰은 필요 없습니다.
후보가 게시된 뒤 다음 명령을 사용합니다. `Source code` 압축본은 설치 묶음이 아닙니다.

```bash
(
  set -e
  VERSION='0.1.0rc22'
  RELEASE='v0.1.0-rc.22'
  BASE_URL="https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/releases/download/${RELEASE}"
  mkdir -p "$HOME/ala-downloads/$VERSION"
  cd "$HOME/ala-downloads/$VERSION"
  curl -fL --proto '=https' --retry 3 "$BASE_URL/ala-$VERSION.tar.gz" -o "ala-$VERSION.tar.gz"
  curl -fL --proto '=https' --retry 3 "$BASE_URL/ala-$VERSION.tar.gz.sha256" -o "ala-$VERSION.tar.gz.sha256"
  sha256sum -c "ala-$VERSION.tar.gz.sha256"
  tar -xzf "ala-$VERSION.tar.gz"
  cd "ala-$VERSION"
  sudo bash ./install.sh
  ala --version
)
```

이 단계는 도구만 설치합니다. 서버 설정은 아래의 간편 설정에서 적용합니다.
서버에서 다운로드할 수 없으면 [Mac 다운로드·SCP 전달](docs/INSTALL.md#mac에서-받아-scp로-전달)을 사용합니다.

## 2. 간편 설정

```bash
ala
```

sudo 인증 후 **간편 설정 / 상태 확인 / 화면 관리**가 나옵니다.

- **간편 설정:** 필요한 패키지·Docker·GPU·SSH·Bash·시간 동기화·업데이트 차단·자동 로그인·Chrome 부팅 등록을 준비합니다. 현재 Chrome은 켜거나 끄지 않습니다.
- **상태 확인:** 기대 구성과 실제 상태, 원인과 다음 조치를 읽기 전용으로 확인합니다.
- **화면 관리:** 연결된 출력, 대상별 사이트·실행·배치·사이트 건강을 확인하고 창을 제어합니다.

정상 설정은 유지하며, 적용 전후 실제 상태를 검사합니다. 설치 중 SSH/TUI를 닫아도 접수된 작업은 계속됩니다.
문제가 있는 항목은 자동 삭제·보안 해제로 우회하지 않습니다. OS 재부팅은 직접 수행합니다.

Tab·화살표·Enter로 조작합니다. `g` 간편 설정, `s` 상태 확인, `c` 화면 관리, `u` 업데이트, `q` 종료입니다.
간편 설정 표의 큰 기능은 Enter로 펼쳐 세부 작업을 확인합니다.

## 3. 화면 관리

`c`에서 표시 대상을 추가하거나 사이트·출력·자동 시작을 설정합니다.
기존 대상의 설정을 저장한 뒤 **설정 적용**을 선택하면 현재 표시에도 반영됩니다.
새 대상의 서비스 등록은 간편 설정에서 준비합니다.

- 출력 배치도는 PC가 인식한 논리 화면입니다. 외부 DID 장비의 패널 수를 추정하지 않습니다.
- 물리 출력 행에서 HDMI 연결을 확인할 수 있습니다. 출력 선택 목록에는 GNOME의 활성 출력이 표시됩니다.
- 기본은 모니터당 전체화면 하나이며, 한 모니터에서 두 창을 시험할 때만 좌우 분할을 선택합니다.
- 서비스 실행, 실제 관리 창 배치, 사이트 응답을 따로 표시합니다. 요청 접수만으로 완료라고 표시하지 않습니다.
- 사이트 장애에는 로컬 ‘연결 복구 중’ 안내를 표시하고 복구 시 원래 화면으로 돌아갑니다.
- 프론트 새 배포 갱신은 [배포 계약](docs/frontend-contract.md)을 연결한 대상에서만 동작합니다. 동일 버전에는 반복 새로고침하지 않습니다.

```bash
ala display list --json
ala chrome start --target default
ala chrome stop --target default
ala chrome reload --target default
```

기존 단일 프로필은 보존합니다. RC20의 관리 코드가 남아 있으면 먼저 새 코드로 이전하고 다중 화면 구성을 저장합니다.
[화면·GNOME·부팅 설명](docs/DISPLAY.md)을 참고하세요.

## 4. 업데이트

```bash
ala update --check          # 정식판 조회
ala update --prerelease     # 시험판을 명시적으로 설치
ala update --status         # CLI 설치 완료·실패 확인
ala update --policy         # 자동 운영 준비와 정책 확인
```

**CLI 설치가 실행 중인 Chrome을 재시작하거나 새로고침하지 않습니다.**
화면별 관리 버전은 별도로 유지하고, 자동 운영이 활성화되면 매일 **03:00–04:00 한국 시간**에 하나씩 전환합니다.
프론트 배포 갱신은 이 야간 일정과 별개로 준비된 새 배포를 감지한 뒤 순차 적용합니다.
OS·Chrome RPM·GPU 드라이버와 자동 재부팅은 포함하지 않습니다.

최초 자동 운영 준비·화면 코드 이전·실기기 수용 확인은 `u → 자동 운영`에서 진행합니다.
서명 검증기와 현재 배포 검증이 필요하며, 실제 수용 확인 전에는 자동 운영을 활성화할 수 없습니다.
[업데이트·복원·최초 이전](docs/UPDATES.md)에 절차와 명령을 정리했습니다.

## 5. 문제 확인

```bash
ala check --json            # 전체 검사
ala setup --plan --json     # 변경 없는 계획
ala runs list              # 설치 기록
ala doctor network         # 연결을 바꾸지 않는 네트워크 진단
ala prepare --plan         # 별도 최초 OS 준비 계획
```

화면 문제는 `c`에서 해당 대상을 선택해 로그와 다음 조치를 확인합니다.
출력 조회 실패를 모니터 없음으로, 패키지 설치를 통신 정상으로 표시하지 않습니다.
실제 화면·전체화면·팝업 부재는 모니터 관찰로 수용해야 합니다.

- [설치·Mac 전송·빌드](docs/INSTALL.md)
- [운영·복구](docs/OPERATIONS.md) · [초기 준비](docs/PREPARATION.md) · [드라이버](docs/DRIVERS.md)
- [실기기 수용](docs/ACCEPTANCE.md) · [공개 데이터 보호](docs/PUBLIC-SAFETY.md)

실제 계정·학교 주소·IP·토큰·장비 목록·운영 로그는 공개 저장소나 이슈에 올리지 마세요.
