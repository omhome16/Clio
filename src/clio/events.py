# src/clio/events.py
"""Typed event bus and SSE serialization — the visibility layer."""
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Callable

EVENT_JOB_CREATED = "job.created"
EVENT_JOB_CLONING = "job.cloning"
EVENT_JOB_CLONED = "job.cloned"
EVENT_JOB_INDEXING = "job.indexing"
EVENT_JOB_GRAPHED = "job.graphed"
EVENT_JOB_GUIDING = "job.guiding"
EVENT_JOB_STAGE = "job.stage"
EVENT_JOB_ANALYZING = "job.analyzing"
EVENT_SUBAGENT_START = "subagent.start"
EVENT_SUBAGENT_TOOL = "subagent.tool"
EVENT_SUBAGENT_DONE = "subagent.done"
EVENT_JOB_SYNTHESIZING = "job.synthesizing"
EVENT_JOB_PERSISTED = "job.persisted"
EVENT_JOB_FAILED = "job.failed"
EVENT_LOG = "log"
EVENT_ASK_TOOL = "ask.tool"
EVENT_ASK_FINAL = "ask.final"


@dataclass(frozen=True)
class Event:
    type: str
    job_id: str
    data: dict = field(default_factory=dict)
    ts: str = ""

    def __post_init__(self) -> None:
        if not self.ts:
            object.__setattr__(self, "ts", datetime.now(UTC).isoformat())


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[Callable[[Event], None]] = []

    def subscribe(self, fn: Callable[[Event], None]) -> None:
        self._subscribers.append(fn)

    def publish(self, event: Event) -> None:
        for fn in self._subscribers:
            fn(event)

    def subscribers(self) -> list[Callable[[Event], None]]:
        return list(self._subscribers)


class SseFormatter:
    @staticmethod
    def format(event: Event) -> str:
        payload = json.dumps(
            {"type": event.type, "job_id": event.job_id, "data": event.data, "ts": event.ts}
        )
        return f"data: {payload}\n\n"
