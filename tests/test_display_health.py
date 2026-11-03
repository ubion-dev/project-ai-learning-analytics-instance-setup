from collections import deque
from dataclasses import replace
import json
import os
import ssl
import urllib.error

import pytest

from ala import cdp, display_health as health
from ala.config import Config
from ala.system import Paths


def seed_process(paths, pid, argv=(), ticks='123', cgroup='0::/user.slice/ala-chrome.service\n'):
    process = paths.at('/proc') / str(pid)
    process.mkdir(parents=True, exist_ok=True)
    (process/'status').write_text(f'Uid:\t{os.getuid()}\n')
    (process/'stat').write_text(f'{pid} (test process) S ' + '0 '*18 + ticks)
    (process/'cmdline').write_bytes(('\0'.join(map(str, argv))+'\0').encode())
    (process/'cgroup').write_text(cgroup)


class Clock:
    now = 1000.0
    def __call__(self): return self.now


class HTTP:
    status = 200
    error = None
    def __init__(self): self.calls=[]
    def __call__(self, request, timeout):
        self.calls.append((request.full_url, timeout))
        if self.error: raise self.error
        if self.status >= 400:
            raise urllib.error.HTTPError(request.full_url, self.status, 'synthetic error', {}, None)
        return self
    def __enter__(self): return self
    def __exit__(self, *args): pass


class Browser:
    pid = 222
    def __init__(self, argv):
        self.argv=list(argv); self.calls=[]; self.closed=False; self.code=None
        self.events=deque(); self.serial=0; self.navigation_error=False
    def call(self, method, params=None, **kwargs):
        self.calls.append((method,params,kwargs))
        if method=='Target.getTargets':
            return {'targetInfos':[{'targetId':'owned','type':'page','url':health.RECOVERY_URL}]}
        if method=='Target.attachToTarget': return {'sessionId':'session'}
        if method=='Page.getFrameTree': return {'frameTree':{'frame':{'id':'main'}}}
        if method=='Page.navigate':
            self.serial+=1
            if self.navigation_error and params['url']!=health.RECOVERY_URL: return {'errorText':'synthetic failure'}
            return {'frameId':'main','loaderId':f'load-{self.serial}'}
        return {}
    def next_event(self, timeout=1): return self.events.popleft() if self.events else None
    def poll(self): return self.code
    def close(self): self.closed=True
    def navigations(self): return [params['url'] for name,params,_ in self.calls if name=='Page.navigate']


@pytest.fixture
def fixture(tmp_path):
    paths=Paths(tmp_path)
    boot=paths.at('/proc/sys/kernel/random/boot_id'); boot.parent.mkdir(parents=True); boot.write_text('test-boot')
    seed_process(paths,os.getpid()); seed_process(paths,Browser.pid)
    home=paths.at('/home/user'); home.mkdir(parents=True)
    http=HTTP(); clock=Clock(); config=Config(user='user',url='https://example.com/display')
    supervisor=health.Supervisor(config,home,['chrome','--remote-debugging-pipe',health.RECOVERY_URL],
                                 paths=paths,opener=http,transport_factory=Browser,clock=clock,monotonic=clock)
    return supervisor,http,clock,paths


def event(supervisor, method, **params):
    supervisor.event({'sessionId':'session','method':method,'params':params})


def document(supervisor, status=200, *, frame='main', type='Document', loader=None):
    loader=loader or supervisor.loader
    event(supervisor,'Network.requestWillBeSent',type=type,frameId=frame,loaderId=loader,requestId='document',
          request={'url':supervisor.config.url})
    event(supervisor,'Network.responseReceived',type=type,frameId=frame,loaderId=loader,response={'status':status})
    event(supervisor,'Page.lifecycleEvent',frameId=frame,loaderId=loader,name='load')


def healthy(fixture):
    supervisor,http,clock,paths=fixture
    supervisor.start(); supervisor.probe(); supervisor.probe(); document(supervisor)
    assert supervisor.value['state']=='healthy'
    return supervisor,http,clock,paths


