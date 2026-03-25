from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from epub2yaml.domain.enums import BatchStatus, RunStatus
from epub2yaml.domain.models import BatchRecord, ChapterBatch, DeltaPackage, FailureInfo, PipelineState
from epub2yaml.domain.services import build_batches, detect_structure_loss, dump_yaml_document, merge_delta_package, parse_delta_yaml, parse_yaml_mapping_document
from epub2yaml.infra.batch_store import BatchArtifactStore
from epub2yaml.infra.review_store import ReviewQueueStore
from epub2yaml.infra.state_store import StateStore
from epub2yaml.infra.yaml_store import YamlDocumentStore
from epub2yaml.llm.chains.document_update_chain import DocumentUpdateChain, DocumentUpdateRequest


class GraphState(TypedDict, total=False):
    book_id: str
    run_id: str | None
    batch_id: str | None
    batch: dict[str, Any] | None
    actors_current: str
    worldinfo_current: str
    prompt_text: str | None
    llm_raw_output: str | None
    delta_yaml: str | None
    actors_delta: dict[str, Any] | None
    worldinfo_delta: dict[str, Any] | None
    actors_merged_preview: str | None
    worldinfo_merged_preview: str | None
    validation_errors: list[str]
    review_decision: str | None
    edited_actors: str | None
    edited_worldinfo: str | None
    next_action: str | None
    error_message: str | None
    batch_record_status: str | None
    failure_stage: str | None
    failure_retryable: bool | None
    suggested_action: str | None
    retry_count: int
    structure_check_passed: bool
    requires_loss_approval: bool
    missing_paths: list[str]
    actors_missing_paths: list[str]
    worldinfo_missing_paths: list[str]


@dataclass(frozen=True)
class PipelineWorkflowContext:
    run_dir: Path
    state_store: StateStore
    yaml_store: YamlDocumentStore
    batch_store: BatchArtifactStore
    review_store: ReviewQueueStore
    document_update_chain: DocumentUpdateChain | None = None


def build_pipeline_graph(context: PipelineWorkflowContext):
    graph = StateGraph(GraphState)
    graph.add_node("load_run_state", _load_run_state(context))
    graph.add_node("handle_finish", _handle_finish(context))
    graph.add_node("prepare_batch", _prepare_batch(context))
    graph.add_node("load_current_documents", _load_current_documents(context))
    graph.add_node("build_prompt", _build_prompt(context))
    graph.add_node("invoke_llm", _invoke_llm(context))
    graph.add_node("parse_delta_output", _parse_delta_output())
    graph.add_node("merge_delta_preview", _merge_delta_preview(context))
    graph.add_node("validate_merged_preview", _validate_merged_preview())
    graph.add_node("detect_structure_loss", _detect_structure_loss())
    graph.add_node("enqueue_review", _enqueue_review(context))
    graph.add_node("handle_failure", _handle_failure(context))

    graph.add_edge(START, "load_run_state")
    graph.add_conditional_edges(
        "load_run_state",
        _route_after_load_run_state,
        {
            "continue": "prepare_batch",
            "finish": "handle_finish",
        },
    )
    graph.add_edge("prepare_batch", "load_current_documents")
    graph.add_edge("load_current_documents", "build_prompt")
    graph.add_conditional_edges(
        "build_prompt",
        _route_after_step,
        {
            "ok": "invoke_llm",
            "failed": "handle_failure",
        },
    )
    graph.add_conditional_edges(
        "invoke_llm",
        _route_after_step,
        {
            "ok": "parse_delta_output",
            "failed": "handle_failure",
        },
    )
    graph.add_conditional_edges(
        "parse_delta_output",
        _route_after_step,
        {
            "ok": "merge_delta_preview",
            "failed": "handle_failure",
        },
    )
    graph.add_edge("merge_delta_preview", "validate_merged_preview")
    graph.add_conditional_edges(
        "validate_merged_preview",
        _route_after_step,
        {
            "ok": "detect_structure_loss",
            "failed": "handle_failure",
        },
    )
    graph.add_edge("detect_structure_loss", "enqueue_review")
    graph.add_edge("enqueue_review", END)
    graph.add_edge("handle_failure", END)
    graph.add_edge("handle_finish", END)
    return graph.compile()


