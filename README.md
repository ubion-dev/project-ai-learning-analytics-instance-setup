# ala — AI 학습 분석실 DID 인스턴스 환경 관리

SSH 터미널에서 Rocky Linux 서버의 설치·설정·검증·Chrome 화면을 관리합니다.
CPU/GPU 작업 환경을 준비하면서 GNOME에서 DID 웹 페이지를 F11 방식 전체화면으로 표시합니다.

> 현재 코드는 `0.1.0rc3` 배포 후보입니다. 자동 테스트와 실제 장비 수용은 별개입니다.
> CPU/GPU 장비의 재부팅·실제 화면 수용이 끝나기 전에는 검증된 정식 운영 버전으로 취급하지 마세요.

## 처음 설치하기 — Mac → Rocky

지원: **Rocky Linux 10 x86_64 + GNOME/GDM + 기존 sudo 사용자**.
물리 서버 각각에 설치하며 Mac에서는 SSH로 조작합니다. OS 설치와 파티션 설정은 먼저 완료해야 합니다.

### 1. Mac에서 배포 파일 받기·전송

Mac의 GitHub CLI(`gh`)가 비공개 저장소에 접근할 수 있어야 합니다. 서버에는 GitHub 로그인이나 저장소 전체 복제가 필요하지 않습니다.
이미 배포 파일 두 개가 있다면 다운로드를 생략하고 해당 폴더에서 검사·전송합니다.

```bash
mkdir -p ~/Downloads/ala-0.1.0rc3-release
cd ~/Downloads/ala-0.1.0rc3-release

gh release download v0.1.0-rc.3 \
  --repo ubion-dev/project-ai-learning-analytics-instance-setup \
  --pattern 'ala-0.1.0rc3.tar.gz*'
shasum -a 256 -c ala-0.1.0rc3.tar.gz.sha256
```

검사 결과가 `OK`이면 전송합니다. 아래 `user`와 `서버IP`를 **현재 SSH 접속에 사용하는 계정·주소**로 바꾸세요.

```bash
TARGET='user@서버IP'
scp ala-0.1.0rc3.tar.gz ala-0.1.0rc3.tar.gz.sha256 "${TARGET}:~/"
ssh "$TARGET"
```

`gh`를 사용하지 않으면 [RC3 릴리스](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/releases/tag/v0.1.0-rc.3)에서 두 파일을 내려받아 같은 방식으로 전송합니다.
SSH 호스트 키 변경 경고가 나오면 장비·키를 확인하고 진행합니다. 검사를 끄거나 저장된 키를 무조건 삭제하지 않습니다.

### 2. Rocky SSH 터미널에서 ala 설치

```bash
cd ~
sha256sum -c ala-0.1.0rc3.tar.gz.sha256
```

`OK`를 확인한 다음 실행합니다.

```bash
tar -xzf ala-0.1.0rc3.tar.gz
cd ala-0.1.0rc3
sudo bash ./install.sh
ala --version
```

버전이 `0.1.0rc3`으로 나오면 도구 설치가 완료된 것입니다. **Docker·드라이버·자동 로그인 등 서버 환경은 이후 `setup --apply`로 적용합니다.**
설치 후에는 어느 폴더에서든 `ala`를 실행할 수 있으며 다운로드·압축 해제 폴더를 지워도 동작합니다.
설치기 전제 조건, 개발 빌드와 버전 갱신은 [설치 안내](docs/INSTALL.md)를 참고하세요.

### 3. 화면 사용자와 운영 구성 저장

```bash
ala config edit
```

