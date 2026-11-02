# ala — AI 학습 분석실 인스턴스 환경 관리

Rocky Linux 서버의 기본 환경·Docker·GPU 드라이버·Chrome DID 화면을 설치하고 검사하는 CLI/TUI입니다.

- 대상: **Rocky Linux 10 x86_64 + GNOME/GDM + 기존 sudo 사용자**
- 현재 배포 후보: **0.1.0rc10**. 실제 장비의 설치·GPU·화면·재부팅 수용은 아직 완료하지 않았습니다.
- **공개 릴리스에서 바로 다운로드합니다. Git·GitHub 로그인·토큰·Mac 파일 전송은 필요 없습니다.**

## 1. 설치 — Rocky 터미널

SSH로 서버에 접속한 일반 사용자 터미널에서 실행합니다. `curl`과 외부 HTTPS 연결이 필요합니다.
아래 블록은 다운로드나 검사 실패 시 멈추며, `sudo`는 설치할 때만 사용합니다.

```bash
(
  set -e
  VERSION='0.1.0rc10'
  RELEASE='v0.1.0-rc.10'
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

`0.1.0rc10`가 나오면 도구 설치 완료입니다. **서버 설정은 다음 단계에서 별도로 적용합니다.**
GitHub의 `Source code` 압축이나 `scripts/rocky-linux` 폴더만으로는 ala를 설치할 수 없습니다.
서버가 GitHub에 접속할 수 없으면 [Mac에서 받아 SCP로 전달](docs/INSTALL.md#mac에서-받아-scp로-전달)할 수 있습니다.

## 2. 간편 설정과 상태 확인

```bash
ala
```

시작할 때 sudo 인증을 받고 **간편 설정 / 상태 확인** 두 화면만 표시합니다.

**간편 설정 실행**을 누르면 저장된 구성으로 설치·설정을 한 번에 진행합니다. 처음에는 sudo 실행자의 일반 계정과 기본값을 사용합니다. 정상인 항목은 유지하며 계정·화면·변경 확인을 단계별로 다시 선택하지 않습니다.

**상태 확인**은 기능별로 의도한 설정과 실제 상태를 비교하고 문제의 원인·다음 조치를 보여줍니다. Tab·화살표·Enter로 조작하고 q로 종료합니다. 조회만으로 설정을 바꾸지 않습니다.

기본 환경은 패키지·Chrome, 시간 동기화, Bash, SSH·방화벽, Docker·Compose, GPU 자동 감지, 자동 업데이트 차단, 자동 로그인·화면 절전 방지를 포함합니다. DID 주소가 없으면 바탕화면부터 준비합니다. Chrome과 그래픽 작업은 일반 사용자로 실행합니다.

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
```

`sudo journalctl -u ala-update -f`에서 완료를 확인하고 ala를 다시 실행합니다. 기존 구성과 실행 기록은 유지합니다. RC5 이하에서는 최초 한 번 위 설치 절차로 새 버전을 받아야 합니다. 정기 자동 적용·서명 검증은 아직 제공하지 않습니다.

## 필요할 때만 쓰는 상세 기능

| 작업 | 명령 |
|---|---|
| DID 주소·선택 옵션 변경 | `ala config edit` |
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
