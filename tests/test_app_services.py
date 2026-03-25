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

    def test_run_to_completion_auto_commits_all_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            service = PipelineService(workspace_dir)
            epub_path = workspace_dir / "book.epub"
            epub_path.write_bytes(b"fake-epub")

            with patch("epub2yaml.app.services.extract_epub") as mock_extract_epub:
                mock_extract_epub.return_value = [
                    self._chapter(0, "第一章", "chapter1.xhtml", "爱丽丝进入学院", 8),
                    self._chapter(1, "第二章", "chapter2.xhtml", "导师介绍学院", 9),
                ]
                service.init_run(epub_path, book_id="book-auto")

            result = service.run_to_completion(
                "book-auto",
                delta_yaml_by_batch={
                    "0001": """
                    delta:
                      actors:
                        Alice:
                          profile:
                            role: hero
                      worldinfo:
                        Academy:
                          content: magic school
                    """,
                },
            )

            self.assertEqual("completed", result["status"])
            self.assertEqual(["0001"], result["processed_batches"])
            self.assertTrue(Path(result["actors_path"]).exists())
            self.assertTrue(Path(result["worldinfo_path"]).exists())

    def test_reject_review_keeps_versions_unchanged_and_requires_retry(self) -> None:
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
                self.assertEqual("retry_failed_batch", status["recommended_action"])

    def test_resume_run_prioritizes_pending_review_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            service = PipelineService(workspace_dir)
            epub_path = workspace_dir / "resume.epub"
            epub_path.write_bytes(b"fake-epub")

            with patch("epub2yaml.app.services.extract_epub") as mock_extract_epub:
                mock_extract_epub.return_value = [
                    self._chapter(0, "第一章", "chapter1.xhtml", "内容", 8),
                ]
                service.init_run(epub_path, book_id="resume-book")

            service.process_next_batch(
                "resume-book",
                delta_yaml_text="""
                delta:
                  actors:
                    Alice:
                      profile:
                        role: hero
                """,
            )

            decision = service.resume_run("resume-book")
            self.assertEqual("resume_pending_review", decision.action)
            self.assertEqual("0001", decision.batch_id)

    def test_retry_last_failed_reuses_same_batch_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            service = PipelineService(workspace_dir)
            epub_path = workspace_dir / "retry.epub"
            epub_path.write_bytes(b"fake-epub")

            with patch("epub2yaml.app.services.extract_epub") as mock_extract_epub:
                mock_extract_epub.return_value = [
                    self._chapter(0, "第一章", "chapter1.xhtml", "内容", 8),
                ]
                service.init_run(epub_path, book_id="retry-book")

            with self.assertRaisesRegex(ValueError, "Delta YAML 解析失败|delta 节点必须是映射|delta.actors 必须是映射"):
                service.process_next_batch("retry-book", delta_yaml_text="delta: [broken]")

            retried_record = service.retry_last_failed(
                "retry-book",
                delta_yaml_text="""
                delta:
                  actors:
                    Alice:
                      profile:
                        role: hero
                """,
            )
            self.assertEqual("0001", retried_record.batch.batch_id)
            self.assertEqual(1, retried_record.retry_count)
            self.assertEqual("review_required", retried_record.status)

    def test_structure_loss_requires_manual_review_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            service = PipelineService(workspace_dir)
            epub_path = workspace_dir / "loss.epub"
            epub_path.write_bytes(b"fake-epub")

            with patch("epub2yaml.app.services.extract_epub") as mock_extract_epub:
                mock_extract_epub.return_value = [
                    self._chapter(0, "第一章", "chapter1.xhtml", "内容", 8),
                ]
                service.init_run(epub_path, book_id="loss-book")

            run_dir = workspace_dir / "runs" / "loss-book"
            (run_dir / "current" / "actors.yaml").write_text(
                "actors:\n  Alice:\n    profile:\n      goals:\n        short_term: 保护妹妹\n",
                encoding="utf-8",
            )
            (run_dir / "current" / "worldinfo.yaml").write_text(
                "worldinfo:\n  MagicSystem:\n    rules:\n      cost: 高\n",
                encoding="utf-8",
            )

            record = service.process_next_batch(
                "loss-book",
                delta_yaml_text="""
                delta:
                  actors:
                    Alice:
                      profile: null
                  worldinfo:
                    MagicSystem:
                      rules: null
                """,
            )

            self.assertTrue(record.requires_loss_approval)
            self.assertEqual("pending", record.loss_approval_status)

            decision = service.resume_run("loss-book")
            self.assertEqual("review_structure_loss", decision.action)
            summary = service.get_review_batch_summary("loss-book", batch_id="0001")
            self.assertEqual(3, summary["missing_paths_count"])
            self.assertEqual("structure_loss_review", summary["review_kind"])

            with self.assertRaisesRegex(ValueError, "必须先人工审阅"):
                service.run_to_completion(
                    "loss-book",
                    delta_yaml_by_batch={
                        "0001": """
                        delta:
                          actors:
                            Alice:
                              profile: null
                          worldinfo:
                            MagicSystem:
                              rules: null
                        """,
                    },
                )

    def test_structure_loss_accept_allows_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            service = PipelineService(workspace_dir)
            epub_path = workspace_dir / "accept.epub"
            epub_path.write_bytes(b"fake-epub")

            with patch("epub2yaml.app.services.extract_epub") as mock_extract_epub:
                mock_extract_epub.return_value = [
                    self._chapter(0, "第一章", "chapter1.xhtml", "内容", 8),
                ]
                service.init_run(epub_path, book_id="accept-book")

            run_dir = workspace_dir / "runs" / "accept-book"
            (run_dir / "current" / "actors.yaml").write_text(
                "actors:\n  Alice:\n    profile:\n      goals:\n        short_term: 保护妹妹\n",
                encoding="utf-8",
            )

            record = service.process_next_batch(
                "accept-book",
                delta_yaml_text="""
                delta:
                  actors:
                    Alice:
                      profile: null
                """,
            )
            self.assertTrue(record.requires_loss_approval)

            decision = service.review_batch(
                "accept-book",
                batch_id="0001",
                action=ReviewAction.ACCEPT,
                reviewer="tester",
                comment="人工确认允许缺失",
            )
            self.assertEqual("accept", decision.decision)

            state = service.show_status("accept-book")
            self.assertEqual("completed", state["status"])
            saved_record = service.get_review_batch_summary("accept-book", batch_id="0001")
            self.assertEqual("approved", saved_record["loss_approval_status"])

    def test_structure_loss_reject_enters_retry_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            service = PipelineService(workspace_dir)
            epub_path = workspace_dir / "reject.epub"
            epub_path.write_bytes(b"fake-epub")

            with patch("epub2yaml.app.services.extract_epub") as mock_extract_epub:
                mock_extract_epub.return_value = [
                    self._chapter(0, "第一章", "chapter1.xhtml", "内容", 8),
                ]
                service.init_run(epub_path, book_id="reject-book")

            run_dir = workspace_dir / "runs" / "reject-book"
            (run_dir / "current" / "actors.yaml").write_text(
                "actors:\n  Alice:\n    profile:\n      goals:\n        short_term: 保护妹妹\n",
                encoding="utf-8",
            )

            service.process_next_batch(
                "reject-book",
                delta_yaml_text="""
                delta:
                  actors:
                    Alice:
                      profile: null
                """,
            )

            decision = service.review_batch(
                "reject-book",
                batch_id="0001",
                action=ReviewAction.REJECT,
                reviewer="tester",
                comment="缺失不可接受",
            )
            self.assertEqual("reject", decision.decision)
            state = service.show_status("reject-book")
            self.assertEqual("retry_failed_batch", state["recommended_action"])
            summary = service.get_review_batch_summary("reject-book", batch_id="0001")
            self.assertEqual("rejected", summary["loss_approval_status"])

    def test_edit_preview_then_commit_clears_structure_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            service = PipelineService(workspace_dir)
            epub_path = workspace_dir / "edit.epub"
            epub_path.write_bytes(b"fake-epub")

            with patch("epub2yaml.app.services.extract_epub") as mock_extract_epub:
                mock_extract_epub.return_value = [
                    self._chapter(0, "第一章", "chapter1.xhtml", "内容", 8),
                ]
                service.init_run(epub_path, book_id="edit-book")

            run_dir = workspace_dir / "runs" / "edit-book"
            (run_dir / "current" / "actors.yaml").write_text(
                "actors:\n  Alice:\n    profile:\n      goals:\n        short_term: 保护妹妹\n",
                encoding="utf-8",
            )

            service.process_next_batch(
                "edit-book",
                delta_yaml_text="""
                delta:
                  actors:
                    Alice:
                      profile: null
                """,
            )

            decision = service.review_batch(
                "edit-book",
                batch_id="0001",
                action=ReviewAction.EDIT,
                reviewer="tester",
                edited_actors_text="""
                actors:
                  Alice:
                    profile:
                      goals:
                        short_term: 保护妹妹
                """,
                edited_worldinfo_text="worldinfo: {}\n",
            )
            self.assertEqual("edit", decision.decision)
            summary = service.get_review_batch_summary("edit-book", batch_id="0001")
            self.assertFalse(summary["requires_loss_approval"])
            self.assertIsNone(summary["loss_approval_status"])

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
