"""Bounded Chrome cold-start checks using fake time and private local pipes."""
from collections import deque
import json
import os

import pytest

from ala import cdp, display_health as health
from ala.config import Config
from ala.system import Paths


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class StartupBrowser:
    pid = 222

    def __init__(self, argv, clock):
        self.argv = list(argv)
        self.clock = clock
        self.calls = []
        self.waits = []
        self.queries = 0
        self.ready_after = 1
        self.behavior = None
        self.on_wait = None
        self.code = None
        self.targets = None

    def poll(self):
        return self.code

    def call(self, method, params=None, **kwargs):
        self.calls.append((method, params, kwargs))
        assert 0 < kwargs['timeout'] <= health.TIMEOUT
        if method == 'Target.getTargets':
            self.queries += 1
        if self.behavior:
            self.behavior(self, method, kwargs['timeout'])
        if method == 'Target.getTargets':
            return {'targetInfos': self.targets if self.targets is not None else
                    [{'targetId': 'owned', 'type': 'page', 'url': health.RECOVERY_URL}]
                    if self.queries >= self.ready_after else []}
        if method == 'Target.attachToTarget':
            return {'sessionId': 'owned-session'}
        if method == 'Page.getFrameTree':
            return {'frameTree': {'frame': {'id': 'main'}}}
        return {}

    def next_event(self, timeout):
        self.waits.append(timeout)
        self.clock.now += timeout
        if self.on_wait:
            self.on_wait(self)
        return None


@pytest.fixture
def startup(tmp_path):
    paths = Paths(tmp_path)
    boot = paths.at('/proc/sys/kernel/random/boot_id')
    boot.parent.mkdir(parents=True)
    boot.write_text('synthetic-boot')
    for pid in (os.getpid(), StartupBrowser.pid):
        process = paths.at('/proc') / str(pid)
        process.mkdir(parents=True)
        (process / 'status').write_text(f'Uid:\t{os.getuid()}\n')
        (process / 'stat').write_text(f'{pid} (synthetic process) S ' + '0 ' * 18 + '123')
    clock = Clock()
    browsers = []
    def factory(argv):
        browser = StartupBrowser(argv, clock)
        browsers.append(browser)
        if configure[0]:
            configure[0](browser)
        return browser
    configure = [None]
    supervisor = health.Supervisor(Config(user='user', url='https://example.com/display'),
                                   paths.at('/home/user'), ['chrome', health.RECOVERY_URL],
                                   paths=paths, transport_factory=factory, clock=clock, monotonic=clock)
    return supervisor, clock, browsers, configure


def test_ready_target_initializes_immediately_without_a_readiness_sleep(startup):
    supervisor, clock, browsers, _ = startup
    supervisor.start()
    assert clock.now == 1000.0
    assert browsers[0].waits == []
    assert [method for method, _, _ in browsers[0].calls] == [
        'Target.getTargets', 'Target.attachToTarget', 'Page.enable', 'Network.enable',
        'Page.setLifecycleEventsEnabled', 'Page.getFrameTree',
    ]
    assert supervisor.target == 'owned' and supervisor.session == 'owned-session' and supervisor.frame == 'main'
    assert supervisor.value['navigation']['display_requests'] == 0
    assert supervisor.value['navigation']['recovery_requests'] == 0


def test_management_target_can_become_ready_after_more_than_ten_queries(startup):
    supervisor, clock, browsers, configure = startup
    configure[0] = lambda browser: setattr(browser, 'ready_after', 11)
    supervisor.start()
    assert browsers[0].queries == 11
    assert clock.now == pytest.approx(1001.0)
    assert supervisor.frame == 'main'


def test_browser_spawn_time_consumes_the_same_startup_budget(startup):
    supervisor, clock, browsers, configure = startup
    def configure_browser(browser):
        clock.now += health.STARTUP_TIMEOUT
    configure[0] = configure_browser
    with pytest.raises(cdp.CDPError, match='준비 시간이 초과'):
        supervisor.start()
    assert browsers[0].calls == []


def test_stop_during_target_wait_does_not_issue_a_later_attach(startup):
    supervisor, _, browsers, configure = startup
    def configure_browser(browser):
        browser.ready_after = float('inf')
        browser.on_wait = lambda current: setattr(supervisor, 'stopping', True)
    configure[0] = configure_browser
    supervisor.start()
    assert browsers[0].queries == 1
    assert supervisor.target is supervisor.session is supervisor.frame is None
    assert supervisor.value['navigation']['display_requests'] == 0


def test_complete_query_response_timeout_is_retried_inside_the_same_startup_budget(startup):
    supervisor, clock, browsers, configure = startup
    def configure_browser(browser):
        def behavior(current, method, timeout):
            if method == 'Target.getTargets' and current.queries == 1:
                clock.now += timeout
                raise cdp.CDPResponseTimeout('고정 합성 응답 시간 초과')
        browser.behavior = behavior
    configure[0] = configure_browser
    supervisor.start()
    assert browsers[0].queries == 2
    assert clock.now == 1005.0
    assert supervisor.frame == 'main'


