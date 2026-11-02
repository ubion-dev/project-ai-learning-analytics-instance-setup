"""Headless keyboard acceptance tests. No host commands or OS writes."""
import asyncio
from copy import deepcopy

from textual.widgets import Button, Checkbox, Input, OptionList, TextArea

from ala.config import Config
from ala.model import CheckResult, Report
from ala.tui import AlaApp, ApplyConfirmation


class FakeService:
    def __init__(self):
        self.config = Config(user="user", url="https://example.com").to_dict()
        self.calls = []
        self.report = Report([
            CheckResult("base.package.chrome", "change_required", "설치됨", "미설치", "설치 필요"),
            CheckResult("gpu", "not_applicable", False, False),
        ]).to_dict()
        self.jobs = {}
        self.auth_required = False
        self.auth_count = 0
        self.status_gate = None

    def authenticate(self):
        self.auth_count += 1
        self.auth_required = False

    async def call(self, action, payload=None):
        payload = deepcopy(payload)
        self.calls.append((action, payload))
        if self.auth_required:
            raise PermissionError("sudo 인증 필요")
        await asyncio.sleep(0)
        if action == "status":
            if self.status_gate is not None:
                await self.status_gate.wait()
            return dict(deepcopy(self.report), hostname="mock-server", config=deepcopy(self.config), runs=deepcopy(list(self.jobs.values())))
        if action == "config-show":
            return {"config": deepcopy(self.config), "exists": True}
        if action == "config-save":
            self.config = Config.from_dict(payload).to_dict()
            return {"config": deepcopy(self.config), "saved": True}
        if action in {"plan", "inspect", "prepare-plan", "doctor-network"}:
            return deepcopy(self.report)
        if action in {"submit", "prepare-submit"}:
            assert payload == {"expected_config": self.config}
            run_id = "20261029T000000Z-1234abcd"
            self.jobs[run_id] = {"run_id": run_id, "operation": "prepare" if action == "prepare-submit" else "setup", "state": "queued", "started_at": "2026-10-29", "stages": []}
            return deepcopy(self.jobs[run_id])
        if action == "runs-list":
            return {"runs": deepcopy(list(self.jobs.values()))}
        if action == "runs-show":
            return deepcopy(self.jobs[payload["run_id"]])
        if action == "logs":
            return {"log": "[completed] base\n설치 로그\n"}
        if action == "chrome":
            if payload["action"] == "logs":
                return {"exit_code": 0, "log": "Chrome 로그\n다음 줄\n"}
            return {"action": payload["action"], "exit_code": 0, "detail": "Chrome 결과"}
        raise AssertionError(f"예상하지 않은 API: {action}")


def run(coro):
    return asyncio.run(coro)


def test_keyboard_navigation_and_report_details_share_structured_results():
    async def scenario():
        service = FakeService()
        app = AlaApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert app.section == "home"
            await pilot.press("down", "enter")
            await pilot.pause()
            assert app.section == "check"
            assert any(action == "inspect" for action, _ in service.calls)
            assert "base.package.chrome" in app.query_one("#check-detail", TextArea).text
            assert "설치 필요" in app.query_one("#check-detail", TextArea).text
            await pilot.press("escape")
            assert app.focused is app.query_one("#navigation", OptionList)
            assert not any(action in {"submit", "config-save", "chrome"} for action, _ in service.calls)
    run(scenario())


def test_configuration_keyboard_input_space_toggle_and_ctrl_s_save():
    async def scenario():
        service = FakeService()
        app = AlaApp(service=service, initial_section="config")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            entry = app.query_one("#config-user", Input)
            entry.value = ""
            entry.focus()
            await pilot.press("u", "s", "e", "r", "q")
            assert entry.value == "userq"  # 'q' in an input must not quit the app.
            check = app.query_one("#config-root_bash", Checkbox)
            check.focus()
            await pilot.press("space")
            assert check.value is True
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert service.config["user"] == "userq"
            assert service.config["root_bash"] is True
            assert any(action == "config-save" for action, _ in service.calls)
            assert not any(action == "submit" for action, _ in service.calls)
    run(scenario())


