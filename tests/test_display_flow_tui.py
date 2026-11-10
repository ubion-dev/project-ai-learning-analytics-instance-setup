import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from textual.widgets import Button, Checkbox, Collapsible, Input, OptionList, Select, Switch, TextArea

from ala.quick_tui import diagnosis
from ala.screens_tui import ScreenPanel, TargetChooser, TargetEditor
from ala.screen_map import ScreenMap
from ala.simple_tui import SimpleApp, DisplayInitializationConfirmation
from test_screen_drag import point
from test_screens_tui import Service as ScreenService, open_target
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
def test_identify_is_explicit_and_does_not_change_sites_or_windows(size):
    async def scenario():
        service = DisplayService()
        before = deepcopy(service.screens)
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            button = app.query_one('#screens-identify', Button)
            assert not app.screen.query('#screens-table')
            assert app.query_one('#screens-map').content_size.height >= 2
            assert button.region.bottom <= size[1] - 1
            assert not any(action == 'screens-identify' for action, _ in service.calls)
            button.focus()
            await pilot.press('enter')
            await pilot.pause()
            assert sum(action == 'screens-identify' for action, _ in service.calls) == 1
            assert not any(action in {'screens-save', 'screens-apply', 'screens-action'} for action, _ in service.calls)
            assert service.screens == before
    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(80, 24), (120, 36)])
def test_output_click_opens_only_its_did_without_any_operation(size):
    async def scenario():
        service = DisplayService()
        before = deepcopy(service.screens)
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            map_view = app.query_one('#screens-map', ScreenMap)
            await pilot.click(offset=point(map_view, 'DP-1'))
            await pilot.pause()
            assert isinstance(app.screen, TargetEditor)
            assert app.screen.target_id == 'display-2'
            assert app.screen.query_one('#target-url', Input).value == 'https://example.com/display-2'
            assert app.screen.query_one('#target-operations', Collapsible).collapsed
            assert app.screen.query_one('#target-details', Collapsible).collapsed
            assert not any(action in {'screens-action', 'screens-save', 'screens-apply'} for action, _ in service.calls)
            await pilot.press('escape')
            assert service.screens == before
    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(80, 24), (120, 36)])
def test_empty_output_opens_prefilled_creation_without_adding_a_did(size):
    async def scenario():
        service = DisplayService()
        service.screens['targets'] = service.screens['targets'][:1]
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await pilot.click(offset=point(app.query_one('#screens-map', ScreenMap), 'DP-1'))
            await pilot.pause()
            assert isinstance(app.screen, TargetEditor)
            assert app.screen.target_id is None
            assert app.screen.query_one('#target-output', Select).value == 'DP-1'
            assert app.screen.query_one('#target-autostart', Switch).value is True
            await pilot.press('escape')
            assert len(service.screens['targets']) == 1
            assert not any(action in {'screens-save', 'screens-apply', 'screens-action'} for action, _ in service.calls)
    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(80, 24), (120, 36)])
def test_split_output_requires_selecting_a_did_before_editing(size):
    async def scenario():
        service = DisplayService()
        service.screens['targets'][0]['layout'] = 'left'
        service.screens['targets'][1].update(output='HDMI-1', layout='right')
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await pilot.click(offset=point(app.query_one('#screens-map', ScreenMap), 'HDMI-1'))
            await pilot.pause()
            assert isinstance(app.screen, TargetChooser)
            choices = app.screen.query_one('#target-choices', OptionList)
            assert choices.option_count == 2
            await pilot.press('end', 'enter')
            await pilot.pause()
            assert isinstance(app.screen, TargetEditor)
            assert app.screen.target_id == 'display-2'
            assert app.screen.query_one('#target-layout', Select).value == 'right'
            assert not any(action in {'screens-save', 'screens-apply', 'screens-action'} for action, _ in service.calls)
    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(80, 24), (120, 36)])
def test_disconnected_targets_remain_accessible_through_collapsed_orphan_list(size):
    async def scenario():
        service = DisplayService()
        service.screens.update(logical_outputs=[], output_state='pending', reason='GNOME 확장 연결 대기')
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            section = app.query_one('#screens-unassigned', Collapsible)
            assert section.collapsed
            section.collapsed = False
            await pilot.pause()
            choices = app.query_one('#screens-unassigned-list', OptionList)
            assert choices.option_count == 2
            choices.focus()
            await pilot.press('end', 'enter')
            await pilot.pause()
            assert isinstance(app.screen, TargetEditor)
            assert app.screen.target_id == 'display-2'
            assert app.screen.query_one('#target-output', Select).value == 'DP-1'
            assert not any(action in {'screens-save', 'screens-apply', 'screens-action'} for action, _ in service.calls)
    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(80, 24), (120, 36)])
