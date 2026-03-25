from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from epub2yaml.app.services import PipelineService
from epub2yaml.domain.models import Chapter
from epub2yaml.utils.hashing import sha256_text


class MVPPipelineTests(unittest.TestCase):
    def test_generate_yaml_runs_end_to_end_and_outputs_current_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            service = PipelineService(workspace_dir)
            epub_path = workspace_dir / "novel.epub"
            epub_path.write_bytes(b"fake-epub")

            with patch("epub2yaml.app.services.extract_epub") as mock_extract_epub:
                mock_extract_epub.return_value = [
                    self._chapter(0, "第一章", "chapter1.xhtml", "爱丽丝进入学院", 8),
                    self._chapter(1, "第二章", "chapter2.xhtml", "学院位于山谷中", 7),
                ]
                result = service.generate_yaml(
                    epub_path,
                    book_id="mvp-book",
                    delta_yaml_by_batch={
                        "0001": """
                        delta:
                          actors:
                            Alice:
                              profile:
                                role: hero
                          worldinfo:
                            Academy:
                              content: mountain valley school
                        """,
                    },
                )

            self.assertEqual("completed", result["status"])
            self.assertEqual(2, result["total_chapters"])
            self.assertEqual(["0001"], result["processed_batches"])

            actors_text = Path(result["actors_path"]).read_text(encoding="utf-8")
            worldinfo_text = Path(result["worldinfo_path"]).read_text(encoding="utf-8")
            self.assertIn("Alice", actors_text)
            self.assertIn("Academy", worldinfo_text)

    def test_generate_yaml_reports_progress_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            service = PipelineService(workspace_dir)
            epub_path = workspace_dir / "progress.epub"
            epub_path.write_bytes(b"fake-epub")
            progress_events: list[dict[str, object]] = []

            with patch("epub2yaml.app.services.extract_epub") as mock_extract_epub:
                mock_extract_epub.return_value = [
                    self._chapter(0, "第一章", "chapter1.xhtml", "爱丽丝进入学院", 8),
                ]
                service.generate_yaml(
                    epub_path,
                    book_id="progress-book",
                    delta_yaml_by_batch={
                        "0001": """
                        delta:
                          actors:
                            Alice:
                              profile:
                                role: hero
                        """,
                    },
                    progress_callback=progress_events.append,
                )

            self.assertEqual("run_initialized", progress_events[0]["event"])
            self.assertEqual("batch_started", progress_events[1]["event"])
            self.assertEqual("batch_completed", progress_events[2]["event"])
            self.assertEqual("0001", progress_events[1]["batch_id"])
            self.assertEqual("0001", progress_events[2]["batch_id"])
            self.assertEqual("continue_new_batch", progress_events[1]["recovery_action"])

    def test_generate_yaml_fails_on_invalid_delta_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            service = PipelineService(workspace_dir)
            epub_path = workspace_dir / "invalid.epub"
            epub_path.write_bytes(b"fake-epub")

            with patch("epub2yaml.app.services.extract_epub") as mock_extract_epub:
                mock_extract_epub.return_value = [
                    self._chapter(0, "第一章", "chapter1.xhtml", "内容", 5),
                ]
                service.init_run(epub_path, book_id="invalid-book")

            with self.assertRaisesRegex(ValueError, "Delta YAML 解析失败|delta 节点必须是映射|delta.actors 必须是映射"):
                service.run_to_completion(
                    "invalid-book",
                    delta_yaml_by_batch={
                        "0001": "delta: [not-a-mapping]",
                    },
                )

    def test_generate_yaml_can_resume_after_failure_during_run_to_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            service = PipelineService(workspace_dir)
            epub_path = workspace_dir / "retry-run.epub"
            epub_path.write_bytes(b"fake-epub")

            with patch("epub2yaml.app.services.extract_epub") as mock_extract_epub:
                mock_extract_epub.return_value = [
                    self._chapter(0, "第一章", "chapter1.xhtml", "内容", 5),
                ]
                service.init_run(epub_path, book_id="retry-run-book")

            with self.assertRaisesRegex(ValueError, "Delta YAML 解析失败|delta 节点必须是映射|delta.actors 必须是映射"):
                service.run_to_completion(
                    "retry-run-book",
                    delta_yaml_by_batch={
                        "0001": "delta: [not-a-mapping]",
                    },
                )

            result = service.run_to_completion(
                "retry-run-book",
                delta_yaml_by_batch={
                    "0001": """
                    delta:
                      actors:
                        Alice:
                          profile:
                            role: hero
                    """,
                },
            )
            self.assertEqual("completed", result["status"])
            self.assertEqual(["0001"], result["processed_batches"])

    def test_generate_yaml_blocks_auto_commit_when_structure_loss_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            service = PipelineService(workspace_dir)
            epub_path = workspace_dir / "loss-run.epub"
            epub_path.write_bytes(b"fake-epub")

            with patch("epub2yaml.app.services.extract_epub") as mock_extract_epub:
                mock_extract_epub.return_value = [
                    self._chapter(0, "第一章", "chapter1.xhtml", "内容", 5),
                ]
                service.init_run(epub_path, book_id="loss-run-book")

            run_dir = workspace_dir / "runs" / "loss-run-book"
            (run_dir / "current" / "actors.yaml").write_text(
                "actors:\n  Alice:\n    profile:\n      goals:\n        short_term: 保护妹妹\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "必须先人工审阅"):
                service.run_to_completion(
                    "loss-run-book",
                    delta_yaml_by_batch={
                        "0001": """
                        delta:
                          actors:
                            Alice:
                              profile: null
                        """,
                    },
                )

    def test_generate_yaml_fails_when_epub_has_no_chapters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            service = PipelineService(workspace_dir)
            epub_path = workspace_dir / "empty.epub"
            epub_path.write_bytes(b"fake-epub")

            with patch("epub2yaml.app.services.extract_epub") as mock_extract_epub:
                mock_extract_epub.return_value = []
                with self.assertRaisesRegex(ValueError, "未从 EPUB 中提取到可处理章节"):
                    service.generate_yaml(epub_path, book_id="empty-book")

    @staticmethod
    def _chapter(index: int, title: str, source_href: str, content_text: str, estimated_tokens: int) -> Chapter:
        return Chapter(
            index=index,
            title=title,
            source_href=source_href,
            content_text=content_text,
            content_hash=sha256_text(content_text),
            estimated_tokens=estimated_tokens,
        )


if __name__ == "__main__":
    unittest.main()
