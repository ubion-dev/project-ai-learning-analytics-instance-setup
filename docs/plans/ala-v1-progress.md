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

## 공개 데이터 보호 강화 — 진행 중

- 개인 이메일·비루프백 IP·실제 접속 정보·비밀정보를 공개하지 않는 규칙을 AGENTS와 운영 문서에 명시한다.
- index 전체/전송 이력 전체 검사, GitHub noreply 메타데이터 검사, Gitleaks 필수 실행을 로컬 훅과 CI에 연결한다. CI만으로 공개 전 차단을 보장하지 않는다.
- 테스트 합성 IP도 루프백으로 변경하고 기존 공개 브랜치/태그의 이전 리터럴을 정리한다. 작성자·날짜·메시지·부모 관계를 보존하되 해시는 변경될 수 있다.
- 같은 RC4 바이너리를 재빌드/덮어쓰지 않는다. 이력 정리로 소스 태그 대상은 갱신되며 공개된 기존 객체/캐시까지 회수되었다고 주장하지 않는다.

## 공개 데이터 검사 구현·검증

- pre-commit(index 전체+메타데이터), commit-msg, pre-push(전송 조상 전체)와 CI public-data 검사를 구현했다. 누락 도구/검사 오류는 차단하고 원문은 로그에 출력하지 않는다.
- CI는 고정 공식 Gitleaks 버전/해시를 검증하며 public-data 성공 전 테스트/배포 후보 업로드를 실행하지 않는다. 로컬 훅은 별도 설치가 필요하고 완전한 서버 측 유출 방지와 같지 않다.
- 새 규칙 테스트 및 network/legacy 회귀: 69 passed + 27 subtests. 기존 모든 커밋의 이름·이메일·두 시각·본문·부모 관계를 유지함을 별도 대조했다.
- 전체 공개 이력 검사 통과. 비루프백 IP 문자열은 조상 이력에서도 제거했다. 주소 분류 시험은 실행 시 합성 주소를 생성하며 실제 장비 값은 사용하지 않는다.
- main에도 보호 기능을 직접 반영한다. 기존 설치 안내 PR을 자동 병합하지 않으며 RC4 설치 자산을 덮어쓰지 않는다.

- 최종 전체 로컬 회귀: 356 passed + 40 subtests (119.34초). 공개 Git 객체 535개 검사 통과. 추가 문서 변경은 런타임에 영향이 없다.
- GitHub secret scanning 및 push protection 활성화 응답 확인. 개인 이메일/IP 판별과 일반 사용자명 등 의미 검토는 별도 로컬/CI/사람의 검사 범위로 명시한다.

## 원격 반영 승인 대기

- 원격 브랜치/태그 갱신은 자동 승인 검토가 실행 전에 거절했다. 게시된 RC4 태그 이동은 별도 승인이 필요하다는 이유다. 원격 Git 이력은 아직 이전 상태다.
- 로컬 구현·이력 정리·회귀 검증과 GitHub secret scanning/push protection 활성화는 완료했다.
- RC4 설치 자산/체크섬은 유지하지만 소스 태그를 이동하면 GitHub 자동 생성 Source code 압축본과 커밋 식별자가 달라진다. 이 범위를 사용자에게 별도로 확인 중이다.
- 승인 전 같은 태그 이동을 재시도하거나 태그 삭제로 우회하지 않는다. 승인 후 예상 원격 해시 확인 → 원자적 반영 → CI → develop 동기화/훅 설치 → 최종 기록 순서로 진행한다.

## 2026-11-02 공개 정책 반영 완료

