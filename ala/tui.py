"""Keyboard-only front end for the shared, structured administrator API.

The UI never runs setup commands, parses shell output, or owns installation jobs.
Only a selected job's lightweight record/log is polled; host inspection is explicit.
"""
from __future__ import annotations

import json
from typing import Any

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, ContentSwitcher, DataTable, Footer, Header, Input, Label, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from .config import Config


SECTIONS = (
    ("dashboard", "대시보드"), ("config", "구성"), ("setup", "설치·설정"),
    ("check", "검사"), ("chrome", "Chrome"), ("runs", "실행 기록"),
)
BOOL_FIELDS = (
    ("root_bash", "root Bash 테마"), ("dev_tools", "Neovim·btop·tmux"),
    ("ssh", "SSH·방화벽 준비"), ("display", "화면 정책 적용"),
    ("autologin", "GNOME 자동 로그인"), ("autostart", "Chrome 자동 실행"),
)
STATE_LABELS = {
    "ok": "정상", "change_required": "변경 필요", "pending": "대기",
    "not_applicable": "해당 없음", "blocked": "차단", "unknown": "확인 불가", "error": "실패",
}
ACTIVE_JOBS = {"queued", "running"}
JOB_LABELS = {"queued": "시작 대기", "running": "실행 중", "completed": "완료", "pending": "적용 대기", "failed": "실패", "interrupted": "중단됨"}


