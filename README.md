# ala — AI 학습 분석실 인스턴스 환경 관리

SSH로 Rocky Linux 서버의 기본 환경·Docker·GPU 드라이버·Chrome DID 화면을 설치하고 관리하는 CLI/TUI입니다.

- 대상: **Rocky Linux 10 x86_64, GNOME/GDM, 기존 sudo 사용자, SSH 접속 가능한 서버**
- 현재 버전: **0.1.0rc3** 배포 후보. 실제 장비의 재부팅·화면 수용은 진행 전입니다.
- 설치 방식: **Mac에서 배포 파일 준비 → scp 전송 → SSH에서 설치 → ala로 설정**

공개 `curl | bash` 설치기는 제공하지 않습니다. 비공개 릴리스는 Mac에서 받고, 서버에는 GitHub 계정 없이 전달합니다.
서버의 패키지 설치에는 인터넷 연결이 필요할 수 있습니다. CPU/GPU 서버 각각 같은 절차로 설치합니다.

## 1. 배포 파일 준비 — Mac

[RC3 릴리스의 Assets](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/releases/tag/v0.1.0-rc.3)에서 다음 **두 파일**을 Mac의 다운로드 폴더에 받습니다. 저장소 접근 권한이 있는 GitHub 계정으로 로그인하세요.

- `ala-0.1.0rc3.tar.gz`
- `ala-0.1.0rc3.tar.gz.sha256`

GitHub의 `Source code` 파일이나 `scripts/rocky-linux` 폴더만으로는 ala를 설치할 수 없습니다.
이미 빌드한 파일이 있으면 다운로드 대신 프로젝트의 `dist/` 폴더를 사용해도 됩니다.

## 2. 파일 전송·SSH 접속 — Mac 터미널

`user`와 `서버IP`를 실제 SSH 계정·주소로 바꾸세요. 파일 전송 명령은 **scp**, 서버 접속 명령은 **ssh**입니다.

```bash
cd ~/Downloads
# 파일이 다른 곳에 있다면 그 폴더로 이동합니다.

TARGET_USER='user'
SERVER_HOST='서버IP'

scp ala-0.1.0rc3.tar.gz ala-0.1.0rc3.tar.gz.sha256 \
  "${TARGET_USER}@${SERVER_HOST}:~/"

ssh "${TARGET_USER}@${SERVER_HOST}"
```

파일 두 개를 보내므로 `scp -r`은 필요하지 않습니다. 이후 명령은 접속한 **Rocky 터미널**에서 실행합니다.

## 3. ala 설치 — Rocky SSH 터미널

먼저 전송한 파일을 검사합니다.

```bash
cd ~
sha256sum -c ala-0.1.0rc3.tar.gz.sha256
```

`OK`가 나오면 설치합니다. 실패하면 진행하지 말고 파일을 다시 받거나 전송하세요.

```bash
tar -xzf ala-0.1.0rc3.tar.gz
cd ala-0.1.0rc3
sudo bash ./install.sh
ala --version
```

`0.1.0rc3`이 나오면 도구 설치가 완료된 것입니다. 이제 어느 폴더에서든 `ala`를 실행할 수 있습니다.
**도구 설치만으로 Docker·GPU·자동 로그인 설정이 적용되지는 않습니다.** 다음 단계에서 적용합니다.

## 4. 환경 설정·사용 — Rocky SSH 터미널

일반 사용자로 관리 화면을 실행합니다. 필요한 작업에서만 sudo 인증을 받습니다.

```bash
ala
```

1. **구성**에서 화면에 로그인할 기존 일반 사용자와 DID URL을 저장합니다. 주소는 비워도 됩니다.
2. 자동 로그인하려면 **화면 정책 적용**, **GNOME 자동 로그인**을 켭니다. DID 페이지를 띄우려면 URL을 입력하고 **Chrome 자동 실행**도 켭니다.
3. **설치·설정 → 환경 설정 계획**으로 변경 내역을 보고 **계획 적용**을 선택합니다.
4. **검사**에서 결과와 남은 조치를 확인합니다.

화살표·Tab으로 이동하고 Enter로 실행, Space로 선택합니다. Esc는 메뉴/취소, `q`는 종료입니다.
구성 저장만으로 설정이 적용되지는 않습니다. 기존 정상 패키지·동일 설정은 유지합니다.

**최초 OS 업데이트도 하려면:** 3번의 환경 설정 전에 **초기 OS 계획**을 선택해 적용합니다. 재부팅 대기가 나오면 직접 재부팅하고 SSH로 재접속해 `ala prepare --apply`로 부팅 결과를 확인합니다. 기존 NVIDIA/DKMS가 있으면 호환성 검토가 필요해 차단될 수 있습니다. [초기 준비 상세](docs/PREPARATION.md)

### 메뉴 대신 명령으로 적용하기

```bash
ala config edit       # 사용자·주소·기능 저장
ala setup --plan      # 필요한 변경 조회만
ala setup --apply     # 설치·설정 적용
ala check             # 결과 검사
```

`setup`은 전체 OS 업데이트를 하지 않습니다. 최초 OS 업데이트는 별도 `prepare` 작업이고, 운영 중 자동 업데이트 차단은 유지됩니다.

### 재부팅 후 확인

재부팅·재로그인 대기가 있으면 작업 결과를 확인한 뒤 `sudo reboot`를 실행합니다. 도구가 자동 재부팅하지는 않습니다.
Mac에서 SSH로 다시 접속해 `ala check`를 실행하고, **실제 모니터에서 자동 로그인과 DID 화면을 확인**하세요.
URL이 없어도 바탕화면 자동 로그인은 설정할 수 있습니다. 이 설정은 SSH 비밀번호를 없애지 않습니다.

## 자주 쓰는 명령

| 작업 | 명령 |
|---|---|
| 전체 상태·설정 검사 | `ala status`, `ala check` |
| 구성 확인·수정 | `ala config show`, `ala config edit` |
| Wi-Fi 진단 | `ala doctor network` |
| Chrome 시작·중지·재시작 | `ala chrome start`, `ala chrome stop`, `ala chrome restart` |
| Chrome 상태·로그 | `ala chrome status`, `ala chrome logs` |
| 설치 기록·로그 | `ala runs list`, `ala runs show RUN_ID`, `ala logs --run RUN_ID --follow` |
| 도움말 | `ala --help` |

`RUN_ID`는 실행 기록에 표시된 값입니다. SSH 연결이나 TUI를 닫아도 시작한 설치는 계속됩니다.
Chrome의 `stop`은 현재 실행만 중지합니다. 다음 부팅에서도 끄려면 구성에서 **Chrome 자동 실행**을 끄고 적용하세요.

## 상세 안내

| 필요한 내용 | 문서 |
|---|---|
| GitHub CLI 다운로드, 배포 빌드, ala 버전 갱신 | [설치·배포](docs/INSTALL.md) |
| 최초 OS 업데이트, Wi-Fi 진단, 로그인·잠금화면 문제 | [초기 준비](docs/PREPARATION.md) |
| 장애 복구, 설정 경로, 무인 화면 정책, 자동화 | [운영 안내](docs/OPERATIONS.md) |
| 드라이버 지원 범위·업데이트 정책 | [드라이버 안내](docs/DRIVERS.md) |
| 실제 장비 검증 항목·결과 | [수용 기록](docs/ACCEPTANCE.md) |

개발 기록: [계획](docs/plans/ala-v1-plan.md) · [진행 상태](docs/plans/ala-v1-progress.md) · [분리 출처](docs/SOURCE.md)
