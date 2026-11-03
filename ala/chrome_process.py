"""Conservative observations of argv and Chromium's Linux process title.

Chromium joins arguments with literal spaces, without quoting them. A process
title therefore cannot generally reconstruct argv. Only the managed stable
Chrome executable and a restricted, whitespace-free token form are supported;
this observation never substitutes for UID, lineage, boot or start-time checks.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import re


MAX_BYTES = 1024 * 1024
MAX_ARGUMENTS = 256
CHROME_EXECUTABLE = '/opt/google/chrome/chrome'
SWITCH = re.compile(r'--[A-Za-z0-9][A-Za-z0-9-]*(?:=[^\s]*)?\Z')


class AmbiguousCommandLine(ValueError):
    """A value-free reason why the process observation cannot be verified."""


@dataclass(frozen=True)
class Observation:
    arguments: tuple[str, ...]
    format: str


def _decode(value):
    try:
        return value.decode('utf-8')
    except UnicodeError:
        raise AmbiguousCommandLine('Chrome 인자 관측의 문자 인코딩을 확인할 수 없습니다.') from None


def _unsafe_title_token(value):
    return (not isinstance(value, str) or not value
            or any(character.isspace() or ord(character) < 32 or ord(character) == 127
                   or character in {'"', "'", '\\'} for character in value))


def observe_chrome_cmdline(raw: bytes, *, executable=None, expected_arguments=()):
    """Return tokens, preserving switch order/duplicates for the caller to check.

    `expected_arguments` only constrains ambiguous title boundaries and allowed
    positional values. It does not authorize missing, wrong or duplicate flags.
    The existing per-switch and forbidden-flag checks remain the caller's job.
    """
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BYTES:
        raise AmbiguousCommandLine('Chrome 인자 관측이 비어 있거나 크기 제한을 초과했습니다.')
    chunks = raw.rstrip(b'\0').split(b'\0')
    if len(chunks) > MAX_ARGUMENTS:
        raise AmbiguousCommandLine('Chrome 인자 관측의 원소 수 제한을 초과했습니다.')
    if len(chunks) > 1:
        if chunks[0].startswith((CHROME_EXECUTABLE + ' ').encode()) and b' --' in chunks[0]:
            raise AmbiguousCommandLine('Linux Chrome 제목 뒤에 다른 NUL 원소가 남아 인자 경계를 확인할 수 없습니다.')
        arguments = tuple(_decode(chunk) for chunk in chunks)
        return Observation(arguments, 'nul_argv')

    title = _decode(chunks[0])
    if executable is None:
        raise AmbiguousCommandLine('실제 Chrome 실행 파일을 조회하지 못해 Linux 제목 형식을 확인할 수 없습니다.')
    try:
        executable = os.fspath(executable)
    except TypeError:
        raise AmbiguousCommandLine('실제 Chrome 실행 파일 관측 형식이 올바르지 않습니다.') from None
    if not isinstance(executable, str):
        raise AmbiguousCommandLine('실제 Chrome 실행 파일 관측 형식이 올바르지 않습니다.')
    if executable.endswith(' (deleted)'):
        executable = executable[:-10]
    if executable != CHROME_EXECUTABLE or not title.startswith(executable + ' '):
        raise AmbiguousCommandLine('지원하는 Chrome 실행 파일과 Linux 제목 접두사가 일치하지 않습니다.')
    expected = tuple(expected_arguments)
    if any(_unsafe_title_token(arg) for arg in expected):
        raise AmbiguousCommandLine('기대 인자에 공백·따옴표 등의 경계 모호함이 있어 Linux 제목을 복원하지 않습니다.')
    arguments = (executable, *title[len(executable) + 1:].split(' '))
    if len(arguments) > MAX_ARGUMENTS or any(_unsafe_title_token(arg) for arg in arguments):
        raise AmbiguousCommandLine('Linux Chrome 제목의 공백·따옴표·인자 경계를 확실히 구분할 수 없습니다.')
    positionals = {arg for arg in expected if not arg.startswith('--')}
    for arg in arguments[1:]:
        if arg not in positionals and not SWITCH.fullmatch(arg):
            raise AmbiguousCommandLine('Linux Chrome 제목에 확인하지 못한 위치 인자가 있어 복원하지 않습니다.')
    return Observation(tuple(arguments), 'linux_title')


def profile_evidence(raw: bytes, profile) -> bool:
    """Detect the requested profile only, without exposing any process values.

    This is a conservative busy-profile guard, not argv reconstruction. In a
    flattened title an exact profile followed by a space may also be a longer
    ambiguous value; that case must block, rather than open the profile again.
    """
    if not isinstance(raw, bytes):
        return False
    requested = os.fspath(profile).encode('utf-8')
    needle = b'--user-data-dir=' + requested
    chunks = raw.rstrip(b'\0').split(b'\0')
    if len(chunks) > 1:
        if needle in chunks:
            return True
        if any(chunks[i] == b'--user-data-dir' and chunks[i + 1] == requested
               for i in range(len(chunks) - 1)):
            return True
        # A title followed by residual NUL data is unsupported, but its first
        # chunk may still evidence this exact profile. Preserve the busy guard.
    title = chunks[0]
    tokens = (needle, b'--user-data-dir ' + requested,
              b'--user-data-dir="' + requested + b'"', b"--user-data-dir='" + requested + b"'",
              b'"' + needle + b'"', b"'" + needle + b"'")
    for token in tokens:
        at = title.find(b' ' + token)
        while at >= 0:
            end = at + len(token) + 1
            if end == len(title) or title[end:end + 1] in {b' ', b'\t', b'\n', b'\r'}:
                return True
            at = title.find(b' ' + token, at + 1)
    return False
