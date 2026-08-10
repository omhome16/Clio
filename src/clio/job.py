# src/clio/job.py
"""Persistent job records: the checkpointing backbone for later phases."""
import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from clio.clone import CloneResult

JOB_STATUSES = (
    "QUEUED", "CLONING", "INDEXING", "ANALYZING",
    "SYNTHESIZING", "GRAPHING", "PERSISTED", "FAILED",
)


@dataclass
class AnalysisJob:
    job_id: str
    url: str
    status: str = "QUEUED"
    workspace: Path | None = None
    commit_sha: str = ""
    created_at: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        if self.workspace is not None:
            data["workspace"] = str(self.workspace)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "AnalysisJob":
        ws = Path(data["workspace"]) if data.get("workspace") else None
        return cls(
            job_id=data["job_id"],
            url=data["url"],
            status=data.get("status", "QUEUED"),
            workspace=ws,
            commit_sha=data.get("commit_sha", ""),
            created_at=data.get("created_at", ""),
        )


def new_job(url: str, *, job_id: str | None = None, now: str | None = None) -> AnalysisJob:
    return AnalysisJob(
        job_id=job_id or f"clio-{secrets.token_hex(4)}",
        url=url,
        status="QUEUED",
        created_at=now or datetime.now(UTC).isoformat(),
    )


def jobs_dir(root: Path) -> Path:
    return Path(root) / "jobs"


def save_job(job: AnalysisJob, root: Path) -> None:
    jd = jobs_dir(root)
    jd.mkdir(parents=True, exist_ok=True)
    (jd / f"{job.job_id}.json").write_text(
        json.dumps(job.to_dict(), indent=2), encoding="utf-8"
    )


def load_job(job_id: str, root: Path) -> AnalysisJob | None:
    path = jobs_dir(root) / f"{job_id}.json"
    if not path.is_file():
        return None
    try:
        return AnalysisJob.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def update_status(job: AnalysisJob, status: str, root: Path) -> AnalysisJob:
    if status not in JOB_STATUSES:
        raise ValueError(f"unknown job status '{status}'")
    job.status = status
    save_job(job, root)
    return job


def record_clone(job: AnalysisJob, result: CloneResult, root: Path) -> AnalysisJob:
    job.workspace = result.repo_path
    job.commit_sha = result.commit_sha
    job.status = "INDEXING"
    save_job(job, root)
    return job
