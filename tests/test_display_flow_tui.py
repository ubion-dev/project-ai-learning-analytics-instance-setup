import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from textual.widgets import Button, Checkbox, DataTable

from ala.quick_tui import diagnosis
from ala.screens_tui import MoveConfirmation, ScreenPanel
from ala.screen_map import ScreenMap
from ala.simple_tui import SimpleApp, DisplayInitializationConfirmation
from test_screen_drag import drag
from test_screens_tui import Service as ScreenService
from test_simple_tui import FakeService


class DisplayService(ScreenService):
    async def call(self, action, payload=None):
        if action in {'screens-swap-submit', 'screens-identify', 'screens-swap-retry'}:
            self.calls.append((action, deepcopy(payload)))
            if action == 'screens-swap-submit':
                assert payload['expected_config'] == self.config
                targets = {target['id']: target for target in self.screens['targets']}
                source, destination = targets[payload['source']], targets[payload['destination']]
                source['output'], destination['output'] = destination['output'], source['output']
                return {'state': 'queued', 'operation_id': 'f' * 32, 'reason': '두 화면 교환 접수'}
            return {'state': 'ok', 'exit_code': 0, 'reason': '식별 표시 요청 완료'}
        return await super().call(action, payload)


@pytest.mark.parametrize('size', [(80, 24), (120, 36)])
@pytest.mark.parametrize('guide', [False, True])
@pytest.mark.parametrize('approved', [False, True])
def test_drag_swaps_one_reviewed_pair_without_sequential_save_apply(size, guide, approved):
    async def scenario():
        service = DisplayService()
        if guide:
            service.screens['targets'][1]['url'] = None
            service.screens['targets'][1]['applied_url'] = None
        before = deepcopy(service.screens['targets'])
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await drag(pilot, app.query_one('#screens-map', ScreenMap))
            assert isinstance(app.screen, MoveConfirmation)
            assert app.screen.other['id'] == 'display-2'
            assert app.focused.id == 'move-cancel'
            assert app.screen.query_one('#move-confirm', Button).region.bottom <= size[1]
            assert not any(action == 'screens-swap-submit' for action, _ in service.calls)
            await pilot.press('tab', 'enter') if approved else await pilot.press('escape')
            await pilot.pause()
            requests = [(action, payload) for action, payload in service.calls
                        if action in {'screens-swap-submit', 'screens-save', 'screens-apply', 'screens-action'}]
            if approved:
                assert len(requests) == 1 and requests[0][0] == 'screens-swap-submit'
                assert requests[0][1]['source'] == 'default'
                assert requests[0][1]['destination'] == 'display-2'
                assert app.query_one('#screens', ScreenPanel).selected == 'default'
                for old, new in zip(before, service.screens['targets']):
                    assert {key: value for key, value in old.items() if key != 'output'} == {
                        key: value for key, value in new.items() if key != 'output'}
            else:
                assert requests == []
                assert service.screens['targets'] == before
    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(80, 24), (120, 36)])
def test_identify_is_explicit_and_does_not_change_sites_or_windows(size):
    async def scenario():
        service = DisplayService()
        before = deepcopy(service.screens)
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            button = app.query_one('#screens-identify', Button)
            assert app.query_one('#screens-table', DataTable).content_size.height >= 2
            assert button.region.bottom <= size[1] - 1
            assert not any(action == 'screens-identify' for action, _ in service.calls)
            button.focus()
            await pilot.press('enter')
            await pilot.pause()
            assert sum(action == 'screens-identify' for action, _ in service.calls) == 1
            assert not any(action in {'screens-save', 'screens-apply', 'screens-action'} for action, _ in service.calls)
            assert service.screens == before
    asyncio.run(scenario())