@pytest.mark.parametrize('response_timeout', [False, True])
def test_startup_has_a_monotonic_deadline_instead_of_an_unbounded_query_count(startup, response_timeout):
    supervisor, clock, browsers, configure = startup
    def configure_browser(browser):
        browser.ready_after = float('inf')
        if response_timeout:
            def behavior(current, method, timeout):
                clock.now += timeout
                raise cdp.CDPResponseTimeout('고정 합성 응답 시간 초과')
            browser.behavior = behavior
    configure[0] = configure_browser
    with pytest.raises(cdp.CDPError, match='준비 시간이 초과'):
        supervisor.start()
    assert clock.now == pytest.approx(1000 + health.STARTUP_TIMEOUT)
    assert supervisor.target is None
    assert all(method == 'Target.getTargets' for method, _, _ in browsers[0].calls)


def test_attach_and_page_initialization_share_the_remaining_startup_deadline(startup):
    supervisor, clock, browsers, configure = startup
    def configure_browser(browser):
        def behavior(current, method, timeout):
            if method == 'Target.getTargets' and current.queries <= 2:
                clock.now += timeout
                raise cdp.CDPResponseTimeout('고정 합성 응답 시간 초과')
            duration = 3 if method == 'Target.getTargets' else 4
            if duration >= timeout:
                clock.now += timeout
                raise cdp.CDPResponseTimeout('고정 합성 응답 시간 초과')
            clock.now += duration
        browser.behavior = behavior
    configure[0] = configure_browser
    with pytest.raises(cdp.CDPError, match='준비 시간이 초과'):
        supervisor.start()
    assert clock.now == 1030.0
    assert browsers[0].calls[-1][0] == 'Page.getFrameTree'
    assert browsers[0].calls[-1][2]['timeout'] == 1.0
    for method in ('Target.attachToTarget', 'Page.enable', 'Network.enable',
                   'Page.setLifecycleEventsEnabled', 'Page.getFrameTree'):
        assert sum(name == method for name, _, _ in browsers[0].calls) == 1


@pytest.mark.parametrize('failed_method', ['Target.attachToTarget', 'Page.enable', 'Network.enable',
                                         'Page.setLifecycleEventsEnabled', 'Page.getFrameTree'])
def test_only_target_discovery_retries_a_typed_response_timeout(startup, failed_method):
    supervisor, clock, browsers, configure = startup
    def configure_browser(browser):
        def behavior(current, method, timeout):
            if method == failed_method:
                clock.now += timeout
                raise cdp.CDPResponseTimeout('고정 합성 응답 시간 초과')
        browser.behavior = behavior
    configure[0] = configure_browser
    with pytest.raises(cdp.CDPResponseTimeout):
        supervisor.start()
    assert browsers[0].queries == 1
    assert sum(method == failed_method for method, _, _ in browsers[0].calls) == 1


@pytest.mark.parametrize('reason', ['전송 시간 초과', 'pipe 연결 종료', '응답 형식 오류',
                                   '응답 크기 제한', '이벤트 처리 한도'])
def test_non_response_timeout_control_failures_do_not_retry_discovery(startup, reason):
    supervisor, _, browsers, configure = startup
    def configure_browser(browser):
        def behavior(*args):
            raise cdp.CDPError(reason)
        browser.behavior = behavior
    configure[0] = configure_browser
    with pytest.raises(cdp.CDPError, match=reason):
        supervisor.start()
    assert browsers[0].queries == 1
    assert len(browsers[0].calls) == 1


@pytest.mark.parametrize('when', ['before_query', 'response_timeout', 'after_empty_query', 'after_reply'])
def test_child_exit_is_not_treated_as_a_cold_start_delay(startup, when):
    supervisor, clock, browsers, configure = startup
    def configure_browser(browser):
        if when == 'before_query':
            browser.code = 7
        elif when == 'after_empty_query':
            browser.ready_after = float('inf')
            browser.on_wait = lambda current: setattr(current, 'code', 7)
        else:
            def behavior(current, method, timeout):
                current.code = 7
                if when == 'response_timeout':
                    clock.now += timeout
                    raise cdp.CDPResponseTimeout('고정 합성 응답 시간 초과')
            browser.behavior = behavior
    configure[0] = configure_browser
    with pytest.raises(cdp.CDPError, match='프로세스가 종료'):
        supervisor.start()
    assert browsers[0].queries <= 1
    assert not any(method == 'Target.attachToTarget' for method, _, _ in browsers[0].calls)


def test_multiple_owned_targets_are_rejected_without_selecting_or_retrying(startup):
    supervisor, _, browsers, configure = startup
    def configure_browser(browser):
        browser.targets = [{'targetId': str(index), 'type': 'page', 'url': health.RECOVERY_URL}
                           for index in range(2)]
    configure[0] = configure_browser
    with pytest.raises(cdp.CDPError, match='여러 개'):
        supervisor.start()
    assert browsers[0].queries == 1
    assert supervisor.target is None


