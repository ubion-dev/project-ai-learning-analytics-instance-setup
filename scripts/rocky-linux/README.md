# ala 내부 Rocky Linux 설치 모듈

일반 사용자는 [프로젝트 README](../../README.md)를 따라 `ala`를 설치·실행합니다.
이 디렉터리만 복사하면 Python CLI/TUI와 의존성이 없어 `ala`를 설치할 수 없습니다.

| 파일 | 역할 |
|---|---|
| `setup.sh`, `common.sh` | 내부 단계 실행·공통 패키지/설정 처리 |
| `setup-base.sh`, `setup-bash.sh`, `setup-dev-tools.sh` | 기본 환경·Chrome·셸·선택 도구 |
| `setup-ssh.sh`, `setup-docker.sh` | SSH·방화벽·Docker |
| `setup-nvidia.sh`, `setup-nvidia-container.sh` | NVIDIA 드라이버·컨테이너 연결 |
| `setup-display.sh`, `setup-update-policy.sh` | 화면·절전·자동 업데이트 정책 |
| `verify.sh`, `lib/`, `bin/` | 검사와 내부 보조 기능 |

개별 Bash 모듈의 직접 실행은 개발·문제 분석용입니다. ala의 구성·잠금·실행 기록을 이용하려면 `ala setup`을 사용합니다.

- [모듈 상세](docs/REFERENCE.md)
- [환경 설정 상세](docs/ENVIRONMENT.md)
