from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from epub2yaml.app.services import PipelineService
from epub2yaml.domain.models import Chapter
from epub2yaml.llm.chains.document_update_chain import DocumentUpdateChain, DocumentUpdateRequest
from epub2yaml.utils.hashing import sha256_text
from epub2yaml.workflow.graph import run_batch_generation_workflow


class LangChainAndLangGraphTests(unittest.TestCase):
    def test_document_update_chain_renders_prompt_and_returns_model_output(self) -> None:
        batch = Chapter(
            index=0,
            title="第一章",
            source_href="chapter1.xhtml",
            content_text="爱丽丝进入学院",
            content_hash=sha256_text("爱丽丝进入学院"),
            estimated_tokens=10,
        )
        request = DocumentUpdateRequest(
            batch=self._batch_from_chapters([batch]),
            filtered_actors_yaml="actors:\n  Alice:\n    trigger_keywords:\n      - 爱丽丝\n",
            filtered_worldinfo_yaml="worldinfo:\n  Academy:\n    keys: 爱丽丝,学院\n",
        )
        chain = DocumentUpdateChain(
            FakeMessagesListChatModel(
                responses=[
                    AIMessage(
                        content="delta:\n  actors:\n    Alice:\n      role: hero\n"
                    )
                ]
            )
        )

        result = chain.invoke(request)

        self.assertIn("章节范围", result.prompt_text)
        self.assertIn("爱丽丝进入学院", result.prompt_text)
        self.assertIn("相关 actors YAML（已裁剪，仅供参考）", result.prompt_text)
        self.assertIn("delta:", result.response_text)
        self.assertIn("Alice", result.response_text)

    def test_document_update_chain_prefers_streaming_output_when_available(self) -> None:
        batch = Chapter(
            index=0,
            title="第一章",
            source_href="chapter1.xhtml",
            content_text="爱丽丝进入学院",
            content_hash=sha256_text("爱丽丝进入学院"),
            estimated_tokens=10,
        )
        request = DocumentUpdateRequest(
            batch=self._batch_from_chapters([batch]),
            filtered_actors_yaml="actors: {}\n",
            filtered_worldinfo_yaml="worldinfo: {}\n",
        )
        stream_model = RunnableLambda(
            lambda _: AIMessage(content="这条 invoke 结果不应被使用")
        )
        stream_model.stream = lambda _: iter(
            [
                AIMessage(content="delta:\n"),
                AIMessage(content="  actors:\n"),
                AIMessage(content="    Alice:\n      role: hero\n"),
            ]
        )
        chain = DocumentUpdateChain(stream_model)

        result = chain.invoke(request)

        self.assertEqual(
            "delta:\n  actors:\n    Alice:\n      role: hero\n",
            result.response_text,
        )

    def test_document_update_chain_prompt_requires_changed_fields_only_and_identifier_fields(self) -> None:
        batch = Chapter(
            index=0,
            title="第一章",
            source_href="chapter1.xhtml",
            content_text="爱丽丝进入学院",
            content_hash=sha256_text("爱丽丝进入学院"),
            estimated_tokens=10,
        )
        request = DocumentUpdateRequest(
            batch=self._batch_from_chapters([batch]),
            filtered_actors_yaml="actors: {}\n",
            filtered_worldinfo_yaml="worldinfo: {}\n",
        )
        chain = DocumentUpdateChain(
            FakeMessagesListChatModel(
                responses=[AIMessage(content="delta:\n  actors: {}\n")]
            )
        )

        prompt_text = chain.render_prompt(request)

        self.assertIn("只输出发生变化的字段或变化子树", prompt_text)
        self.assertIn("必须保留用于定位该元素的识别字段", prompt_text)
        self.assertNotIn("上一版 actors YAML", prompt_text)

    def test_run_batch_generation_workflow_persists_preview_and_debug_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            service = PipelineService(workspace_dir)
            epub_path = workspace_dir / "book.epub"
            epub_path.write_bytes(b"fake-epub")

            with patch("epub2yaml.app.services.extract_epub") as mock_extract_epub:
                mock_extract_epub.return_value = [
                    self._chapter(0, "第一章", "chapter1.xhtml", "爱丽丝进入学院，学院位于山谷中", 8),
                ]
                service.init_run(epub_path, book_id="workflow-book")

            run_dir = workspace_dir / "runs" / "workflow-book"
            actors_path = run_dir / "current" / "actors.yaml"
            actors_path.write_text(
                "actors:\n  Alice:\n    trigger_keywords:\n      - 爱丽丝\n  Bob:\n    trigger_keywords:\n      - 鲍勃\n",
                encoding="utf-8",
            )
            worldinfo_path = run_dir / "current" / "worldinfo.yaml"
            worldinfo_path.write_text(
                "worldinfo:\n  Academy:\n    keys: 学院,山谷\n    content: old\n  Castle:\n    keys: 城堡\n    content: old\n",
                encoding="utf-8",
            )

            state = run_batch_generation_workflow(
                run_dir=run_dir,
                book_id="workflow-book",
                document_update_chain=None,
                llm_raw_output="""
                delta:
                  actors:
                    Alice:
                      profile:
                        role: hero
                """,
            )

            self.assertEqual("0001", state.batch_id)
            self.assertEqual("review_required", state.batch_record_status)
            self.assertIn("role: hero", state.actors_merged_preview or "")
            self.assertIn("Alice", state.filtered_actors_yaml)
            self.assertIn("Academy", state.filtered_worldinfo_yaml)

            batch_dir = workspace_dir / "runs" / "workflow-book" / "batches" / "0001"
            self.assertTrue((batch_dir / "prompt.txt").exists())
            self.assertTrue((batch_dir / "raw_output.md").exists())
            self.assertTrue((batch_dir / "delta.yaml").exists())
            self.assertTrue((batch_dir / "merged_actors.preview.yaml").exists())
            self.assertTrue((batch_dir / "filtered_context_summary.json").exists())
            self.assertTrue((batch_dir / "merge_warnings.json").exists())

            summary = json.loads((batch_dir / "filtered_context_summary.json").read_text(encoding="utf-8"))
            self.assertEqual("Alice", summary["actors"]["matched"][0]["name"])

    def test_pipeline_service_can_generate_delta_via_langchain_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            chain = DocumentUpdateChain(
                FakeMessagesListChatModel(
                    responses=[
                        AIMessage(
                            content="""
                            delta:
                              actors:
                                Alice:
                                  profile:
                                    role: hero
                              worldinfo:
                                Academy:
                                  content: magic school
                            """
                        )
                    ]
                )
            )
            service = PipelineService(workspace_dir, document_update_chain=chain)
            epub_path = workspace_dir / "book.epub"
            epub_path.write_bytes(b"fake-epub")

            with patch("epub2yaml.app.services.extract_epub") as mock_extract_epub:
                mock_extract_epub.return_value = [
                    self._chapter(0, "第一章", "chapter1.xhtml", "爱丽丝进入学院", 8),
                ]
                service.init_run(epub_path, book_id="chain-book")

            run_dir = workspace_dir / "runs" / "chain-book"
            (run_dir / "current" / "actors.yaml").write_text(
                "actors:\n  Alice:\n    trigger_keywords:\n      - 爱丽丝\n    profile:\n      role: student\n",
                encoding="utf-8",
            )
            (run_dir / "current" / "worldinfo.yaml").write_text(
                "worldinfo:\n  Academy:\n    keys: 学院,爱丽丝\n    content: old\n",
                encoding="utf-8",
            )

            record = service.process_next_batch("chain-book")

            self.assertEqual("review_required", record.status)
            batch_dir = workspace_dir / "runs" / "chain-book" / "batches" / record.batch.batch_id
            prompt_text = (batch_dir / "prompt.txt").read_text(encoding="utf-8")
            raw_output = (batch_dir / "raw_output.md").read_text(encoding="utf-8")
            self.assertIn("当前批次章节正文", prompt_text)
            self.assertIn("magic school", raw_output)
            self.assertIn("相关 actors YAML（已裁剪，仅供参考）", prompt_text)

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

    @staticmethod
    def _batch_from_chapters(chapters: list[Chapter]):
        from epub2yaml.domain.services import build_batches

        return build_batches(
            chapters,
            target_input_tokens=100,
            max_input_tokens=100,
            min_chapters_per_batch=1,
            max_chapters_per_batch=4,
        )[0]


if __name__ == "__main__":
    unittest.main()
