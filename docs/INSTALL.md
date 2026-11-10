# 설치·배포·갱신

## Rocky Linux 설치

UEFI를 지원하는 x86_64 장비의 **전용 디스크 신규 설치** 기준입니다. 포맷 전에 필요한
데이터를 백업하고, 설치할 디스크의 모델·용량을 확인하세요.

| 설치 화면 | 권장값 |
|---|---|
| 설치 미디어 | Rocky Linux 10 x86_64 **DVD ISO**의 SHA-256을 공식 체크섬과 비교하고 USB에 기록합니다. 펌웨어 메뉴에서 **UEFI USB**로 부팅한 뒤 미디어 검사 후 설치를 선택합니다. |
| 설치 원천 | DVD ISO는 **자동 감지 설치 미디어**를 선택합니다. Boot ISO를 사용하면 접근 가능한 네트워크 저장소를 지정합니다. |
| 설치 목적지·부트로더 | 새 OS 디스크는 **GPT**를 사용합니다. **전체 디스크 요약 및 부트로더**에서 Rocky를 설치할 디스크를 부팅 장치로 지정합니다. GRUB2의 EFI 파일은 해당 디스크의 `/boot/efi`에 설치됩니다. 첫 부팅 전에 USB를 빼고 Rocky의 UEFI 항목을 선택합니다. |
| 소프트웨어 | DID 장비는 **서버 GUI 사용(Server with GUI)**으로 GNOME/GDM을 준비합니다. |
| 사용자·root | 암호가 있는 일반 SSH 관리 계정을 만들고 **관리자로 지정**을 체크합니다. root 계정은 잠근 상태로 두고, DID 자동 로그인에는 별도의 일반 계정을 사용합니다. |
| 네트워크·시간대 | 설치 중 네트워크를 연결하고 시간대는 `Asia/Seoul`로 선택합니다. |

