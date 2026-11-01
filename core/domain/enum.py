from enum import StrEnum


class BatchStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    ACTIVATION_FAILED = "activation_failed"
    ROLLED_BACK = "rolled_back"


class RowStatus(StrEnum):
    PENDING = "pending"
    CREATED = "created"
    FAILED = "failed"
    CREATED_AND_ACTIVATED = "created_and_activated"
    ROLLED_BACK = "rolled_back"


class RunTrigger(StrEnum):
    INITIAL = "initial"
    RESUME = "resume"
    CORRECTION = "correction"


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ACTIVATION_FAILED = "activation_failed"
    INTERRUPTED = "interrupted"
