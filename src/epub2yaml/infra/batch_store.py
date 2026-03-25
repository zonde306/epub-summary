from __future__ import annotations

from pathlib import Path


class BatchArtifactStore:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.batches_dir = run_dir / "batches"
        self.batches_dir.mkdir(parents=True, exist_ok=True)

    def write_text_artifact(self, batch_id: str, name: str, content: str) -> Path:
        batch_dir = self._batch_dir(batch_id)
        path = batch_dir / name
        path.write_text(content, encoding="utf-8")
        return path

    def read_text_artifact(self, batch_id: str, name: str) -> str:
        path = self._batch_dir(batch_id) / name
        return path.read_text(encoding="utf-8")

    def _batch_dir(self, batch_id: str) -> Path:
        batch_dir = self.batches_dir / batch_id
        batch_dir.mkdir(parents=True, exist_ok=True)
        return batch_dir