| 구성 항목 | 설정 방법 |
|---|---|
| 화면 사용자 (`user`) | 바탕화면과 Chrome을 실행할 기존 일반 계정. `root`는 사용할 수 없습니다. |
| DID URL (`url`) | 로그인 없는 실제 HTTP/HTTPS 주소. 아직 없으면 비워 둡니다. |
| 화면 정책 적용 (`display`) | 켜면 화면 꺼짐·잠금·절전 방지와 표시 환경을 준비합니다. |
| GNOME 자동 로그인 (`autologin`) | 화면 정책과 함께 켜면 재부팅 후 지정 계정의 바탕화면 진입을 준비합니다. |
| Chrome 자동 실행 (`autostart`) | DID 주소가 준비되었을 때 켭니다. 꺼도 자동 로그인은 별도로 유지할 수 있습니다. |
| 시간대 (`timezone`) | 기본값 `Asia/Seoul`. |
| root Bash·개발 도구 | 필요한 경우에만 켭니다. 기본값은 꺼짐입니다. |

저장 후 `ala config show`로 확인합니다. **구성 저장은 기대값 저장이며, 서버 설정 적용은 다음 단계입니다.**
DID 주소가 없어도 바탕화면 자동 로그인을 준비할 수 있습니다. Chrome만 주소 입력 대기로 남습니다.

### 4. 초기 OS 준비 — 필요한 새 서버에서만

최초 OS 업데이트를 원하면 드라이버·환경 설정 **전에** 실행합니다. 기존 서버는 이 단계를 생략할 수 있습니다.

```bash
ala prepare --plan
ala prepare --apply
```

재부팅 대기가 나오면 `sudo reboot`를 직접 실행하고, Mac에서 SSH로 다시 접속한 뒤 다음을 실행합니다.

```bash
ala prepare --apply
```

이 재실행은 예상 커널과 부팅 결과를 확인합니다. 초기 준비 완료 후 전체 업데이트를 반복하지 않습니다.
이미 NVIDIA/DKMS 등 외부 모듈이 있으면 호환성 검토가 필요해 차단될 수 있습니다. 업데이트 중 서비스가 재시작될 수 있으며 자동 재부팅은 하지 않습니다.
상세 범위와 실패 복구는 [초기 준비 안내](docs/PREPARATION.md)를 참고하세요.

### 5. 환경 설정 계획 확인·적용

```bash
ala setup --plan
ala setup --apply
ala check
```

| 실행 | 의미 |
|---|---|
| `ala setup` 또는 `ala setup --plan` | 현재 상태와 필요한 변경 조회. 설치·설정을 적용하지 않습니다. |
| `ala setup --apply` | 상태를 다시 검사하고 누락된 설치·다른 설정만 적용한 뒤 검사합니다. |
| `ala check` | 저장된 기대 구성과 현재 상태를 비교합니다. 수정하지 않습니다. |

이미 정상인 패키지·동일 설정은 유지합니다. `setup`은 전체 OS 업데이트를 하지 않으며 운영 중 자동 업데이트 차단을 유지합니다.
기능을 꺼도 기존 패키지를 자동 삭제하거나 과거 설정을 일괄 복원하지는 않습니다.

### 6. 재부팅 후 자동 로그인·화면 확인

드라이버나 화면 설정에 재부팅·재로그인이 필요하다고 나오면 작업 결과를 확인한 뒤 직접 재부팅합니다.

```bash
sudo reboot
```

Mac에서 SSH로 다시 접속한 뒤 실행합니다.

```bash
ala check
ala chrome status
```

모니터에서 암호 입력 없이 바탕화면에 진입하는지, DID 주소가 있으면 Chrome 전체화면이 뜨는지 확인합니다.
`ala check`는 설정·현재 세션·잠금 상태를 검사하지만 실제 화면 내용이나 팝업 부재를 대신 확인하지는 않습니다.
재부팅 후 로그인 화면 또는 잠금화면이 남으면 `ala config show`의 사용자·`display`·`autologin`과 `ala check`의 다음 조치를 확인하세요.
현재 GDM·그래픽 세션을 강제로 재시작하거나 계정 비밀번호를 삭제하지 않습니다.

## TUI로 사용하기

일반 사용자 SSH 터미널에서 `ala`를 실행합니다. 필요한 작업에서만 sudo 인증을 받습니다.

