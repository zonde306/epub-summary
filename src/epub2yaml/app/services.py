from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from epub2yaml.app.editor import EditorLauncher
from epub2yaml.domain.enums import BatchStatus, ControlAction, ManualEditSessionStatus, ReviewAction, RunStatus
from epub2yaml.domain.models import BatchRecord, DocumentVersion, FailureInfo, ManualEditSession, RecoveryDecision, ReviewDecision, RunState
from epub2yaml.domain.services import build_batches, dump_yaml_document, parse_yaml_mapping_document
from epub2yaml.infra.batch_store import BatchArtifactStore
from epub2yaml.infra.review_store import ReviewQueueStore
from epub2yaml.infra.state_store import StateStore
from epub2yaml.infra.yaml_store import YamlDocumentStore
from epub2yaml.llm.chains.document_update_chain import DocumentUpdateChain
from epub2yaml.utils.hashing import sha256_bytes, sha256_text
from epub2yaml.workflow.graph import WorkflowControlInterrupt, run_batch_generation_workflow
from utils.epub_extract import extract_epub


class PipelineService:
    def __init__(
        self,
        workspace_dir: Path,
        *,
        document_update_chain: DocumentUpdateChain | None = None,
        editor_launcher: EditorLauncher | None = None,
    ) -> None:
        self.workspace_dir = workspace_dir
        self.runs_dir = workspace_dir / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.document_update_chain = document_update_chain
        self.editor_launcher = editor_launcher or EditorLauncher()

    def init_run(self, epub_path: Path, *, book_id: str | None = None) -> RunState:
        resolved_book_id = book_id or epub_path.stem
        run_dir = self.runs_dir / resolved_book_id
        run_dir.mkdir(parents=True, exist_ok=True)

        chapters = extract_epub(str(epub_path))
        if not chapters:
            raise ValueError("未从 EPUB 中提取到可处理章节")

        source_dir = run_dir / "source"
        source_dir.mkdir(parents=True, exist_ok=True)
        source_copy_path = source_dir / "original.epub"
        source_copy_path.write_bytes(epub_path.read_bytes())

        source_bytes = source_copy_path.read_bytes()
        run_state = RunState(
            book_id=resolved_book_id,
            source_file=str(source_copy_path.relative_to(run_dir)),
            source_hash=sha256_bytes(source_bytes),
            total_chapters=len(chapters),
            next_chapter_index=0,
            status=RunStatus.INITIALIZED,
            recommended_action="continue_new_batch",
        )

        state_store = StateStore(run_dir)
        yaml_store = YamlDocumentStore(run_dir)
        state_store.save_chapters(chapters)
        state_store.save_run_state(run_state)
        state_store.append_checkpoint(
            "run_initialized",
            {
                "book_id": resolved_book_id,
                "total_chapters": len(chapters),
            },
        )
        yaml_store.save_current_document("actors", {})
        yaml_store.save_current_document("worldinfo", {})
        return run_state

    def request_control_action(self, book_id: str, action: ControlAction) -> RunState:
        run_dir = self.runs_dir / book_id
        state_store = StateStore(run_dir)
        state_store.request_control_action(action.value)
        return state_store.load_run_state()

    def process_next_batch(self, book_id: str, *, delta_yaml_text: str | None = None) -> BatchRecord:
        run_dir = self.runs_dir / book_id
        state_store = StateStore(run_dir)
        run_state = state_store.load_run_state()
        pending_review = state_store.find_pending_review_batch(run_state)
        if pending_review is not None:
            raise ValueError(f"存在待审阅批次 {pending_review.batch.batch_id}，请先 resume 或 review")
        if run_state.awaiting_manual_edit:
            raise ValueError("当前运行正在等待人工修订完成")
        if run_state.status == RunStatus.PAUSED:
            raise ValueError("当前运行已暂停，请先 resume")
        if run_state.next_chapter_index >= run_state.total_chapters:
            run_state.status = RunStatus.COMPLETED
            run_state.recommended_action = "completed"
            state_store.save_run_state(run_state)
            raise ValueError("所有章节都已处理完成")

        return self._invoke_batch_workflow(
            book_id,
            delta_yaml_text=delta_yaml_text,
            recovery_action="continue_new_batch",
        )

    def run_to_completion(
        self,
        book_id: str,
        *,
        delta_yaml_by_batch: dict[str, str] | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        run_dir = self.runs_dir / book_id
        state_store = StateStore(run_dir)
        processed_batches: list[str] = []

        while True:
            run_state = state_store.load_run_state()
            if run_state.control_action == ControlAction.PAUSE.value:
                run_state.status = RunStatus.PAUSED
                run_state.recommended_action = ControlAction.RESUME.value
                run_state.control_action = None
                run_state.control_requested_at = None
                state_store.save_run_state(run_state)
                break

            if run_state.control_action == ControlAction.RESUME.value and run_state.status == RunStatus.PAUSED:
                run_state.status = RunStatus.RUNNING
                run_state.control_action = None
                run_state.control_requested_at = None
                run_state.recommended_action = "continue_new_batch"
                state_store.save_run_state(run_state)
                run_state = state_store.load_run_state()

            if run_state.control_action == ControlAction.PREPARE_MANUAL_EDIT.value and run_state.last_generated_batch_id:
                session = self.prepare_manual_edit(book_id, batch_id=run_state.last_generated_batch_id, open_editor=True)
                if session.status != ManualEditSessionStatus.APPLIED:
                    break
                run_state = state_store.load_run_state()

            decision = self.get_recovery_decision(book_id)
            if decision.action == "completed":
                final_state = state_store.load_run_state()
                final_state.status = RunStatus.COMPLETED
                final_state.recommended_action = "completed"
                state_store.save_run_state(final_state)
                break

            if decision.action in {"paused", "await_manual_edit"}:
                break

            target_batch_id = decision.batch_id or self._predict_next_batch_id(state_store.load_run_state())
            if progress_callback is not None:
                current_state = state_store.load_run_state()
                progress_callback(
                    {
                        "event": "batch_started",
                        "book_id": book_id,
                        "batch_id": target_batch_id,
                        "processed_batches": len(processed_batches),
                        "total_chapters": current_state.total_chapters,
                        "next_chapter_index": current_state.next_chapter_index,
                        "recovery_action": decision.action,
                    }
                )

            try:
                if decision.action == "resume_pending_review":
                    self.commit_batch(book_id, batch_id=target_batch_id, action=ReviewAction.ACCEPT, reviewer="system-auto")
                    processed_batches.append(target_batch_id)
                elif decision.action == "retry_failed_batch":
                    record = self.retry_batch(
                        book_id,
                        batch_id=target_batch_id,
                        delta_yaml_text=(delta_yaml_by_batch or {}).get(target_batch_id),
                    )
                    self.commit_batch(book_id, batch_id=record.batch.batch_id, action=ReviewAction.ACCEPT, reviewer="system-auto")
                    processed_batches.append(record.batch.batch_id)
                elif decision.action == "continue_after_manual_edit":
                    record = self.continue_after_manual_edit(
                        book_id,
                        delta_yaml_text=(delta_yaml_by_batch or {}).get(target_batch_id),
                    )
                    self.commit_batch(book_id, batch_id=record.batch.batch_id, action=ReviewAction.ACCEPT, reviewer="system-auto")
                    processed_batches.append(record.batch.batch_id)
                elif decision.action == "continue_new_batch":
                    record = self.process_next_batch(
                        book_id,
                        delta_yaml_text=(delta_yaml_by_batch or {}).get(target_batch_id),
                    )
                    self.commit_batch(book_id, batch_id=record.batch.batch_id, action=ReviewAction.ACCEPT, reviewer="system-auto")
                    processed_batches.append(record.batch.batch_id)
                else:
                    raise ValueError(decision.reason or f"不支持的恢复动作: {decision.action}")
            except WorkflowControlInterrupt as interrupt:
                if progress_callback is not None:
                    progress_callback(
                        {
                            "event": "control_interrupted",
                            "book_id": book_id,
                            "batch_id": interrupt.batch_id,
                            "control_action": interrupt.action,
                        }
                    )
                follow_up = self._handle_workflow_control_interrupt(book_id, interrupt)
                if follow_up == "continue":
                    continue
                break

            latest_state = state_store.load_run_state()
            if progress_callback is not None:
                progress_callback(
                    {
                        "event": "batch_completed",
                        "book_id": book_id,
                        "batch_id": target_batch_id,
                        "processed_batches": len(processed_batches),
                        "total_chapters": latest_state.total_chapters,
                        "next_chapter_index": latest_state.next_chapter_index,
                        "recovery_action": decision.action,
                    }
                )

        final_state = state_store.load_run_state()
        actors_path = run_dir / "current" / "actors.yaml"
        worldinfo_path = run_dir / "current" / "worldinfo.yaml"
        return {
            "book_id": book_id,
            "status": final_state.status,
            "processed_batches": processed_batches,
            "actors_path": str(actors_path),
            "worldinfo_path": str(worldinfo_path),
        }

    def generate_yaml(
        self,
        epub_path: Path,
        *,
        book_id: str | None = None,
        delta_yaml_by_batch: dict[str, str] | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        run_state = self.init_run(epub_path, book_id=book_id)
        if progress_callback is not None:
            progress_callback(
                {
                    "event": "run_initialized",
                    "book_id": run_state.book_id,
                    "total_chapters": run_state.total_chapters,
                    "next_chapter_index": run_state.next_chapter_index,
                }
            )
        result = self.run_to_completion(
            run_state.book_id,
            delta_yaml_by_batch=delta_yaml_by_batch,
            progress_callback=progress_callback,
        )
        result["total_chapters"] = run_state.total_chapters
        return result

    def review_batch(
        self,
        book_id: str,
        *,
        batch_id: str,
        action: ReviewAction,
        reviewer: str | None = None,
        comment: str | None = None,
        edited_actors_text: str | None = None,
        edited_worldinfo_text: str | None = None,
    ) -> ReviewDecision:
        return self.commit_batch(
            book_id,
            batch_id=batch_id,
            action=action,
            reviewer=reviewer,
            comment=comment,
            edited_actors_text=edited_actors_text,
            edited_worldinfo_text=edited_worldinfo_text,
        )

    def prepare_manual_edit(
        self,
        book_id: str,
        *,
        batch_id: str | None = None,
        open_editor: bool = True,
    ) -> ManualEditSession:
        run_dir = self.runs_dir / book_id
        state_store = StateStore(run_dir)
        yaml_store = YamlDocumentStore(run_dir)
        run_state = state_store.load_run_state()
        record, batch = self._resolve_manual_edit_target(book_id, batch_id=batch_id)
        workspace_dir = state_store.get_manual_edit_workspace_dir()
        actors_text = self._load_current_document_text(yaml_store.current_dir / "actors.yaml", "actors")
        worldinfo_text = self._load_current_document_text(yaml_store.current_dir / "worldinfo.yaml", "worldinfo")
        actors_path = workspace_dir / "actors.editable.yaml"
        worldinfo_path = workspace_dir / "worldinfo.editable.yaml"
        note_path = workspace_dir / "note.txt"
        actors_path.write_text(actors_text, encoding="utf-8")
        worldinfo_path.write_text(worldinfo_text, encoding="utf-8")
        note_path.write_text(self._build_manual_edit_note(book_id, batch.batch_id, batch.start_chapter_index, batch.end_chapter_index), encoding="utf-8")

        session = ManualEditSession(
            book_id=book_id,
            batch_id=batch.batch_id,
            chapter_start=batch.start_chapter_index,
            chapter_end=batch.end_chapter_index,
            workspace_dir=str(workspace_dir.relative_to(run_dir)),
            editable_actors_path=str(actors_path.relative_to(run_dir)),
            editable_worldinfo_path=str(worldinfo_path.relative_to(run_dir)),
            note_path=str(note_path.relative_to(run_dir)),
            source_current_actors_hash=sha256_text(actors_text),
            source_current_worldinfo_hash=sha256_text(worldinfo_text),
            status=ManualEditSessionStatus.ACTIVE,
        )

        if open_editor:
            launch_result = self.editor_launcher.open(actors_path)
            session = session.model_copy(
                update={
                    "editor_command": launch_result.command,
                    "editor_exit_code": launch_result.exit_code,
                    "last_error": launch_result.error,
                }
            )

        state_store.save_manual_edit_session(session)
        preview_exists = self._batch_preview_exists(run_dir, batch.batch_id)
        record = record or BatchRecord(batch=batch, status=BatchStatus.MANUAL_EDIT_REQUESTED)
        record.status = BatchStatus.AWAITING_MANUAL_EDIT_RESUME if preview_exists else BatchStatus.CANCELLED_FOR_MANUAL_EDIT
        record.manual_edit_session = session
        record.manual_edit_requested_at = datetime.utcnow()
        state_store.save_batch_record(record)

        run_state.status = RunStatus.AWAITING_MANUAL_EDIT
        run_state.awaiting_manual_edit = True
        run_state.manual_edit_batch_id = batch.batch_id
        run_state.manual_edit_workspace = str(workspace_dir.relative_to(run_dir))
        run_state.manual_edit_applied = False
        run_state.resume_from_manual_edit = False
        run_state.pending_review_batch_id = None
        run_state.control_action = None
        run_state.control_requested_at = None
        run_state.recommended_action = "await_manual_edit"
        run_state.last_recovery_action = "prepare_manual_edit"
        run_state.last_recovery_batch_id = batch.batch_id
        state_store.save_run_state(run_state)
        state_store.append_checkpoint(
            "manual_edit_prepared",
            {
                "batch_id": batch.batch_id,
                "chapter_start": batch.start_chapter_index,
                "chapter_end": batch.end_chapter_index,
                "workspace_dir": str(workspace_dir.relative_to(run_dir)),
            },
        )

        if not open_editor:
            return session
        if session.last_error or (session.editor_exit_code not in {None, 0}):
            return session
        try:
            return self.apply_manual_edit_session(book_id)
        except ValueError:
            return state_store.load_manual_edit_session() or session

    def apply_manual_edit_session(self, book_id: str) -> ManualEditSession:
        run_dir = self.runs_dir / book_id
        state_store = StateStore(run_dir)
        yaml_store = YamlDocumentStore(run_dir)
        run_state = state_store.load_run_state()
        session = state_store.load_manual_edit_session()
        if session is None:
            raise ValueError("当前不存在人工修订会话")

        actors_path = run_dir / session.editable_actors_path
        worldinfo_path = run_dir / session.editable_worldinfo_path
        actors_text = actors_path.read_text(encoding="utf-8")
        worldinfo_text = worldinfo_path.read_text(encoding="utf-8")

        try:
            actors_content = parse_yaml_mapping_document(actors_text, root_key="actors")
            worldinfo_content = parse_yaml_mapping_document(worldinfo_text, root_key="worldinfo")
        except ValueError as exc:
            session = session.model_copy(update={"last_error": str(exc)})
            state_store.save_manual_edit_session(session)
            run_state.status = RunStatus.AWAITING_MANUAL_EDIT
            run_state.awaiting_manual_edit = True
            run_state.manual_edit_applied = False
            run_state.resume_from_manual_edit = False
            run_state.recommended_action = "await_manual_edit"
            state_store.save_run_state(run_state)
            raise

        yaml_store.save_current_document("actors", actors_content)
        yaml_store.save_current_document("worldinfo", worldinfo_content)
        session = session.model_copy(
            update={
                "status": ManualEditSessionStatus.APPLIED,
                "applied_at": datetime.utcnow(),
                "last_error": None,
            }
        )
        state_store.save_manual_edit_session(session)
        record = state_store.load_batch_record(session.batch_id) or BatchRecord(
            batch=state_store.load_batch_input_model(session.batch_id),
            status=BatchStatus.AWAITING_MANUAL_EDIT_RESUME,
        )
        record.status = BatchStatus.AWAITING_MANUAL_EDIT_RESUME
        record.manual_edit_session = session
        state_store.save_batch_record(record)
        run_state.status = RunStatus.RUNNING
        run_state.awaiting_manual_edit = False
        run_state.manual_edit_batch_id = session.batch_id
        run_state.manual_edit_workspace = session.workspace_dir
        run_state.manual_edit_applied = True
        run_state.resume_from_manual_edit = True
        run_state.recommended_action = "continue_after_manual_edit"
        run_state.last_recovery_action = "apply_manual_edit_session"
        run_state.last_recovery_batch_id = session.batch_id
        state_store.save_run_state(run_state)
        state_store.append_checkpoint(
            "manual_edit_applied",
            {
                "batch_id": session.batch_id,
                "workspace_dir": session.workspace_dir,
            },
        )
        return session

    def open_manual_edit_workspace(self, book_id: str) -> ManualEditSession:
        run_dir = self.runs_dir / book_id
        state_store = StateStore(run_dir)
        session = state_store.load_manual_edit_session()
        if session is None:
            raise ValueError("当前不存在人工修订会话")
        actors_path = run_dir / session.editable_actors_path
        launch_result = self.editor_launcher.open(actors_path)
        session = session.model_copy(
            update={
                "editor_command": launch_result.command,
                "editor_exit_code": launch_result.exit_code,
                "last_error": launch_result.error,
            }
        )
        state_store.save_manual_edit_session(session)
        return session

    def continue_after_manual_edit(self, book_id: str, *, delta_yaml_text: str | None = None) -> BatchRecord:
        run_dir = self.runs_dir / book_id
        state_store = StateStore(run_dir)
        run_state = state_store.load_run_state()
        session = state_store.load_manual_edit_session()
        if session is None:
            raise ValueError("当前不存在人工修订会话")
        if run_state.awaiting_manual_edit or not run_state.manual_edit_applied:
            self.apply_manual_edit_session(book_id)
            run_state = state_store.load_run_state()

        record = state_store.load_batch_record(session.batch_id)
        retry_count = record.retry_count if record is not None else 0
        if record is not None:
            record.status = BatchStatus.PENDING
            state_store.save_batch_record(record)

        run_state.status = RunStatus.RUNNING
        run_state.awaiting_manual_edit = False
        run_state.manual_edit_applied = False
        run_state.resume_from_manual_edit = False
        run_state.recommended_action = "continue_after_manual_edit"
        run_state.last_recovery_action = "continue_after_manual_edit"
        run_state.last_recovery_batch_id = session.batch_id
        state_store.save_run_state(run_state)
        state_store.append_checkpoint(
            "manual_edit_resuming_batch",
            {
                "batch_id": session.batch_id,
                "retry_count": retry_count,
            },
        )
        return self._invoke_batch_workflow(
            book_id,
            delta_yaml_text=delta_yaml_text,
            batch_id=session.batch_id,
            retry_count=retry_count,
            recovery_action="continue_after_manual_edit",
        )

    def resume_run(self, book_id: str) -> RecoveryDecision:
        run_dir = self.runs_dir / book_id
        state_store = StateStore(run_dir)
        run_state = state_store.load_run_state()
        if run_state.status == RunStatus.PAUSED:
            run_state.status = RunStatus.RUNNING
            run_state.control_action = None
            run_state.control_requested_at = None
            state_store.save_run_state(run_state)
        elif run_state.awaiting_manual_edit:
            try:
                self.apply_manual_edit_session(book_id)
            except ValueError:
                pass

        decision = self.get_recovery_decision(book_id)
        run_state = state_store.load_run_state()
        run_state.last_recovery_action = decision.action
        run_state.last_recovery_batch_id = decision.batch_id
        run_state.recommended_action = decision.action
        state_store.save_run_state(run_state)
        return decision

    def retry_last_failed(self, book_id: str, *, delta_yaml_text: str | None = None) -> BatchRecord:
        decision = self.get_recovery_decision(book_id)
        if decision.action != "retry_failed_batch" or decision.batch_id is None:
            raise ValueError(decision.reason or "当前没有可重试失败批次")
        return self.retry_batch(book_id, batch_id=decision.batch_id, delta_yaml_text=delta_yaml_text)

    def retry_batch(self, book_id: str, *, batch_id: str, delta_yaml_text: str | None = None) -> BatchRecord:
        run_dir = self.runs_dir / book_id
        state_store = StateStore(run_dir)
        review_store = ReviewQueueStore(run_dir)
        run_state = state_store.load_run_state()
        record = state_store.load_batch_record(batch_id)
        if record is None:
            raise ValueError(f"批次 {batch_id} 不存在")
        if record.status not in {BatchStatus.FAILED, BatchStatus.REJECTED}:
            raise ValueError(f"批次 {batch_id} 当前状态不支持重试: {record.status}")
        if record.last_failure is not None and not record.last_failure.retryable:
            raise ValueError(f"批次 {batch_id} 标记为不可重试")

        review_store.mark_retried(batch_id)
        run_state.status = RunStatus.RUNNING
        run_state.last_recovery_action = "retry_failed_batch"
        run_state.last_recovery_batch_id = batch_id
        run_state.recommended_action = "retry_failed_batch"
        state_store.save_run_state(run_state)
        return self._invoke_batch_workflow(
            book_id,
            delta_yaml_text=delta_yaml_text,
            batch_id=batch_id,
            retry_count=record.retry_count + 1,
            recovery_action="retry_failed_batch",
        )

    def commit_batch(
        self,
        book_id: str,
        *,
        batch_id: str,
        action: ReviewAction,
        reviewer: str | None = None,
        comment: str | None = None,
        edited_actors_text: str | None = None,
        edited_worldinfo_text: str | None = None,
    ) -> ReviewDecision:
        run_dir = self.runs_dir / book_id
        state_store = StateStore(run_dir)
        yaml_store = YamlDocumentStore(run_dir)
        batch_store = BatchArtifactStore(run_dir)
        review_store = ReviewQueueStore(run_dir)

        run_state = state_store.load_run_state()
        record = state_store.load_batch_record(batch_id)
        if record is None:
            raise ValueError(f"批次 {batch_id} 不存在")

        decision = ReviewDecision(
            batch_id=batch_id,
            decision=action.value,
            reviewer=reviewer,
            comment=comment,
            reviewed_at=datetime.utcnow(),
        )

        if action is ReviewAction.REJECT:
            record.status = BatchStatus.REJECTED
            record.review_decision = decision
            record.last_failure = FailureInfo(
                stage="review",
                message=comment or "审阅拒绝",
                errors=[comment] if comment else ["审阅拒绝"],
                retryable=True,
                suggested_action="retry_failed_batch",
            )
            record.validation_errors = record.last_failure.errors
            state_store.save_batch_record(record)
            state_store.save_review_decision(decision)
            review_store.mark_decision(decision)
            state_store.append_checkpoint("batch_rejected", {"batch_id": batch_id})
            run_state.status = RunStatus.RUNNING
            run_state.pending_review_batch_id = None
            run_state.last_failed_batch_id = batch_id
            run_state.last_failed_stage = "review"
            run_state.last_failure_reason = record.last_failure.message
            run_state.last_failure_retryable = True
            run_state.recommended_action = "retry_failed_batch"
            run_state.last_recovery_batch_id = batch_id
            self._clear_manual_edit_runtime(run_state, batch_id=batch_id)
            state_store.save_run_state(run_state)
            return decision

        actors_text = edited_actors_text or batch_store.read_text_artifact(batch_id, "merged_actors.preview.yaml")
        worldinfo_text = edited_worldinfo_text or batch_store.read_text_artifact(batch_id, "merged_worldinfo.preview.yaml")
        actors_content = parse_yaml_mapping_document(actors_text, root_key="actors")
        worldinfo_content = parse_yaml_mapping_document(worldinfo_text, root_key="worldinfo")

        actors_version = run_state.current_actors_version + 1
        worldinfo_version = run_state.current_worldinfo_version + 1
        actors_current_path = yaml_store.save_current_document("actors", actors_content)
        worldinfo_current_path = yaml_store.save_current_document("worldinfo", worldinfo_content)
        actors_history_path = yaml_store.save_history_document("actors", actors_version, actors_content)
        worldinfo_history_path = yaml_store.save_history_document("worldinfo", worldinfo_version, worldinfo_content)

        input_payload = state_store.load_batch_input(batch_id)
        chapter_start = int(input_payload["start_chapter_index"])
        chapter_end = int(input_payload["end_chapter_index"])

        state_store.save_document_version(
            DocumentVersion(
                doc_type="actors",
                version=actors_version,
                batch_id=batch_id,
                chapter_start=chapter_start,
                chapter_end=chapter_end,
                file_path=str(actors_history_path.relative_to(run_dir)),
                content_hash=sha256_text(actors_text),
                status=action.value,
                delta_path=str((run_dir / "batches" / batch_id / "delta.yaml").relative_to(run_dir)),
                approved_by=reviewer,
                approved_at=decision.reviewed_at,
            )
        )
        state_store.save_document_version(
            DocumentVersion(
                doc_type="worldinfo",
                version=worldinfo_version,
                batch_id=batch_id,
                chapter_start=chapter_start,
                chapter_end=chapter_end,
                file_path=str(worldinfo_history_path.relative_to(run_dir)),
                content_hash=sha256_text(worldinfo_text),
                status=action.value,
                delta_path=str((run_dir / "batches" / batch_id / "delta.yaml").relative_to(run_dir)),
                approved_by=reviewer,
                approved_at=decision.reviewed_at,
            )
        )

        state_store.save_review_decision(decision)
        review_store.mark_decision(decision)
        state_store.append_checkpoint(
            "batch_committed",
            {
                "batch_id": batch_id,
                "actors_current": str(actors_current_path.relative_to(run_dir)),
                "worldinfo_current": str(worldinfo_current_path.relative_to(run_dir)),
            },
        )

        record.status = BatchStatus.EDITED if action is ReviewAction.EDIT else BatchStatus.ACCEPTED
        record.review_decision = decision
        record.validation_errors = []
        record.last_failure = None
        state_store.save_batch_record(record)

        run_state.last_accepted_batch_id = batch_id
        run_state.current_actors_version = actors_version
        run_state.current_worldinfo_version = worldinfo_version
        run_state.next_chapter_index = chapter_end + 1
        run_state.status = RunStatus.COMPLETED if run_state.next_chapter_index >= run_state.total_chapters else RunStatus.RUNNING
        run_state.pending_review_batch_id = None
        run_state.last_failed_batch_id = None
        run_state.last_failed_stage = None
        run_state.last_failure_reason = None
        run_state.last_failure_retryable = None
        run_state.recommended_action = "completed" if run_state.status == RunStatus.COMPLETED else "continue_new_batch"
        self._clear_manual_edit_runtime(run_state, batch_id=batch_id)
        state_store.save_run_state(run_state)
        return decision

    def show_status(self, book_id: str) -> dict[str, Any]:
        run_dir = self.runs_dir / book_id
        state_store = StateStore(run_dir)
        run_state = state_store.load_run_state()
        recovery_decision = self.get_recovery_decision(book_id)
        session = state_store.load_manual_edit_session()
        return {
            "book_id": run_state.book_id,
            "status": run_state.status,
            "total_chapters": run_state.total_chapters,
            "next_chapter_index": run_state.next_chapter_index,
            "last_accepted_batch_id": run_state.last_accepted_batch_id,
            "last_generated_batch_id": run_state.last_generated_batch_id,
            "pending_review_batch_id": run_state.pending_review_batch_id,
            "last_failed_batch_id": run_state.last_failed_batch_id,
            "last_failed_stage": run_state.last_failed_stage,
            "last_failure_reason": run_state.last_failure_reason,
            "recommended_action": recovery_decision.action,
            "actors_version": run_state.current_actors_version,
            "worldinfo_version": run_state.current_worldinfo_version,
            "control_action": run_state.control_action,
            "awaiting_manual_edit": run_state.awaiting_manual_edit,
            "manual_edit_batch_id": run_state.manual_edit_batch_id,
            "manual_edit_workspace": run_state.manual_edit_workspace,
            "manual_edit_applied": run_state.manual_edit_applied,
            "manual_edit_session": session.model_dump(mode="json") if session else None,
        }

    def get_recovery_decision(self, book_id: str) -> RecoveryDecision:
        run_dir = self.runs_dir / book_id
        state_store = StateStore(run_dir)
        run_state = state_store.load_run_state()
        if run_state.manual_edit_applied and run_state.manual_edit_batch_id:
            return RecoveryDecision(
                action="continue_after_manual_edit",
                batch_id=run_state.manual_edit_batch_id,
                reason="人工修订已应用，等待重跑同一批次",
                run_status=run_state.status,
                next_chapter_index=run_state.next_chapter_index,
                total_chapters=run_state.total_chapters,
                manual_edit_workspace=run_state.manual_edit_workspace,
            )

        if run_state.awaiting_manual_edit and run_state.manual_edit_batch_id:
            manual_record = state_store.find_manual_edit_batch(run_state)
            return RecoveryDecision(
                action="await_manual_edit",
                batch_id=run_state.manual_edit_batch_id,
                reason="当前正在等待人工修订完成",
                run_status=run_state.status,
                next_chapter_index=run_state.next_chapter_index,
                total_chapters=run_state.total_chapters,
                batch_status=manual_record.status if manual_record else None,
                manual_edit_workspace=run_state.manual_edit_workspace,
            )

        if run_state.status == RunStatus.PAUSED:
            return RecoveryDecision(
                action="paused",
                reason="当前运行已暂停",
                run_status=run_state.status,
                next_chapter_index=run_state.next_chapter_index,
                total_chapters=run_state.total_chapters,
            )

        pending_record = state_store.find_pending_review_batch(run_state)
        if pending_record is not None:
            return RecoveryDecision(
                action="resume_pending_review",
                batch_id=pending_record.batch.batch_id,
                reason="存在待审阅批次",
                run_status=run_state.status,
                next_chapter_index=run_state.next_chapter_index,
                total_chapters=run_state.total_chapters,
                batch_status=pending_record.status,
            )

        failed_record = state_store.find_retryable_failed_batch(run_state)
        if failed_record is not None:
            return RecoveryDecision(
                action="retry_failed_batch",
                batch_id=failed_record.batch.batch_id,
                reason=failed_record.last_failure.message if failed_record.last_failure else "存在可重试失败批次",
                retryable=True,
                target_stage=failed_record.last_failure.stage if failed_record.last_failure else None,
                run_status=run_state.status,
                next_chapter_index=run_state.next_chapter_index,
                total_chapters=run_state.total_chapters,
                batch_status=failed_record.status,
            )

        if run_state.next_chapter_index < run_state.total_chapters:
            return RecoveryDecision(
                action="continue_new_batch",
                batch_id=self._predict_next_batch_id(run_state),
                reason="无待审阅和失败批次，继续新批次",
                run_status=run_state.status,
                next_chapter_index=run_state.next_chapter_index,
                total_chapters=run_state.total_chapters,
            )

        return RecoveryDecision(
            action="completed",
            reason="章节已全部提交",
            run_status=run_state.status,
            next_chapter_index=run_state.next_chapter_index,
            total_chapters=run_state.total_chapters,
        )

    def _invoke_batch_workflow(
        self,
        book_id: str,
        *,
        delta_yaml_text: str | None = None,
        batch_id: str | None = None,
        retry_count: int = 0,
        recovery_action: str | None = None,
    ) -> BatchRecord:
        run_dir = self.runs_dir / book_id
        state_store = StateStore(run_dir)
        run_state = state_store.load_run_state()
        run_state.status = RunStatus.RUNNING
        if recovery_action is not None:
            run_state.last_recovery_action = recovery_action
            run_state.last_recovery_batch_id = batch_id
            run_state.recommended_action = recovery_action
        state_store.save_run_state(run_state)
        pipeline_state = run_batch_generation_workflow(
            run_dir=run_dir,
            book_id=book_id,
            document_update_chain=self.document_update_chain,
            llm_raw_output=delta_yaml_text,
            batch_id=batch_id,
            retry_count=retry_count,
        )
        if pipeline_state.batch is None:
            raise ValueError(pipeline_state.error_message or "工作流未生成批次")
        if pipeline_state.batch_record_status == BatchStatus.FAILED:
            raise ValueError(pipeline_state.error_message or "批次处理失败")

        record = BatchRecord(
            batch=pipeline_state.batch,
            status=pipeline_state.batch_record_status or "unknown",
            validation_errors=pipeline_state.validation_errors,
            retry_count=pipeline_state.retry_count,
        )
        if record.status == BatchStatus.FAILED:
            raise ValueError(pipeline_state.error_message or "批次处理失败")
        return record

    def _handle_workflow_control_interrupt(self, book_id: str, interrupt: WorkflowControlInterrupt) -> str:
        run_dir = self.runs_dir / book_id
        state_store = StateStore(run_dir)
        run_state = state_store.load_run_state()
        batch_id = interrupt.batch_id or run_state.last_generated_batch_id
        run_state.last_generated_batch_id = batch_id
        run_state.last_recovery_batch_id = batch_id
        if interrupt.action == ControlAction.PAUSE.value:
            run_state.status = RunStatus.PAUSED
            run_state.recommended_action = ControlAction.RESUME.value
            run_state.control_action = None
            run_state.control_requested_at = None
            state_store.save_run_state(run_state)
            state_store.append_checkpoint(
                "run_paused",
                {
                    "batch_id": batch_id,
                },
            )
            return "break"

        if interrupt.action == ControlAction.PREPARE_MANUAL_EDIT.value:
            state_store.save_run_state(run_state)
            session = self.prepare_manual_edit(book_id, batch_id=batch_id, open_editor=True)
            return "continue" if session.status == ManualEditSessionStatus.APPLIED else "break"

        state_store.save_run_state(run_state)
        return "break"

    def _resolve_manual_edit_target(self, book_id: str, *, batch_id: str | None) -> tuple[BatchRecord | None, Any]:
        run_dir = self.runs_dir / book_id
        state_store = StateStore(run_dir)
        run_state = state_store.load_run_state()
        candidate_ids = [batch_id, run_state.manual_edit_batch_id, run_state.last_generated_batch_id, run_state.pending_review_batch_id]
        for candidate_id in candidate_ids:
            if not candidate_id:
                continue
            try:
                batch = state_store.load_batch_input_model(candidate_id)
            except FileNotFoundError:
                continue
            return state_store.load_batch_record(candidate_id), batch

        batch = self._build_next_batch_for_manual_edit(run_state, state_store)
        state_store.save_batch_input(batch)
        run_state.last_generated_batch_id = batch.batch_id
        run_state.last_recovery_batch_id = batch.batch_id
        state_store.save_run_state(run_state)
        return state_store.load_batch_record(batch.batch_id), batch

    def _build_next_batch_for_manual_edit(self, run_state: RunState, state_store: StateStore):
        chapters = state_store.load_chapters()
        remaining = chapters[run_state.next_chapter_index :]
        if not remaining:
            raise ValueError("没有可供人工修订的剩余批次")
        next_batch_number = 1
        if run_state.last_accepted_batch_id is not None:
            next_batch_number = int(run_state.last_accepted_batch_id) + 1
        return build_batches(
            remaining,
            target_input_tokens=run_state.target_input_tokens,
            max_input_tokens=run_state.max_input_tokens,
            min_chapters_per_batch=run_state.min_chapters_per_batch,
            max_chapters_per_batch=run_state.max_chapters_per_batch,
            batch_number_start=next_batch_number,
        )[0]

    @staticmethod
    def _load_current_document_text(path: Path, root_key: str) -> str:
        if path.exists():
            return path.read_text(encoding="utf-8")
        return dump_yaml_document(root_key, {})

    @staticmethod
    def _batch_preview_exists(run_dir: Path, batch_id: str) -> bool:
        batch_dir = run_dir / "batches" / batch_id
        return (batch_dir / "merged_actors.preview.yaml").exists() or (batch_dir / "merged_worldinfo.preview.yaml").exists()

    @staticmethod
    def _build_manual_edit_note(book_id: str, batch_id: str, chapter_start: int, chapter_end: int) -> str:
        return (
            f"book_id: {book_id}\n"
            f"batch_id: {batch_id}\n"
            f"chapter_range: {chapter_start}-{chapter_end}\n\n"
            "请修改同目录下的 actors.editable.yaml 与 worldinfo.editable.yaml。\n"
            "修改完成后关闭编辑器，再执行 resume 或继续自动流程。\n"
        )

    @staticmethod
    def _clear_manual_edit_runtime(run_state: RunState, *, batch_id: str) -> None:
        if run_state.manual_edit_batch_id != batch_id:
            return
        run_state.awaiting_manual_edit = False
        run_state.manual_edit_batch_id = None
        run_state.manual_edit_workspace = None
        run_state.manual_edit_applied = False
        run_state.resume_from_manual_edit = False
        run_state.control_action = None
        run_state.control_requested_at = None

    @staticmethod
    def _predict_next_batch_id(run_state: RunState) -> str:
        next_batch_number = 1
        if run_state.last_accepted_batch_id is not None:
            next_batch_number = int(run_state.last_accepted_batch_id) + 1
        return f"{next_batch_number:04d}"