- 사용자가 RC4 소스 태그 이동을 명시 승인했다. main·설치 안내 브랜치·RC4 태그를 예상 원격 SHA 조건으로 원자적 갱신했다. 원격 반영 승인 대기는 해소됐다.
- 태그 대상: ff5653fb4588d1c747b63e89e3f57364e97d4673. RC4 설치 자산 두 개의 ID·수정 시각·SHA-256 유지 확인. 릴리스에 소스 이력 변경과 바이너리 불변성을 명시했다.
- main CI 41366999277, 설치 브랜치 CI 41366999380, PR CI 41367003952/41367003684 모두 성공. public-data, 회귀, 후보 빌드/CLI smoke 통과.
- main에 public-data 필수 검사(strict)와 관리자 적용, 강제 푸시/삭제 금지를 설정했다. GitHub secret scanning/push protection도 활성 상태다.
- 작업 폴더·develop 복제본에 로컬 pre-commit/commit-msg/pre-push 훅을 활성화했고, develop의 기존 이력은 별도 bundle 백업 후 동기화했다.
- PR #1은 검토를 위해 OPEN 상태로 유지한다. 실제 Rocky 서버·OS 설정은 변경하지 않았고 실기기 수용은 여전히 미완료다.

## ALA-13 시작

- 사용자가 메뉴 복잡성과 다음 행동 불명확을 지적해 순서형 기본 흐름을 승인했다. main 627dea4에서 codex/guided-setup으로 작업한다.
- 기존 TUI는 여섯 기능 탭과 별도 저장/계획/적용/인증 조작을 요구한다. 설치 엔진은 유지하고 기본 탐색과 설정 동선을 바꾼다.
- 완료 조건: 기본 메뉴 4개, 계정/화면 선택만으로 설정 시작, 친숙한 변경 요약, 작업 종료 후 다음 행동, 기존 안전 계약 및 회귀 테스트 통과.

## ALA-13 구현·대상 검증

- 첫 화면 네 메뉴, 계정/화면/변경 요약 단계, 명시적 설정 시작, 진행/다음 행동을 추가했다. sudo는 순서형 시작에서 요청한다.
- config-save/plan/submit 계약과 구성 변경 감지·단일 작업·SSH 독립 실행을 재사용한다. 고급 옵션은 보존하고 순서형 흐름에서는 자동 로그인/화면 정책을 켠다.
- 변경 확인 시 구성만 저장하며 이전/홈으로 나가도 설치하지 않는다. 저장한 선택은 남는다는 점을 README에 명시했다.
- TUI 25개, TUI+CLI/job 45개 통과. 80×24 합성 화면을 렌더링해 홈/선택/요약과 버튼 가시성을 확인했다. 기존 세 메뉴 순서 의존 테스트는 새 네 메뉴 동선으로 변경했다.
- RC5 버전/설치 안내를 준비한다. RC4 자산·실제 Rocky 환경은 변경하지 않는다. 전체 회귀와 CI/RC5 배포가 남아 있다.

## ALA-13 자동 검증·RC5 게시 완료

- 구현 커밋 9cdffcf. 전체 로컬 회귀 362 passed + 40 subtests (126.49초). GitHub CI 41391116792 성공: public-data, Python 3.12 회귀, 고정 의존성, Bash 문법, 후보 빌드 및 CLI smoke 통과.
- CI 산출물 RC5의 manifest 52개 파일 해시와 entry --version을 검증했다. 인증 없는 공개 다운로드도 같은 SHA-256임을 확인했다: 083928a450059b5213be1da365a9e8c3a6473c31f97f172f5e3c943b4b0382c6.
- PR #2: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/pull/2 — 검토 대기, 자동 병합하지 않는다.
- 시험판: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/releases/tag/v0.1.0-rc.5 — RC4 자산은 유지한다.
- 남은 조건: 실제 Rocky에서 RC5 설치/갱신 후 키보드 설정 흐름과 실제 화면·재부팅 수용. 이번 작업에서는 실제 서버를 변경하지 않았다.

## ALA-14 시작

- UI PR #2 병합 확인, main 042c191 기반 codex/self-update. 조회/채널/검증/갱신/실패 테스트와 문서화 진행.

## ALA-14 구현·로컬 검증

