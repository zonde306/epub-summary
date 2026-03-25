from __future__ import annotations

from enum import StrEnum


class BatchStatus(StrEnum):
    PENDING = "pending"
    GENERATED_DELTA = "generated_delta"
    MERGED_PREVIEW_READY = "merged_preview_ready"
    REVIEW_REQUIRED = "review_required"
    ACCEPTED = "accepted"
    EDITED = "edited"
    REJECTED = "rejected"
    FAILED = "failed"


class RunStatus(StrEnum):
    INITIALIZED = "initialized"
    RUNNING = "running"
    REVIEW_REQUIRED = "review_required"
    COMPLETED = "completed"
    FAILED = "failed"


class ReviewAction(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    EDIT = "edit"
