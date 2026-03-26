from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

import yaml

from epub2yaml.domain.models import Chapter, ChapterBatch, DeltaPackage
from epub2yaml.utils.hashing import sha256_text

SCALAR_ARRAY_FIELD_NAMES: set[str] = {
    "trigger_keywords",
    "identity",
    "character_brief_description",
    "physical_quirks",
    "style",
    "likes",
    "dislikes",
    "fan_tropes",
    "verbatim_quotes",
    "trivia_facts",
}

OBJECT_ARRAY_MERGE_RULES: dict[str, tuple[str, ...]] = {
    "actors.*.personality_core.personal_traits": ("trait_name", "scope"),
    "actors.*.personality_core.internal_conflicts": ("conflict_name", "scope"),
    "actors.*.skills_and_vulnerabilities.talents_and_skills": ("category", "skill_name"),
    "actors.*.skills_and_vulnerabilities.special_abilities": ("name",),
    "actors.*.skills_and_vulnerabilities.tools_and_equipment": ("item_name",),
    "actors.*.canon_timeline": ("event", "timeframe"),
    "actors.*.dialogue_and_quotes.other_dialogue_examples": ("cue", "response"),
    "actors.*.sex_history": ("partner", "behavior", "result"),
    "actors.*.pregnancy": ("weeks", "father", "race", "bloodline"),
    "actors.*.offspring": ("name", "dob", "father"),
}

ACTOR_FIELD_NORMALIZATION_RULES: dict[str, tuple[str, ...]] = {
    "character_brief_description": ("basic_settings", "character_brief_description"),
}


@dataclass(frozen=True)
class MergeWarning:
    path: str
    code: str
    message: str


@dataclass(frozen=True)
class MergeDeltaResult:
    actors: dict[str, Any]
    worldinfo: dict[str, Any]
    warnings: list[MergeWarning]


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


def merge_document(current: dict[str, Any], delta: dict[str, Any] | None, *, path: str = "") -> dict[str, Any]:
    merged, _ = merge_document_with_warnings(current, delta, path=path)
    return merged


def merge_document_with_warnings(current: dict[str, Any], delta: dict[str, Any] | None, *, path: str = "") -> tuple[dict[str, Any], list[MergeWarning]]:
    if not isinstance(current, dict):
        raise ValueError("current 文档根节点必须是映射")
    if delta is not None and not isinstance(delta, dict):
        raise ValueError("delta 文档根节点必须是映射")

    warnings: list[MergeWarning] = []
    merged = _merge_mapping(current, delta or {}, path=path, warnings=warnings)
    return merged, warnings


def merge_delta_package(actors_current: dict[str, Any], worldinfo_current: dict[str, Any], delta_package: DeltaPackage) -> tuple[dict[str, Any], dict[str, Any]]:
    result = merge_delta_package_with_warnings(actors_current, worldinfo_current, delta_package)
    return result.actors, result.worldinfo


def merge_delta_package_with_warnings(actors_current: dict[str, Any], worldinfo_current: dict[str, Any], delta_package: DeltaPackage) -> MergeDeltaResult:
    merged_actors, actor_warnings = merge_document_with_warnings(actors_current, delta_package.actors, path="actors")
    merged_worldinfo, worldinfo_warnings = merge_document_with_warnings(worldinfo_current, delta_package.worldinfo, path="worldinfo")
    return MergeDeltaResult(
        actors=merged_actors,
        worldinfo=merged_worldinfo,
        warnings=[*actor_warnings, *worldinfo_warnings],
    )


def dump_yaml_document(root_key: str, content: dict[str, Any]) -> str:
    payload = {root_key: content}
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


def dump_merge_warnings(warnings: list[MergeWarning]) -> str:
    return yaml.safe_dump([asdict(warning) for warning in warnings], allow_unicode=True, sort_keys=False)


def _merge_mapping(current: dict[str, Any], delta: dict[str, Any], *, path: str, warnings: list[MergeWarning]) -> dict[str, Any]:
    normalized_delta = _normalize_mapping_delta(delta, path=path)
    merged = deepcopy(current)
    for key, value in normalized_delta.items():
        key_path = _join_path(path, key)
        if key not in merged:
            merged[key] = deepcopy(value)
            continue
        merged[key] = _merge_value(merged[key], value, path=key_path, warnings=warnings)
    return merged


def _normalize_mapping_delta(delta: dict[str, Any], *, path: str) -> dict[str, Any]:
    normalized = deepcopy(delta)
    path_parts = path.split(".") if path else []
    if len(path_parts) != 2 or path_parts[0] != "actors":
        return normalized

    for source_key, target_path in ACTOR_FIELD_NORMALIZATION_RULES.items():
        if source_key not in normalized:
            continue
        _move_to_nested_path(normalized, source_key=source_key, target_path=target_path)
    return normalized


