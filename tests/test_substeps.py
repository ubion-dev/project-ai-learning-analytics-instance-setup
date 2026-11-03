from copy import deepcopy

from ala.progress import progress_rows
from ala.substeps import STAGE_TASKS, child_rows, step_ids


def checks(*items, **extra):
    return dict(results=[{'id': identifier, 'state': state, 'actual': state} for identifier, state in items], **extra)


def children(stage, **kwargs):
    rows, current = child_rows(stage, **kwargs)
    return {row['id'].split('.', 1)[1]: row for row in rows}, current


def test_long_runtime_evidence_does_not_push_its_next_action_out_of_view():
    actual = {'runc': {'status': 'synthetic-runtime-metadata-' * 20}}
    report = {'results': [{'id':'gpu.runtime.live', 'state':'pending', 'actual':actual,
                           'next_action':'유지보수 시간에 Docker 재시작 후 검사'}]}
    rows, _ = children('gpu_container', report=report)
    detail = next(row['detail'] for row in rows.values() if 'gpu.runtime.live' in row['detail'])
    assert detail.index('다음:') < detail.index('현재:')
    assert 'synthetic-runtime-metadata-' * 20 in detail
    assert report['results'][0]['actual'] == actual


def test_site_monitoring_has_separate_actual_health_evidence():
    rows, _ = children('chrome', report=checks(('chrome.runtime', 'ok'), ('chrome.health', 'pending')))
    assert rows['session']['state'] == 'ok'
    assert rows['health']['state'] == 'pending' and rows['health']['evidence'] == 'check'
    rows, _ = children('chrome', report=checks(('chrome.runtime', 'ok')))
    assert rows['health']['evidence'] == 'missing'


def test_catalog_is_known_safe_and_every_install_stage_has_details():
    assert len(STAGE_TASKS) == 11
    for parent, tasks in STAGE_TASKS.items():
        assert tasks and len(step_ids(parent)) == len(tasks)
        assert all(task['label'] and task['checks'] is not None for task in tasks)
        assert all(identifier.replace('_', '').isalnum() for identifier in step_ids(parent))
    rows = progress_rows()
    assert all(row['children'] and not row['current_step'] for row in rows)
    assert all(child['state'] == 'unknown' for row in rows for child in row['children'])
    assert all(child['evidence'] == 'missing' for row in rows for child in row['children'])


def test_report_only_explains_actual_settings_without_guessing_running_step():
    rows, current = children('base', report=checks(('base.package.git', 'ok'),
        ('base.package.google-chrome-stable', 'change_required'), ('base.chrome.version', 'pending'),
        ('base.timezone', 'ok'), ('base.service.chronyd.active', 'ok')))
    assert rows['packages']['state'] == 'ok'
    assert rows['chrome']['state'] == 'change_required'
    assert rows['timezone']['state'] == rows['time_sync']['state'] == 'ok'
    assert rows['chrome_policy']['state'] == 'unknown'
    assert current == ''


def test_explicit_signal_selects_only_the_actual_latest_running_child():
    report = checks(('base.package.git', 'change_required'))
    job = {'state': 'running', 'stages': [{'id': 'base', 'state': 'started'}], 'substeps': [
        {'stage': 'base', 'id': 'packages', 'state': 'started'},
        {'stage': 'base', 'id': 'packages', 'state': 'completed'},
        {'stage': 'base', 'id': 'chrome', 'state': 'started'},
    ]}
    original = deepcopy(job)
    parent = next(row for row in progress_rows(report=report, job=job) if row['id'] == 'base')
    rows = {row['id']: row for row in parent['children']}
    assert rows['base.packages']['state'] == 'completed'
    assert rows['base.chrome']['state'] == 'running'
    assert parent['current_step'] == 'Chrome 설치·실행 확인'
    assert rows['base.timezone']['state'] == 'waiting'
    assert job == original


def test_old_jobs_do_not_invent_detail_progress_from_parent():
    job = {'state': 'running', 'stages': [{'id': 'base', 'state': 'started'}]}
    rows, current = children('base', report=checks(('base.timezone', 'change_required')), job=job, parent_state='running')
    assert not current
    assert rows['timezone']['state'] == 'waiting'
    assert all(row['state'] != 'running' for row in rows.values())
    assert '추정하지 않습니다' in rows['timezone']['detail']


def test_finished_child_never_leaves_a_stale_other_child_running():
    rows, current = children('base', job={'state': 'running', 'substeps': [
        {'stage': 'base', 'id': 'packages', 'state': 'started'},
        {'stage': 'base', 'id': 'chrome', 'state': 'completed'},
    ]}, parent_state='running')
    assert current == ''
    assert rows['packages']['state'] == 'unknown'
    assert rows['chrome']['state'] == 'completed'


