# tests/test_orchestrator.py
import json

import pytest

from clio.config import Limits
from clio.events import (
    EVENT_JOB_CLONED, EVENT_JOB_FAILED, EVENT_JOB_GRAPHED, EVENT_JOB_PERSISTED,
    EVENT_SUBAGENT_DONE, EVENT_SUBAGENT_START, Event, EventBus,
)
from clio.job import load_job
from clio.llm import LLMMessage, MockLLM
from clio.orchestrator import AnalysisReport, Orchestrator
from clio.sandbox import Sandbox


def _mock_handler(limits):
    def handler(messages, model):
        if model == limits.frontier_model:
            return json.dumps({"final": '{"summary": "merged", "modules": ["core"]}'})
        if len(messages) < 3:
            return json.dumps({"tool": "list_tree", "args": {}})
        return json.dumps({"final": '{"findings": ["nothing"]}'})
    return handler


async def test_full_pipeline(tmp_path, local_repo):
    limits = Limits(workspace_root=tmp_path / "sandbox", max_agent_steps=5)
    sandbox = Sandbox(root=tmp_path / "sandbox", limits=limits)
    client = MockLLM(handler=_mock_handler(limits))
    bus = EventBus()
    seen = []
    bus.subscribe(seen.append)
    orch = Orchestrator(sandbox, client, bus=bus, limits=limits)
    report = await orch.run(local_repo.as_uri(), root=tmp_path, job_id="clio-test")
    assert report.repo_url == local_repo.as_uri()
    assert len(report.commit_sha) == 12
    assert set(report.aspects) == {"structure", "dependencies", "risks", "entrypoints"}
    assert all(a["ok"] for a in report.aspects.values())
    assert report.summary == "merged"
    assert load_job("clio-test", tmp_path).status == "PERSISTED"
    report_file = (tmp_path / "jobs" / "clio-test.report.json")
    assert report_file.is_file()
    types = [e.type for e in seen]
    assert EVENT_JOB_CLONED in types and EVENT_JOB_PERSISTED in types
    assert types.count(EVENT_SUBAGENT_START) == 4
    assert types.count(EVENT_SUBAGENT_DONE) == 4


async def test_failed_clone_marks_job_failed(tmp_path):
    limits = Limits(workspace_root=tmp_path / "sandbox")
    sandbox = Sandbox(root=tmp_path / "sandbox", limits=limits)
    client = MockLLM(handler=_mock_handler(limits))
    bus = EventBus()
    seen = []
    bus.subscribe(seen.append)
    orch = Orchestrator(sandbox, client, bus=bus, limits=limits)
    with pytest.raises(Exception):
        await orch.run("https://github.com/omhome16/does-not-exist-xyz.git", root=tmp_path, job_id="clio-fail")
    job = load_job("clio-fail", tmp_path)
    assert job.status == "FAILED"
    assert any(e.type == EVENT_JOB_FAILED for e in seen)


def test_report_roundtrip():
    report = AnalysisReport(
        job_id="clio-1", repo_url="https://github.com/x/y.git", commit_sha="abc",
        aspects={"a": {"ok": True, "content": "z"}}, summary="s",
        created_at="2026-08-10T00:00:00+00:00",
    )
    restored = AnalysisReport.from_dict(report.to_dict())
    assert restored == report


async def test_pipeline_builds_graph(tmp_path, local_repo):
    limits = Limits(workspace_root=tmp_path / "sandbox", max_agent_steps=5)
    sandbox = Sandbox(root=tmp_path / "sandbox", limits=limits)
    client = MockLLM(handler=_mock_handler(limits))
    bus = EventBus()
    seen = []
    bus.subscribe(seen.append)
    orch = Orchestrator(sandbox, client, bus=bus, limits=limits)
    report = await orch.run(local_repo.as_uri(), root=tmp_path, job_id="clio-graph")
    assert report.graph is not None
    assert report.graph["modules"] >= 3
    assert report.graph["symbols"] >= 2
    assert report.graph["clusters"] >= 1
    assert (tmp_path / "jobs" / "clio-graph.graph.db").is_file()
    types = [e.type for e in seen]
    assert types.count(EVENT_JOB_GRAPHED) == 1


def test_report_roundtrip_with_graph():
    report = AnalysisReport(
        job_id="clio-1", repo_url="https://github.com/x/y.git", commit_sha="abc",
        aspects={"a": {"ok": True, "content": "z"}}, summary="s",
        created_at="2026-08-10T00:00:00+00:00",
        graph={"modules": 3, "symbols": 2, "calls": 1, "clusters": 2},
    )
    restored = AnalysisReport.from_dict(report.to_dict())
    assert restored == report
