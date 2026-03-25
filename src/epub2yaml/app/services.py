from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from epub2yaml.domain.enums import BatchStatus, ReviewAction, RunStatus
from epub2yaml.domain.models import BatchRecord, DocumentVersion, ReviewDecision, RunState
from epub2yaml.domain.services import build_batches, dump_yaml_document, merge_delta_package, parse_delta_yaml
from epub2yaml.infra.batch_store import BatchArtifactStore
from epub2yaml.infra.review_store import ReviewQueueStore
from epub2yaml.infra.state_store import StateStore
from epub2yaml.infra.yaml_store import YamlDocumentStore
from epub2yaml.utils.hashing import sha256_bytes, sha256_text
from utils.epub_extract import extract_epub


class PipelineService:
    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = workspace_dir
        self.runs_dir = workspace_dir / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

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

    def process_next_batch(self, book_id: str, *, delta_yaml_text: str) -> BatchRecord:
        run_dir = self.runs_dir / book_id
        state_store = StateStore(run_dir)
        yaml_store = YamlDocumentStore(run_dir)
        batch_store = BatchArtifactStore(run_dir)
        review_store = ReviewQueueStore(run_dir)

        run_state = state_store.load_run_state()
        chapters = state_store.load_chapters()
        remaining = chapters[run_state.next_chapter_index :]
        if not remaining:
            run_state.status = RunStatus.COMPLETED
            state_store.save_run_state(run_state)
            raise ValueError("所有章节都已处理完成")

        next_batch_number = 1
        if run_state.last_accepted_batch_id is not None:
            next_batch_number = int(run_state.last_accepted_batch_id) + 1

        batches = build_batches(
            remaining,
            target_input_tokens=run_state.target_input_tokens,
            max_input_tokens=run_state.max_input_tokens,
            min_chapters_per_batch=run_state.min_chapters_per_batch,
            max_chapters_per_batch=run_state.max_chapters_per_batch,
            batch_number_start=next_batch_number,
        )
        batch = batches[0]

        actors_current = yaml_store.load_document("actors")
        worldinfo_current = yaml_store.load_document("worldinfo")
        delta_package = parse_delta_yaml(delta_yaml_text)
        merged_actors, merged_worldinfo = merge_delta_package(actors_current, worldinfo_current, delta_package)

        batch_store.write_text_artifact(batch.batch_id, "delta.yaml", delta_yaml_text)
        batch_store.write_text_artifact(batch.batch_id, "merged_actors.preview.yaml", dump_yaml_document("actors", merged_actors))
        batch_store.write_text_artifact(batch.batch_id, "merged_worldinfo.preview.yaml", dump_yaml_document("worldinfo", merged_worldinfo))
        state_store.save_batch_input(batch)

        record = BatchRecord(batch=batch, status=BatchStatus.REVIEW_REQUIRED)
        state_store.save_batch_record(record)
        review_store.enqueue(batch.batch_id)
        state_store.append_checkpoint(
            "batch_generated",
            {
                "batch_id": batch.batch_id,
                "chapter_start": batch.start_chapter_index,
                "chapter_end": batch.end_chapter_index,
            },
        )

        run_state.status = RunStatus.REVIEW_REQUIRED
        state_store.save_run_state(run_state)
        return record

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

        actors_payload = yaml.safe_load(actors_text) or {}
        worldinfo_payload = yaml.safe_load(worldinfo_text) or {}
        actors_content = actors_payload.get("actors", {})
        worldinfo_content = worldinfo_payload.get("worldinfo", {})

        if not isinstance(actors_content, dict) or not isinstance(worldinfo_content, dict):
            raise ValueError("审阅后的 YAML 根节点结构不合法")

        actors_version = run_state.current_actors_version + 1
        worldinfo_version = run_state.current_worldinfo_version + 1
        actors_current_path = yaml_store.save_current_document("actors", actors_content)
        worldinfo_current_path = yaml_store.save_current_document("worldinfo", worldinfo_content)
        actors_history_path = yaml_store.save_history_document("actors", actors_version, actors_content)
        worldinfo_history_path = yaml_store.save_history_document("worldinfo", worldinfo_version, worldinfo_content)

        input_payload = yaml.safe_load((run_dir / "batches" / batch_id / "input.json").read_text(encoding="utf-8"))
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
