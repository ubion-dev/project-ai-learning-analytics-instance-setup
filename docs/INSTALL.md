# 배포 빌드

Python 3.12 이상에서 프로젝트 루트 기준으로 실행합니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.lock
.venv/bin/python scripts/build-release.py
```

`dist/`에 설치용 압축 파일과 SHA-256 검증 파일을 생성합니다.
공개 릴리스의 직접 다운로드·설치 안내는 준비 중입니다.
