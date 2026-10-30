# ala 배포·설치·갱신

대상은 **Rocky Linux 10 x86_64, GNOME/GDM과 기존 일반 사용자가 준비된 서버**입니다.
OS 자체 설치와 파티션 설정은 하지 않습니다. 설치 작업은 SSH와 sudo로 수행합니다.

## 배포 묶음 만들기 — Mac 또는 개발 PC

Python 3.12 이상과 pip가 있는 개발 환경에서 프로젝트 루트 기준으로 실행합니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.lock
.venv/bin/python scripts/build-release.py
```

생성 결과는 다음과 같습니다. GitHub 후보 태그 `v0.1.0-rc.3`의 Python 버전 표기는 `0.1.0rc3`입니다.

```text
dist/ala-0.1.0rc3.tar.gz
dist/ala-0.1.0rc3.tar.gz.sha256
```

묶음 내부 최상위 디렉터리는 `ala-0.1.0rc3`입니다. 실행 코드, 내부 Rocky 스크립트, 설치기,
고정된 runtime lock, universal wheel, 파일별 SHA256 manifest만 포함합니다.
전체 Git 이력, 작업 기록, 고객 자료, 테스트, 개발 가상환경은 포함하지 않습니다.

기존 wheel 폴더를 사용하려면 `--wheelhouse /경로`를 지정합니다. 이 경우에도 lock의 버전·해시를 검사합니다.
최초 다운로드는 Python 3.12를 대상으로 하며 현재 runtime lock은 모두 universal wheel입니다.

```bash
.venv/bin/python scripts/build-release.py --output dist --wheelhouse /경로/wheels
```

파일 SHA256은 전송 오류·변조 여부 검사입니다. 배포자를 인증하는 서명을 대신하지는 않습니다.
승인한 레포지토리와 릴리스에서 받은 파일을 사용합니다.

## 배포 묶음 전달 — Mac

정해진 버전의 GitHub Release가 있다면 Mac에서 `gh release download`로 받을 수 있습니다.
아직 릴리스하지 않은 후보는 위 builder로 생성한 파일을 사용합니다.

```bash
gh release download v0.1.0-rc.3 \
  --repo ubion-dev/project-ai-learning-analytics-instance-setup \
  --pattern 'ala-0.1.0rc3.tar.gz*'
shasum -a 256 -c ala-0.1.0rc3.tar.gz.sha256

TARGET_USER='user'
SERVER_HOST='서버IP'
scp ala-0.1.0rc3.tar.gz ala-0.1.0rc3.tar.gz.sha256 "${TARGET_USER}@${SERVER_HOST}:~/"
ssh "${TARGET_USER}@${SERVER_HOST}"
```

서버에 GitHub 계정·토큰·프로젝트 전체를 전달하지 않습니다.

## CLI 설치 — Rocky SSH 터미널

```bash
sha256sum -c ala-0.1.0rc3.tar.gz.sha256
tar -xzf ala-0.1.0rc3.tar.gz
cd ala-0.1.0rc3
sudo bash ./install.sh
ala --version
ala --help
```

이 단계는 **ala 실행 도구 설치**입니다. Docker·GPU·화면 정책 등 서버 환경 변경은 아직 실행하지 않습니다.

설치기는 root·OS·아키텍처·Python 버전을 검사합니다. 시스템 `python3`가 없으면 DNF로 설치하고,
가상환경 준비에 pip가 없으면 `python3-pip`을 설치합니다. Python은 3.12 이상이 필요합니다.
이 OS 전제 패키지의 DNF 설치에는 패키지 저장소 접속이 필요할 수 있습니다.
기본 설치는 부팅 커널과 Rocky release 패키지를 제외합니다. 해당 변경이 필요한 의존성은 설치 실패로 남기며 자동 OS 업그레이드로 우회하지 않습니다.
Textual과 그 의존성은 배포 묶음의 wheel에서 해시를 검증해 전용 venv에 오프라인 설치합니다.
시스템 Python에 pip 패키지를 설치하거나 uv를 설치하지 않습니다.

설치되는 주요 경로는 다음과 같습니다.

| 경로 | 역할 |
|---|---|
| `/opt/ala/releases/0.1.0rc3` | 버전 고정 코드·wheel·전용 venv |
| `/opt/ala/current` | 사용할 버전의 심볼릭 링크 |
| `/usr/local/bin/ala` | root 소유 명령 진입점 |
| `/run/lock/ala/operation.lock` | 환경 변경·도구 갱신 공유 잠금 |

설치 이후 다운로드한 압축·해제 폴더를 삭제해도 명령은 동작합니다.

## 최초 환경 구성

일반 사용자로 실행합니다. 필요한 순간에만 sudo 인증을 받습니다.

```bash
ala config edit
# 새 서버의 최초 OS 업데이트를 원하는 경우에만 실행
ala prepare --plan
ala prepare --apply
# 재부팅 대기이면 직접 재부팅 → SSH 재접속 → ala prepare --apply
ala setup --plan
ala setup --apply
ala check
```

TUI에서는 화면 사용자와 DID 주소를 지정합니다. 자동화에서는 JSON을 가져올 수 있습니다.
`user`와 주소는 실제 값으로 바꿉니다. 주소에는 로그인 자격증명이나 토큰을 넣지 않습니다.

```json
{
  "schema_version": 1,
  "user": "user",
  "url": "https://example.com"
}
```

```bash
ala config import ./instance.json
ala config show --json
ala setup --plan --json
ala setup --apply --detach
ala status
```

CLI 설치와 환경 설치는 별도입니다. 환경 설치에는 Google·Docker·NVIDIA 등 필요한 공식 저장소 접속이 필요합니다.
`--plan`은 변경을 적용하지 않으며 `--apply`를 명시해야 설치·설정이 수행됩니다.
완료 여부는 정상·실패·입력/재부팅 대기를 구분해서 확인합니다.

## 명시적으로 도구 갱신하기

새 버전의 배포 묶음을 전달하고 같은 `install.sh`를 실행합니다. 자동 갱신은 하지 않습니다.
설치·환경 변경 작업이 진행 중이면 갱신을 거부합니다.

- 같은 버전과 같은 파일은 기존 venv·파일·진입점을 유지합니다.
- 같은 버전에 다른 내용이 있으면 덮어쓰지 않습니다. 새 버전으로 배포해야 합니다.
- 신규 버전은 검증·venv 준비·실행 확인이 성공한 다음 `current`를 전환합니다.
- 준비 중 실패하면 기존 `current`와 명령을 유지하고 실패한 새 경로를 정리합니다.
- 이전 버전을 자동 삭제하거나 OS 설정을 자동 롤백하지 않습니다.

기존 버전으로 되돌릴 필요가 있으면 그 버전의 원래 배포 묶음으로 설치기를 다시 실행합니다.
현재 실행 중인 Chrome은 도구 갱신 때문에 강제로 종료하지 않습니다. 변경한 표시 실행을 적용하려면
유지보수 시간에 `ala chrome restart`를 명시적으로 실행합니다.

현재 후보의 자동 테스트는 임시 디렉터리·모의 명령을 사용합니다.
실제 Rocky CPU/GPU의 설치·GNOME 표시·재부팅 수용 결과는 별도로 확인해야 합니다.
