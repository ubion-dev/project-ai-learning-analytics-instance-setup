# Rocky Linux 학습분석실 서버 환경 설정

**처음 설치할 때는 `setup.sh` 한 개만 실행합니다.** 나머지 파일은 통합 실행의 구성 요소이며,
필요한 단계만 점검·재실행할 때도 사용할 수 있습니다. 교육기관 AI 학습분석실에서 무거운 CPU/GPU 작업과
Chrome 전체화면 DID 웹 표시를 함께 수행하는 두 서버의 공통 OS 설정입니다.
현재 운영 구성에는 Rocky Linux 10과 GNOME이 필요하며, 두 서버 모두 `--with-display`를 사용합니다.
단일 앱만 허용하거나 사용자 입력을 제한하는 전용 키오스크 모드를 구성하지 않습니다.

| 문서 | 확인할 내용 |
|---|---|
| 이 README | 복사·설치 명령, 실행 순서와 옵션 |
| [기본 환경과 운영 관리](docs/ENVIRONMENT.md) | 설치 여부 검사, 기본 구성, Chrome·OS 업데이트 관리, 재부팅 후 확인 |
| [스크립트 상세 안내](docs/REFERENCE.md) | 개별 스크립트의 옵션·변경 파일·충돌 해결·복원 방법 |

### 배치 대상 먼저 확인

관리 대상은 CPU 서버와 GPU 서버로 구분하는 **물리 서버 두 대**입니다.
두 서버는 CPU/GPU 자원이 필요한 무거운 작업을 수행하면서 Chrome에서 DID 웹 화면을 전체화면으로 표시합니다.
영상 감지·집중도 분석·Whisper 추론 등 실제 작업의 배치는 애플리케이션 구성에서 결정합니다.
NVIDIA 설정 여부는 장비의 실제 GPU 모델로 판단합니다.

**DID를 제어하는 인스턴스는 별도 담당자의 관리 범위**입니다.
10개 패널의 영상 배치·입력 연결·DID 제어 장비 설정은 이 스크립트의 대상이 아닙니다.
서버에서 DID 웹 화면을 띄우는 역할과 별도 장비에서 패널을 제어하는 역할을 구분합니다.

두 서버 모두 같은 통합 스크립트를 사용합니다. `--with-display`는 **실행하는 서버 자체의
GNOME 화면 꺼짐·잠금·절전을 방지**하여 Chrome 웹 화면을 유지합니다.
Chrome은 `setup-base.sh`에서 기본 설치합니다. 이미 설치되어 있으면 유지하고 버전만 확인합니다.
번역·기본 브라우저·소개 안내 정책과 SSH용 `chrome-did` 명령도 기본 단계에서 준비합니다.
표시할 URL은 `chrome-did start`에서 지정하며, 로그인·부팅 후 자동 실행은 별도 구성입니다.
이 문서는 직접 마우스·키보드를 조작하지 않고 **SSH에서 서버를 관리**하는 기준입니다.


## 1. Mac에서 서버로 복사

Mac의 **프로젝트 루트**에서 실행하세요. `user`와 `서버IP`를 실제 값으로 바꿉니다.
서버에는 SSH 접속과 sudo 권한이 이미 있어야 합니다.

```bash
TARGET_USER='user'
SERVER_HOST='서버IP'
scp -r scripts/rocky-linux "${TARGET_USER}@${SERVER_HOST}:~/"
ssh "${TARGET_USER}@${SERVER_HOST}"
```

스크립트 폴더만 복사하므로 서버의 GitHub 로그인은 필요 없습니다.
프로젝트의 `.git` 이력·사내 자료·인증 키를 서버에 전달하지 않습니다.
SSH 접속이 준비되어 있지 않다면 장비 관리 담당자를 통해 먼저 접속 경로를 준비해야 합니다.
패키지와 Oh My Bash 다운로드를 위한 외부 네트워크 연결은 필요합니다.

## 2. Rocky에서 계획 확인 후 설치

**아래부터 모든 명령은 Rocky의 `~/rocky-linux` 안에서 실행합니다.**
`$(id -un)`은 SSH로 로그인한 현재 사용자 이름입니다.

```bash
cd ~/rocky-linux

# 설치 없이 실행 순서와 선택 항목만 확인
bash ./setup.sh --user "$(id -un)" --with-root --with-ssh --with-display --plan

# 확인한 구성으로 설치
sudo bash ./setup.sh --user "$(id -un)" --with-root --with-ssh --with-display
```

