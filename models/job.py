from dataclasses import dataclass
from enum import Enum
from typing import Any


class JobStatus(Enum):
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ProcessingJob:
    track_id: str
    status: JobStatus
    progress: int = 0
    created_at: float = 0
    error: str | None = None
    result: dict[str, Any] | None = None
    completed_at: float | None = None
