# Rocky Linux 기본 환경과 운영 관리

[설치 안내](../README.md) · [스크립트 상세 안내](REFERENCE.md)

이 문서는 스크립트가 준비하는 기본 환경과 설치 이후 관리 방법을 설명합니다.
대상은 Rocky Linux 10이 설치된 CPU 서버와 GPU 서버 두 대이며, 두 서버 모두 GNOME에서
Chrome 전체화면으로 DID 웹 화면을 표시하면서 CPU/GPU 작업을 수행하는 구성입니다.
OS 자체 설치와 실제 애플리케이션 배치는 별도 단계입니다.
직접 마우스·키보드를 조작하지 않고 SSH에서 설치·실행·종료·상태를 관리합니다.

## 기본 구성

대상 서버에서 사용하는 공통 명령은 다음과 같습니다. 명령은 Rocky 서버에서 실행합니다.
`--user`에는 기존 sudo 권한 사용자를 지정하며, 아래 `$(id -un)`은 현재 로그인한 계정입니다.

```bash
cd ~/rocky-linux
sudo bash ./setup.sh --user "$(id -un)" --with-root --with-ssh --with-display
```

| 항목 | 구성 결과 | 적용 조건 |
|---|---|---|
| 기본 패키지 | Git, curl, CA 인증서, bash-completion, Python 3, 네트워크·패키지 관리 도구, firewalld, chrony | 항상 |
| Chrome | stable 설치·버전 확인, 번역·기본 브라우저·소개 정책, SSH용 `chrome-did` 명령 | 항상 |
| 시간 | Asia/Seoul 시간대, chronyd 실행과 부팅 자동 시작 | 항상 |
| Bash | 사용자 Bash·Oh My Bash·서버용 테마, 기존 `.bashrc`에 로딩 구문 추가 | 항상, root는 `--with-root` |
| OS 업데이트 | DNF Automatic·PackageKit 자동 업데이트 경로 차단 | 항상 |
| 화면 | GNOME 화면 자동 꺼짐·자동 잠금·절전 방지, GNOME Software 업데이트 정책 | `--with-display` |
| SSH | openssh-server와 firewalld 준비, 현재 SSH 포트 허용, 부팅 자동 시작 | `--with-ssh` |
| Docker | Engine·Buildx·Compose, 부팅 자동 시작, 기존 로그 정책이 없을 때 크기 제한 추가 | 항상 |
| NVIDIA | 실제 GPU 감지, 정상 드라이버 유지 또는 지원 드라이버 설치 | NVIDIA GPU가 감지된 경우 |
| 컨테이너 GPU | NVIDIA Container Toolkit과 Docker runtime 연결 | NVIDIA 호스트 드라이버가 정상인 경우 |
| 편의 도구 | Neovim·btop·tmux | `--dev-tools`, 위 공통 명령에는 미포함 |

`--plan`은 실행할 단계와 옵션만 출력합니다. 장비의 패키지·설정 상태를 검사한 결과는 아니며,
실제 설치 명령을 실행할 때 각 단계가 현재 상태를 검사합니다.

## 설치와 설정 전에 확인하는 것

설치 완료 표시 하나로 전체 과정을 건너뛰지 않고 실행할 때마다 필요한 상태를 확인합니다.

| 대상 | 확인 방법 | 이미 준비된 경우 | 준비되지 않은 경우 |
|---|---|---|---|
| 일반 패키지 | `rpm -q`로 설치 여부 확인 | 설치 목록에서 제외 | 누락된 패키지만 DNF에 설치 요청 |
| Chrome | `rpm -q google-chrome-stable`, 실행 파일 확인, `--version` 실행 | 재설치·서명 키 재등록 없이 버전만 확인 | 공식 서명 키와 x86_64 RPM으로 설치 후 실행 파일·버전 확인 |
| Chrome 안내 정책·실행 도구 | 정책 값·읽기 권한·다른 파일과의 충돌, 실행 도구 내용·권한 | 같은 파일은 유지, 다른 파일의 동일 정책도 보존 | 누락 설정·도구 준비, 관리 파일의 다른 키 보존, 충돌은 중단 |
| 설정 파일 | 내용·권한·소유자 비교, Docker JSON은 의미 비교 | 원본 유지, 새 백업 생략 | 원본이 있으면 백업한 뒤 관리하는 설정 적용 |
| 서비스 | 실행 상태와 영구 부팅 자동 시작 여부 | 실행 중인 서비스를 재시작하지 않음 | 필요한 자동 시작 등록·서비스 시작 |
| 업데이트 차단 | 해당 systemd unit의 `masked` 여부 | mask를 다시 적용하지 않음 | mask 적용, 활성 업데이트 타이머는 중지 |
| SSH 방화벽 | 해당 zone의 런타임·영구 허용 규칙 | 같은 규칙을 다시 추가하지 않음 | 누락된 규칙만 추가 |
| GNOME 정책 | 정책 소스와 컴파일된 dconf DB 상태 | 소스·DB가 최신이면 유지 | 소스 적용 또는 DB 재컴파일 |
| NVIDIA | PCI 장치, `nvidia-smi`, 설치 상태와 재부팅 대기 상태 | 정상 드라이버 유지 | 지원·서명 조건 확인 후 설치하거나 원인을 알리고 중단 |