Secure Boot는 현장 정책에 맞추고, NVIDIA 장비는 서명 조건을 확인하세요. [Rocky 공식 ISO와 체크섬](https://dl.rockylinux.org/pub/rocky/10/isos/x86_64/) · [Anaconda 부트로더·파티션](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html-single/interactively_installing_rhel_from_installation_media/index) · [NVIDIA 드라이버 조건](DRIVERS.md)

### 설치 화면에서 부트로더와 파티션 지정

1. 펌웨어 부팅 메뉴에서 **UEFI USB**로 부팅합니다. **설치 요약 → 설치 대상(Installation Destination)**에서 Rocky를 설치할 디스크만 선택합니다.
2. **전체 디스크 요약 및 부트로더(Full disk summary and bootloader)**를 열고, 같은 디스크가 부팅 장치로 지정되어 있는지 확인합니다. 필요하면 디스크를 선택하고 **부팅 장치로 설정(Set as Boot Device)**을 누릅니다.
3. **스토리지 구성(Storage Configuration) → 사용자 정의(Custom) → 완료(Done)**를 누르면 **수동 파티션(Manual Partitioning)** 화면이 열립니다.
4. 새 마운트 지점의 구성표를 **LVM**으로 고릅니다. **+**를 눌러 아래 표의 마운트 지점과 용량을 한 항목씩 입력합니다. 각 항목을 추가한 뒤 오른쪽에서 장치 유형과 파일 시스템을 표에 맞춥니다. 변경한 항목은 **설정 업데이트(Update Settings)**를 눌러 반영합니다.
5. **완료(Done)**를 누르고 변경 사항 요약을 확인합니다. 포맷 대상이 설치용 디스크뿐이고, `/boot/efi`와 `/boot`가 모두 그 디스크에 있는지 확인한 뒤 **변경 사항 적용(Accept Changes)**을 누릅니다.

### 1TB·2TB 수동 파티션 예시

RAM 32GB, 최대 절전을 사용하지 않는 장비 기준입니다. 신규 마운트 지점의 구성표는
**LVM**으로 두고, `/boot/efi`와 `/boot`는 **표준 파티션**으로 지정합니다.

| 마운트 지점 | 장치 유형 | 파일 시스템 | 1TB 디스크 | 2TB 디스크 |
|---|---|---|---:|---:|
| `/boot/efi` | 표준 파티션 | EFI System Partition (FAT32) | 600 MiB | 600 MiB |
| `/boot` | 표준 파티션 | XFS | 2 GiB | 2 GiB |
| `/` | LVM | XFS | 150 GiB | 150 GiB |
| `swap` | LVM | swap | 8 GiB | 8 GiB |
| `/var` | LVM | XFS | 650 GiB | 1,500 GiB |

`/var`는 기본 경로를 쓰는 Docker 이미지·볼륨과 로그에 공간을 배정한 예시입니다.
디스크 전체를 볼륨에 할당하지 않고 여유를 남깁니다. 제품 표기의 1TB와 2TB는 각각
약 931GiB, 1,863GiB로 표시될 수 있습니다.

### 설치 화면에서 root 잠금과 관리자 계정 생성

1. **설치 요약 → 사용자 설정 → 루트 계정(Root Account)**에서 **루트 계정 비활성화(Disable root account)**를 선택하고 **완료(Done)**를 누릅니다.
2. **사용자 생성(User Creation)**에서 **전체 이름(Full name)**에는 표시할 이름을 입력합니다. **사용자 이름(User name)**에는 SSH 로그인에 쓸 이름을 입력합니다. 예를 들어 `ala-admin`처럼 영문 소문자로 시작하고 공백 없이 작성합니다.
3. **이 사용자를 관리자로 지정(Make this user administrator)**과 **이 계정을 사용하려면 암호 필요(Require a password to use this account)**를 체크합니다. 이 장비에 쓸 길고 고유한 비밀번호를 입력하고 확인란에 다시 입력한 뒤 **완료(Done)**를 누릅니다.
4. 설치 요약에서 관리자 계정이 생성되었는지 확인한 뒤 **설치 시작(Begin Installation)**을 누릅니다. 첫 부팅과 SSH 접속에 이 계정을 사용하고, 관리 작업은 `sudo`로 실행합니다. DID 자동 로그인에 쓸 별도의 일반 계정은 설치 후 만들고 `ala config edit`에서 화면 사용자로 지정합니다.

[Anaconda 사용자 계정 설정](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/interactively_installing_rhel_over_the_network/customizing-the-system-in-the-installer#configuring-the-root-account-and-creating-users)

## 최초 SSH 준비

Rocky의 로컬 콘솔에서 관리 계정으로 로그인합니다. 패키지가 없다면
`sudo dnf install openssh-server firewalld`로 준비합니다.

```bash
sudo systemctl enable --now sshd firewalld
ip -br -4 address
sudo firewall-cmd --get-active-zones
```

`systemctl enable --now sshd`는 SSH 서버를 즉시 시작하고 다음 부팅에도 자동 실행되도록
등록합니다. 위 명령은 Rocky 로컬 터미널에서 실행합니다.

기본 SSH 포트 22를 사용하는 경우, Mac에서 접속할 네트워크 인터페이스의
firewalld 영역에 `ssh` 서비스를 허용합니다. 아래 `public`은 예시입니다. 다른 영역이
표시되면 실제 이름으로 바꾸세요. 별도 할당이 없으면 `sudo firewall-cmd --get-default-zone`으로 확인합니다.

```bash
zone='public'
sudo firewall-cmd --zone="$zone" --add-service=ssh
sudo firewall-cmd --zone="$zone" --permanent --add-service=ssh
```

Mac을 Rocky와 같은 네트워크 또는 Rocky로 라우팅되는 네트워크에 연결하고
`ssh user@서버IP`로 접속합니다. 첫 접속 또는 OS 재설치 후에는 Rocky 콘솔에서
`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`로 확인한 지문과 대조합니다. [firewalld 영역 관리](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/configuring_firewalls_and_packet_filters/using-and-configuring-firewalld)

### 재설치 후 SSH 호스트 키 변경 경고

OS 재설치로 서버의 호스트 키가 바뀌면 Mac에 저장된 이전 키와 달라
`REMOTE HOST IDENTIFICATION HAS CHANGED!`가 표시될 수 있습니다.

1. **Rocky 로컬 터미널**에서 아래 명령으로 새 키의 지문을 확인합니다.

   ```bash
   sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
   ```

2. 출력의 `SHA256:...`가 Mac 경고에 나온 ED25519 지문과 같은지 대조합니다.
   일치하고 재설치한 대상 장비임을 확인했으면 **Mac 터미널**에서 아래 명령을 실행합니다.
   `서버IP`와 `user`는 실제 접속 주소와 관리자 계정명으로 바꿉니다.

   ```bash
   ssh-keygen -R 서버IP
   ssh user@서버IP
   ```

3. 재접속 시 표시되는 지문도 대조한 뒤 `yes`를 입력해 새 키를 저장합니다.
   지문이 다르면 접속 대상 주소와 장비를 다시 확인합니다.

### SSH 로그인 뒤 비밀번호 입력 생략

관리자로 지정하면 sudo 권한이 생깁니다. 비밀번호 입력을 생략해야 하고 현장 정책이 허용할
때만 SSH 관리자 계정에 `NOPASSWD`를 설정합니다. 이 규칙은 해당 계정의 모든 sudo
명령에 적용됩니다. DID 자동 로그인 계정은 대상에서 제외합니다.

1. Rocky에 일반 SSH 관리자 계정으로 접속합니다. **터미널**에서 아래 명령을 실행하면
   현재 로그인한 계정의 이름으로 설정 줄이 출력됩니다. 출력된 한 줄을 복사합니다.

   ```bash
   printf '%s ALL=(ALL:ALL) NOPASSWD: ALL\n' "$(id -un)"
   ```

2. **터미널**에서 아래 명령으로 편집기를 엽니다. sudo가 암호를 요청하면 로그인한 계정의 암호를 입력합니다.

   ```bash
   sudo visudo -f /etc/sudoers.d/90-ala-admin
   ```

   복사한 설정 줄은 **열린 편집기 안에** 붙여 넣습니다. vi에서는 `i`를 누른 뒤 붙여 넣고
   `Esc`, `:wq`, `Enter` 순서로 저장합니다.

3. **터미널로 돌아와** `sudo -k && sudo -n true`를 실행합니다. 암호 입력이나 오류 없이
   끝나면 설정이 적용된 것입니다.

첫 부팅부터 적용하려면 OS 설치용
Kickstart의 `%post`에서 같은 정책을 준비합니다. [Kickstart 설치 후 설정](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/automatically_installing_rhel/kickstart-script-file-format-reference)

## 설치 전제와 파일 검사

Rocky에서 직접 받는 명령은 [README의 ala 설치](../README.md#4-ala-설치)를 따릅니다.
공개 릴리스는 GitHub 계정이나 토큰 없이 받을 수 있습니다.

Rocky Linux 10 x86_64, GNOME/GDM, 기존 일반 sudo 사용자를 지원합니다.
시스템 Python 3.12 이상을 사용합니다. 설치기는 Python/pip이 없으면 DNF로 전제 패키지를 준비하므로 저장소 접속이 필요할 수 있습니다.
Textual과 고정 의존성은 배포 묶음의 wheel로 전용 venv에 설치하며 시스템 Python 패키지를 덮어쓰지 않습니다.
환경 설정 단계에는 Google·Docker·NVIDIA 등 필요한 패키지 저장소 접속이 필요합니다.

`ala-0.1.0rc35.tar.gz`와 `.tar.gz.sha256`은 같은 릴리스에서 받습니다.
수동 설치는 GitHub HTTPS 경로에서 받은 파일을 SHA-256과 설치 manifest로 확인합니다.
자동 운영 준비와 자동 CLI 갱신은 Sigstore 서명도 검증합니다. [업데이트 절차](UPDATES.md)
검사가 실패하면 설치하지 않습니다. GitHub의 Source code 압축은 wheel과 manifest가 없는 개발 소스이므로 설치용 배포 파일과 다릅니다.

## Mac에서 받아 SCP로 전달

Rocky가 GitHub에 직접 접속하지 못하는 경우 사용합니다. Mac 터미널에서 실행합니다.
`user`와 `서버IP`는 실제 SSH 대상에 맞게 바꿉니다.

```bash
(
  set -e
  VERSION='0.1.0rc35'
  RELEASE='v0.1.0-rc.35'
  BASE_URL="https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/releases/download/${RELEASE}"
  mkdir -p "$HOME/Downloads/ala-$VERSION"
  cd "$HOME/Downloads/ala-$VERSION"
  curl -fL --proto '=https' --retry 3 \
    "$BASE_URL/ala-$VERSION.tar.gz" -o "ala-$VERSION.tar.gz"
  curl -fL --proto '=https' --retry 3 \
    "$BASE_URL/ala-$VERSION.tar.gz.sha256" -o "ala-$VERSION.tar.gz.sha256"
  shasum -a 256 -c "ala-$VERSION.tar.gz.sha256"
  scp "ala-$VERSION.tar.gz" "ala-$VERSION.tar.gz.sha256" user@서버IP:~/
)
ssh user@서버IP
```

접속한 Rocky에서 실행합니다.

```bash
(
  set -e
  cd ~
  sha256sum -c ala-0.1.0rc35.tar.gz.sha256
  tar -xzf ala-0.1.0rc35.tar.gz
  cd ala-0.1.0rc35
  sudo bash ./install.sh
  ala --version
)
```

SSH 호스트 키 변경 경고가 나오면 대상 장비·키를 확인합니다. 검사를 끄거나 저장된 키를 무조건 삭제하지 않습니다.

## 버전 갱신

새 릴리스의 버전과 태그를 사용해 README 설치 절차를 반복합니다. 기존 ala를 먼저 삭제하지 않습니다.

- 새 코드는 `/opt/ala/releases/<버전>`에 설치합니다. 검증 성공 후 `/opt/ala/current`를 전환합니다.
- `/usr/local/bin/ala`는 현재 버전을 실행합니다. 다운로드/압축 해제 폴더를 지워도 동작합니다.
- 기존 `/etc/ala/config.json`과 실행 기록·백업은 유지합니다.
- 같은 버전·같은 파일은 유지하고, 같은 버전·다른 내용은 거부합니다.
- 설치·설정 작업과 갱신은 단일 잠금을 공유합니다. 다른 변경 작업 중이면 거부합니다.
- 준비 실패 시 기존 current를 유지합니다. 기존 버전은 자동 삭제하지 않습니다.
- CLI만 갱신하며 OS 설정 적용·Chrome 강제 재시작·재부팅을 실행하지 않습니다.

기존 버전으로 돌릴 때는 그 버전의 원래 배포 파일을 설치합니다. CLI를 되돌려도 이미 변경한 OS 설정·패키지까지 복원하지는 않습니다.
공개 배포 이전 버전의 파일은 공개 릴리스에 포함하지 않습니다.
아래 명령으로 수동 갱신합니다. 자동 운영 준비와 타이머는 [업데이트 절차](UPDATES.md)를 따릅니다.

```bash
ala update --prerelease        # 접수 후 완료·실패까지 표시
ala update --status            # 마지막 결과·실제 설치 버전
ala update --status --json     # 자동화용 읽기 전용 결과
ala update --prerelease --detach  # 완료를 기다리지 않고 접수 결과 반환
```

백그라운드 작업과 화면의 수명은 독립적입니다. Ctrl+C나 SSH 종료는 갱신을 취소하지 않습니다. `--detach`는 진행 중을 의미하는 종료 코드 2, 검증된 완료는 0, 실패·중단·확인 불가는 1입니다. TUI에서는 `u` 업데이트 관리창에서 같은 기록을 확인합니다. 설치된 버전과 현재 실행 중인 ala 버전이 다르면 다시 실행합니다.

RC15 이하에서 새 버전으로 처음 갱신할 때는 기존 명령의 동작에 따라 journal에서 완료를 확인해야 합니다. 갱신된 ala를 다시 실행한 이후 위 상태 조회를 사용합니다.

## 개발자용 배포 빌드

Python 3.12 이상에서 프로젝트 루트 기준으로 실행합니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.lock
.venv/bin/python scripts/build-release.py
```

`dist/ala-0.1.0rc35.tar.gz`와 `.sha256`을 생성합니다. 기존 wheel이 있으면 `--wheelhouse /경로/wheels`를 지정할 수 있으며 해시 검증은 유지됩니다.
빌더는 허용 목록의 코드·운영 문서·고정 wheel·파일별 manifest만 포함합니다. Git 이력, 테스트, 개발 가상환경과 작업 기록은 포함하지 않습니다.
게시된 파일을 같은 버전으로 다시 만들거나 덮어쓰지 않습니다.
후보 태그 `v0.1.0-rc.35`와 Python 버전 `0.1.0rc35`의 표기 차이에 주의하세요.

릴리스는 자동 테스트와 실제 Rocky 수용을 구분합니다. 현재 후보의 실제 장비 설치·GNOME·GPU·재부팅은 미검증입니다.

### 자체 갱신 기능 도입 이후

`ala update --check`는 권한 요청 없이 공개 릴리스를 확인합니다. `--json`으로 구조화된 결과를 받을 수 있습니다. `ala update`는 정식판만, `ala update --prerelease`는 시험판도 포함합니다. 이미 설치한 버전 이하로는 내려가지 않습니다. 조회는 최근 릴리스 100개 중 완전한 설치 자산이 있는 버전을 비교합니다.

현재 버전은 `ala update`에서 완료·실패까지 추적하고 `ala update --status`로 재조회합니다. 상세 로그는 `sudo journalctl -u ala-update -f`에서 확인합니다. 실패 후에는 실제 설치 버전을 대조해 기존 버전이 유지됐는지 구분합니다. 진행 중 갱신은 중복 시작을 거부하고 최종 설치는 기존 공통 잠금을 사용합니다. 전원 차단된 갱신은 자동 재개하지 않습니다.

다운로드는 공식 공개 저장소의 HTTPS 자산으로 제한하며 SHA-256과 파일별 manifest를 확인합니다. 체크섬은 손상 검증이며 별도 배포 서명 검증이 아닙니다. GitHub 저장소 및 배포 계정에 대한 신뢰가 필요합니다. API 공식 호스트 api.github.com을 공개 데이터 검사 허용 목록에 추가했습니다. 근거: https://docs.github.com/en/rest/releases/releases

RC5 이하에는 이 명령이 없으므로 최초 한 번 수동 갱신해야 합니다. 기존에 실행 중인 ala 화면은 이전 코드이므로 갱신 완료 후 종료하고 다시 실행하세요.

### 관리자 권한으로 실행

SSH 터미널에서 `ala`를 실행하면 sudo가 직접 암호를 받고 root 관리 화면으로 전환합니다. 실행 중인 버전의 root 소유 설치 경로를 검증한 뒤 고정 경로로 실행하며, 원본 소스 디렉터리는 자동 승격하지 않습니다. 이미 `sudo /usr/local/bin/ala`로 시작했다면 재인증하지 않습니다.

Chrome과 그래픽 세션의 사용자 작업은 지정한 일반 계정으로 실행합니다. `sudo` 실행자의 일반 계정 또는 저장된 화면 계정을 기본으로 사용합니다. 비대화형 CLI는 sudo -n RPC를 사용하며, 인증이 없으면 오류로 종료합니다. `--help`, `--version`, `update --check`는 승격하지 않습니다.
