# tests/test_job.py
import json
from pathlib import Path

import pytest

from clio.job import (
    JOB_STATUSES, AnalysisJob, jobs_dir, load_job, new_job,
    record_clone, save_job, update_status,
)
from clio.clone import CloneResult


def test_job_defaults(tmp_path):
    job = new_job("https://github.com/omhome16/Clio.git", now="2026-08-10T00:00:00")
    assert job.status == "QUEUED"
    assert job.job_id.startswith("clio-")
    assert len(job.job_id) == len("clio-") + 8
    assert job.workspace is None


def test_save_and_load_roundtrip(tmp_path):
    job = new_job("https://github.com/omhome16/Clio.git")
    save_job(job, tmp_path)
    loaded = load_job(job.job_id, tmp_path)
    assert loaded == job


def test_load_missing_returns_none(tmp_path):
    assert load_job("clio-00000000", tmp_path) is None


def test_load_corrupt_json_returns_none(tmp_path):
    jd = jobs_dir(tmp_path)
    jd.mkdir(parents=True)
    (jd / "clio-deadbeef.json").write_text("{not json")
    assert load_job("clio-deadbeef", tmp_path) is None


def test_update_status_validates(tmp_path):
    job = new_job("https://github.com/omhome16/Clio.git")
    with pytest.raises(ValueError):
        update_status(job, "BOGUS", tmp_path)


def test_update_status_persists(tmp_path):
    job = new_job("https://github.com/omhome16/Clio.git")
    update_status(job, "CLONING", tmp_path)
    assert load_job(job.job_id, tmp_path).status == "CLONING"


def test_record_clone_sets_workspace_and_sha(tmp_path):
    job = new_job("https://github.com/omhome16/Clio.git")
    result = CloneResult(repo_path=Path("x"), commit_sha="abc123abc123")
    record_clone(job, result, tmp_path)
    assert job.status == "INDEXING"
    assert job.workspace == Path("x")
    assert job.commit_sha == "abc123abc123"
    assert load_job(job.job_id, tmp_path).workspace == Path("x")


def test_statuses_are_stable():
    assert JOB_STATUSES == (
        "QUEUED", "CLONING", "INDEXING", "GRAPHING",
        "GUIDING", "PERSISTED", "FAILED",
    )