def test_final_real_check_overrides_completion_and_parent_failure_marks_open_step():
    job = {'state': 'failed', 'substeps': [{'stage': 'base', 'id': 'timezone', 'state': 'completed'},
                                        {'stage': 'base', 'id': 'time_sync', 'state': 'started'}],
           'report': checks(('base.timezone', 'change_required'), ('base.service.chronyd.active', 'ok'))}
    rows, current = children('base', job=job, parent_state='error')
    assert rows['timezone']['state'] == 'change_required'
    assert rows['time_sync']['state'] == 'error'
    assert not current


def test_reboot_wait_overrides_driver_completion():
    rows, current = children('gpu', job={'state': 'pending', 'substeps': [
        {'stage': 'gpu', 'id': 'driver', 'state': 'completed'}],
        'report': checks(('gpu.driver', 'pending'))}, parent_state='pending')
    assert rows['driver']['state'] == 'pending'
    assert current == ''


def test_missing_and_empty_final_reports_never_reuse_old_green_values():
    for report in (None, {}):
        job = {'state': 'failed', 'substeps': [{'stage': 'base', 'id': 'chrome', 'state': 'completed'}]}
        if report is not None:
            job['report'] = report
        rows, current = children('base', report=checks(('base.chrome.version', 'ok')), job=job, parent_state='unknown')
        assert rows['chrome']['state'] == 'unknown'
        assert not current


def test_disabled_scope_and_optional_root_theme_are_not_applicable():
    rows, _ = children('gpu', report=checks(), parent_state='not_applicable')
    assert all(row['state'] == 'not_applicable' for row in rows.values())
    rows, _ = children('shell', report=checks(config={'root_bash': False}))
    assert rows['root_shell']['state'] == 'not_applicable'
    assert rows['user_shell']['state'] == 'unknown'


def test_warning_and_interruption_are_not_live_progress():
    event = [{'stage': 'base', 'id': 'timezone', 'state': 'started'}]
    rows, current = children('base', job={'state': 'running', 'substeps': event, 'query_warning': 'query failed'}, parent_state='unknown')
    assert rows['timezone']['state'] == 'unknown' and current == ''
    rows, current = children('base', job={'state': 'interrupted', 'substeps': event}, parent_state='interrupted')
    assert rows['timezone']['state'] == 'interrupted' and current == ''


def test_unknown_marker_and_wrong_parent_are_ignored():
    rows, current = children('base', job={'state': 'running', 'substeps': [
        {'stage': 'base', 'id': 'arbitrary', 'state': 'started'},
        {'stage': 'docker', 'id': 'packages', 'state': 'started'},
    ]}, parent_state='running')
    assert current == ''
    assert all(row['state'] == 'waiting' for row in rows.values())


def test_normal_final_checks_remain_current_normal_for_kept_modules():
    rows, _ = children('base', job={'state': 'completed', 'report': checks(('base.timezone', 'ok'))}, parent_state='kept')
    assert rows['timezone']['state'] == 'ok'
    assert rows['chrome']['state'] == 'not_run'


def test_login_config_and_session_conditions_are_separate():
    rows, _ = children('chrome', report=checks(('display.autologin', 'ok'), ('display.gdm_enabled', 'ok'),
        ('display.gdm_runtime', 'pending'), ('display.session', 'pending'), ('chrome.forbidden', 'error')))
    assert rows['autologin']['state'] == 'ok'
    assert rows['session']['state'] == 'pending'
    assert rows['policy']['state'] == 'error'


def test_display_masks_and_hardware_firmware_match_real_inspector_ids():
    rows, _ = children('display', report=checks(('display.mask.sleep.target', 'ok'), ('display.logind.runtime', 'pending')))
    assert rows['system_sleep']['state'] == 'pending'
    rows, _ = children('hardware', report=checks(('hardware.package.iwlwifi-firmware', 'change_required'),
        ('hardware.package.kernel-modules-test', 'ok'), ('hardware.device.pci.mock-device', 'blocked')))
    assert rows['firmware']['state'] == 'change_required'
    assert rows['modules']['state'] == 'blocked'


def test_chrome_runtime_pending_reason_precedes_healthy_session_details():
    report = {'results': [
        {'id': 'display.session', 'state': 'ok', 'actual': 'active'},
        {'id': 'chrome.environment', 'state': 'ok', 'actual': 'ready'},
        {'id': 'chrome.runtime', 'state': 'pending', 'actual': '화면 환경 전달 대기',
         'next_action': '다음 그래픽 로그인 후 자동 시작'},
    ]}
    rows, _ = children('chrome', report=report)
    detail = rows['session']['detail']
    assert rows['session']['state'] == 'pending'
    assert detail.index('현재: 화면 환경 전달 대기') < detail.index('display.session')
    assert detail.index('다음: 다음 그래픽 로그인 후 자동 시작') < detail.index('chrome.runtime')


