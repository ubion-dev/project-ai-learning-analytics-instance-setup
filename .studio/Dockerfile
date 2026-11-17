# ala 스튜디오 검사 이미지: Python 3.12 + node(확장 문법 검사) + 고정 Gitleaks 8.30.1(arm64, 공식 체크섬 확인)
FROM python:3.12-bookworm
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
RUN curl -fsSL --proto '=https' -o /tmp/gitleaks.tar.gz \
      https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_arm64.tar.gz \
 && echo "e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080  /tmp/gitleaks.tar.gz" | sha256sum --check \
 && tar -xzf /tmp/gitleaks.tar.gz -C /usr/local/bin gitleaks && rm /tmp/gitleaks.tar.gz \
 && git config --system --add safe.directory '*'
RUN useradd -m user
USER user
ENV PATH=/home/user/.local/bin:$PATH
