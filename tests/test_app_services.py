from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from epub2yaml.app.services import PipelineService
from epub2yaml.domain.enums import ReviewAction


class PipelineServiceTests(unittest.TestCase):
    def test_process_and_review_batch_updates_run_state_and_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            service = PipelineService(workspace_dir)
            epub_path = workspace_dir / "book.epub"
            epub_path.write_bytes(b"fake-epub")

            with patch("epub2yaml.app.services.extract_epub") as mock_extract_epub:
                mock_extract_epub.return_value = [
                    self._chapter(0, "第一章", "chapter1.xhtml", "爱丽丝进入学院", 8),
                    self._chapter(1, "第二章", "chapter2.xhtml", "爱丽丝遇到导师", 9),
                ]

                state = service.init_run(epub_path, book_id="book-a")
                self.assertEqual("book-a", state.book_id)
                self.assertEqual(2, state.total_chapters)

                record = service.process_next_batch(
                    "book-a",
                    delta_yaml_text="""
                    delta:
                      actors:
                        Alice:
                          profile:
                            role: hero
                      worldinfo:
                        Academy:
                          content: magic school
                    """,
                )

                self.assertEqual("0001", record.batch.batch_id)
                self.assertEqual("review_required", record.status)

                decision = service.review_batch(
                    "book-a",
                    batch_id="0001",
                    action=ReviewAction.ACCEPT,
                    reviewer="tester",
                )

                self.assertEqual("accept", decision.decision)

                status = service.show_status("book-a")
                self.assertEqual("completed", status["status"])
                self.assertEqual(2, status["next_chapter_index"])
                self.assertEqual(1, status["actors_version"])
                self.assertEqual(1, status["worldinfo_version"])

                actors_path = workspace_dir / "runs" / "book-a" / "current" / "actors.yaml"
                worldinfo_path = workspace_dir / "runs" / "book-a" / "current" / "worldinfo.yaml"
                self.assertTrue(actors_path.exists())
                self.assertTrue(worldinfo_path.exists())
                self.assertIn("Alice", actors_path.read_text(encoding="utf-8"))
                self.assertIn("Academy", worldinfo_path.read_text(encoding="utf-8"))

    def test_reject_review_keeps_versions_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            service = PipelineService(workspace_dir)
            epub_path = workspace_dir / "book.epub"
            epub_path.write_bytes(b"fake-epub")

            with patch("epub2yaml.app.services.extract_epub") as mock_extract_epub:
                mock_extract_epub.return_value = [
                    self._chapter(0, "第一章", "chapter1.xhtml", "内容", 8),
                ]

                service.init_run(epub_path, book_id="book-b")
                service.process_next_batch(
                    "book-b",
                    delta_yaml_text="""
                    delta:
                      actors:
                        Alice:
                          profile:
                            role: hero
                    """,
                )
                decision = service.review_batch(
                    "book-b",
                    batch_id="0001",
                    action=ReviewAction.REJECT,
                    reviewer="tester",
                )

                self.assertEqual("reject", decision.decision)
                status = service.show_status("book-b")
                self.assertEqual("running", status["status"])
                self.assertEqual(0, status["actors_version"])
                self.assertEqual(0, status["worldinfo_version"])
                self.assertEqual(0, status["next_chapter_index"])

    @staticmethod
    def _chapter(index: int, title: str, source_href: str, content_text: str, estimated_tokens: int):
        from epub2yaml.domain.models import Chapter
        from epub2yaml.utils.hashing import sha256_text

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
