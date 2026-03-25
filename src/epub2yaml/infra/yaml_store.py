from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from epub2yaml.domain.services import dump_yaml_document


class YamlDocumentStore:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.current_dir = run_dir / "current"
        self.history_dir = run_dir / "history"
        self.current_dir.mkdir(parents=True, exist_ok=True)
        self.history_dir.mkdir(parents=True, exist_ok=True)

    def load_document(self, doc_type: str) -> dict[str, Any]:
        path = self.current_dir / f"{doc_type}.yaml"
        if not path.exists():
            return {}

        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            raise ValueError(f"{doc_type}.yaml 根节点必须是映射")
        value = payload.get(doc_type, {})
        if not isinstance(value, dict):
            raise ValueError(f"{doc_type}.yaml 的 {doc_type} 节点必须是映射")
        return value

    def save_current_document(self, doc_type: str, content: dict[str, Any]) -> Path:
        path = self.current_dir / f"{doc_type}.yaml"
        path.write_text(dump_yaml_document(doc_type, content), encoding="utf-8")
        return path

    def save_history_document(self, doc_type: str, version: int, content: dict[str, Any]) -> Path:
        history_dir = self.history_dir / doc_type
        history_dir.mkdir(parents=True, exist_ok=True)
        path = history_dir / f"v{version:04d}.yaml"
        path.write_text(dump_yaml_document(doc_type, content), encoding="utf-8")
        return path