def pipe_pair():
    browser = object.__new__(cdp.PipeBrowser)
    incoming_r, incoming_w = os.pipe()
    outgoing_r, outgoing_w = os.pipe()
    browser._read, browser._write = incoming_r, outgoing_w
    browser._buffer, browser._events, browser._serial = bytearray(), deque(), 0
    browser.pid, browser.returncode = None, 0
    return browser, incoming_w, outgoing_r


def test_response_timeout_is_typed_only_after_the_whole_command_was_sent():
    browser, incoming, outgoing = pipe_pair()
    try:
        with pytest.raises(cdp.CDPResponseTimeout):
            browser.call('Target.getTargets', timeout=0.001)
        sent = os.read(outgoing, 65536)
        assert sent.endswith(b'\0')
        assert json.loads(sent[:-1])['method'] == 'Target.getTargets'
    finally:
        browser.close()
        os.close(incoming)
        os.close(outgoing)


def test_partial_command_send_timeout_remains_fatal(monkeypatch):
    browser, incoming, outgoing = pipe_pair()
    selections = 0
    def select_once(readers, writers, errors, timeout):
        nonlocal selections
        selections += 1
        return ([], writers, []) if selections == 1 else ([], [], [])
    try:
        with monkeypatch.context() as patch:
            patch.setattr(cdp.select, 'select', select_once)
            patch.setattr(cdp.os, 'write', lambda fd, packet: 1)
            with pytest.raises(cdp.CDPError, match='전송 시간이 초과') as found:
                browser.call('Target.getTargets', timeout=1)
            assert not isinstance(found.value, cdp.CDPResponseTimeout)
    finally:
        browser.close()
        os.close(incoming)
        os.close(outgoing)


@pytest.mark.parametrize('partial_old_reply', [False, True])
def test_late_reply_does_not_match_the_new_request_and_queued_events_keep_order(partial_old_reply):
    browser, incoming, outgoing = pipe_pair()
    try:
        old_reply = json.dumps({'id': 1, 'result': {'targetInfos': []}}).encode() + b'\0'
        if partial_old_reply:
            os.write(incoming, old_reply[:10])
        with pytest.raises(cdp.CDPResponseTimeout):
            browser.call('Target.getTargets', timeout=0.001)
        assert json.loads(os.read(outgoing, 65536)[:-1])['id'] == 1
        packets = [
            {'method': 'Target.targetCreated', 'params': {'sequence': 1}},
            {'id': 1, 'result': {'targetInfos': []}},
            {'method': 'Target.targetInfoChanged', 'params': {'sequence': 2}},
            {'id': 2, 'result': {'targetInfos': [{'targetId': 'owned'}]}},
        ]
        if partial_old_reply:
            packets.pop(1)
        late = b''.join(json.dumps(packet).encode() + b'\0' for packet in packets)
        os.write(incoming, old_reply[10:] + late if partial_old_reply else late)
        assert browser.call('Target.getTargets')['targetInfos'] == [{'targetId': 'owned'}]
        assert json.loads(os.read(outgoing, 65536)[:-1])['id'] == 2
        assert [browser.next_event()['params']['sequence'] for _ in range(2)] == [1, 2]
    finally:
        browser.close()
        os.close(incoming)
        os.close(outgoing)


@pytest.mark.parametrize('packet', [b'not-json\0', b'[]\0', b'x' * 70,
                                   b'{"id":1,"error":{}}\0'])
def test_protocol_and_size_errors_are_never_retryable_response_timeouts(packet):
    browser, incoming, outgoing = pipe_pair()
    browser.MAX_MESSAGE = 64
    try:
        os.write(incoming, packet)
        with pytest.raises(cdp.CDPError) as found:
            browser.call('Target.getTargets', timeout=0.1)
        assert not isinstance(found.value, cdp.CDPResponseTimeout)
    finally:
        browser.close()
        os.close(incoming)
        os.close(outgoing)


def test_queued_event_limit_remains_fatal_while_waiting_for_a_reply():
    browser, incoming, outgoing = pipe_pair()
    browser.MAX_EVENTS = 1
    try:
        os.write(incoming, b'{"method":"First"}\0{"method":"Second"}\0{"id":1,"result":{}}\0')
        with pytest.raises(cdp.CDPError, match='이벤트 처리 한도') as found:
            browser.call('Target.getTargets', timeout=0.1)
        assert not isinstance(found.value, cdp.CDPResponseTimeout)
    finally:
        browser.close()
        os.close(incoming)
        os.close(outgoing)


def test_pipe_eof_is_not_a_retryable_response_timeout():
    browser, incoming, outgoing = pipe_pair()
    os.close(incoming)
    try:
        with pytest.raises(cdp.CDPError, match='pipe 연결이 종료') as found:
            browser.call('Target.getTargets', timeout=0.1)
        assert not isinstance(found.value, cdp.CDPResponseTimeout)
    finally:
        browser.close()
        os.close(outgoing)
