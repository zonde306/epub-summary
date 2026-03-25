from __future__ import annotations

from enum import Enum


class StringEnum(str, Enum):
    pass


class BatchStatus(StringEnum):
    PENDING = "pending"
    GENERATED_DELTA = "generated_delta"
    MERGED_PREVIEW_READY = "merged_preview_ready"
    REVIEW_REQUIRED = "review_required"
    ACCEPTED = "accepted"
    EDITED = "edited"
    REJECTED = "rejected"
    FAILED = "failed"


class RunStatus(StringEnum):
    INITIALIZED = "initialized"
    RUNNING = "running"
    REVIEW_REQUIRED = "review_required"
    COMPLETED = "completed"
    FAILED = "failed"


class ReviewAction(StringEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    EDIT = "edit"
