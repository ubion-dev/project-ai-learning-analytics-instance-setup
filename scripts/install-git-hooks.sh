#!/bin/sh
set -eu
cd "$(git rev-parse --show-toplevel)"
command -v python3 >/dev/null
command -v gitleaks >/dev/null || { echo 'Install Gitleaks from https://github.com/gitleaks/gitleaks first.' >&2; exit 1; }
existing=$(git config --get core.hooksPath || true)
if [ -n "$existing" ] && [ "$existing" != '.githooks' ]; then
  echo 'Existing hooksPath preserved. Integrate the public-data checks into your hooks first.' >&2
  exit 1
fi
git config --local core.hooksPath .githooks
printf '%s\n' 'Public-data pre-commit/pre-push hooks enabled for this clone.'
