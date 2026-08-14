# tests/test_guide.py
import pytest

from clio.config import Limits
from clio.events import EVENT_JOB_STAGE, Event, EventBus
from clio.graph import build_repo_graph
from clio.guide import (
    GUIDE_STAGES, build_guide, entrypoint_modules, evidence_blocks,
    lint_citations, load_repo_notes, module_table, readme_head, repo_memory_text,
    run_hints, strip_readme_noise,
)
from clio.llm import LLMMessage


class ScriptedClient:
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[list[LLMMessage]] = []

    async def complete(self, messages, **kwargs):
        self.calls.append(list(messages))
        return self._responses.pop(0)


FILES = {
    "README.md": "# demo\n\nRun me with:\n\n```bash\npython main.py\n```\n",
    "app/__init__.py": "",
    "app/service.py": "def greet(name):\n    return f'hi {name}'\n",
    "app/main.py": "from app.service import greet\n\n\ndef run():\n    print(greet('clio'))\n",
    "Makefile": "test:\n\tpython -m pytest\n",
}


def _setup(tmp_path, write_tree):
    root = write_tree(FILES)
    graph = build_repo_graph(root)
    return root, graph


async def test_guide_builds_all_stages(tmp_path, write_tree):
    root, graph = _setup(tmp_path, write_tree)
    client = ScriptedClient(["what text", "how text", "modules text", "run text"])
    guide = await build_guide(root, graph, client,
                              limits=Limits(workspace_root=tmp_path / "sandbox"))
    assert list(guide["stages"]) == list(GUIDE_STAGES)
    assert guide["stages"]["what"]["text"] == "what text"
    assert guide["stages"]["run"]["text"] == "run text"
    bodies = [c[-1].content for c in client.calls]
    assert len(bodies) == 4
    assert all(b.startswith("Repository evidence:") for b in bodies)
    assert "--- E2: Entry points ---" in bodies[0] and "# demo" in bodies[0]
    assert "--- E1: Repo map ---" in bodies[2]
    assert "Module table" in bodies[2]
    assert "python main.py" in bodies[3] and "make test" in bodies[3]


async def test_stage_events_streamed(tmp_path, write_tree):
    root, graph = _setup(tmp_path, write_tree)
    client = ScriptedClient(["w", "h", "m", "r"])
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append)
    await build_guide(root, graph, client, job_id="job-1", bus=bus,
                      limits=Limits(workspace_root=tmp_path / "sandbox"))
    stages = [e for e in seen if e.type == EVENT_JOB_STAGE]
    assert [(e.data["stage"], e.data["status"]) for e in stages] == [
        ("what", "started"), ("what", "done"),
        ("how", "started"), ("how", "done"),
        ("modules", "started"), ("modules", "done"),
        ("run", "started"), ("run", "done"),
    ]


async def test_guide_falls_back_when_llm_fails(tmp_path, write_tree):
    root, graph = _setup(tmp_path, write_tree)

    class FailingClient:
        async def complete(self, messages, **kwargs):
            raise RuntimeError("boom")

    guide = await build_guide(root, graph, FailingClient(),
                              limits=Limits(workspace_root=tmp_path / "sandbox"))
    assert guide["stages"]["what"]["text"]
    assert guide["stages"]["modules"]["text"]
    assert "app.main" in guide["stages"]["modules"]["text"]


async def test_guide_falls_back_when_llm_empty(tmp_path, write_tree):
    root, graph = _setup(tmp_path, write_tree)
    client = ScriptedClient(["", "  ", "", None])
    guide = await build_guide(root, graph, client,
                              limits=Limits(workspace_root=tmp_path / "sandbox"))
    assert guide["stages"]["what"]["text"]
    assert guide["stages"]["run"]["text"] != ""


def test_readme_head_extracts_readme(tmp_path, write_tree):
    root, graph = _setup(tmp_path, write_tree)
    assert readme_head(root).startswith("# demo")


def test_entrypoint_modules_detected(tmp_path, write_tree):
    root, graph = _setup(tmp_path, write_tree)
    assert "app.main" in entrypoint_modules(graph)


def test_module_table_has_headers_and_rows(tmp_path, write_tree):
    root, graph = _setup(tmp_path, write_tree)
    table = module_table(graph)
    assert "symbols" in table and "app.main" in table and "app.service" in table


def test_run_hints_from_fences_and_makefile(tmp_path, write_tree):
    root, graph = _setup(tmp_path, write_tree)
    hints = run_hints(root)
    assert "python main.py" in hints
    assert any(h.startswith("make ") for h in hints)


def test_run_hints_finds_commands_past_2000_chars(tmp_path):
    readme = tmp_path / "README.md"
    head = "# Repo\n" + ("<img src='https://img.shields.io/badge/x'/>\n" * 80)
    readme.write_text(
        head + "## Run\n\n```bash\nuvicorn backend.main:app --reload --port 8000\n```\n",
        encoding="utf-8",
    )
    hints = run_hints(tmp_path)
    assert any("uvicorn" in h for h in hints)


def test_run_hints_plain_fence_and_git_cd(tmp_path):
    (tmp_path / "README.md").write_text(
        "```\ngit clone https://github.com/x/y.git\ncd y\nsource venv/bin/activate\n"
        "pip install -r requirements.txt\n```\n",
        encoding="utf-8",
    )
    hints = run_hints(tmp_path)
    for cmd in ("git clone", "cd y", "source venv", "pip install"):
        assert any(h.startswith(cmd) for h in hints)


def test_run_hints_nested_package_json(tmp_path):
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text(
        '{"scripts": {"dev": "vite", "build": "tsc && vite build"}}', encoding="utf-8"
    )
    hints = run_hints(tmp_path)
    assert any(h == "npm run dev" for h in hints)


def test_run_hints_docker_compose(tmp_path):
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  web:\n    build: .\n", encoding="utf-8"
    )
    hints = run_hints(tmp_path)
    assert any("docker compose up" in h for h in hints)


def test_run_hints_requirements_txt(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n", encoding="utf-8")
    hints = run_hints(tmp_path)
    assert any(h.startswith("pip install -r requirements.txt") for h in hints)


def test_strip_readme_noise():
    noisy = "[![PyPI](https://img.shields.io/pypi/v/x.svg)](https://pypi.org/x)\n"
    noisy += '<p align="center"><img src="https://img.shields.io/badge/Python-3.11-3776AB"/></p>\n'
    noisy += "# Real title\nActual description here.\n"
    cleaned = strip_readme_noise(noisy)
    assert "shields.io" not in cleaned
    assert "# Real title" in cleaned and "Actual description here" in cleaned


def test_evidence_blocks_numbered():
    blocks = [("README", "line one"), ("Entry points", "app.main")]
    text = evidence_blocks(blocks)
    assert "--- E1: README ---" in text and "--- E2: Entry points ---" in text


def test_lint_citations(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    assert lint_citations("see [app.py:1] and [missing.py:3]", tmp_path) == ["missing.py:3"]
    assert lint_citations("no anchors here", tmp_path) == []


def test_repo_notes_steering(tmp_path):
    (tmp_path / "clio.json").write_text(
        '{"repo_notes": "This is a research project.", "run_commands": ["make all"]}',
        encoding="utf-8",
    )
    notes = load_repo_notes(tmp_path)
    assert notes["repo_notes"] == "This is a research project."
    assert notes["run_commands"] == ["make all"]


def test_repo_memory_text(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Run tests with pytest.\n", encoding="utf-8")
    assert "pytest" in repo_memory_text(tmp_path)