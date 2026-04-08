from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ProgressStage(Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    SEPARATING = "separating"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    FAILED = "failed"


STAGE_PROGRESS: dict[ProgressStage, int] = {
    ProgressStage.QUEUED: 0,
    ProgressStage.DOWNLOADING: 10,
    ProgressStage.SEPARATING: 20,
    ProgressStage.UPLOADING: 90,
    ProgressStage.COMPLETED: 100,
    ProgressStage.FAILED: 0,
}

STAGE_MESSAGES: dict[ProgressStage, str] = {
    ProgressStage.QUEUED: "Job queued for processing",
    ProgressStage.DOWNLOADING: "Downloading audio file",
    ProgressStage.SEPARATING: "Separating audio tracks",
    ProgressStage.UPLOADING: "Uploading separated tracks",
    ProgressStage.COMPLETED: "Processing complete",
    ProgressStage.FAILED: "Processing failed",
}


@dataclass
class ProgressEvent:
    track_id: str
    stage: str
    progress: int
    message: str
    timestamp: float = field(default_factory=time.time)
    error: str | None = None
    result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "stage": self.stage,
            "progress": self.progress,
            "message": self.message,
            "timestamp": self.timestamp,
            "error": self.error,
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProgressEvent":
        return cls(
            track_id=data["track_id"],
            stage=data["stage"],
            progress=data["progress"],
            message=data["message"],
            timestamp=data.get("timestamp", time.time()),
            error=data.get("error"),
            result=data.get("result"),
        )

    def to_sse(self) -> str:
        data = json.dumps(self.to_dict())
        return f"event: progress\ndata: {data}\n\n"

    @classmethod
    def create(
        cls,
        track_id: str,
        stage: ProgressStage,
        message: str | None = None,
        progress: int | None = None,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> "ProgressEvent":
        return cls(
            track_id=track_id,
            stage=stage.value,
            progress=progress if progress is not None else STAGE_PROGRESS[stage],
            message=message or STAGE_MESSAGES[stage],
            error=error,
            result=result,
        )