1. **구성**에서 사용자·주소·기능을 저장합니다.
2. **설치·설정**에서 **초기 OS 계획** 또는 **환경 설정 계획**을 선택합니다.
3. 변경 내역을 확인하고 **계획 적용**을 선택합니다.
4. **검사**, **Chrome**, **실행 기록**에서 결과와 로그를 확인합니다.

화살표·Tab으로 이동하고 Enter로 실행, Space로 선택합니다. Esc는 메뉴/취소, `q`는 종료입니다.
터미널은 80×24 이상을 권장합니다. **TUI를 닫거나 SSH 연결이 끊겨도 시작된 설치 작업과 Chrome은 계속 실행됩니다.**

## 자주 쓰는 명령

| 목적 | 명령 |
|---|---|
| 도움말·버전 | `ala --help`, `ala --version` |
| 구성 편집·확인 | `ala config edit`, `ala config show` |
| 서버 요약·구성 검사 | `ala status`, `ala check` |
| Wi-Fi 계층별 진단 | `ala doctor network` |
| Chrome 시작·중지·재시작 | `ala chrome start`, `ala chrome stop`, `ala chrome restart` |
| Chrome 상태·실행 검사·로그 | `ala chrome status`, `ala chrome check`, `ala chrome logs` |
| 설치 기록 목록·상세 | `ala runs list`, `ala runs show RUN_ID` |
| 특정 설치 로그 추적 | `ala logs --run RUN_ID --follow` |

Chrome 제어도 SSH에서 실행합니다. `DISPLAY=:0`을 임의 지정하거나 root로 Chrome을 실행할 필요가 없습니다.
표시 계정의 실제 그래픽 세션이 없으면 자동 로그인 설정과 재부팅 결과부터 확인합니다.

Wi-Fi 진단은 장치·모듈·펌웨어·초기화·무선 차단·자동 연결·IP/라우팅/DNS를 구분하며 연결 설정을 바꾸지 않습니다.
기본 진단은 외부 사이트에 접속하지 않습니다. 명시적 외부 확인은 `ala doctor network --probe-url https://example.com`으로 요청합니다.
요청은 호스트 경로를 사용하므로 유선 인터넷 성공을 Wi-Fi 성공으로 판정하지 않습니다.

## SSH 재접속 후 진행 상황 확인·자동화

설치 시작 후 바로 터미널로 돌아오려면 다음과 같이 실행합니다.

```bash
ala setup --apply --detach --json
ala runs list
ala runs show RUN_ID --json
ala logs --run RUN_ID --follow
```

`RUN_ID`는 적용 시 반환되거나 목록에 표시된 값으로 바꿉니다. `prepare --apply --detach`도 지원합니다.
전원 차단으로 중단된 설치는 자동 재개하지 않습니다. 기록·현재 상태를 확인한 뒤 해당 `prepare` 또는 `setup --apply`를 명시적으로 다시 실행합니다.

비대화형 구성은 [구성 예시](examples/instance.json)의 사용자·주소를 실제 값으로 수정한 후 `ala config import ./instance.json`으로 가져옵니다.
`config show`, `status`, `check`, `doctor network`, 계획 조회와 실행 기록 조회에는 `--json`을 사용할 수 있습니다.
비TTY 환경의 sudo는 미리 준비해야 하며 암호 입력을 무한히 기다리지 않습니다.

## 기본으로 준비하는 것

| 기능 | 적용 내용 |
|---|---|
| 기본 환경 | Git/curl/인증서/패키지 도구/Python/chrony, Asia/Seoul |
| 셸 | Bash·Oh My Bash·ASCII 테마, 기존 개인 설정 보존 |
| SSH | 서버·부팅 자동 시작·현재 포트의 런타임/영구 방화벽 규칙 |
| Docker | Engine·Compose·Buildx·부팅 자동 시작·기존 JSON 보존 |
| 장치 드라이버 | PCI/USB 조사, 확인된 공식 펌웨어·실행 커널 모듈의 누락 보완 |
| GPU | NVIDIA 자동 감지, 정상 드라이버 유지, 지원 드라이버/컨테이너 연결 |
| 운영 정책 | DNF Automatic·PackageKit 차단, GNOME 화면 꺼짐·잠금·절전 방지 |
| DID 화면 | 기존 계정 자동 로그인, 새 표시 프로필, Chrome 부팅·장애 복구 |

