from __future__ import annotations

from copy import deepcopy
from typing import Any

import yaml

from epub2yaml.domain.models import Chapter, ChapterBatch, DeltaPackage
from epub2yaml.utils.hashing import sha256_text


def build_batches(chapters: list[Chapter], *, target_input_tokens: int, max_input_tokens: int, min_chapters_per_batch: int = 1, max_chapters_per_batch: int = 8, batch_number_start: int = 1) -> list[ChapterBatch]:
    batches: list[ChapterBatch] = []
    index = 0

    while index < len(chapters):
        batch_chapters: list[Chapter] = []
        batch_tokens = 0

        while index < len(chapters) and len(batch_chapters) < max_chapters_per_batch:
            chapter = chapters[index]
            next_tokens = batch_tokens + chapter.estimated_tokens

            if batch_chapters and next_tokens > max_input_tokens:
                break

            if batch_chapters and len(batch_chapters) >= min_chapters_per_batch and batch_tokens >= target_input_tokens:
                break

            batch_chapters.append(chapter)
            batch_tokens = next_tokens
            index += 1

            if batch_tokens >= target_input_tokens and len(batch_chapters) >= min_chapters_per_batch:
                break

        if not batch_chapters:
            chapter = chapters[index]
            batch_chapters = [chapter]
            batch_tokens = chapter.estimated_tokens
            index += 1

        combined_text = "\n\n".join(
            f"# Chapter {chapter.index + 1}: {chapter.title}\n{chapter.content_text}"
            for chapter in batch_chapters
        )
        batch_id = f"{batch_number_start + len(batches):04d}"
        batches.append(
            ChapterBatch(
                batch_id=batch_id,
                start_chapter_index=batch_chapters[0].index,
                end_chapter_index=batch_chapters[-1].index,
                chapter_indices=[chapter.index for chapter in batch_chapters],
                combined_text=combined_text,
                combined_hash=sha256_text(combined_text),
                estimated_input_tokens=batch_tokens,
            )
        )

    return batches


def _unwrap_markdown_code_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text

    lines = stripped.splitlines()
    if len(lines) < 2:
        return text

    if not lines[0].startswith("```"):
        return text

    closing_index: int | None = None
    for index in range(len(lines) - 1, 0, -1):
        if lines[index].strip().startswith("```"):
            closing_index = index
            break

    if closing_index is None:
        return text

    return "\n".join(lines[1:closing_index]).strip()


def parse_delta_yaml(delta_yaml: str) -> DeltaPackage:
    normalized_yaml = _unwrap_markdown_code_fence(delta_yaml)
    try:
        parsed = yaml.safe_load(normalized_yaml) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Delta YAML 解析失败: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError("Delta YAML 根节点必须是映射")

    delta = parsed.get("delta", parsed)
    if not isinstance(delta, dict):
        raise ValueError("delta 节点必须是映射")

    actors = delta.get("actors")
    worldinfo = delta.get("worldinfo")

    if actors is not None and not isinstance(actors, dict):
        raise ValueError("delta.actors 必须是映射")
    if worldinfo is not None and not isinstance(worldinfo, dict):
        raise ValueError("delta.worldinfo 必须是映射")

    return DeltaPackage(actors=actors, worldinfo=worldinfo)


def parse_yaml_mapping_document(document_text: str, *, root_key: str) -> dict[str, Any]:
    normalized_yaml = _unwrap_markdown_code_fence(document_text)
    try:
        payload = yaml.safe_load(normalized_yaml) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{root_key}.yaml 解析失败: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"{root_key}.yaml 根节点必须是映射")

    content = payload.get(root_key, {})
    if not isinstance(content, dict):
        raise ValueError(f"{root_key}.yaml 的 {root_key} 节点必须是映射")

    return content


def merge_document(current: dict[str, Any], delta: dict[str, Any] | None) -> dict[str, Any]:
    merged = deepcopy(current)
    if not delta:
        return merged

    for key, value in delta.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = merge_document(merged[key], value)
        else:
            merged[key] = deepcopy(value)

    return merged


def merge_delta_package(actors_current: dict[str, Any], worldinfo_current: dict[str, Any], delta_package: DeltaPackage) -> tuple[dict[str, Any], dict[str, Any]]:
    merged_actors = merge_document(actors_current, delta_package.actors)
    merged_worldinfo = merge_document(worldinfo_current, delta_package.worldinfo)
    return merged_actors, merged_worldinfo


def dump_yaml_document(root_key: str, content: dict[str, Any]) -> str:
    payload = {root_key: content}
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


def extract_mapping_paths(mapping: dict[str, Any], *, prefix: str | None = None) -> set[str]:
    paths: set[str] = set()
    for key, value in mapping.items():
        current_path = f"{prefix}.{key}" if prefix else key
        paths.add(current_path)
        if isinstance(value, dict):
            paths.update(extract_mapping_paths(value, prefix=current_path))
    return paths


def detect_missing_mapping_paths(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    previous_paths = extract_mapping_paths(previous)
    current_paths = extract_mapping_paths(current)
    return sorted(previous_paths - current_paths)


def detect_missing_document_paths(previous_document_text: str, current_document_text: str, *, root_key: str) -> list[str]:
    previous_content = parse_yaml_mapping_document(previous_document_text, root_key=root_key)
    current_content = parse_yaml_mapping_document(current_document_text, root_key=root_key)
    return detect_missing_mapping_paths(previous_content, current_content)


def detect_structure_loss(
    *,
    previous_actors_document: str,
    current_actors_document: str,
    previous_worldinfo_document: str,
    current_worldinfo_document: str,
) -> dict[str, Any]:
    actors_missing_paths = detect_missing_document_paths(
        previous_actors_document,
        current_actors_document,
        root_key="actors",
    )
    worldinfo_missing_paths = detect_missing_document_paths(
        previous_worldinfo_document,
        current_worldinfo_document,
        root_key="worldinfo",
    )
    missing_paths = [*actors_missing_paths, *worldinfo_missing_paths]
    structure_check_passed = not missing_paths
    return {
        "structure_check_passed": structure_check_passed,
        "requires_loss_approval": not structure_check_passed,
        "missing_paths": missing_paths,
        "actors_missing_paths": actors_missing_paths,
        "worldinfo_missing_paths": worldinfo_missing_paths,
    }
