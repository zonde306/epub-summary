from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class Chapter(BaseModel):
    index: int
    title: str
    source_href: str | None = None
    content_text: str
    content_hash: str
    estimated_tokens: int


class ChapterBatch(BaseModel):
    batch_id: str
    start_chapter_index: int
    end_chapter_index: int
    chapter_indices: list[int]
    combined_text: str
    combined_hash: str
    estimated_input_tokens: int


class DocumentVersion(BaseModel):
    doc_type: str
    version: int
    batch_id: str
    chapter_start: int
    chapter_end: int
    file_path: str
    content_hash: str
    status: str
    delta_path: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None


class DeltaPackage(BaseModel):
    actors: dict[str, Any] | None = None
    worldinfo: dict[str, Any] | None = None


class ReviewDecision(BaseModel):
    batch_id: str
    decision: str
    reviewer: str | None = None
    comment: str | None = None
    edited_actors_path: str | None = None
    edited_worldinfo_path: str | None = None
    reviewed_at: datetime


class FailureInfo(BaseModel):
    stage: str
    message: str | None = None
    errors: list[str] = Field(default_factory=list)
    retryable: bool = True
    suggested_action: str | None = None
    failed_at: datetime = Field(default_factory=datetime.utcnow)


class RunState(BaseModel):
    book_id: str
    source_file: str
    source_hash: str
    total_chapters: int
    next_chapter_index: int = 0
    last_accepted_batch_id: str | None = None
    last_generated_batch_id: str | None = None
    pending_review_batch_id: str | None = None
    pending_loss_review_batch_id: str | None = None
    last_failed_batch_id: str | None = None
    last_failed_stage: str | None = None
    last_failure_reason: str | None = None
    last_failure_retryable: bool | None = None
    last_structure_check_batch_id: str | None = None
    last_structure_check_passed: bool | None = None
    recommended_action: str | None = None
    last_recovery_action: str | None = None
    last_recovery_batch_id: str | None = None
    current_actors_version: int = 0
    current_worldinfo_version: int = 0
    status: str = "initialized"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    target_input_tokens: int = 12000
    max_input_tokens: int = 16000
    min_chapters_per_batch: int = 1
    max_chapters_per_batch: int = 8
    reserved_output_tokens: int = 4000
    reserved_system_tokens: int = 1500


class BatchRecord(BaseModel):
    batch: ChapterBatch
    status: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    validation_errors: list[str] = Field(default_factory=list)
    review_decision: ReviewDecision | None = None
    retry_count: int = 0
    last_failure: FailureInfo | None = None
    structure_check_passed: bool = True
    missing_paths: list[str] = Field(default_factory=list)
    actors_missing_paths: list[str] = Field(default_factory=list)
    worldinfo_missing_paths: list[str] = Field(default_factory=list)
    requires_loss_approval: bool = False
    loss_approval_status: str | None = None
    loss_approval_comment: str | None = None


class RecoveryDecision(BaseModel):
    action: str
    batch_id: str | None = None
    reason: str | None = None
    retryable: bool | None = None
    target_stage: str | None = None
    run_status: str | None = None
    next_chapter_index: int | None = None
    total_chapters: int | None = None
    batch_status: str | None = None
    review_kind: str | None = None


class PipelineState(BaseModel):
    book_id: str
    run_id: str | None = None
    batch_id: str | None = None
    batch: ChapterBatch | None = None
    actors_current: str = "actors: {}\n"
    worldinfo_current: str = "worldinfo: {}\n"
    prompt_text: str | None = None
    llm_raw_output: str | None = None
    delta_yaml: str | None = None
    actors_delta: dict[str, Any] | None = None
    worldinfo_delta: dict[str, Any] | None = None
    actors_merged_preview: str | None = None
    worldinfo_merged_preview: str | None = None
    validation_errors: list[str] = Field(default_factory=list)
    review_decision: str | None = None
    edited_actors: str | None = None
    edited_worldinfo: str | None = None
    next_action: str | None = None
    error_message: str | None = None
    batch_record_status: str | None = None
    failure_stage: str | None = None
    failure_retryable: bool | None = None
    suggested_action: str | None = None
    retry_count: int = 0
    structure_check_passed: bool = True
    requires_loss_approval: bool = False
    missing_paths: list[str] = Field(default_factory=list)
    actors_missing_paths: list[str] = Field(default_factory=list)
    worldinfo_missing_paths: list[str] = Field(default_factory=list)
