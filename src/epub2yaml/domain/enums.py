from __future__ import annotations

from enum import Enum


class StringEnum(str, Enum):
    pass


class BatchStatus(StringEnum):
    PENDING = "pending"
    GENERATED_DELTA = "generated_delta"
    MERGED_PREVIEW_READY = "merged_preview_ready"
    REVIEW_REQUIRED = "review_required"
    MANUAL_EDIT_REQUESTED = "manual_edit_requested"
    CANCELLED_FOR_MANUAL_EDIT = "cancelled_for_manual_edit"
    AWAITING_MANUAL_EDIT_RESUME = "awaiting_manual_edit_resume"
    ACCEPTED = "accepted"
    EDITED = "edited"
    REJECTED = "rejected"
    FAILED = "failed"


class RunStatus(StringEnum):
    INITIALIZED = "initialized"
    RUNNING = "running"
    PAUSED = "paused"
    REVIEW_REQUIRED = "review_required"
    AWAITING_MANUAL_EDIT = "awaiting_manual_edit"
    COMPLETED = "completed"
    FAILED = "failed"


class ReviewAction(StringEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    EDIT = "edit"


class ControlAction(StringEnum):
    PAUSE = "pause"
    PREPARE_MANUAL_EDIT = "prepare_manual_edit"
    RESUME = "resume"
    OPEN_MANUAL_EDIT_WORKSPACE = "open_manual_edit_workspace"


class ManualEditSessionStatus(StringEnum):
    ACTIVE = "active"
    APPLIED = "applied"
    CANCELLED = "cancelled"
