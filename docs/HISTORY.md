# 개발 이력 복구

공개 전 main의 개발 커밋 21개를 정리해 현재 공개 main의 조상으로 연결했다. 기존 공개 커밋도 유지하며 강제 덮어쓰기는 하지 않았다.

- 작성자 이름, 작성·커밋 시각, 커밋 메시지, 병합 관계를 보존했다. 빈 커밋도 제거하지 않았다.
- 실제 접속 정보·호스트 키 지문·고객명·내부 원본 경로를 문서에서 제거하거나 일반화했다.
- 개인 이메일은 같은 GitHub 계정의 noreply 주소로 변경했다.
- 내용과 부모 해시가 바뀌어 기존 커밋 해시는 변경됐다. 아래 표로 대응 관계를 확인할 수 있다.
- 과거 구현 코드·테스트는 변경하지 않았다. 현재 main 파일 트리와 이미 게시한 RC4 파일·태그도 유지했다.
- 과거 문서의 PR·CI·RC1~3 링크는 당시 기록이다. 삭제된 GitHub PR 페이지·댓글·승인 상태는 복원하지 않았다.

## 검증

21개 커밋의 메타데이터·부모 관계와 고유 파일 내용 134개를 검사했다. 문서 3개 외 구현 파일은 원본과 동일하다. 금지 패턴 및 Gitleaks 검사에서 탐지되지 않았다. 실제 Rocky 장비 수용은 별도다.

## 커밋 대응표

| 이전 커밋 | 공개 이력의 커밋 |
|---|---|
| `0a1c87f3baf1a6446a8837aa4d01f261f48a80f7` | [8fc0a2d](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/8fc0a2dbec16feb43ad0eddf528533525873b9f5) |
| `2c8fa6ff4b35fa63c75240ccd744eae1d6f0458f` | [cf6dfdc](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/cf6dfdc74f1a8c88f374c5f55c51b6866f2a67bb) |
| `41c870f603a7bab249a7a827893630ffc5a97f36` | [9554b11](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/9554b1151536980c3eac439116b2b65825c9bdfd) |
| `4bc033b762e061555abad30b9e17ef0c0744e475` | [b4c99bd](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/b4c99bd408354fc63a041c12a4fa1a427449ab2f) |
| `4d3a62a74a0cc6d5c33bc248fe223fbb4a4788fe` | [59d905c](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/59d905c85116ec3c218786abaf5ed5bcb9eabe9a) |
| `52f453a020944721724083fd81013336648db00b` | [49f7330](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/49f7330e3e0be122bc1a9b9a6312a3d0ac7ab985) |
| `659d97e222f12ad16b279c4b6af2a8c44a1291a3` | [4ab255c](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/4ab255c9b65d76b174ddf43f67f123c65d217169) |
| `668278019f011c26f2e20a3d038edc75c126380e` | [59d700e](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/59d700e97ad677ea5de0533c0fd9fa31a93cfcdb) |
| `773ebb540f64268f392f88c3fb5eda1ee0ee9dae` | [80dd83a](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/80dd83ae86959fbe6f47ae3ba7ce4a31636a7afa) |
| `80998a8626faebd60d0061b47bb3578c4b644037` | [aaf0503](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/aaf0503099bbee3e2825ee293564f698d5ec6b36) |
| `81a5f347ba0732b1393e865afcf0a0ec7fa648bc` | [a459add](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/a459add4e301fbc8f74960e544834dc43184c5c3) |
| `903103125f11cb1734904bac0ea601dc7fde207d` | [8d5bab6](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/8d5bab65bbb866282e8b7486197b2c5fe6b95f45) |
| `92445986d38d514944e80b7e2cc58ac62682f990` | [38facf0](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/38facf08e8dcdd73819c37870278167339c7ef5f) |
| `9aa3df027fce148c82172595e440f85b11434993` | [90a9b16](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/90a9b168713dead40280581d02056cf2f3d9235b) |
| `ce2065cd43a5ef833e39abfc5410080ce525001c` | [2d605e6](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/2d605e68dea2e29d513c9713a9d76ec81e8d70de) |
| `cf81793d75b47820c1984a35eafb5da323169f46` | [e4594b0](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/e4594b0fc00ad4e9358b456b631c5bbcae124766) |
| `d4c5bf6d4f87faeaef1b5d5e87d109d338a5215c` | [85962dd](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/85962dd97fe77a17b7b8e3ab4f3d9171b94e08e4) |
| `d576bfb8e16119e58da67e2ade9ef5382d77a528` | [d0033fc](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/d0033fc615b2f82f52e886ab189b9a1dc241ec2c) |
| `d60d2e75c7419908342340698b70fa9a6d112afa` | [56179ba](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/56179bad45e9dd6c44b9749dc424a40a889c6416) |
| `efe9bed642eeb0c29feaa55d53480fbafe90ff00` | [9d31b30](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/9d31b3053a4a7745606d57e8af5ec62e685a1eed) |
| `f9a85466c022a11e38848e50a1b50167dc15c527` | [2d35143](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/2d35143caea2a2116d7a566aa3ff67de3f00897f) |

## 공개 데이터 정책 강화

후속 정리에서 테스트의 비루프백 IP 리터럴을 제거해 커밋 해시가 다시 변경됐다. 위 표는 현재 이력 기준이다. 당시 합성 주소는 실제 장비 주소가 아니었지만 엄격한 공개 규칙에 맞췄다. 원래 작성자·날짜·메시지·병합 관계는 유지한다. RC4 설치 바이너리는 변경하지 않고 소스 태그만 정리된 이력으로 연결한다. 과거 공개 객체/PR 캐시의 완전 삭제를 의미하지 않는다.