def run_batch_generation_workflow(
    *,
    run_dir: Path,
    book_id: str,
    document_update_chain: DocumentUpdateChain | None,
    llm_raw_output: str | None = None,
    batch_id: str | None = None,
    retry_count: int = 0,
) -> PipelineState:
    context = PipelineWorkflowContext(
        run_dir=run_dir,
        state_store=StateStore(run_dir),
        yaml_store=YamlDocumentStore(run_dir),
        batch_store=BatchArtifactStore(run_dir),
        review_store=ReviewQueueStore(run_dir),
        document_update_chain=document_update_chain,
    )
    app = build_pipeline_graph(context)
    final_state = app.invoke(
        {
            "book_id": book_id,
            "run_id": book_id,
            "batch_id": batch_id,
            "llm_raw_output": llm_raw_output,
            "validation_errors": [],
            "retry_count": retry_count,
        }
    )
    return PipelineState.model_validate(final_state)


def _load_run_state(context: PipelineWorkflowContext):
    def node(state: GraphState) -> GraphState:
        run_state = context.state_store.load_run_state()
        if run_state.next_chapter_index >= run_state.total_chapters and state.get("batch_id") is None:
            return {"next_action": "finish"}
        return {"next_action": "continue"}

    return node


def _handle_finish(context: PipelineWorkflowContext):
    def node(state: GraphState) -> GraphState:
        run_state = context.state_store.load_run_state()
        run_state.status = RunStatus.COMPLETED
        run_state.recommended_action = "completed"
        context.state_store.save_run_state(run_state)
        return {
            "next_action": "finish",
            "batch_record_status": None,
            "error_message": None,
            "validation_errors": [],
        }

    return node


def _prepare_batch(context: PipelineWorkflowContext):
    def node(state: GraphState) -> GraphState:
        run_state = context.state_store.load_run_state()
        existing_batch_id = state.get("batch_id")
        if existing_batch_id:
            batch = context.state_store.load_batch_input_model(existing_batch_id)
            context.state_store.append_checkpoint(
                "batch_reused",
                {
                    "batch_id": batch.batch_id,
                    "retry_count": state.get("retry_count", 0),
                },
            )
            run_state.last_generated_batch_id = batch.batch_id
            run_state.last_recovery_action = "retry_batch"
            run_state.last_recovery_batch_id = batch.batch_id
            context.state_store.save_run_state(run_state)
            return {
                "batch_id": batch.batch_id,
                "batch": batch.model_dump(mode="python"),
            }

        chapters = context.state_store.load_chapters()
        remaining = chapters[run_state.next_chapter_index :]
        next_batch_number = 1
        if run_state.last_accepted_batch_id is not None:
            next_batch_number = int(run_state.last_accepted_batch_id) + 1

        batch = build_batches(
            remaining,
            target_input_tokens=run_state.target_input_tokens,
            max_input_tokens=run_state.max_input_tokens,
            min_chapters_per_batch=run_state.min_chapters_per_batch,
            max_chapters_per_batch=run_state.max_chapters_per_batch,
            batch_number_start=next_batch_number,
        )[0]
        context.state_store.save_batch_input(batch)
        context.state_store.append_checkpoint(
            "batch_created",
            {
                "batch_id": batch.batch_id,
                "chapter_start": batch.start_chapter_index,
                "chapter_end": batch.end_chapter_index,
            },
        )
        run_state.last_generated_batch_id = batch.batch_id
        run_state.last_recovery_action = "continue_new_batch"
        run_state.last_recovery_batch_id = batch.batch_id
        context.state_store.save_run_state(run_state)
        return {
            "batch_id": batch.batch_id,
            "batch": batch.model_dump(mode="python"),
        }

    return node