Chrome 패키지가 설치된 것으로 나오더라도 실행 파일이 없거나 버전 명령이 실패하면 중단합니다.
손상된 설치를 정상으로 간주하거나 자동 재설치하지 않습니다.
일반 패키지 설치는 누락분만 요청하지만 의존성 해결에 따라 다른 패키지가 변경될 수 있습니다.

실행 중인 자동 업데이트 트랜잭션이나 예약된 오프라인 업데이트가 있으면 정책 적용을 중단합니다.
기존 PackageKit 데몬은 강제 종료하지 않으므로 유지보수 시간에 재부팅 후 다시 검사해야 할 수 있습니다.
Podman 충돌, 기존 사용자 셸 구성, 손상된 GPU 드라이버처럼 확인이 필요한 상태도 원인을 표시하고 중단합니다.

같은 통합 명령을 다시 실행하면 현재 상태를 검사하여 남은 작업을 진행합니다.
스크립트가 관리하는 설정을 수동 변경했다면 다음 실행에서 지정된 값으로 복구될 수 있습니다.
앞 단계에서 실패하면 이후 단계는 실행하지 않으며, 이전 단계에서 적용한 변경을 자동으로 되돌리지는 않습니다.

## Chrome 표시와 SSH 관리

`setup-base.sh`는 Chrome 설치 여부와 별개로 다음 정책과 실행 명령을 준비합니다.
이미 Chrome이 있는 서버에서도 최신 폴더 전체를 복사한 뒤 `sudo bash ./setup-base.sh`를 다시 실행하면 됩니다.

| 안내 | 제어 방식 | 적용 범위 |
|---|---|---|
| Google Translate 번역 제안 | `TranslateEnabled=false` | 이 서버의 모든 Chrome 사용자 |
| 기본 브라우저 확인 | `DefaultBrowserSettingEnabled=false` | 이 서버의 모든 Chrome 사용자 |
| Chrome 소개·홍보 안내 | `PromotionsEnabled=false` | 이 서버의 모든 Chrome 사용자, Chrome 128 이상 |
| 처음 실행 안내 | `--no-first-run` | `chrome-did start` 실행 |
| 비정상 종료 후 “Restore pages?” | `--hide-crash-restore-bubble` | `chrome-did start` 실행 |
| F11 방식 전체화면 | `--start-fullscreen` | `chrome-did start` 실행 |

