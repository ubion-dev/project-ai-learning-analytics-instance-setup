# 분리 출처

- 원본: [비공개 원본 저장소]
- 기준: `4a7128c6e7bd188e523faaf316a28eac0557008f` (PR #10 merge)
- 추출: `git archive <commit> scripts/rocky-linux`
- 원본 전체 Git 이력·고객 자료·카메라 수집 자료는 가져오지 않았다.
- 분리 직후 `python3 -m unittest discover -s scripts/rocky-linux/tests -v`: 85 tests, 72.021초, OK.
- 테스트는 호스트 명령을 모의 실행한다. 실제 장비 설치·GPU·화면·재부팅 수용 결과가 아니다.

기존 Bash 설치 동작은 내부 실행 모듈로 유지한다. 새 진입점은 `ala`이며 구성과 검사 기준은 새 공통 엔진에서 관리한다.
원본 문서의 수동 명령은 분리 전 참고 자료다. 신규 사용자는 최상위 README와 ala 운영 문서를 따른다.