- 첫 화면 비동기 정식판 안내와 update --check/--json/--prerelease 구현. API 장애는 관리 기능과 분리한다. 조회는 인증/파일 변경 없음.
- 고정 공개 저장소의 설치 자산만 root 임시 디렉터리로 다운로드하고 SHA-256·manifest·압축 경로/유형/크기 검증 후 기존 installer를 실행한다. 별도 서명 검증은 미구현이다.
- 버전 고정 systemd 실행기와 고정 unit으로 SSH 종료·중복 갱신에 대응한다. 설치 공통 잠금 아래 현재 버전을 재검사하여 오래 열린 CLI에서의 다운그레이드도 차단한다. 기존 구성/current 보존 동작 유지.
- 전체 로컬 371 passed + 40 subtests (128.88초). 이후 추가 검증 포함 update/TUI 39 passed, installer 다운그레이드 방지 1 passed. 최종 전체 집계는 CI에서 확인한다.
- RC6 준비, RC5 불변. 최초 수동 갱신 필요/정식 기본 채널/시험판 명시 선택/로그 조회를 문서화했다. 실제 Rocky 갱신은 미검증이다.

## ALA-14 RC6 후보 게시 완료

- 구현 cb75e4b. CI 41393391577 성공: Python 3.12 최종 377 passed + 40 subtests (81.11초), 공개 이력 검사, Bash, 고정 의존성, 배포 빌드/CLI smoke 통과.
- CI RC6의 체크섬·manifest 53개 파일 검증 및 인증 없는 공개 다운로드 byte 일치 확인. 실제 공개 API 조회도 인증 없이 정상 응답했다.
- PR #3: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/pull/3 — 검토 대기.
- 시험판: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/releases/tag/v0.1.0-rc.6
- RC5 이하 최초 수동 갱신 필요. 이후 정식 기본/시험판 명시 선택으로 자체 갱신한다. 자동 적용 타이머는 없다.
- 실제 Rocky 갱신·SSH 종료 후 지속·재접속 수용은 남아 있다. 이번 작업은 실제 서버를 변경하지 않았다.

## ALA-15 시작

- PR #3 병합 main b869bb9 기반. 사용자가 Podman 차단과 낮은 가독성을 보고했다.
- Docker RHEL 공식 안내에서 podman 사전 제거 대상 확인. 검사 우회 대신 원인/다음 행동 노출 및 시각 계층을 개선한다. 실제 장비 패키지는 변경하지 않는다.

## ALA-15 구현·대상 검증

- 공통 화면 색상/여백/포커스 대비, 차단 상태 카드, 변경 요약, 접는 상세 내역으로 개선했다. 실제값과 항목 ID를 상세에서 유지한다.
- 설치 차단과 적용 후 대기를 분리하고 계획 허용 판정을 UI 상태 카드와 공유한다. Podman 자동 제거는 추가하지 않는다.
- TUI·엔진 59 passed + 3 subtests. Podman 차단 1개와 재부팅 대기가 섞이지 않음, 80×24 하단 버튼 가시성, 키보드 상세 펼침을 검증했다.
- 합성 데이터로 80×24/120×36 실제 TUI SVG를 렌더링하고 색상/무색 환경을 확인했다. 실제 장비 화면은 미검증이다.
- RC7 후보 준비. 전체 회귀·빌드는 CI에서 확인한다.

## ALA-15 RC7 후보 완료

- 구현 d4ce013, CI 41405610840: 전체 378 passed + 40 subtests (53.37초). 공개 이력 검사·빌드·CLI smoke 통과.
- RC7 체크섬과 manifest 53개 파일 검증, 인증 없는 공개 다운로드 byte 일치 확인.
- PR #4: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/pull/4
- 시험판: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/releases/tag/v0.1.0-rc.7
- RC6에서 ala update --prerelease 사용 가능. 기존 후보는 유지한다. 실제 장비의 Podman 사용 여부 확인·정리와 화면 수용은 남아 있다.

## ALA-16 시작

- 사용자가 단계별 집중형(가운데 메뉴, 한 화면에 한 작업)을 선택했다.
- PR #4 병합 main 1587195 기준 codex/focused-tui. 이전 RC7 기능은 재구현하지 않고 탐색/레이아웃/포커스를 바꾼다.

