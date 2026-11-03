"""UID-scoped journal reads use synthetic entries and never inspect a real host."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from ala import display, display_journal
from ala.config import Config
from ala.system import Command, Paths

UID = 12345


class JournalHost:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append((list(argv), deepcopy(kwargs)))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def record(message, **fields):
    return json.dumps({'_UID': str(UID), 'MESSAGE': message, **fields})


def runtime(message='synthetic display event', **fields):
    return record(message, _SYSTEMD_USER_UNIT='ala-chrome.service', **fields)


@pytest.fixture(autouse=True)
def administrator(monkeypatch):
    monkeypatch.setattr(display_journal.os, 'geteuid', lambda: 0)


def test_runtime_process_manager_and_login_helper_messages_are_read_with_two_fixed_scopes():
    host = JournalHost(Command(0, runtime() + '\n' + record('synthetic service started', USER_UNIT='ala-chrome.service')),
                       Command(0, record('[ala-autostart] phase=request outcome=accepted')))
    value = display_journal.read_display_journal(host, UID)
    assert value['exit_code'] == 0 and value['state'] == 'ok'
    assert 'synthetic display event' in value['log'] and 'synthetic service started' in value['log']
    assert '[ala-autostart] phase=request outcome=accepted' in value['log']
    assert len(host.calls) == 2
    service, helper = [args for args, _ in host.calls]
    split = service.index('+')
    assert service[split - 2:split] == ['_UID=12345', '_SYSTEMD_USER_UNIT=ala-chrome.service']
    assert service[split + 1:] == ['_UID=12345', 'USER_UNIT=ala-chrome.service']
    assert helper[-2:] == [r'--grep=^\[ala-autostart\]', '_UID=12345']
    for args, options in host.calls:
        assert args[0] == 'journalctl'
        assert '--boot' in args and '--no-pager' in args and '--lines=100' in args
        assert '--output=json' in args
        assert '--output-fields=MESSAGE,_UID,_SYSTEMD_USER_UNIT,USER_UNIT' in args
        assert '--user' not in args and '--all' not in args and '--merge' not in args
        assert options['timeout'] == 10 and options['env']['LC_ALL'] == 'C'
        assert set(options['env']) == {'PATH', 'LC_ALL', 'SYSTEMD_COLORS', 'SYSTEMD_PAGER'}


@pytest.mark.parametrize('uid', (0, -1, True, 1.0, '12345', '12345 + _UID=0', 2**32 - 1, None))
def test_invalid_or_root_uid_cannot_expand_the_journal_scope(uid):
    host = JournalHost()
    result = display_journal.read_display_journal(host, uid)
    assert result['exit_code'] == 1 and result['log'] == ''
    assert not host.calls


def test_nonadministrator_cannot_read_the_root_fallback(monkeypatch):
    monkeypatch.setattr(display_journal.os, 'geteuid', lambda: UID)
    host = JournalHost()
    result = display_journal.read_display_journal(host, UID)
    assert result['state'] == 'unknown' and result['exit_code'] == 1 and result['log'] == ''
    assert not host.calls


@pytest.mark.parametrize('bad_record', (
    {'_UID': '12346', 'MESSAGE': 'another user must stay hidden', '_SYSTEMD_USER_UNIT': 'ala-chrome.service'},
    {'_UID': '0', 'MESSAGE': 'administrator data must stay hidden', 'USER_UNIT': 'ala-chrome.service'},
    {'_UID': str(UID), 'MESSAGE': 'another service must stay hidden', '_SYSTEMD_USER_UNIT': 'other.service'},
    {'_UID': [str(UID), '0'], 'MESSAGE': 'ambiguous owner must stay hidden', 'USER_UNIT': 'ala-chrome.service'},
))
def test_out_of_scope_records_are_never_returned_as_a_successful_empty_or_partial_log(bad_record):
    host = JournalHost(Command(0, runtime() + '\n' + json.dumps(bad_record)), Command(0, '-- No entries --\n'))
    value = display_journal.read_display_journal(host, UID)
    assert value['exit_code'] == 1 and value['log'] == ''
    assert 'stay hidden' not in str(value) and 'synthetic display event' not in str(value)
    assert len(host.calls) == 2


def test_login_helper_filter_cannot_return_unrelated_multiline_application_messages():
    message = '[ala-autostart] phase=connect outcome=failed\nunrelated application text\n[ala-autostart] phase=return outcome=failed'
    host = JournalHost(Command(0, ''), Command(0, record(message)))
    result = display_journal.read_display_journal(host, UID)
    assert result['exit_code'] == 0
    assert '[ala-autostart] phase=connect outcome=failed' in result['log']
    assert '[ala-autostart] phase=return outcome=failed' in result['log']
    assert 'unrelated application' not in result['log']


def test_helper_without_required_prefix_is_not_returned():
    result = display_journal.read_display_journal(JournalHost(Command(0, ''), Command(0, record('unrelated application'))), UID)
    assert result['exit_code'] == 1 and result['log'] == ''
    assert 'unrelated application' not in str(result)


@pytest.mark.parametrize('service_output', ('', '-- No entries --\n'))
@pytest.mark.parametrize('helper_output', ('', '-- No entries --\n'))
def test_no_entries_is_normal_empty_including_json_grep_no_match_exit_one(service_output, helper_output):
    # v257 OUTPUT_JSON sets arg_quiet while no-match grep still exits 1.
    # The old marker-only expectation missed that normal empty stdout case.
    # https://github.com/systemd/systemd/blob/v257/src/journal/journalctl.c#L449-L459
    # https://github.com/systemd/systemd/blob/v257/src/journal/journalctl-show.c#L421-L432
    host = JournalHost(Command(0, service_output), Command(1, helper_output))
    value = display_journal.read_display_journal(host, UID)
    assert value['exit_code'] == 0 and value['state'] == 'ok' and value['log'] == ''
    assert '로그가 없습니다' in value['reason']


@pytest.mark.parametrize('failure', (
    Command(127, '', 'private executable path'),
    Command(124, '', 'private timeout detail'),
    Command(1, '-- No entries --\n', 'Permission denied: private journal path'),
    Command(1, '{"MESSAGE":', ''),
    Command(1, ' \n', ''),
    Command(1, '', ' '),
    Command(1, '', 'Permission denied: private journal path'),
    Command(2, '', ''),
    Command(0, '', 'Some journal files were not opened due to insufficient permissions.'),
    PermissionError('private host detail'),
))
def test_missing_command_timeout_permission_or_partial_failure_never_exposes_partial_logs(failure):
    host = JournalHost(Command(0, runtime()), failure)
    value = display_journal.read_display_journal(host, UID)
    assert value['exit_code'] == 1 and value['state'] == 'unknown' and value['log'] == ''
    assert '로그인 진입점' in value['reason']
    assert 'private' not in str(value) and 'synthetic display event' not in str(value)


def test_failed_runtime_query_cannot_be_hidden_by_successful_login_helper_query():
    host = JournalHost(Command(1, runtime(), 'synthetic denied'),
                       Command(0, record('[ala-autostart] phase=request outcome=accepted')))
    result = display_journal.read_display_journal(host, UID)
    assert result['exit_code'] == 1 and result['log'] == ''
    assert 'Chrome 서비스' in result['reason'] and len(host.calls) == 2
    assert 'phase=request' not in str(result)


def test_runtime_query_without_grep_cannot_treat_exit_one_empty_as_normal_no_match():
    host = JournalHost(Command(1, '', ''), Command(1, '', ''))
    result = display_journal.read_display_journal(host, UID)
    assert result['exit_code'] == 1 and result['state'] == 'unknown' and result['log'] == ''
    assert 'Chrome 서비스' in result['reason']
    assert '로그인 진입점' not in result['reason']


@pytest.mark.parametrize('invalid', ('not json', '{}', '[]', '{"_UID":'))
def test_invalid_json_is_failure_not_empty_log(invalid):
    result = display_journal.read_display_journal(JournalHost(Command(0, invalid), Command(0, '')), UID)
    assert result['exit_code'] == 1 and result['log'] == ''


def test_query_result_limits_fail_closed_before_returning_partial_entries():
    for output in ('x' * (display_journal.MAX_QUERY_CHARS + 1), '\n'.join(runtime() for _ in range(101))):
        result = display_journal.read_display_journal(JournalHost(Command(0, output), Command(0, '')), UID)
        assert result['exit_code'] == 1 and result['log'] == ''
        assert '제한' in result['reason']


def test_combined_log_has_bounded_lines_and_characters_without_slicing_authentication_keys():
    messages = [f'complete synthetic line {index} ' + 'x' * 900 for index in range(100)]
    service = '\n'.join(runtime(message) for message in messages)
    helper = record('\n'.join('[ala-autostart] phase=connect outcome=accepted' for _ in range(180)))
    result = display_journal.read_display_journal(JournalHost(Command(0, service), Command(0, helper)), UID)
    assert result['exit_code'] == 0
    assert len(result['log']) <= 65536 and len(result['log'].splitlines()) <= 200
    expected = set(messages) | {'[ala-autostart] phase=connect outcome=accepted', '[Chrome 서비스]', '[로그인 자동 시작]'}
    assert set(result['log'].splitlines()) <= expected


def test_large_or_binary_message_fields_are_hidden_without_reconstructing_payload():
    result = display_journal.read_display_journal(JournalHost(Command(0, runtime(None) + '\n' + runtime([65, 66, 67])), Command(0, '')), UID)
    assert result['exit_code'] == 0
    assert '크기·형식 제한으로 로그 항목 숨김' in result['log']
    assert 'ABC' not in result['log'] and '[65' not in result['log']


def test_chrome_logs_preserves_existing_nonempty_user_journal_without_root_fallback(monkeypatch):
    user_result = {'exit_code': 0, 'log': 'existing synthetic user log', 'reason': ''}
    monkeypatch.setattr(display, '_account', lambda *_: (UID, UID, Path('/home/user')))
    monkeypatch.setattr(display, '_user_call', lambda *_: user_result)
    monkeypatch.setattr(display_journal, 'read_display_journal', lambda *_: pytest.fail('unexpected fallback'))
    assert display.chrome_action('logs', Config(user='user'), host=JournalHost(), paths=Paths()) == user_result


@pytest.mark.parametrize('user_result', (
    {'exit_code': 0, 'log': ''},
    {'exit_code': 0, 'log': '-- No entries --\n'},
    {'exit_code': 1, 'log': '', 'reason': 'synthetic user journal absent'},
))
def test_chrome_logs_missing_user_journal_calls_the_same_account_root_fallback(monkeypatch, user_result):
    calls = []
    host = JournalHost()
    fallback = {'exit_code': 0, 'state': 'ok', 'log': '[ala-autostart] phase=request outcome=accepted', 'reason': ''}
    monkeypatch.setattr(display, '_account', lambda *_: (UID, UID, Path('/home/user')))
    monkeypatch.setattr(display, '_user_call', lambda *_: user_result)
    monkeypatch.setattr(display_journal, 'read_display_journal', lambda actual_host, uid: calls.append((actual_host, uid)) or fallback)
    result = display.chrome_action('logs', Config(user='user'), host=host, paths=Paths())
    assert calls == [(host, UID)]
    assert result == fallback


def test_named_target_journal_query_and_each_record_use_exact_scope():
    unit='ala-chrome@'+'display-2.service'
    host=JournalHost(Command(0,record('named event',_SYSTEMD_USER_UNIT=unit)),Command(0,''))
    result=display_journal.read_display_journal(host,UID,unit=unit)
    assert result['exit_code']==0 and 'named event' in result['log']
    args=host.calls[0][0]
    assert '_SYSTEMD_USER_UNIT='+unit in args and 'USER_UNIT='+unit in args
    assert not any('*' in arg for arg in args)


@pytest.mark.parametrize('unit',[None,'','*','ala-chrome@*.service','other.service','ala-chrome@../escape.service',
                                'ala-chrome@'+'Default.service','ala-chrome@'+'display.service + _UID=0'])
def test_unsafe_named_journal_scope_is_rejected_without_query(unit):
    host=JournalHost()
    result=display_journal.read_display_journal(host,UID,unit=unit)
    assert result['exit_code']==1 and not host.calls


def test_named_target_cannot_return_default_or_another_target_partial_log():
    host=JournalHost(Command(0,runtime('default must remain hidden')),Command(0,''))
    result=display_journal.read_display_journal(host,UID,unit='ala-chrome@'+'display-2.service')
    assert result['state']=='unknown' and result['log']=='' and 'hidden' not in str(result)


def test_named_target_user_journal_empty_uses_the_named_root_fallback(monkeypatch):
    config=Config(schema_version=2,user='user',screens=[{'id':'display-2'}]).for_screen('display-2')
    calls=[]
    monkeypatch.setattr(display,'_account',lambda *_:(UID,UID,Path('/home/user')))
    monkeypatch.setattr(display,'_user_call',lambda *_:{'exit_code':0,'log':''})
    monkeypatch.setattr(display_journal,'read_display_journal',lambda h,u,**kw:calls.append((u,kw)) or {'exit_code':0,'log':'named'})
    assert display.chrome_action('logs',config,host=JournalHost())['log']=='named'
    assert calls==[(UID,{'unit':'ala-chrome@'+'display-2.service'})]
