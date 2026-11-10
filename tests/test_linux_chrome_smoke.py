"""Opt-in real Linux Chrome proc evidence, separate from synthetic unit tests.

Run as an ordinary user inside a network namespace containing only loopback.
The CI step provides that namespace; no sandbox-disabling flag is permitted.
"""
import os
from pathlib import Path
import shutil
import sys

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get('ALA_LINUX_CHROME_SMOKE') != '1',
    reason='Real Linux Chrome smoke is opt-in: ALA_LINUX_CHROME_SMOKE=1.',
)


@pytest.mark.parametrize('preparing', [False, True])
def test_real_linux_offline_setup_image_and_document_without_http(tmp_path, preparing):
    if sys.platform != 'linux':
        pytest.skip('Linux Chrome smoke is unavailable on this OS.')
    require(os.geteuid() != 0, 'Setup image smoke requires a normal user and Chrome sandbox.')
    chrome = shutil.which('google-chrome-stable') or shutil.which('google-chrome')
    require(bool(chrome), 'Setup image smoke requires official Chrome.')
    interfaces = [line.split(':', 1)[0].strip() for line in Path('/proc/self/net/dev').read_text().splitlines()[2:] if ':' in line]
    require(bool(interfaces) and all(name == 'lo' for name in interfaces), 'Setup image smoke requires isolated loopback-only networking.')
    import base64
    import time
    from ala.cdp import PipeBrowser
    from ala import display_health as health
    from ala.config import Config
    browser = None
    supervisor = None
    try:
        argv = [chrome, '--headless=new', '--disable-background-networking', '--disable-component-update',
                '--disable-default-apps', '--disable-extensions', '--disable-sync', '--metrics-recording-only',
                '--disable-dev-shm-usage', '--no-first-run', '--no-default-browser-check', '--password-store=basic',
                '--remote-debugging-pipe', '--user-data-dir=' + str(tmp_path / 'setup-image-profile'), health.RECOVERY_URL]
        supervisor = health.Supervisor(Config(user='user', url=None), tmp_path, argv,
            setup_context_callback=lambda: {'preparing': preparing, 'output': 'DP-1', 'display_number': 1},
            transport_factory=lambda args: PipeBrowser(args, spawn=quiet_spawn))
        supervisor.start()
        browser = supervisor.browser
        deadline = time.monotonic() + 10
        while not supervisor.value.get('local_page_ready') and time.monotonic() < deadline:
            event = browser.next_event(timeout=0.1)
            if event is not None: supervisor.event(event)
        require(supervisor.value.get('local_page_ready') is True, 'Local setup document did not finish loading.')
        tree = browser.call('DOM.getDocument', session_id=supervisor.session)
        root = tree.get('root', {}).get('nodeId')
        image = browser.call('DOM.querySelector', {'nodeId': root, 'selector': 'svg.setup-image'}, session_id=supervisor.session)
        require(bool(image.get('nodeId')), 'Inline monitor setup image was not rendered in the document.')
        frame = browser.call('Page.captureScreenshot', {'format': 'png'}, session_id=supervisor.session)
        data = base64.b64decode(frame.get('data', ''))
        require(data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) > 1000, 'Setup document screenshot was empty.')
        require(supervisor.value['state'] == 'waiting_configuration' and supervisor.value['target_matches_config'] is False,
                'Local setup image must remain configuration-pending, not a healthy site.')
    except Exception:
        pytest.fail('Sandboxed offline setup-image rendering or document verification failed.', pytrace=False)
    finally:
        if browser is not None: browser.close()


def require(condition, message):
    # Do not let assertion introspection publish proc argv or temporary paths.
    if not condition:
        pytest.fail(message, pytrace=False)