## ALA-16 구현·대상 검증

- 작업 목록을 홈 가운데로 옮기고 단계별 화면에서 고정 사이드바를 제거했다. 한 줄 헤더·홈 이동·단계 진행 표시·하단 작업 버튼으로 정리했다.
- 목록에는 작업별 설명을 붙이고, 이동한 화면의 입력/표/버튼으로 포커스를 옮긴다. 안내 중복을 줄여 80×24 정상 변경 확인에서 요약과 상세 진입점까지 보이도록 했다.
- 기존 sudo/저장/계획/명시적 적용/작업 지속 계약은 재사용한다. 설치 엔진은 변경하지 않았다.
- 기존 TUI·CLI/jobs 47개 통과, 추가 집중형 검증 포함 TUI 29개 통과. 홈 전용 목록/각 작업 포커스/상단 홈/뒤로/미저장 입력 보존/화면 크기 변경/무색 터미널을 확인했다.
- 합성 데이터로 홈·계정·정상·차단 화면을 80×24와 120×36에서 실제 TUI SVG로 렌더링·확인했다. RC8 전체 회귀·배포 후보는 CI에서 확인한다. 실제 Rocky 화면 수용은 미완료다.

## ALA-16 RC8 후보 완료

- 구현 3c95499. CI 41407661268: Python 3.12 전체 380 passed + 40 subtests (57.37초). 공개 이력 검사·Bash·고정 의존성·후보 빌드·CLI smoke 통과.
- RC8 체크섬과 manifest 53개 파일 검증, 인증 없는 공개 다운로드 byte 일치 확인.
- PR #5: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/pull/5
- 시험판: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/releases/tag/v0.1.0-rc.8
- RC6 이상에서 ala update --prerelease로 설치 가능하다. 실제 장비 터미널의 가독성·조작 수용은 남아 있다. 실제 서버는 이번 작업에서 변경하지 않았다.

## ALA-17 시작

- 사용자가 관리자 권한 실행을 요청했다. 대화형 ala의 시작 시 sudo 인증 및 root 재실행으로 정리한다.
- 현재 sudo /usr/local/bin/ala 직접 실행은 이미 가능함을 확인했다. 비대화형 RPC·일반 사용자 Chrome 계약을 유지한다.
- RC8/PR #5 기준 aa381cd에서 codex/admin-entry. PR #5는 현재 OPEN이므로 후속 PR은 해당 브랜치를 기반으로 한다.
- 디자인 구체화 질문은 대기 중이며 관리자 진입 경로를 먼저 구현한다.

## ALA-18 범위 결정

- 사용자 답변: Claude/Codex 같은 간결한 CLI를 기대했고, 간편 설정 한 번과 기대 상태 확인이 필요함. 기본 전체화면 디자인을 계속 바꾸는 방향을 중단한다.
- ALA-17과 같은 브랜치에서 기본 진입을 단순화한다. 이전 Textual 기능은 상세 진입으로 보존하고 일반 설정에 추가 선택을 요구하지 않는다.

## ALA-17/18 구현·대상 검증

- 대화형 시작은 검증된 고정 릴리스로 sudo 재실행하고 root이면 재인증하지 않는다. help/version/update 확인·비TTY·사용자 Chrome 경로는 승격하지 않는다.
- 기본 ala는 전체화면을 열지 않고 Enter 간편 설정 / s 상태 확인 / q 종료를 제공한다. Textual은 ala tui로만 명시적으로 연다.
- 처음 명시적 적용에서만 sudo 일반 사용자 기본 구성을 저장한다. 기본 URL 미지정은 바탕화면 준비로 처리한다. 기존 구성/선택값은 유지한다. 검토 구성 변경·작업 중복 검사 후 공통 잠금 안에서만 최초 파일을 쓴다.
- 기능별 상태 요약과 원인·조치를 제공하며 전체 JSON 및 기존 작업 기록을 유지한다. 새 버전 알림은 비동기 조회 후 입력 사이에 표시한다.
- 관리자 진입/간편 콘솔/CLI jobs/표시 관련 65개, 이후 최초 저장/멱등성/중복 거부 및 기존 TUI 포함 62개 검증 통과. 전체 회귀 진행 중.
- README는 설치 → 간편 설정/상태 확인 → 업데이트 순서로 정리하고 상세 옵션은 별도로 뒀다. 실제 Rocky sudo/기본 콘솔 수용은 미검증이다.

