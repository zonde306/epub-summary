from __future__ import annotations

import json
from pathlib import Path

from epub2yaml.domain.models import ReviewDecision


class ReviewQueueStore:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.state_dir = run_dir / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def enqueue(self, batch_id: str) -> Path:
        path = self.state_dir / "review_queue.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"batch_id": batch_id}, ensure_ascii=False))
            handle.write("\n")
        return path

    def save_decision(self, decision: ReviewDecision) -> Path:
        path = self.state_dir / "review_queue.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "batch_id": decision.batch_id,
                        "decision": decision.decision,
                        "reviewed_at": decision.reviewed_at.isoformat(),
                    },
                    ensure_ascii=False,
                )
            )
            handle.write("\n")
        return path
