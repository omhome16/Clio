# src/clio/orchestrator.py
"""The orchestrator: phase machine that drives the whole analysis pipeline."""
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from clio.clone import CloneError, clone_repo
from clio.config import Limits, get_limits
from clio.events import (
    EVENT_JOB_ANALYZING, EVENT_JOB_CLONED, EVENT_JOB_CLONING, EVENT_JOB_CREATED,
    EVENT_JOB_FAILED, EVENT_JOB_INDEXING, EVENT_JOB_PERSISTED, EVENT_JOB_SYNTHESIZING,
    EVENT_SUBAGENT_DONE, Event, EventBus,
)
from clio.job import AnalysisJob, jobs_dir, new_job, record_clone, update_status
from clio.llm import LLMClient
from clio.sandbox import Sandbox
from clio.scheduler import fan_out
from clio.subagent import Subagent, SubagentReport, SubagentSpec
from clio.tools import ToolRegistry

ASPECT_TASK = (
    "Analyze the repository {repo} (commit {commit}) for the aspect: {aspect}.\n"
    "Use the available tools to inspect the workspace. "
    "Reply with a final JSON object containing your findings."
)


def make_aspect_specs() -> tuple[SubagentSpec, ...]:
    return (
        SubagentSpec(
            name="structure",
            role="files and layout",
            system_prompt=(
                "You map a repository's file layout: top-level organization, "
                "package boundaries, and what each major directory contains."
            ),
            tools=("list_tree", "read_file"),
        ),
        SubagentSpec(
            name="dependencies",
            role="import and dependency relationships",
            system_prompt=(
                "You trace how modules depend on each other: imports, shared "
                "components, and coupling hotspots."
            ),
            tools=("grep", "read_file", "list_tree"),
        ),
        SubagentSpec(
            name="risks",
            role="quality risks and failure points",
            system_prompt=(
                "You find quality risks: dead code, swallowed exceptions, "
                "missing tests, hardcoded secrets, and fragile patterns."
            ),
            tools=("grep", "read_file"),
        ),
        SubagentSpec(
            name="entrypoints",
            role="entry points and run flow",
            system_prompt=(
                "You identify entry points (main functions, CLI, scripts, "
                "servers) and trace the main execution flow."
            ),
            tools=("list_tree", "read_file", "git_log"),
        ),
    )


SYNTH_SPEC = SubagentSpec(
    name="synthesizer",
    role="merge aspect findings into an architecture summary",
    system_prompt=(
        "You are the synthesis stage. Merge per-aspect findings into a final "
        "architecture summary. Reply with a JSON object: "
        '{"summary": "...", "modules": ["..."]}.'
    ),
    tools=(),
)

SYNTH_TASK = (
    "Per-aspect findings for {repo}:\n{findings}\n"
    'Produce the final summary JSON: {{"summary": "...", "modules": ["..."]}}.'
)


@dataclass
class AnalysisReport:
    job_id: str
    repo_url: str
    commit_sha: str
    aspects: dict[str, dict]
    summary: str
    created_at: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AnalysisReport":
        return cls(**data)


class Orchestrator:
    def __init__(
        self,
        sandbox: Sandbox,
        client: LLMClient,
        *,
        bus: EventBus | None = None,
        limits: Limits | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._client = client
        self._bus = bus
        self._limits = limits or get_limits()

    def _emit(self, event_type: str, job_id: str, data: dict) -> None:
        if self._bus is not None:
            self._bus.publish(Event(type=event_type, job_id=job_id, data=data))

    async def run(
        self,
        url: str,
        root: Path,
        *,
        job_id: str | None = None,
    ) -> AnalysisReport:
        job = new_job(url, job_id=job_id)
        self._emit(EVENT_JOB_CREATED, job.job_id, {"url": url})
        try:
            update_status(job, "CLONING", root)
            self._emit(EVENT_JOB_CLONING, job.job_id, {})
            clone = clone_repo(url, self._sandbox, job.job_id)
            self._emit(EVENT_JOB_CLONED, job.job_id, {"commit_sha": clone.commit_sha})
            record_clone(job, clone, root)
            self._emit(EVENT_JOB_INDEXING, job.job_id, {})

            update_status(job, "ANALYZING", root)
            self._emit(EVENT_JOB_ANALYZING, job.job_id, {})
            registry = ToolRegistry(self._sandbox, job.job_id, limits=self._limits)
            specs = make_aspect_specs()
            subs = {
                spec.name: Subagent(
                    spec, self._client, registry,
                    bus=self._bus, job_id=job.job_id,
                    model=self._limits.cheap_model, max_steps=self._limits.max_agent_steps,
                )
                for spec in specs
            }
            task = ASPECT_TASK.format(repo=url, commit=clone.commit_sha, aspect="{aspect}")
            outcomes = await fan_out(
                list(specs),
                lambda spec: subs[spec.name].run(task.format(aspect=spec.role)),
                max_concurrency=self._limits.max_concurrency,
            )
            aspects: dict[str, dict] = {}
            for spec in specs:
                outcome = outcomes[spec]
                if isinstance(outcome, BaseException):
                    aspects[spec.name] = {"ok": False, "error": repr(outcome), "content": ""}
                    self._emit(EVENT_SUBAGENT_DONE, job.job_id, {"name": spec.name, "ok": False})
                else:
                    aspects[spec.name] = outcome.to_dict()

            update_status(job, "SYNTHESIZING", root)
            self._emit(EVENT_JOB_SYNTHESIZING, job.job_id, {})
            synth = Subagent(
                SYNTH_SPEC, self._client, registry,
                job_id=job.job_id,
                model=self._limits.frontier_model, max_steps=self._limits.max_agent_steps,
            )
            synth_report = await synth.run(
                SYNTH_TASK.format(repo=url, findings=json.dumps(aspects, indent=2))
            )
            try:
                _synth = json.loads(synth_report.content)
                summary = _synth.get("summary", synth_report.content) if isinstance(_synth, dict) else synth_report.content
            except json.JSONDecodeError:
                summary = synth_report.content
            report = AnalysisReport(
                job_id=job.job_id,
                repo_url=url,
                commit_sha=clone.commit_sha,
                aspects=aspects,
                summary=summary,
                created_at=datetime.now(UTC).isoformat(),
            )
            jobs_dir(root).mkdir(parents=True, exist_ok=True)
            (jobs_dir(root) / f"{job.job_id}.report.json").write_text(
                json.dumps(report.to_dict(), indent=2), encoding="utf-8"
            )
            update_status(job, "PERSISTED", root)
            self._emit(EVENT_JOB_PERSISTED, job.job_id, {"report": f"{job.job_id}.report.json"})
            return report
        except Exception as exc:
            try:
                update_status(job, "FAILED", root)
            except Exception:
                pass
            self._emit(EVENT_JOB_FAILED, job.job_id, {"error": str(exc)})
            raise