## ALA-17/18 로컬 전체 검증

- 전체 로컬 393 passed + 40 subtests (123.25초). 요약 안내 문구 보강 후 관리자 진입·간편 콘솔·CLI jobs 33개 재검증 통과.
- 일반 계정 기본값 최초 저장/두 번째 적용 mtime 유지/검토 구성 변경 및 작업 중복 때 미저장/소스 승격 거부/일반 사용자 Chrome 회귀를 확인했다.
- 준비된 간편 흐름은 번호·다단계 메뉴 없이 Enter로 시작하며, 자세한 기대값/현재값은 JSON으로 조회한다. 재부팅 대기는 재설치 안내와 구분했다.
- RC9 후보와 검토 PR 준비. 실제 Rocky에서 sudo 인증과 간편 설정 실기기 수용은 미완료다.

- PR #5가 미병합 상태이므로 후속 PR을 늘리지 않고 최종 간편 흐름으로 해당 PR의 구현·설명을 갱신한다. 관리자/간편 설정 커밋은 14cb8a9이며 기존 focused-tui 브랜치를 fast-forward한다.

## ALA-17/18 RC9 후보 완료

- 최종 구현 bacffd7. CI 41411915700: 전체 393 passed + 40 subtests (88.26초), 공개 이력 검사·Bash·고정 의존성·후보 빌드·CLI smoke 통과.
- RC9 체크섬과 manifest 54개 파일 검증, 인증 없는 공개 다운로드 byte 일치 확인.
- 기존 PR #5 제목·설명을 최종 간편 설정/관리자 진입 범위로 갱신했다: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/pull/5
- 시험판: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/releases/tag/v0.1.0-rc.9
- RC6 이상에서 sudo /usr/local/bin/ala update --prerelease로 설치 가능하다. 이후 ala 실행 시 관리자 인증과 Enter 간편 설정/s 상태 확인을 제공한다.
- 실제 Rocky의 sudo 진입·간편 설정·화면 수용은 미완료다. 실제 서버는 이번 작업에서 변경하지 않았다.

## ALA-19 시작

- 사용자 정정에 따라 기본 TUI는 유지하되 간편 설정/상태 확인 두 화면으로 제한한다. 텍스트 콘솔 제거가 목적은 아니며 선택 명령으로 둔다.
- 현재 codex/focused-tui / 32bf7f1에서 후속 작업. 이전 RC9 자산은 유지한다.
- 두 화면 TUI와 Podman 안내 검토를 분담하고 CLI·문서·통합은 주담당이 진행한다. 실제 장비 변경은 하지 않는다.

## ALA-19 Podman 판단 보강

- 공식 Docker RHEL 절차의 사전 정리 대상이므로 차단은 유지한다. 설치 감지를 사용 중으로 단정하지 않는다. 일반 계정과 root 저장소를 각각 확인하고 미사용 확인 후 제거 예정 목록을 검토하도록 안내한다.
- 실제 Engine.apply는 Docker 차단 뒤 이후 단계도 중단한다. 이전 OPERATIONS의 'Docker만 차단' 취지 설명을 정정했다. 엔진 동작은 변경하지 않는다.
- Podman 공식 rootless 문서를 근거로 docs.podman.io를 공개 문서 호스트 허용 목록에 추가한다. 기본 흐름에 없는 '이전→변경 확인' 문구를 제거했다.

## ALA-19 구현·대상 검증

