from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from epub2yaml.domain.enums import ReviewAction
from epub2yaml.domain.models import ReviewDecision


class ReviewQueueStore:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.state_dir = run_dir / "state"
        self.queue_path = self.state_dir / "review_queue.json"
        self.history_path = self.state_dir / "review_history.jsonl"
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def enqueue(self, batch_id: str, *, review_kind: str = "normal_review") -> Path:
        queue = self._load_queue()
        queue[batch_id] = {
            "batch_id": batch_id,
            "status": "pending",
            "review_kind": review_kind,
        }
        self._save_queue(queue)
        self._append_history({"event": "enqueued", "batch_id": batch_id, "review_kind": review_kind})
        return self.queue_path

    def mark_decision(self, decision: ReviewDecision) -> Path:
        queue = self._load_queue()
        previous_entry = queue.get(decision.batch_id, {})
        status = "accepted"
        if decision.decision == ReviewAction.REJECT.value:
            status = "rejected"
        elif decision.decision == ReviewAction.EDIT.value:
            status = "edited"

        queue[decision.batch_id] = {
            "batch_id": decision.batch_id,
            "status": status,
            "review_kind": previous_entry.get("review_kind", "normal_review"),
            "reviewed_at": decision.reviewed_at.isoformat(),
        }
        self._save_queue(queue)
        self._append_history(
            {
                "event": "decision_recorded",
                "batch_id": decision.batch_id,
                "decision": decision.decision,
                "review_kind": previous_entry.get("review_kind", "normal_review"),
                "reviewed_at": decision.reviewed_at.isoformat(),
            }
        )
        return self.queue_path

    def mark_retried(self, batch_id: str) -> Path:
        queue = self._load_queue()
        previous_entry = queue.get(batch_id, {})
        queue[batch_id] = {
            "batch_id": batch_id,
            "status": "retried",
            "review_kind": previous_entry.get("review_kind", "normal_review"),
        }
        self._save_queue(queue)
        self._append_history(
            {
                "event": "retried",
                "batch_id": batch_id,
                "review_kind": previous_entry.get("review_kind", "normal_review"),
            }
        )
        return self.queue_path

    def get_pending_batch_ids(self) -> list[str]:
        return [batch_id for batch_id, payload in self._load_queue().items() if payload.get("status") == "pending"]

    def has_pending_batch(self, batch_id: str) -> bool:
        payload = self._load_queue().get(batch_id)
        return payload is not None and payload.get("status") == "pending"

    def get_entry(self, batch_id: str) -> dict[str, Any] | None:
        return self._load_queue().get(batch_id)

    def save_decision(self, decision: ReviewDecision) -> Path:
        return self.mark_decision(decision)

    def _load_queue(self) -> dict[str, dict[str, Any]]:
        if not self.queue_path.exists():
            return {}
        payload = json.loads(self.queue_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("review_queue.json 根节点必须是对象")
        return payload

    def _save_queue(self, queue: dict[str, dict[str, Any]]) -> Path:
        self.queue_path.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.queue_path

    def _append_history(self, payload: dict[str, Any]) -> Path:
        with self.history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False))
            handle.write("\n")
        return self.history_path