def _load_current_documents(context: PipelineWorkflowContext):
    def node(state: GraphState) -> GraphState:
        actors_current = context.yaml_store.load_document("actors")
        worldinfo_current = context.yaml_store.load_document("worldinfo")
        return {
            "actors_current": dump_yaml_document("actors", actors_current),
            "worldinfo_current": dump_yaml_document("worldinfo", worldinfo_current),
        }

    return node


def _build_prompt(context: PipelineWorkflowContext):
    def node(state: GraphState) -> GraphState:
        batch = _require_batch(state)
        if state.get("llm_raw_output") is not None:
            prompt_text = "复用已有 Delta YAML 或原始模型输出，跳过 LangChain Prompt 渲染。"
        elif context.document_update_chain is None:
            message = "未配置 LangChain 文档更新链，无法调用模型"
            return {
                "validation_errors": [message],
                "error_message": message,
                "failure_stage": "build_prompt",
                "failure_retryable": False,
                "suggested_action": "provide_delta_or_configure_model",
            }
        else:
            request = DocumentUpdateRequest(
                batch=batch,
                previous_actors_yaml=state.get("actors_current", "actors: {}\n"),
                previous_worldinfo_yaml=state.get("worldinfo_current", "worldinfo: {}\n"),
            )
            prompt_text = context.document_update_chain.render_prompt(request)

        context.state_store.append_checkpoint(
            "prompt_built",
            {
                "batch_id": batch.batch_id,
                "prompt_length": len(prompt_text),
            },
        )
        return {
            "prompt_text": prompt_text,
            "validation_errors": [],
            "error_message": None,
        }

    return node


def _invoke_llm(context: PipelineWorkflowContext):
    def node(state: GraphState) -> GraphState:
        batch = _require_batch(state)
        if state.get("llm_raw_output") is not None:
            raw_output = state["llm_raw_output"]
        elif context.document_update_chain is None:
            message = "未配置 LangChain 文档更新链，且未提供 Delta YAML"
            return {
                "validation_errors": [message],
                "error_message": message,
                "failure_stage": "invoke_llm",
                "failure_retryable": False,
                "suggested_action": "provide_delta_or_configure_model",
            }
        else:
            request = DocumentUpdateRequest(
                batch=batch,
                previous_actors_yaml=state.get("actors_current", "actors: {}\n"),
                previous_worldinfo_yaml=state.get("worldinfo_current", "worldinfo: {}\n"),
            )
            try:
                result = context.document_update_chain.invoke(request)
            except Exception as exc:
                message = f"模型调用失败: {exc}"
                return {
                    "validation_errors": [message],
                    "error_message": message,
                    "failure_stage": "invoke_llm",
                    "failure_retryable": True,
                    "suggested_action": "retry_batch",
                }
            raw_output = result.response_text
            state["prompt_text"] = result.prompt_text

        context.state_store.append_checkpoint(
            "llm_output_ready",
            {
                "batch_id": batch.batch_id,
                "output_length": len(raw_output),
            },
        )
        return {
            "prompt_text": state.get("prompt_text"),
            "llm_raw_output": raw_output,
            "validation_errors": [],
            "error_message": None,
        }

    return node


def _parse_delta_output():
    def node(state: GraphState) -> GraphState:
        raw_output = state.get("llm_raw_output")
        if raw_output is None:
            message = "缺少 LLM 原始输出"
            return {
                "validation_errors": [message],
                "error_message": message,
                "failure_stage": "parse_delta_output",
                "failure_retryable": True,
                "suggested_action": "retry_batch",
            }

        try:
            delta_package = parse_delta_yaml(raw_output)
        except ValueError as exc:
            return {
                "delta_yaml": raw_output,
                "validation_errors": [str(exc)],
                "error_message": str(exc),
                "failure_stage": "parse_delta_output",
                "failure_retryable": True,
                "suggested_action": "retry_batch",
            }

        return {
            "delta_yaml": raw_output,
            "actors_delta": delta_package.actors,
            "worldinfo_delta": delta_package.worldinfo,
            "validation_errors": [],
            "error_message": None,
        }

    return node


