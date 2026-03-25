from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable

from epub2yaml.domain.models import ChapterBatch

DEFAULT_SYSTEM_PROMPT = """你是一个负责维护小说 YAML 知识库的分析助手。
你的任务是读取当前批次章节正文，并基于上一版 actors/worldinfo 文档输出增量 Delta YAML。
输出必须只包含 YAML，不要添加解释、代码围栏或额外注释。"""

DEFAULT_FORMAT_RULES = """输出规则：
1. 顶层必须是 delta 映射。
2. 允许包含 delta.actors 与 delta.worldinfo 两个键。
3. 仅输出当前批次发生变化的角色或设定。
4. 字典字段按深层覆盖语义更新，列表字段按整字段替换语义输出。
5. 对不确定信息保持保守，不要输出“未知”“待补”“同上”等占位值。"""

DEFAULT_HUMAN_TEMPLATE = """请根据以下上下文生成 YAML Delta。

章节范围: {chapter_range}
批次编号: {batch_id}

上一版 actors YAML:
{previous_actors_yaml}

上一版 worldinfo YAML:
{previous_worldinfo_yaml}

当前批次章节正文:
{current_batch_chapters}
"""


@dataclass(frozen=True)
class DocumentUpdateRequest:
    batch: ChapterBatch
    previous_actors_yaml: str
    previous_worldinfo_yaml: str

    @property
    def chapter_range(self) -> str:
        return f"{self.batch.start_chapter_index}-{self.batch.end_chapter_index}"


@dataclass(frozen=True)
class DocumentUpdateResult:
    prompt_text: str
    response_text: str


class DocumentUpdateChain:
    def __init__(
        self,
        model: Runnable[Any, Any],
        *,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        format_rules: str = DEFAULT_FORMAT_RULES,
    ) -> None:
        self.model = model
        self.prompt = ChatPromptTemplate.from_messages(
            [
                ("system", "{system_prompt}\n\n{format_rules}"),
                ("human", DEFAULT_HUMAN_TEMPLATE),
            ]
        ).partial(system_prompt=system_prompt, format_rules=format_rules)
        self._chain = self.prompt | self.model

    def build_payload(self, request: DocumentUpdateRequest) -> dict[str, str]:
        return {
            "batch_id": request.batch.batch_id,
            "chapter_range": request.chapter_range,
            "previous_actors_yaml": request.previous_actors_yaml,
            "previous_worldinfo_yaml": request.previous_worldinfo_yaml,
            "current_batch_chapters": request.batch.combined_text,
        }

    def render_prompt(self, request: DocumentUpdateRequest) -> str:
        prompt_value = self.prompt.invoke(self.build_payload(request))
        return "\n\n".join(
            f"[{message.type}]\n{self._message_content_to_text(message)}" for message in prompt_value.messages
        )

    def invoke(self, request: DocumentUpdateRequest) -> DocumentUpdateResult:
        payload = self.build_payload(request)
        prompt_text = self.render_prompt(request)
        response = self._chain.invoke(payload)
        return DocumentUpdateResult(
            prompt_text=prompt_text,
            response_text=self._coerce_response_to_text(response),
        )

    def _coerce_response_to_text(self, response: Any) -> str:
        if isinstance(response, BaseMessage):
            return self._message_content_to_text(response)
        if isinstance(response, str):
            return response
        return str(response)

    @staticmethod
    def _message_content_to_text(message: BaseMessage) -> str:
        content = message.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if text is not None:
                        parts.append(str(text))
                        continue
                parts.append(str(item))
            return "\n".join(parts)
        return str(content)