def readable(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


class ApplyConfirmation(ModalScreen[bool]):
    """One deliberate confirmation after a successful plan, defaulting to cancel."""
    BINDINGS = [("escape", "cancel", "취소")]
    CSS = """
    ApplyConfirmation { align: center middle; background: $background 65%; }
    #apply-dialog { width: 62; height: auto; max-height: 18; border: thick $accent; padding: 1 2; }
    #apply-dialog Horizontal { height: 3; align-horizontal: right; }
    #apply-dialog Button { margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="apply-dialog"):
            yield Label("확인한 구성으로 설치·설정을 적용합니다.")
            yield Static("현재 상태를 다시 검사하고 필요한 변경만 수행합니다.\n화면을 종료하거나 SSH가 끊겨도 시작한 설치는 계속됩니다.", markup=False)
            with Horizontal():
                yield Button("취소", id="cancel-apply")
                yield Button("적용", variant="warning", id="confirm-apply")

    def on_mount(self):
        self.query_one("#cancel-apply", Button).focus()

    @on(Button.Pressed, "#cancel-apply")
    def cancel_pressed(self):
        self.dismiss(False)

    @on(Button.Pressed, "#confirm-apply")
    def confirm_pressed(self):
        self.dismiss(True)

    def action_cancel(self):
        self.dismiss(False)


class AlaApp(App[None]):
    TITLE = "ala · AI 학습 분석실 인스턴스 관리"
    SUB_TITLE = "SSH 환경 관리"
    CSS = """
    Screen { min-width: 1; }
    #small-terminal { display: none; height: auto; background: $warning; color: $background; padding: 0 1; }
    #toolbar { height: 3; }
    #toolbar Button { min-width: 10; margin-right: 1; }
    #notice { height: auto; max-height: 3; padding: 0 1; }
    #body { height: 1fr; }
    #navigation { width: 18; min-width: 15; height: 1fr; border-right: solid $accent; }
    #content { width: 1fr; padding: 0 1; }
    .panel { height: 1fr; overflow-y: auto; }
    .actions { height: auto; min-height: 3; }
    .actions Button { margin-right: 1; min-width: 9; }
    .summary { height: auto; max-height: 4; }
    .report-table { height: 1fr; min-height: 4; }
    .details { height: 5; min-height: 3; border: solid $primary; }
    #config-form { height: 1fr; }
    #config-form Label { height: 1; }
    #config-form Input { margin-bottom: 1; }
    #config-form Checkbox { height: 3; }
    #config-note { height: auto; }
    #job-summary { height: auto; max-height: 4; }
    #setup-log { height: 5; min-height: 3; }
    #chrome-output, #run-output { height: 1fr; min-height: 4; }
    #run-table { height: 6; min-height: 3; }
    """
    BINDINGS = [
        Binding("q", "leave", "종료"), Binding("escape", "navigation", "메뉴"),
        Binding("r", "refresh", "새로고침"), Binding("a", "authenticate", "권한 인증"),
        Binding("ctrl+s", "save", "구성 저장", show=False),
    ]

    def __init__(self, *, service, initial_section="dashboard", **kwargs):
        super().__init__(**kwargs)
        self.service = service
        self.section = initial_section if initial_section in dict(SECTIONS) else "dashboard"
        self._stored_config: dict[str, Any] | None = None
        self._plan_config: dict[str, Any] | None = None
        self._last_plan: dict[str, Any] | None = None
        self._reports: dict[str, list[dict[str, Any]]] = {}
        self._active_job: str | None = None
        self._selected_job: str | None = None
        self._polling = False
        self._poll_paused = False
        self._submitting = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static("화면이 작습니다. 80×24 이상으로 늘리거나 CLI 하위 명령을 사용하세요.", id="small-terminal", markup=False)
        with Horizontal(id="toolbar"):
            yield Button("권한 인증", id="authenticate")
            yield Button("새로고침", id="refresh")
        yield Static("읽기 전용 상태 조회 중입니다. 필요하면 'a'로 sudo 권한을 인증하세요.", id="notice", markup=False)
        with Horizontal(id="body"):
            yield OptionList(*(Option(title, id=key) for key, title in SECTIONS), id="navigation")
            with ContentSwitcher(initial=self.section, id="content"):
                with Vertical(id="dashboard", classes="panel"):
                    yield Static("상태 요약", id="dashboard-summary", classes="summary", markup=False)
                    yield DataTable(id="dashboard-table", classes="report-table", cursor_type="row")
                    yield TextArea("", id="dashboard-detail", classes="details", read_only=True)
                with Vertical(id="config", classes="panel"):
                    with VerticalScroll(id="config-form", disabled=True):
                        yield Label("화면 사용자 (기존 일반 계정)")
                        yield Input(id="config-user", placeholder="user")
                        yield Label("DID URL (로그인 없는 http/https, 미정이면 비움)")
                        yield Input(id="config-url", placeholder="https://example.com")
                        yield Label("시간대")
                        yield Input("Asia/Seoul", id="config-timezone")
                        yield Label("SSH 방화벽 zone (비우면 감지)")
                        yield Input(id="config-ssh_zone")
                        for key, title in BOOL_FIELDS:
                            yield Checkbox(title, id=f"config-{key}")
                        yield Static("표시용 새 Chrome 프로필만 키링 비사용으로 실행합니다.\n이 프로필에 계정 암호·인증정보를 저장하지 마세요.", id="config-note", markup=False)
                    with Horizontal(classes="actions"):
                        yield Button("구성 저장", id="save-config", variant="primary", disabled=True)
                        yield Button("다시 읽기", id="reload-config")
                with Vertical(id="setup", classes="panel"):
                    with Horizontal(classes="actions"):
                        yield Button("변경 계획", id="make-plan", variant="primary")
                        yield Button("계획 적용", id="apply-plan", variant="warning", disabled=True)
                    yield Static("변경 계획을 먼저 확인하세요.", id="plan-summary", classes="summary", markup=False)
                    yield DataTable(id="plan-table", classes="report-table", cursor_type="row")
                    yield TextArea("", id="plan-detail", classes="details", read_only=True)
                    yield Static("설치 실행 기록이 없습니다.", id="job-summary", markup=False)
                    yield TextArea("", id="setup-log", read_only=True)
                with Vertical(id="check", classes="panel"):
                    yield Static("설정·서비스를 변경하지 않고 검사합니다.", id="check-summary", classes="summary", markup=False)
                    yield DataTable(id="check-table", classes="report-table", cursor_type="row")
                    yield TextArea("", id="check-detail", classes="details", read_only=True)
                with Vertical(id="chrome", classes="panel"):
                    with Horizontal(classes="actions"):
                        yield Button("시작", id="chrome-start")
                        yield Button("중지", id="chrome-stop")
                        yield Button("재시작", id="chrome-restart")
                    with Horizontal(classes="actions"):
                        yield Button("상태", id="chrome-status")
                        yield Button("로그", id="chrome-logs")
                        yield Button("검사", id="chrome-check")
                    yield Static("중지는 현재 실행만 종료합니다. 다음 로그인·부팅에서는 자동 시작합니다.", classes="summary", markup=False)
                    yield TextArea("", id="chrome-output", read_only=True)
                with Vertical(id="runs", classes="panel"):
                    yield Static("실행을 선택하고 Enter로 결과·로그를 확인하세요.", classes="summary", markup=False)
                    yield DataTable(id="run-table", cursor_type="row")
                    yield TextArea("", id="run-output", read_only=True)
        yield Footer()

    def on_mount(self):
        for section in ("dashboard", "plan", "check"):
            self.query_one(f"#{section}-table", DataTable).add_columns("항목", "상태")
        self.query_one("#run-table", DataTable).add_columns("실행 ID", "상태", "시작")
        navigation = self.query_one("#navigation", OptionList)
        navigation.highlighted = [key for key, _ in SECTIONS].index(self.section)
        navigation.focus()
        self._resize_warning()
        self.set_interval(1.5, self.poll_job)
        self.refresh_section()

    def on_resize(self, event: events.Resize):
        if self.is_mounted:
            self._resize_warning(event.size)

    def _resize_warning(self, size=None):
        size = self.size if size is None else size
        self.query_one("#small-terminal").display = size.width < 80 or size.height < 24

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in {"leave", "refresh", "authenticate"} and isinstance(self.focused, Input):
            return False
        return True

    def notice(self, message: str):
        self.query_one("#notice", Static).update(Text(message))

    def invalidate_plan(self):
        self._plan_config = None
        self._last_plan = None
        self.query_one("#apply-plan", Button).disabled = True

    async def api(self, action, payload=None):
        try:
            return await self.service.call(action, payload)
        except Exception as exc:
            self.notice(f"실패: {exc} · 권한이 만료됐다면 Esc → a로 인증 후 다시 요청하세요.")
            return None

    def refresh_section(self):
        self.run_worker(self.load_section(self.section), group="section", exclusive=True, exit_on_error=False)

    async def load_section(self, section):
        if section == "dashboard":
            value = await self.api("status")
            if value is None:
                return
            self.SUB_TITLE = str(value.get("hostname", "SSH 환경 관리"))
            self.render_report("dashboard", value)
            config = value.get("config", {})
            active = next((r for r in value.get("runs", []) if r.get("state") in ACTIVE_JOBS), None)
            self.query_one("#dashboard-summary", Static).update(Text(
                f"서버: {value.get('hostname', '-')} · 화면 계정: {config.get('user') or '미지정'}\n"
                f"진행 작업: {active['run_id'] if active else '없음'}"))
            if active:
                self._active_job = active["run_id"]
                self._selected_job = self._active_job
                self.render_job(active)
            self.notice("상태를 조회했습니다. r은 현재 영역만 새로고침합니다.")
        elif section == "config":
            await self.load_config()
        elif section == "setup":
            self.notice("변경 계획을 확인한 후 적용하세요. 설치가 시작되면 q로 나가도 작업은 계속됩니다.")
        elif section == "check":
            value = await self.api("inspect")
            if value is not None:
                self.render_report("check", value)
                self.notice("읽기 전용 검사를 마쳤습니다.")
        elif section == "chrome":
            await self.chrome_action("status")
        elif section == "runs":
            await self.load_runs()

    @on(OptionList.OptionSelected, "#navigation")
    def choose_section(self, event: OptionList.OptionSelected):
        section = event.option.id
        if section is None:
            return
        self.section = section
        self.query_one("#content", ContentSwitcher).current = section
        self.refresh_section()

    def action_navigation(self):
        self.query_one("#navigation", OptionList).focus()

    def action_leave(self):
        self.exit()

    def action_refresh(self):
        self._poll_paused = False
        self.refresh_section()

    def action_save(self):
        if self.section == "config":
            self.run_worker(self.save_config(), group="save", exclusive=True, exit_on_error=False)

    def action_authenticate(self):
        self.run_worker(self.authenticate(), group="auth", exclusive=True, exit_on_error=False)

    async def authenticate(self):
        try:
            # Password entry is handled by sudo in the real terminal, never by a UI input.
            if self.is_headless:
                self.service.authenticate()
            else:
                with self.suspend():
                    self.service.authenticate()
            self.notice("권한을 인증했습니다. 필요한 작업을 다시 선택하세요.")
            self._poll_paused = False
            self.invalidate_plan()
            # Authentication must not throw away an edited, not-yet-saved form.
            if self.section != "config" or self._stored_config is None:
                self.refresh_section()
        except Exception as exc:
            self.notice(f"권한 인증 실패: {exc}")

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed):
        key = event.button.id
        if key == "authenticate":
            self.action_authenticate()
        elif key == "refresh":
            self.action_refresh()
        elif key == "save-config":
            self.action_save()
        elif key == "reload-config":
            self.run_worker(self.load_config(), group="section", exclusive=True, exit_on_error=False)
        elif key == "make-plan":
            self.run_worker(self.make_plan(), group="plan", exclusive=True, exit_on_error=False)
        elif key == "apply-plan":
            if self._plan_config is not None and not self._submitting:
                self.push_screen(ApplyConfirmation(), self.confirmed_apply)
        elif key and key.startswith("chrome-"):
            self.run_worker(self.chrome_action(key.removeprefix("chrome-")), group="chrome", exclusive=True, exit_on_error=False)

    def confirmed_apply(self, confirmed):
        if confirmed:
            self.run_worker(self.submit_plan(), group="submit", exclusive=True, exit_on_error=False)
        else:
            self.notice("적용을 취소했습니다. 시스템 설정은 변경하지 않았습니다.")

    async def load_config(self):
        value = await self.api("config-show")
        if value is None:
            return
        config = Config.from_dict(value["config"])
        self._stored_config = config.to_dict()
        for key in ("user", "url", "timezone", "ssh_zone"):
            self.query_one(f"#config-{key}", Input).value = str(getattr(config, key) or "")
        for key, _ in BOOL_FIELDS:
            self.query_one(f"#config-{key}", Checkbox).value = getattr(config, key)
        self.query_one("#config-form").disabled = False
        self.query_one("#save-config", Button).disabled = False
        self.notice("기대 구성을 읽었습니다. 저장은 설치·설정을 적용하지 않습니다.")

    def draft_config(self):
        data = Config().to_dict()
        for key in ("user", "url", "timezone", "ssh_zone"):
            value = self.query_one(f"#config-{key}", Input).value.strip()
            data[key] = (value or None) if key in {"url", "ssh_zone"} else value
        for key, _ in BOOL_FIELDS:
            data[key] = self.query_one(f"#config-{key}", Checkbox).value
        return Config.from_dict(data)

    def config_dirty(self):
        if self._stored_config is None:
            return False
        try:
            return self.draft_config().to_dict() != self._stored_config
        except (ValueError, TypeError):
            return True

    @on(Input.Changed)
    @on(Checkbox.Changed)
    def config_field_changed(self):
        if self._stored_config is not None and self.config_dirty():
            self.invalidate_plan()
            if self.section == "config":
                self.notice("저장하지 않은 구성입니다. Ctrl+S 또는 '구성 저장'으로 저장하세요.")

    async def save_config(self):
        if self._stored_config is None:
            self.notice("먼저 권한을 인증하고 현재 기대 구성을 읽으세요.")
            return
        try:
            config = self.draft_config()
        except (ValueError, TypeError) as exc:
            self.notice(f"구성 오류: {exc}")
            return
        value = await self.api("config-save", config.to_dict())
        if value is not None:
            self._stored_config = value["config"]
            self.invalidate_plan()
            self.notice("구성을 저장했습니다. 설치·설정에서 변경 계획을 다시 확인하세요.")

    async def make_plan(self):
        self.invalidate_plan()
        if self.config_dirty():
            self.notice("편집한 구성을 먼저 저장하거나 다시 읽은 뒤 변경 계획을 확인하세요.")
            return
        before = await self.api("config-show")
        if before is None:
            return
        value = await self.api("plan")
        if value is None:
            return
        after = await self.api("config-show")
        if after is None:
            return
        self.render_report("plan", value)
        if before["config"] != after["config"]:
            self.notice("조사 중 구성이 변경됐습니다. 변경 계획을 다시 확인하세요.")
            return
        blockers = {"blocked", "unknown", "error"}
        if any(r.get("state") in blockers for r in value.get("results", [])):
            self.notice("차단·확인 불가·실패 항목을 해결한 후 계획을 다시 확인하세요.")
            return
        self._plan_config = before["config"]
        self._last_plan = value
        self.query_one("#apply-plan", Button).disabled = self._active_job is not None
        self.notice("변경 계획을 확인했습니다. '계획 적용'에서 한 번 더 확인한 후 시작합니다.")

    async def submit_plan(self):
        if self._submitting or self._plan_config is None or self._active_job:
            return
        self._submitting = True
        reviewed = dict(self._plan_config)
        self.query_one("#apply-plan", Button).disabled = True
        try:
            current = await self.api("config-show")
            if current is None or current["config"] != reviewed:
                self.invalidate_plan()
                self.notice("구성이 바뀌었거나 확인할 수 없습니다. 변경 계획을 다시 확인하세요.")
                return
            job = await self.api("submit", {"expected_config": reviewed})
            self.invalidate_plan()
            if job is not None:
                self._active_job = job["run_id"]
                self._selected_job = job["run_id"]
                self.render_job(job)
                self.notice(f"설치 시작: {job['run_id']} · SSH/화면 종료 후에도 계속 실행합니다.")
        finally:
            self._submitting = False

    def render_report(self, target, value):
        rows = value.get("results", [])
        self._reports[target] = rows
        table = self.query_one(f"#{target}-table", DataTable)
        table.clear()
        for index, result in enumerate(rows):
            table.add_row(Text(str(result.get("id", ""))), Text(STATE_LABELS.get(result.get("state"), str(result.get("state")))), key=str(index))
        text = " / ".join(f"{STATE_LABELS.get(key, key)} {count}" for key, count in value.get("summary", {}).items() if count)
        self.query_one(f"#{target}-summary", Static).update(Text(text or "검사 결과가 없습니다."))
        if rows:
            self.show_result(target, 0)

    @on(DataTable.RowHighlighted)
    def highlight_result(self, event: DataTable.RowHighlighted):
        table_id = event.data_table.id or ""
        if table_id.endswith("-table") and table_id != "run-table":
            target = table_id.removesuffix("-table")
            try:
                self.show_result(target, int(event.row_key.value))
            except (TypeError, ValueError):
                pass

    def show_result(self, target, index):
        rows = self._reports.get(target, [])
        if not 0 <= index < len(rows):
            return
        result = rows[index]
        text = (f"{result.get('id')} [{STATE_LABELS.get(result.get('state'), result.get('state'))}]\n"
                f"기대: {readable(result.get('expected'))}\n현재: {readable(result.get('actual'))}\n"
                f"다음: {result.get('next_action') or '-'}\n{result.get('detail') or ''}")
        self.query_one(f"#{target}-detail", TextArea).load_text(text)

    def render_job(self, job):
        stages = job.get("stages", [])
        current = stages[-1] if stages else None
        text = f"실행: {job['run_id']} · {JOB_LABELS.get(job.get('state'), job.get('state'))}"
        if current:
            text += f"\n최근 단계: {current.get('id')} · {current.get('state')}"
        if job.get("reason"):
            text += f"\n{job['reason']}"
        self.query_one("#job-summary", Static).update(Text(text))
        if job.get("report"):
            self.render_report("plan", job["report"])

    async def poll_job(self):
        if self._polling or self._poll_paused or self._active_job is None:
            return
        self._polling = True
        run_id = self._active_job
        try:
            job = await self.api("runs-show", {"run_id": run_id})
            if job is None:
                self._poll_paused = True
                return
            self.render_job(job)
            log = await self.api("logs", {"run_id": run_id})
            if log is not None:
                self.query_one("#setup-log", TextArea).load_text(log.get("log", ""))
                if self._selected_job == run_id:
                    self.query_one("#run-output", TextArea).load_text(readable(job) + "\n\n" + log.get("log", ""))
            if job.get("state") not in ACTIVE_JOBS:
                self._active_job = None
                self.notice(f"설치 결과: {JOB_LABELS.get(job.get('state'), job.get('state'))} · 실행 기록에서 결과와 다음 조치를 확인하세요.")
        finally:
            self._polling = False

    async def chrome_action(self, action):
        value = await self.api("chrome", {"action": action})
        if value is not None:
            self.query_one("#chrome-output", TextArea).load_text(value["log"] if isinstance(value.get("log"), str) else readable(value))
            result = {0: "요청 처리", 1: "실패·확인 필요", 2: "적용 대기"}.get(value.get("exit_code", 0), "확인 필요")
            self.notice(f"Chrome {action}: {result}. {value.get('reason') or ''}")

    async def load_runs(self):
        value = await self.api("runs-list")
        if value is None:
            return
        table = self.query_one("#run-table", DataTable)
        table.clear()
        for job in value.get("runs", []):
            table.add_row(Text(job["run_id"]), Text(JOB_LABELS.get(job.get("state"), job.get("state", ""))), Text(job.get("started_at", "")), key=job["run_id"])
        self.notice("실행 기록을 읽었습니다. Enter로 선택한 결과와 로그를 엽니다.")

    @on(DataTable.RowSelected, "#run-table")
    def select_run(self, event: DataTable.RowSelected):
        if event.row_key.value:
            self.run_worker(self.load_run(str(event.row_key.value)), group="run-detail", exclusive=True, exit_on_error=False)

    async def load_run(self, run_id):
        job = await self.api("runs-show", {"run_id": run_id})
        if job is None:
            return
        self._selected_job = run_id
        if job.get("state") in ACTIVE_JOBS:
            self._active_job = run_id
        log = await self.api("logs", {"run_id": run_id})
        self.query_one("#run-output", TextArea).load_text(readable(job) + "\n\n" + (log or {}).get("log", ""))
