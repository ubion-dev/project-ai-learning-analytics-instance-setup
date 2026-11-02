# ala — AI 학습 분석실 인스턴스 환경 관리

Rocky Linux 서버의 기본 환경·Docker·GPU 드라이버·Chrome DID 화면을 설치하고 검사하는 CLI/TUI입니다.

- 대상: **Rocky Linux 10 x86_64 + GNOME/GDM + 기존 sudo 사용자**
- 현재 배포 후보: **0.1.0rc6**. 실제 장비의 설치·GPU·화면·재부팅 수용은 아직 완료하지 않았습니다.
- **공개 릴리스에서 바로 다운로드합니다. Git·GitHub 로그인·토큰·Mac 파일 전송은 필요 없습니다.**

## 1. 설치 — Rocky 터미널

SSH로 서버에 접속한 일반 사용자 터미널에서 실행합니다. `curl`과 외부 HTTPS 연결이 필요합니다.
아래 블록은 다운로드나 검사 실패 시 멈추며, `sudo`는 설치할 때만 사용합니다.

```bash
(
  set -e
  VERSION='0.1.0rc6'
  RELEASE='v0.1.0-rc.6'
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

`0.1.0rc6`가 나오면 도구 설치 완료입니다. **서버 설정은 다음 단계에서 별도로 적용합니다.**
GitHub의 `Source code` 압축이나 `scripts/rocky-linux` 폴더만으로는 ala를 설치할 수 없습니다.
서버가 GitHub에 접속할 수 없으면 [Mac에서 받아 SCP로 전달](docs/INSTALL.md#mac에서-받아-scp로-전달)할 수 있습니다.

## 2. 구성·환경 적용

어느 폴더에서든 일반 사용자로 실행합니다. 필요한 작업에서만 sudo 인증을 받습니다.

```bash
ala
```

1. **처음 설정하기**를 선택합니다. 저장된 구성이 있으면 **현재 설정 점검·보완**으로 표시됩니다.
2. **계정 확인:** 바탕화면에 로그인할 기존 일반 계정을 확인합니다. 필요한 sudo 인증은 이 흐름에서 안내합니다.
3. **화면 선택:** DID 주소가 없으면 바탕화면만, 주소가 있으면 **DID 웹페이지를 전체화면으로 표시**를 선택합니다.
4. **변경 확인 → 설정 시작:** 필요한 설치·설정과 이미 정상인 항목을 확인하고 시작합니다.
5. **진행·결과:** 이 화면에서 다음 조치를 확인합니다. SSH를 종료해도 시작한 설치는 계속됩니다.

화살표·Tab으로 이동, Enter로 실행, Space로 선택, Esc로 홈, `q`로 종료합니다.
변경 확인 단계에서는 선택한 구성만 저장하며, **설정 시작 전에는 패키지 설치나 OS 설정을 적용하지 않습니다.**
취소해도 저장된 선택은 남습니다. 기존 고급 옵션은 보존하며, 이 흐름은 자동 로그인과 화면 절전 방지를 켭니다.

첫 화면의 다른 메뉴는 **서버 상태 확인**, **화면 관리**, **고급 설정**입니다.
상세 패키지 옵션, 초기 OS 업데이트, 실행 기록은 **고급 설정**에 있습니다.

메뉴 대신 명령으로도 실행할 수 있습니다.

```bash
ala config edit
ala setup --plan      # 조회만
ala setup --apply     # 설치·설정 반영
ala check             # 검사만
```

최초 OS 업데이트를 원하면 환경 적용 전에 별도 `ala prepare --plan` / `ala prepare --apply`를 사용합니다.
재부팅 대기이면 직접 재부팅하고 SSH 재접속 후 `ala prepare --apply`로 부팅 결과를 확인합니다. [초기 준비 상세](docs/PREPARATION.md)
`setup`은 전체 OS 업데이트를 하지 않습니다. 드라이버·화면 설정의 재부팅 대기도 직접 처리하고, `ala check` 및 실제 모니터로 결과를 확인합니다.

## 3. ala 자체 업데이트

**새 릴리스의 버전으로 1번 설치 블록의 `VERSION`과 `RELEASE`를 바꿔 다시 실행합니다.**
새 버전 설치·검증에 성공하면 ala 명령이 새 버전으로 전환되며, 기존 구성과 실행 기록은 유지합니다.
설치 준비가 실패하면 기존 버전을 유지합니다. 같은 버전의 다른 파일은 덮어쓰지 않습니다.

CLI 갱신만으로 환경 설정이나 재부팅을 실행하지 않습니다. 새 설정을 반영하려면 `ala setup --plan`으로 확인하고 적용하세요.
**주기적 자동 업데이트·서명 검증·중앙 상태 보고는 아직 구현되지 않았습니다.**
현재는 버전을 지정한 수동 다운로드·설치 방식입니다. [버전 갱신 상세](docs/INSTALL.md#버전-갱신)

## 자주 쓰는 명령

| 작업 | 명령 |
|---|---|
| 상태·검사 | `ala status`, `ala check` |
| 구성 확인·수정 | `ala config show`, `ala config edit` |
| Wi-Fi 진단 | `ala doctor network` |
| Chrome 시작·중지·재시작 | `ala chrome start`, `ala chrome stop`, `ala chrome restart` |
| Chrome 상태·로그 | `ala chrome status`, `ala chrome logs` |
| 설치 기록·로그 | `ala runs list`, `ala runs show RUN_ID`, `ala logs --run RUN_ID --follow` |
| 도움말 | `ala --help` |

`RUN_ID`는 실행 기록의 값입니다. SSH나 TUI를 닫아도 접수된 설치 작업은 계속됩니다.
Chrome `stop`은 현재 실행만 중지합니다. 다음 부팅에도 끄려면 구성에서 자동 실행을 끄고 적용합니다.

## 상세 문서

- [설치·Mac 전송·빌드·버전 갱신](docs/INSTALL.md)
- [초기 OS 준비·Wi-Fi·자동 로그인](docs/PREPARATION.md)
- [운영·복구·자동화](docs/OPERATIONS.md) · [드라이버 정책](docs/DRIVERS.md)
- [실기기 검증 기준](docs/ACCEPTANCE.md) · [공개 코드 구성](docs/SOURCE.md)

학교별 실제 구성·장비 주소·계정·토큰·운영 로그를 공개 이슈나 저장소에 올리지 마세요.

개발 기여 전에는 [공개 데이터 보호 규칙과 Git 훅 설치](docs/PUBLIC-SAFETY.md)를 확인하세요.

### ala 자체 업데이트

이 기능이 포함된 버전부터 `ala` 첫 화면에서 정식 새 버전을 안내합니다. 확인 실패가 서버 관리를 막지는 않습니다. RC5 이하에서는 아래 명령이 없으므로 먼저 설치 절차로 새 버전을 한 번 설치하세요.

```bash
ala update --check              # 정식 버전 확인만
ala update                     # 정식 버전 다운로드·검증·갱신 시작
ala update --check --prerelease # 시험판까지 확인
ala update --prerelease        # 시험판 포함 갱신
sudo journalctl -u ala-update -f
ala --version
```

GitHub 로그인은 필요 없습니다. 갱신은 sudo 인증 후 systemd에서 계속되며 SSH를 종료해도 유지됩니다. 로그에서 설치 완료를 확인한 뒤 ala를 다시 실행하세요. 구성과 실행 기록은 유지하며 OS 업데이트·재부팅·Chrome 재시작은 하지 않습니다. 자동 적용 타이머는 없습니다. 정식판이 아직 없으면 기본 채널에서는 새 버전 없음으로 표시합니다.
