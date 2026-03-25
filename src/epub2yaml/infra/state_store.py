from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from epub2yaml.domain.enums import BatchStatus
from epub2yaml.domain.models import BatchRecord, Chapter, ChapterBatch, DocumentVersion, ReviewDecision, RunState


class StateStore:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.state_dir = run_dir / "state"
        self.extracted_dir = run_dir / "extracted"
        self.batches_dir = run_dir / "batches"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.extracted_dir.mkdir(parents=True, exist_ok=True)
        self.batches_dir.mkdir(parents=True, exist_ok=True)

    def save_run_state(self, run_state: RunState) -> Path:
        run_state.updated_at = datetime.utcnow()
        path = self.state_dir / "run_state.json"
        path.write_text(run_state.model_dump_json(indent=2), encoding="utf-8")
        return path

    def load_run_state(self) -> RunState:
        path = self.state_dir / "run_state.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        return RunState.model_validate(payload)

    def save_chapters(self, chapters: list[Chapter]) -> Path:
        path = self.extracted_dir / "chapters.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for chapter in chapters:
                handle.write(chapter.model_dump_json())
                handle.write("\n")
        return path

    def load_chapters(self) -> list[Chapter]:
        path = self.extracted_dir / "chapters.jsonl"
        chapters: list[Chapter] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                chapters.append(Chapter.model_validate(json.loads(line)))
        return chapters

    def save_batch_input(self, batch: ChapterBatch) -> Path:
        batch_dir = self._batch_dir(batch.batch_id)
        path = batch_dir / "input.json"
        path.write_text(batch.model_dump_json(indent=2), encoding="utf-8")
        return path

    def load_batch_input(self, batch_id: str) -> dict[str, Any]:
        path = self._batch_dir(batch_id) / "input.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def load_batch_input_model(self, batch_id: str) -> ChapterBatch:
        return ChapterBatch.model_validate(self.load_batch_input(batch_id))

    def save_batch_record(self, record: BatchRecord) -> Path:
        batch_dir = self._batch_dir(record.batch.batch_id)
        record.updated_at = datetime.utcnow()
        path = batch_dir / "record.json"
        path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        return path

    def load_batch_record(self, batch_id: str) -> BatchRecord | None:
        path = self._batch_dir(batch_id) / "record.json"
        if not path.exists():
            return None
        return BatchRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def list_batch_records(self) -> list[BatchRecord]:
        records: list[BatchRecord] = []
        if not self.batches_dir.exists():
            return records

        for batch_dir in sorted(self.batches_dir.iterdir()):
            if not batch_dir.is_dir():
                continue
            record = self.load_batch_record(batch_dir.name)
            if record is not None:
                records.append(record)
        return records

    def list_failed_batches(self) -> list[BatchRecord]:
        return [
            record
            for record in self.list_batch_records()
            if record.status in {BatchStatus.FAILED, BatchStatus.REJECTED}
        ]

    def find_pending_review_batch(self, run_state: RunState | None = None) -> BatchRecord | None:
        active_state = run_state or self.load_run_state()
        candidate_ids: list[str] = []
        if active_state.pending_review_batch_id:
            candidate_ids.append(active_state.pending_review_batch_id)
        if active_state.last_generated_batch_id and active_state.last_generated_batch_id not in candidate_ids:
            candidate_ids.append(active_state.last_generated_batch_id)

        for batch_id in candidate_ids:
            record = self.load_batch_record(batch_id)
            if record is not None and record.status == BatchStatus.REVIEW_REQUIRED:
                return record

        for record in reversed(self.list_batch_records()):
            if record.status == BatchStatus.REVIEW_REQUIRED:
                return record
        return None

    def find_retryable_failed_batch(self, run_state: RunState | None = None) -> BatchRecord | None:
        active_state = run_state or self.load_run_state()
        if active_state.last_failed_batch_id:
            record = self.load_batch_record(active_state.last_failed_batch_id)
            if record is not None and record.status in {BatchStatus.FAILED, BatchStatus.REJECTED}:
                if record.last_failure is None or record.last_failure.retryable:
                    return record

        for record in reversed(self.list_failed_batches()):
            if record.last_failure is None or record.last_failure.retryable:
                return record
        return None

    def save_review_decision(self, decision: ReviewDecision) -> Path:
        batch_dir = self._batch_dir(decision.batch_id)
        path = batch_dir / "review.json"
        path.write_text(decision.model_dump_json(indent=2), encoding="utf-8")
        return path

    def save_document_version(self, version: DocumentVersion) -> Path:
        path = self.state_dir / "document_versions.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(version.model_dump_json())
            handle.write("\n")
        return path

    def load_latest_checkpoint(self) -> dict[str, Any] | None:
        checkpoints = self.list_checkpoints()
        return checkpoints[-1] if checkpoints else None

    def list_checkpoints(self, event: str | None = None) -> list[dict[str, Any]]:
        path = self.state_dir / "checkpoints.jsonl"
        if not path.exists():
            return []

        records = self._read_jsonl(path)
        if event is None:
            return records
        return [record for record in records if record.get("event") == event]

    def append_checkpoint(self, event: str, payload: dict[str, Any]) -> Path:
        path = self.state_dir / "checkpoints.jsonl"
        record = {
            "event": event,
            "timestamp": datetime.utcnow().isoformat(),
            "payload": payload,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False))
            handle.write("\n")
        return path

    def _batch_dir(self, batch_id: str) -> Path:
        batch_dir = self.batches_dir / batch_id
        batch_dir.mkdir(parents=True, exist_ok=True)
        return batch_dir

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                records.append(json.loads(line))
        return records