def test_real_linux_three_independent_chrome_waiting_windows_survive_one_handoff(tmp_path):
    """Real Chrome concurrency and private profiles, without a GNOME claim."""
    if sys.platform != 'linux': pytest.skip('Linux Chrome smoke is unavailable on this OS.')
    require(os.geteuid() != 0, 'Waiting windows require an ordinary user and sandbox.')
    chrome = shutil.which('google-chrome-stable') or shutil.which('google-chrome')
    require(bool(chrome), 'Waiting windows require official Chrome.')
    interfaces = [line.split(':', 1)[0].strip() for line in Path('/proc/self/net/dev').read_text().splitlines()[2:] if ':' in line]
    require(bool(interfaces) and all(name == 'lo' for name in interfaces), 'Waiting-window smoke requires isolated networking.')
    import time
    from ala.cdp import PipeBrowser
    from ala import display_health as health
    from ala.config import Config
    from ala.screen_coverage import identifier
    supervisors = []
    event_flags = None
    try:
        for output in ('DP-1', 'DP-2', 'DP-3'):
            target = identifier(output)
            profile = tmp_path / '.config/ala/chrome-wait' / target
            argv = [chrome, '--headless=new', '--disable-background-networking', '--disable-component-update',
                    '--disable-default-apps', '--disable-extensions', '--disable-sync', '--metrics-recording-only',
                    '--disable-dev-shm-usage', '--no-first-run', '--no-default-browser-check', '--password-store=basic',
                    '--remote-debugging-pipe', '--user-data-dir=' + str(profile), health.RECOVERY_URL]
            supervisor = health.Supervisor(Config(user='user', url=None), tmp_path, argv, target_id=target,
                setup_context_callback=lambda output=output: {'output': output},
                transport_factory=lambda args: PipeBrowser(args, spawn=quiet_spawn))
            supervisors.append(supervisor)
            supervisor.start()
        deadline = time.monotonic() + 15
        while not all(s.value.get('local_page_ready') for s in supervisors) and time.monotonic() < deadline:
            for supervisor in supervisors:
                event = supervisor.browser.next_event(timeout=0.1)
                if event is not None:
                    if event.get('method') == 'Page.frameNavigated':
                        frame = event.get('params', {}).get('frame', {})
                        event_flags = {'frame_matches': frame.get('id') == supervisor.frame,
                            'url_is_setup': frame.get('url') == supervisor._setup_url,
                            'url_is_launch': frame.get('url') == health.RECOVERY_URL,
                            'loader_matches': frame.get('loaderId') == supervisor.loader,
                            'launch_pending': supervisor._bootstrap_pending}
                    supervisor.event(event)
        require(all(s.value.get('local_page_ready') is True for s in supervisors), 'Every independent waiting document must load.')
        require(len({s.browser.pid for s in supervisors}) == 3, 'Each output must have an independent Chrome process.')
        supervisors[0].browser.close()
        for supervisor in supervisors[1:]:
            require(supervisor.browser.poll() is None, 'Closing one waiting window must preserve other Chrome processes.')
            tree = supervisor.browser.call('DOM.getDocument', session_id=supervisor.session)
            found = supervisor.browser.call('DOM.querySelector', {'nodeId': tree['root']['nodeId'], 'selector': 'svg.setup-image'}, session_id=supervisor.session)
            require(bool(found.get('nodeId')), 'Other output waiting images must remain loaded after handoff.')
    except Exception:
        pytest.fail('Independent sandboxed waiting-window lifecycle verification failed; sanitized event flags: ' + str(event_flags), pytrace=False)
    finally:
        for supervisor in supervisors:
            if supervisor.browser is not None: supervisor.browser.close()


def quiet_spawn(program, arguments, environment, *, file_actions):
    # Chromium sets this before setproctitle_init. Adding a missing environment
    # entry then can break its original contiguous argv/environ layout check.
    # Seed only this child so title rewriting can be exercised without a real
    # desktop bus or D-Bus autolaunch; no user/system configuration is changed.
    child_environment = dict(environment, DBUS_SESSION_BUS_ADDRESS='disabled:')
    # Browser stderr can contain environment/account/profile details. Keep this
    # smoke's public evidence limited to success/failure and the observed format.
    silence = [(os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0o600),
               (os.POSIX_SPAWN_DUP2, 1, 2)]
    return os.posix_spawn(program, arguments, child_environment, file_actions=[*silence, *file_actions])