@pytest.mark.parametrize('approved', [False, True])
def test_new_configuration_preselects_initialization_and_reviews_before_submit(approved):
    class InitialService(FakeService):
        async def call(self, action, payload=None):
            if action == 'config-show':
                return {'config': deepcopy(self.config), 'exists': False}
            if action == 'display-initialization-preview':
                return {'state': 'ready', 'targets': [{'id': 'default', 'output': 'HDMI-1'}]}
            if action == 'submit':
                self.calls.append((action, deepcopy(payload)))
                assert payload == {'expected_config': self.config, 'initialize_displays': True}
                return {'run_id': 'initial-run', 'state': 'queued', 'stages': []}
            return await super().call(action, payload)

    async def scenario():
        service = InitialService()
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert app.query_one('#setup-initialize-displays', Checkbox).value is True
            await pilot.press('enter')
            await pilot.pause()
            assert isinstance(app.screen, DisplayInitializationConfirmation)
            assert app.focused.id == 'initialization-cancel'
            assert not any(action == 'submit' for action, _ in service.calls)
            if approved:
                app.screen.query_one('#initialization-confirm', Button).focus()
                await pilot.press('enter')
            else:
                await pilot.press('escape')
            await pilot.pause()
            assert sum(action == 'submit' for action, _ in service.calls) == int(approved)
            if approved:
                assert not app.query_one('#setup-initialize-displays', Checkbox).value
    asyncio.run(scenario())


def test_guide_triage_keeps_actual_guide_separate_from_pending_site_settings():
    target = ScreenService().target('default', 'HDMI-1')
    target.update(applied_url=None, runtime={'committed': {'revision': 'current'}},
                  health={'state': 'waiting_configuration', 'local_page_ready': True, 'config_revision': 'current'})
    reason, action, stages, _, button, _ = diagnosis({'session': 'ok'}, target)
    assert reason == '설정 안내 표시 중' and button == 'manage'
    assert '사이트 주소 설정 · 점검 필요' in stages
    target['settings'] = {'pending': True}
    assert '저장한 설정을 적용' in diagnosis({'session': 'ok'}, target)[1]
    target['health']['config_revision'] = 'stale'
    assert diagnosis({'session': 'ok'}, target)[0] == '설정 안내 표시 확인 필요'
    target['settings'] = {}
    panel = ScreenPanel(SimpleNamespace(_active_job=None))
    panel.report = {'output_state': 'ok'}
    assert panel.priority(target)[0] == '설정 안내 표시 확인 필요'
    target['health']['config_revision'] = 'current'
    assert panel.priority(target)[0] == '설정 안내 표시 중'


def test_site_configuration_clears_an_initialization_choice_that_became_ineligible():
    async def scenario():
        service = FakeService()
        app = SimpleApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            option = app.query_one('#setup-initialize-displays', Checkbox)
            option.value = True
            service.config['url'] = 'https://example.com/display'
            await app.load_state()
            assert option.disabled and not option.value
            app.query_one('#setup-start', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            assert not isinstance(app.screen, DisplayInitializationConfirmation)
            assert sum(action == 'submit' for action, _ in service.calls) == 1
    asyncio.run(scenario())


def test_swap_request_message_tracks_the_persisted_completion_after_poll():
    async def scenario():
        service = DisplayService()
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            panel = app.query_one('#screens', ScreenPanel)
            panel.launches['display-2'] = ('start', None)
            await drag(pilot, app.query_one('#screens-map', ScreenMap))
            await pilot.press('tab', 'enter')
            await pilot.pause()
            panel = app.query_one('#screens', ScreenPanel)
            assert 'display-2' not in panel.launches
            service.screens['targets'][0]['swap'] = {'operation_id': 'f' * 32, 'state': 'completed',
                                                     'phase': 'completed', 'exit_code': 0, 'reason': '두 화면 배정 검증 완료'}
            await panel.refresh_state()
            assert '두 화면 교환 · 완료' in panel.messages['default']
            assert '접수' not in panel.messages['default']
    asyncio.run(scenario())
