"""Synthetic CDP ordering regressions; no browser, network, or private state."""
from collections import deque
import json
import os

import pytest

from ala import cdp, display_health as health
from ala import __version__
from ala.config import Config


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class HTTP200:
    status = 200

    def __init__(self):
        self.calls = 0

    def __call__(self, request, timeout):
        self.calls += 1
        assert request.full_url == 'https://example.com/display'
        assert timeout == health.TIMEOUT
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class NavigationBrowser:
    """Only models responses; tests deliver document events separately."""
    def __init__(self, *, target_error=None):
        self.calls = []
        self.serial = 0
        self.target_error = target_error

    def call(self, method, params=None, **kwargs):
        assert method == 'Page.navigate'
        self.calls.append(params['url'])
        self.serial += 1
        if params['url'] != health.RECOVERY_URL and self.target_error:
            return dict(self.target_error)
        return {'frameId': 'main', 'loaderId': f'loader-{self.serial}'}


@pytest.fixture
def ready(tmp_path):
    clock, http = Clock(), HTTP200()
    supervisor = health.Supervisor(
        Config(user='user', url='https://example.com/display'), tmp_path,
        ['chrome', health.RECOVERY_URL], opener=http, clock=clock, monotonic=clock,
    )
    supervisor.browser = NavigationBrowser()
    supervisor.target = 'owned'
    supervisor.session = 'session'
    supervisor.frame = 'main'
    return supervisor, http, clock


def event(supervisor, method, **params):
    return {'sessionId': supervisor.session, 'method': method, 'params': params}


def document_events(supervisor, loader, request='document', *, include_load=True):
    events = [
        event(supervisor, 'Network.requestWillBeSent', type='Document', frameId='main',
              loaderId=loader, requestId=request, request={'url': supervisor.config.url}),
        event(supervisor, 'Network.responseReceived', type='Document', frameId='main',
              loaderId=loader, requestId=request,
              response={'status': 200, 'url': supervisor.config.url}),
        event(supervisor, 'Page.frameNavigated',
              frame={'id': 'main', 'loaderId': loader, 'url': supervisor.config.url}),
    ]
    if include_load:
        events.append(event(supervisor, 'Page.lifecycleEvent', frameId='main',
                            loaderId=loader, name='load'))
    return events


def pipe_pair():
    """Use the real bounded pipe reader without spawning any child process."""
    browser = object.__new__(cdp.PipeBrowser)
    incoming_r, incoming_w = os.pipe()
    outgoing_r, outgoing_w = os.pipe()
    browser._read, browser._write = incoming_r, outgoing_w
    browser._buffer = bytearray()
    browser._events = deque()
    browser._serial = 0
    browser.pid, browser.returncode = None, 0
    return browser, incoming_w, outgoing_r


@pytest.mark.parametrize('before_response', [0, 2, 4])
def test_commit_and_load_events_queued_around_navigate_reply_do_not_cause_periodic_reload(ready, before_response):
    supervisor, http, clock = ready
    browser, incoming, outgoing = pipe_pair()
    supervisor.browser = browser
    events = document_events(supervisor, 'first-loader')
    response = {'id': 1, 'result': {'frameId': 'main', 'loaderId': 'first-loader'}}
    packets = events[:before_response] + [response] + events[before_response:]
    try:
        os.write(incoming, b''.join(json.dumps(packet).encode() + b'\0' for packet in packets))
        supervisor.probe()
        request = os.read(outgoing, 65536)
        assert json.loads(request[:-1])['params'] == {'url': supervisor.config.url}
        # PipeBrowser queues events received before a matching command response;
        # Supervisor must accept them after adopting the returned loader ID.
        for _ in events:
            supervisor.event(browser.next_event(timeout=0.01))
        assert supervisor.value['state'] == 'healthy'
        assert supervisor.deadline is None
        for _ in range(8):
            clock.now += health.INTERVAL
            supervisor.probe()
        assert supervisor.value['state'] == 'healthy'
        assert browser._serial == 1  # No later CDP command, even after 8 probes.
        assert http.calls == 9
    finally:
        browser.close()
        os.close(incoming)
        os.close(outgoing)


@pytest.mark.parametrize('result', [{'errorText': 'synthetic navigation failure'}, {'isDownload': True}])
def test_initial_navigation_failure_shows_recovery_before_target_is_retried(ready, result):
    supervisor, _, clock = ready
    supervisor.browser = NavigationBrowser(target_error=result)
    supervisor.probe()
    assert supervisor.value['state'] == 'recovering'
    assert not supervisor.value['target_matches_config']
    assert supervisor.browser.calls == [supervisor.config.url, health.RECOVERY_URL]
    for _ in range(4):
        clock.now += health.INTERVAL
        supervisor.probe()
    assert supervisor.browser.calls == [supervisor.config.url, health.RECOVERY_URL] * 3