def test_real_linux_chrome_proc_observation_and_independent_browser_pid(tmp_path):
    if sys.platform != 'linux':
        pytest.skip('Linux /proc smoke is unavailable on this OS; Mac is not Linux validation.')
    require(os.geteuid() != 0, 'The opt-in smoke must run as an ordinary user with Chrome sandbox enabled.')
    chrome = shutil.which('google-chrome-stable') or shutil.which('google-chrome')
    require(bool(chrome), 'Opt-in Linux smoke requires installed official Google Chrome; absence is not a semantic skip.')
    interfaces = [line.split(':', 1)[0].strip()
                  for line in Path('/proc/self/net/dev').read_text().splitlines()[2:] if ':' in line]
    require(bool(interfaces) and all(name == 'lo' for name in interfaces),
            'Opt-in Linux smoke requires an isolated network namespace with no external interfaces.')

    from ala.cdp import PipeBrowser
    from ala.chrome_process import observe_chrome_cmdline

    arguments = [chrome, '--headless=new', '--disable-background-networking',
                 '--disable-component-update', '--disable-default-apps', '--disable-extensions',
                 '--disable-sync', '--metrics-recording-only', '--disable-dev-shm-usage',
                 '--no-first-run', '--no-default-browser-check', '--hide-crash-restore-bubble',
                 '--start-fullscreen', '--password-store=basic', '--remote-debugging-pipe',
                 '--user-data-dir=' + str(tmp_path / 'isolated-profile'), 'about:blank']
    browser = None
    try:
        try:
            browser = PipeBrowser(arguments, spawn=quiet_spawn)
            info = browser.call('SystemInfo.getProcessInfo', timeout=15)
        except Exception:
            pytest.fail('Sandboxed Linux Chrome private CDP process query failed.', pytrace=False)
        processes = info.get('processInfo') if isinstance(info, dict) else None
        require(isinstance(processes, list), 'Browser-level CDP returned no process inventory.')
        browsers = [item for item in processes if isinstance(item, dict) and item.get('type') == 'browser']
        require(len(browsers) == 1, 'CDP did not identify exactly one browser process.')
        pid = browsers[0].get('id')
        require(type(pid) is int and pid > 0 and pid == browser.pid,
                'CDP browser identity differs from the child spawned through the private pipe.')
        try:
            process = Path('/proc') / str(pid)
            executable = (process / 'exe').readlink()
            raw = (process / 'cmdline').read_bytes()
            require(str(executable).removesuffix(' (deleted)') == '/opt/google/chrome/chrome',
                    'Real smoke requires the supported official Google Chrome executable, not a Chromium alternative.')
            observation = observe_chrome_cmdline(raw, executable=executable, expected_arguments=arguments[1:])
        except Exception:
            pytest.fail('Shared proc observer rejected live Linux Chrome process evidence.', pytrace=False)
        require(observation.format == 'linux_title',
                'Live Linux Chrome did not exercise the Linux process-title parser branch required by this smoke.')
        require(all(observation.arguments.count(item) == 1 for item in arguments[1:]),
                'A requested Chrome argument was absent or duplicated in live proc evidence.')
        require(not any(item.split('=', 1)[0] in {'--no-sandbox', '--remote-debugging-port', '--remote-debugging-address'}
                        for item in observation.arguments), 'Live Chrome evidence contains a prohibited launch option.')
        print('Linux Chrome proc observation validated: ' + observation.format)
    finally:
        if browser is not None:
            browser.close()


