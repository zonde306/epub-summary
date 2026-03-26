from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from epub2yaml.app.editor import EditorLaunchResult
from epub2yaml.app.services import PipelineService
from epub2yaml.domain.enums import ControlAction, ReviewAction


class StubEditorLauncher:
    def __init__(self, *, exit_code: int | None = 0, error: str | None = None) -> None:
        self.exit_code = exit_code
        self.error = error
        self.opened_files: list[str] = []

    def open(self, file_path: Path) -> EditorLaunchResult:
        self.opened_files.append(str(file_path))
        return EditorLaunchResult(
            command=f'notepad "{file_path}"',
            exit_code=self.exit_code,
            waited=True,
            error=self.error,
        )


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

    def test_prepare_manual_edit_exports_workspace_and_marks_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            editor = StubEditorLauncher()
            service = PipelineService(workspace_dir, editor_launcher=editor)
            epub_path = workspace_dir / "manual.epub"
            epub_path.write_bytes(b"fake-epub")

            with patch("epub2yaml.app.services.extract_epub") as mock_extract_epub:
                mock_extract_epub.return_value = [
                    self._chapter(0, "第一章", "chapter1.xhtml", "内容", 8),
                ]
                service.init_run(epub_path, book_id="manual-book")

            session = service.prepare_manual_edit("manual-book", open_editor=False)
            run_dir = workspace_dir / "runs" / "manual-book"
            actors_path = run_dir / "manual_edit" / "actors.editable.yaml"
            worldinfo_path = run_dir / "manual_edit" / "worldinfo.editable.yaml"
            self.assertEqual("0001", session.batch_id)
            self.assertTrue(actors_path.exists())
            self.assertTrue(worldinfo_path.exists())
            status = service.show_status("manual-book")
            self.assertTrue(status["awaiting_manual_edit"])
            self.assertEqual("await_manual_edit", status["recommended_action"])
            self.assertEqual("manual_edit", status["manual_edit_workspace"])

    def test_apply_manual_edit_and_continue_reuses_same_batch_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            editor = StubEditorLauncher()
            service = PipelineService(workspace_dir, editor_launcher=editor)
            epub_path = workspace_dir / "manual-continue.epub"
            epub_path.write_bytes(b"fake-epub")

            with patch("epub2yaml.app.services.extract_epub") as mock_extract_epub:
                mock_extract_epub.return_value = [
                    self._chapter(0, "第一章", "chapter1.xhtml", "内容", 8),
                ]
                service.init_run(epub_path, book_id="manual-continue-book")

            service.prepare_manual_edit("manual-continue-book", open_editor=False)
            run_dir = workspace_dir / "runs" / "manual-continue-book"
            (run_dir / "manual_edit" / "actors.editable.yaml").write_text(
                """
                actors:
                  Alice:
                    profile:
                      role: baseline
                """,
                encoding="utf-8",
            )
            (run_dir / "manual_edit" / "worldinfo.editable.yaml").write_text(
                """
                worldinfo:
                  Academy:
                    content: baseline
                """,
                encoding="utf-8",
            )

            applied = service.apply_manual_edit_session("manual-continue-book")
            self.assertEqual("applied", applied.status)
            decision = service.get_recovery_decision("manual-continue-book")
            self.assertEqual("continue_after_manual_edit", decision.action)
            self.assertEqual("0001", decision.batch_id)

            record = service.continue_after_manual_edit(
                "manual-continue-book",
                delta_yaml_text="""
                delta:
                  actors:
                    Alice:
                      profile:
                        role: merged
                  worldinfo:
                    Academy:
                      content: merged
                """,
            )
            self.assertEqual("0001", record.batch.batch_id)
            self.assertEqual("review_required", record.status)

    def test_pause_interrupts_running_workflow_at_control_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            service = PipelineService(workspace_dir)
            epub_path = workspace_dir / "pause.epub"
            epub_path.write_bytes(b"fake-epub")

            with patch("epub2yaml.app.services.extract_epub") as mock_extract_epub:
                mock_extract_epub.return_value = [
                    self._chapter(0, "第一章", "chapter1.xhtml", "内容", 8),
                ]
                service.init_run(epub_path, book_id="pause-book")

            def callback(event: dict[str, object]) -> None:
                if event.get("event") == "batch_started":
                    service.request_control_action("pause-book", ControlAction.PAUSE)

            result = service.run_to_completion(
                "pause-book",
                delta_yaml_by_batch={
                    "0001": """
                    delta:
                      actors:
                        Alice:
                          profile:
                            role: hero
                    """,
                },
                progress_callback=callback,
            )

            self.assertEqual("paused", result["status"])
            status = service.show_status("pause-book")
            self.assertEqual("paused", status["status"])
            self.assertEqual("pause-book", status["book_id"])
            self.assertEqual("paused", status["recommended_action"])

    def test_prepare_manual_edit_interrupts_running_workflow_and_exports_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            editor = StubEditorLauncher(exit_code=1)
            service = PipelineService(workspace_dir, editor_launcher=editor)
            epub_path = workspace_dir / "interrupt-manual.epub"
            epub_path.write_bytes(b"fake-epub")

            with patch("epub2yaml.app.services.extract_epub") as mock_extract_epub:
                mock_extract_epub.return_value = [
                    self._chapter(0, "第一章", "chapter1.xhtml", "内容", 8),
                ]
                service.init_run(epub_path, book_id="interrupt-manual-book")

            def callback(event: dict[str, object]) -> None:
                if event.get("event") == "batch_started":
                    service.request_control_action("interrupt-manual-book", ControlAction.PREPARE_MANUAL_EDIT)

            result = service.run_to_completion(
                "interrupt-manual-book",
                delta_yaml_by_batch={
                    "0001": """
                    delta:
                      actors:
                        Alice:
                          profile:
                            role: hero
                    """,
                },
                progress_callback=callback,
            )

            self.assertEqual("awaiting_manual_edit", result["status"])
            run_dir = workspace_dir / "runs" / "interrupt-manual-book"
            self.assertTrue((run_dir / "manual_edit" / "actors.editable.yaml").exists())
            self.assertTrue((run_dir / "manual_edit" / "worldinfo.editable.yaml").exists())
            status = service.show_status("interrupt-manual-book")
            self.assertTrue(status["awaiting_manual_edit"])
            self.assertEqual("await_manual_edit", status["recommended_action"])

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
