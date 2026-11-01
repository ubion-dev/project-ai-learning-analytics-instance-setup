# ala 공개판 진행 기록

## 현재 상태

- CLI/TUI, 설치·검사, Chrome 표시, 초기 OS 준비, 네트워크 진단 기능을 포함한다.
- 공개 코드 기준선을 새 이력으로 준비했다. 내부 Git 이력·PR·운영 기록은 포함하지 않는다.
- 실제 Rocky CPU/GPU의 설치·화면·재부팅 수용은 미완료다.
- 공개 RC4 게시 완료. 다음 작업은 설치 안내 PR 검토·병합과 실제 장비 수용이다.

## 공개 설치 안내·RC4 준비

- Rocky에서 curl로 공개 릴리스를 직접 받고 SHA-256 검사 후 설치하는 절차를 README 기본 경로로 제공한다.
- Mac 다운로드·SCP 전달은 직접 다운로드가 불가능한 경우의 대안이다. GitHub 인증은 필요하지 않다.
- 설치기 동작은 유지하고 배포 버전을 0.1.0rc4로 올린다. 같은 버전의 다른 payload를 덮어쓰지 않는다.
- ala update/자동 갱신/서명 검증은 미구현임을 명시한다. 자동 운영 기능을 새로 추가하는 작업은 아니다.
- 학교별 정보나 기존 내부 기록은 새 공개 이력·배포 파일에 포함하지 않는다.
- 검증: 설치/동일 버전 유지/실패 갱신 보존/잠금/원본 폴더 삭제 회귀 16개 통과(25.26초).
- Gitleaks 공개 후보 소스·배포 파일 검사에서 탐지 없음. 배포 manifest 51개 해시와 내부 경로 제외 확인.
- 버전 상수 외 기존 Python/Bash 실행 코드가 기준선과 동일함을 대조했다. README/INSTALL 상대 링크 10개와 Bash 예시 6개 문법 정상.
- RC4 SHA256: a864fcb093c656092546d1f9428ead352ea019955eafb2b15bcd747dcedee618. 공개 CI·익명 다운로드 검증은 다음 단계다.

## 공개 배포 검증 완료

- PR #1: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/pull/1 (자동 병합하지 않음).
- 기능/배포 commit: 055c52d7fe3be1f8ff41601488c27559f1341e6d.
- Python3.12 CI: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/actions/runs/41310328567 — 348 passed + 40 subtests, 67.36초. Bash/고정 wheel/CLI smoke 통과.
- 공개 RC4: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/releases/tag/v0.1.0-rc.4
- CI 산출물과 로컬 산출물 byte 일치. 인증 없이 curl로 두 자산 다운로드, SHA-256 OK, CI와 byte 일치, 배포 entry의 --version/--help 정상.
- 공개 Git 이력은 새 기준선부터 시작하며 이전 내부 이력은 가져오지 않았다. 공개 저장소 visibility PUBLIC 확인.
- 후속 조건: PR 검토·병합, 실제 CPU/GPU 수용. 자동 업데이트·서명 검증은 별도 구현 대상이다.

## 2026-11-02 공개 저장소 단일화 완료

- 공개 저장소 하나에서 코드·릴리스·설치 안내 PR을 관리한다. 기존 비공개 저장소는 백업 후 삭제했다.
- 이전 내부 이력은 공개 저장소에 가져오지 않았다. 기존 자료는 로컬 백업으로만 보관한다.
- 공개 RC4 자산과 해시는 변경하지 않았다. 설치·다운로드 URL도 그대로 유지한다.
- CLI 삭제 요청은 권한 오류로 거절됐으며, 로그인된 GitHub 관리 화면에서 삭제 성공을 확인했다.
- 이번 변경은 진행 문서와 저장소 관리에 한정한다. 런타임 자동 테스트 결과를 재사용하며 실제 Rocky 수용은 여전히 미완료다.

## 2026-11-02 개발 커밋 이력 복구

- 사용자 요청에 따라 새 이력만 유지하는 결정을 변경했다. 민감한 운영 정보만 정리한 원래 main 커밋 21개를 공개 main의 조상으로 연결한다.
- 작성자·두 시각·메시지·병합 관계를 모두 보존하고 개인 이메일만 동일 계정 noreply로 변경했다.
- 134개 고유 blob 전수 패턴 검사와 Gitleaks 통과. 변경 문서는 SOURCE, 옛 진행 기록, 내부 Bash README의 3개뿐이며 구현 코드는 원본과 동일하다.
- 복구 main 트리는 기존 공개 main과 동일하다. 공개 이력 강제 덮어쓰기·RC4 태그/파일 변경은 없다. 원본 bundle 및 대응표는 로컬에도 보관한다.
- 공개 대응표와 복구 범위: [HISTORY](../HISTORY.md). 과거 PR 페이지·댓글·승인 상태는 복원되지 않는다.
- 설치 안내 PR은 유지한다. 이력·트리·배포 불변성을 검증하고 기존 런타임 테스트 결과를 재사용한다.