def test_healthy_start_probes_once_then_checks_real_document_without_30_second_delay(fixture):
    supervisor,http,_,_=fixture
    supervisor.start()
    assert supervisor.browser.navigations()==[]
    supervisor.probe()
    assert supervisor.browser.navigations()==[supervisor.config.url]
    assert supervisor.value['state']=='recovering'
    document(supervisor)
    assert supervisor.value['state']=='healthy' and supervisor.value['target_matches_config']
    assert all(timeout==5 for _,timeout in http.calls)


def test_healthy_probes_never_reload_or_navigate(fixture):
    supervisor,http,_,_=healthy(fixture)
    before=list(supervisor.browser.navigations())
    for _ in range(8): supervisor.probe()
    assert supervisor.browser.navigations()==before
    assert supervisor.value['state']=='healthy'


def test_three_failures_show_one_fallback_and_two_successes_navigate_once(fixture):
    supervisor,http,_,_=healthy(fixture)
    http.status=503
    supervisor.probe(); supervisor.probe()
    assert supervisor.value['state']=='healthy'
    supervisor.probe()
    assert supervisor.value['state']=='recovering'
    assert supervisor.browser.navigations().count(health.RECOVERY_URL)==1
    for _ in range(4): supervisor.probe()
    assert supervisor.browser.navigations().count(health.RECOVERY_URL)==1
    http.status=200
    supervisor.probe()
    assert supervisor.browser.navigations().count(supervisor.config.url)==1
    supervisor.probe(); supervisor.probe()
    assert supervisor.browser.navigations().count(supervisor.config.url)==2
    document(supervisor)
    assert supervisor.value['state']=='healthy'


@pytest.mark.parametrize('status',[404,500,503])
def test_main_document_error_immediately_falls_back_even_chrome_alive(fixture,status):
    supervisor,_,_,_=healthy(fixture)
    assert supervisor.browser.poll() is None
    document(supervisor,status)
    assert supervisor.value['state']=='recovering'
    assert supervisor.browser.navigations()[-1]==health.RECOVERY_URL
    assert supervisor.browser.poll() is None


@pytest.mark.parametrize('frame,type',[('child','Document'),('main','Image'),('main','Other')])
def test_iframe_and_subresources_do_not_replace_healthy_display(fixture,frame,type):
    supervisor,_,_,_=healthy(fixture)
    before=list(supervisor.browser.navigations())
    document(supervisor,404,frame=frame,type=type)
    event(supervisor,'Network.loadingFailed',requestId='favicon')
    assert supervisor.value['state']=='healthy'
    assert supervisor.browser.navigations()==before


def test_probe_success_then_navigation_error_race_returns_to_fallback(fixture):
    supervisor,_,_,_=fixture
    supervisor.start(); supervisor.browser.navigation_error=True; supervisor.probe()
    assert supervisor.value['state']=='recovering'
    assert not supervisor.value['target_matches_config']


def test_main_network_failure_is_immediate_and_unrelated_session_ignored(fixture):
    supervisor,_,_,_=healthy(fixture)
    supervisor.event({'sessionId':'other','method':'Network.loadingFailed','params':{'requestId':'document'}})
    assert supervisor.value['state']=='healthy'
    event(supervisor,'Network.loadingFailed',requestId='document')
    assert supervisor.value['state']=='recovering'


def test_redirect_chain_keeps_config_navigation_association(fixture):
    supervisor,_,_,_=fixture
    supervisor.start(); supervisor.probe(); supervisor.probe()
    event(supervisor,'Network.requestWillBeSent',type='Document',frameId='main',loaderId=supervisor.loader,
          requestId='redirect',request={'url':'https://example.com/redirected'},redirectResponse={'status':302})
    event(supervisor,'Page.frameNavigated',frame={'id':'main','loaderId':supervisor.loader,'url':'https://example.com/redirected'})
    document(supervisor)
    assert supervisor.value['state']=='healthy' and supervisor.value['target_matches_config']