def test_http_response_and_frame_commit_without_load_keep_the_navigation_deadline(ready):
    supervisor, _, clock = ready
    supervisor.probe()
    deadline = supervisor.deadline
    assert deadline == clock.now + health.NAVIGATION_TIMEOUT
    for incoming in document_events(supervisor, supervisor.loader, include_load=False):
        supervisor.event(incoming)
    assert supervisor.document_ok and not supervisor.loaded
    assert supervisor.value['state'] == 'recovering'
    assert supervisor.navigating and supervisor.deadline == deadline
    # An HTTP probe is not a replacement for the real tab's load event.
    clock.now += health.INTERVAL
    supervisor.probe()
    assert supervisor.browser.calls == [supervisor.config.url]
    assert supervisor.deadline == deadline


def test_late_previous_request_failure_does_not_replace_a_new_healthy_document(ready):
    supervisor, _, _ = ready
    supervisor.probe()
    for incoming in document_events(supervisor, supervisor.loader, request='previous-document'):
        supervisor.event(incoming)
    for incoming in document_events(supervisor, 'site-reload', request='current-document'):
        supervisor.event(incoming)
    assert supervisor.value['state'] == 'healthy'
    supervisor.event(event(supervisor, 'Network.loadingFailed', requestId='previous-document',
                           canceled=True, errorText='net::ERR_ABORTED'))
    assert supervisor.value['state'] == 'healthy'
    assert supervisor.browser.calls == [supervisor.config.url]
    assert supervisor.value['navigation']['last_error'] is None


def complete_document(supervisor, *, loader=None, request='document'):
    for incoming in document_events(supervisor, loader or supervisor.loader, request=request):
        supervisor.event(incoming)


def test_navigation_diagnostics_start_empty_and_healthy_probes_do_not_change_them(ready):
    supervisor, _, clock = ready
    assert supervisor.value['supervisor_version'] == __version__
    assert supervisor.value['navigation'] == {
        'display_requests': 0, 'recovery_requests': 0, 'browser_navigations': 0,
        'last_command': None, 'last_browser_navigation': None, 'last_error': None,
    }
    supervisor.probe()
    complete_document(supervisor)
    expected = {
        'display_requests': 1, 'recovery_requests': 0, 'browser_navigations': 0,
        'last_command': {'kind': 'display', 'at': health.stamp(clock.now)},
        'last_browser_navigation': None, 'last_error': None,
    }
    assert supervisor.value['navigation'] == expected
    for _ in range(8):
        clock.now += health.INTERVAL
        supervisor.probe()
    assert supervisor.value['navigation'] == expected
    assert supervisor.browser.calls == [supervisor.config.url]


@pytest.mark.parametrize(('initiator', 'category'), [
    ({'type': 'script'}, 'script'),
    ({'type': 'parser'}, 'parser'),
    ({'type': 'other'}, 'other'),
    (None, 'unknown'),
    ({}, 'unknown'),
    ({'type': 'preload'}, 'unknown'),
    ({'type': 3}, 'unknown'),
    ('script', 'unknown'),
])
def test_new_allowed_main_loader_is_observed_once_without_claiming_the_cause(ready, initiator, category):
    supervisor, _, clock = ready
    supervisor.probe()
    complete_document(supervisor)
    clock.now += 11
    request = event(supervisor, 'Network.requestWillBeSent', type='Document', frameId='main',
                    loaderId='page-loader', requestId='page-document',
                    request={'url': supervisor.config.url})
    if initiator is not None:
        request['params']['initiator'] = initiator
    supervisor.event(request)
    navigation = supervisor.value['navigation']
    assert navigation['display_requests'] == 1
    assert navigation['recovery_requests'] == 0
    assert navigation['browser_navigations'] == 1
    assert navigation['last_browser_navigation'] == {'initiator': category, 'at': health.stamp(clock.now)}
    # Redirects and repeated request events retain the loader; they are not a
    # second browser navigation even when a different initiator is attached.
    request['params']['redirectResponse'] = {'status': 302}
    request['params']['initiator'] = {'type': 'script'}
    supervisor.event(request)
    complete_document(supervisor, request='page-document')
    assert supervisor.value['state'] == 'healthy'
    assert supervisor.value['navigation']['browser_navigations'] == 1
    assert supervisor.value['navigation']['last_browser_navigation']['initiator'] == category
    assert supervisor.browser.calls == [supervisor.config.url]


@pytest.mark.parametrize(('frame', 'kind', 'session'), [
    ('child', 'Document', 'session'),
    ('main', 'Image', 'session'),
    ('main', 'Other', 'session'),
    ('main', 'Document', 'unrelated-session'),
])
def test_non_main_document_observations_cannot_increment_browser_navigation(ready, frame, kind, session):
    supervisor, _, _ = ready
    supervisor.probe()
    complete_document(supervisor)
    incoming = event(supervisor, 'Network.requestWillBeSent', type=kind, frameId=frame,
                     loaderId='unrelated-loader', requestId='unrelated-request',
                     request={'url': supervisor.config.url}, initiator={'type': 'script'})
    incoming['sessionId'] = session
    supervisor.event(incoming)
    assert supervisor.value['navigation']['browser_navigations'] == 0
    assert supervisor.value['navigation']['last_browser_navigation'] is None
    assert supervisor.browser.calls == [supervisor.config.url]