def test_modal_poll_preserves_unsaved_form_and_log_action_uses_its_fixed_target(size):
    async def scenario():
        service = DisplayService()
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            editor = await open_target(pilot, app, 'display-2', operations=True, details=True)
            draft = 'https://example.com/draft?revision=2#section'
            editor.query_one('#target-url', Input).value = draft
            editor.query_one('#target-layout', Select).value = 'right'
            editor.query_one('#target-autostart', Switch).value = False
            app.query_one('#screens', ScreenPanel).selected = 'default'
            before = sum(action == 'screens-inspect' for action, _ in service.calls)
            service.screens['targets'][1]['service'] = {'ActiveState': 'failed'}
            await pilot.pause(3.2)
            assert sum(action == 'screens-inspect' for action, _ in service.calls) > before
            assert editor.target_id == 'display-2'
            assert editor.query_one('#target-url', Input).value == draft
            assert editor.query_one('#target-layout', Select).value == 'right'
            assert editor.query_one('#target-autostart', Switch).value is False
            assert editor.query_one('#screens-stop', Button).disabled
            logs = editor.query_one('#screens-logs', Button)
            assert not logs.disabled
            logs.focus()
            await pilot.press('enter')
            await pilot.pause()
            requests = [payload for action, payload in service.calls if action == 'screens-action']
            assert requests == [{'target': 'display-2', 'action': 'logs'}]
            assert 'fixed event' in editor.query_one('#screens-detail', TextArea).text
            assert not any(action in {'screens-save', 'screens-apply'} for action, _ in service.calls)
            await pilot.press('escape')
            assert service.screens['targets'][1]['url'] == 'https://example.com/display-2'
    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(80, 24), (120, 36)])
def test_recommended_log_action_is_available_without_opening_operations(size):
    async def scenario():
        service = DisplayService()
        service.screens['targets'][1]['service'] = {'ActiveState': 'failed'}
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            editor = await open_target(pilot, app, 'display-2')
            assert editor.query_one('#target-operations', Collapsible).collapsed
            primary = editor.query_one('#target-primary', Button)
            assert primary.display and str(primary.label) == '로그'
            assert primary.region.bottom <= size[1]
            app.query_one('#screens', ScreenPanel).selected = 'default'
            primary.focus()
            await pilot.press('enter')
            await pilot.pause()
            assert [payload for action, payload in service.calls if action == 'screens-action'] == [
                {'target': 'display-2', 'action': 'logs'}]
            assert not editor.query_one('#target-details', Collapsible).collapsed
            assert 'fixed event' in editor.query_one('#screens-detail', TextArea).text
    asyncio.run(scenario())


def test_configuration_change_after_opening_disables_unreviewed_apply():
    async def scenario():
        service = DisplayService()
        service.screens['targets'][1]['settings'] = {'pending': True, 'state': 'pending'}
        app = SimpleApp(service=service, initial_section='screens')
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            editor = await open_target(pilot, app, 'display-2')
            apply = editor.query_one('#screens-apply', Button)
            assert not apply.disabled
            service.config['timezone'] = 'UTC'
            await app.load_state()
            await app.query_one('#screens', ScreenPanel).refresh_state()
            assert apply.disabled
            assert '구성이 바뀌었습니다' in str(apply.tooltip)
            assert not editor.query_one('#target-primary', Button).display
            await pilot.press('escape')
            assert not any(action in {'screens-save', 'screens-apply'} for action, _ in service.calls)
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
            panel.swap_following['default'] = 'f' * 32
            panel.messages['default'] = '두 화면 교환 · 접수'
            service.screens['targets'][0]['swap'] = {'operation_id': 'f' * 32, 'state': 'completed',
                                                     'phase': 'completed', 'exit_code': 0, 'reason': '두 화면 배정 검증 완료'}
            await panel.refresh_state()
            assert '두 화면 교환 · 완료' in panel.messages['default']
            assert '접수' not in panel.messages['default']
    asyncio.run(scenario())