def _merge_delta_preview(context: PipelineWorkflowContext):
    def node(state: GraphState) -> GraphState:
        actors_current = context.yaml_store.load_document("actors")
        worldinfo_current = context.yaml_store.load_document("worldinfo")
        delta_package = DeltaPackage(
            actors=state.get("actors_delta"),
            worldinfo=state.get("worldinfo_delta"),
        )
        merged_actors, merged_worldinfo = merge_delta_package(actors_current, worldinfo_current, delta_package)
        return {
            "actors_merged_preview": dump_yaml_document("actors", merged_actors),
            "worldinfo_merged_preview": dump_yaml_document("worldinfo", merged_worldinfo),
        }

    return node


def _validate_merged_preview():
    def node(state: GraphState) -> GraphState:
        errors: list[str] = []
        for root_key, content in (
            ("actors", state.get("actors_merged_preview")),
            ("worldinfo", state.get("worldinfo_merged_preview")),
        ):
            try:
                parse_yaml_mapping_document(content or f"{root_key}: {{}}\n", root_key=root_key)
            except ValueError as exc:
                errors.append(str(exc))
                continue

        if errors:
            return {
                "validation_errors": errors,
                "error_message": "; ".join(errors),
                "failure_stage": "validate_merged_preview",
                "failure_retryable": True,
                "suggested_action": "retry_batch",
            }
        return {
            "validation_errors": [],
            "error_message": None,
        }

    return node


def _detect_structure_loss():
    def node(state: GraphState) -> GraphState:
        result = detect_structure_loss(
            previous_actors_document=state.get("actors_current", "actors: {}\n"),
            current_actors_document=state.get("actors_merged_preview") or "actors: {}\n",
            previous_worldinfo_document=state.get("worldinfo_current", "worldinfo: {}\n"),
            current_worldinfo_document=state.get("worldinfo_merged_preview") or "worldinfo: {}\n",
        )
        return {
            "structure_check_passed": bool(result["structure_check_passed"]),
            "requires_loss_approval": bool(result["requires_loss_approval"]),
            "missing_paths": list(result["missing_paths"]),
            "actors_missing_paths": list(result["actors_missing_paths"]),
            "worldinfo_missing_paths": list(result["worldinfo_missing_paths"]),
        }

    return node

