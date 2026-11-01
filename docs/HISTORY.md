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
| `0a1c87f3baf1a6446a8837aa4d01f261f48a80f7` | [2959b9f](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/2959b9fc25d1e83cc61c0dc60f9623e8e1a87681) |
| `2c8fa6ff4b35fa63c75240ccd744eae1d6f0458f` | [ce49f40](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/ce49f405015ed424152d91060dfeae4e70e17f9e) |
| `41c870f603a7bab249a7a827893630ffc5a97f36` | [a755b63](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/a755b6324dd88bc7e99b5661f3d8705ac8195487) |
| `4bc033b762e061555abad30b9e17ef0c0744e475` | [459ed26](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/459ed264496e8f620c2b6ec16a94537fbfb2d774) |
| `4d3a62a74a0cc6d5c33bc248fe223fbb4a4788fe` | [ea680d0](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/ea680d04e2ae7c32be8f05db2c21121ef6db30dc) |
| `52f453a020944721724083fd81013336648db00b` | [4eeaf10](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/4eeaf10c6b8804a1505ef32389b3bc527adcbe71) |
| `659d97e222f12ad16b279c4b6af2a8c44a1291a3` | [de0d317](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/de0d3176bc784bf0060da1471b8b301de540e097) |
| `668278019f011c26f2e20a3d038edc75c126380e` | [7805759](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/78057598e892941b1304c4354bbe298af7d0251c) |
| `773ebb540f64268f392f88c3fb5eda1ee0ee9dae` | [6ca7361](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/6ca7361b4f73120499a5c8653fa32afa5cd7d5bb) |
| `80998a8626faebd60d0061b47bb3578c4b644037` | [9c24c43](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/9c24c439bdb4f8ca5d3eca358aa3e0b33a6a80a3) |
| `81a5f347ba0732b1393e865afcf0a0ec7fa648bc` | [1e879c4](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/1e879c42059ee52a3151f9ba5814f67e6a64e54d) |
| `903103125f11cb1734904bac0ea601dc7fde207d` | [6552026](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/65520263d946a34d53ef6e111c6099137b3e8fcf) |
| `92445986d38d514944e80b7e2cc58ac62682f990` | [0d4a33f](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/0d4a33fda8632a319515b35f053deb45d14240c8) |
| `9aa3df027fce148c82172595e440f85b11434993` | [a69d458](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/a69d4580a59d77aca0974d50b6988e83e82ea381) |
| `ce2065cd43a5ef833e39abfc5410080ce525001c` | [b206cfe](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/b206cfe6d304af28983f3eee6f24e142f166c16d) |
| `cf81793d75b47820c1984a35eafb5da323169f46` | [e4594b0](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/e4594b0fc00ad4e9358b456b631c5bbcae124766) |
| `d4c5bf6d4f87faeaef1b5d5e87d109d338a5215c` | [57fa46c](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/57fa46c3109235e1df00b3b332c13ede71699b21) |
| `d576bfb8e16119e58da67e2ade9ef5382d77a528` | [e6b7a59](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/e6b7a5981bfdaffb74436bc0f634519499d76b94) |
| `d60d2e75c7419908342340698b70fa9a6d112afa` | [ef83893](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/ef838937e75f936f5756d05142f65d0641705da7) |
| `efe9bed642eeb0c29feaa55d53480fbafe90ff00` | [1449db9](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/1449db9f88563b2e8d8a63e54b1932f024dca4de) |
| `f9a85466c022a11e38848e50a1b50167dc15c527` | [120e309](https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/commit/120e309305a95229a9720906ed07fa15c33bafa8) |
