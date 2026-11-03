import asyncio
from copy import deepcopy

from textual.widgets import Button, Switch

from ala.automatic_tui import AutomaticScreen
from ala.cli import main
from ala.simple_tui import SimpleApp
from test_screens_tui import Service as BaseService


class Service(BaseService):
    async def call(self, action, payload=None):
        if action.startswith('auto-update-') or action == 'runtime-apply':
            self.calls.append((action, deepcopy(payload)))
            if action == 'auto-update-status':
                return {'state': 'bootstrap_required', 'enabled': False, 'verifier': 'missing_or_invalid'}
            return {'state': 'queued', 'exit_code': 2, 'reason': '작업 접수 · 상태 확인 필요'}
        return await super().call(action, payload)


def test_opening_automatic_controls_never_enrolls_or_confirms_acceptance():
    async def scenario():
        service = Service()
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.push_screen(AutomaticScreen(app))
            await pilot.pause()
            assert not app.screen.query_one('#automatic-accept', Switch).value
            assert app.screen.query_one('#automatic-enable', Button).disabled
            assert not any(a in {'auto-update-enable', 'auto-update-bootstrap', 'runtime-apply'} for a, _ in service.calls)
            for key in ('prepare', 'apply', 'enable', 'disable', 'close', 'refresh'):
                region = app.screen.query_one('#automatic-'+key).region
                assert region.x >= 0 and region.right <= 80 and region.bottom <= 24
            await pilot.press('escape')
            assert len(app.screen_stack) == 1
    asyncio.run(scenario())


def test_explicit_bootstrap_is_detached_and_closing_view_does_not_cancel_it():
    async def scenario():
        service = Service()
        app = SimpleApp(service=service)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            app.push_screen(AutomaticScreen(app))
            await pilot.pause()
            app.screen.query_one('#automatic-prepare', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            assert ('auto-update-bootstrap', {}) in service.calls
            await pilot.press('escape')
            assert not any('cancel' in action for action, _ in service.calls)
    asyncio.run(scenario())


class CliService:
    def __init__(self):
        self.calls = []

    def authenticate(self):
        self.calls.append(('authenticate', None))

    def request(self, action, payload):
        self.calls.append((action, payload))
        return {'exit_code': 2, 'state': 'queued'}


def test_explicit_runtime_apply_and_bootstrap_use_fixed_privileged_actions(capsys):
    for argv, expected in [(['update', '--bootstrap', '--json'], ('auto-update-bootstrap', {})),
                           (['update', '--apply-runtime', 'display-2', '--json'], ('runtime-apply', {'target': 'display-2'})),
                           (['update', '--enable-auto', '--confirm-acceptance', '--json'], ('auto-update-enable', {'accepted': True}))]:
        service = CliService()
        assert main(argv, service=service) == 2
        assert service.calls[-1] == expected
        capsys.readouterr()