def _enqueue_review(context: PipelineWorkflowContext):
    def node(state: GraphState) -> GraphState:
        batch = _require_batch(state)
        batch_id = batch.batch_id
        actors_preview = state.get("actors_merged_preview") or "actors: {}\n"
        worldinfo_preview = state.get("worldinfo_merged_preview") or "worldinfo: {}\n"
        missing_paths = list(state.get("missing_paths", []))
        actors_missing_paths = list(state.get("actors_missing_paths", []))
        worldinfo_missing_paths = list(state.get("worldinfo_missing_paths", []))
        structure_check_passed = bool(state.get("structure_check_passed", True))
        requires_loss_approval = bool(state.get("requires_loss_approval", False))
        review_kind = "structure_loss_review" if requires_loss_approval else "normal_review"

        context.batch_store.write_text_artifact(batch_id, "prompt.txt", state.get("prompt_text") or "")
        context.batch_store.write_text_artifact(batch_id, "raw_output.md", state.get("llm_raw_output") or "")
        context.batch_store.write_text_artifact(batch_id, "delta.yaml", state.get("delta_yaml") or "")
        context.batch_store.write_text_artifact(batch_id, "merged_actors.preview.yaml", actors_preview)
        context.batch_store.write_text_artifact(batch_id, "merged_worldinfo.preview.yaml", worldinfo_preview)

        structure_check_payload = {
            "checked_at": datetime.utcnow().isoformat(),
            "batch_id": batch_id,
            "baseline": {
                "actors": "current/actors.yaml",
                "worldinfo": "current/worldinfo.yaml",
            },
            "preview": {
                "actors": f"batches/{batch_id}/merged_actors.preview.yaml",
                "worldinfo": f"batches/{batch_id}/merged_worldinfo.preview.yaml",
            },
            "structure_check_passed": structure_check_passed,
            "requires_loss_approval": requires_loss_approval,
            "missing_paths_count": len(missing_paths),
            "actors_missing_paths": actors_missing_paths,
            "worldinfo_missing_paths": worldinfo_missing_paths,
        }
        context.batch_store.write_text_artifact(
            batch_id,
            "structure_check.json",
            json.dumps(structure_check_payload, ensure_ascii=False, indent=2),
        )
        context.batch_store.write_text_artifact(
            batch_id,
            "missing_paths.txt",
            "\n".join(missing_paths) + ("\n" if missing_paths else ""),
        )

        record = BatchRecord(
            batch=batch,
            status=BatchStatus.REVIEW_REQUIRED,
            retry_count=state.get("retry_count", 0),
            structure_check_passed=structure_check_passed,
            missing_paths=missing_paths,
            actors_missing_paths=actors_missing_paths,
            worldinfo_missing_paths=worldinfo_missing_paths,
            requires_loss_approval=requires_loss_approval,
            loss_approval_status="pending" if requires_loss_approval else None,
        )
        context.state_store.save_batch_record(record)
        context.review_store.enqueue(batch_id, review_kind=review_kind)
        context.state_store.append_checkpoint(
            "batch_generated",
            {
                "batch_id": batch_id,
                "chapter_start": batch.start_chapter_index,
                "chapter_end": batch.end_chapter_index,
                "retry_count": state.get("retry_count", 0),
                "structure_check_passed": structure_check_passed,
                "requires_loss_approval": requires_loss_approval,
                "missing_paths_count": len(missing_paths),
            },
        )

        run_state = context.state_store.load_run_state()
        run_state.status = RunStatus.REVIEW_REQUIRED
        run_state.last_generated_batch_id = batch_id
        run_state.pending_review_batch_id = batch_id
        run_state.pending_loss_review_batch_id = batch_id if requires_loss_approval else None
        run_state.last_failed_batch_id = None
        run_state.last_failed_stage = None
        run_state.last_failure_reason = None
        run_state.last_failure_retryable = None
        run_state.last_structure_check_batch_id = batch_id
        run_state.last_structure_check_passed = structure_check_passed
        run_state.recommended_action = "review_structure_loss" if requires_loss_approval else "resume_pending_review"
        run_state.last_recovery_batch_id = batch_id
        context.state_store.save_run_state(run_state)
        return {
            "batch_record_status": BatchStatus.REVIEW_REQUIRED,
            "structure_check_passed": structure_check_passed,
            "requires_loss_approval": requires_loss_approval,
            "missing_paths": missing_paths,
            "actors_missing_paths": actors_missing_paths,
            "worldinfo_missing_paths": worldinfo_missing_paths,
        }

    return node


