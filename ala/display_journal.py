"""Root-only, UID-scoped fallback for display and login-helper journal messages."""
from __future__ import annotations

from collections import deque
import json
import os

CHROME_UNIT = 'ala-chrome.service'
HELPER_PREFIX = '[ala-autostart]'
MAX_ENTRIES = 100
MAX_QUERY_CHARS = 512 * 1024
MAX_LOG_CHARS = 65536
MAX_LOG_LINES = 200
QUERY_TIMEOUT = 10


class _JournalError(ValueError):
    pass


def _messages(response, uid, *, helper):
    if not isinstance(response.stdout, str) or not isinstance(response.stderr, str):
        raise _JournalError('로그 조회 응답 형식을 확인하지 못했습니다.')
    output = response.stdout.strip()
    # With --grep journalctl exits 1 for no matches. Its JSON output also sets
    # arg_quiet, suppressing the empty marker. Only a marker or exactly empty
    # stdout without any stderr is a no-match result for this fixed grep query.
    empty = output == '-- No entries --'
    no_match = (helper and response.returncode == 1 and not response.stderr and
                (empty or response.stdout == ''))
    if response.returncode != 0 and not no_match:
        reason = ('journalctl 명령을 사용할 수 없습니다.' if response.returncode == 127 else
                  '로그 조회 제한 시간이 초과되었습니다.' if response.returncode == 124 else
                  '대상 로그 조회에 실패했습니다. 권한과 journal 상태를 확인하세요.')
        raise _JournalError(reason)
    if response.stderr:
        raise _JournalError('로그 조회 진단이 있어 결과의 완전성을 확인하지 못했습니다.')
    if empty or not output:
        return []
    if len(output) > MAX_QUERY_CHARS:
        raise _JournalError('로그 조회 결과가 크기 제한을 초과했습니다.')
    records = output.splitlines()
    if len(records) > MAX_ENTRIES:
        raise _JournalError('로그 조회 결과가 항목 제한을 초과했습니다.')
    messages = []
    for line in records:
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            raise _JournalError('로그 조회 응답 형식을 확인하지 못했습니다.') from None
        if not isinstance(record, dict) or record.get('_UID') != str(uid):
            raise _JournalError('로그 기록의 대상 사용자 범위를 확인하지 못했습니다.')
        if not helper and not (record.get('_SYSTEMD_USER_UNIT') == CHROME_UNIT or
                               record.get('USER_UNIT') == CHROME_UNIT):
            raise _JournalError('로그 기록의 대상 서비스 범위를 확인하지 못했습니다.')
        message = record.get('MESSAGE')
        if not isinstance(message, str):
            # journalctl JSON uses null for fields over 4096 bytes and arrays
            # for binary or repeated fields. Never reconstruct arbitrary bytes.
            messages.append('[크기·형식 제한으로 로그 항목 숨김]')
        elif helper:
            # Re-check each physical line: a matching multiline MESSAGE must
            # not carry unrelated trailing application text into this view.
            matching = [part for part in message.splitlines() if part.startswith(HELPER_PREFIX)]
            if not matching:
                raise _JournalError('로그인 진입점 로그의 접두사를 확인하지 못했습니다.')
            messages.extend(matching)
        else:
            messages.extend(message.splitlines())
    return messages


def _bounded_log(groups):
    lines = deque()
    size = 0
    for label, messages in groups:
        if not messages:
            continue
        for line in [label, *messages]:
            if len(line) + 1 > MAX_LOG_CHARS:
                line = '[크기 제한으로 로그 행 숨김]'
            lines.append(line)
            size += len(line) + 1
            while len(lines) > MAX_LOG_LINES or size > MAX_LOG_CHARS:
                size -= len(lines.popleft()) + 1
    return '\n'.join(lines)


def read_display_journal(host, uid):
    """Read two fixed current-boot scopes, never change a service or journal.

    _SYSTEMD_USER_UNIT identifies the emitting process's unit; USER_UNIT is
    used by the user manager when logging about a unit. Both OR branches carry
    the same trusted _UID match. See systemd.journal-fields(7), systemd 257.
    No --user is used: volatile logs may exist only in the system journal.
    """
    if os.geteuid() != 0:
        return {'exit_code': 1, 'state': 'unknown', 'log': '',
                'reason': '시스템 저널 보완 조회에 관리자 권한이 필요합니다.'}
    if type(uid) is not int or not 0 < uid < 2**32 - 1:
        return {'exit_code': 1, 'state': 'error', 'log': '',
                'reason': '로그 조회에는 유효한 일반 사용자 UID가 필요합니다.'}
    base = ['journalctl', '--boot', '--no-pager', '--output=json',
            '--output-fields=MESSAGE,_UID,_SYSTEMD_USER_UNIT,USER_UNIT',
            '--lines=' + str(MAX_ENTRIES)]
    target = '_UID=' + str(uid)
    queries = [
        (False, base + [target, '_SYSTEMD_USER_UNIT=' + CHROME_UNIT,
                        '+', target, 'USER_UNIT=' + CHROME_UNIT]),
        (True, base + [r'--grep=^\[ala-autostart\]', target]),
    ]
    environment = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C',
                   'SYSTEMD_COLORS': '0', 'SYSTEMD_PAGER': 'cat'}
    groups, errors = [], []
    for helper, argv in queries:
        try:
            response = host.run(argv, timeout=QUERY_TIMEOUT, env=environment)
            messages = _messages(response, uid, helper=helper)
            groups.append(('[로그인 자동 시작]' if helper else '[Chrome 서비스]', messages))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            reason = str(exc) if isinstance(exc, _JournalError) else '대상 로그 조회를 실행하지 못했습니다.'
            errors.append(('로그인 진입점' if helper else 'Chrome 서비스') + ': ' + reason)
    if errors:
        # Do not present one successful query as the complete log when its
        # companion failed. Raw stderr and exception messages are not returned.
        return {'exit_code': 1, 'state': 'unknown', 'log': '', 'reason': '\n'.join(errors)}
    log = _bounded_log(groups)
    return {'exit_code': 0, 'state': 'ok', 'log': log,
            'reason': '대상 사용자의 Chrome·자동 시작 로그를 조회했습니다.' if log else
                      '현재 부팅에 대상 Chrome·자동 시작 로그가 없습니다.'}
