from copy import deepcopy

from ala.progress import progress_rows, STATE_LABELS, STATE_STYLES


def row_map(**kwargs):
    return {row['id']: row for row in progress_rows(**kwargs)}


def result(identifier, state, **extra):
    return {'id': identifier, 'state': state, **extra}


def test_all_stages_exist_before_execution_and_are_not_assumed_complete():
    rows = progress_rows()
    assert [row['id'] for row in rows] == ['base', 'update', 'hardware', 'shell', 'ssh', 'docker',
                                          'gpu', 'gpu_container', 'dev_tools', 'display', 'chrome']
    assert all(row['state'] == 'unknown' for row in rows)
    queued = progress_rows(job={'state': 'queued', 'stages': []})
    assert all(row['state'] == 'waiting' for row in queued)


def test_last_event_per_stage_replaces_history_and_translates_kept():
    stages = [{'id': name, 'state': 'kept'} for name in ('base', 'update', 'hardware', 'shell', 'ssh')]
    stages += [{'id': 'docker', 'state': 'started'}, {'id': 'docker', 'state': 'completed'},
               {'id': 'gpu', 'state': 'started'}]
    job = {'state': 'running', 'stages': stages}
    original = deepcopy(job)
    rows = row_map(job=job)
    assert len(rows) == 11
    assert rows['base']['state'] == 'kept'
    assert STATE_LABELS[rows['base']['state']] == '✓ 정상 유지'
    assert STATE_STYLES[rows['base']['state']] == '#b5e9d9'
    assert rows['docker']['state'] == 'completed'
    assert rows['gpu']['state'] == 'running'
    assert rows['display']['state'] == 'waiting'
    assert job == original


def test_terminal_report_overrides_completed_and_does_not_green_unvisited_steps():
    job = {'state': 'failed', 'stages': [{'id': 'docker', 'state': 'completed'}], 'report': {'results': [
        result('docker.info', 'error', actual='unavailable', next_action='서비스 확인'),
        result('display.gnome', 'ok'), result('display.autologin', 'change_required'),
    ]}}
    rows = row_map(job=job)
    assert rows['docker']['state'] == 'error'
    assert '서비스 확인' in rows['docker']['detail']
    assert rows['display']['state'] == rows['chrome']['state'] == 'not_run'
    assert STATE_STYLES[rows['display']['state']] != '#b5e9d9'


def test_terminal_pending_report_is_not_completed_and_disabled_tools_are_not_applicable():
    rows = row_map(job={'state': 'pending', 'stages': [{'id': 'gpu', 'state': 'completed'},
                                                   {'id': 'dev_tools', 'state': 'kept'}],
                       'report': {'results': [result('gpu.driver', 'pending', actual='재부팅 대기'),
                                              result('dev_tools', 'not_applicable')]}})
    assert rows['gpu']['state'] == 'pending'
    assert rows['dev_tools']['state'] == 'not_applicable'
    assert STATE_STYLES[rows['gpu']['state']] == '#f1d5a3'


def test_missing_final_report_does_not_reuse_old_green_snapshot():
    rows = row_map(report={'results': [result('docker.info', 'ok')]},
                   job={'state': 'failed', 'stages': [{'id': 'docker', 'state': 'completed'}]})
    assert rows['docker']['state'] == 'unknown'
    assert '검사 결과가 없습니다' in rows['docker']['detail']


def test_gpu_container_observations_and_conditional_absence_are_separate():
    rows = row_map(report={'results': [result('gpu.driver', 'ok'),
                                      result('gpu.package.nvidia-container-toolkit', 'change_required'),
                                      result('gpu.runtime.live', 'pending')]})
    assert rows['gpu']['state'] == 'ok'
    assert rows['gpu_container']['state'] == 'change_required'
    assert 'nvidia-container-toolkit' in rows['gpu_container']['detail']
    absent = row_map(report={'results': [result('gpu', 'not_applicable')]})
    assert absent['gpu']['state'] == absent['gpu_container']['state'] == 'not_applicable'


def test_query_warning_and_interruption_do_not_continue_running_forever():
    event = [{'id': 'gpu', 'state': 'started'}]
    rows = row_map(job={'state': 'running', 'stages': event, 'query_warning': 'unit 조회 실패'})
    assert rows['gpu']['state'] == 'unknown'
    assert 'unit 조회 실패' in rows['gpu']['detail']
    stopped = row_map(job={'state': 'interrupted', 'stages': event})
    assert stopped['gpu']['state'] == 'interrupted'
    assert stopped['chrome']['state'] == 'not_run'


def test_unmapped_diagnostic_failure_remains_visible_with_no_install_event():
    rows = row_map(job={'state': 'failed', 'stages': [{'id': 'base', 'state': 'kept'}],
                       'report': {'results': [result('base.package.curl', 'ok'),
                                              result('network.device.initialization', 'unknown', actual='timeout'),
                                              result('user', 'blocked', next_action='계정 확인')]}})
    assert rows['base']['state'] == 'kept'
    assert rows['network']['state'] == 'unknown'
    assert rows['network']['label'] == '네트워크 진단'
    assert rows['user']['state'] == 'blocked' and '계정 확인' in rows['user']['detail']


def test_display_policy_and_final_login_integration_map_to_the_actual_steps():
    rows = row_map(report={'results': [result('display.dconf.compiled', 'ok'), result('display.mask.sleep.target', 'ok'),
                                      result('display.autologin', 'change_required'),
                                      result('display.session', 'pending'), result('chrome.runtime', 'unknown')]})
    assert rows['display']['state'] == 'ok'
    assert rows['chrome']['state'] == 'unknown'


def test_unknown_events_are_visible_and_styles_do_not_replace_textual_labels():
    rows = row_map(job={'state': 'running', 'stages': [{'id': 'unexpected', 'state': 'new-state'}]})
    assert rows['unexpected']['state'] == 'unknown'
    assert all(STATE_LABELS[state] and STATE_STYLES[state] for state in STATE_LABELS)


def test_current_event_wins_over_a_stale_not_applicable_snapshot():
    rows = row_map(report={'results': [result('gpu', 'not_applicable'),
                                      result('dev_tools', 'not_applicable')]},
                   job={'state': 'running', 'stages': [{'id': 'gpu', 'state': 'started'},
                                                       {'id': 'dev_tools', 'state': 'kept'}]})
    assert rows['gpu']['state'] == 'running'
    assert rows['dev_tools']['state'] == 'not_applicable'
