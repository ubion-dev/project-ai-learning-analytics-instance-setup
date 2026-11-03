# ala — AI 학습 분석실 인스턴스 환경 관리

Rocky Linux 서버의 기본 환경·Docker·GPU 드라이버·Chrome DID 화면을 설치하고 검사하는 CLI/TUI입니다.

- 대상: **Rocky Linux 10 x86_64 + GNOME/GDM + 기존 sudo 사용자**
- 현재 배포 후보: **0.1.0rc19**. 실제 장비의 설치·GPU·화면·재부팅 수용은 아직 완료하지 않았습니다.
- **공개 릴리스에서 바로 다운로드합니다. Git·GitHub 로그인·토큰·Mac 파일 전송은 필요 없습니다.**

## 1. 설치 — Rocky 터미널

SSH로 서버에 접속한 일반 사용자 터미널에서 실행합니다. `curl`과 외부 HTTPS 연결이 필요합니다.
아래 블록은 다운로드나 검사 실패 시 멈추며, `sudo`는 설치할 때만 사용합니다.

```bash
(
  set -e
  VERSION='0.1.0rc19'
  RELEASE='v0.1.0-rc.19'
  BASE_URL="https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/releases/download/${RELEASE}"
  mkdir -p "$HOME/ala-downloads/$VERSION"
  cd "$HOME/ala-downloads/$VERSION"

  curl -fL --proto '=https' --retry 3 \
    "$BASE_URL/ala-$VERSION.tar.gz" -o "ala-$VERSION.tar.gz"
  curl -fL --proto '=https' --retry 3 \
    "$BASE_URL/ala-$VERSION.tar.gz.sha256" -o "ala-$VERSION.tar.gz.sha256"
  sha256sum -c "ala-$VERSION.tar.gz.sha256"

  tar -xzf "ala-$VERSION.tar.gz"
  cd "ala-$VERSION"
  sudo bash ./install.sh
  ala --version
)
```