def test_invalid_config_is_not_sent_and_unsaved_draft_cannot_make_plan():
    async def scenario():
        service = FakeService()
        app = AlaApp(service=service, initial_section="config")
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#config-url", Input).value = "https://user:password@example.com"
            await app.save_config()
            await app.make_plan()
            assert not any(action in {"config-save", "plan", "submit"} for action, _ in service.calls)
            assert app.query_one("#apply-plan", Button).disabled
    run(scenario())


async def make_plan_with_keyboard(app, pilot):
    app.query_one("#make-plan", Button).focus()
    await pilot.press("enter")
    await pilot.pause()
    assert app.query_one("#apply-plan", Button).disabled is False


async def confirm_with_keyboard(app, pilot):
    app.query_one("#apply-plan", Button).focus()
    await pilot.press("enter")
    await pilot.pause()
    assert isinstance(app.screen, ApplyConfirmation)
    await pilot.press("tab", "enter")
    await pilot.pause()


def test_plan_apply_confirmation_cancel_and_background_job_survives_ui_exit():
    async def scenario():
        service = FakeService()
        app = AlaApp(service=service, initial_section="setup")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await make_plan_with_keyboard(app, pilot)
            app.query_one("#apply-plan", Button).focus()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ApplyConfirmation)
            await pilot.press("escape")
            assert not any(action == "submit" for action, _ in service.calls)
            await confirm_with_keyboard(app, pilot)
            assert sum(action == "submit" for action, _ in service.calls) == 1
            run_id = app._active_job
            assert run_id in service.jobs
            await pilot.press("escape", "q")
        assert service.jobs[run_id]["state"] == "queued"
        assert not any(action == "chrome" and payload["action"] == "stop" for action, payload in service.calls)
    run(scenario())


def test_changed_saved_config_invalidates_reviewed_plan_before_submit():
    async def scenario():
        service = FakeService()
        app = AlaApp(service=service, initial_section="setup")
        async with app.run_test() as pilot:
            await pilot.pause()
            await make_plan_with_keyboard(app, pilot)
            service.config["url"] = "https://other.example.com"
            await confirm_with_keyboard(app, pilot)
            assert not any(action == "submit" for action, _ in service.calls)
            assert app.query_one("#apply-plan", Button).disabled
    run(scenario())


def test_blocked_plan_cannot_be_applied():
    async def scenario():
        service = FakeService()
        service.report = Report([CheckResult("gpu.support", "unknown", next_action="네트워크 확인")]).to_dict()
        app = AlaApp(service=service, initial_section="setup")
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.make_plan()
            assert app.query_one("#apply-plan", Button).disabled
            await app.submit_plan()
            assert not any(action == "submit" for action, _ in service.calls)
    run(scenario())


def test_job_poll_uses_lightweight_records_and_logs_only():
    async def scenario():
        service = FakeService()
        run_id = "20261029T000000Z-1234abcd"
        service.jobs[run_id] = {"run_id": run_id, "state": "running", "stages": [{"id": "base", "state": "started"}]}
        app = AlaApp(service=service)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._active_job == run_id
            service.calls.clear()
            await app.poll_job()
            assert [action for action, _ in service.calls] == ["runs-show", "logs"]
            assert "설치 로그" in app.query_one("#setup-log", TextArea).text
            service.jobs[run_id].update(state="pending", report=Report([CheckResult("gpu.driver", "pending", next_action="재부팅")]).to_dict())
            await app.poll_job()
            assert app._active_job is None
            assert app._reports["plan"][0]["state"] == "pending"
    run(scenario())


def test_authentication_failure_and_explicit_retry_without_password_ui():
    async def scenario():
        service = FakeService()
        service.auth_required = True
        app = AlaApp(service=service)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert service.auth_count == 0
            await pilot.press("a")
            await pilot.pause()
            assert service.auth_count == 1
            assert app.SUB_TITLE == "mock-server"
            assert not any("password" in (input.id or "") for input in app.query(Input))
    run(scenario())


