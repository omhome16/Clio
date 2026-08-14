# src/clio/orchestrator.py
"""The orchestrator: phase machine that drives the whole analysis pipeline."""
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from clio.clone import clone_repo
from clio.clustering import cluster_by_package
from clio.config import Limits, get_limits
from clio.events import (
    EVENT_JOB_CLONED, EVENT_JOB_CLONING, EVENT_JOB_CREATED,
    EVENT_JOB_FAILED, EVENT_JOB_GRAPHED, EVENT_JOB_GUIDING, EVENT_JOB_INDEXING,
    EVENT_JOB_PERSISTED, Event, EventBus,
)
from clio.graph import RepoGraph, build_repo_graph
from clio.guide import build_guide
from clio.job import AnalysisJob, jobs_dir, new_job, record_clone, update_status
from clio.llm import LLMClient
from clio.sandbox import Sandbox
from clio.store import GraphStore


@dataclass
class AnalysisReport:
    job_id: str
    repo_url: str
    commit_sha: str
    summary: str
    created_at: str
    graph: dict | None = None
    guide: dict | None = None

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
            graph = build_repo_graph(self._sandbox.workspace(job.job_id))
            graph_stats = {
                "modules": graph.module_count,
                "symbols": graph.symbol_count,
                "calls": graph.call_count,
                "clusters": len(cluster_by_package(graph)),
                "languages": graph.language_stats(),
            }
            jobs_dir(root).mkdir(parents=True, exist_ok=True)
            GraphStore(jobs_dir(root) / f"{job.job_id}.graph.db").save(graph)
            self._emit(EVENT_JOB_GRAPHED, job.job_id, graph_stats)

            update_status(job, "GUIDING", root)
            self._emit(EVENT_JOB_GUIDING, job.job_id, {})
            workspace = self._sandbox.workspace(job.job_id)
            guide = await build_guide(
                workspace, graph, self._client,
                job_id=job.job_id, bus=self._bus, limits=self._limits,
            )
            summary = guide["stages"]["what"]["text"] or "no README and no summary"

            report = AnalysisReport(
                job_id=job.job_id,
                repo_url=url,
                commit_sha=clone.commit_sha,
                summary=summary,
                created_at=datetime.now(UTC).isoformat(),
                graph=graph_stats,
                guide=guide,
            )
            jobs_dir(root).mkdir(parents=True, exist_ok=True)
            (jobs_dir(root) / f"{job.job_id}.guide.json").write_text(
                json.dumps(guide, indent=2), encoding="utf-8"
            )
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