from __future__ import annotations

import json
import threading
import traceback
from pathlib import Path
from typing import Any

from epub2yaml.app.services import PipelineService
from epub2yaml.domain.enums import ControlAction
from epub2yaml.domain.services import parse_yaml_mapping_document
from epub2yaml.llm.model_factory import create_document_update_chain_from_env

MAX_RECENT_RUN_BUTTONS = 8


def run_control_ui(workspace_dir: Path, book_id: str | None = None) -> None:
    try:
        from textual.app import App, ComposeResult
        from textual.containers import Horizontal, Vertical
        from textual.widgets import Button, Footer, Header, Input, Log, Static
    except ModuleNotFoundError as exc:
        raise RuntimeError("未安装 textual，无法启动 control-ui") from exc

    service = PipelineService(
        workspace_dir,
        document_update_chain=create_document_update_chain_from_env(),
    )
    runs_dir = workspace_dir / "runs"
    ui_state_path = runs_dir / ".control_ui_state.json"

    def _load_ui_memory() -> dict[str, str]:
        if not ui_state_path.exists():
            return {}
        try:
            payload = json.loads(ui_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            "epub_path": str(payload.get("epub_path") or ""),
            "book_id": str(payload.get("book_id") or ""),
        }

    def _save_ui_memory(epub_path: str, book_id_value: str) -> None:
        runs_dir.mkdir(parents=True, exist_ok=True)
        ui_state_path.write_text(
            json.dumps(
                {
                    "epub_path": epub_path,
                    "book_id": book_id_value,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _list_recent_runs(limit: int = MAX_RECENT_RUN_BUTTONS) -> list[dict[str, str]]:
        if not runs_dir.exists():
            return []
        records: list[dict[str, str]] = []
        for run_dir in runs_dir.iterdir():
            if not run_dir.is_dir():
                continue
            state_path = run_dir / "state" / "run_state.json"
            if not state_path.exists():
                continue
            try:
                payload = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            source_file = str(payload.get("source_file") or "")
            source_path = str((run_dir / source_file).resolve()) if source_file else ""
            records.append(
                {
                    "book_id": str(payload.get("book_id") or run_dir.name),
                    "status": str(payload.get("status") or "unknown"),
                    "updated_at": str(payload.get("updated_at") or ""),
                    "source_path": source_path,
                }
            )
        records.sort(key=lambda item: item["updated_at"], reverse=True)
        return records[:limit]

    def _load_actor_names(book_id_value: str) -> tuple[list[str], str | None]:
        if not book_id_value:
            return [], None
        actors_path = runs_dir / book_id_value / "current" / "actors.yaml"
        if not actors_path.exists():
            return [], None
        try:
            actors = parse_yaml_mapping_document(actors_path.read_text(encoding="utf-8"), root_key="actors")
        except ValueError as exc:
            return [], str(exc)
        return sorted(actors.keys()), None

    def _read_clipboard_text() -> str | None:
        try:
            import tkinter as tk
        except Exception:
            return None
        root: tk.Tk | None = None
        try:
            root = tk.Tk()
            root.withdraw()
            value = root.clipboard_get()
            text = str(value).strip()
            return text or None
        except Exception:
            return None
        finally:
            if root is not None:
                try:
                    root.destroy()
                except Exception:
                    pass

    remembered_state = _load_ui_memory()

    class ControlApp(App[None]):
        TITLE = "EPUB2YAML Control UI"
        SUB_TITLE = "任务创建、启动、暂停、人工修订、恢复"
        BINDINGS = [
            ("q", "quit", "退出"),
            ("r", "refresh", "刷新"),
            ("f5", "refresh", "刷新"),
            ("ctrl+i", "init_run", "初始化"),
            ("ctrl+s", "start_run", "启动/继续"),
            ("ctrl+p", "pause_run", "暂停"),
            ("ctrl+m", "prepare_manual_edit", "人工修订"),
            ("ctrl+o", "open_manual_edit_workspace", "打开修订区"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self._initial_epub_path = remembered_state.get("epub_path", "")
            self._initial_book_id = book_id or remembered_state.get("book_id", "")
            self._worker_lock = threading.Lock()
            self._worker_thread: threading.Thread | None = None
            self._worker_kind: str | None = None
            self._last_status: dict[str, Any] | None = None
            self._recent_runs: list[dict[str, str]] = []

        def compose(self) -> ComposeResult:
            yield Header()
            with Vertical():
                with Horizontal():
                    yield Input(value=self._initial_epub_path, placeholder="EPUB 文件路径，例如 F:/projects/epub2summary/xxx.epub", id="epub_path")
                    yield Button("Paste EPUB", id="paste_epub")
                with Horizontal():
                    yield Input(value=self._initial_book_id, placeholder="book_id", id="book_id")
                    yield Button("Paste Book ID", id="paste_book_id")
                with Horizontal():
                    yield Button("Init Run", id="init_run")
                    yield Button("Start / Continue", id="start_run")
                    yield Button("Pause", id="pause")
                    yield Button("Prepare Manual Edit", id="prepare_manual_edit")
                    yield Button("Resume", id="resume")
                    yield Button("Open Manual Edit Workspace", id="open_manual_edit_workspace")
                yield Static(id="status")
                yield Static(id="details")
                yield Static(id="history_runs")
                with Horizontal():
                    for index in range(MAX_RECENT_RUN_BUTTONS):
                        yield Button(f"Recent {index + 1}", id=f"recent_run_{index}")
                yield Static(id="actor_list")
                yield Log(id="log", highlight=False)
            yield Footer()

        def on_mount(self) -> None:
            self.set_interval(0.5, self.refresh_status)
            self.call_after_refresh(self._focus_primary_input)
            self.refresh_status()
            self._log("快捷键: Ctrl+I 初始化 | Ctrl+S 启动/继续 | Ctrl+P 暂停 | Ctrl+M 人工修订 | Ctrl+O 打开修订区 | R/F5 刷新 | Q 退出")
            self._log("提示: 可用 Paste EPUB / Paste Book ID 按钮从系统剪贴板读取内容。")
            self._log("提示: 历史任务现在可以直接点 Recent 按钮回填，不需要每次重输。")
            self._log("注意: 人工修订编辑器关闭并校验通过后，后台任务会自动继续，无需再次点击 Start / Continue。")

        def _focus_primary_input(self) -> None:
            target = "#book_id" if self._initial_epub_path else "#epub_path"
            self.query_one(target, Input).focus()

        def action_refresh(self) -> None:
            self.refresh_status()

        def action_init_run(self) -> None:
            self._handle_init_run()

        def action_start_run(self) -> None:
            self._start_background_job("run_to_completion")

        def action_pause_run(self) -> None:
            self._request_pause()

        def action_prepare_manual_edit(self) -> None:
            self._request_manual_edit()

        def action_open_manual_edit_workspace(self) -> None:
            self._open_manual_edit_workspace()

        def on_button_pressed(self, event: Button.Pressed) -> None:
            button_id = event.button.id or ""
            if button_id == "init_run":
                self._handle_init_run()
            elif button_id == "start_run":
                self._start_background_job("run_to_completion")
            elif button_id == "pause":
                self._request_pause()
            elif button_id == "prepare_manual_edit":
                self._request_manual_edit()
            elif button_id == "resume":
                self._start_background_job("resume_run")
            elif button_id == "open_manual_edit_workspace":
                self._open_manual_edit_workspace()
            elif button_id == "paste_epub":
                self._paste_into_input("#epub_path", label="EPUB 路径")
            elif button_id == "paste_book_id":
                self._paste_into_input("#book_id", label="book_id")
            elif button_id.startswith("recent_run_"):
                self._select_recent_run(button_id)

        def refresh_status(self) -> None:
            current_book_id = self._book_id()
            payload: dict[str, Any] | None = None
            if current_book_id:
                try:
                    payload = service.show_status(current_book_id)
                except Exception:
                    payload = None
            self._last_status = payload
            self._recent_runs = _list_recent_runs()

            running_label = "idle"
            with self._worker_lock:
                if self._worker_thread is not None and self._worker_thread.is_alive():
                    running_label = f"running:{self._worker_kind}"
                elif self._worker_kind is not None:
                    running_label = f"finished:{self._worker_kind}"
                    self._worker_kind = None
                    self._worker_thread = None

            status_widget = self.query_one("#status", Static)
            details_widget = self.query_one("#details", Static)
            history_widget = self.query_one("#history_runs", Static)
            actor_widget = self.query_one("#actor_list", Static)

            if self._recent_runs:
                history_widget.update(
                    "历史任务（点击下方 Recent 按钮直接载入）:\n" + "\n".join(
                        f"{index + 1}. {item['book_id']} | {item['status']} | {item['updated_at']}"
                        for index, item in enumerate(self._recent_runs)
                    )
                )
            else:
                history_widget.update("历史任务:\n- 暂无")

            for index in range(MAX_RECENT_RUN_BUTTONS):
                button = self.query_one(f"#recent_run_{index}", Button)
                if index < len(self._recent_runs):
                    item = self._recent_runs[index]
                    button.label = f"{index + 1}:{item['book_id']}"
                    button.display = True
                else:
                    button.label = f"Recent {index + 1}"
                    button.display = False

            actor_names, actor_error = _load_actor_names(current_book_id)
            if actor_error:
                actor_widget.update(f"历史人物:\n- 读取失败: {actor_error}")
            elif actor_names:
                preview = "、".join(actor_names[:30])
                suffix = " …" if len(actor_names) > 30 else ""
                actor_widget.update(f"历史人物({len(actor_names)}):\n{preview}{suffix}")
            else:
                actor_widget.update("历史人物:\n- 暂无")

            if payload is None:
                status_widget.update(
                    "\n".join(
                        [
                            f"book_id: {current_book_id or '-'}",
                            f"worker: {running_label}",
                            "status: 未初始化或状态文件不存在",
                        ]
                    )
                )
                details_widget.update(
                    "\n".join(
                        [
                            "recommended_action: -",
                            "current_batch: -",
                            "manual_edit_workspace: -",
                            "last_failure: -",
                        ]
                    )
                )
                return

            current_batch = payload.get("manual_edit_batch_id") or payload.get("pending_review_batch_id") or payload.get("last_generated_batch_id")
            session = payload.get("manual_edit_session") or {}
            status_widget.update(
                "\n".join(
                    [
                        f"book_id: {payload['book_id']}",
                        f"worker: {running_label}",
                        f"status: {payload['status']}",
                        f"recommended_action: {payload['recommended_action']}",
                        f"current_batch: {current_batch}",
                    ]
                )
            )
            details_widget.update(
                "\n".join(
                    [
                        f"next_chapter_index: {payload['next_chapter_index']} / {payload['total_chapters']}",
                        f"last_failed_batch_id: {payload.get('last_failed_batch_id')}",
                        f"last_failed_stage: {payload.get('last_failed_stage')}",
                        f"manual_edit_workspace: {payload.get('manual_edit_workspace')}",
                        f"manual_edit_session_status: {session.get('status')}",
                        f"manual_edit_editor_exit: {session.get('editor_exit_code')}",
                    ]
                )
            )

        def _handle_init_run(self) -> None:
            epub_path_text = self.query_one("#epub_path", Input).value.strip()
            book_id_text = self._book_id()
            if not epub_path_text:
                self._log("error: 请先填写 EPUB 路径")
                return
            try:
                state = service.init_run(Path(epub_path_text), book_id=book_id_text or None)
            except Exception as exc:
                self._log(f"error: 初始化失败: {exc}")
                return
            self.query_one("#book_id", Input).value = state.book_id
            _save_ui_memory(epub_path_text, state.book_id)
            self._log(f"initialized: book_id={state.book_id} total_chapters={state.total_chapters}")
            self.refresh_status()

        def _start_background_job(self, kind: str) -> None:
            current_book_id = self._book_id()
            epub_path_text = self.query_one("#epub_path", Input).value.strip()
            if not current_book_id:
                self._log("error: 请先填写或初始化 book_id")
                return
            if epub_path_text:
                _save_ui_memory(epub_path_text, current_book_id)
            with self._worker_lock:
                if self._worker_thread is not None and self._worker_thread.is_alive():
                    self._log_running_worker_hint()
                    return
                self._worker_kind = kind
                self._worker_thread = threading.Thread(target=self._run_background_job, args=(kind, current_book_id), daemon=True)
                self._worker_thread.start()
            self._log(f"worker started: {kind} book_id={current_book_id}")
            self.refresh_status()

        def _run_background_job(self, kind: str, current_book_id: str) -> None:
            try:
                if kind == "run_to_completion":
                    result = service.run_to_completion(current_book_id, progress_callback=self._threadsafe_progress)
                    self.call_from_thread(self._log, f"worker finished: run_to_completion status={result['status']} processed={result['processed_batches']}")
                elif kind == "resume_run":
                    decision = service.resume_run(current_book_id)
                    self.call_from_thread(self._log, f"resume decision: {decision.action} batch={decision.batch_id}")
                    result = service.run_to_completion(current_book_id, progress_callback=self._threadsafe_progress)
                    self.call_from_thread(self._log, f"worker finished: resumed status={result['status']} processed={result['processed_batches']}")
                else:
                    self.call_from_thread(self._log, f"error: 未知后台任务 {kind}")
            except Exception:
                self.call_from_thread(self._log, f"worker error:\n{traceback.format_exc()}")
            finally:
                self.call_from_thread(self.refresh_status)

        def _threadsafe_progress(self, event: dict[str, Any]) -> None:
            event_name = event.get("event")
            if event_name == "batch_started":
                message = f"batch started: {event['batch_id']} action={event.get('recovery_action')} next={event['next_chapter_index']}"
            elif event_name == "batch_completed":
                message = f"batch completed: {event['batch_id']} action={event.get('recovery_action')} next={event['next_chapter_index']}"
            elif event_name == "control_interrupted":
                message = f"control interrupted: action={event.get('control_action')} batch={event.get('batch_id')}"
            elif event_name == "run_initialized":
                message = f"run initialized: book_id={event['book_id']} total={event['total_chapters']}"
            else:
                message = json.dumps(event, ensure_ascii=False)
            self.call_from_thread(self._log, message)
            self.call_from_thread(self.refresh_status)

        def _request_pause(self) -> None:
            current_book_id = self._book_id()
            if not current_book_id:
                self._log("error: 请先填写或初始化 book_id")
                return
            try:
                state = service.request_control_action(current_book_id, ControlAction.PAUSE)
            except Exception as exc:
                self._log(f"error: pause 失败: {exc}")
                return
            self._log(f"pause requested at {state.control_requested_at}")
            self.refresh_status()

        def _request_manual_edit(self) -> None:
            current_book_id = self._book_id()
            if not current_book_id:
                self._log("error: 请先填写或初始化 book_id")
                return
            try:
                state = service.request_control_action(current_book_id, ControlAction.PREPARE_MANUAL_EDIT)
            except Exception as exc:
                self._log(f"error: prepare_manual_edit 失败: {exc}")
                return
            self._log(f"prepare_manual_edit requested at {state.control_requested_at}")
            self._log("提示: 编辑器关闭并校验成功后，后台任务会自动继续，无需再次点击 Start / Continue。")
            self.refresh_status()

        def _open_manual_edit_workspace(self) -> None:
            current_book_id = self._book_id()
            if not current_book_id:
                self._log("error: 请先填写或初始化 book_id")
                return
            try:
                session = service.open_manual_edit_workspace(current_book_id)
            except Exception as exc:
                self._log(f"error: 打开人工修订工作区失败: {exc}")
                return
            self._log(
                f"editor reopened: command={session.editor_command} exit={session.editor_exit_code} error={session.last_error}"
            )
            self.refresh_status()

        def _paste_into_input(self, selector: str, *, label: str) -> None:
            value = _read_clipboard_text()
            if not value:
                self._log(f"error: 剪贴板为空，无法粘贴到 {label}")
                return
            self.query_one(selector, Input).value = value
            if selector == "#book_id":
                _save_ui_memory(self.query_one("#epub_path", Input).value.strip(), value)
            self._log(f"pasted into {label}")
            self.refresh_status()

        def _select_recent_run(self, button_id: str) -> None:
            try:
                index = int(button_id.rsplit("_", maxsplit=1)[-1])
            except ValueError:
                self._log(f"error: 无法识别历史任务按钮 {button_id}")
                return
            if index < 0 or index >= len(self._recent_runs):
                self._log("error: 历史任务索引超出范围")
                return
            item = self._recent_runs[index]
            self.query_one("#book_id", Input).value = item["book_id"]
            if item.get("source_path"):
                self.query_one("#epub_path", Input).value = item["source_path"]
            _save_ui_memory(self.query_one("#epub_path", Input).value.strip(), item["book_id"])
            self._log(f"selected recent run: {item['book_id']}")
            self.refresh_status()

        def _log_running_worker_hint(self) -> None:
            payload = self._last_status or {}
            session = payload.get("manual_edit_session") or {}
            if session.get("status") == "applied":
                self._log("提示: 人工修订已应用，当前后台任务仍在继续处理，无需再次点击 Start / Continue。")
                return
            self._log("error: 当前已有后台任务运行中")

        def _book_id(self) -> str:
            return self.query_one("#book_id", Input).value.strip()

        def _log(self, message: str) -> None:
            self.query_one("#log", Log).write_line(message)

    ControlApp().run()
