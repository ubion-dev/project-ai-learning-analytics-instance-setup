from dataclasses import replace
from urllib.parse import unquote

import pytest

from ala import display_health as health
from ala.config import Config
from test_display_health import fixture, event


def setup_supervisor(fixture):
    supervisor, http, clock, paths = fixture
    supervisor.config = replace(supervisor.config, url=None)
    supervisor.value['config_fingerprint'] = health.fingerprint(None)
    supervisor.value['local_page_ready'] = False
    supervisor.setup_context_callback = lambda: {'output': 'HDMI-1', 'display_number': 1}
    return supervisor, http, clock, paths


def complete_local(supervisor):
    event(supervisor, 'Page.frameNavigated', frame={'id': 'main', 'loaderId': supervisor.loader, 'url': supervisor._setup_url})
    event(supervisor, 'Page.lifecycleEvent', frameId='main', loaderId=supervisor.loader, name='load')


def test_delayed_verified_launch_document_does_not_fail_new_setup_or_claim_it_loaded(fixture):
    from test_display_health import Browser
    supervisor, http, clock, paths = setup_supervisor(fixture)
    class InitialBrowser(Browser):
        def call(self, method, params=None, **kwargs):
            if method == 'Page.getFrameTree':
                return {'frameTree': {'frame': {'id': 'main', 'loaderId': 'bootstrap', 'url': health.RECOVERY_URL}}}
            return super().call(method, params, **kwargs)
    supervisor.transport_factory = InitialBrowser
    supervisor.start()
    event(supervisor, 'Page.frameNavigated', frame={'id': 'main', 'loaderId': 'bootstrap', 'url': health.RECOVERY_URL})
    assert supervisor.value['local_page_ready'] is False and not supervisor.document_ok
    complete_local(supervisor)
    assert supervisor.value['local_page_ready'] is True
    with pytest.raises(health.CDPError):
        event(supervisor, 'Page.frameNavigated', frame={'id': 'main', 'loaderId': 'bootstrap', 'url': health.RECOVERY_URL})


@pytest.mark.parametrize('change', [{'url': 'https://example.com/other'}, {'loaderId': 'other', 'url': 'data:text/plain,other'}])
def test_unverified_old_document_is_still_rejected_before_setup_load(fixture, change):
    supervisor, http, clock, paths = setup_supervisor(fixture)
    supervisor.start()
    supervisor._bootstrap_loader = 'bootstrap'; supervisor._bootstrap_pending = True
    with pytest.raises(health.CDPError):
        event(supervisor, 'Page.frameNavigated', frame={'id': 'main', 'loaderId': 'bootstrap', 'url': health.RECOVERY_URL} | change)


def test_launch_document_with_unobserved_loader_is_only_pending_not_ready(fixture):
    supervisor, _, _, _ = setup_supervisor(fixture)
    supervisor.start()
    event(supervisor, 'Page.frameNavigated', frame={'id': 'main', 'loaderId': 'late-launch', 'url': health.RECOVERY_URL})
    assert not supervisor.document_ok and supervisor.value['local_page_ready'] is False
    complete_local(supervisor)
    assert supervisor.value['local_page_ready'] is True


def test_stale_frame_event_is_ignored_only_if_fresh_owned_frame_proves_expected_document(fixture):
    supervisor, _, _, _ = setup_supervisor(fixture)
    supervisor.start()
    browser = supervisor.browser
    original = browser.call
    def current(method, params=None, **kwargs):
        if method == 'Page.getFrameTree':
            return {'frameTree': {'frame': {'id': supervisor.frame, 'loaderId': supervisor.loader, 'url': supervisor._setup_url}}}
        return original(method, params, **kwargs)
    browser.call = current
    event(supervisor, 'Page.frameNavigated', frame={'id': 'main', 'loaderId': 'older', 'url': 'data:text/plain,old'})
    assert supervisor.value['local_page_ready'] is False
    complete_local(supervisor)
    assert supervisor.value['local_page_ready'] is True


def test_setup_uses_owned_tab_and_never_probes_network_or_claims_site_health(fixture):
    supervisor, http, clock, paths = setup_supervisor(fixture)
    supervisor.start()
    assert supervisor.browser.argv[-1] == health.RECOVERY_URL
    assert len(supervisor.browser.navigations()) == 1
    assert supervisor.value['local_page_ready'] is False
    complete_local(supervisor)
    for _ in range(5):
        clock.now += 30
        supervisor.probe()
        supervisor.frontend_tick()
    assert http.calls == []
    assert len(supervisor.browser.navigations()) == 1
    assert supervisor.value['state'] == 'waiting_configuration'
    assert supervisor.value['local_page_ready'] is True
    assert supervisor.value['target_matches_config'] is False
    assert not any(method == 'Runtime.evaluate' for method, _, _ in supervisor.browser.calls)
    assert health.read_health(supervisor.home, paths, supervisor.value['supervisor_pid'], None, now=clock.now)['state'] == 'waiting_configuration'


def test_setup_updates_number_after_topology_change_and_waits_for_new_document(fixture):
    supervisor, http, _, _ = setup_supervisor(fixture)
    supervisor.start()
    complete_local(supervisor)
    supervisor.setup_context_callback = lambda: {'output': 'HDMI-1', 'display_number': 2}
    supervisor.probe()
    assert '화면 2' in unquote(supervisor._setup_url)
    assert supervisor.value['local_page_ready'] is False
    complete_local(supervisor)
    assert supervisor.value['local_page_ready'] is True
    assert http.calls == []


def test_setup_refuses_unexpected_main_document(fixture):
    supervisor, _, _, _ = setup_supervisor(fixture)
    supervisor.start()
    with pytest.raises(health.CDPError, match='실제 표시'):
        event(supervisor, 'Page.frameNavigated', frame={'id': 'main', 'loaderId': supervisor.loader, 'url': 'https://example.com/other'})
    assert not supervisor.value['local_page_ready']


def test_same_document_setup_refresh_requires_new_local_document_proof(fixture):
    supervisor, http, _, _ = setup_supervisor(fixture)
    supervisor.start()
    complete_local(supervisor)
    original = supervisor.browser.call
    def same_document(method, params=None, **kwargs):
        if method == 'Page.navigate':
            return {'frameId': 'main'}
        return original(method, params, **kwargs)
    supervisor.browser.call = same_document
    supervisor._navigate(supervisor._setup_url, ensure_document=True)
    assert supervisor.value['local_page_ready'] is False
    event(supervisor, 'Page.frameNavigated', frame={'id': 'main', 'loaderId': 'reloaded', 'url': supervisor._setup_url})
    event(supervisor, 'Page.lifecycleEvent', frameId='main', loaderId='reloaded', name='load')
    assert supervisor.value['local_page_ready'] is True
    assert supervisor.value['state'] == 'waiting_configuration'
    assert http.calls == []


def test_setup_html_escapes_labels_and_config_still_rejects_internal_urls():
    document = unquote(health.setup_page(output='<script>bad</script>', display_number=1))
    assert '<script>' not in document
    assert '&lt;script&gt;' in document
    assert "default-src 'none'" in document
    with pytest.raises(ValueError):
        Config(user='user', url=health.setup_page())


def test_preparing_image_is_inline_and_offline_with_no_false_monitor_identity():
    document = unquote(health.setup_page(preparing=True))
    assert '<svg' in document and '기기 설정 중' in document
    assert '모니터 연결과 표시 환경을 준비' in document
    assert '화면 확인 필요' not in document
    assert '<script' not in document and '<img' not in document
    assert 'https://' not in document and "default-src 'none'" in document