- 기본 ala / ala tui를 새 SimpleApp의 간편 설정·상태 확인 두 탭으로 연결했다. 다단계 wizard와 네 메뉴는 기본 경로에서 제거했고 텍스트 콘솔은 ala console로 선택한다.
- 기존 config/submit/worker 계약으로 한 번 적용하며, 관리자 시작·단일 잠금·일반 사용자 Chrome·최초 구성 저장을 재사용한다. 상태 조회는 쓰기 작업이 없다.
- 상태 조회 실패·빈 결과 때 이전 정상 표를 제거하고, 실행 중 작업을 재연결하며, 늦은 상태 응답이 새/완료 작업을 덮어쓰지 않도록 했다.
- 새 TUI 전용 7개, TUI/CLI 관리자 통합 43개 통과. root 담당 엔진/콘솔/공개 정책 등 52개 + 3 subtests 통과.
- 합성 데이터로 80×24/120×36 설정·상태 화면을 렌더링했다. Podman 원인 다음에 실제 조치를 먼저 표시하고 상태 상세 영역을 늘렸다.
- 전체 회귀와 RC10 후보 빌드 검증 예정. 실제 Rocky 수용·Podman 사용 여부 확인은 남아 있다.

## ALA-19 RC10 후보 완료

- 구현 3a340ad. CI 41415708436: Python 3.12 전체 401 passed + 40 subtests (73.20초), 공개 이력 검사·Bash·고정 의존성·후보 빌드·CLI smoke 통과.
- RC10 체크섬과 manifest 55개 파일 검증, 인증 없는 공개 다운로드 byte 일치 확인.
- 기존 PR #5를 최종 두 화면 TUI 범위로 갱신했다: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/pull/5
- 시험판: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/releases/tag/v0.1.0-rc.10
- 실제 Rocky의 TUI·sudo·화면 수용과 Podman 사용 여부 확인·정리는 남아 있다. 실제 서버 패키지는 이번 작업에서 변경하지 않았다.

## ALA-20 시작

- 사용자가 RC10 진행 화면의 kept 원문/중복 이벤트와 낮은 가시성을 지적했다. 간편 설정에도 모든 항목의 현재 상태 표를 추가한다.
- codex/focused-tui / 820fbab 기준. 순수 진행 모델·테스트와 이벤트 의미 검토를 위임하고 주담당은 표 UI·통합·배포를 진행한다.
- completed는 최종 검증 전 이벤트이며 최종 report 우선, 하드웨어 blocked 뒤 계속 진행 예외, 설치 밖 진단 상태 보존을 공통 계약으로 반영한다.

## ALA-20 구현·대상 검증

- 설치 이벤트를 단계별 마지막 상태로 투영하는 순수 모델을 추가하고 간편 설정에 현재 상태 표·합계·선택 상세를 연결했다. 기본 11단계와 별도 진단 행을 유지한다.
- kept를 정상 유지로 번역하고 진행/완료/대기/차단에 색상·기호·문자를 적용했다. 동일 단계의 시작/완료 중복 행이 없다.
- 완료 이벤트 후 최종 검사 실패·대기 우선, 미실행과 해당 없음 구분, 조회 불가/중단 시 진행 중 오인 방지, 미매핑 진단 실패 보존을 검증했다.
- 선택·스크롤 위치를 유지하고 사용자가 상세를 읽지 않을 때 새 진행 행을 따라간다. 실행 중 불필요한 적용 버튼·안내를 숨겨 표 공간을 확보했다.
- 모델 11개, UI/기본 콘솔/관리자 통합 총 31개 통과. 기존 UI 검증은 전체 보고서 문자열 대신 해당 행/상세를 확인하도록 바꾸어 미적용·불명확 상태 검증 의도를 유지했다.
- 합성 데이터의 RC10 사진 패턴(kept 5개, Docker 시작/완료, GPU 시작)을 80×24/120×36으로 렌더링해 가시성을 확인했다. 실제 장비 변경은 하지 않았다.
- PR #5 병합 확인으로 main cbb1292 기반 codex/setup-progress에 분리했다. 전체 CI와 RC11 후보 준비가 남아 있다.

## ALA-20 RC11 후보 완료