`--plan`은 설치 없이 실행 목록만 보여 주는 선택 단계입니다.
실제 설치 명령은 매번 현재 상태를 검사해 누락된 패키지와 다른 설정만 적용합니다.
Chrome 설치 여부와 업데이트 차단 설정도 검사 대상입니다.
검사 기준과 재실행 결과는 [기본 환경과 운영 관리](docs/ENVIRONMENT.md#설치와-설정-전에-확인하는-것)를 참고하세요.

Docker 단계에서 Podman/runc가 감지되어 중단되면 기존 컨테이너 사용 여부를 먼저 확인해야 합니다.
스크립트가 패키지를 자동 삭제하지는 않습니다. [충돌 확인과 재실행 방법](docs/REFERENCE.md#docker)을 참고하세요.

서버에 저장소 전체가 이미 있다면 `cd ~/rocky-linux` 대신
`cd ~/legacy-project/scripts/rocky-linux`로 이동하세요.
이후 명령은 동일합니다. 위 예시는 작업 실행과 Chrome 표시를 병행하는 두 서버의 공통 구성입니다.
향후 화면 표시가 없는 서버에 적용할 때는 `--with-display`를 생략할 수 있습니다.
모든 스크립트를 따로 실행할 필요는 없습니다.

## 무엇을 실행하는가

| 구분 | 파일 | 역할 | 통합 실행 |
|---|---|---|---|
| 시작점 | `setup.sh` | 아래 단계를 순서대로 실행, 실패하면 중단 | 직접 실행 |
| 기본 | `setup-base.sh` | 필수 패키지·Chrome·안내 정책·SSH 실행 도구·시간 동기화 | 항상 |
| 화면 유지 | `setup-display.sh` | 이 PC의 화면 자동 꺼짐·잠금·시스템 절전 방지 | `--with-display` |
| 운영 정책 | `setup-update-policy.sh` | 자동 OS 업데이트 차단, 선택 GNOME 정책 | 항상, GNOME은 `--with-display` |
| 셸 | `setup-bash.sh` | Bash·Oh My Bash·사용자/root 테마 | 항상, root는 옵션 |
| 원격 관리 | `setup-ssh.sh` | SSH 설치·자동 시작·방화벽 허용 | `--with-ssh` |
| 관리 편의 | `setup-dev-tools.sh` | Neovim·btop·tmux 설치 | `--dev-tools` |
| 컨테이너 | `setup-docker.sh` | Docker·Compose·자동 시작·로그 제한 | 항상 |
| GPU 드라이버 | `setup-nvidia.sh` | NVIDIA 감지·정상 드라이버 유지·지원 드라이버 자동 설치 | 항상 감지, 없으면 건너뜀 |
| GPU 연결 | `setup-nvidia-container.sh` | Docker에서 NVIDIA GPU 사용 | 감지된 GPU의 드라이버가 준비되면 자동 |
| 검사 | `verify.sh` | 서비스·화면/업데이트 정책·선택 SSH/GPU 검사 | 설치 마지막 |
| 내부 공통 | `common.sh` | 권한 검사·백업·저장소·공통 함수 | 직접 실행 안 함 |
| 내부 GPU 검사 | `lib/nvidia_gpu.py` | PCI 장치·공식 지원 표 확인 | 직접 실행 안 함 |
| 내부 Chrome 정책 | `lib/chrome_policy.py` | 정책 적용·권한·충돌 검사, 별도 경로에 백업 | 기본 단계·검사에서 사용 |
| 설치 후 사용 | `bin/chrome-did` | SSH에서 DID Chrome 실행·종료·상태·로그 확인 | 기본 단계에서 `/usr/local/bin/chrome-did`로 설치 |

기본 순서는 **기본 환경 → 업데이트 정책 → Bash → Docker → NVIDIA 자동 감지 → 검사**입니다.
기본 환경 단계에 Chrome 설치가 포함되어 있어 별도 옵션을 추가할 필요가 없습니다.
`--with-display`는 기본 환경 뒤에 화면 유지 단계를 추가하고 GNOME 업데이트 정책도 적용합니다.
SSH·관리 도구를 선택하면 Bash와 Docker 사이에 해당 단계가 추가됩니다.
실제 단계 수와 파일명은 `--plan`과 설치 로그에 표시됩니다.

### 통합 실행 옵션

| 옵션 | 의미 | 기본값 |
|---|---|---|
| `--user 이름` | Bash 설정 대상 계정 | sudo 실행 사용자, 명시 권장 |
| `--with-root` | root에도 Bash 테마 적용 | 미적용 |
| `--with-ssh` | SSH·방화벽 설정 포함 | 미포함 |
| `--with-display` | 이 서버 자체의 GNOME 화면 유지·업데이트 정책 포함 | 미포함 |
| `--ssh-zone 이름` | SSH를 허용할 방화벽 zone, `--with-ssh` 필요 | 인터페이스 기준 추정 |
| `--dev-tools` | Neovim·btop·tmux 포함 | 미포함 |
| `--upgrade` | 이번 실행에서 전체 OS 패키지 업데이트 | 하지 않음 |
| `--plan` | 실행 계획만 출력, 패키지·설정 변경 없음 | 설치 실행 |
| `--help` | 사용법 표시 | — |

Git은 Oh My Bash에 필요해 유지합니다. uv·원격 데스크톱·앱 배포는 포함하지 않습니다.
`--upgrade`는 관리자가 요청하는 **수동 업데이트**이며, 자동 업데이트 차단 정책과 별개입니다.

이미 다른 설정을 마친 서버에서 기본 패키지와 Chrome만 추가·확인하려면 다음 명령을 실행합니다.

```bash
sudo bash ./setup-base.sh
```

Chrome이 없으면 Google 공식 x86_64 RPM과 서명 키로 설치합니다. 설치된 Chrome은 재설치하거나
업데이트하지 않으며 브라우저를 자동 실행하지 않습니다. 전체 업데이트를 명시한 `--upgrade`는 예외입니다.
Chrome이 이미 설치되어 있어도 안내 정책과 실행 도구는 매번 확인하고 누락되거나 달라진 부분만 반영합니다.
`setup-base.sh` 단독 실행에는 자동 업데이트 차단 단계가 포함되지 않습니다.
차단 정책 적용과 Chrome 수동 업데이트 방법은 [업데이트 관리](docs/ENVIRONMENT.md#chrome과-os-업데이트-관리)에 정리했습니다.

### SSH에서 DID Chrome 실행과 관리

Rocky의 SSH 터미널에서 **GNOME 화면에 로그인된 계정으로, sudo 없이** 실행합니다.
아래 주소를 실제 DID 사이트 주소로 바꾸세요.

```bash
chrome-did start 'https://example.com'
```

F11 방식의 전체화면과 처음 실행·기본 브라우저·탭 복원 안내 차단 옵션을 포함합니다.
기기 정책은 번역 제안·기본 브라우저 확인·Chrome 소개 안내를 비활성화합니다.

| 작업 | SSH 명령 |
|---|---|
| 실행 상태 확인 | `chrome-did status` |
| 최근 오류·로그 확인 | `chrome-did logs` |
| 실행 옵션 검사 | `chrome-did check` |
| 기대한 시작 사이트도 비교 | `chrome-did check 'https://example.com'` |
| DID Chrome 종료 | `chrome-did stop` |
| 표시할 주소 변경 | `chrome-did stop` 후 새 주소로 `chrome-did start 'https://example.com'` |

이미 실행 중이면 유지합니다. 전용 프로필 `~/.config/chrome-did`를 사용하므로 사이트 로그인은 처음에
별도로 필요하고, 이후 로그인 데이터는 이 프로필에 유지됩니다. 일반 Chrome과 기존 테스트 창은 별도로 동작합니다.
이 명령은 로그인된 GNOME의 사용자 서비스 환경을 사용하며 `DISPLAY=:0` 같은 값을 가정하지 않습니다.
GNOME 로그인 세션이 없는 상태에서 바탕화면을 만들거나 부팅 자동 실행을 등록하지는 않습니다.
확인 방법·정책 범위는 [Chrome 표시와 SSH 관리](docs/ENVIRONMENT.md#chrome-표시와-ssh-관리)를 참고하세요.

## 3. NVIDIA 자동 준비와 적용 대기

**GPU 설치 옵션을 별도로 고르지 않아도 됩니다.** 위 공통 설치 명령이 자동으로 판단합니다.

- NVIDIA GPU 없음: 관련 패키지를 설치하지 않고 건너뜀
- `nvidia-smi` 정상: 기존 드라이버 유지 후 컨테이너 GPU 설정 검사
- 드라이버 없음: 공식 open 모듈 지원 목록과 Secure Boot 상태 확인 후 지원 드라이버 설치
- 새 드라이버 설치 후: `[대기]` 안내. 유지보수 시간에 재부팅 후 **동일한 공통 설치 명령**을 다시 실행

같은 부팅에서 재실행해도 설치한 드라이버를 반복 설치하지 않습니다.
재부팅 후에는 드라이버 동작을 다시 확인하고 Container Toolkit 연결을 이어서 준비합니다.

실행 중인 Docker는 자동 재시작하지 않습니다. GPU runtime 적용 대기가 나오면
컨테이너 영향을 확인한 유지보수 시간에 Docker를 재시작하거나 재부팅한 뒤 검사하세요.

```bash
sudo bash ./verify.sh --display-policy --ssh --gpu
```

자동 설치 대상은 x86_64와 open 모듈 지원 GPU입니다. Secure Boot 서명/MOK 등록,
레거시·미확인 GPU, 기존 고장 드라이버는 원인을 표시하고 중단합니다.
[GPU 상세 조건](docs/REFERENCE.md#nvidia-gpu-드라이버)을 참고하세요.

## 4. 적용 시점과 재부팅 후 확인

| 설정 | 적용 시점 | 재부팅 후 유지 |
|---|---|---|
| Bash 테마 | 새 터미널/재로그인 | 유지 |
| GNOME 화면·업데이트 정책 (`--with-display`) | 로그아웃 후 로그인 | 유지 |
| 시스템 절전 차단 (`--with-display`) | target mask 즉시, logind 설정은 다음 부팅 | 유지 |
| SSH·Docker·시간 동기화 | 해당 단계 성공 시 서비스 실행 | 자동 시작 |
| Docker 설정 변경 | Docker 재시작 후 | 유지 |
| 커널·NVIDIA 드라이버 | 필요 시 OS 재부팅 | 정상 설치 후 유지 |

스크립트는 자동 재부팅하지 않습니다. 설정 파일이 저장돼 있으므로 매 부팅마다 다시 설치할 필요도 없습니다.
**Docker 자동 시작만으로 앱이 자동 복구되지는 않습니다.** 컨테이너 재시작 정책과 데이터 영속화는 앱별 구성입니다.
서버 GUI 계정의 자동 로그인·앱 자동 실행·BIOS 전원 복구·DB 백업은 별도 작업입니다.

```bash
# SSH와 Chrome 화면 유지 정책을 포함한 위 공통 구성 기준
sudo bash ./verify.sh --display-policy --ssh

# 향후 화면 표시 없이 구성한 서버의 검사
sudo bash ./verify.sh --update-policy --ssh
```

`verify.sh`는 설정·서비스 상태 검사이며 화면 실동작, 외부 SSH 접속, GPU 컨테이너 실행과
강제 전원 차단 후 앱 복구까지 자동 검증하지는 않습니다.
실행 중인 Chrome의 전체화면·팝업 차단 옵션과 기대한 시작 사이트까지 한 번에 검사하려면 다음 명령을 사용합니다.
아래 주소는 실제 DID 주소로 바꾸세요. 화면 사용자는 기본적으로 sudo를 실행한 계정입니다.

```bash
sudo bash ./verify.sh --display-policy --ssh --chrome-url 'https://example.com'
```

`--chrome`은 주소 비교 없이 실행 옵션을 검사하고, 다른 화면 계정은 `--user user`로 지정합니다.
검사는 설정·서비스를 변경하지 않고 실패한 항목 뒤에도 계속 진행합니다.
마지막에 **정상·실패·대기 개수**를 출력합니다. 종료 코드 `0`은 선택한 검사 통과, `1`은 실패,
`2`는 실행 대기입니다. `--chrome`에서 Chrome이 꺼져 있으면 대기이며, 주소까지 지정했다면 실패입니다.
실행 인자 검사는 시작 옵션과 주소를 비교합니다. 현재 탭의 이동 결과나 실제 창의 전체화면·팝업 상태는 별도입니다.

현재 화면 유지는 로그인한 GNOME 세션에 적용되며, 재부팅 뒤 로그인과 Chrome 전체화면 표시의
자동 복구까지 구성된 것은 아닙니다.

## 필요한 부분만 다시 설정하기

같은 통합 설치 명령을 다시 실행할 수 있습니다. 패키지는 설치 여부, 설정 파일은 내용·권한,
서비스는 자동 시작·실행 상태, 방화벽은 포트 규칙을 먼저 검사합니다.
설정이 같으면 원본 파일과 백업을 다시 만들지 않습니다. Docker JSON은 들여쓰기·키 순서 차이도 무시합니다.
스크립트가 관리하는 값을 수동 변경했다면 재실행 시 백업 후 지정된 값으로 복구합니다.

개별 스크립트는 중단된 단계 재실행이나 해당 설정만 바꿀 때 사용합니다.
전체 실행은 실패 시 이전 단계의 변경까지 자동 복원하지 않습니다.
이미 화면 정책을 적용했다면 `--with-display`를 생략해 재실행해도 그 정책을 제거하지 않습니다.

```bash
sudo bash ./setup-display.sh
sudo bash ./setup-update-policy.sh --with-display
sudo bash ./setup-ssh.sh --ssh-zone public
```

옵션·백업·복원·기존 환경 충돌·운영 제한은 [상세 안내](docs/REFERENCE.md)를 보세요.
이미 설치된 도구를 자동 제거하지 않으며, 실행 중인 서비스를 강제 재시작하는 옵션은 명시해야 합니다.

## 로컬 검증

이 폴더에서 실행합니다. 실제 패키지 설치/서비스 조작 없이 문법과 모의 실행을 검사합니다.

```bash
python3 -m unittest discover -s tests -v
```

실제 Rocky Linux 10의 설치·GUI·GPU·재부팅 복구 검증은 아직 수행하지 않았습니다.