@pytest.mark.parametrize(('result', 'category'), [
    ({'errorText': 'net::ERR_ABORTED'}, 'aborted'),
    ({'errorText': 'ERR_ABORTED'}, 'aborted'),
    ({'errorText': 'net::ERR_ABORTED with raw-browser-error-marker'}, 'navigation_error'),
    ({'errorText': 'raw-browser-error-marker'}, 'navigation_error'),
    ({'isDownload': True}, 'download'),
])
def test_navigation_failure_records_a_fixed_error_category_and_real_recovery_request(ready, result, category):
    supervisor, _, clock = ready
    supervisor.browser = NavigationBrowser(target_error=result)
    supervisor.probe()
    navigation = supervisor.value['navigation']
    assert navigation['display_requests'] == 1
    assert navigation['recovery_requests'] == 1
    assert navigation['browser_navigations'] == 0
    assert navigation['last_command'] == {'kind': 'recovery', 'at': health.stamp(clock.now)}
    assert navigation['last_error'] == {'kind': category, 'at': health.stamp(clock.now)}
    assert 'raw-browser-error-marker' not in health.state_path(supervisor.home).read_text()
    before = list(supervisor.browser.calls)
    # Duplicate error events while already displaying recovery must not reload
    # the local recovery page. A later explicit display retry remains allowed.
    supervisor._fallback('고정된 합성 복구 사유')
    supervisor._fallback('고정된 합성 복구 사유')
    assert supervisor.browser.calls == before
    assert supervisor.value['navigation']['recovery_requests'] == 1


def test_recorded_navigation_has_no_destination_or_raw_initiator_stack(ready):
    supervisor, _, clock = ready
    supervisor.probe()
    complete_document(supervisor)
    clock.now += 7
    supervisor.event(event(
        supervisor, 'Network.requestWillBeSent', type='Document', frameId='main',
        loaderId='script-loader', requestId='script-document',
        request={'url': supervisor.config.url},
        initiator={'type': 'script', 'url': 'https://example.com/raw-script-source-marker',
                   'stack': {'callFrames': [{'url': 'https://example.com/raw-stack-marker',
                                            'functionName': 'raw-function-marker'}]}},
    ))
    complete_document(supervisor, request='script-document')
    saved = json.loads(health.state_path(supervisor.home).read_text())
    assert saved['supervisor_version'] == __version__
    assert set(saved['navigation']) == {
        'display_requests', 'recovery_requests', 'browser_navigations',
        'last_command', 'last_browser_navigation', 'last_error',
    }
    assert saved['navigation']['last_browser_navigation'] == {
        'initiator': 'script', 'at': health.stamp(clock.now),
    }
    serialized = json.dumps(saved)
    for raw in (supervisor.config.url, 'raw-script-source-marker', 'raw-stack-marker', 'raw-function-marker'):
        assert raw not in serialized


@pytest.mark.parametrize(('error_text', 'canceled', 'category'), [
    ('net::ERR_ABORTED', False, 'aborted'),
    ('raw-browser-error-marker', True, 'aborted'),
    ('ERR_ABORTED', False, 'navigation_error'),
    ('raw-browser-error-marker', False, 'navigation_error'),
    ('raw-browser-error-marker', 1, 'navigation_error'),
])
def test_matching_main_request_failure_records_only_a_fixed_error_category(ready, error_text, canceled, category):
    supervisor, _, clock = ready
    supervisor.probe()
    supervisor.event(event(
        supervisor, 'Network.requestWillBeSent', type='Document', frameId='main',
        loaderId=supervisor.loader, requestId='active-document', request={'url': supervisor.config.url},
    ))
    clock.now += 3
    supervisor.event(event(supervisor, 'Network.loadingFailed', requestId='active-document',
                           type='Document', canceled=canceled, errorText=error_text))
    assert supervisor.value['navigation']['last_error'] == {
        'kind': category, 'at': health.stamp(clock.now),
    }
    assert supervisor.browser.calls == [supervisor.config.url, health.RECOVERY_URL]
    assert supervisor.value['state'] == 'recovering'
    assert 'raw-browser-error-marker' not in health.state_path(supervisor.home).read_text()


def test_other_requests_and_sessions_cannot_create_or_overwrite_a_navigation_error(ready):
    supervisor, _, clock = ready
    supervisor.probe()
    complete_document(supervisor, request='first-document')
    clock.now += 4
    complete_document(supervisor, loader='next-loader', request='next-document')
    for request, session in [('first-document', 'session'), ('subresource', 'session'),
                             ('next-document', 'unrelated-session')]:
        incoming = event(supervisor, 'Network.loadingFailed', requestId=request,
                         canceled=True, errorText='net::ERR_ABORTED')
        incoming['sessionId'] = session
        supervisor.event(incoming)
    assert supervisor.value['state'] == 'healthy'
    assert supervisor.value['navigation']['last_error'] is None
    assert supervisor.browser.calls == [supervisor.config.url]
