#!/bin/bash
# 스튜디오 검사(ubion-dev/tool-hooks): 옛 tests.yml의 public-data · tests 잡(Chrome smoke 빼고).
# 실제 Linux Chrome smoke는 공식 Chrome이 amd64뿐이고 unshare 권한이 필요해 .github/workflows/tests.yml에 남김.
# 서명 릴리스(release.yml)는 Sigstore 서명 신원이 Actions 워크플로라 그대로 Actions.
set -euo pipefail
case "$HOOK_EVENT:$HOOK_REF" in
  pull_request:*|push:refs/heads/main|push:refs/heads/codex/*) ;;
  *) echo "main · codex/** push와 PR만 검사"; exit 0 ;;
esac
echo "== 공개 이력 검사(값은 찍지 않음)"
python scripts/check-public.py --history
echo "== 잠근 개발 의존성"
python -m pip install --user -q --require-hashes -r requirements-dev.lock
echo "== Bash 문법"
bash -n install.sh
find scripts/rocky-linux -name '*.sh' -exec bash -n {} \;
echo "== GNOME 확장 모듈 문법"
node --input-type=module --check < assets/gnome-extension/ala-display@example.com/extension.js
echo "== 회귀 · 격리 CLI/TUI/설치 테스트"
python -m pytest -q --tb=short --show-capture=no --ignore=tests/test_linux_chrome_smoke.py
echo "== 오프라인 설치 후보 묶음 + 진입점 smoke"
python scripts/build-release.py
V=$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')
mkdir -p build/smoke
tar -xzf "dist/ala-$V.tar.gz" -C build/smoke
python -I "build/smoke/ala-$V/entry.py" --version
python -I "build/smoke/ala-$V/entry.py" --help > /dev/null
ls -l dist/