정책 파일은 `/etc/opt/chrome/policies/managed/did-display.json`, 실행 명령은
`/usr/local/bin/chrome-did`입니다. 정책은 재부팅 후에도 유지되고 실행 옵션은 도구가 매번 포함합니다.
사이트 자체 팝업, 카메라·마이크 권한, TLS 오류 등의 안내는 이 설정의 대상이 아닙니다.
[Linux 정책 설정](https://support.google.com/chrome/a/answer/9027408?hl=en),
[Chrome 실행 옵션](https://github.com/chromium/chromium/blob/main/chrome/common/chrome_switches.h)

### SSH에서 실행하기

화면에 로그인된 일반 사용자로 SSH에 접속하고 **sudo 없이** 실행합니다. 주소는 실제 DID 주소로 바꿉니다.

```bash
chrome-did start 'https://example.com'
```

| 작업 | 명령 |
|---|---|
| 프로세스·서비스 실행 상태 | `chrome-did status` |
| 현재 부팅의 최근 로그 | `chrome-did logs` |
| 화면 환경·실행 인자 확인 | `chrome-did check` |
| 기대한 시작 주소까지 확인 | `chrome-did check 'https://example.com'` |
| DID Chrome 종료 | `chrome-did stop` |
| URL 변경 또는 설정 적용 후 재실행 | `chrome-did stop` 후 원하는 주소로 `chrome-did start 'https://example.com'` |

같은 `did-chrome.service`가 실행 중이면 다시 시작하지 않습니다.
사용자 서비스에 등록된 GNOME 화면 환경을 사용하므로 SSH 터미널의 `DISPLAY`가 비어 있어도 실행할 수 있습니다.
사용자 서비스에도 화면 환경이 없으면 중단합니다. 값 확인은 SSH에서 다음과 같이 합니다.

```bash
loginctl list-sessions --no-pager
systemctl --user show-environment | grep -E '^(DISPLAY|WAYLAND_DISPLAY|XAUTHORITY|XDG_RUNTIME_DIR)='
```

이 도구는 로그인 세션을 새로 만들거나 GNOME 자동 로그인·부팅 자동 실행을 설정하지 않습니다.
GNOME이 로그인된 상태여야 하며, 해당 계정과 다른 SSH 계정이나 root로 실행하면 화면 환경을 사용할 수 없습니다.
종료 명령은 이 도구가 실행한 DID Chrome만 대상으로 합니다. 기존 일반 Chrome·수동 테스트 창을 일괄 종료하지 않습니다.

DID 프로필은 기본 `~/.config/chrome-did`, `XDG_CONFIG_HOME`을 사용하면 그 아래 `chrome-did`입니다.
일반 Chrome의 로그인 정보와 구분되므로 DID 사이트의 첫 로그인은 별도로 필요합니다.
이후 해당 프로필의 쿠키·로그인 데이터는 유지됩니다. 초기 설정 재실행은 프로필을 초기화하지 않습니다.

### SSH에서 정책 검사하기

스크립트 폴더에서 읽기 전용 검사로 파일·값·소유자·읽기 권한·다른 정책 파일과의 충돌을 확인합니다.

```bash
cd ~/rocky-linux
sudo python3 ./lib/chrome_policy.py --check
```

다른 파일이 같은 값을 이미 제공하면 중복 키를 만들지 않습니다. 다른 값이나 잘못된 JSON이 있으면 원본을
변경하지 않고 중단하므로 표시된 파일을 확인해야 합니다. 이 검사는 로컬 정책 파일을 확인하며,
브라우저가 실제 표시하는 유효 정책과 팝업까지 자동 검증하는 것은 아닙니다.
실행 상태와 오류는 `chrome-did status`, `chrome-did logs`로 확인합니다.
`chrome-did check`는 사용자 서비스의 실제 MainPID와 `/proc` 실행 인자를 읽어 전체화면·안내 차단 옵션,
DID 프로필과 선택한 시작 주소를 비교합니다. 서비스를 시작·종료하거나 설정 파일을 수정하지 않습니다.

Chrome은 `managed/` 안의 파일을 확장자와 관계없이 읽습니다. 정책 변경 백업은 그 밖의
`/var/lib/rocky-setup/chrome-policy-backups/`에 보관하고, 임시 파일도 정책 디렉터리 밖에서 완성해 교체합니다.
백업을 `managed/`에 복사해 두면 과거 정책이 다시 읽힐 수 있습니다.

## Chrome과 OS 업데이트 관리

운영 기준은 **자동 업데이트 경로를 차단하고 점검 시간에 수동 업데이트**하는 것입니다.
특정 Chrome 버전의 영구 고정이나 DNF `versionlock`은 기본으로 적용하지 않습니다.

Linux의 Chrome 본체는 자체 업데이트 대신 시스템 패키지 관리자를 이용합니다.
따라서 이 스크립트가 막는 DNF Automatic·PackageKit 경로의 Chrome RPM 업데이트도 차단됩니다.
Chrome 공식 RPM이 추가하는 Google 저장소는 이후 수동 업데이트에 사용하도록 유지합니다.
[Chromium 업데이트 안내](https://www.chromium.org/administrators/turning-off-auto-updates/),
[Google Linux 저장소 안내](https://www.google.com/linuxrepositories/)

| 실행 또는 경로 | Chrome 본체에 미치는 영향 |
|---|---|
| `setup-base.sh` | 없으면 현재 stable 설치, 설치되어 있으면 유지, 안내 정책·SSH 실행 도구도 준비 |
| 공통 `setup.sh` | 기본 설치·확인 후 자동 업데이트 차단 정책 적용 |
| DNF Automatic·PackageKit | 정책 적용 후 해당 자동 업데이트 경로 차단 |
| `setup-base.sh --upgrade`, `setup.sh --upgrade`, `sudo dnf upgrade` | 관리자의 전체 수동 업데이트이며 Chrome도 업데이트될 수 있음 |
| `sudo dnf upgrade --refresh google-chrome-stable` | 지정한 Chrome 패키지와 필요한 의존성의 수동 업데이트 |

**`setup-base.sh`만 실행하면 업데이트 차단 정책은 적용되지 않습니다.**
기존 서버에서 Chrome만 추가했고 정책 적용을 확인하지 않았다면 다음 명령으로 적용·확인합니다.
`verify.sh`는 Docker 등 공통 환경도 함께 검사하므로 다른 설치 단계가 끝난 서버에서 사용합니다.

```bash
cd ~/rocky-linux
sudo bash ./setup-update-policy.sh --with-display
sudo bash ./verify.sh --display-policy --ssh
```

별도 cron·사내 관리 에이전트·사용자 정의 업데이트 스크립트는 이 정책의 차단 범위 밖입니다.
DNF makecache는 패키지 목록을 갱신하는 작업이므로 유지하며, 이것만으로 패키지가 설치되지는 않습니다.

Chrome 내부 구성요소와 확장 프로그램의 업데이트는 브라우저 본체 패키지 업데이트와 별개입니다.
이 스크립트는 해당 Chrome 정책을 추가하지 않고 기본 동작을 유지합니다.
구성요소 업데이트를 제한하는 정책도 모든 구성요소를 차단하지는 않습니다.
[Google의 Linux Chrome 업데이트 관리 안내](https://support.google.com/chrome/a/answer/9052345?hl=en),
[Chrome 확장 프로그램 업데이트 안내](https://www.google.com/intl/us/chrome/update/)

### 점검 시간에 Chrome 업데이트하기

운영 화면 중단이 가능한 시간에 현재 버전을 기록하고 업데이트할 패키지 목록을 확인합니다.
다음 명령은 승인 목록을 확인할 수 있도록 `-y`를 사용하지 않습니다.

```bash
rpm -q google-chrome-stable
sudo dnf upgrade --refresh google-chrome-stable
google-chrome-stable --version
```

업데이트 후 실행 중이던 Chrome을 완전히 종료하고 다시 열어 DID 웹 화면·영상·음성 동작을 확인합니다.
다른 서버에도 같은 절차를 적용하고 설치 버전을 기록합니다.
운영 중 자동 업데이트를 차단하더라도 보안 패치를 적용할 점검 일정은 별도로 관리합니다.

OS 전체 업데이트가 필요하면 유지보수 시간에 `sudo dnf upgrade --refresh`로 변경 목록을 검토합니다.
커널·NVIDIA 드라이버 호환성과 재부팅 필요 여부를 확인한 뒤 아래 절차로 서비스·실제 작업을 점검합니다.
Chrome만 기존 버전으로 유지하면서 다른 패키지를 업데이트해야 한다면 별도 버전 고정 정책이 필요합니다.
여러 서버에 정확히 같은 버전을 재현해야 할 때도 승인한 패키지 버전과 서명 검증된 설치 파일을 별도로 관리해야 합니다.

## 설정이 저장되는 위치와 재부팅

| 항목 | 주요 저장 위치 또는 등록 대상 | 적용 시점 |
|---|---|---|
| Chrome | RPM 설치 기록, 공식 패키지가 구성한 Google 저장소 | 설치 후 실행 가능, 업데이트 후 브라우저 재시작 |
| Chrome 안내·SSH 실행 | `/etc/opt/chrome/policies/managed/did-display.json`, `/usr/local/bin/chrome-did`, 사용자별 DID 프로필 | 정책 저장 후, 실행 옵션은 `chrome-did start`에 포함 |
| Bash | 대상 계정의 `~/.bashrc`, `~/.config/server-bash/`, `~/.oh-my-bash/` | 새 터미널 또는 재로그인 |
| 시간·SSH·Docker | systemd의 chronyd·sshd·firewalld·docker·containerd 자동 시작 등록 | 서비스 실행 후, 다음 부팅에도 시작 |
| 업데이트 차단 | DNF Automatic·PackageKit unit의 영구 systemd mask | 새 실행 차단, 기존 PackageKit은 다음 재부팅 후 종료 확인 |
| GNOME 화면·업데이트 | `/etc/dconf/profile/user`, `/etc/dconf/db/rocky_setup.d/`와 컴파일 DB | 로그아웃 후 로그인 |
| 시스템 절전 | 절전 target mask, `/etc/systemd/logind.conf.d/60-rocky-kiosk.conf` | target 차단 즉시, logind는 다음 재부팅 |
| Docker 로그·GPU runtime | `/etc/docker/daemon.json` | Docker 재시작 후, 로그 기본값은 새로 생성하는 컨테이너에 적용 |
| NVIDIA 드라이버 | 드라이버 패키지·커널 모듈, 재부팅 대기 기록 `/var/lib/rocky-setup/` | 신규 드라이버 설치 시 재부팅 후 재검사 |

설정과 자동 시작 등록은 디스크에 저장되므로 매 부팅마다 설치 스크립트를 실행할 필요가 없습니다.
설정이 변경될 때 원본이 있으면 같은 디렉터리에 `.파일명.backup.<시각>.<PID>`로 보관합니다.
Chrome 정책 백업은 Chrome이 다시 읽지 않도록 `/var/lib/rocky-setup/chrome-policy-backups/`에 따로 보관합니다.
새로 만든 파일은 원본 백업이 없으며, 복원은 [스크립트 상세 안내](REFERENCE.md)를 확인합니다.

설치 스크립트는 자동 재부팅하거나 실행 중인 Docker를 강제로 재시작하지 않습니다.
NVIDIA 설치 후 `[대기]`가 나오면 점검 시간에 재부팅한 뒤 **같은 통합 설치 명령**을 다시 실행해
드라이버 동작 확인과 Container Toolkit 설정을 이어갑니다.

## 설치 후·재부팅 후 점검

Chrome 설치 상태만 직접 확인할 때는 다음 명령을 사용합니다. 설치·설정을 변경하지 않습니다.

```bash
rpm -q google-chrome-stable
google-chrome-stable --version
```

공통 환경·SSH·화면·업데이트 정책을 점검합니다. 이 명령은 검사만 하며 누락 항목을 설치하지 않습니다.
`--ssh`는 SSH 설정을 포함한 서버, `--gpu`는 GPU 구성을 완료한 서버에서 사용합니다.

```bash
cd ~/rocky-linux
sudo bash ./verify.sh --display-policy --ssh

# GPU 드라이버와 Container Toolkit까지 준비한 서버
sudo bash ./verify.sh --display-policy --ssh --gpu
```

Chrome의 실제 실행 옵션과 기대한 사이트까지 함께 검사하려면 다음 명령을 사용합니다.
이때 화면 사용자는 기본적으로 sudo를 실행한 사용자이며, 필요하면 `--user user`로 지정합니다.

```bash
sudo bash ./verify.sh --display-policy --ssh --chrome-url 'https://example.com'
```

| 검사 | 기대 상태 |
|---|---|
| 서비스 | 실행 중이며 `enabled`로 영구 부팅 자동 시작 등록. `enabled-runtime`은 실패 |
| Chrome 설치 환경 | 패키지·버전 명령, 로컬 정책 값·권한·충돌, 설치된 실행 도구와 현재 소스의 일치 |
| 업데이트 정책 | 선택 시 해당 unit이 masked이며 현재 실행 중이지 않음 |
| GNOME·SSH·GPU | 선택한 플래그에 따라 기존 정책 파일·절전 차단, SSH 문법·서비스, 호스트 GPU·Docker runtime 검사 |
| `--chrome` | 화면 환경과 실제 실행 옵션·DID 프로필 확인. Chrome 미실행이면 대기 |
| `--chrome-url URL` | 위 검사와 시작 주소 비교. Chrome 미실행도 실패 |

실패가 있어도 나머지 검사를 계속하고 정상·실패·대기 개수를 출력합니다.
종료 코드는 `0` 선택한 검사 통과, `1` 실패, `2` 실행 대기입니다. 실패가 있으면 대기보다 우선합니다.
설치 마지막의 자동 검사는 브라우저 실행을 요구하지 않습니다. 표시 단계까지 준비한 뒤 `--chrome` 또는
`--chrome-url`로 실제 실행 인자 검사를 추가합니다. 현재 탭 URL·실제 전체화면 상태·팝업 유무는 검사하지 않습니다.

`[FAIL]`이 있으면 해당 단계를 점검하고 같은 통합 설치 명령을 다시 실행합니다.
패키지·설정 검사가 통과해도 실제 화면과 애플리케이션 동작은 별도 확인해야 합니다.
장비별로 적용한 스크립트 커밋, 실행 옵션, OS·커널·Chrome·GPU 드라이버 버전,
적용 일시, 재부팅 여부와 확인 결과를 기록하면 서버 간 차이를 추적할 수 있습니다.

Docker 서비스의 부팅 자동 시작과 앱 컨테이너의 자동 복구는 별도입니다.
앱 재시작 정책·DB 볼륨과 백업, GNOME 자동 로그인, Chrome URL·전체화면 자동 실행,
전원 복구 후 자동 부팅은 각 애플리케이션과 장비의 운영 구성에서 준비해야 합니다.
외부 DID 제어 장비·디스플레이 패널 배치·영상 입력 설정도 이 스크립트 범위 밖입니다.
