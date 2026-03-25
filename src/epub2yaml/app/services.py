from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from epub2yaml.domain.enums import BatchStatus, ReviewAction, RunStatus
from epub2yaml.domain.models import BatchRecord, DocumentVersion, FailureInfo, RecoveryDecision, ReviewDecision, RunState
from epub2yaml.domain.services import detect_structure_loss, parse_yaml_mapping_document
from epub2yaml.infra.batch_store import BatchArtifactStore
from epub2yaml.infra.review_store import ReviewQueueStore
from epub2yaml.infra.state_store import StateStore
from epub2yaml.infra.yaml_store import YamlDocumentStore
from epub2yaml.llm.chains.document_update_chain import DocumentUpdateChain
from epub2yaml.utils.hashing import sha256_bytes, sha256_text
from epub2yaml.workflow.graph import run_batch_generation_workflow
from utils.epub_extract import extract_epub


class PipelineService:
    def __init__(self, workspace_dir: Path, *, document_update_chain: DocumentUpdateChain | None = None) -> None:
        self.workspace_dir = workspace_dir
        self.runs_dir = workspace_dir / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.document_update_chain = document_update_chain

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

    def process_next_batch(self, book_id: str, *, delta_yaml_text: str | None = None) -> BatchRecord:
        run_dir = self.runs_dir / book_id
        state_store = StateStore(run_dir)
        run_state = state_store.load_run_state()
        pending_review = state_store.find_pending_review_batch(run_state)
        if pending_review is not None:
            raise ValueError(f"存在待审阅批次 {pending_review.batch.batch_id}，请先 resume 或 review")
        if run_state.next_chapter_index >= run_state.total_chapters:
            run_state.status = RunStatus.COMPLETED
            run_state.recommended_action = "completed"
            state_store.save_run_state(run_state)
            raise ValueError("所有章节都已处理完成")

        pipeline_state = run_batch_generation_workflow(
            run_dir=run_dir,
            book_id=book_id,
            document_update_chain=self.document_update_chain,
            llm_raw_output=delta_yaml_text,
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
            structure_check_passed=pipeline_state.structure_check_passed,
            missing_paths=pipeline_state.missing_paths,
            actors_missing_paths=pipeline_state.actors_missing_paths,
            worldinfo_missing_paths=pipeline_state.worldinfo_missing_paths,
            requires_loss_approval=pipeline_state.requires_loss_approval,
            loss_approval_status="pending" if pipeline_state.requires_loss_approval else None,
        )
        if record.status == BatchStatus.FAILED:
            raise ValueError(pipeline_state.error_message or "批次处理失败")
        return record

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
            decision = self.get_recovery_decision(book_id)
            if decision.action == "completed":
                final_state = state_store.load_run_state()
                final_state.status = RunStatus.COMPLETED
                final_state.recommended_action = "completed"
                state_store.save_run_state(final_state)
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

            if decision.action == "review_structure_loss":
                raise ValueError(f"批次 {target_batch_id} 检测到结构缺失，必须先人工审阅")
            if decision.action == "resume_pending_review":
                self.commit_batch(book_id, batch_id=target_batch_id, action=ReviewAction.ACCEPT, reviewer="system-auto")
                processed_batches.append(target_batch_id)
            elif decision.action == "retry_failed_batch":
                record = self.retry_batch(
                    book_id,
                    batch_id=target_batch_id,
                    delta_yaml_text=(delta_yaml_by_batch or {}).get(target_batch_id),
                )
                if record.requires_loss_approval:
                    raise ValueError(f"批次 {record.batch.batch_id} 检测到结构缺失，必须先人工审阅")
                self.commit_batch(book_id, batch_id=record.batch.batch_id, action=ReviewAction.ACCEPT, reviewer="system-auto")
                processed_batches.append(record.batch.batch_id)
            elif decision.action == "continue_new_batch":
                record = self.process_next_batch(
                    book_id,
                    delta_yaml_text=(delta_yaml_by_batch or {}).get(target_batch_id),
                )
                if record.requires_loss_approval:
                    raise ValueError(f"批次 {record.batch.batch_id} 检测到结构缺失，必须先人工审阅")
                self.commit_batch(book_id, batch_id=record.batch.batch_id, action=ReviewAction.ACCEPT, reviewer="system-auto")
                processed_batches.append(record.batch.batch_id)
            else:
                raise ValueError(decision.reason or f"不支持的恢复动作: {decision.action}")

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

    def resume_run(self, book_id: str) -> RecoveryDecision:
        decision = self.get_recovery_decision(book_id)
        run_dir = self.runs_dir / book_id
        state_store = StateStore(run_dir)
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
        pipeline_state = run_batch_generation_workflow(
            run_dir=run_dir,
            book_id=book_id,
            document_update_chain=self.document_update_chain,
            llm_raw_output=delta_yaml_text,
            batch_id=batch_id,
            retry_count=record.retry_count + 1,
        )
        if pipeline_state.batch is None:
            raise ValueError(pipeline_state.error_message or "工作流未生成批次")
        if pipeline_state.batch_record_status == BatchStatus.FAILED:
            raise ValueError(pipeline_state.error_message or "批次处理失败")

        return BatchRecord(
            batch=pipeline_state.batch,
            status=pipeline_state.batch_record_status or "unknown",
            validation_errors=pipeline_state.validation_errors,
            retry_count=pipeline_state.retry_count,
            structure_check_passed=pipeline_state.structure_check_passed,
            missing_paths=pipeline_state.missing_paths,
            actors_missing_paths=pipeline_state.actors_missing_paths,
            worldinfo_missing_paths=pipeline_state.worldinfo_missing_paths,
            requires_loss_approval=pipeline_state.requires_loss_approval,
            loss_approval_status="pending" if pipeline_state.requires_loss_approval else None,
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
            if record.requires_loss_approval:
                record.loss_approval_status = "rejected"
                record.loss_approval_comment = comment
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
            run_state.pending_loss_review_batch_id = None
            run_state.last_failed_batch_id = batch_id
            run_state.last_failed_stage = "review"
            run_state.last_failure_reason = record.last_failure.message
            run_state.last_failure_retryable = True
            run_state.recommended_action = "retry_failed_batch"
            run_state.last_recovery_batch_id = batch_id
            state_store.save_run_state(run_state)
            return decision

        actors_text = edited_actors_text or batch_store.read_text_artifact(batch_id, "merged_actors.preview.yaml")
        worldinfo_text = edited_worldinfo_text or batch_store.read_text_artifact(batch_id, "merged_worldinfo.preview.yaml")
        actors_content = parse_yaml_mapping_document(actors_text, root_key="actors")
        worldinfo_content = parse_yaml_mapping_document(worldinfo_text, root_key="worldinfo")

        previous_actors_text = (run_dir / "current" / "actors.yaml").read_text(encoding="utf-8")
        previous_worldinfo_text = (run_dir / "current" / "worldinfo.yaml").read_text(encoding="utf-8")
        structure_result = detect_structure_loss(
            previous_actors_document=previous_actors_text,
            current_actors_document=actors_text,
            previous_worldinfo_document=previous_worldinfo_text,
            current_worldinfo_document=worldinfo_text,
        )
        current_missing_paths = list(structure_result["missing_paths"])
        current_actors_missing_paths = list(structure_result["actors_missing_paths"])
        current_worldinfo_missing_paths = list(structure_result["worldinfo_missing_paths"])
        current_requires_loss_approval = bool(structure_result["requires_loss_approval"])
        if current_requires_loss_approval and action is not ReviewAction.ACCEPT:
            raise ValueError("存在结构缺失时，仅允许 accept 继续提交或 reject 进入重试")
        if current_requires_loss_approval and action is ReviewAction.ACCEPT and not reviewer:
            reviewer = "manual-review"

        record.structure_check_passed = bool(structure_result["structure_check_passed"])
        record.missing_paths = current_missing_paths
        record.actors_missing_paths = current_actors_missing_paths
        record.worldinfo_missing_paths = current_worldinfo_missing_paths
        record.requires_loss_approval = current_requires_loss_approval
        if current_requires_loss_approval:
            record.loss_approval_status = "approved_after_edit" if edited_actors_text or edited_worldinfo_text else "approved"
            record.loss_approval_comment = comment
        else:
            record.loss_approval_status = None
            record.loss_approval_comment = None

        batch_store.write_text_artifact(batch_id, "merged_actors.preview.yaml", actors_text)
        batch_store.write_text_artifact(batch_id, "merged_worldinfo.preview.yaml", worldinfo_text)
        batch_store.write_text_artifact(
            batch_id,
            "structure_check.json",
            json.dumps(
                {
                    "checked_at": datetime.utcnow().isoformat(),
                    "batch_id": batch_id,
                    "baseline": {
                        "actors": "current/actors.yaml",
                        "worldinfo": "current/worldinfo.yaml",
                    },
                    "preview": {
                        "actors": f"batches/{batch_id}/merged_actors.preview.yaml",
                        "worldinfo": f"batches/{batch_id}/merged_worldinfo.preview.yaml",
                    },
                    "structure_check_passed": record.structure_check_passed,
                    "requires_loss_approval": record.requires_loss_approval,
                    "missing_paths_count": len(record.missing_paths),
                    "actors_missing_paths": record.actors_missing_paths,
                    "worldinfo_missing_paths": record.worldinfo_missing_paths,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        batch_store.write_text_artifact(
            batch_id,
            "missing_paths.txt",
            "\n".join(record.missing_paths) + ("\n" if record.missing_paths else ""),
        )

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
                "structure_check_passed": record.structure_check_passed,
                "requires_loss_approval": record.requires_loss_approval,
                "missing_paths_count": len(record.missing_paths),
                "loss_approval_status": record.loss_approval_status,
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
        run_state.pending_loss_review_batch_id = None
        run_state.last_failed_batch_id = None
        run_state.last_failed_stage = None
        run_state.last_failure_reason = None
        run_state.last_failure_retryable = None
        run_state.last_structure_check_batch_id = batch_id
        run_state.last_structure_check_passed = record.structure_check_passed
        run_state.recommended_action = "completed" if run_state.status == RunStatus.COMPLETED else "continue_new_batch"
        state_store.save_run_state(run_state)
        return decision

    def show_status(self, book_id: str) -> dict[str, Any]:
        run_dir = self.runs_dir / book_id
        state_store = StateStore(run_dir)
        run_state = state_store.load_run_state()
        recovery_decision = self.get_recovery_decision(book_id)
        return {
            "book_id": run_state.book_id,
            "status": run_state.status,
            "total_chapters": run_state.total_chapters,
            "next_chapter_index": run_state.next_chapter_index,
            "last_accepted_batch_id": run_state.last_accepted_batch_id,
            "last_generated_batch_id": run_state.last_generated_batch_id,
            "pending_review_batch_id": run_state.pending_review_batch_id,
            "pending_loss_review_batch_id": run_state.pending_loss_review_batch_id,
            "last_failed_batch_id": run_state.last_failed_batch_id,
            "last_failed_stage": run_state.last_failed_stage,
            "last_failure_reason": run_state.last_failure_reason,
            "last_structure_check_batch_id": run_state.last_structure_check_batch_id,
            "last_structure_check_passed": run_state.last_structure_check_passed,
            "recommended_action": recovery_decision.action,
            "actors_version": run_state.current_actors_version,
            "worldinfo_version": run_state.current_worldinfo_version,
        }

    def get_recovery_decision(self, book_id: str) -> RecoveryDecision:
        run_dir = self.runs_dir / book_id
        state_store = StateStore(run_dir)
        review_store = ReviewQueueStore(run_dir)
        run_state = state_store.load_run_state()
        pending_record = state_store.find_pending_review_batch(run_state)
        if pending_record is not None:
            review_entry = review_store.get_entry(pending_record.batch.batch_id) or {}
            review_kind = review_entry.get("review_kind", "normal_review")
            action = "review_structure_loss" if pending_record.requires_loss_approval else "resume_pending_review"
            reason = "存在结构缺失待审批批次" if pending_record.requires_loss_approval else "存在待审阅批次"
            return RecoveryDecision(
                action=action,
                batch_id=pending_record.batch.batch_id,
                reason=reason,
                run_status=run_state.status,
                next_chapter_index=run_state.next_chapter_index,
                total_chapters=run_state.total_chapters,
                batch_status=pending_record.status,
                review_kind=review_kind,
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
                review_kind="structure_loss_review" if failed_record.requires_loss_approval else "normal_review",
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

    def get_review_batch_summary(self, book_id: str, *, batch_id: str) -> dict[str, Any]:
        run_dir = self.runs_dir / book_id
        state_store = StateStore(run_dir)
        review_store = ReviewQueueStore(run_dir)
        record = state_store.load_batch_record(batch_id)
        if record is None:
            raise ValueError(f"批次 {batch_id} 不存在")
        review_entry = review_store.get_entry(batch_id) or {}
        return {
            "batch_id": batch_id,
            "chapter_start": record.batch.start_chapter_index,
            "chapter_end": record.batch.end_chapter_index,
            "status": record.status,
            "retry_count": record.retry_count,
            "structure_check_passed": record.structure_check_passed,
            "requires_loss_approval": record.requires_loss_approval,
            "loss_approval_status": record.loss_approval_status,
            "missing_paths_count": len(record.missing_paths),
            "actors_missing_count": len(record.actors_missing_paths),
            "worldinfo_missing_count": len(record.worldinfo_missing_paths),
            "actors_missing_paths": record.actors_missing_paths,
            "worldinfo_missing_paths": record.worldinfo_missing_paths,
            "missing_paths": record.missing_paths,
            "review_kind": review_entry.get("review_kind", "normal_review"),
            "files": {
                "delta": f"runs/{book_id}/batches/{batch_id}/delta.yaml",
                "merged_actors_preview": f"runs/{book_id}/batches/{batch_id}/merged_actors.preview.yaml",
                "merged_worldinfo_preview": f"runs/{book_id}/batches/{batch_id}/merged_worldinfo.preview.yaml",
                "structure_check": f"runs/{book_id}/batches/{batch_id}/structure_check.json",
                "missing_paths": f"runs/{book_id}/batches/{batch_id}/missing_paths.txt",
            },
        }

    @staticmethod
    def _predict_next_batch_id(run_state: RunState) -> str:
        next_batch_number = 1
        if run_state.last_accepted_batch_id is not None:
            next_batch_number = int(run_state.last_accepted_batch_id) + 1
        return f"{next_batch_number:04d}"
