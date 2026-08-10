# src/clio/reports.py
"""Queryable archive over persisted job artifacts (reports + graph dbs)."""
from __future__ import annotations

import json
from pathlib import Path

from clio.graph import RepoGraph
from clio.store import GraphStore


class ReportArchive:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _jobs_dir(self) -> Path:
        return self.root / "jobs"

    def _report_path(self, job_id: str) -> Path:
        return self._jobs_dir() / f"{job_id}.report.json"

    def _graph_path(self, job_id: str) -> Path:
        return self._jobs_dir() / f"{job_id}.graph.db"

    def list_reports(self) -> list[dict]:
        reports: list[dict] = []
        if not self._jobs_dir().is_dir():
            return reports
        for path in sorted(self._jobs_dir().glob("*.report.json")):
            try:
                reports.append(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
        return reports

    def get_report(self, job_id: str) -> dict | None:
        path = self._report_path(job_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def latest(self) -> dict | None:
        reports = [r for r in self.list_reports() if r.get("created_at")]
        if not reports:
            return None
        return max(reports, key=lambda r: r["created_at"])

    def get_graph(self, job_id: str) -> RepoGraph | None:
        if not self._graph_path(job_id).is_file():
            return None
        try:
            return GraphStore(self._graph_path(job_id)).load()
        except Exception:
            return None

    def graph_store(self, job_id: str) -> GraphStore:
        return GraphStore(self._graph_path(job_id))