def test_native_pending_stage_without_substep_signals_keeps_verified_current_state():
    job = {'state': 'pending', 'stages': [{'id': 'chrome', 'state': 'started'},
                                        {'id': 'chrome', 'state': 'pending'}],
           'report': checks(('display.autologin', 'ok'), ('display.gdm_enabled', 'ok'),
                            ('display.session', 'pending'))}
    rows, current = children('chrome', job=job, parent_state='pending')
    assert rows['autologin']['state'] == 'ok'
    assert rows['session']['state'] == 'pending'
    assert rows['profile']['state'] == 'unknown'
    assert rows['service']['state'] == 'unknown'
    assert not current
    assert all(row['state'] not in {'running', 'completed', 'kept'} for row in rows.values())


def test_native_hardware_error_does_not_hide_an_individually_verified_final_check():
    job = {'state': 'failed', 'report': checks(('hardware.kernel', 'ok'),
                                             ('hardware.device.pci.mock-device', 'blocked'))}
    rows, current = children('hardware', job=job, parent_state='blocked')
    assert rows['detection']['state'] == 'ok'
    assert rows['modules']['state'] == 'blocked'
    assert rows['firmware']['state'] == 'unknown'
    assert not current


def test_unvisited_parent_still_marks_markerless_healthy_child_unrun():
    job = {'state': 'failed', 'report': checks(('display.autologin', 'ok'))}
    for parent_state in ('waiting', 'not_run'):
        rows, current = children('chrome', job=job, parent_state=parent_state)
        assert rows['autologin']['state'] == 'not_run'
        assert rows['profile']['state'] == 'not_run'
        assert not current


def test_missing_observations_and_actual_unknown_checks_have_distinct_evidence():
    report = checks(('base.timezone', 'unknown'))
    snapshots = [dict(report=report)]
    for parent_state in ('waiting', 'not_run', 'running'):
        snapshots.append(dict(report=report, job={'state': 'running'}, parent_state=parent_state))
    snapshots.append(dict(job={'state': 'pending', 'report': report}, parent_state='pending'))
    for snapshot in snapshots:
        rows, current = children('base', **snapshot)
        assert rows['timezone']['evidence'] == 'check'
        assert rows['chrome']['evidence'] == 'missing'
        assert not current
    rows, _ = children('base', report=report)
    assert rows['timezone']['state'] == rows['chrome']['state'] == 'unknown'


def test_valid_live_event_is_evidence_even_with_old_precheck_and_invalid_signals_are_not():
    job = {'state': 'running', 'substeps': [
        {'stage': 'base', 'id': 'packages', 'state': 'started'},
        {'stage': 'base', 'id': 'chrome', 'state': 'invalid'},
        {'stage': 'base', 'id': 'unlisted', 'state': 'completed'},
        {'stage': 'docker', 'id': 'timezone', 'state': 'completed'},
    ]}
    rows, current = children('base', report=checks(('base.package.git', 'change_required')),
                             job=job, parent_state='running')
    assert rows['packages']['evidence'] == 'event'
    assert rows['chrome']['evidence'] == rows['timezone']['evidence'] == 'missing'
    assert rows['chrome']['state'] == 'unknown'
    assert not current


def test_final_check_evidence_overrides_completion_but_failure_event_remains_observed():
    job = {'state': 'failed', 'report': checks(('base.timezone', 'unknown'),
                                              ('base.service.chronyd.active', 'ok')),
           'substeps': [{'stage': 'base', 'id': 'timezone', 'state': 'completed'},
                        {'stage': 'base', 'id': 'time_sync', 'state': 'failed'}]}
    rows, current = children('base', job=job, parent_state='error')
    assert rows['timezone']['state'] == 'unknown' and rows['timezone']['evidence'] == 'check'
    assert rows['time_sync']['state'] == 'error' and rows['time_sync']['evidence'] == 'event'
    assert rows['chrome']['evidence'] == 'missing'
    assert not current


def test_valid_event_without_final_check_is_not_reported_as_missing_record():
    job = {'state': 'failed', 'substeps': [{'stage': 'base', 'id': 'chrome', 'state': 'completed'}]}
    rows, current = children('base', job=job, parent_state='unknown')
    assert rows['chrome']['state'] == 'unknown' and rows['chrome']['evidence'] == 'event'
    assert rows['timezone']['state'] == 'unknown' and rows['timezone']['evidence'] == 'missing'
    assert not current
