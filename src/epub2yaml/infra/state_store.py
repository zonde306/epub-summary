from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

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

    def save_batch_record(self, record: BatchRecord) -> Path:
        batch_dir = self._batch_dir(record.batch.batch_id)
        path = batch_dir / "record.json"
        path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        return path

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