- 구현 bc52445. CI 41420766710: Python 3.12 전체 413 passed + 40 subtests (89.18초), 공개 이력 검사·Bash·고정 의존성·후보 빌드·CLI smoke 통과.
- RC11 체크섬·manifest 56개 파일 검증, 인증 없는 공개 다운로드 byte 일치 확인.
- PR #6: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/pull/6
- 시험판: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/releases/tag/v0.1.0-rc.11
- 실제 설정 작업 중에는 도구 갱신 잠금이 적용되므로 작업 종료 후 업데이트해야 한다. 실제 Rocky 화면 수용은 미완료이며 이번 변경은 설치 엔진/실제 장비를 수정하지 않는다.

## ALA-21 시작

- codex/setup-progress / 86021a4에서 큰 기능 내부 세부 구성/현재 작업 표시를 추가한다. 이전 RC11 자산은 유지한다.
- 카탈로그/자식 상태 투영과 Shell 구조화 마커를 분담했고 출력 스트리밍·worker 부가 기록·TUI는 주담당이 진행한다.
- 실제 진행 근거가 없는 세부 항목에 진행 중/완료를 추측하지 않는 계약을 확정했다.

## ALA-21 구현·대상 검증

- 기본 환경/업데이트 정책/Bash/SSH/Docker/GPU/컨테이너/선택 도구/화면의 세부 구성 카탈로그를 실제 검사 ID와 연결했다. Enter로 자식 행을 펼치고 현재 세부 작업 열에서 실제 started 신호를 확인한다.
- Shell 구조화 마커는 opt-in이며 기존 명령 앞뒤에서만 보고한다. Host는 출력과 마커를 실시간으로 전달하고 모듈 종료 코드를 유지한다. 허용 ID/state만 worker의 additive substeps에 저장한다.
- 진행 관측 저장이 실패해도 자식 출력을 끝까지 읽은 뒤 오류로 보고해 RPM 프로세스를 중간에 죽이지 않는다. 별도 상세 신호가 없는 작업은 구성 항목과 검사 결과만 표시한다.
- 카탈로그/순수 모델 25개, 기존 Shell 모의 회귀 90개+34 subtests, 새 Shell 검사 포함 대상 35개+29 subtests 통과. 출력 스트리밍·worker·TUI·카탈로그·진행 통합 61개 통과.
- 즉시 callback/비정상 종료 코드/관측 실패 시 drain/허용 마커 필터/기존 단계 호환/Enter 펼침·접기·상태 조회 무변경을 검증했다.
- 80×24/120×36에서 기본 환경의 시간대 설정 진행을 합성 데이터로 렌더링·확인했다. 원래 설치 정책과 실제 장비는 변경하지 않았다.
- 기존 PR #6이 OPEN이므로 최종 세부 진행 범위로 이어 반영한다. RC12 전체 CI·배포 파일 검증이 남아 있다.

## ALA-21 최종 판정 보강

- 상세 신호가 없는 네이티브 단계가 대기/차단으로 끝나더라도 정상인 개별 최종 검사는 현재 정상으로 유지한다. 신호·검사가 모두 없으면 확인 불가로 표시하며 단순히 신호가 없다는 이유로 미실행을 추정하지 않는다.
- 실제로 방문하지 않은 부모 단계와 정상 유지로 건너뛴 단계의 미실행 구분은 유지했다. 네이티브 화면/하드웨어·미실행 회귀 3개를 추가했다.
- 세부 모델·스트리밍·worker·TUI·진행 통합 64개 통과. 최초 CI 41425251970의 전체 436 passed + 42 subtests는 통과했고, 이 판정 보강을 포함한 최종 CI를 다시 확인한다.

## ALA-21 RC12 후보 완료