root Bash 테마와 Neovim/btop/tmux는 선택 사항입니다. uv·원격 데스크톱·애플리케이션 배포·DB 관리는 포함하지 않습니다.
별도 DID 제어 인스턴스와 10개 패널의 배치는 이 도구의 범위 밖입니다.

## 무인 화면 운영

- 로그인 없는 DID URL을 사용합니다. 주소 미정이면 환경은 준비하지만 화면은 입력 대기로 표시합니다.
- 같은 Linux 계정을 사용하면서 `~/.config/ala/chrome-display`에 새 프로필을 만듭니다.
- 기존 일반 Chrome·chrome-did 프로필은 보존하며 기존 로그인 쿠키를 복사하지 않습니다.
- 표시용 프로세스에만 `--password-store=basic`을 사용해 키링 암호 입력을 피합니다. 키링을 쓰는 저장 방식보다 데이터 보호가 약하므로 이 프로필에 웹 계정·암호·인증정보를 저장하지 않습니다.
- `--no-sandbox`나 전용 키오스크 모드는 사용하지 않습니다.
- 네트워크/페이지가 늦게 준비되면 기동 대기로 남기고 재시도합니다. 열린 페이지의 지속 장애 감시는 별도입니다.
- `stop`은 현재 실행만 중지합니다. 다음 로그인·재부팅 자동 시작을 끄려면 구성에서 `autostart`를 끄고 적용합니다.

OS가 부팅된 뒤부터 자동 복구합니다. BIOS 전원 복구나 부팅 전 디스크 암호 입력까지 설정하지 않습니다.
일반 사용자의 로컬 자동 로그인이 활성화되는 운영 구성입니다.

## 결과 해석

검사 결과는 정상·변경 필요·대기·해당 없음·차단·확인 불가·실패로 나뉩니다.
GPU 없음은 해당 없음, 새 드라이버 재부팅은 대기입니다. 실행 기록은 실제 현재 상태를 대신하지 않습니다.

- `check`/적용 결과: `0` 정상, `1` 불일치·실패·차단·확인 불가, `2` 적용/입력 대기.
- 명령 사용 오류: `64`.
- `setup --plan`은 변경 필요 항목이 있어도 계획 계산에 성공하면 `0`입니다.
- `--detach`의 성공은 작업 접수 성공입니다. 최종 결과는 실행 ID로 조회합니다.

동일 구성은 재설치·재작성하지 않습니다. 실행 중 Docker/SSH/GDM을 강제 재시작하거나 자동 재부팅하지 않습니다.
`ala setup`은 전체 OS 업그레이드를 실행하지 않으며, 패키지 설치가 새 부팅 커널·Rocky release 패키지를 요구하면 중단합니다.
일반 의존성 패키지까지 고정하는 정책은 아닙니다. 자동 OS 업데이트 차단과 관리자 수동 업데이트는 별개입니다.
드라이버 준비는 기존 정상 연결을 유지하며 진행합니다. 재부팅 대기는 완전 완료와 구분하고, 미지원 장치는 장치 ID와 다음 조치를 표시합니다.

## 문서와 개발

- [설치·배포](docs/INSTALL.md)
- [초기 OS 준비·Wi-Fi 진단·자동 로그인](docs/PREPARATION.md)
- [운영·복구·검증](docs/OPERATIONS.md)
- [드라이버와 업데이트 정책](docs/DRIVERS.md)
- [실기기 수용 기록](docs/ACCEPTANCE.md)
- [승인 계획](docs/plans/ala-v1-plan.md) · [진행 상태](docs/plans/ala-v1-progress.md)
- [원본 분리 출처](docs/SOURCE.md)

기존 `scripts/rocky-linux/`는 내부 설치 모듈과 분리 전 참고 문서입니다. 신규 진입점은 `ala`입니다.
