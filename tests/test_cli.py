# tests/test_cli.py
import json
import sys

import pytest

from clio.cli import amain, build_parser


def test_parser_defaults():
    args = build_parser().parse_args(["https://github.com/x/y.git"])
    assert args.url == "https://github.com/x/y.git"
    assert args.provider == "mock"


def test_parser_invalid_provider_rejected():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["https://github.com/x/y.git", "--provider", "nope"])


async def test_cli_end_to_end_mock(tmp_path, local_repo, monkeypatch, capsys):
    monkeypatch.setenv("CLIO_WORKSPACE_ROOT", str(tmp_path / "sandbox"))
    args = build_parser().parse_args([local_repo.as_uri()])
    assert await amain(args) == 0
    out = capsys.readouterr().out
    assert "job.cloned" in out
    assert "job.graphed" in out
    assert "REPORT:" in out
    payload = out.split("REPORT:", 1)[1]
    report = json.loads(payload)
    assert report["summary"] == "merged"
    assert report["graph"]["modules"] >= 3
    assert (tmp_path / "sandbox" / "jobs").is_dir()


async def test_cli_impact_e2e(tmp_path, local_repo, monkeypatch, capsys):
    monkeypatch.setenv("CLIO_WORKSPACE_ROOT", str(tmp_path / "sandbox"))
    args = build_parser().parse_args([local_repo.as_uri(), "--impact", "app.service::greet"])
    assert await amain(args) == 0
    out = capsys.readouterr().out
    assert "job.graphed" in out
    assert "IMPACT:" in out
    payload = out.split("IMPACT:", 1)[1]
    impact = json.loads(payload)
    assert impact["verdict"] == "contained"
    assert "app.main" in impact["affected_modules"]


async def test_cli_impact_missing_symbol(tmp_path, local_repo, monkeypatch, capsys):
    monkeypatch.setenv("CLIO_WORKSPACE_ROOT", str(tmp_path / "sandbox"))
    args = build_parser().parse_args([local_repo.as_uri(), "--impact", "app::ghost"])
    assert await amain(args) == 0
    out = capsys.readouterr().out
    assert '"verdict": "missing"' in out


def test_parser_accepts_groq():
    args = build_parser().parse_args(["https://github.com/x/y.git", "--provider", "groq"])
    assert args.provider == "groq"


def test_parser_default_provider_from_env(monkeypatch):
    monkeypatch.setenv("CLIO_PROVIDER", "groq")
    args = build_parser().parse_args(["https://github.com/x/y.git"])
    assert args.provider == "groq"
