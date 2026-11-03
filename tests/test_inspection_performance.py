"""Read-only inspection scheduling with synthetic hosts; no live commands."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
import itertools
import time

import pytest

from ala.config import Config
from ala.engine import Engine
from ala.model import CheckResult, Report
from ala.service import Operations
from ala.system import Command, InspectionHost, Paths


class FakeHost:
    def __init__(self):
        self.calls = []
        self.executed = []
        self.lock = Lock()
        self.reply = Command(0, 'read', '')

    def run(self, argv, *, timeout=10, env=None):
        with self.lock:
            self.calls.append((tuple(argv), timeout, env))
        return self.reply

    def execute(self, argv, **kwargs):
        self.executed.append(argv)
        pytest.fail('inspection executed a mutation')

    execute_reported = execute


@pytest.mark.parametrize('code', [0, 1, 124, 127])
def test_identical_parallel_reads_are_single_flight_and_keep_failure(code):
    base = FakeHost()
    base.reply = Command(code, 'original output', 'original diagnostic')
    entered, release = Event(), Event()
    original = base.run

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    base.run = delayed
    host = InspectionHost(base)
    ready = Barrier(4)

    def query():
        ready.wait(timeout=2)
        return host.run(['rpm', '-q', 'git'])

    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = [pool.submit(query) for _ in range(4)]
        try:
            assert entered.wait(2)
            assert not any(f.done() for f in pending)
        finally:
            release.set()
        results = [f.result(timeout=2) for f in pending]
    assert len(base.calls) == 1
    assert results == [base.reply] * 4
    results[0].stdout = 'changed by caller'
    assert host.run(['rpm', '-q', 'git']).stdout == 'original output'


def test_command_keys_preserve_timeout_environment_and_arguments():
    base = FakeHost()
    host = InspectionHost(base)
    host.run(['read', 'literal;$(ignored)'])
    host.run(['read', 'literal;$(ignored)'], timeout=10)
    host.run(['read', 'literal;$(ignored)'], timeout=11)
    host.run(['read', 'literal;$(ignored)'], env={})
    host.run(['read', 'literal;$(ignored)'], env={'LANG': 'C', 'LC_ALL': 'C'})
    host.run(['read', 'literal;$(ignored)'], env={'LC_ALL': 'C', 'LANG': 'C'})
    host.run(['read', 'another literal'])
    assert len(base.calls) == 5
    assert base.calls[0] == (('read', 'literal;$(ignored)'), 10, None)
    assert base.calls[3][2] == {'LANG': 'C', 'LC_ALL': 'C'}


def test_single_flight_exceptions_are_shared_without_hanging():
    base = FakeHost()

    def denied(*args, **kwargs):
        base.calls.append(args)
        time.sleep(0.01)
        raise PermissionError('synthetic read denied')

    base.run = denied
    host = InspectionHost(base)
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = [pool.submit(host.run, ['read']) for _ in range(4)]
        for result in pending:
            with pytest.raises(PermissionError, match='synthetic read denied'):
                result.result(timeout=2)
    assert len(base.calls) == 1


def test_read_only_wrapper_rejects_mutation_methods():
    base = FakeHost()
    host = InspectionHost(base)
    for method in (host.execute, host.execute_reported):
        with pytest.raises(RuntimeError, match='변경 명령'):
            method(['a-mutation'])
    assert not base.calls and not base.executed


STAGES = ('base', 'updates', 'hardware', 'network', 'shell', 'ssh', 'docker',
          'gpu', 'dev_tools', 'display_policy', 'extra_display')


def synthetic_engine(monkeypatch, tmp_path, *, state='ok', workers_seen=None):
    monkeypatch.setattr('ala.engine.platform_error', lambda paths: None)
    engine = Engine(Config(user='user'), host=FakeHost(), paths=Paths(tmp_path))
    monkeypatch.setattr(engine, '_account', lambda name: (None, CheckResult('user', 'ok')))
    lock = Lock()
    active = 0

    def make_stage(name):
        def inspect_stage():
            nonlocal active
            with lock:
                active += 1
                if workers_seen is not None:
                    workers_seen.append(active)
            try:
                if workers_seen is not None:
                    time.sleep(0.01)
                return [CheckResult(name + '.check', state)]
            finally:
                with lock:
                    active -= 1
        inspect_stage.__name__ = '_' + name
        return inspect_stage

    for name in STAGES:
        monkeypatch.setattr(engine, '_' + name, make_stage(name))
    return engine


def assert_timing(value, names):
    assert set(value) == {'duration_ms', 'stages'}
    assert isinstance(value['duration_ms'], (int, float)) and value['duration_ms'] >= 0
    assert [s['id'] for s in value['stages']] == names
    for stage in value['stages']:
        assert set(stage) == {'id', 'duration_ms'}
        assert isinstance(stage['duration_ms'], (int, float)) and stage['duration_ms'] >= 0


@pytest.mark.parametrize('state', ['ok', 'pending', 'blocked', 'unknown', 'error', 'change_required', 'not_applicable'])
def test_default_engine_is_serial_and_parallelism_never_exceeds_four(monkeypatch, tmp_path, state):
    seen = []
    engine = synthetic_engine(monkeypatch, tmp_path, workers_seen=seen, state=state)
    serial = engine.inspect().to_dict()
    assert max(seen) == 1
    seen.clear()
    parallel = engine.inspect(workers=100).to_dict()
    assert 1 < max(seen) <= 4
    assert serial == parallel
    assert [r['id'] for r in parallel['results']] == ['platform', 'user'] + [s + '.check' for s in STAGES]
    assert_timing(engine.inspection, ['platform', 'user', *STAGES])


def test_each_failed_stage_retains_unknown_and_report_order(monkeypatch, tmp_path):
    engine = synthetic_engine(monkeypatch, tmp_path)

    def _network():
        raise PermissionError('synthetic read denied')

    monkeypatch.setattr(engine, '_network', _network)
    serial = engine.inspect(configuration_only=True).to_dict()
    parallel = engine.plan(workers=4).to_dict()
    assert serial == parallel
    assert parallel['exit_code'] == 1
    assert parallel['results'][5]['id'] == 'network'
    assert parallel['results'][5]['state'] == 'unknown'
    assert next(r for r in parallel['results'] if r['id'] == 'docker.check')['state'] == 'ok'


@pytest.mark.parametrize('workers', [0, -1, True, '4'])
def test_invalid_worker_count_is_rejected(monkeypatch, tmp_path, workers):
    engine = synthetic_engine(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        engine.inspect(workers=workers)


def test_platform_failure_has_nonnegative_timing_and_no_commands(monkeypatch, tmp_path):
    engine = Engine(Config(user='user'), FakeHost(), Paths(tmp_path))
    monkeypatch.setattr('ala.engine.platform_error', lambda paths: 'synthetic unsupported platform')
    assert engine.inspect(workers=4).exit_code == 1
    assert_timing(engine.inspection, ['platform'])
    assert not engine.host.calls


def test_timing_never_becomes_negative(monkeypatch, tmp_path):
    engine = synthetic_engine(monkeypatch, tmp_path)
    clock = itertools.count(100, -1)
    monkeypatch.setattr('ala.engine.perf_counter', lambda: next(clock))
    engine.inspect()
    assert_timing(engine.inspection, ['platform', 'user', *STAGES])
    assert engine.inspection['duration_ms'] == 0
    assert all(stage['duration_ms'] == 0 for stage in engine.inspection['stages'])


@pytest.mark.parametrize('action', ['inspect', 'plan', 'status'])
def test_operations_cache_is_fresh_each_request_and_original_host_is_unchanged(monkeypatch, tmp_path, action):
    from ala.jobs import JobStore
    from ala.prepare import Prepare

    base = FakeHost()
    operations = Operations(paths=Paths(tmp_path), host=base)
    monkeypatch.setattr(operations, 'config', lambda: Config(user='user'))
    monkeypatch.setattr('ala.engine.platform_error', lambda paths: None)
    monkeypatch.setattr(Engine, '_account', lambda self, name: (None, CheckResult('user', 'ok')))

    def make_stage(name):
        def inspect_stage(self):
            value = self.host.run(['read', 'shared'])
            return [CheckResult(name, 'ok' if value.returncode == 0 else 'unknown', actual=value.stdout)]
        inspect_stage.__name__ = '_' + name
        return inspect_stage

    for name in STAGES:
        monkeypatch.setattr(Engine, '_' + name, make_stage(name))
    monkeypatch.setattr(JobStore, 'list', lambda self: [])
    monkeypatch.setattr(Prepare, 'inspect', lambda self: Report())
    before = list(tmp_path.rglob('*'))
    first = operations.dispatch(action, {})
    base.reply = Command(124, '', 'synthetic timeout')
    second = operations.dispatch(action, {})
    assert len(base.calls) == 2
    assert first['exit_code'] == 0 and second['exit_code'] == 1
    assert operations.host is base
    names = ['platform', 'user', *STAGES] + (['runs', 'preparation'] if action == 'status' else [])
    assert_timing(first['inspection'], names)
    assert_timing(second['inspection'], names)
    assert not base.executed
    assert before == list(tmp_path.rglob('*'))


def test_prepare_and_submit_actions_keep_the_original_host(monkeypatch, tmp_path):
    from ala.jobs import JobStore
    from ala.prepare import Prepare

    base = FakeHost()
    operations = Operations(paths=Paths(tmp_path), host=base)
    monkeypatch.setattr(operations, 'config', lambda: Config(user='user'))
    seen = []

    def plan(self):
        seen.append(self.host)
        self.host.run(['read', 'shared'])
        self.host.run(['read', 'shared'])
        return Report()

    def submit(self, *args, **kwargs):
        seen.append(self.host)
        return {'state': 'queued'}

    monkeypatch.setattr(Prepare, 'plan', plan)
    monkeypatch.setattr(JobStore, 'submit', submit)
    operations.dispatch('prepare-plan', {})
    operations.dispatch('prepare-submit', {})
    operations.dispatch('submit', {})
    assert seen == [base, base, base]
    assert len(base.calls) == 2


def test_real_inspectors_keep_reports_and_files_equivalent():
    # Reuse the established synthetic OS layout, never the developer's host.
    import test_engine as fixtures
    fixture = fixtures.EngineTests()
    fixture.setUp()
    try:
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in fixture.paths.root.rglob('*') if p.is_file()}
        serial = fixture.engine.inspect().to_dict()
        fixture.engine.host = InspectionHost(fixture.host)
        parallel = fixture.engine.inspect(workers=4).to_dict()
        assert parallel == serial
        assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns)
                          for p in fixture.paths.root.rglob('*') if p.is_file()}
        assert fixture.host.executed == []
        assert not any(part in {'start', 'stop', 'enable', 'mask', 'install'}
                       for call in fixture.host.calls for part in call)
    finally:
        fixture.doCleanups()


def test_apply_rechecks_changed_packages_without_reusing_query_cache(monkeypatch):
    import test_engine as fixtures
    fixture = fixtures.EngineTests()
    fixture.setUp()
    try:
        operations = Operations(paths=fixture.paths, host=fixture.host)
        monkeypatch.setattr(operations, 'config', lambda: fixture.config)
        assert operations.dispatch('inspect', {})['exit_code'] == 0
        fixture.host.packages.remove('curl')
        fixture.host.calls.clear()
        fixture.host.on_execute = lambda argv: fixture.host.packages.add('curl')
        stages = []
        result = fixture.engine.apply(lambda *event: stages.append(event))
        assert result.exit_code == 0
        assert ('base', 'started') in stages and ('base', 'completed') in stages
        assert len(fixture.host.executed) == 1
        assert fixture.host.calls.count(('rpm', '-q', 'curl')) >= 3
        assert fixture.engine.host is operations.host is fixture.host
    finally:
        fixture.doCleanups()
