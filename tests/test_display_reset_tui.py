import asyncio
from copy import deepcopy
import pytest
from textual.widgets import Button,Collapsible,Static
from ala.simple_tui import SimpleApp
from ala.screens_tui import ScreenPanel,ResetConfirmation
from test_screens_tui import Service


class ResetService(Service):
    def __init__(self):
        super().__init__()
        self.reset={'state':'idle'}
    async def call(self,action,payload=None):
        if action=='screens-reset-preview':
            self.calls.append((action,deepcopy(payload)))
            return {'state':'ready','review':{'fingerprint':'synthetic','expires_at':1},
                    'before':self.screens['targets'],
                    'after':[dict(t,url=None) for t in self.screens['targets']],
                    'removed':[],'manual_stops':['default']}
        if action=='screens-reset-submit':
            self.calls.append((action,deepcopy(payload)))
            self.reset={'state':'queued','phase':'prepared','operation_id':'a'*32,'reason':'초기화 접수'}
            return deepcopy(self.reset)
        result=await super().call(action,payload)
        if action=='screens-inspect':
            result['reset']=deepcopy(self.reset)
        return result


@pytest.mark.parametrize('size',[(80,24),(120,36)])
@pytest.mark.parametrize('approve',[False,True])
def test_reset_review_cancel_default_and_explicit_submit(size,approve):
    async def scenario():
        service=ResetService()
        app=SimpleApp(service=service,initial_section='screens')
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            panel=app.query_one('#screens',ScreenPanel)
            assert not any(a.startswith('screens-reset') for a,_ in service.calls)
            await panel.review_reset()
            await pilot.pause()
            assert isinstance(app.screen,ResetConfirmation)
            cancel=app.screen.query_one('#reset-cancel',Button)
            assert app.focused is cancel
            for id in ('reset-cancel','reset-confirm'):
                button=app.screen.query_one('#'+id,Button)
                assert button.region.y>=0 and button.region.bottom<=size[1]
            if approve:
                app.screen.query_one('#reset-confirm',Button).focus()
                await pilot.press('enter')
            else:
                await pilot.press('escape')
            await pilot.pause()
            submitted=[p for a,p in service.calls if a=='screens-reset-submit']
            assert bool(submitted)==approve
            assert not any(a in {'screens-save','screens-apply','screens-action'} for a,_ in service.calls)
            if approve:
                assert submitted[0]['review']['fingerprint']=='synthetic'
                assert not panel.action_state('start',panel.target())[0]
                assert panel.action_state('logs',panel.target())[0]
                assert 'DID 전체 초기화' in str(panel.query_one('#screens-result',Static).render())
    asyncio.run(scenario())


@pytest.mark.parametrize('size',[(80,24),(120,36)])
def test_reconnected_completed_reset_refreshes_edit_configuration_and_shows_record(size):
    async def scenario():
        service=ResetService()
        service.config['timezone']='UTC'
        service.reset={'state':'completed','phase':'completed','operation_id':'b'*32,
                       'targets':['default'],'reason':'모니터별 안내 준비 완료'}
        app=SimpleApp(service=service,initial_section='screens')
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            panel=app.query_one('#screens',ScreenPanel)
            assert app._config==service.config
            assert panel._reset_config_seen=='b'*32
            tools=panel.query_one('#screens-tools',Collapsible)
            tools.collapsed=False
            await pilot.pause()
            assert 'b'*32 in str(panel.query_one('#screens-reset-status',Static).render())
            refresh=panel.query_one('#screens-refresh',Button)
            assert refresh.region.bottom<=size[1]
            await panel.refresh_state()
            assert not any(a=='screens-reset-submit' for a,_ in service.calls)
    asyncio.run(scenario())