def test_verified_final_redirect_can_reload_without_restart_or_new_tab(fixture):
    supervisor,_,_,_=fixture
    supervisor.start();supervisor.probe()
    redirected='https://example.com/display/'
    loader=supervisor.loader
    event(supervisor,'Network.requestWillBeSent',type='Document',frameId='main',loaderId=loader,
          requestId='redirect',request={'url':redirected},redirectResponse={'status':301})
    event(supervisor,'Network.responseReceived',type='Document',frameId='main',loaderId=loader,
          response={'status':200,'url':redirected})
    event(supervisor,'Page.frameNavigated',frame={'id':'main','loaderId':loader,'url':redirected})
    event(supervisor,'Page.lifecycleEvent',frameId='main',loaderId=loader,name='load')
    assert supervisor.value['state']=='healthy'
    before=list(supervisor.browser.navigations())
    event(supervisor,'Network.requestWillBeSent',type='Document',frameId='main',loaderId='reload',
          requestId='reloaded',request={'url':redirected})
    event(supervisor,'Network.responseReceived',type='Document',frameId='main',loaderId='reload',response={'status':200})
    event(supervisor,'Page.lifecycleEvent',frameId='main',loaderId='reload',name='load')
    assert supervisor.value['state']=='healthy'
    assert supervisor.browser.navigations()==before
    assert supervisor.allowed_reload_urls=={supervisor.config.url,redirected}
    assert redirected not in health.state_path(supervisor.home).read_text()
    with pytest.raises(cdp.CDPError):
        event(supervisor,'Network.requestWillBeSent',type='Document',frameId='main',loaderId='unrelated',
              requestId='other',request={'url':'https://example.com/unverified'})


def test_subresource_url_cannot_become_an_allowed_main_document(fixture):
    supervisor,_,_,_=fixture
    supervisor.start();supervisor.probe()
    event(supervisor,'Network.responseReceived',type='Image',frameId='main',loaderId=supervisor.loader,
          response={'status':200,'url':'https://example.com/image'})
    document(supervisor)
    assert supervisor.allowed_reload_urls=={supervisor.config.url}
    with pytest.raises(cdp.CDPError):
        event(supervisor,'Network.requestWillBeSent',type='Document',frameId='main',loaderId='new',
              requestId='other',request={'url':'https://example.com/image'})


def test_site_initiated_reload_document_error_is_observed(fixture):
    supervisor,_,_,_=healthy(fixture)
    document(supervisor,404,loader='next-load')
    assert supervisor.value['state']=='recovering'


def test_unexpected_target_or_navigation_is_not_reported_healthy(fixture):
    supervisor,_,_,_=healthy(fixture)
    with pytest.raises(cdp.CDPError):
        event(supervisor,'Network.requestWillBeSent',type='Document',frameId='main',loaderId='foreign',
              requestId='wrong',request={'url':'https://example.org/other'})
    with pytest.raises(cdp.CDPError):
        supervisor.event({'method':'Target.targetDestroyed','params':{'targetId':'owned'}})


@pytest.mark.parametrize('error',[urllib.error.URLError(ssl.SSLError('synthetic TLS error')),
                                 urllib.error.URLError('synthetic DNS error'),TimeoutError()])
def test_http_failure_preserves_tls_and_has_no_sensitive_error_text(fixture,error):
    supervisor,http,_,_=fixture
    http.error=error
    value=health.probe_url(supervisor.config.url,opener=http)
    assert not value.ok and value.status is None
    assert http.calls[0][1]==5
    assert 'synthetic' not in value.reason


def test_http_200_does_not_claim_actual_app_contents_are_healthy(fixture):
    supervisor,_,_,_=fixture
    supervisor.start(); supervisor.probe(); supervisor.probe()
    assert supervisor.value['state']=='recovering'
    document(supervisor)
    assert '앱 내부' in supervisor.value['next_action']