- 최종 구현 63e7325e4f048b03f515a962fe20b042c0920066. CI 41426462906: Python 3.12 전체 439 passed + 42 subtests (95.81초), 공개 이력 검사·Bash·고정 의존성·빌드·CLI smoke 통과.
- 별도 읽기 검토에서 설치 명령/보안 조건/종료 코드 유지, 관측 오류 후 출력 소비, 구형 worker/Prepare 호환성을 확인했다.
- CI RC12 설치 묶음의 체크섬·manifest 57개 파일 해시 검증, 인증 없는 공개 다운로드와 CI 파일 byte 일치 확인. SHA-256: 26f09035d9ac1c2646e7db573f403354386b4136b412f76d0ae1a660ca63c153.
- PR #6을 최종 세부 진행 표시와 검증 결과로 갱신했다: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/pull/6
- 시험판: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/releases/tag/v0.1.0-rc.12
- 현재 설정 작업이 끝난 뒤 ala update --prerelease로 갱신하고 다시 실행한다. 이미 실행 중인 구버전 작업의 세부 신호를 새로 복원하지 않는다.
- 실제 Rocky 수용은 미완료다. 이번 작업은 실제 장비를 변경하지 않았다. 후속은 PR 검토·병합과 장비에서 세부 진행/최종 상태의 가시성 확인이다.

## ALA-22 시작

- 사용자 요청: 버튼·여백·간격 부족으로 시인성과 가시성이 낮다. codex/setup-progress / 54b1205에서 배치를 개선한다. PR #6은 OPEN이다.
- 현재 고정 최대 폭/열 폭과 영역 사이 간격 부족을 확인했다. 작은 화면에 여백만 일괄 추가하면 표·상세를 밀어내므로 화면 크기별 배치를 적용한다.
- 주담당은 배치/렌더링/통합, 분담은 읽기 검토와 접근성·크기 변경 테스트다. 설치 엔진과 실제 장비는 변경하지 않는다.

## ALA-22 구현·대상 검증

- 계정/표시 정보와 버튼을 툴바로 묶고 작업 요약·표·선택 상세를 분리했다. 영역별 제목·패딩·간격과 읽기 전용 상세 표시를 정리했다.
- 최대 내용 폭을 늘리고 실제 표 크기에 맞춰 열을 조정한다. 80×24 압축 배치, 높은 화면의 부모 행 간격을 분리하며, 크기 변경 때 선택한 항목을 보존한다.
- 렌더링에서 발견한 작은 화면 버튼 캡션 잘림과 커서 없는 상세 문서의 오래된 화면 잔상을 보완했다. 문서 데이터뿐 아니라 실제 SVG 표시 갱신을 검사한다.
- 기존 TUI/상태 모델/관리자 진입/콘솔 대상 66개 통과 후 추가 렌더 검증 포함 신규 레이아웃 18개 통과. 초기·진행·종료대기·조회오류, 3개 크기, 비겹침·가로 스크롤·긴 항목·끝행 탐색·선택 보존·버튼 캡션을 검증했다.
- 80×24/120×36/160×48의 준비·진행·종료대기·상태 화면을 합성 데이터로 렌더링했다. 별도 읽기 검토에서 기존 실행/권한/조회 계약 유지 확인. 실제 Rocky 장비는 변경하지 않았다.
- RC13 전체 CI·배포 파일 검증이 남아 있다. 이전 RC12 파일은 유지한다.

## ALA-22 RC13 후보 완료

- 최종 구현 2f9ef51e8e0da9be08e23b706977ef2eb2f6de5e. CI 41431039704: Python 3.12 전체 457 passed + 42 subtests (105.82초), 공개 이력·Bash·고정 의존성·빌드·CLI smoke 통과.
- CI 설치 묶음의 체크섬·manifest 57개 파일 해시를 검증하고 인증 없는 공개 다운로드와 byte 일치를 확인했다. SHA-256: fd53774f7f7127532272de7d35add5a961fffa723d13443fa36e02c05716e03f.
- 기존 PR #6을 최종 반응형 배치·진행 표시 범위로 갱신했다: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/pull/6
- 시험판: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/releases/tag/v0.1.0-rc.13
- 설정 작업 종료 후 ala update --prerelease로 갱신하고 다시 실행한다. 실제 Rocky 터미널의 가독성·화면 수용은 남아 있으며 이번 작업은 실제 장비를 변경하지 않았다.
