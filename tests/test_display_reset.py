"""DID reset orchestration uses synthetic releases and no real system changes."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
from test_screen_swap import fixture as base_fixture, control_for
from ala import display_reset as reset, runtime, screens
from ala.config import Config
from ala.system import Command


@pytest.fixture
def fixture(base_fixture,monkeypatch):
    paths,host,clock,store,config,topology,releases,active=base_fixture
    topology['capabilities']=['workspace-placement']
    topology['logical_outputs'][0]['primary']=True
    for release in releases:
        file=release/'COMPATIBILITY.json'
        data=json.loads(file.read_text())
        data.update(workspace_protocol=1,display_reset_protocol=1,extension_protocol=1,frontend_protocol=1)
        file.write_text(json.dumps(data))
    monkeypatch.setattr(screens,'inspect_session',lambda *_:'ok')
    monkeypatch.setattr(runtime,'_observed_release_matches',lambda *_:True)
    original=host.run
    def run(args,**kwargs):
        result=original(args,**kwargs)
        if args[:2]==['systemctl','show'] and '--value' in args:
            return Command(result.returncode,result.stdout.removeprefix('ActiveState='))
        return result
    host.run=run
    return base_fixture


def submit(fixture):
    paths,host,_,_,config,_,releases,_=fixture
    plan=reset.preview(config,release=releases[1],paths=paths,host=host)
    assert plan['state']=='ready',plan
    return reset.submit(plan['review'],release=releases[1],paths=paths,host=host)


def controller(fixture,hook=None):
    old,calls=control_for(fixture)
    def control(store,target,action,selection,**kwargs):
        if hook:
            hook(store,target,action,selection)
        # Reset has new IDs, whose synthetic service starts inactive.
        fixture[-1].setdefault(target,False)
        # Base swap helper expects only its two original output IDs for starts.
        model=Config.from_dict(store.runtime._validate_selection(selection)).for_screen(target)
        calls.append((target,action,model.output))
        active=fixture[-1]
        if action=='stop':
            active[target]=False
            return {'state':'stopped','exit_code':0}
        if action=='start':
            active[target]=True
            return {'state':'starting','exit_code':2}
        if action=='check':
            return {'service':{'ActiveState':'active' if active[target] else 'inactive'},
                    'health':{'state':'waiting_configuration' if model.url is None else 'healthy',
                              'supervisor_version':selection['version'],'config_revision':selection['revision'],
                              'browser_pid':123,'local_page_ready':True},'placement':{'state':'ok'},'results':[]}
        raise AssertionError(action)
    return control,calls


def execute(fixture,operation_id,**kwargs):
    paths,host,clock,*_=fixture
    return reset.execute(operation_id,paths=paths,host=host,monotonic=clock,sleep=clock.sleep,**kwargs)


def test_preview_has_no_files_or_mutations(fixture):
    paths,host,_,_,config,_,releases,_=fixture
    before={str(p):p.read_bytes() for p in paths.root.rglob('*') if p.is_file()}
    host.calls=[]
    plan=reset.preview(config,release=releases[1],paths=paths,host=host)
    assert plan['state']=='ready'
    assert {str(p):p.read_bytes() for p in paths.root.rglob('*') if p.is_file()}==before
    assert not any(c[0]=='systemd-run' or c[:2]==['systemctl','enable'] for c in host.calls)
    assert all(t['url'] is None and t['autostart'] and not t['frontend']['enabled'] for t in plan['after'])


def test_reset_backs_up_and_retires_old_targets_preserving_code_versions(fixture):
    paths,host,_,store,before,_,_,_=fixture
    original={s['target_id']:s for s in store.selections()}
    request=submit(fixture)
    private=reset.Store(paths,host).path(request['operation_id'])
    assert private.stat().st_mode & 0o777==0o600
    record=reset.Store(paths,host).load(request['operation_id'])
    assert record['backup']['config']==before.to_dict()
    with pytest.raises(RuntimeError):
        reset.user_guard('default',paths=paths)
    control,calls=controller(fixture)
    result=execute(fixture,request['operation_id'],control=control)
    assert result['state']=='completed',result
    after=Config.load(paths.config)
    assert len(after.screen_targets())==2
    assert all(t.url is None and t.autostart for t in after.screen_targets())
    assert after.user==before.user and after.ssh==before.ssh
    assert store.load('default')['committed']['version']==original['default']['committed']['version']
    assert store.load('default')['desired']['version']==original['default']['desired']['version']
    with pytest.raises(RuntimeError):
        reset.user_guard('other',paths=paths)
    reset.user_guard('default',paths=paths)
    assert reset.Store(paths,host).load(request['operation_id'])['backup']['states']==original


@pytest.mark.parametrize('boundary',['stop','materialize'])
def test_interruption_recovers_by_durable_decision(fixture,monkeypatch,boundary):
    request=submit(fixture)
    original=reset._materialize
    def crash(*args,**kwargs):
        raise KeyboardInterrupt()
    def hook(store,target,action,selection):
        if boundary=='stop' and action=='stop':
            raise KeyboardInterrupt()
    control,calls=controller(fixture,hook)
    if boundary=='materialize':
        monkeypatch.setattr(reset,'_materialize',crash)
    with pytest.raises(KeyboardInterrupt):
        execute(fixture,request['operation_id'],control=control)
    monkeypatch.setattr(reset,'_materialize',original)
    paths,host,_,_,before,_,_,_=fixture
    journal=reset.Store(paths,host).load(request['operation_id'])
    assert journal['decision']==('commit' if boundary=='materialize' else None)
    paths.at('/proc/sys/kernel/random/boot_id').write_text('next-synthetic-boot')
    monkeypatch.setattr(screens,'inspect_session',lambda *_:'pending')
    control,calls=controller(fixture)
    waiting=execute(fixture,request['operation_id'],control=control,recovering=True)
    assert waiting['state']=='queued' and waiting['phase']=='waiting_session'
    assert not [c for c in calls if c[1]=='start']
    assert reset.read_fence(paths)['pending']
    monkeypatch.setattr(screens,'inspect_session',lambda *_:'ok')
    result=execute(fixture,request['operation_id'],control=control,recovering=True)
    assert result['state']==('completed' if boundary=='materialize' else 'rolled_back'),result
    after=Config.load(paths.config)
    assert (all(t.url is None for t in after.screen_targets())) if boundary=='materialize' else after==before
    assert [c for c in calls if c[1]=='start']
    assert not reset.read_fence(paths)['pending']


def test_new_manual_stop_before_commit_cancels_without_restarting_target(fixture):
    request=submit(fixture)
    once=False
    def hook(store,target,action,selection):
        nonlocal once
        if action=='stop' and not once:
            once=True
            from ala.jobs import change_lock
            with change_lock(store.paths):
                store.runtime.cancel('default',stopped=True)
    control,calls=controller(fixture,hook)
    result=execute(fixture,request['operation_id'],control=control)
    assert result['state']=='rolled_back',result
    assert not any(t=='default' and action=='start' for t,action,_ in calls)
    assert fixture[3].is_stopped(fixture[3].load('default'))


def test_stale_review_and_missing_capability_do_not_stop_windows(fixture):
    paths,host,_,store,config,_,releases,_=fixture
    review=reset.preview(config,release=releases[1],paths=paths,host=host)['review']
    store.cancel('default',stopped=True)
    with pytest.raises(RuntimeError):
        reset.submit(review,release=releases[1],paths=paths,host=host)
    assert not reset.Store(paths,host).records()
    file=releases[0]/'COMPATIBILITY.json'
    data=json.loads(file.read_text());data.pop('display_reset_protocol');file.write_text(json.dumps(data))
    assert reset.preview(config,release=releases[1],paths=paths,host=host)['state']=='blocked'


@pytest.mark.parametrize('when',['before','after'])
def test_partial_materialization_is_resumed_after_reboot(fixture,monkeypatch,when):
    request=submit(fixture)
    paths,host,_,runtime_store,*_=fixture
    original=runtime_store._write.__func__
    crashed=False
    def write(self,path,value):
        nonlocal crashed
        is_target='/targets/' in str(path) and path.name=='state.json'
        if is_target and not crashed:
            crashed=True
            if when=='after':
                original(self,path,value)
            raise KeyboardInterrupt()
        original(self,path,value)
    monkeypatch.setattr(runtime.RuntimeStore,'_write',write)
    control,_=controller(fixture)
    with pytest.raises(KeyboardInterrupt):
        execute(fixture,request['operation_id'],control=control)
    monkeypatch.setattr(runtime.RuntimeStore,'_write',original)
    paths.at('/proc/sys/kernel/random/boot_id').write_text('next-synthetic-boot')
    result=execute(fixture,request['operation_id'],control=control,recovering=True)
    assert result['state']=='completed',result
    model=Config.load(paths.config)
    for screen in model.screen_targets():
        selected=runtime_store.load(screen.id)
        for key in ('committed','desired'):
            saved=Config.from_dict(runtime_store._validate_selection(selected[key]))
            assert saved.for_screen(screen.id).url is None
    assert 'other' not in {s['target_id'] for s in runtime_store.selections()}
    assert 'other' in {s['target_id'] for s in runtime_store.selections(include_retired=True)}


def test_failed_backup_verification_prevents_lifecycle(fixture,monkeypatch):
    paths,host,_,_,config,_,releases,_=fixture
    review=reset.preview(config,release=releases[1],paths=paths,host=host)['review']
    monkeypatch.setattr(reset.Store,'load',lambda *_: (_ for _ in ()).throw(ValueError('synthetic backup failure')))
    host.calls=[]
    with pytest.raises(ValueError):
        reset.submit(review,release=releases[1],paths=paths,host=host)
    assert not any(c[0]=='systemd-run' or c[:2]==['systemctl','stop'] for c in host.calls)
    assert Config.load(paths.config)==config


def test_failed_stop_quarantines_until_explicit_recovery(fixture):
    request=submit(fixture)
    control,calls=controller(fixture)
    def fail(store,target,action,selection,**kwargs):
        if action=='stop' and target=='other':
            return {'state':'unknown','exit_code':1}
        return control(store,target,action,selection,**kwargs)
    result=execute(fixture,request['operation_id'],control=fail)
    assert result['state']=='recovery_required'
    assert result['decision']=='rollback'
    assert Config.load(fixture[0].config)==fixture[4]
    assert reset.read_fence(fixture[0])['pending']
    assert not any(action=='start' for _,action,_ in calls)
    assert reset.recover_all(paths=fixture[0],host=fixture[1])==1


def test_stop_after_commit_is_preserved_by_recovery(fixture,monkeypatch):
    request=submit(fixture)
    original=reset._materialize
    def interrupt(store,value,after):
        if after:
            raise KeyboardInterrupt()
        return original(store,value,after)
    monkeypatch.setattr(reset,'_materialize',interrupt)
    control,_=controller(fixture)
    with pytest.raises(KeyboardInterrupt):
        execute(fixture,request['operation_id'],control=control)
    monkeypatch.setattr(reset,'_materialize',original)
    from ala.jobs import change_lock
    with change_lock(fixture[0]):
        fixture[3].cancel('default',stopped=True)
    fixture[0].at('/proc/sys/kernel/random/boot_id').write_text('next-synthetic-boot')
    result=execute(fixture,request['operation_id'],control=control,recovering=True)
    assert result['state']=='completed'
    assert fixture[3].is_stopped(fixture[3].load('default'))


def test_corrupt_backup_remains_fenced(fixture):
    request=submit(fixture)
    store=reset.Store(fixture[0],fixture[1])
    path=store.path(request['operation_id'])
    record=json.loads(path.read_text())
    record['backup']['config']['timezone']='UTC'
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError):
        store.load(request['operation_id'])
    with pytest.raises(RuntimeError):
        reset.user_guard('default',paths=fixture[0])
    with pytest.raises(ValueError):
        reset.guard(paths=fixture[0],host=fixture[1])


def test_registration_bytes_and_profile_data_are_backed_up_or_preserved(fixture):
    from ala import display
    paths,host,_,_,config,_,releases,_=fixture
    unit=paths.at('/etc/systemd/user/'+display.unit_name())
    unit.parent.mkdir(parents=True)
    unit.write_text(display.unit_text()+'\n# synthetic customization\n')
    desktop=paths.at('/home/user/.config/autostart/ala-display.desktop')
    desktop.parent.mkdir(parents=True)
    desktop.write_text(display.desktop_text())
    profile=paths.at('/home/user/.config/ala-display/Default/synthetic-profile')
    profile.parent.mkdir(parents=True)
    profile.write_text('synthetic profile data')
    unit_before,desktop_before=unit.read_text(),desktop.read_text()
    request=submit(fixture)
    backup=reset.Store(paths,host).load(request['operation_id'])['backup']['registrations']
    assert backup[display.unit_name()]==unit_before
    assert backup['autostart']==desktop_before
    control,_=controller(fixture)
    assert execute(fixture,request['operation_id'],control=control)['state']=='completed'
    assert profile.read_text()=='synthetic profile data'


@pytest.mark.parametrize('kind',['update','reload'])
def test_queued_other_worker_blocks_reset_preview(fixture,monkeypatch,kind):
    from ala import update,screen_control
    paths,host,_,_,config,_,releases,_=fixture
    if kind=='update':
        monkeypatch.setattr(update,'status',lambda **_: {'state':'queued'})
    else:
        monkeypatch.setattr(screen_control,'read_status',lambda *a,**_: {'state':'queued'})
    assert reset.preview(config,release=releases[1],paths=paths,host=host)['state']=='blocked'
    assert not reset.Store(paths,host).records()


def test_update_compatibility_requires_reset_support_when_fence_exists(fixture,tmp_path):
    from ala.update import check_bundle_compatibility
    request=submit(fixture)
    paths,host,_,_,_,_,releases,_=fixture
    metadata=releases[1]/'COMPATIBILITY.json'
    value=json.loads(metadata.read_text());value.pop('display_reset_protocol')
    metadata.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='초기화 보호'):
        check_bundle_compatibility(releases[1],paths=paths,host=host)


def test_second_worker_cannot_claim_same_operation(fixture):
    request=submit(fixture)
    control,_=controller(fixture)
    once=False
    def nested(store,target,action,selection,**kwargs):
        nonlocal once
        if not once:
            once=True
            with pytest.raises(RuntimeError,match='복구'):
                execute(fixture,request['operation_id'],control=control)
        return control(store,target,action,selection,**kwargs)
    assert execute(fixture,request['operation_id'],control=nested)['state']=='completed'


def test_manual_retry_after_failed_recovery_clears_stale_owner(fixture):
    request=submit(fixture)
    paths,host,_,_,_,_,releases,_=fixture
    store=reset.Store(paths,host)
    value=store.load(request['operation_id'])
    host.units.discard(value['unit'])
    value.update(state='recovery_required',owner_unit='ala-display-reset-recover.service',
                 owner_boot_id=value['boot_id'],automatic_recovery_attempted=True)
    store.save(value)
    reset.recover(value['operation_id'],release=releases[1],paths=paths,host=host)
    latest=store.load(value['operation_id'])
    assert 'owner_unit' not in latest and 'automatic_recovery_attempted' not in latest
    control,_=controller(fixture)
    assert execute(fixture,value['operation_id'],control=control,recovering=True)['state']=='rolled_back'


def test_boot_session_wait_is_bounded_and_does_not_start_before_login(fixture,monkeypatch):
    request=submit(fixture)
    paths,host,clock,_,_,_,_,_=fixture
    paths.at('/proc/sys/kernel/random/boot_id').write_text('next-synthetic-boot')
    monkeypatch.setattr(screens,'inspect_session',lambda *_:'pending')
    control,calls=controller(fixture)
    result=execute(fixture,request['operation_id'],control=control,recovering=True)
    assert result['phase']=='waiting_session'
    assert not any(action=='start' for _,action,_ in calls)
    clock.value+=601
    result=execute(fixture,request['operation_id'],control=control,recovering=True)
    assert result['state']=='recovery_required' and result['phase']=='waiting_session_expired'
    assert reset.read_fence(paths)['pending']
    assert reset.recover_all(paths=paths,host=host)==1
    assert reset.status(paths=paths,host=host,operation_id=request['operation_id'])['operation_id']==request['operation_id']


def test_original_running_target_with_autostart_off_is_resumed_after_boot(fixture,monkeypatch):
    from dataclasses import replace
    from ala.system import atomic_json
    paths,host,_,store,config,_,_,_=fixture
    config=replace(config,screens=tuple(replace(s,autostart=False) for s in config.screen_targets()))
    atomic_json(paths.config,config.to_dict(),0o644)
    for target in config.screen_targets():
        state=store.load(target.id)
        for key in ('committed','desired'):
            state[key]=store.selection(state[key]['release'],config)
        store._write(store.state_path(target.id),state)
    # Fixture's saved model is replaced too; both services were manually running.
    fixture=(*fixture[:4],config,*fixture[5:])
    request=submit(fixture)
    paths.at('/proc/sys/kernel/random/boot_id').write_text('next-synthetic-boot')
    monkeypatch.setattr(screens,'inspect_session',lambda *_:'pending')
    control,calls=controller(fixture)
    assert execute(fixture,request['operation_id'],control=control,recovering=True)['phase']=='waiting_session'
    monkeypatch.setattr(screens,'inspect_session',lambda *_:'ok')
    assert execute(fixture,request['operation_id'],control=control,recovering=True)['state']=='rolled_back'
    assert {target for target,action,_ in calls if action=='start'}=={'default','other'}
    assert all(not t.autostart for t in Config.load(paths.config).screen_targets())


def test_live_worker_is_not_recovered_or_its_timer_disabled(fixture):
    request=submit(fixture)
    host=fixture[1]
    host.calls=[]
    assert reset.recover_all(paths=fixture[0],host=host)==0
    assert not any(c[:3]==['systemctl','disable','--now'] for c in host.calls)
    assert reset.Store(fixture[0],host).load(request['operation_id'])['state']=='queued'


def test_unknown_worker_state_is_not_claimed_as_running(fixture):
    request=submit(fixture)
    fixture[1].query_code=1
    report=reset.status(paths=fixture[0],host=fixture[1])
    assert report['state']=='unknown'
    with pytest.raises(RuntimeError):
        reset.recover(request['operation_id'],release=fixture[6][1],paths=fixture[0],host=fixture[1])


def test_timer_disable_failure_does_not_undo_committed_configuration(fixture,monkeypatch):
    request=submit(fixture)
    monkeypatch.setattr(reset,'_disable_retry',lambda *_:False)
    control,_=controller(fixture)
    result=execute(fixture,request['operation_id'],control=control)
    assert result['state']=='completed' and result['decision']=='commit'
    assert result['schedule_warning']
    assert all(s.url is None for s in Config.load(fixture[0].config).screen_targets())