def test_state_is_private_bound_to_pids_boot_and_config_without_url(fixture):
    supervisor,_,clock,paths=healthy(fixture)
    path=health.state_path(supervisor.home)
    assert path.stat().st_mode & 0o777==0o600
    assert supervisor.config.url not in path.read_text()
    value=health.read_health(supervisor.home,paths,os.getpid(),supervisor.config.url,now=clock.now)
    assert value['state']=='healthy'
    assert health.read_health(supervisor.home,paths,os.getpid()+1,supervisor.config.url,now=clock.now)['state']=='unknown'
    assert health.read_health(supervisor.home,paths,os.getpid(),'https://example.com/changed',now=clock.now)['state']=='unknown'
    clock.now+=91
    assert health.read_health(supervisor.home,paths,os.getpid(),supervisor.config.url,now=clock.now)['state']=='unknown'


def test_missing_browser_is_unknown_rather_than_fresh_start_waiting(fixture):
    supervisor,_,clock,paths=healthy(fixture)
    paths.at('/proc/222/stat').unlink()
    value=health.read_health(supervisor.home,paths,os.getpid(),supervisor.config.url,now=clock.now)
    assert value['state']=='unknown'


def test_initial_outage_requires_two_successful_probes_before_first_navigation(fixture):
    supervisor,http,_,_=fixture
    supervisor.start();http.status=503;supervisor.probe()
    http.status=200;supervisor.probe()
    assert supervisor.browser.navigations()==[]
    supervisor.probe()
    assert supervisor.browser.navigations()==[supervisor.config.url]


def test_navigation_load_timeout_falls_back_without_restarting_browser(fixture):
    supervisor,_,clock,_=fixture
    original=supervisor.transport_factory
    ticks=0
    def factory(argv):
        browser=original(argv)
        def next_event(timeout=1):
            nonlocal ticks
            ticks+=1
            clock.now+=21
            if ticks>1:supervisor.stopping=True
            return None
        browser.next_event=next_event
        return browser
    supervisor.transport_factory=factory
    assert supervisor.run()==0
    assert health.RECOVERY_URL in supervisor.browser.navigations()
    assert supervisor.browser.closed


def test_missing_state_is_pending_and_inspection_does_not_create_it(fixture):
    supervisor,_,clock,paths=fixture
    value=health.read_health(supervisor.home,paths,os.getpid(),supervisor.config.url,now=clock.now)
    assert value['state']=='waiting_site'
    assert not health.state_path(supervisor.home).parent.exists()


@pytest.mark.parametrize('alteration',['boot','pid_reuse','permissions','symlink'])
def test_untrusted_or_stale_state_is_unknown(fixture,alteration):
    supervisor,_,clock,paths=healthy(fixture)
    path=health.state_path(supervisor.home)
    if alteration=='boot': paths.at('/proc/sys/kernel/random/boot_id').write_text('different-boot')
    if alteration=='pid_reuse': seed_process(paths,Browser.pid,ticks='456')
    if alteration=='permissions': path.chmod(0o644)
    if alteration=='symlink':
        saved=path.with_suffix('.saved'); path.rename(saved); path.symlink_to(saved)
    assert health.read_health(supervisor.home,paths,os.getpid(),supervisor.config.url,now=clock.now)['state']=='unknown'


def test_stop_closes_only_own_browser_and_does_not_respawn(fixture):
    supervisor,_,_,_=fixture
    supervisor.stop_requested=lambda:True
    assert supervisor.run()==0
    assert supervisor.browser.closed
    assert supervisor.browser.navigations()==[]


