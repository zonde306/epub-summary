from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from epub2yaml.llm.chains.document_update_chain import DocumentUpdateChain


@dataclass(frozen=True)
class ModelFactoryConfig:
    provider: str = "openai"
    model: str = ""
    api_key: str | None = None
    base_url: str | None = None
    temperature: float = 0.0

    @classmethod
    def from_env(
        cls,
        *,
        provider: str | None = None,
        model: str | None = None,
        env: dict[str, str] | None = None,
    ) -> "ModelFactoryConfig":
        source = env or os.environ
        resolved_provider = (provider or source.get("EPUB2YAML_MODEL_PROVIDER") or "openai").strip().lower()
        resolved_model = (model or source.get("EPUB2YAML_MODEL") or "").strip()
        if not resolved_model:
            raise ValueError("缺少模型名称，请设置环境变量 EPUB2YAML_MODEL 或通过参数传入 --model")

        api_key = source.get("EPUB2YAML_API_KEY") or source.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("缺少 API Key，请设置环境变量 EPUB2YAML_API_KEY 或 OPENAI_API_KEY")

        temperature_raw = source.get("EPUB2YAML_TEMPERATURE", "0")
        try:
            temperature = float(temperature_raw)
        except ValueError as exc:
            raise ValueError(f"环境变量 EPUB2YAML_TEMPERATURE 不是合法数字: {temperature_raw}") from exc

        return cls(
            provider=resolved_provider,
            model=resolved_model,
            api_key=api_key,
            base_url=(source.get("EPUB2YAML_BASE_URL") or source.get("OPENAI_BASE_URL") or "").strip() or None,
            temperature=temperature,
        )


def create_chat_model(config: ModelFactoryConfig) -> Any:
    provider = config.provider.lower()
    if provider != "openai":
        raise ValueError(f"暂不支持的模型提供方: {config.provider}")

    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise RuntimeError("缺少依赖 langchain-openai，无法创建 OpenAI 聊天模型") from exc

    return ChatOpenAI(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
    )


def create_document_update_chain_from_env(*, provider: str | None = None, model: str | None = None) -> DocumentUpdateChain:
    config = ModelFactoryConfig.from_env(provider=provider, model=model)
    return DocumentUpdateChain(create_chat_model(config))
