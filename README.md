# ala — AI 학습 분석실 DID 인스턴스 환경 관리

SSH 터미널에서 Rocky Linux 서버의 설치·설정·검증·Chrome 화면을 관리합니다.
CPU/GPU 작업 환경을 준비하면서 GNOME에서 DID 웹 페이지를 F11 방식 전체화면으로 표시합니다.

> 현재 코드는 `0.1.0rc2` 배포 후보입니다. 자동 테스트와 실제 장비 수용은 별개입니다.
> CPU/GPU 장비의 재부팅·실제 화면 수용이 끝나기 전에는 검증된 정식 운영 버전으로 취급하지 마세요.

## 시작하기

지원: **Rocky Linux 10 x86_64 + GNOME/GDM + 기존 sudo 사용자**.
물리 서버 각각에 설치하며 Mac에서는 SSH로 조작합니다. OS 설치와 파티션 설정은 먼저 완료해야 합니다.

1. Mac에서 배포 묶음을 받아 서버로 복사합니다. 서버에 GitHub 로그인이나 저장소 전체 복제가 필요하지 않습니다.
2. 서버에서 묶음의 `install.sh`로 ala를 설치합니다. 설치 명령과 개발 빌드는 [설치 안내](docs/INSTALL.md)를 따르세요.
3. 일반 사용자 SSH 터미널에서 다음을 실행합니다.

```bash
ala
```

처음에는 **구성**에서 기존 화면 사용자와 DID 주소를 저장하고, **설치·설정**에서 변경 계획을 확인한 뒤 적용합니다.
필요할 때 sudo 인증을 진행합니다. 설정 저장만으로 OS 설정이 적용되지는 않습니다.

## 명령으로 사용하기

```bash
ala config edit
ala config show --json
ala setup --plan
ala setup --apply
ala check
ala status --json
ala chrome start
ala chrome status
ala chrome check
ala chrome logs
ala chrome stop
ala chrome restart
ala runs list
```

비대화형 구성은 [구성 예시](examples/instance.json)를 수정한 후 가져옵니다.

```bash
ala config import ./instance.json
ala setup --apply --detach --json
ala runs show RUN_ID --json
ala logs --run RUN_ID --follow
```

`RUN_ID`는 적용 시 반환된 값으로 바꿉니다. `setup`의 기본 동작은 **계획 조회**입니다.
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
- [운영·복구·검증](docs/OPERATIONS.md)
- [드라이버와 업데이트 정책](docs/DRIVERS.md)
- [실기기 수용 기록](docs/ACCEPTANCE.md)
- [승인 계획](docs/plans/ala-v1-plan.md) · [진행 상태](docs/plans/ala-v1-progress.md)
- [원본 분리 출처](docs/SOURCE.md)

기존 `scripts/rocky-linux/`는 내부 설치 모듈과 분리 전 참고 문서입니다. 신규 진입점은 `ala`입니다.