@pytest.mark.parametrize('problem',['process_exit','pipe','navigate_timeout'])
def test_runtime_control_failure_is_visible_and_closes_child(fixture,problem):
    supervisor,_,clock,_=fixture
    original=supervisor.transport_factory
    def factory(argv):
        browser=original(argv)
        if problem=='process_exit': browser.code=7
        if problem=='pipe':
            def fail(*args,**kwargs): raise cdp.CDPError('브라우저 제어 pipe 연결이 종료되었습니다.')
            browser.next_event=fail
        if problem=='navigate_timeout':
            def call(method,*args,**kwargs): raise cdp.CDPError('브라우저 제어 명령 응답 시간이 초과되었습니다.')
            browser.call=call
        return browser
    supervisor.transport_factory=factory
    assert supervisor.run()==1
    assert supervisor.value['state']=='failed' and supervisor.browser.closed


def transport_pair():
    transport=object.__new__(cdp.PipeBrowser)
    incoming_r,incoming_w=os.pipe(); outgoing_r,outgoing_w=os.pipe()
    transport._read=incoming_r; transport._write=outgoing_w
    transport._buffer=bytearray(); transport._events=deque(); transport._serial=0
    transport.pid=None; transport.returncode=0
    return transport,incoming_w,outgoing_r


def test_cdp_json_nul_frames_keep_events_and_request_ids():
    transport,incoming,outgoing=transport_pair()
    try:
        os.write(incoming,b'{"method":"Page.event","params":{}}\0{"id":1,"result":{"ready":true}}\0')
        assert transport.call('Page.enable',session_id='owned')=={'ready':True}
        packet=os.read(outgoing,4096)
        assert packet.endswith(b'\0') and json.loads(packet[:-1])['sessionId']=='owned'
        assert transport.next_event()['method']=='Page.event'
    finally:
        transport.close(); os.close(incoming); os.close(outgoing)


@pytest.mark.parametrize('packet',[b'not-json\0',b'[]\0',b'x'*70,b'"'+b'x'*70+b'"\0'])
def test_cdp_rejects_malformed_and_oversized_complete_or_partial_packets(packet):
    transport,incoming,outgoing=transport_pair(); transport.MAX_MESSAGE=64
    try:
        os.write(incoming,packet)
        with pytest.raises(cdp.CDPError): transport.next_event(timeout=0.1)
    finally:
        transport.close(); os.close(incoming); os.close(outgoing)


def test_cdp_eof_and_command_timeout_are_errors():
    transport,incoming,outgoing=transport_pair()
    try:
        with pytest.raises(cdp.CDPError,match='시간'): transport.call('Page.enable',timeout=0.001)
        os.close(incoming); incoming=None
        with pytest.raises(cdp.CDPError,match='종료'): transport.next_event()
    finally:
        transport.close()
        if incoming is not None: os.close(incoming)
        os.close(outgoing)


def test_close_signals_and_reaps_only_the_child_it_spawned(monkeypatch):
    transport,incoming,outgoing=transport_pair()
    transport.pid=456;transport.returncode=None
    waits=iter([(0,0),(456,0)])
    kills=[]
    monkeypatch.setattr(cdp.os,'waitpid',lambda pid,options:next(waits))
    monkeypatch.setattr(cdp.os,'kill',lambda pid,signum:kills.append((pid,signum)))
    try:
        transport.close()
        assert kills==[(456,cdp.signal.SIGTERM)]
    finally:
        os.close(incoming);os.close(outgoing)


def test_spawn_maps_high_private_fds_to_three_and_four_without_preexec():
    calls=[]
    def spawn(executable,argv,env,*,file_actions):
        calls.append((executable,argv,file_actions))
        mapping=[action for action in file_actions if action[0]==os.POSIX_SPAWN_DUP2]
        assert [action[2] for action in mapping]==[3,4]
        assert all(action[1]>=10 for action in mapping)
        for _,fd,_ in mapping: os.fstat(fd)
        return 456
    transport=cdp.PipeBrowser(['/usr/bin/google-chrome-stable','--remote-debugging-pipe'],spawn=spawn)
    transport.pid=None
    transport.close()
    assert len(calls)==1
    assert not any('remote-debugging-port' in arg for arg in calls[0][1])
