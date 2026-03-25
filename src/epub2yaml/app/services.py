from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from epub2yaml.domain.enums import BatchStatus, ReviewAction, RunStatus
from epub2yaml.domain.models import BatchRecord, DocumentVersion, ReviewDecision, RunState
from epub2yaml.domain.services import parse_yaml_mapping_document
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
        if run_state.next_chapter_index >= run_state.total_chapters:
            run_state.status = RunStatus.COMPLETED
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
        )
        if record.status == BatchStatus.FAILED:
            raise ValueError(pipeline_state.error_message or "批次处理失败")
        return record

    def run_to_completion(self, book_id: str, *, delta_yaml_by_batch: dict[str, str] | None = None) -> dict[str, Any]:
        run_dir = self.runs_dir / book_id
        state_store = StateStore(run_dir)
        processed_batches: list[str] = []

        while True:
            run_state = state_store.load_run_state()
            if run_state.next_chapter_index >= run_state.total_chapters:
                run_state.status = RunStatus.COMPLETED
                state_store.save_run_state(run_state)
                break

            next_batch_id = self._predict_next_batch_id(run_state)
            record = self.process_next_batch(
                book_id,
                delta_yaml_text=(delta_yaml_by_batch or {}).get(next_batch_id),
            )
            self.commit_batch(book_id, batch_id=record.batch.batch_id, action=ReviewAction.ACCEPT, reviewer="system-auto")
            processed_batches.append(record.batch.batch_id)

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
    ) -> dict[str, Any]:
        run_state = self.init_run(epub_path, book_id=book_id)
        result = self.run_to_completion(run_state.book_id, delta_yaml_by_batch=delta_yaml_by_batch)
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
        decision = ReviewDecision(
            batch_id=batch_id,
            decision=action.value,
            reviewer=reviewer,
            comment=comment,
            reviewed_at=datetime.utcnow(),
        )

        if action is ReviewAction.REJECT:
            state_store.save_review_decision(decision)
            review_store.save_decision(decision)
            state_store.append_checkpoint("batch_rejected", {"batch_id": batch_id})
            run_state.status = RunStatus.RUNNING
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
        review_store.save_decision(decision)
        state_store.append_checkpoint(
            "batch_committed",
            {
                "batch_id": batch_id,
                "actors_current": str(actors_current_path.relative_to(run_dir)),
                "worldinfo_current": str(worldinfo_current_path.relative_to(run_dir)),
            },
        )

        run_state.last_accepted_batch_id = batch_id
        run_state.current_actors_version = actors_version
        run_state.current_worldinfo_version = worldinfo_version
        run_state.next_chapter_index = chapter_end + 1
        run_state.status = RunStatus.COMPLETED if run_state.next_chapter_index >= run_state.total_chapters else RunStatus.RUNNING
        state_store.save_run_state(run_state)
        return decision

    def show_status(self, book_id: str) -> dict[str, Any]:
        run_dir = self.runs_dir / book_id
        run_state = StateStore(run_dir).load_run_state()
        return {
            "book_id": run_state.book_id,
            "status": run_state.status,
            "total_chapters": run_state.total_chapters,
            "next_chapter_index": run_state.next_chapter_index,
            "last_accepted_batch_id": run_state.last_accepted_batch_id,
            "actors_version": run_state.current_actors_version,
            "worldinfo_version": run_state.current_worldinfo_version,
        }

    @staticmethod
    def _predict_next_batch_id(run_state: RunState) -> str:
        next_batch_number = 1
        if run_state.last_accepted_batch_id is not None:
            next_batch_number = int(run_state.last_accepted_batch_id) + 1
        return f"{next_batch_number:04d}"