`0.1.0rc19`가 나오면 도구 설치 완료입니다. **서버 설정은 다음 단계에서 별도로 적용합니다.**
GitHub의 `Source code` 압축이나 `scripts/rocky-linux` 폴더만으로는 ala를 설치할 수 없습니다.
서버가 GitHub에 접속할 수 없으면 [Mac에서 받아 SCP로 전달](docs/INSTALL.md#mac에서-받아-scp로-전달)할 수 있습니다.

## 2. 간편 설정과 상태 확인

```bash
ala
```

시작할 때 sudo 인증을 받고 **간편 설정 / 상태 확인** 두 화면만 표시합니다.

**간편 설정 실행**을 누르면 저장된 구성으로 설치·설정을 한 번에 진행합니다. 처음에는 sudo 실행자의 일반 계정과 기본값을 사용합니다. 정상인 항목은 유지하며 계정·화면·변경 확인을 단계별로 다시 선택하지 않습니다. 진행 표는 항목별 현재 상태를 한 행에서 갱신합니다. 완료·정상 유지는 초록, 진행 중은 청록, 적용 대기는 노랑, 차단·실패는 빨강이며 문자·기호도 함께 표시합니다.

설정 표에서 큰 기능을 선택한 뒤 **Enter**로 세부 항목을 펼치거나 접습니다. 기본 환경의 패키지·Chrome 정책·시간대·시간 동기화처럼 어떤 설정이 포함되는지 볼 수 있습니다. **세부 작업** 열은 실제 설치 신호가 있는 현재 작업을 표시하며, 신호가 없는 항목의 진행 상태를 추측하지 않습니다.

계정 정보·실행 버튼, 작업 요약, 진행 표, 선택 항목의 원인·다음 조치를 구분해 표시합니다. 터미널이 넓으면 열 폭과 여백을 늘리고, 80×24에서는 압축 배치를 사용합니다. 긴 목록은 화살표로 스크롤할 수 있습니다.

처음에는 계정·기존 실행 작업을 먼저 표시하고 환경 검사를 진행합니다. 검사 중 경과 시간을 보여주며, 갱신 중에는 이전 결과임을 표시합니다. 개별 세부 기록이 없는 항목과 실제 조회 확인이 필요한 항목을 구분합니다.

**상태 확인**은 기능별로 의도한 설정과 실제 상태를 비교하고 문제의 원인·다음 조치를 보여줍니다. Tab·화살표·Enter로 조작하고 q로 종료합니다. 조회만으로 설정을 바꾸지 않습니다.

기본 환경은 패키지·Chrome, 시간 동기화, Bash, SSH·방화벽, Docker·Compose, GPU 자동 감지, 자동 업데이트 차단, 자동 로그인·화면 절전 방지를 포함합니다. DID 주소가 없으면 바탕화면부터 준비합니다. Chrome과 그래픽 작업은 일반 사용자로 실행합니다.

간편 설정은 Chrome 자동 시작 등록까지 준비하며 현재 Chrome 창을 열거나 닫지 않습니다. **화면 관리** 버튼 또는 **c**에서 시작·중지·재시작·상태·로그를 관리합니다. 현재는 단일 표시용 Chrome을 관리하며 모니터별 배치는 별도 준비 중입니다. `자동 설정 대상`은 사용자가 파일을 직접 고치라는 뜻이 아닙니다.

자동 로그인·Chrome 자동 시작이 켜져 있으면 다음 정상 부팅 시 **GDM 자동 로그인 → GNOME 자동 시작 → 표시용 Chrome 서비스** 순서로 실행합니다. 설정 준비 완료와 현재 실행·사이트 상태를 나눠 표시합니다. 처음 구성한 장비는 직접 재부팅한 뒤 SSH로 재접속하고 모니터의 실제 표시를 확인하세요.

표시 사이트는 실행 중에도 감시합니다. 사이트 장애가 확인되면 로컬 복구 안내를 표시하고, 복구가 확인되면 원래 주소로 돌아갑니다. 정상 화면을 일정 주기로 새로고침하지 않습니다. 화면 관리 상세에서 ala 탐색 요청과 브라우저 문서 이동 횟수를 구분해 확인할 수 있습니다. 실행 중인 이전 감시에는 새 기록이 없으므로, 새 진단은 업데이트 후 화면 관리에서 명시적으로 재시작한 뒤 적용됩니다. HTTP 응답·관리 탭 상태와 실제 앱 데이터·모니터 표시의 정상 여부는 구분합니다.

TUI 대신 명령으로 바로 실행할 수도 있습니다.

```bash
ala setup --apply   # 간편 설치·설정
ala check           # 기대 구성과 실제 상태 검사
ala status          # 상태·진행 중 작업
```

SSH나 화면을 닫아도 접수된 설치는 계속됩니다. 재부팅이 필요하면 안내 후 직접 진행하고 `ala check`로 다시 확인하세요. Podman 충돌 등 조치가 필요한 항목은 자동 삭제로 해결하지 않으며 결과에 원인과 다음 행동을 표시합니다.

## 3. 자체 업데이트

```bash
ala update --check        # 정식판 확인
ala update               # 정식판으로 갱신
ala update --prerelease   # 시험판 포함 갱신
ala update --status       # 마지막 갱신의 완료·실패와 설치 버전
```

`ala update`는 완료·실패까지 추적합니다. SSH나 표시를 종료해도 접수된 작업은 계속되며, 다시 접속한 뒤 `ala update --status`로 확인합니다. 즉시 반환하려면 `--detach`를 사용합니다. TUI에서는 `u`로 업데이트 관리창을 열어 확인·갱신·결과 조회를 할 수 있습니다. 새 버전 설치가 끝나면 ala를 다시 실행합니다.

기존 구성과 실행 기록은 유지합니다. RC15 이하의 업데이트 화면은 journal에 의존하므로 이번 버전으로 최초 갱신할 때는 `sudo journalctl -u ala-update -f`에서 완료를 확인하고 다시 실행하세요. 정기 자동 적용·서명 검증은 아직 제공하지 않습니다.

## 필요할 때만 쓰는 상세 기능

| 작업 | 명령 |
|---|---|
| DID 주소·선택 옵션 변경 | `ala config edit` |
| 현재 Chrome 시작·중지·재시작·상태·로그 | TUI의 `c` / 화면 관리 또는 `ala chrome` |
| 구성 확인 | `ala config show` |
| 변경 없이 전체 결과 확인 | `ala check --json`, `ala setup --plan --json` |
| 최초 전체 OS 준비 | `ala prepare --plan`, `ala prepare --apply` |
| Wi-Fi 진단 | `ala doctor network` |
| 실행 기록·로그 | `ala runs list`, `ala logs --run RUN_ID --follow` |
| 간편 관리 TUI | `ala tui` (기본 ala와 동일) |
| 텍스트 콘솔 | `ala console` |

`ala`는 관리자 인증을 안내하며 직접 `sudo /usr/local/bin/ala`로 실행해도 됩니다. root로 직접 로그인했다면 화면에 사용할 일반 계정을 먼저 지정하세요. 도움말·버전·`ala update --check`는 인증하지 않습니다. 비대화형 CLI는 sudo 인증이 없으면 암호를 기다리지 않고 오류로 종료합니다.

## 상세 문서

- [설치·Mac 전송·빌드·버전 갱신](docs/INSTALL.md)
- [초기 OS 준비·Wi-Fi·자동 로그인](docs/PREPARATION.md)
- [운영·복구·자동화](docs/OPERATIONS.md) · [드라이버 정책](docs/DRIVERS.md)
- [실기기 검증 기준](docs/ACCEPTANCE.md) · [공개 코드 구성](docs/SOURCE.md)

학교별 실제 구성·장비 주소·계정·토큰·운영 로그를 공개 이슈나 저장소에 올리지 마세요.

개발 기여 전에는 [공개 데이터 보호 규칙과 Git 훅 설치](docs/PUBLIC-SAFETY.md)를 확인하세요.