def _move_to_nested_path(payload: dict[str, Any], *, source_key: str, target_path: tuple[str, ...]) -> None:
    if source_key not in payload:
        return
    value = payload.pop(source_key)
    cursor = payload
    for part in target_path[:-1]:
        current = cursor.get(part)
        if not isinstance(current, dict):
            current = {}
            cursor[part] = current
        cursor = current
    cursor[target_path[-1]] = value


def _merge_value(current: Any, delta: Any, *, path: str, warnings: list[MergeWarning]) -> Any:
    current_kind = _node_kind(current)
    delta_kind = _node_kind(delta)

    if current_kind == "mapping" and delta_kind == "mapping":
        return _merge_mapping(current, delta, path=path, warnings=warnings)
    if current_kind == "array" and delta_kind == "array":
        return _merge_array(current, delta, path=path, warnings=warnings)
    if current_kind != delta_kind and (current_kind in {"mapping", "array"} or delta_kind in {"mapping", "array"}):
        raise ValueError(f"类型不兼容: {path} ({current_kind} -> {delta_kind})")
    return deepcopy(delta)


def _merge_array(current: list[Any], delta: list[Any], *, path: str, warnings: list[MergeWarning]) -> list[Any]:
    if _is_scalar_array(current, delta, path=path):
        return deepcopy(delta)

    identifier_fields = _resolve_identifier_fields(path)
    if identifier_fields is None:
        warnings.append(
            MergeWarning(
                path=path,
                code="object_array_replace_fallback",
                message="对象数组路径未登记归并规则，已回退为整字段替换。",
            )
        )
        return deepcopy(delta)

    if not _is_object_array(current) or not _is_object_array(delta):
        warnings.append(
            MergeWarning(
                path=path,
                code="object_array_non_mapping_items",
                message="对象数组包含非映射元素，已回退为整字段替换。",
            )
        )
        return deepcopy(delta)

    return _merge_object_array(current, delta, path=path, identifier_fields=identifier_fields, warnings=warnings)


def _merge_object_array(current: list[dict[str, Any]], delta: list[dict[str, Any]], *, path: str, identifier_fields: tuple[str, ...], warnings: list[MergeWarning]) -> list[dict[str, Any]]:
    current_index: dict[tuple[str, ...], int] = {}
    merged = [deepcopy(item) for item in current]

    for index, item in enumerate(current):
        identity = _extract_identifier(item, identifier_fields)
        if identity is None:
            continue
        current_index[identity] = index

    for item in delta:
        identity = _extract_identifier(item, identifier_fields)
        if identity is None:
            warnings.append(
                MergeWarning(
                    path=path,
                    code="missing_identifier_fields",
                    message=f"对象数组元素缺少识别字段 {', '.join(identifier_fields)}，已回退为整字段替换。",
                )
            )
            return deepcopy(delta)

        existing_index = current_index.get(identity)
        if existing_index is None:
            merged.append(deepcopy(item))
            current_index[identity] = len(merged) - 1
            continue

        merged[existing_index] = _merge_mapping(merged[existing_index], item, path=path, warnings=warnings)

    return merged


def _resolve_identifier_fields(path: str) -> tuple[str, ...] | None:
    for pattern, identifier_fields in OBJECT_ARRAY_MERGE_RULES.items():
        if _path_matches(path, pattern):
            return identifier_fields
    return None


def _is_scalar_array(current: list[Any], delta: list[Any], *, path: str) -> bool:
    field_name = path.rsplit(".", maxsplit=1)[-1]
    if field_name in SCALAR_ARRAY_FIELD_NAMES:
        return True
    return _all_scalar_items(current) and _all_scalar_items(delta)


def _all_scalar_items(values: list[Any]) -> bool:
    return all(_node_kind(value) == "scalar" for value in values)


def _is_object_array(values: list[Any]) -> bool:
    return all(isinstance(value, dict) for value in values)


def _extract_identifier(item: dict[str, Any], identifier_fields: tuple[str, ...]) -> tuple[str, ...] | None:
    values: list[str] = []
    for field in identifier_fields:
        value = item.get(field)
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        values.append(text)
    return tuple(values)


def _path_matches(path: str, pattern: str) -> bool:
    path_parts = path.split(".")
    pattern_parts = pattern.split(".")
    if len(path_parts) != len(pattern_parts):
        return False
    return all(pattern_part == "*" or pattern_part == path_part for path_part, pattern_part in zip(path_parts, pattern_parts))


def _join_path(base: str, key: str) -> str:
    if not base:
        return key
    return f"{base}.{key}"


def _node_kind(value: Any) -> str:
    if isinstance(value, dict):
        return "mapping"
    if isinstance(value, list):
        return "array"
    return "scalar"