def _handle_failure(context: PipelineWorkflowContext):
    def node(state: GraphState) -> GraphState:
        batch = state.get("batch")
        if batch is not None:
            resolved_batch = _require_batch(state)
            batch_id = resolved_batch.batch_id
            if state.get("prompt_text") is not None:
                context.batch_store.write_text_artifact(batch_id, "prompt.txt", state.get("prompt_text") or "")
            if state.get("llm_raw_output") is not None:
                context.batch_store.write_text_artifact(batch_id, "raw_output.md", state.get("llm_raw_output") or "")
            if state.get("delta_yaml") is not None:
                context.batch_store.write_text_artifact(batch_id, "delta.yaml", state.get("delta_yaml") or "")
            if state.get("actors_merged_preview") is not None:
                context.batch_store.write_text_artifact(batch_id, "merged_actors.preview.yaml", state.get("actors_merged_preview") or "actors: {}\n")
            if state.get("worldinfo_merged_preview") is not None:
                context.batch_store.write_text_artifact(batch_id, "merged_worldinfo.preview.yaml", state.get("worldinfo_merged_preview") or "worldinfo: {}\n")

            record = context.state_store.load_batch_record(batch_id) or BatchRecord(
                batch=resolved_batch,
                status=BatchStatus.FAILED,
            )
            record.status = BatchStatus.FAILED
            record.validation_errors = state.get("validation_errors", [])
            record.retry_count = state.get("retry_count", 0)
            record.structure_check_passed = bool(state.get("structure_check_passed", record.structure_check_passed))
            record.missing_paths = list(state.get("missing_paths", record.missing_paths))
            record.actors_missing_paths = list(state.get("actors_missing_paths", record.actors_missing_paths))
            record.worldinfo_missing_paths = list(state.get("worldinfo_missing_paths", record.worldinfo_missing_paths))
            record.requires_loss_approval = bool(state.get("requires_loss_approval", record.requires_loss_approval))
            record.last_failure = FailureInfo(
                stage=state.get("failure_stage") or "unknown",
                message=state.get("error_message"),
                errors=state.get("validation_errors", []),
                retryable=bool(state.get("failure_retryable", False)),
                suggested_action=state.get("suggested_action"),
            )
            context.state_store.save_batch_record(record)
        else:
            batch_id = state.get("batch_id")

        run_state = context.state_store.load_run_state()
        run_state.status = RunStatus.FAILED
        run_state.last_generated_batch_id = batch_id
        run_state.pending_review_batch_id = None
        run_state.pending_loss_review_batch_id = None
        run_state.last_failed_batch_id = batch_id
        run_state.last_failed_stage = state.get("failure_stage")
        run_state.last_failure_reason = state.get("error_message")
        run_state.last_failure_retryable = state.get("failure_retryable")
        if batch_id is not None:
            run_state.last_structure_check_batch_id = batch_id
            run_state.last_structure_check_passed = state.get("structure_check_passed")
        run_state.recommended_action = state.get("suggested_action") or "retry_batch"
        run_state.last_recovery_batch_id = batch_id
        context.state_store.save_run_state(run_state)
        context.state_store.append_checkpoint(
            "batch_failed",
            {
                "batch_id": batch_id,
                "stage": state.get("failure_stage"),
                "errors": state.get("validation_errors", []),
                "retryable": state.get("failure_retryable"),
                "suggested_action": state.get("suggested_action"),
                "retry_count": state.get("retry_count", 0),
                "structure_check_passed": state.get("structure_check_passed"),
                "requires_loss_approval": state.get("requires_loss_approval"),
                "missing_paths_count": len(state.get("missing_paths", [])),
            },
        )
        return {
            "batch_record_status": BatchStatus.FAILED,
            "failure_stage": state.get("failure_stage"),
            "failure_retryable": state.get("failure_retryable"),
            "suggested_action": state.get("suggested_action"),
        }

    return node


def _route_after_load_run_state(state: GraphState) -> str:
    return "finish" if state.get("next_action") == "finish" else "continue"


def _route_after_step(state: GraphState) -> str:
    return "failed" if state.get("validation_errors") else "ok"


def _require_batch(state: GraphState) -> ChapterBatch:
    batch = state.get("batch")
    if batch is None:
        raise ValueError("工作流状态中缺少 batch 信息")
    if isinstance(batch, ChapterBatch):
        return batch
    return ChapterBatch.model_validate(batch)