@pytest.mark.parametrize('page_reload', [False, True])
def test_real_linux_display_probe_and_page_reload_are_distinguished(tmp_path, monkeypatch, page_reload):
    if sys.platform != 'linux':
        pytest.skip('Linux /proc smoke is unavailable on this OS; Mac is not Linux validation.')
    require(os.geteuid() != 0, 'Linux display smoke requires an ordinary sandboxed browser user.')
    interfaces = [line.split(':', 1)[0].strip()
                  for line in Path('/proc/self/net/dev').read_text().splitlines()[2:] if ':' in line]
    require(bool(interfaces) and all(name == 'lo' for name in interfaces),
            'Linux display smoke requires an isolated loopback-only network namespace.')
    chrome = shutil.which('google-chrome-stable') or shutil.which('google-chrome')
    require(bool(chrome), 'Linux display smoke requires official Google Chrome.')

    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from ala import display_health as health
    from ala.cdp import PipeBrowser
    from ala.config import Config

    requests = {'probe': 0, 'document': 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != '/display':
                self.send_response(204)
                self.end_headers()
                return
            kind = 'probe' if self.headers.get('User-Agent', '').startswith('ala-display-readiness/') else 'document'
            requests[kind] += 1
            script = '<script>setTimeout(()=>location.reload(),400)</script>' if page_reload else ''
            body = ('<!doctype html><title>synthetic display</title><p>display</p>' + script).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        url = 'http://127.0.0.1:' + str(server.server_port) + '/display'
        arguments = [chrome, '--headless=new', '--disable-background-networking',
                     '--disable-component-update', '--disable-default-apps', '--disable-extensions',
                     '--disable-sync', '--metrics-recording-only', '--no-first-run',
                     '--no-default-browser-check', '--password-store=basic', '--remote-debugging-pipe',
                     '--user-data-dir=' + str(tmp_path / 'display-profile'), health.RECOVERY_URL]
        hard_stop = time.monotonic() + 20
        healthy_stop = [None]
        supervisor = health.Supervisor(Config(user='user', url=url), tmp_path, arguments,
            transport_factory=lambda argv: PipeBrowser(argv, spawn=quiet_spawn),
            stop_requested=lambda: time.monotonic() >= hard_stop or
                (healthy_stop[0] is not None and time.monotonic() >= healthy_stop[0]))
        save = supervisor._save

        def observed_save():
            if supervisor.value['state'] == 'healthy' and healthy_stop[0] is None:
                healthy_stop[0] = time.monotonic() + 1.5
            save()

        supervisor._save = observed_save
        monkeypatch.setattr(health, 'INTERVAL', 0.2)
        code = supervisor.run()
        require(code == 0 and healthy_stop[0] is not None,
                'Linux display supervisor did not reach healthy state and stop cleanly.')
        navigation = supervisor.value.get('navigation') or {}
        require(requests['probe'] >= 3, 'Linux display smoke did not perform repeated real HTTP probes.')
        require(navigation.get('display_requests') == 1 and navigation.get('recovery_requests') == 0,
                'Healthy real HTTP probes unexpectedly requested another display navigation.')
        if page_reload:
            require(requests['document'] >= 2 and navigation.get('browser_navigations', 0) >= 1,
                    'Browser-initiated reloads were not observed independently of ala commands.')
        else:
            require(requests['document'] == 1 and navigation.get('browser_navigations') == 0,
                    'Static display was unexpectedly requested or counted as reloading.')
        print('Linux display reload attribution validated: ' + ('browser_reload' if page_reload else 'static'))
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_real_linux_frontend_manifest_private_reload_and_readiness_postconditions(tmp_path):
    if sys.platform != 'linux':
        pytest.skip('Linux frontend smoke is unavailable on this OS; Mac is not Linux validation.')
    require(os.geteuid() != 0, 'Linux frontend smoke requires an ordinary sandboxed browser user.')
    interfaces = [line.split(':', 1)[0].strip()
                  for line in Path('/proc/self/net/dev').read_text().splitlines()[2:] if ':' in line]
    require(bool(interfaces) and all(name == 'lo' for name in interfaces),
            'Linux frontend smoke requires an isolated loopback-only network namespace.')
    chrome = shutil.which('google-chrome-stable') or shutil.which('google-chrome')
    require(bool(chrome), 'Linux frontend smoke requires official Google Chrome.')

    import json
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from ala import display_health as health, frontend
    from ala.cdp import PipeBrowser
    from ala.config import Config

    deployment = {'schema_version': 1, 'deployment_seq': 1, 'build_id': 'build-a', 'ready': True}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/deployment.json':
                body, kind = json.dumps(deployment).encode(), 'application/json'
            else:
                body = ('<!doctype html><meta name="ala-build-id" content="' + deployment['build_id'] + '">'
                        '<meta name="ala-app-ready" content="false"><p>synthetic display</p>'
                        '<script>setTimeout(()=>document.querySelector(\'meta[name="ala-app-ready"]\').content="true",200)</script>').encode()
                kind = 'text/html'
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    supervisor = None
    try:
        origin = 'http://127.0.0.1:' + str(server.server_port)
        manifest_url = origin + '/deployment.json'
        config = Config(user='user', url=origin + '/display')
        arguments = [chrome, '--headless=new', '--disable-background-networking',
                     '--disable-component-update', '--disable-default-apps', '--disable-extensions',
                     '--disable-sync', '--metrics-recording-only', '--no-first-run',
                     '--no-default-browser-check', '--password-store=basic', '--remote-debugging-pipe',
                     '--user-data-dir=' + str(tmp_path / 'frontend-profile'), health.RECOVERY_URL]
        supervisor = health.Supervisor(config, tmp_path, arguments,
            transport_factory=lambda argv: PipeBrowser(argv, spawn=quiet_spawn),
            config_revision='c' * 64, frontend=frontend.FrontendConfig(True, manifest_url))
        def pump(until):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                event = supervisor.browser.next_event(timeout=0.05)
                if event is not None:
                    supervisor.event(event)
                supervisor.control_tick()
                if until():
                    return
            pytest.fail('Linux frontend document/build/readiness postcondition timed out.', pytrace=False)
        supervisor.start()
        supervisor.probe()
        pump(lambda: supervisor.value['state'] == 'healthy')
        pump(lambda: supervisor.observe_frontend()['app_ready'] is True)
        coordinator = frontend.Coordinator({}, save=lambda _: None)
        def observe(_):
            current = supervisor.observe_frontend()
            return {'running': True, 'build_id': current['build_id'], 'app_ready': current['app_ready']}
        def reload(target, current, token):
            response = frontend.request_reload(target, 'c' * 64, tmp_path,
                expected_build_id=current.build_id, deployment_seq=current.deployment_seq, source='frontend')
            require(response.get('state') == 'pending', 'Linux private reload request was not accepted.')
            pump(lambda: (supervisor.value.get('control') or {}).get('nonce') == response['nonce'] and
                         supervisor.value['control'].get('state') in {'completed', 'failed'})
            state = supervisor.value['control']['state']
            return {'exit_code': 0 if state == 'completed' else 1, 'state': state}
        def poll():
            return coordinator.poll(manifest_url, ['default'], observe=observe,
                reserve=lambda *_: {'revision': 'c' * 64}, check=lambda _: True,
                reload=reload, finish=lambda *_: None)
        poll()
        require(poll()['state'] == 'completed' and supervisor.value['navigation']['display_requests'] == 1,
                'An already loaded ready build was unnecessarily navigated.')
        # A SPA may change only its fragment. The following explicit deployment
        # control must still load one new document rather than accept that
        # same-document move as a reload or crash on its missing loader ID.
        supervisor._call('Runtime.evaluate', {'expression': "location.hash='synthetic-route'"})
        deployment.update(deployment_seq=2, build_id='build-b')
        require(poll()['state'] == 'pending', 'A new deployment did not wait for a second ready observation.')
        require(poll()['state'] == 'completed', 'A new frontend deployment did not satisfy private CDP postconditions.')
        require(supervisor.value['control'].get('build_id') == 'build-b' and supervisor.value['control'].get('app_ready') is True,
                'Linux reload completion did not confirm the requested build and app readiness.')
        for _ in range(3):
            supervisor.probe()
            poll()
        require(supervisor.value['navigation']['display_requests'] == 2 and supervisor.value['navigation']['recovery_requests'] == 0,
                'Repeated deployment checks or healthy HTTP probes caused an extra navigation.')
        print('Linux frontend private reload validated: deployment_once_and_ready')
    except Exception:
        pytest.fail('Linux frontend private reload smoke failed.', pytrace=False)
    finally:
        if supervisor is not None and supervisor.browser is not None:
            supervisor.browser.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