def test_authentication_preserves_unsaved_config_draft():
    async def scenario():
        service = FakeService()
        app = AlaApp(service=service, initial_section="config")
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#config-user", Input).value = "changed"
            await app.authenticate()
            await pilot.pause()
            assert app.query_one("#config-user", Input).value == "changed"
            assert service.config["user"] == "user"
    run(scenario())


def test_config_cannot_be_saved_before_loading_authoritative_values():
    async def scenario():
        service = FakeService()
        service.auth_required = True
        app = AlaApp(service=service, initial_section="config")
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#config-form").disabled
            assert app.query_one("#save-config", Button).disabled
            await app.save_config()
            assert not any(action == "config-save" for action, _ in service.calls)
            await pilot.press("a")
            await pilot.pause()
            assert not app.query_one("#config-form").disabled
            assert app.query_one("#config-ssh", Checkbox).value is True
    run(scenario())


def test_small_terminal_warning_and_keyboard_chrome_command():
    async def scenario():
        service = FakeService()
        app = AlaApp(service=service, initial_section="chrome")
        async with app.run_test(size=(60, 15)) as pilot:
            await pilot.pause()
            assert app.query_one("#small-terminal").display
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            assert not app.query_one("#small-terminal").display
            app.query_one("#chrome-restart", Button).focus()
            await pilot.press("enter")
            await pilot.pause()
            assert ("chrome", {"action": "restart"}) in service.calls
            assert "restart" in app.query_one("#chrome-output", TextArea).text
            await app.chrome_action("logs")
            assert app.query_one("#chrome-output", TextArea).text == "Chrome 로그\n다음 줄\n"
    run(scenario())


def test_slow_status_does_not_block_keyboard_navigation():
    async def scenario():
        service = FakeService()
        service.status_gate = asyncio.Event()
        app = AlaApp(service=service)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert not service.status_gate.is_set()
            await pilot.press("down", "enter")
            await pilot.pause()
            assert app.section == "check"
            assert any(action == "inspect" for action, _ in service.calls)
    run(scenario())


def test_run_history_opens_selected_record_and_plain_log():
    async def scenario():
        service = FakeService()
        run_id = "20261029T000000Z-1234abcd"
        service.jobs[run_id] = {"run_id": run_id, "state": "completed", "started_at": "2026-10-29", "stages": []}
        app = AlaApp(service=service, initial_section="runs")
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#run-table").focus()
            await pilot.press("enter")
            await pilot.pause()
            assert run_id in app.query_one("#run-output", TextArea).text
            assert "설치 로그" in app.query_one("#run-output", TextArea).text
    run(scenario())


def test_entire_install_path_is_reachable_with_keys_only():
    async def scenario():
        service = FakeService()
        app = AlaApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert app.section == "wizard"
            assert app._wizard_stage == "account"
            await pilot.press("enter")
            await pilot.pause()
            assert app._wizard_stage == "display"
            await pilot.press("tab", "enter")
            await pilot.pause()
            assert app._wizard_stage == "review"
            assert app._plan_config == service.config
            assert not any(action == "submit" for action, _ in service.calls)
            await pilot.press("enter")
            await pilot.pause()
            assert app._wizard_stage == "running"
            assert sum(action == "submit" for action, _ in service.calls) == 1
    run(scenario())


def test_q_exits_readonly_log_without_stopping_browser():
    async def scenario():
        service = FakeService()
        app = AlaApp(service=service, initial_section="chrome")
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#chrome-output", TextArea).focus()
            await pilot.press("q")
        assert not any(action == "chrome" and payload["action"] == "stop" for action, payload in service.calls)
    run(scenario())


