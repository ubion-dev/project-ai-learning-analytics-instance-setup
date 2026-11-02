# 설치·배포·갱신

기본 설치는 [README](../README.md)의 **Rocky 직접 다운로드** 절차를 따릅니다.
공개 릴리스는 인증 없이 받을 수 있습니다. 서버에 Git·GitHub CLI·회사 계정을 설치할 필요가 없습니다.

## 설치 전제와 파일 검사

Rocky Linux 10 x86_64, GNOME/GDM, 기존 일반 sudo 사용자를 지원합니다.
시스템 Python 3.12 이상을 사용합니다. 설치기는 Python/pip이 없으면 DNF로 전제 패키지를 준비하므로 저장소 접속이 필요할 수 있습니다.
Textual과 고정 의존성은 배포 묶음의 wheel로 전용 venv에 설치하며 시스템 Python 패키지를 덮어쓰지 않습니다.
환경 설정 단계에는 Google·Docker·NVIDIA 등 필요한 패키지 저장소 접속이 필요합니다.

`ala-0.1.0rc12.tar.gz`와 `.tar.gz.sha256`은 같은 릴리스에서 받습니다.
SHA-256 검사는 파일 일치/손상 확인이며 배포자 서명을 대신하지 않습니다. 현재 배포는 GitHub HTTPS 경로를 신뢰하며 별도 서명 검증은 미구현입니다.
검사가 실패하면 설치하지 않습니다. GitHub의 Source code 압축은 wheel과 manifest가 없는 개발 소스이므로 설치용 배포 파일과 다릅니다.

## Mac에서 받아 SCP로 전달

Rocky가 GitHub에 직접 접속하지 못하는 경우 사용합니다. Mac 터미널에서 실행합니다.
`user`와 `서버IP`는 실제 SSH 대상에 맞게 바꿉니다.

```bash
(
  set -e
  VERSION='0.1.0rc12'
  RELEASE='v0.1.0-rc.12'
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
  sha256sum -c ala-0.1.0rc12.tar.gz.sha256
  tar -xzf ala-0.1.0rc12.tar.gz
  cd ala-0.1.0rc12
  sudo bash ./install.sh
  ala --version
)
```

SSH 호스트 키 변경 경고가 나오면 대상 장비·키를 확인합니다. 검사를 끄거나 저장된 키를 무조건 삭제하지 않습니다.

## 버전 갱신

새 릴리스의 `VERSION`/`RELEASE`를 사용해 README 설치 절차를 반복합니다. 기존 ala를 먼저 삭제하지 않습니다.

- 새 코드는 `/opt/ala/releases/<버전>`에 설치합니다. 검증 성공 후 `/opt/ala/current`를 전환합니다.
- `/usr/local/bin/ala`는 현재 버전을 실행합니다. 다운로드/압축 해제 폴더를 지워도 동작합니다.
- 기존 `/etc/ala/config.json`과 실행 기록·백업은 유지합니다.
- 같은 버전·같은 파일은 유지하고, 같은 버전·다른 내용은 거부합니다.
- 설치·설정 작업과 갱신은 단일 잠금을 공유합니다. 다른 변경 작업 중이면 거부합니다.
- 준비 실패 시 기존 current를 유지합니다. 기존 버전은 자동 삭제하지 않습니다.
- CLI만 갱신하며 OS 설정 적용·Chrome 강제 재시작·재부팅을 실행하지 않습니다.

기존 버전으로 돌릴 때는 그 버전의 원래 배포 파일을 설치합니다. CLI를 되돌려도 이미 변경한 OS 설정·패키지까지 복원하지는 않습니다.
공개 배포 이전 버전의 파일은 공개 릴리스에 포함하지 않습니다.
배포 서명 검증과 자동 확인 타이머는 미구현입니다. 자체 갱신은 아래의 명시적 명령으로 실행합니다.

## 개발자용 배포 빌드

Python 3.12 이상에서 프로젝트 루트 기준으로 실행합니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.lock
.venv/bin/python scripts/build-release.py
```

`dist/ala-0.1.0rc12.tar.gz`와 `.sha256`을 생성합니다. 기존 wheel이 있으면 `--wheelhouse /경로/wheels`를 지정할 수 있으며 해시 검증은 유지됩니다.
빌더는 허용 목록의 코드·운영 문서·고정 wheel·파일별 manifest만 포함합니다. Git 이력, 테스트, 개발 가상환경과 작업 기록은 포함하지 않습니다.
게시된 파일을 같은 버전으로 다시 만들거나 덮어쓰지 않습니다.
후보 태그 `v0.1.0-rc.12`와 Python 버전 `0.1.0rc12`의 표기 차이에 주의하세요.

릴리스는 자동 테스트와 실제 Rocky 수용을 구분합니다. 현재 후보의 실제 장비 설치·GNOME·GPU·재부팅은 미검증입니다.

### 자체 갱신 기능 도입 이후

`ala update --check`는 권한 요청 없이 공개 릴리스를 확인합니다. `--json`으로 구조화된 결과를 받을 수 있습니다. `ala update`는 정식판만, `ala update --prerelease`는 시험판도 포함합니다. 이미 설치한 버전 이하로는 내려가지 않습니다. 조회는 최근 릴리스 100개 중 완전한 설치 자산이 있는 버전을 비교합니다.

`sudo journalctl -u ala-update -f`에서 결과를 확인하세요. 실패하면 기존 current가 유지되며, 다음 실행에서 다시 조회할 수 있습니다. 진행 중 갱신은 중복 시작을 거부하고 최종 설치는 기존 공통 잠금을 사용합니다. 전원 차단된 갱신은 자동 재개하지 않습니다.

다운로드는 공식 공개 저장소의 HTTPS 자산으로 제한하며 SHA-256과 파일별 manifest를 확인합니다. 체크섬은 손상 검증이며 별도 배포 서명 검증이 아닙니다. GitHub 저장소 및 배포 계정에 대한 신뢰가 필요합니다. API 공식 호스트 api.github.com을 공개 데이터 검사 허용 목록에 추가했습니다. 근거: https://docs.github.com/en/rest/releases/releases

RC5 이하에는 이 명령이 없으므로 최초 한 번 수동 갱신해야 합니다. 기존에 실행 중인 ala 화면은 이전 코드이므로 갱신 완료 후 종료하고 다시 실행하세요.

### 관리자 권한으로 실행

SSH 터미널에서 `ala`를 실행하면 sudo가 직접 암호를 받고 root 관리 화면으로 전환합니다. 실행 중인 버전의 root 소유 설치 경로를 검증한 뒤 고정 경로로 실행하며, 원본 소스 디렉터리는 자동 승격하지 않습니다. 이미 `sudo /usr/local/bin/ala`로 시작했다면 재인증하지 않습니다.

Chrome과 그래픽 세션의 사용자 작업은 지정한 일반 계정으로 실행합니다. `sudo` 실행자의 일반 계정 또는 저장된 화면 계정을 기본으로 사용합니다. 비대화형 CLI는 sudo -n RPC를 사용하며, 인증이 없으면 오류로 종료합니다. `--help`, `--version`, `update --check`는 승격하지 않습니다.
