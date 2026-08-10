# src/clio/subagent.py
"""A subagent: isolated context window + tool loop + context budget."""
from dataclasses import asdict, dataclass

from clio.config import Limits, get_limits
from clio.events import (
    EVENT_SUBAGENT_DONE, EVENT_SUBAGENT_START, EVENT_SUBAGENT_TOOL, Event, EventBus,
)
from clio.llm import LLMClient, LLMMessage, parse_reply


@dataclass(frozen=True)
class SubagentSpec:
    name: str
    role: str
    system_prompt: str
    tools: tuple[str, ...]


@dataclass
class SubagentReport:
    name: str
    content: str
    steps: int
    tool_calls: int
    ok: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SubagentReport":
        return cls(**data)


class Subagent:
    def __init__(
        self,
        spec: SubagentSpec,
        client: LLMClient,
        registry,
        *,
        bus: EventBus | None = None,
        job_id: str = "",
        model: str | None = None,
        max_steps: int | None = None,
        max_context_chars: int | None = None,
    ) -> None:
        self.spec = spec
        self._client = client
        self._registry = registry
        self._bus = bus
        self._job_id = job_id
        self._model = model
        limits = get_limits()
        self._max_steps = max_steps if max_steps is not None else limits.max_agent_steps
        self._max_context_chars = (
            max_context_chars if max_context_chars is not None else limits.subagent_max_context_chars
        )

    def _emit(self, event_type: str, data: dict) -> None:
        if self._bus is not None:
            self._bus.publish(Event(type=event_type, job_id=self._job_id, data=data))

    def _compact(self, messages: list[LLMMessage]) -> None:
        if sum(len(m.content) for m in messages) <= self._max_context_chars:
            return
        head = messages[:2]
        tail = messages[-4:] if len(messages) > 4 else messages[2:]
        note = LLMMessage(role="tool", content="...(earlier context dropped to fit budget)")
        messages[:] = head + tail + [note]
        if len(head[1].content) > self._max_context_chars // 2:
            head[1] = LLMMessage(
                role=head[1].role,
                content=head[1].content[: self._max_context_chars // 2]
                + "...(truncated to fit budget)",
            )
            messages[:] = [head[0], head[1]] + tail + [note]

    async def run(self, task: str) -> SubagentReport:
        messages = [
            LLMMessage(role="system", content=self.spec.system_prompt),
            LLMMessage(role="user", content=task),
        ]
        self._emit(EVENT_SUBAGENT_START, {"name": self.spec.name, "role": self.spec.role})
        steps = 0
        tool_calls = 0
        content = ""
        ok = True
        while steps < self._max_steps:
            steps += 1
            self._compact(messages)
            text = await self._client.complete(messages, model=self._model)
            reply = parse_reply(text)
            if reply.kind == "tool":
                tool_calls += 1
                self._emit(EVENT_SUBAGENT_TOOL, {"name": self.spec.name, "tool": reply.tool.tool, "args": reply.tool.args})
                result = await self._registry.execute(reply.tool.tool, reply.tool.args)
                message = (
                    f"tool result (ok={result.ok}):\n{result.content}"
                    if result.ok
                    else f"tool error: {result.error}"
                )
                messages.append(LLMMessage(role="assistant", content=text))
                messages.append(LLMMessage(role="tool", content=message))
                continue
            if reply.kind == "final":
                content = reply.final or ""
                break
            ok = False
            content = text or "(unparseable model output)"
            break
        else:
            ok = False
            content = "(max steps reached)"
        report = SubagentReport(
            name=self.spec.name, content=content, steps=steps, tool_calls=tool_calls, ok=ok
        )
        self._emit(EVENT_SUBAGENT_DONE, {"name": self.spec.name, "ok": ok, "steps": steps, "tool_calls": tool_calls})
        return report