def test_prepare_is_explicit_keyboard_plan_and_survives_exit():
    async def scenario():
        service = FakeService()
        app = AlaApp(service=service, initial_section='setup')
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_one('#prepare-plan', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            assert app._plan_operation == 'prepare'
            assert any(action == 'prepare-plan' for action, _ in service.calls)
            await confirm_with_keyboard(app, pilot)
            job = service.jobs[app._active_job]
            assert job['operation'] == 'prepare'
            assert not any(action == 'submit' for action, _ in service.calls)
            await pilot.press('escape', 'q')
        assert job['state'] == 'queued'
    run(scenario())


def test_network_diagnosis_button_is_readonly():
    async def scenario():
        service = FakeService()
        app = AlaApp(service=service, initial_section='check')
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_one('#network-check', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            assert any(action == 'doctor-network' for action, _ in service.calls)
            assert not any(action in {'submit', 'prepare-submit', 'config-save'} for action, _ in service.calls)
    run(scenario())


def test_network_diagnostic_failure_does_not_block_unrelated_configuration():
    async def scenario():
        service = FakeService()
        service.report = Report([
            CheckResult('network.device.pci.initialization', 'blocked', actual='timeout -110'),
            CheckResult('display.autologin', 'change_required'),
        ]).to_dict()
        app = AlaApp(service=service, initial_section='setup')
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await make_plan_with_keyboard(app, pilot)
            assert app._reports['plan'][0]['state'] == 'blocked'
            await confirm_with_keyboard(app, pilot)
            assert any(action == 'submit' for action, _ in service.calls)
    run(scenario())


def test_prepare_verification_retry_can_be_reviewed_and_applied():
    async def scenario():
        service = FakeService()
        service.report = Report([CheckResult('prepare.recovery', 'change_required', detail='검증만 재시도')]).to_dict()
        app = AlaApp(service=service, initial_section='setup')
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.make_plan('prepare')
            assert not app.query_one('#apply-plan', Button).disabled
            await confirm_with_keyboard(app, pilot)
            assert any(action == 'prepare-submit' for action, _ in service.calls)
    run(scenario())


def test_home_has_four_task_entries_and_no_technical_table():
    async def scenario():
        service = FakeService()
        app = AlaApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert app.query_one('#navigation', OptionList).option_count == 4
            assert not app.query_one('#toolbar').display
            assert app.section == 'home'
            assert not any(a in {'config-save', 'submit', 'prepare-submit'} for a, _ in service.calls)
    run(scenario())


async def open_wizard(app, pilot):
    await pilot.pause()
    app.query_one('#navigation', OptionList).focus()
    await pilot.press('home', 'enter')
    await pilot.pause()
    assert app._wizard_base is not None


async def guided_review(app, pilot, desktop=False):
    await open_wizard(app, pilot)
    await app.wizard_advance()
    if desktop:
        app.query_one('#wizard-web', Checkbox).value = False
    await pilot.pause()
    await app.wizard_advance()
    await pilot.pause()


def test_guided_desktop_preserves_advanced_options_and_cancel_does_not_install():
    async def scenario():
        service = FakeService()
        service.config.update(root_bash=True, dev_tools=True, timezone='UTC', ssh_zone='public')
        app = AlaApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await guided_review(app, pilot, desktop=True)
            assert app._wizard_stage == 'review'
            assert service.config['display'] and service.config['autologin']
            assert not service.config['autostart']
            assert service.config['url'] == 'https://example.com'
            assert service.config['root_bash'] and service.config['dev_tools']
            assert service.config['timezone'] == 'UTC'
            assert service.config['ssh_zone'] == 'public'
            await pilot.press('escape')
            assert app.section == 'home'
            assert not any(a in {'submit', 'prepare-submit'} for a, _ in service.calls)
    run(scenario())


def test_guided_did_requires_url_and_existing_general_account():
    async def scenario():
        service = FakeService()
        app = AlaApp(service=service)
        async with app.run_test() as pilot:
            await open_wizard(app, pilot)
            app.query_one('#wizard-user', Input).value = 'root'
            await app.wizard_advance()
            assert app._wizard_stage == 'account'
            app.query_one('#wizard-user', Input).value = 'user'
            await app.wizard_advance()
            app.query_one('#wizard-url', Input).value = ''
            await app.wizard_advance()
            assert app._wizard_stage == 'display'
            assert not any(a == 'config-save' for a, _ in service.calls)
    run(scenario())


def test_guided_plan_blocker_empty_result_and_stale_config_do_not_submit():
    async def scenario():
        for rows in ([], [CheckResult('platform', 'blocked', next_action='지원 OS 확인')]):
            service = FakeService()
            service.report = Report(rows).to_dict()
            app = AlaApp(service=service)
            async with app.run_test() as pilot:
                await guided_review(app, pilot)
                assert app.query_one('#wizard-next', Button).disabled
                await app.wizard_advance()
                assert not any(a == 'submit' for a, _ in service.calls)
        service = FakeService()
        app = AlaApp(service=service)
        async with app.run_test() as pilot:
            await guided_review(app, pilot)
            service.config['url'] = 'https://other.example.com'
            await app.wizard_advance()
            assert not any(a == 'submit' for a, _ in service.calls)
    run(scenario())


def test_guided_auth_failure_retries_without_saving_or_installing():
    async def scenario():
        service = FakeService()
        app = AlaApp(service=service)
        async with app.run_test() as pilot:
            await pilot.pause()
            original = service.authenticate
            def fail():
                raise PermissionError('인증 실패')
            service.authenticate = fail
            await pilot.press('enter')
            await pilot.pause()
            assert app.query_one('#wizard-next', Button).disabled
            assert not any(a == 'config-save' for a, _ in service.calls)
            service.authenticate = original
            await app.begin_wizard()
            assert not app.query_one('#wizard-next', Button).disabled
            assert app._wizard_stage == 'account'
    run(scenario())


def test_guided_progress_resumes_existing_job_and_shows_next_action():
    async def scenario():
        service = FakeService()
        run_id = '20261029T000000Z-1234abcd'
        service.jobs[run_id] = dict(run_id=run_id, state='running', stages=[])
        app = AlaApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press('enter')
            await pilot.pause()
            assert app._wizard_stage == 'running'
            assert not any(a == 'config-save' for a, _ in service.calls)
            service.jobs[run_id].update(state='pending', report=Report([
                CheckResult('gpu.driver', 'pending', next_action='재부팅 후 ala check 실행')
            ]).to_dict())
            await app.poll_job()
            assert '재부팅 후' in app.query_one('#wizard-result', TextArea).text
            await pilot.press('q')
        assert service.jobs[run_id]['state'] == 'pending'
    run(scenario())


def test_update_banner_and_offline_state(monkeypatch):
    from ala import update
    from textual.widgets import Static
    async def scenario():
        app = AlaApp(service=FakeService())
        async with app.run_test(size=(80, 24)):
            monkeypatch.setattr(update, 'check', lambda: {'available': True, 'latest': '2.0.0'})
            await app.check_update()
            assert 'ala update' in str(app.query_one('#update-notice', Static).render())
            monkeypatch.setattr(update, 'check', lambda: {'available': False, 'error': 'offline'})
            await app.check_update()
            assert '계속' in str(app.query_one('#update-notice', Static).render())
    run(scenario())


def test_review_prioritizes_blocker_separates_pending_and_exposes_details():
    from textual.widgets import Static, Collapsible
    async def scenario():
        service = FakeService()
        service.report['results'] += [
            {'id': 'docker.conflict.podman', 'state': 'blocked', 'actual': 'podman', 'next_action': '컨테이너 확인'},
            {'id': 'display.session', 'state': 'pending', 'actual': '세션 대기', 'next_action': '재부팅 후 확인'},
        ]
        app = AlaApp(service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.show_section('wizard')
            await app.begin_wizard()
            await app.wizard_advance()
            await app.wizard_advance()
            await pilot.pause()
            text = str(app.query_one('#review-status', Static).render())
            assert 'podman' in text and '1개' in text
            assert '재부팅' not in text
            assert app.query_one('#wizard-next', Button).disabled
            assert app.query_one('#wizard-next').region.bottom <= 23
            details = app.query_one('#review-details', Collapsible)
            assert details.collapsed
            details.query_one('CollapsibleTitle').focus()
            await pilot.press('enter')
            await pilot.pause()
            assert not details.collapsed
            assert 'docker.conflict.podman' in app.query_one('#wizard-summary', TextArea).text
            assert '적용 후 확인' in app.query_one('#wizard-summary', TextArea).text
    run(scenario())
